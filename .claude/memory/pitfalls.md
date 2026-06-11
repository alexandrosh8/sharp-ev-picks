# Pitfalls

- **Project path contains a space** (`Betting Picks Bot`) — always quote
  `"$CLAUDE_PROJECT_DIR"` in hooks/scripts; absolute quoted paths in shell.
- **Standalone `github` MCP server has bad credentials** — use the
  `mcp__plugin_everything-claude-code_github__*` server or `gh` CLI instead.
- **ruff is not on PATH** — use `uvx ruff` or the project venv.
- **GateGuard hook** blocks the first Write to every new file path; retry
  passes. Budget for prime+write when scaffolding many files.
- **gitleaks v8.30 syntax**: staged scan is `gitleaks git --pre-commit --staged`
  (not `protect`).
- No `&&` in shell commands (user hard rule + pre_bash_guard hook blocks it).
- **`.gitignore` `models/` trap**: an unanchored `models/` line matches
  `app/models/` too, silently un-tracking the source package — fresh clones
  break with `ModuleNotFoundError: app.models`. Anchor data-artifact ignores
  to root (`/models/`, `/data/`). Verify with a throwaway `git clone` of HEAD.
- **OddsHarvester loader**: pass `date=None` (general upcoming page) for live
  odds — pinning `date=today` filters to that exact date and usually returns
  0 matches. Needs `uv run playwright install chromium`.

- **OddsPortal timestamps inherit the scraping BROWSER's timezone** (found
  2026-06-10): the page's embedded `startDate` epoch is pre-shifted to the
  browser tz, so a Cyprus-time Mac produced kickoffs/capture times +3h while
  labeled "UTC". Fix: ALWAYS pass `browser_timezone_id="UTC"` to
  OddsHarvester's run_scraper (done in app/ingestion/oddsportal.py and both
  pick scripts). Verified vs published WC2026 kickoffs. This was also the
  root cause of the "future captured_at" clamp.

- **OddsHarvester 0.3.0 quirk patches live in app/ingestion/oddsportal.py**
  (`_patch_upstream_quirks`, 2026-06-11): the PyPI package is patched at
  runtime, NOT forked. Fixes: OneTrust consent DOM (hidden `ot-*` nodes)
  polluting tab/More selectors — the 'More' fallback clicked the consent
  dialog; `wait_for_market_switch` never passing (first-`.active`-match
  check) costing a warning + 9s per market; team crests resolving as
  bookmaker names via the bare `<img alt>` fallback when table scoping
  misses (phantom "Racing"/"Al-Mabarrah" books). RE-VERIFY all patches after
  any oddsharvester version bump — they replace two upstream methods
  wholesale. Expected scrape gaps (market tab absent, submarket absent,
  bookies-filter nav absent) are downgraded to INFO by design; the durable
  DOM-break alarm is the per-market snapshot counts each cycle.
