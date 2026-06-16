# Free Odds Sources — Live + Historical (user-mandated deep-dive)

- **Date:** 2026-06-10
- **Question:** can the platform run on FREE historical + live odds, and can
  CLV be tracked credibly without paid data? **Answer: yes for football
  (fully), yes-prospectively for NBA.** This document drives ADR-0010 and the
  `app/ingestion/` design.

## Source evaluation

| Source                                     | Type                                                    | Sports                             | Cost                | ToS risk                                | Access                                                                                         | Notes (evidence-backed)                                                                                                                                   |
| ------------------------------------------ | ------------------------------------------------------- | ---------------------------------- | ------------------- | --------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **football-data.co.uk CSVs**               | historical                                              | Football, 22+ divisions, 1993/94+  | Free, no key        | low                                     | HTTPS GET `mmz4281/{season}/{div}.csv` (our `app/ingestion/football_data.py`)                  | **Pinnacle CLOSING odds (PSCH/PSCD/PSCA) since 2012/13** + Max/Avg closing for 1X2/OU2.5/AH; verified live in the 2025/26 EPL file; updates ~twice weekly |
| **The Odds API free tier**                 | both (live snapshots; historical endpoint is PAID-only) | 70+ sports incl. our leagues + NBA | 500 credits/mo free | low                                     | REST v4 (our `app/ingestion/odds_api.py`, key rotation)                                        | Credit math: cost = markets × regions per request. `regions=eu` includes Pinnacle. First paid step $30/mo = 20k credits                                   |
| Betfair Historical BASIC                   | historical                                              | All exchange sports, May 2015+     | Free w/ account     | low                                     | tar.bz2 downloads + `betfair_data` parser                                                      | Exchange price enrichment, later phase                                                                                                                    |
| Betfair API-NG delayed key                 | live                                                    | All exchange sports                | Free (delayed)      | low                                     | clean-room read-only client only — **betfairlightweight ships bet execution, never import it** | Optional exchange fair-odds reference, phase 6+                                                                                                           |
| OddsPortal scrapers (OddsHarvester et al.) | both                                                    | Many                               | Free (compute)      | **high** (ToS; anti-blocking arms race) | Playwright                                                                                     | One-off historical NBA backfill at most; never a pipeline dependency                                                                                      |
| sportsbookreview-scraper (flancast90)      | historical                                              | NBA/NFL/MLB/NHL                    | Free (MIT)          | medium                                  | **bundled 2011–2021 NBA odds dataset in-repo** (zero-scrape)                                   | NBA training base; cross-validate before trusting                                                                                                         |
| openfootball / datahub.io                  | historical                                              | Football                           | Free                | low                                     | raw JSON/CSV                                                                                   | **datahub mirror verified: odds columns STRIPPED** — results only; do not use for odds                                                                    |
| Kaggle NBA odds dumps                      | historical                                              | NBA                                | Free                | low                                     | Kaggle download                                                                                | Bridge/backfill candidates; validate against sportsbookreview data                                                                                        |

## Recommended MVP combination (→ ADR-0010)

1. **Football live + closing:** The Odds API free tier — one slate poll
   per league with `regions=eu&markets=h2h,totals` (2 credits/poll); final
   snapshot ~10–15 min pre-kickoff as quasi-close, self-stored
   (`odds_snapshots.is_closing`). **Authoritative close arrives free later
   via football-data.co.uk PSC\* columns** — settlement trues up CLV.
2. **Football historical training:** football-data.co.uk CSVs (already
   implemented loader), optionally enriched with Betfair BASIC files.
3. **NBA live + closing:** The Odds API free tier, `basketball_nba`,
   `markets=h2h,spreads,totals` (3 credits/snapshot) — schedule a snapshot in
   the ~5-min pre-tipoff window and persist it; this self-built archive IS the
   closing-line record (no free historical endpoint exists). Make this capture
   job redundant — a missed snapshot loses that slate's close forever.
4. **NBA historical:** sportsbookreview bundled 2011–2021 dataset as the
   base; bridge 2021→today via one-off OddsPortal backfill (accepting ToS
   risk) or validated Kaggle dump; from launch forward our own snapshots close
   the gap permanently.

**Credit budget:** ~90–180 credits/mo NBA + ~200 football fits inside the free
500/mo. Escalation path: $30/mo (20k credits) only if NBA backfill or tighter
cadence is needed.

## CLV feasibility verdict

- **Football: fully credible at $0** — pick price recorded at alert time;
  CLV settled against the true Pinnacle close from football-data.co.uk.
- **NBA: credible prospectively** — CLV vs self-captured Pinnacle
  (regions=eu) pre-tipoff snapshots; caveats: minutes-level snapshot-vs-close
  drift, no retroactive CLV, snapshot job must be redundant.

## Update 2026-06-16 — free unofficial LIVE Pinnacle (the biggest-gap candidate)

GitHub discovery surfaced a FREE, accountless, pre-match **Pinnacle** feed:
the unofficial guest JSON API `guest.api.arcadia.pinnacle.com/0.1` (bulk
`/sports/{id}/markets/straight?primaryOnly=false`; per-match
`/matchups/{id}/markets`). Verified live in two repos' code
(ACHBIDHAN/Pinnacle_Football_Odds_Scraper, NateDeMoro/prediction-market-ev-engine
— both UNLICENSED, NO code copyable). This is the first concrete path to the
LIVE Pinnacle sharp anchor this doc lists as the biggest gap: it would let us
self-capture near-kickoff Pinnacle snapshots, set picks.anchor_type='pinnacle'
(today mostly 'consensus'), and measure FORWARD CLV against a true sharp line.
Caveat: direct Pinnacle scraping is **high ToS risk + endpoint-fragile** (same
class as OddsPortal), and the guest `x-api-key` rotates. Doctrine-compliant
route = clean-room a GET-only, rate-gated forward-capture job (read-only, no
account, no order code), NOT a backfill (historical Pinnacle stays paid-only).

## Fetch-honesty log

football-data.co.uk notes.txt 429'd via WebFetch (succeeded via curl);
historicdata.betfair.com is a JS app (403) — Betfair tier facts verified via
official support pages; The Odds API historical-endpoint pricing verified in
their docs (paid-only despite homepage marketing). GitHub MCP rate-limit
fallbacks to authenticated `gh api` recorded in the workflow output.
