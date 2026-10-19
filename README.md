Arcade Backend

Arcade Backend is the score server for the arcade games. It is for games that want to post verified high scores to a blockchain vault.

It issues short lived tokens, checks scores against a per game limit, and writes accepted scores on chain. It has routes for health, transaction checks, score submission, audit, and game state. Games are listed in a registry file. It runs on Railway with a health check.

This app is checked every morning by Control Troll.

Latest change. Version 2 now locks every on chain write so two scores can never reuse the same number. Each game and each player has a lock, and players wait through a cooldown between submissions. Every submission is logged with its outcome for audit.

See CHANGELOG.md for the full history.
