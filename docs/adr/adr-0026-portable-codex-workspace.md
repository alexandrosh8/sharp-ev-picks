# ADR-0026: Portable repository-scoped Codex workspace

- **Status:** accepted
- **Date:** 2026-07-14
- **Deciders:** GodFather (Alexis) + Codex
- **Supersedes:** ADR-0003 for Codex sessions

## Context

Development must continue from a fresh Codex device without copying global
configuration, credentials, stale absolute paths, or ignored protocol state.
The prior hook design used Claude payload fields, a machine-specific project
path, parallel edit hooks, and a fail-open secret scan. A user-global formatter
also demonstrated that an empty/incorrect edit path can recursively rewrite a
repository.

## Decision

1. Version project instructions in `AGENTS.md`, skills in `.agents/skills/`,
   specialist agents in `.codex/agents/`, and bounded feature settings in
   `.codex/config.toml`.
2. Enable repository hooks from `.codex/hooks.json`; its top-level schema is
   exactly `{"hooks": ...}`. Commands resolve the Git root at runtime.
3. Parse Codex `apply_patch` paths from `tool_input.command`. One sequential
   PostToolUse hook formats/lints changed Python and runs tests for app/test
   changes; failures return bounded diagnostics on stderr.
4. PreToolUse blocks destructive shell patterns and scans both workspace and
   staged content before Codex-issued commits. Missing `jq`/`gitleaks` and scan
   errors fail closed. Manual verification and CI remain mandatory because
   terminal/IDE commits do not execute Codex hooks.
5. Successful informational hooks emit Codex JSON `systemMessage` on stdout.
6. Support macOS, Linux, and WSL. Native Windows would require equivalent
   PowerShell hooks through `commandWindows` and is not claimed here.
7. Bootstrap never imports or overwrites `.env`; authentication and secrets are
   recreated out of band. The original spent AH one-shot record is tracked in
   Git as protocol metadata.

## Consequences

- A clone carries the project behavior, agents, skills, and spent-domain guard.
- User auth, MCP/plugin login, databases, datasets, models, and `.env` remain
  outside Git and must be rebuilt or transferred privately.
- Project hook trust remains a per-device operator action through `/hooks`.
- The verification script mirrors CI's browser, coverage, lint, type,
  dependency, safety, and secret gates; migration checks additionally require a
  local `.env` or CI services.
- Global edit hooks must never infer a path from unsupported payload fields or
  run a formatter against a directory fallback.
