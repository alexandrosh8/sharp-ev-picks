# ADR-0001: Memory System — Project-Local Markdown, External Memory Tools Rejected

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

The master requirements ask whether to install `claude-mem`,
`claude-memory-compiler`, another memory tool, or use project-local markdown
memory. Hard constraint: memory must never store API keys, betting
credentials, tokens, cookies, or `.env` values — and this project's sessions
routinely reference odds-API keys, so any system that captures raw session
content is a secret-leak surface. Both candidates were deep-inspected
file-by-file via GitHub MCP on 2026-06-10 (see
`docs/research/claude-environment-research-log.md`).

## Decision

Use **project-local markdown memory**:

- Canonical store: `.claude/memory/` in-repo (MEMORY.md index + decisions /
  data-sources / modeling-notes / pitfalls), git-versioned — survives the
  Mac→Ubuntu migration with the repo.
- Auto-loaded stub: `~/.claude/projects/-Users-alexis-code-Betting-Picks-Bot/memory/MEMORY.md`
  pointing at the canonical store.
- Formal decisions: `docs/adr/` (this numbering).
- A Stop-hook reminder (`stop_memory_reminder.sh`) prompts updates after
  decision-heavy sessions; writing memory stays a deliberate, reviewed act.

**claude-mem: not installed (PATTERN-ONLY).** Evidence: wildcard PostToolUse
hook persists raw prompts and tool I/O into SQLite with no secret redaction
(only `<private>`-tag stripping); transcript-derived content goes to the model
provider by design; PostHog telemetry is default-on while its SECURITY.md
claims no telemetry; the plugin ships multi-MB compiled bundles and
auto-installs Bun/uv (not reviewable-plain-files); beta self-update channel.
Apache-2.0 and very active — but it fails the no-raw-transcript and
reviewable-install criteria outright for this project.

**claude-memory-compiler: not installed (PATTERN-ONLY).** Evidence: **no
license at all** (LICENSE fetch 404) — code is legally unreusable; verbatim
30-turn transcript excerpts written to plaintext temp files with no
redaction (orphaned if the flush process dies); spawns an unsupervised
background agent with `acceptEdits` writing files; single-drop repo with
zero maintenance since 2026-04-06.

## Justification

The markdown system already proved itself across sibling projects (kestrel,
polymarket), satisfies the no-secrets constraint by construction (human-curated
entries, gitleaks scans the committed memory files), and costs nothing to
operate. Neither tool's genuine advantage (automatic mid-session capture +
post-compaction re-injection) outweighs an unredacted capture pipeline in a
project whose sessions handle betting API keys.

## Overturn criteria

Revisit only if a memory tool satisfies ALL of: OSI license; reviewable
plain-file install; no transcript/session egress beyond the user's own model
provider; built-in secret-pattern redaction BEFORE storage; clean uninstall.
If we ever want compaction-survival, re-implement locally: SessionStart hook
matched on `compact|clear` injecting a digest of `.claude/memory/MEMORY.md`,
plus an explicitly redacting capture hook (patterns documented in the
research log).

## Alternatives considered

- claude-mem — rejected (above).
- claude-memory-compiler — rejected (above).
- MCP knowledge-graph memory server — already available in-session; useful as
  scratch, but not canonical: not git-versioned with the repo and not
  reviewable in PRs.

## Consequences

- Memory updates are manual (reminder-assisted) — slower than automatic
  capture, but every byte is reviewed before it persists.
- Compaction can lose unsaved session nuance; mitigate by updating memory at
  decision points (CLAUDE.md rule).
