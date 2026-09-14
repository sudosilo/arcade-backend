# GREEN PHONE EDIT - arcade-backend-app-v2.py
# Changes from v1:
#   a global lock serializes every on chain write so two submissions
#   can never read the same nonce
#   a Redis lock per game blocks concurrent submissions across replicas
#   a Redis lock per player blocks rapid resubmission
#   nonce is tracked locally and reconciled against chain on failure
#   every submission is logged to Redis with its outcome for audit
#
# Environment variables in Railway:
#   CONTRACT_ADDRESS       deployed ArcadeVaultV2 address
#   OPERATOR_PRIVATE_KEY   operator wallet key, never in a file
#   POLYGON_RPC_URL        Alchemy endpoint
#   REDIS_URL              injected by the Redis plugin
#   TOKEN_TTL_SECONDS      token lifetime, default 900
#   SUBMIT_COOLDOWN_SECONDS  per player cooldown, default 30

import os
import json
import secrets
import time
import threading

from flask import Flask, request, jsonify
from web3 import Web3
import redis

app = Flask(__name__)

CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "")
OPERATOR_KEY     = os.environ.get("OPERATOR_PRIVATE_KEY", "")
POLYGON_RPC      = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
REDIS_URL        = os.environ.get("REDIS_URL", "redis://localhost:6379")
TOKEN_TTL        = int(os.environ.get("TOKEN_TTL_SECONDS", "900"))
SUBMIT_COOLDOWN  = int(os.environ.get("SUBMIT_COOLDOWN_SECONDS", "30"))

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
r  = redis.from_url(REDIS_URL, decode_responses=True)

# Serializes chain writes inside this process.
_chain_lock = threading.Lock()

# Locally tracked nonce. Reconciled against chain on every failure.
_nonce_state = {"value": None}

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
            {"name": "gameId", "type": "uint256"},
            {"name": "player", "type": "address"},
            {"name": "score",  "type": "uint256"}
        ],
        "outputs": []
    },
    {
        "name": "crownCooldownRemaining",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "gameId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}]
    },
    {
        "name": "getGame",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "gameId", "type": "uint256"}],
        "outputs": [
            {"name": "name",             "type": "string"},
            {"name": "prizePool",        "type": "uint256"},
            {"name": "highScore",        "type": "uint256"},
            {"name": "highScoreHolder",  "type": "address"},
            {"name": "consecutiveCount", "type": "uint256"},
            {"name": "lastCrownAt",      "type": "uint256"},
            {"name": "active",           "type": "bool"}
        ]
    }
]

with open("game_registry.json") as f:
    REGISTRY = json.load(f)


def get_game(game_id):
    for g in REGISTRY["games"]:
        if str(g["id"]) == str(game_id):
            return g
    return None


def contract():
    return w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACT_ADDRESS),
        abi=CONTRACT_ABI
    )


def audit(event, payload):
    """Append only log of every submission attempt and its outcome."""
    entry = dict(payload)
    entry["event"] = event
    entry["at"] = int(time.time())
    try:
        r.rpush("audit:submissions", json.dumps(entry))
        r.ltrim("audit:submissions", -5000, -1)
    except Exception:
        pass


def acquire_lock(key, ttl_seconds):
    """Redis lock that works across replicas. Returns a token or None."""
    token = secrets.token_urlsafe(16)
    if r.set(key, token, nx=True, ex=ttl_seconds):
        return token
    return None


def release_lock(key, token):
    """Only release if this process still owns the lock."""
    try:
        if r.get(key) == token:
            r.delete(key)
    except Exception:
        pass


def next_nonce(address):
    """
    Returns the nonce to use. Called only while holding _chain_lock.
    Uses pending count so queued transactions are accounted for.
    """
    chain_nonce = w3.eth.get_transaction_count(address, "pending")
    local = _nonce_state["value"]

    if local is None or chain_nonce > local:
        _nonce_state["value"] = chain_nonce
        return chain_nonce

    return local


def bump_nonce():
    if _nonce_state["value"] is not None:
        _nonce_state["value"] += 1


def reset_nonce():
    _nonce_state["value"] = None


def verify_tx(tx_hash, expected_game_id):
    try:
        tx      = w3.eth.get_transaction(tx_hash)
        receipt = w3.eth.get_transaction_receipt(tx_hash)

        if receipt is None:
            return None, "transaction is not yet confirmed"
        if receipt["status"] != 1:
            return None, "transaction reverted on chain"
        if tx["to"].lower() != CONTRACT_ADDRESS.lower():
            return None, "transaction was not sent to the arcade contract"

        func, args = contract().decode_function_input(tx["input"])

        if func.fn_name != "playGame":
            return None, "transaction is not a playGame call"
        if str(args["gameId"]) != str(expected_game_id):
            return None, "game id in transaction does not match requested game"

        return tx["from"], None

    except Exception as e:
        return None, str(e)


def submit_score_to_chain(game_id, player_address, score):
    """
    Single point of on chain write. The process lock guarantees that
    no two calls read the same nonce.
    """
    with _chain_lock:
        try:
            operator = w3.eth.account.from_key(OPERATOR_KEY)
            nonce = next_nonce(operator.address)

            tx = contract().functions.submitScore(
                int(game_id),
                Web3.to_checksum_address(player_address),
                int(score)
            ).build_transaction({
                "from":     operator.address,
                "nonce":    nonce,
                "gas":      300000,
                "gasPrice": w3.eth.gas_price
            })

            signed  = operator.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

            bump_nonce()

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)

            if receipt["status"] != 1:
                reset_nonce()
                return None, "submitScore transaction reverted"

            return tx_hash.hex(), None

        except Exception as e:
            # Any failure invalidates the local nonce. Next call re-reads chain.
            reset_nonce()
            return None, str(e)


