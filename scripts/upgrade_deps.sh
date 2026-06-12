#!/usr/bin/env bash
# Tested upgrade of the bound upstream engines (penaltyblog, oddsharvester).
#
# Bumps the lockfile, reinstalls, then runs the FULL gate: pytest (incl. the
# penaltyblog devig-parity tests and the AH-bridge tests that pin upstream
# behavior), ruff, mypy, and the no-autobet safety audit. If ANY gate fails,
# the previous lockfile is restored and re-synced — the platform never ends
# up running an unverified dependency.
#
# Never commits: review `git diff uv.lock`, then commit yourself.
# This is the only sanctioned upgrade path — fully automatic updates are
# deliberately not wired (a release can change devig numbers or scraper
# behavior under the live engine; CLAUDE.md: never commit untested code).

set -u

# Derive the repo from the script location (like safety_audit.sh) — this
# script also runs on the VPS clone at /opt/betting-ai; never hardcode a path.
cd "$(dirname "$0")/.." || exit 1
REPO="$(pwd)"

BACKUP="$(mktemp /tmp/uv.lock.backup.XXXXXX)"
cp "$REPO/uv.lock" "$BACKUP" || exit 1
echo "[upgrade] previous lockfile backed up to $BACKUP"

restore() {
  echo
  echo "[upgrade] GATE FAILED: $1 — restoring previous lockfile"
  cp "$BACKUP" "$REPO/uv.lock"
  uv sync --extra football --extra backfill
  echo "[upgrade] rolled back; the platform still runs the previous versions"
  exit 1
}

uv lock --upgrade-package penaltyblog --upgrade-package oddsharvester
if [ $? -ne 0 ]; then restore "uv lock"; fi

if git diff --quiet -- uv.lock; then
  echo "[upgrade] already at the latest releases — nothing to do"
  exit 0
fi

uv sync --extra football --extra backfill
if [ $? -ne 0 ]; then restore "uv sync"; fi

uv run pytest -q
if [ $? -ne 0 ]; then restore "pytest"; fi

uvx ruff check app tests scripts
if [ $? -ne 0 ]; then restore "ruff"; fi

uv run mypy app tests
if [ $? -ne 0 ]; then restore "mypy"; fi

bash scripts/safety_audit.sh
if [ $? -ne 0 ]; then restore "safety audit"; fi

echo
echo "[upgrade] GREEN — all gates passed on the new versions:"
uv run python - << 'PY'
from importlib import metadata

for pkg in ("penaltyblog", "oddsharvester"):
    print(f"  {pkg} {metadata.version(pkg)}")
PY
echo
echo "[upgrade] review:  git diff uv.lock"
echo "[upgrade] commit:  git add uv.lock   (then)   git commit -m 'chore: bump upstream engines (gated)'"
echo "[upgrade] restart the app to run the new versions"
echo "[upgrade] Docker: rebuild the image (docker compose up -d --build) — and"
echo "[upgrade]   re-verify the Dockerfile's oddsharvester sandbox note: 0.3.0"
echo "[upgrade]   launches Chromium with --no-sandbox/--disable-dev-shm-usage"
echo "[upgrade]   when /.dockerenv exists; a bump must keep that behavior."
