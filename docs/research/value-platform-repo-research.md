# Repo Research — Data/Tooling Extensions for the Value Platform

- **Date:** 2026-06-10
- **Question:** do any GitHub repos materially extend the validated
  sharp-vs-soft value platform — more historical multi-book odds, a free
  sharp (Pinnacle) live feed, line-movement/CLV datasets, or proven
  value-betting research?
- **Method:** 4 parallel discovery sweeps (historical odds data, sharp
  feeds, CLV tooling, value-betting research) → dedupe → 5 file-by-file
  inspections with structured verdicts. GitHub MCP rate-limited after 4
  calls; remaining searches via authenticated `gh` CLI. Every fact below is
  fetched, not assumed.

## Answer: nothing qualifies for binding

All 5 inspected repos are **reference-only**. The conclusion is itself
valuable: the platform already sits on the best free data for this strategy
(football-data.co.uk per-book Pinnacle + Max + closing columns), and **no
free Pinnacle live feed exists on GitHub** — every Pinnacle path requires a
funded account or an unverified paid proxy.

## Verdicts

| Repository                                                    | Decision       | Why (one line)                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| georgedouzas/sports-betting (MIT, active)                     | reference-only | Data is `market_average`/`market_maximum` only — no per-book columns, no Pinnacle, no closing pairs; strictly a subset of our football-data loaders. Backtest weaker than ours (index TimeSeriesSplit, no CLV). Residual value: arXiv 1710.02824 consensus-value rule as an independent cross-check. |
| xgabora/Club-Football-Match-Data-2000-2025 (MIT, stale 10 mo) | reference-only | 226k matches 2000-2025 but the derivative **dropped PSH/PSCH closing columns** — no sharp anchor, no CLV computable. Pre-2019 history is better obtained from raw football-data.co.uk season files, which keep those columns.                                                                        |
| flancast90/sportsbookreview-scraper (MIT, stale 23 mo)        | reference-only | NOT multi-book: one anonymous consensus line per game (US sports 2011-2021), garbled 2H columns. Only useful as a crude NBA open/close consensus baseline someday.                                                                                                                                   |
| iliyasone/ps3838api (MIT, active)                             | reference-only | Best-typed PS3838/Pinnacle schema docs + delta-sync pattern, but needs a funded account, and `place_straight_bet` would enter the dependency tree; PS3838 auth has **no read-only scope** → conflicts with hard rules 1+3. Never bind.                                                               |
| spdimov/RapidAPI-Pinnacle (MIT, abandoned 1-day repo)         | reference-only | Trivial capture code (hardcoded key, root MySQL). Only the pointer matters: a RapidAPI "pinnacle-odds" proxy exists — free-tier limits/ToS unverified, check independently before designing around it.                                                                                               |

## Rejected at discovery gate (no file inspection)

- `ovignez-hash/*` "BOOKMAKER BRAIN" family — lead-gen sample CSVs, no
  license, 0 stars.
- `gingeleski/odds-portal-scraper`, `Mg30/odds-portal-scraper` — duplicate
  the already-bound OddsHarvester.
- `probberechts/soccerdata` — odds coverage is football-data.co.uk
  MatchHistory only (already direct); rest is stats scrapers.
- `pinnacleapi/pinnacleapi-documentation`, `rozzac90/pinnacle` — docs
  only / stale 2022; API needs a funded account.
- `ianalloway/closing-line-archive`, `neeljshah/clvtrack` — 0 stars, no
  data committed.
- `Seb943/scrapeOP` — HTTP 404 (deleted/renamed).

## Follow-up worth remembering (not acted on)

- **goto_conversion** (110★, MIT, pushed 2026-05) — Kaggle-validated
  odds→probability conversion; a possible future devig alternative for the
  sharp anchor. Off-domain for this sweep (not a dataset); evaluate against
  the shin/power devig parity tests before any adoption.
- A live **Pinnacle anchor for forward CLV capture** remains the platform's
  biggest data gap. Realistic options: The Odds API `regions=eu` (has
  Pinnacle, paid credits) or the unverified RapidAPI pinnacle-odds proxy.
  football-data.co.uk closing columns remain the free settlement-time
  reference.

## Safety

None of the inspected repos was bound. The only repo with bet-placement
capability (ps3838api) is explicitly never to be added as a dependency —
its auth model cannot satisfy the read-only-scope requirement (hard rule 3).
