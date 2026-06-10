# ADR-0010: Free-First Odds Ingestion

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

User mandate: verify and prefer FREE historical + live odds sources. Full
evaluation: `docs/research/free-odds-sources.md` (8 sources scored for
coverage, cost, ToS risk, CLV suitability). Proven tools are used directly
(user direction 2026-06-10).

## Decision

| Need                                               | Source                                                                       | Mechanism                                                                                                                                       |
| -------------------------------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Football live + quasi-close                        | The Odds API free tier (500 cr/mo)                                           | `app/ingestion/odds_api.py` (key rotation); slate polls `regions=eu&markets=h2h,totals` = 2 cr; final pre-kickoff snapshot flagged `is_closing` |
| Football authoritative close + historical training | football-data.co.uk CSVs (Pinnacle PSC\* closing columns)                    | `app/ingestion/football_data.py` (ours, working) and/or **penaltyblog's FootballData scraper used directly as a dependency**                    |
| NBA live + self-built closing archive              | The Odds API free tier (`basketball_nba`, 3 cr/snapshot)                     | redundant pre-tipoff capture job — a missed snapshot loses that close forever                                                                   |
| NBA stats                                          | **nba_api (pip dependency, 3.7k★, MIT)**                                     | phase 5                                                                                                                                         |
| NBA historical odds 2011–2021                      | sportsbookreview bundled dataset (MIT, zero-scrape)                          | one-off import + validation                                                                                                                     |
| NBA odds bridge 2021→now                           | **OddsHarvester used directly** (pip, MIT) for a one-off OddsPortal backfill | accepted ToS/fragility risk for backfill ONLY — never a recurring pipeline dependency                                                           |
| Exchange enrichment (later)                        | Betfair Historical BASIC (free) + delayed API-NG key                         | clean-room read-only client; **betfairlightweight is never imported** (ships bet execution)                                                     |

Credit budget fits the free tier (~90–180 NBA + ~200 football of 500/mo);
escalation path $30/mo if cadence/backfill demands it.

## CLV verdict

Football CLV is fully credible at $0 (true Pinnacle close from
football-data.co.uk). NBA CLV is credible prospectively via self-captured
closes (minutes-level drift accepted; no retroactive CLV).

## Alternatives considered

- Paid The Odds API historical endpoint — deferred (only paid escalation).
- OddsPortal scraping as the recurring live source — rejected: high ToS risk
  - fragility as a _pipeline_ dependency; sanctioned for one-off backfills.
- datahub.io mirrors — rejected for odds (columns verified stripped).

## Consequences

- The closing-snapshot job must be redundant (two scheduled attempts).
- `odds_snapshots.is_closing` + football-data true-up populate
  `picks.closing_odds/closing_fair_probability/clv_log/beat_close`.
- API-Football remains SUSPENDED and absent from the codebase.
