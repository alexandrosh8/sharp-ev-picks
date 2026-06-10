# ADR-0012: Direct Engine Binding — OddsHarvester + penaltyblog as the Master App's Spine

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) — explicit `/goal`: "download complete code
  from the repos I provide and from your research, use it as it is as it's
  proven, bind all together and make a betting picks master app."

## Context

ADR-0011 already adopted proven libraries as direct dependencies. This ADR
records the actual BINDING: the master app's live data + modeling spine is the
two user-named repos (OddsHarvester for free OddsPortal odds, penaltyblog for
football modeling), consumed as installed packages, wired into the existing
pure-math pick pipeline.

## Decision

| Layer                                    | Repo (used as-is)                                                 | Adapter in our code                                                                                                                                                     |
| ---------------------------------------- | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Free live + historical odds (OddsPortal) | **OddsHarvester** `run_scraper` coroutine                         | `app/ingestion/oddsportal.py::OddsPortalLoader` — converts its match dicts to `OddsSnapshotIn`, registers teams in `EventDirectory`                                     |
| Football probabilities                   | **penaltyblog** `DixonColesGoalModel` + `FootballProbabilityGrid` | `app/models/football_dc.py::DixonColesFootballModel` — fits with our time-decay weights, resolves team names, emits 1X2/totals/BTTS via the `ProbabilityModel` protocol |
| Historical training odds                 | football-data.co.uk (our loader)                                  | `app/scheduler.py::fetch_football_history`                                                                                                                              |
| Binding                                  | —                                                                 | `app/scheduler.py::build_scheduler` selects the spine via `ODDS_SOURCE` (default `oddsportal`); refits DC daily, polls odds, runs the pipeline                          |

The thin adapter pattern (not forking/copying source) keeps the proven code
authoritative: upgrades are `uv lock` bumps, and our 142 tests + the
penaltyblog parity test (1e-8) pin the contract.

## Verification (live, 2026-06-10)

- penaltyblog Dixon-Coles fitted on **760 real EPL matches** (football-data
  E0 2024/25+2025/26); priced West Ham vs Leeds with coherent
  1X2/totals/BTTS probabilities (each market sums to 1.0).
- OddsHarvester scraped **150 live odds snapshots across 10 Brazil Série A
  matches** (bet365, Betfury, … per selection) through `OddsPortalLoader`
  into our schema, with team context registered.
- Full pipeline (devig → gates → fractional Kelly → idempotent alert) runs
  over both; `scripts/master_demo.py` is the runnable proof.

## Safety (unchanged)

OddsPortal is an odds AGGREGATOR, not a bookmaker — read-only data, no betting
surface. OddsHarvester contains zero order-placement/login code (Phase B file
inspection); the safety audit still passes. WagerBrain (buggy Kelly) and
betfairlightweight (ships bet execution) remain excluded.

## Consequences

- `ODDS_SOURCE=oddsportal` is the default; `odds_api` remains available.
- OddsPortal scraping is ToS-sensitive and DOM-fragile (anti-bot, layout
  drift); for production cadence add proxies/delays and treat scrape gaps as
  expected. The Odds API path is the robust paid alternative.
- Live scraping needs Playwright Chromium: `uv run playwright install chromium`.
- Team-name resolution (OddsPortal ↔ football-data) uses an alias table +
  containment heuristic; misses are logged and simply yield no pick (safe).
