#!/bin/bash
# PostToolUse(Write|Edit) — format Python files with ruff (best-effort).
# Markdown/JSON are already covered by the global prettier hook.
# Reads the hook JSON on stdin: {"tool_input": {"file_path": "..."}}

input=$(cat)
f=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$f" ] && exit 0

case "$f" in
  *.py)
    if [ -f "$f" ]; then
      if command -v ruff >/dev/null 2>&1; then
        ruff format "$f" >/dev/null 2>&1 || true
      elif command -v uvx >/dev/null 2>&1; then
        uvx ruff format "$f" >/dev/null 2>&1 || true
      fi
    fi
    ;;
esac

exit 0
