#!/bin/bash
# Stop hook — gentle reminder to persist decisions before the session ends.
# Informational only; never blocks.

proj="${CLAUDE_PROJECT_DIR:-$PWD}"

echo "Reminder: if decisions were made this session, update .claude/memory/ (decisions.md / pitfalls.md / data-sources.md) and add an ADR in docs/adr/ for anything significant. Memory must never contain secrets." >&2

exit 0
