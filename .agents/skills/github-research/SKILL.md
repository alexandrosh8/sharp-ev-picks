---
name: github-research
description: "Repository-grounded GitHub research via MCP. Use when evaluating, scoring, or citing any external repository, or running discovery searches for betting/odds/modeling code."
allowed_tools:
  - Read
  - Grep
  - Glob
  - ToolSearch
---

# GitHub Research

## Purpose

Inspect repositories file-by-file before any recommendation, producing
Use/Reject scoring tables with zero hallucinated contents.

## Procedure

1. Load tools: ToolSearch
   `select:mcp__plugin_everything-Codex-code_github__search_repositories,mcp__plugin_everything-Codex-code_github__get_file_contents,mcp__plugin_everything-Codex-code_github__search_code`.
2. For each candidate: fetch README → manifest (pyproject.toml /
   package.json, including install/postinstall scripts) → the core modules
   behind the repo's claim. Quote one function per core file as inspection
   proof.
3. Apply the inspect-worthy gate for discovery hits: (>100 stars OR unique
   capability) AND pushed within 18 months AND not a fork.
4. Score in the standard 11-column table (Repository / Category /
   Stars-activity / Core function / Code quality / Maintenance / Directly
   reusable / Best file to adapt / Security concern / Automatic betting
   risk / Final decision).
5. Verdicts: adapt-math | adopt-pattern | reference-only | reject. List
   surfaced-but-uninspected repos separately — never recommend them.
6. Write results to `docs/research/` with the files-opened list.

## Checklist

- [ ] Every recommended repo has files_inspected with quoted code
- [ ] License checked before any "adapt" verdict
- [ ] Automatic-betting risk assessed explicitly per repo
- [ ] Install-time code execution reviewed (postinstall, setup.py)

## Gotchas

- **The standalone `github` MCP server has bad credentials** — it returns
  "Authentication Failed". Always use the
  `mcp__plugin_everything-Codex-code_github__*` server; `gh api` is the
  fallback.
- **search_code requires being authenticated and indexes default branches
  only** — absence of search hits is not absence of code; fall back to
  directory listing via get_file_contents on a path of `""` or the tree.
- **Stars ≠ quality** — check last_push, committed artifacts (e.g. a
  committed `db.sqlite3` is a quality red flag), and test presence.
- **README claims drift from code** — verify the specific module exists
  before citing a capability.

## Forbidden mistakes

- Recommending a repository without opening its files.
- Running clones or install scripts during research.
- Quoting stats (stars, dates) from memory instead of the API response.