@app.route("/health", methods=["GET"])
def health():
    try:
        return jsonify({
            "status":   "ok",
            "block":    w3.eth.block_number,
            "redis":    r.ping(),
            "contract": CONTRACT_ADDRESS or "not set",
            "cooldown": SUBMIT_COOLDOWN
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/verify-tx", methods=["POST"])
def verify_tx_endpoint():
    data    = request.get_json(silent=True) or {}
    tx_hash = data.get("txHash", "").strip()
    game_id = data.get("gameId", "").strip()

    if not tx_hash or not game_id:
        return jsonify({"message": "txHash and gameId are required"}), 400

    if not get_game(game_id):
        return jsonify({"message": "unrecognized game id"}), 400

    used_key = "used_tx:" + tx_hash

    # Atomic claim. Two simultaneous requests cannot both win this.
    claim = acquire_lock(used_key, 86400)
    if claim is None:
        return jsonify({"message": "this transaction has already been redeemed"}), 400

    player, error = verify_tx(tx_hash, game_id)
    if error:
        release_lock(used_key, claim)
        return jsonify({"message": error}), 400

    r.setex(used_key, 86400, player)

    token = secrets.token_urlsafe(32)
    r.setex("token:" + token, TOKEN_TTL, json.dumps({
        "player":   player,
        "gameId":   str(game_id),
        "txHash":   tx_hash,
        "issuedAt": int(time.time())
    }))

    return jsonify({"token": token})


@app.route("/submit-score", methods=["POST"])
def submit_score_endpoint():
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
        return jsonify({"message": "score must be a non negative integer"}), 400

    game = get_game(game_id)
    if not game:
        return jsonify({"message": "unrecognized game id"}), 400

    if score > game.get("max_score", 9999999):
        return jsonify({"message": "score is outside valid range for this game"}), 400

    raw = r.get("token:" + token)
    if not raw:
        return jsonify({"message": "token not found or expired"}), 401

    token_data = json.loads(raw)
    if str(token_data["gameId"]) != str(game_id):
        return jsonify({"message": "token does not match this game"}), 401

    player = token_data["player"]

    # Burn the token first. A replay of this request finds nothing.
    r.delete("token:" + token)

    # Per player cooldown. Blocks rapid fire submissions from one wallet.
    cooldown_key = "cooldown:" + player.lower()
    cd = acquire_lock(cooldown_key, SUBMIT_COOLDOWN)
    if cd is None:
        ttl = r.ttl(cooldown_key)
        audit("rejected_cooldown", {
            "player": player, "gameId": game_id,
            "score": score, "retryAfter": ttl
        })
        return jsonify({
            "message": "submission cooldown active for this wallet",
            "retryAfterSeconds": ttl if ttl and ttl > 0 else SUBMIT_COOLDOWN
        }), 429

    # Per game lock. Only one submission per game is in flight at a time,
    # including across multiple Railway replicas.
    game_key = "submitting:game:" + str(game_id)
    gl = acquire_lock(game_key, 120)
    if gl is None:
        audit("rejected_game_busy", {
            "player": player, "gameId": game_id, "score": score
        })
        return jsonify({
            "message": "another submission for this game is in flight",
            "retryAfterSeconds": 5
        }), 409

    try:
        audit("attempt", {
            "player": player, "gameId": game_id,
            "score": score, "txHash": token_data.get("txHash")
        })

        tx_hex, error = submit_score_to_chain(game_id, player, score)

        if error:
            audit("failed", {
                "player": player, "gameId": game_id,
                "score": score, "detail": error
            })
            return jsonify({
                "message": "on chain score submission failed",
                "detail": error
            }), 500

        audit("confirmed", {
            "player": player, "gameId": game_id,
            "score": score, "txHash": tx_hex
        })

        return jsonify({
            "submitted": True,
            "score":     score,
            "player":    player,
            "txHash":    tx_hex
        })

    finally:
        release_lock(game_key, gl)


@app.route("/audit", methods=["GET"])
def audit_log():
    """Recent submission attempts with outcomes. Read only."""
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 1000))

    entries = r.lrange("audit:submissions", -limit, -1)
    return jsonify({
        "count":   len(entries),
        "entries": [json.loads(e) for e in entries]
    })


@app.route("/game-state/<game_id>", methods=["GET"])
def game_state(game_id):
    """Live contract state for one game, including cooldown remaining."""
    if not get_game(game_id):
        return jsonify({"message": "unrecognized game id"}), 400

    try:
        g = contract().functions.getGame(int(game_id)).call()
        remaining = contract().functions.crownCooldownRemaining(int(game_id)).call()

        return jsonify({
            "gameId":            int(game_id),
            "name":              g[0],
            "prizePoolWei":      str(g[1]),
            "highScore":         g[2],
            "highScoreHolder":   g[3],
            "consecutiveCount":  g[4],
            "lastCrownAt":       g[5],
            "active":            g[6],
            "cooldownRemaining": remaining
        })
    except Exception as e:
        return jsonify({"message": "could not read contract", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
