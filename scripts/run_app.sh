#!/usr/bin/env bash
# MAC DEV ONLY — never run this on the VPS. In production the app lives in
# the compose stack (docs/deployment/openclaw-ubuntu.md): :8000 there is the
# container's published port, and lsof is not installed on a stock Ubuntu
# server anyway.
#
# The one true way to start the platform locally: frees port 8000 first, then
# runs uvicorn in THIS terminal. Fixes the recurring "[Errno 48] address
# already in use" ping-pong.
#
# The kill is PORT-scoped (lsof -ti :8000), never a name-matched pkill —
# 'pkill -f "uvicorn app.main"' could kill another project's uvicorn whose
# module path merely looks the same.

REPO="/Users/alexis/code/Betting Picks Bot"
cd "$REPO" || exit 1

PIDS="$(lsof -ti :8000)"
if [ -n "$PIDS" ]; then
  # shellcheck disable=SC2086 — word-splitting over the PID list is intended
  kill $PIDS 2>/dev/null
  sleep 1
fi

exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
