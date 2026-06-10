# Claude Environment Research Log

- **Date:** 2026-06-10
- **Method:** Repository-grounded inspection via the plugin GitHub MCP server
  (`mcp__plugin_everything-claude-code_github__*`). The standalone `github`
  MCP server failed authentication (bad `GITHUB_PERSONAL_ACCESS_TOKEN`) and
  was not used. Three parallel read-only research agents; every claim below
  comes from actually-fetched files (fetch failures are recorded as such).
- **Scope rationale (delta pass):** obra/superpowers (plugin v5.0.7, 14
  skills) and all 10 VoltAgent subagent packs were already installed and
  enabled before this project; betting-domain skills (kelly-bankroll,
  walkforward-backtest, clv-evaluation, calibration-eval,
  betting-feature-engineering) already exist globally. Research therefore
  deep-dived only the two memory tools (install-vs-reject decision) and
  skimmed the awesome-lists for genuinely missing capabilities.

## Acceptance criteria (a USE verdict required ALL)

1. Pushed within 18 months. 2. OSI license. 3. Install is reviewable plain
   files (no curl|sh, no remote-fetching install steps, no opaque compiled
   bundles). 4. No mechanism sending transcript/session-derived data to external
   services. 5. Fills a gap the installed tooling doesn't cover. Memory tools
   additionally: must not persist raw transcript content (betting sessions
   reference API keys — transcript stores are a secret-leak surface).

## Verdict table

| Repository                              | Purpose                                                                                                                  | Activity (last push)                             | License                                  | Provides                                                                                                    | Install method                                                                                                               | Security concerns                                                                                                                                                                                                                                        | Verdict                              | Reason                                                                                                                                                                                                                                                                                                                |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------ | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| thedotmack/claude-mem                   | Hook-based memory: captures every tool call, AI-compresses to SQLite+FTS5+Chroma, re-injects on start/clear/compact      | 2026-06-10 (very active, v13.5.4)                | Apache-2.0                               | Compaction survival, 3-layer progressive-disclosure search, web viewer                                      | npx / plugin marketplace; ships multi-MB compiled bundles; auto-installs Bun+uv; beta self-update; curl\|sh on OpenClaw path | Wildcard PostToolUse stores raw prompts + tool I/O with NO secret redaction; sends transcript context to model provider by design; PostHog telemetry default-ON (metadata-only, contradicts its own SECURITY.md); reads Claude OAuth token from keychain | **PATTERN-ONLY**                     | Fails criteria 3+4; raw-capture without redaction is unacceptable for a project whose sessions reference betting API keys. Patterns worth re-implementing locally: SessionStart `compact\|clear` re-injection, capture skip-lists, FTS5 IDs-first search, privacy-tag stripping (+ add the secret redaction it lacks) |
| coleam00/claude-memory-compiler         | Hooks capture last-30-turn transcript excerpts; LLM compiles daily logs into a markdown wiki; SessionStart injects index | 2026-04-06 (single drop, zero maintenance since) | **NONE** (LICENSE 404; no license field) | Automated capture loop, no-RAG index-guided retrieval, lint checks                                          | git clone + uv sync (plain reviewable files, 3 deps)                                                                         | Verbatim transcript excerpts in plaintext temp files (orphaned on crash), no secret redaction; sends excerpts to Anthropic API; unsupervised background agent with acceptEdits writing files after 18:00                                                 | **PATTERN-ONLY**                     | No license = code legally unreusable (fails criterion 2 outright); transcript egress + unsupervised compiler against criteria; architecture (AGENTS.md spec) is a good reference for a future local capture hook                                                                                                      |
| obra/superpowers                        | SDLC skills framework (brainstorm→plan→TDD→review)                                                                       | 2026-06-10                                       | MIT                                      | 14 skills incl. writing-plans, executing-plans, TDD, debugging                                              | Plugin marketplace (plain markdown)                                                                                          | None for the Claude Code path                                                                                                                                                                                                                            | **ALREADY-INSTALLED** (v5.0.7)       | Skill roster in README matches installed set; periodic `/plugin update` suffices                                                                                                                                                                                                                                      |
| VoltAgent/awesome-claude-code-subagents | 154 subagents in 10 plugin packs                                                                                         | 2026-05-27                                       | MIT                                      | core-dev, lang, infra, qa-sec, data-ai, dev-exp, domains (quant-analyst, risk-manager), biz, meta, research | Plugin marketplace (plain .md)                                                                                               | README disclaims any security audit of agents; avoid its curl-piped Option-3 installer                                                                                                                                                                   | **ALREADY-INSTALLED** (all 10 packs) | Category index matches installed packs; nothing new                                                                                                                                                                                                                                                                   |
| ComposioHQ/awesome-claude-skills        | Index of 1000+ skills, Composio-ecosystem-oriented                                                                       | 2026-05-22                                       | Apache-2.0 (repo)                        | Discovery index; postgres skill candidate (sanjay3290); in-repo Telegram skill                              | Index only; in-repo skills depend on Composio Rube MCP (cloud relay + API key)                                               | connect-apps routes actions through Composio's hosted service — external data path we avoid                                                                                                                                                              | **PATTERN-ONLY**                     | No odds/betting/secret-scan matches; Telegram skill fails the no-external-service bar; postgres candidate would need its own vetting                                                                                                                                                                                  |
| travisvn/awesome-claude-skills          | Small curated skills list                                                                                                | 2026-04-28 (curation stale since 2025-11)        | **NONE**                                 | Nothing not already installed (superpowers, Trail of Bits, scientific skills, TACHES all present locally)   | Index only                                                                                                                   | No license blocks content reuse; unaudited third-party links                                                                                                                                                                                             | **REJECT**                           | Fails license criterion; zero matches for our four scan categories; strict subset of installed tooling                                                                                                                                                                                                                |
| hesreallyhim/awesome-claude-code        | Catalog (CSV) of skills/hooks/commands + vendored slash-commands                                                         | 2026-04-27 (README mid-rework stub)              | CC BY-NC-ND 4.0 (non-OSI)                | Hook patterns mined (see below); 23 vendored slash-commands                                                 | Reference catalog only                                                                                                       | NoDerivatives license forbids copying its files; entries unaudited                                                                                                                                                                                       | **PATTERN-ONLY**                     | Used exactly as intended here: concept source for our own hooks; nothing vendored                                                                                                                                                                                                                                     |
| alirezarezvani/claude-skills            | Mega-library: 338 skills/51 agents across 16 domains                                                                     | 2026-06-10 (v2.9.0, very active)                 | MIT                                      | Cherry-pick candidates: skill-security-auditor, env-secrets-manager; db-designer (redundant)                | Plugin marketplace or manual copy (plain files); avoid its curl OpenClaw path                                                | 533 bundled scripts not independently audited; huge AI-generated breadth of variable depth; ships a root .mcp.json needing review                                                                                                                        | **PATTERN-ONLY**                     | 95% out of scope; installing bundles would flood the skill namespace; the 2 useful items remain candidates pending individual file-level vetting                                                                                                                                                                      |

