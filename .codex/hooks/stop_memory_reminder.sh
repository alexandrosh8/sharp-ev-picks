#!/usr/bin/env bash
# Stop — informational reminder only. Successful Codex hooks surface JSON on
# stdout; stderr is discarded.

printf '%s\n' '{"systemMessage":"Reminder: update the Codex handoff and durable memory for meaningful decisions; never record secrets."}'
exit 0
