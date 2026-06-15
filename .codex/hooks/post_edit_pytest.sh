#!/bin/bash
# PostToolUse(Write|Edit) — run the test suite after edits to app/ or tests/
# Python files. Non-blocking (the edit already happened); failures surface to
# Claude via exit 2 + stderr. No-ops until .venv and tests/ exist.
# Reads the hook JSON on stdin: {"tool_input": {"file_path": "..."}}

input=$(cat)
f=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$f" ] && exit 0

proj="${CLAUDE_PROJECT_DIR:-$PWD}"

case "$f" in
  "$proj"/app/*.py|"$proj"/tests/*.py) ;;
  *) exit 0 ;;
esac

[ -x "$proj/.venv/bin/python" ] || exit 0
[ -d "$proj/tests" ] || exit 0

cd "$proj" || exit 0

runner=""
if command -v timeout >/dev/null 2>&1; then
  runner="timeout 120"
elif command -v gtimeout >/dev/null 2>&1; then
  runner="gtimeout 120"
fi

out=$($runner "$proj/.venv/bin/python" -m pytest -q -x -p no:cacheprovider tests/ 2>&1)
rc=$?

if [ "$rc" -ne 0 ]; then
  echo "pytest FAILED after editing $f (rc=$rc):" >&2
  printf '%s\n' "$out" | tail -30 >&2
  exit 2
fi

exit 0
