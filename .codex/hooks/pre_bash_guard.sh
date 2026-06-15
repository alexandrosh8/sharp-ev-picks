#!/bin/bash
# PreToolUse(Bash) guard — blocks destructive/forbidden shell patterns (exit 2),
# warns on patterns that deserve review (exit 0 + stderr note).
# Reads the hook JSON on stdin: {"tool_input": {"command": "..."}}

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$cmd" ] && exit 0

block() {
  echo "BLOCKED by pre_bash_guard: $1" >&2
  exit 2
}

# rm -rf / rm -fr (any flag order containing both r and f)
if printf '%s' "$cmd" | grep -qE '(^|[;&|[:space:]])rm[[:space:]]+-[A-Za-z]*([rR][A-Za-z]*f|f[A-Za-z]*[rR])'; then
  block "'rm -rf' is forbidden. Use 'trash' instead."
fi

# bare rm as a command (git rm is allowed; 'trash' is the sanctioned tool)
if printf '%s' "$cmd" | grep -qE '(^|[;&|][[:space:]]*)rm[[:space:]]'; then
  block "bare 'rm' is forbidden. Use 'trash' (or 'git rm' for tracked files)."
fi

# curl|sh / wget|sh — remote code execution
if printf '%s' "$cmd" | grep -qE '(curl|wget)[^|;&]*\|[[:space:]]*(ba|z|da)?sh'; then
  block "piping a download into a shell is forbidden. Download, inspect, then run."
fi

# pip/uv install directly from a URL
if printf '%s' "$cmd" | grep -qE '(pip3?|uv)[[:space:]]+(pip[[:space:]]+)?install[^;|&]*https?://'; then
  block "installing packages from raw URLs is forbidden. Use a registry package and review it."
fi

# && chains (user hard rule: separate commands only)
if printf '%s' "$cmd" | grep -q '&&'; then
  block "'&&' chains are forbidden by project rules. Run commands separately."
fi

# force-push to main/master
if printf '%s' "$cmd" | grep -qE 'git[[:space:]]+push[^;|&]*(--force|-f)[^;|&]*(main|master)|git[[:space:]]+push[^;|&]*(main|master)[^;|&]*(--force|-f)'; then
  block "force-pushing to main/master is forbidden."
fi

# Warn-only: dependency installs deserve a review note
if printf '%s' "$cmd" | grep -qE '(^|[;&|][[:space:]]*)(uv[[:space:]]+add|pip3?[[:space:]]+install)'; then
  echo "NOTE: dependency change detected — review maintenance/install scripts/CVEs before relying on it (security-review skill)." >&2
fi

# Warn-only: redirecting into .env
if printf '%s' "$cmd" | grep -qE '>[[:space:]]*\.env([[:space:]]|$)'; then
  echo "NOTE: writing to .env — ensure no secrets end up in logs or git; .env must stay gitignored and 0600." >&2
fi

exit 0
