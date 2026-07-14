#!/bin/bash
# PreToolUse(Bash) secret gate — when the command is a `git commit`, scan the
# staged changes with gitleaks. Leaks => exit 2 (block, fail closed).
# gitleaks missing => warn + allow (fail open, documented in ADR-0003).
# Reads the hook JSON on stdin: {"tool_input": {"command": "..."}}

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$cmd" ] && exit 0

# Only act on git commit commands
printf '%s' "$cmd" | grep -qE '(^|[;&|][[:space:]]*)git[[:space:]]+([-A-Za-z0-9=. /"]*[[:space:]])?commit' || exit 0

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "WARN: gitleaks not installed — staged secret scan SKIPPED. Install gitleaks (brew install gitleaks)." >&2
  exit 0
fi

proj="${CLAUDE_PROJECT_DIR:-$PWD}"
cd "$proj" || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

out=$(gitleaks git --pre-commit --staged --no-banner --redact 2>&1)
rc=$?

if [ "$rc" -eq 1 ]; then
  echo "BLOCKED by pre_commit_secret_scan: gitleaks found potential secrets in staged changes:" >&2
  printf '%s\n' "$out" | tail -25 >&2
  echo "Remove the secret, then re-stage. Never commit credentials." >&2
  exit 2
elif [ "$rc" -ne 0 ]; then
  echo "WARN: gitleaks errored (rc=$rc) — scan inconclusive, allowing commit. Output tail:" >&2
  printf '%s\n' "$out" | tail -5 >&2
fi

exit 0
