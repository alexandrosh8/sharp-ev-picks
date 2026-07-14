#!/usr/bin/env bash
# PostToolUse(Write|Edit) — Codex serializes apply_patch text at
# tool_input.command. Format and lint changed Python files, then run the
# regression suite once when app/ or tests/ Python changed.

set -u

fail() {
  printf 'post_edit_validate: %s\n' "$1" >&2
  exit 2
}

run_checked() {
  label="$1"
  shift
  output="$("$@" 2>&1)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$output" | tail -80 >&2
    fail "$label failed"
  fi
}

command -v jq >/dev/null 2>&1 || fail "jq is required; run scripts/bootstrap_codex.sh --check"

input_json="$(cat)"
cwd="$(printf '%s' "$input_json" | jq -er '.cwd | select(type == "string" and length > 0)' 2>/dev/null)" || fail "invalid hook input: cwd missing"
patch="$(printf '%s' "$input_json" | jq -er '.tool_input.command | select(type == "string")' 2>/dev/null)" || fail "invalid hook input: patch command missing"
repo_root="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)" || fail "cannot resolve repository root from hook cwd"
python_bin="$repo_root/.venv/bin/python"

[ -x "$python_bin" ] || exit 0

changed_paths="$(
  printf '%s\n' "$patch" \
    | sed -nE 's/^\*\*\* (Add|Update|Delete) File: (.*)$/\2/p; s/^\*\*\* Move to: (.*)$/\1/p'
)"
[ -n "$changed_paths" ] || exit 0

needs_tests=0
while IFS= read -r supplied_path; do
  [ -n "$supplied_path" ] || continue
  case "$supplied_path" in
    "$repo_root"/*)
      relative_path="${supplied_path#"$repo_root"/}"
      ;;
    /*)
      fail "refusing absolute path outside repository: $supplied_path"
      ;;
    *)
      relative_path="$supplied_path"
      ;;
  esac
  case "$relative_path" in
    ""|..|../*|*/../*)
      fail "refusing path outside repository: $supplied_path"
      ;;
  esac

  absolute_path="$repo_root/$relative_path"
  case "$relative_path" in
    *.py)
      if [ -f "$absolute_path" ]; then
        run_checked "ruff format for $relative_path" "$python_bin" -m ruff format "$absolute_path"
        run_checked "ruff check for $relative_path" "$python_bin" -m ruff check "$absolute_path"
      fi
      ;;
  esac
  case "$relative_path" in
    app/*.py|tests/*.py)
      needs_tests=1
      ;;
  esac
done <<EOF_PATHS
$changed_paths
EOF_PATHS

if [ "$needs_tests" -eq 1 ]; then
  run_checked "pytest" "$python_bin" -m pytest -q -x -p no:cacheprovider "$repo_root/tests"
fi

exit 0
