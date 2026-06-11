#!/usr/bin/env bash
# The one true way to start the platform: frees port 8000 first (kills any
# previous instance, whoever started it), then runs uvicorn in THIS terminal.
# Fixes the recurring "[Errno 48] address already in use" ping-pong.

REPO="/Users/alexis/code/Betting Picks Bot"
cd "$REPO" || exit 1

pkill -f "uvicorn app.main" 2>/dev/null
sleep 1

exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
