#!/usr/bin/env bash
# Container entrypoint: migrate, then serve. Referenced by the Dockerfile
# ENTRYPOINT — not meant to be run on a host.
#
# - `alembic upgrade head` is idempotent (no-op at head), so restarts are
#   safe. It runs BEFORE uvicorn so the scheduler never polls an unmigrated
#   database (first boot, or after a schema-changing `git pull` + rebuild).
# - Migration race: none by design — this platform runs exactly ONE app
#   instance (APScheduler in-process, ADR-0007; in-memory exposure ledger and
#   odds_seen cache assume one process). Never `--scale app=2`.
# - Venv binaries are called DIRECTLY (no `uv run`): a plain `uv run` would
#   re-sync the venv at container start and uninstall the build-time extras
#   (football/backfill). alembic/env.py reads DATABASE_URL from Settings, so
#   the in-container URL injected by compose is picked up with no extra wiring.
# - The bounded retry covers daemon-restart ordering (postgres not yet
#   accepting connections); `depends_on: service_healthy` covers normal `up`.

set -euo pipefail

VENV="/srv/betting-ai/.venv"

attempts=0
max_attempts=10
until "$VENV/bin/alembic" upgrade head; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge "$max_attempts" ]; then
    echo "[entrypoint] alembic upgrade head failed after ${max_attempts} attempts — exiting" >&2
    exit 1
  fi
  echo "[entrypoint] database not ready (attempt ${attempts}/${max_attempts}); retrying in 3s" >&2
  sleep 3
done

echo "[entrypoint] migrations at head — starting uvicorn"
exec "$VENV/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
