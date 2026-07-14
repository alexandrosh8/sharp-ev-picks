#!/usr/bin/env bash
# Mac development launcher. Production runs the app in the Compose stack.

set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1; pwd -P)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)" || {
  printf '%s\n' 'run_app: cannot resolve the Git repository root' >&2
  exit 1
}
python_bin="$repo_root/.venv/bin/python"

if [ ! -x "$python_bin" ]; then
  printf '%s\n' "Missing $python_bin; run scripts/bootstrap_codex.sh first." >&2
  exit 1
fi
if ! command -v lsof >/dev/null 2>&1; then
  printf '%s\n' 'run_app: lsof is required for the Mac port guard' >&2
  exit 1
fi

pids="$(lsof -ti :8000 || true)"
if [ -n "$pids" ]; then
  # Word splitting over the newline-delimited PID list is intentional.
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
fi

cd "$repo_root"
exec "$python_bin" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
