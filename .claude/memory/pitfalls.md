# Pitfalls

- **Free Betfair/Pinnacle odds: the ONLY two free sources, verified 2026-06-20
  (Playwright DOM + arcadia live).** (1) PINNACLE is ABSENT from the OddsPortal
  odds table in EVERY scrapeable region (Cyprus/US/BR exits all checked — no
  Pinnacle img-alt; the lone "pinnacle" HTML hit was SEO prose). So Pinnacle's
  ONLY free source is `app/ingestion/pinnacle_arcadia.py` (guest.api.arcadia.
  pinnacle.com guest JSON, geo-independent, no key — confirmed live: 584 soccer
  matchups). Do NOT try to capture Pinnacle from OddsPortal. (2) BETFAIR EXCHANGE
  is in a SEPARATE `data-testid="betting-exchanges-section"` (NOT the main book
  table); `app/ingestion/betfair_exchange.py` selectors are EXACTLY correct (walk
  up >=5 levels from the `betting-exchanges-table-row` to 6 odd-container cells =
  BACK triple then LAY triple; each cell = value + liquidity div). NO selector
  change needed. **The section appears ONLY on liquidity-rich MAJOR fixtures AND
  ONLY from a UK or EU proxy exit** — a missing section is the expected geo or
  liquidity gap, not a bug. Keep the Betfair reader on a UK/EU proxy
  (SCRAPER_PROXY_POOL). Odds format (decimal vs fractional) is a per-visitor
  COOKIE — parse_odds_value handles both. roundproxies.com blog re-reviewed: its
  stealth/CAPTCHA core stays REJECTED (hard rule); every doctrine-safe technique
  it lists is already implemented. NO new free Betfair/Pinnacle source exists.

- **Dixon-Coles rho conventions: penaltyblog 1.11.0 ships TWO OPPOSITE tau
  parameterizations** (verified 2026-06-12). PAPER (DC 1997: tau(0,1)=
  1+rho*lambda_home) lives in the compiled DixonColes model kernel
  (compute_dixon_coles_probabilities) and basic goal_expectancy; TRANSPOSED
  (tau(0,1)=1+rho*lambda_away) lives in goal_expectancy_extended and
  create_dixon_coles_grid. NEVER mix rho across the two families — a fitted
  DixonColes model rho fed into create_dixon_coles_grid silently mis-prices
  the 1-0/0-1 cells (moves AH ±0.5/±1.0). The conventions coincide ONLY when
  lambda_home == lambda_away. app/models/ah_bridge.py pairs extended->grid
  (both transposed) and is consistent AS-IS. Pinned by
  tests/test_penaltyblog_rho_convention.py — re-verify on any penaltyblog
  bump.
- **"Polymarket/sports trading bot" GitHub search results are an SEO-spam /
  scam cluster** (2026-06-11): near-identical repos from throwaway accounts,
  some auto-executing with committed `.env.bak` files. The user-suggested
  GastonDeMichele/Polymarket-Sports-Bot does not even exist (404). Any future
  repo from this cluster needs install-script scrutiny BEFORE cloning; never
  run their code. See betting-repo-research.md Wave 4.
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

- **NEVER loosen the strict cross-source (Pinnacle) matcher to chase coverage**
  (2026-06-20): the shadow match-rate gap (~37% matched, with a 36-pick "alias
  gap") is MOSTLY the strict matcher CORRECTLY refusing DANGEROUS matches —
  women's "W" vs men's games, dropped mascot suffixes (live "Dandenong" vs
  Pinnacle "Dandenong Rangers"), and same-city ambiguity (live "Canberra" ->
  Pinnacle "Canberra Gunners" OR "Canberra Nationals"). Aggressive aliasing mints
  FALSE sharp closes (a WRONG CLV anchor) — worse than no close. ~37% is the
  doctrine-correct ceiling on obscure/lower-league slates; the lever for more
  coverage is scoping premium picks to MAJOR leagues (canonical names + full
  Pinnacle coverage), NOT looser matching. Only add aliases for UNAMBIGUOUS
  spelling variants (hyphen/space, accents) — never suffix-drops or gender markers.

- **Betfair reader: OddsPortal odds format is a per-visitor COOKIE**
  (2026-06-20, ADR-0015 update / PR #80): a fresh scraper context gets DECIMAL
  ("6.51") OR fractional ("28/25") unpredictably. The v1 reader parsed only
  fractional AND walked-up-all-leaves (capturing the hidden-<a>/visible-<p>
  DUPLICATE value + mixing BACK+LAY rows) — so it stored majors BROKEN
  (Brazil-Haiti as "Draw 1.13" = the home price MISLABELED, 1/3 outcomes) or
  nothing at all on a decimal-format page. Fix: parse_odds_value (both formats) +
  \_ROW_EXTRACT_JS scoped to [data-testid="odd-container"] cells (one value + one
  liquidity per cell, BACK-triple-first, payout-container excluded). Betfair
  BASKETBALL liquidity is THIN (£28-£342, under the £500 floor) — Pinnacle is the
  deep basketball anchor, so basketball needs no Betfair to have a sharp close.
- **`uv sync` (bare) PRUNES the optional extras the running app needs — use
  `uv sync --all-extras`.** The real engines live in `[project.optional-
dependencies]`: `football` (penaltyblog → `app/models/football_dc.py`),
  `backfill` (oddsharvester==0.3.0 → `app/ingestion/oddsportal.py`), plus
  playwright (transitive) for betfair_exchange/oddsportal. A plain `uv sync`
  (e.g. to add a dep) installs ONLY the default set and REMOVES everything else
  → the live app then throws `PackageNotFoundError` (oddsharvester) /
  `ModuleNotFoundError` (penaltyblog, playwright) and the football/extra test
  suites silently SKIP. To add a dependency without breaking the env, run
  `uv add <pkg>` or follow a bare sync with `uv sync --all-extras`. (Hit
  2026-06-22 adding a dependency; restored with `uv sync --all-extras`.)
