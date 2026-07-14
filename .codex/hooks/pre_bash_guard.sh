#!/usr/bin/env bash
# PreToolUse(Bash) — fail closed on malformed input and block forbidden shell
# patterns before Codex executes them.

set -u

block() {
  printf 'BLOCKED by pre_bash_guard: %s\n' "$1" >&2
  exit 2
}

command -v jq >/dev/null 2>&1 || block "jq is required; run scripts/bootstrap_codex.sh --check"

input_json="$(cat)"
cmd="$(printf '%s' "$input_json" | jq -er '.tool_input.command | select(type == "string")' 2>/dev/null)" || block "invalid hook input: command missing"
[ -n "$cmd" ] || exit 0

# A direct git-rm command is the only sanctioned removal form. Detect direct,
# path-qualified, assignment-prefixed, shell-evaluated, and common wrapper
# invocations without treating quoted prose or search patterns as commands.
segment_start='(^|[;&|][[:space:]]*)[[:space:]]*'
assignment_prefix='([A-Za-z_][A-Za-z0-9_]*=[^[:space:];|&]+[[:space:]]+)*'
remove_pattern="${segment_start}${assignment_prefix}([\"']?)([^[:space:];|&]*/)?rm([[:space:]\"']|$)"
wrapped_remove_pattern="${segment_start}${assignment_prefix}([^[:space:];|&]*/)?(sudo|command|env|nice|nohup|time|xargs|exec|bash|sh|zsh|dash|eval)([[:space:]][^;&|]*)?[[:space:]\"']+([^[:space:];|&]*/)?rm([[:space:]\"']|$)"
if printf '%s\n' "$cmd" | grep -qE "$remove_pattern|$wrapped_remove_pattern"; then
  block "bare or wrapped rm is forbidden; use trash or a separate git rm command"
fi

download_pipe_pattern='(curl|wget)[^|;&]*\|[^|;&]*((^|[[:space:]])([^[:space:];|&]*/)?([[:alnum:]_-]*sh|fish))([[:space:]]|$)'
if printf '%s' "$cmd" | grep -qE "$download_pipe_pattern"; then
  block "download-to-shell execution is forbidden; download and inspect first"
fi

if printf '%s' "$cmd" | grep -qE '(pip3?|uv)[[:space:]]+(pip[[:space:]]+)?install[^;|&]*https?://'; then
  block "raw-URL package installation is forbidden"
fi

if printf '%s' "$cmd" | grep -q '&&'; then
  block "command chaining with double ampersands is forbidden; run commands separately"
fi

push_pattern='(^|[;&|][[:space:]]*)[[:space:]]*([^[:space:];|&]*/)?git([[:space:]][^;&|]*)?[[:space:]]+push([[:space:]]|$)'
force_flag_pattern='(^|[[:space:]])(--force([=-][^[:space:]]*)?|-[A-Za-z]*f[A-Za-z]*)([[:space:]]|$)'
target_branch_pattern='(^|[[:space:]:/])(main|master)([[:space:]]|$)'
delete_refspec_pattern='(^|[[:space:]]):(refs/heads/)?(main|master)([[:space:]]|$)'
if printf '%s' "$cmd" | grep -qE "$push_pattern"; then
  if printf '%s' "$cmd" | grep -qE "$force_flag_pattern"; then
    block "force-pushing is forbidden; push normally and use a squash merge"
  fi
  if printf '%s' "$cmd" | grep -qE '(^|[[:space:]])--mirror([[:space:]]|$)'; then
    block "mirror pushes are forbidden"
  fi
  if printf '%s' "$cmd" | grep -qE '(^|[[:space:]])\+[^[:space:]]+'; then
    block "forced refspecs are forbidden"
  fi
  if printf '%s' "$cmd" | grep -qE "$delete_refspec_pattern"; then
    block "deleting main or master by refspec is forbidden"
  fi
  if printf '%s' "$cmd" | grep -qE '(^|[[:space:]])(--delete|-d)([[:space:]]|$)'; then
    if printf '%s' "$cmd" | grep -qE "$target_branch_pattern"; then
      block "deleting main or master is forbidden"
    fi
  fi
fi

warning=""
if printf '%s' "$cmd" | grep -qE '(^|[;&|][[:space:]]*)(uv[[:space:]]+add|pip3?[[:space:]]+install)'; then
  warning="Dependency change detected; review maintenance, install scripts, the lockfile, and CVEs."
fi
if printf '%s' "$cmd" | grep -qE '>[[:space:]]*([^[:space:]]*/)?\.env([[:space:]]|$)'; then
  env_warning=".env must remain gitignored, mode 0600, absent from logs, and never be overwritten."
  if [ -n "$warning" ]; then
    warning="$warning $env_warning"
  else
    warning="$env_warning"
  fi
fi
if [ -n "$warning" ]; then
  jq -cn --arg message "$warning" '{systemMessage: $message}'
fi

exit 0
