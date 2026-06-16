---
name: github-research
description: "Fast, repository-grounded GitHub research. Use when searching/discovering, evaluating, scoring, or citing any external repository or library for betting/odds/modeling/tooling — and for fast code/repo search + clean fetch."
allowed_tools:
  - Read
  - Grep
  - Glob
  - Bash
  - ToolSearch
---

# GitHub Research

## Fast search + fetch (pick the cheapest tool that answers the question)

Three layers, fastest first:

1. **`gh` CLI (already installed + authed as alexandrosh8)** — fastest for
   exact search and raw fetch, no MCP round-trip:
   - repos: `gh search repos "<terms>" --sort stars --limit 15 --json fullName,stargazerCount,pushedAt,description,license`
   - code: `gh search code "<symbol or string>" --limit 20` (default branches only)
   - file/meta: `gh api repos/OWNER/REPO` (license, pushed_at, archived);
     `gh api repos/OWNER/REPO/contents/PATH --jq .download_url` then fetch raw.
2. **exa MCP (semantic, clean markdown)** — best for "find me a repo/lib that
   does X" and for reading READMEs/source as clean text (load via ToolSearch
   `select:mcp__plugin_everything-claude-code_exa__web_search_exa,mcp__plugin_everything-claude-code_exa__web_fetch_exa`):
   `web_search_exa("describe the ideal repo, not keywords")` then
   `web_fetch_exa([urls])` to read README/source in one call.
3. **GitHub plugin MCP** — structured repo metadata + in-repo file reads
   (`...github__search_repositories` / `search_code` / `get_file_contents`).

Repo-search query tip: 2–4 plain words (over-qualified queries like
`devig vig-free implied probability stars:>15` return 0); add `language:python`
/ `stars:>N` only to narrow a noisy result set.

## Purpose

Inspect repositories file-by-file before any recommendation, producing
Use/Reject scoring tables with zero hallucinated contents.

## Procedure

1. Load tools: ToolSearch
   `select:mcp__plugin_everything-claude-code_github__search_repositories,mcp__plugin_everything-claude-code_github__get_file_contents,mcp__plugin_everything-claude-code_github__search_code`.
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

## Project evaluation gate (betting-ai — apply BEFORE recommending anything)

The doctrine is sharp-vs-soft line shopping + CLV. Every repo eval has been
decided by these, in order:

1. **Strategy shape.** Winner/ATS/score ML predictors are the WRONG shape —
   our own backtests show outcome-prediction loses on CLV. Reject as a
   strategy; mine only data loaders, devig math, or backtest patterns.
2. **THE data gate (decisive for any new sport/market).** A source is only
   backtestable for us if it carries, for free, BOTH a **sharp pre-match
   anchor (Pinnacle/recognized sharp)** AND the **closing line**. Single
   consensus closes (SBR, nflverse, tennis-data.co.uk) and soft-book-only
   feeds CANNOT measure incremental CLV vs close — they fail the gate
   (proven for NBA, NFL, tennis). A genuinely free LIVE Pinnacle source is
   the project's biggest gap; flag any candidate that plausibly closes it.
3. **No-autobet (hard rule).** Any place/cancel-order, bookmaker-login, or
   stored-credential code = unusable directly; idea-only at most.
4. **License.** No license = legally unliftable (clean-room/ideas only).
5. **We already have:** OddsHarvester (OddsPortal scrape), penaltyblog
   (devig matches to 1e-8), our own walk-forward backtester + CLV/Kelly.
   Don't recommend redundant scrapers/devig libs as dependencies.

Record verdicts in `docs/research/betting-repo-research.md` and a one-line
pointer in `.claude/memory/decisions.md` so the same repos are not
re-evaluated (kyleskom/georgedouzas/ProphitBet/NBA_Betting/NBA_AI, nflverse,
sbrscrape are already settled — see decisions.md).

## Gotchas

- **The standalone `github` MCP server has bad credentials** — it returns
  "Authentication Failed". Always use the
  `mcp__plugin_everything-claude-code_github__*` server; `gh api` is the
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
