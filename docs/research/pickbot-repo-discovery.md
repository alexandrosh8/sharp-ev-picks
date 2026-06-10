# Pick-Bot Repo Discovery — Did Any Other Repo Help?

- **Date:** 2026-06-10
- **Method:** 13 read-only research agents (Workflow) over the plugin GitHub
  MCP server: 5 parallel domain searches (Elo, xG, injuries, backtest
  frameworks, holistic bots) → file-level inspection of the top 8 candidates.
  Goal: find data/code that would MATERIALLY improve pick quality beyond the
  already-bound stack, with license + automatic-betting checks.

## Already bound (not re-evaluated)

penaltyblog (Dixon-Coles + ClubElo/Understat/FBref scrapers), lightgbm,
xgboost, nba_api, OddsHarvester (oddsportal odds), our football-data.co.uk
loaders, and **martj42/international_results** (CC0 — 49k intl matches, bound
this session for the World Cup). Permanently excluded: betfairlightweight
(ships bet execution), WagerBrain (buggy Kelly).

## Verdicts

| Repo                                 | Category                                                 | License                                                                                    | Verdict                  | Why                                                                                                                                                                                               |
| ------------------------------------ | -------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **statsbomb/open-data**              | freeze-frame xG (richest available; covers WC/Euro/Copa) | StatsBomb User Agreement — **prohibits commercial use of derived analysis** (clause 1.2.2) | **reference-only**       | A +EV bot is "commercial exploitation of analysis derived from the data" → cannot productionize. Research/calibration only.                                                                       |
| statsbomb/statsbombpy                | StatsBomb loader                                         | proprietary (no OSI license; README links a User Agreement)                                | reference-only           | Same license wall; penaltyblog already reads StatsBomb open-data for research use.                                                                                                                |
| **felipeall/transfermarkt-api**      | injuries / suspensions                                   | MIT (code) — data is Transfermarkt's                                                       | **adapt-pattern**        | Genuinely adds injuries (which Dixon-Coles is blind to), but it's a self-host FastAPI scraper (not pip), Transfermarkt HTML is fragile/ToS-gray, and **no historical feed to backtest the gain**. |
| **manucabral/EasySoccerData**        | confirmed lineups (Sofascore)                            | **CONFLICT** — LICENSE=MIT vs pyproject=GPL-3.0                                            | **rejected for binding** | Pip-installable + uniquely exposes confirmed XIs, but an unresolved MIT/GPL conflict + unofficial-API fragility = "unclear/unsafe" → do not bind into production until upstream resolves it.      |
| hericlibong/Fifa-Api-Ranking-Scraper | FIFA national-team rankings                              | **NONE** (all rights reserved)                                                             | rejected for binding     | Useful WC seeding/strength feature, but no license = cannot legally vendor; Scrapy crawler, not a library; freshness unverified.                                                                  |
| dcaribou/transfermarkt-scraper       | squad rosters / injuries                                 | **NONE**                                                                                   | rejected for binding     | No license.                                                                                                                                                                                       |
| machina-sports/sports-skills         | agent-skill wrappers                                     | MIT                                                                                        | reference-only           | Wraps other sports APIs; no unique data; not additive.                                                                                                                                            |
| kochlisGit/ProphitBet                | form-based soccer ML (GUI)                               | MIT                                                                                        | reference-only           | No tests/CI, leaky StratifiedKFold CV — a catalogue of the mistakes our ADRs already forbid.                                                                                                      |

## Conclusion (answers "did you check other repos for updates/optimization?")

**Yes — and the honest finding is that nothing new is safely bindable for a
real edge today.** The two categories that could create genuine edge — **xG**
and **injuries/lineups** — are blocked: StatsBomb's xG license forbids
commercial use; the injury/lineup sources are either license-unclear
(EasySoccerData), unlicensed (transfermarkt, FIFA scraper), or fragile
unofficial-API scrapers with no historical feed to backtest. Per the project's
own rules ("never blindly install random GitHub code; reject unclear/unsafe
repos"), none were bound into the production bot.

The one clean, additive, license-safe source — **martj42 international results
(CC0)** — was bound, enabling the World Cup model.

**The real lever is not another repo; it is information the market underweights
plus a backtest that proves positive CLV.** See `docs/backtesting/findings.md`:
the current Dixon-Coles model has negative CLV, so a "solid pick finder" is not
achievable by binding more goals-only data — only by adding (and backtesting)
xG/injury signal from a license-clean feed.