## Memory decision

**Project-local markdown memory wins** (`.claude/memory/` in-repo, canonical +
git-versioned; thin auto-loaded stub in
`~/.claude/projects/-Users-alexis-code-Betting-Picks-Bot/memory/MEMORY.md`;
formal decisions in `docs/adr/`). Both candidate tools were rejected for
install — full evidence and overturn criteria in **ADR-0001**.

## Hook patterns adopted (from hesreallyhim catalog mining; concepts only, no copied text)

1. **PreToolUse Bash denylist** (fcakyon/claude-codex-settings, Apache-2.0):
   jq-extract command → regex denylist → decision JSON / exit 2. Ours:
   `pre_bash_guard.sh` blocking rm -rf, bare rm, curl|sh, pip-from-URL, `&&`,
   force-push.
2. **Post-edit lint skeleton** (same source): jq file_path → glob filter →
   tool `|| true`. Ours: `post_edit_format.sh` (uvx ruff format) and
   `post_edit_pytest.sh` (scoped pytest, non-blocking).
3. **Secret gate**: no gitleaks hook exists anywhere in the catalog's 111KB
   CSV — designed our own: PreToolUse on `git commit` →
   `gitleaks git --pre-commit --staged`, fail closed (`pre_commit_secret_scan.sh`).
4. **Full-suite-on-commit + generated-file-sync** (their .pre-commit-config):
   kept manual/CI for us (documented in ADR-0003); the schema-docs-sync idea
   is noted for db-schema.md vs migrations.
5. **Config-hash caching** (bartolli TS hooks): noted for the pytest hook if
   per-edit latency ever matters.

## Fetch failures (recorded, not papered over)

- claude-mem `src/npx-cli/install/` listing: GitHub rate limit on the final
  call — Bun/uv auto-install behavior sourced from its README/SECURITY.md
  statements instead of installer source.
- MCP `search_code` rejected unauthenticated calls in one agent — worked
  around via direct directory navigation.
- Star counts were not present in the trimmed search-API responses —
  activity reported via `pushed_at` dates rather than guessed star figures.
