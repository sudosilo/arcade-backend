# GREEN PHONE EDIT - arcade-backend-app-v1.py
# Railway Python server for the arcade bounty system
# Handles on-chain payment verification, one-time token issuance, and score submission
#
# Required environment variables in Railway:
#   CONTRACT_ADDRESS       deployed ArcadeVault contract on Polygon
#   OPERATOR_PRIVATE_KEY   wallet private key for submitScore calls (NEVER commit this)
#   POLYGON_RPC_URL        Polygon RPC endpoint (public or Alchemy/QuickNode)
#   REDIS_URL              Railway Redis plugin provides this automatically
#   TOKEN_TTL_SECONDS      seconds a token stays valid before expiring (default 900 = 15 min)
#   PORT                   Railway sets this automatically

import os
import json
import secrets
import time
from flask import Flask, request, jsonify
from web3 import Web3
import redis

app = Flask(__name__)

CONTRACT_ADDRESS   = os.environ.get("CONTRACT_ADDRESS", "")
OPERATOR_KEY       = os.environ.get("OPERATOR_PRIVATE_KEY", "")
POLYGON_RPC        = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
REDIS_URL          = os.environ.get("REDIS_URL", "redis://localhost:6379")
TOKEN_TTL          = int(os.environ.get("TOKEN_TTL_SECONDS", "900"))

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
r  = redis.from_url(REDIS_URL, decode_responses=True)

# GREEN PHONE EDIT - only the functions the backend needs to call or decode
CONTRACT_ABI = [
    {
        "name": "playGame",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "gameId", "type": "uint256"}],
        "outputs": []
    },
    {
        "name": "submitScore",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "gameId",  "type": "uint256"},
            {"name": "player",  "type": "address"},
            {"name": "score",   "type": "uint256"}
        ],
        "outputs": []
    }
]

# GREEN PHONE EDIT - load game registry from local file
with open("game_registry.json") as f:
    REGISTRY = json.load(f)

def get_game(game_id):
    for g in REGISTRY["games"]:
        if str(g["id"]) == str(game_id):
            return g
    return None

def verify_tx(tx_hash, expected_game_id):
    """
    Confirms that tx_hash is a real confirmed call to playGame on our contract
    with the correct gameId. Returns (player_address, None) on success
    or (None, error_string) on failure.
    """
    try:
        tx      = w3.eth.get_transaction(tx_hash)
        receipt = w3.eth.get_transaction_receipt(tx_hash)

        if receipt is None:
            return None, "transaction is not yet confirmed"

        if receipt["status"] != 1:
            return None, "transaction reverted on-chain"

        if tx["to"].lower() != CONTRACT_ADDRESS.lower():
            return None, "transaction was not sent to the arcade contract"

        contract       = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=CONTRACT_ABI)
        func, args     = contract.decode_function_input(tx["input"])

        if func.fn_name != "playGame":
            return None, "transaction is not a playGame call"

        if str(args["gameId"]) != str(expected_game_id):
            return None, "game id in transaction does not match requested game"

        return tx["from"], None

    except Exception as e:
        return None, str(e)

def submit_score_to_chain(game_id, player_address, score):
    """
    Calls submitScore on the contract as the operator wallet.
    Returns (tx_hash_hex, None) on success or (None, error_string) on failure.
    """
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=CONTRACT_ABI)
        operator = w3.eth.account.from_key(OPERATOR_KEY)

        tx = contract.functions.submitScore(
            int(game_id),
            Web3.to_checksum_address(player_address),
            int(score)
        ).build_transaction({
            "from":     operator.address,
            "nonce":    w3.eth.get_transaction_count(operator.address),
            "gas":      200000,
            "gasPrice": w3.eth.gas_price
        })

        signed  = operator.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] != 1:
            return None, "submitScore transaction reverted"

        return tx_hash.hex(), None

    except Exception as e:
        return None, str(e)


@app.route("/health", methods=["GET"])
def health():
    try:
        block = w3.eth.block_number
        redis_ok = r.ping()
        return jsonify({
            "status":   "ok",
            "block":    block,
            "redis":    redis_ok,
            "contract": CONTRACT_ADDRESS or "not set"
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/verify-tx", methods=["POST"])
def verify_tx_endpoint():
    """
    Called by the token bridge snippet in each game.
    Receives txHash and gameId, verifies the payment on Polygon,
    and returns a one-time token the game uses to initialize.
    """
    data    = request.get_json(silent=True) or {}
    tx_hash = data.get("txHash", "").strip()
    game_id = data.get("gameId", "").strip()

    if not tx_hash or not game_id:
        return jsonify({"message": "txHash and gameId are required"}), 400

    game = get_game(game_id)
    if not game:
        return jsonify({"message": "unrecognized game id"}), 400

    # reject replayed transactions
    used_key = "used_tx:" + tx_hash
    if r.get(used_key):
        return jsonify({"message": "this transaction has already been redeemed"}), 400

    player, error = verify_tx(tx_hash, game_id)
    if error:
        return jsonify({"message": error}), 400

    # mark transaction consumed for 24 hours
    r.setex(used_key, 86400, player)

    # issue one-time token
    token     = secrets.token_urlsafe(32)
    token_key = "token:" + token
    r.setex(token_key, TOKEN_TTL, json.dumps({
        "player":   player,
        "gameId":   str(game_id),
        "txHash":   tx_hash,
        "issuedAt": int(time.time())
    }))

    return jsonify({"token": token})


@app.route("/submit-score", methods=["POST"])
def submit_score_endpoint():
    """
    Called by window.arcadeSubmitScore() at the end of each game session.
    Validates the one-time token, burns it, then calls submitScore on the contract.
    The contract handles bounty distribution if the player achieves three consecutive highs.
    """
    data    = request.get_json(silent=True) or {}
    token   = data.get("token", "").strip()
    game_id = data.get("gameId", "").strip()
    score   = data.get("score")

    if not token or not game_id or score is None:
        return jsonify({"message": "token, gameId, and score are required"}), 400

    try:
        score = int(score)
        if score < 0:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({"message": "score must be a non-negative integer"}), 400

    game = get_game(game_id)
    if not game:
        return jsonify({"message": "unrecognized game id"}), 400

    if score > game.get("max_score", 9999999):
        return jsonify({"message": "score is outside valid range for this game"}), 400

    token_key = "token:" + token
    raw = r.get(token_key)
    if not raw:
        return jsonify({"message": "token not found or expired"}), 401

    token_data = json.loads(raw)
    if str(token_data["gameId"]) != str(game_id):
        return jsonify({"message": "token does not match this game"}), 401

    # burn the token before making the chain call
    r.delete(token_key)

    player = token_data["player"]
    tx_hex, error = submit_score_to_chain(game_id, player, score)
    if error:
        return jsonify({"message": "on-chain score submission failed", "detail": error}), 500

    return jsonify({
        "submitted": True,
        "score":     score,
        "player":    player,
        "txHash":    tx_hex
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
