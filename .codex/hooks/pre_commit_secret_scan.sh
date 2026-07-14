#!/usr/bin/env bash
# PreToolUse(Bash) — before any Codex-issued git commit, scan both the current
# workspace and staged content. Missing tooling or an inconclusive scan blocks.

set -u

block() {
  printf 'BLOCKED by pre_commit_secret_scan: %s\n' "$1" >&2
  exit 2
}

scan() {
  label="$1"
  shift
  output="$("$@" 2>&1)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$output" | tail -25 >&2
    if [ "$rc" -eq 1 ]; then
      block "potential secret found in $label"
    fi
    block "gitleaks $label scan errored with exit code $rc"
  fi
}

command -v jq >/dev/null 2>&1 || block "jq is required; run scripts/bootstrap_codex.sh --check"

input_json="$(cat)"
cmd="$(printf '%s' "$input_json" | jq -er '.tool_input.command | select(type == "string")' 2>/dev/null)" || block "invalid hook input: command missing"
commit_pattern='(^|[^[:alnum:]_])git([[:space:]]+[^;&|[:space:]]+)*[[:space:]]+commit([[:space:]]|$)'
printf '%s' "$cmd" | grep -qE "$commit_pattern" || exit 0

command -v gitleaks >/dev/null 2>&1 || block "gitleaks is required before committing"

cwd="$(printf '%s' "$input_json" | jq -er '.cwd | select(type == "string" and length > 0)' 2>/dev/null)" || block "invalid hook input: cwd missing"
repo_root="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)" || block "cannot resolve repository root"

# Scan exactly the content that Git can commit while excluding ignored private
# state such as .env: unstaged tracked changes, non-ignored untracked files, and
# the index. Together these cover commit -a/pathspec and ordinary staged commits.
scan "unstaged tracked changes" gitleaks git "$repo_root" --pre-commit --no-banner --redact
while IFS= read -r -d '' relative_path; do
  scan "untracked file $relative_path" gitleaks dir "$repo_root/$relative_path" --no-banner --redact
done < <(git -C "$repo_root" ls-files --others --exclude-standard -z)
scan "staged changes" gitleaks git "$repo_root" --pre-commit --staged --no-banner --redact

exit 0
