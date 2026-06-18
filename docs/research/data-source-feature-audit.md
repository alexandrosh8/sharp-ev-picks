# Data-source & penaltyblog feature audit (2026-06-18)

**Question:** do we use all the features of the sites/libraries we depend on
(football-data.co.uk, penaltyblog, …), and are we missing data/features worth
adding? **Method:** 4-agent audit — 2 codebase-inventory (what we consume) + 2
external-research (full current feature sets). Read-only; file:line / fetched-URL
cited. Framed against the doctrine: the only validated edge is **sharp-vs-soft
line shopping + CLV**; outcome/goal prediction backtested NEGATIVE and is
screens-only. A feature is "worth adding" only if it improves
**devig / pricing / CLV / market coverage**, not standalone prediction.

> **Update (2026-06-18, post-audit):** two items below are now stale.
> (1) **Arcadia is no longer moneyline-only** — `app/ingestion/pinnacle_arcadia.py`
> already captures period-0 **moneyline + main-line totals + main-line
> spreads** (the "A1 — extract Pinnacle totals/spreads" recommendation is
> effectively shipped for the _capture_ side; the remaining work is consuming
> them in live CLV, still gated on match rate).
> (2) **NFL is now VISIBILITY-ONLY, not rejected.** `american_football`
> (arcadia sport-id 15) was added to `ARCADIA_SPORTS`, so a free Pinnacle
> ML/totals/spread **close is forward-captured** for NFL — which is exactly the
> "free sharp price + true close" the old NFL REJECT said did not exist (it now
> exists going forward). NFL + tennis are scraped and shown but mint **no
> picks** until accrued closes clear the held-out >2 SE incremental-CLV bar.

## TL;DR

1. **penaltyblog 1.11.0 is the LATEST release** (PyPI, 2026-06-02). Our `>=1.11`
   pin is at the ceiling — the MatchFlow / Bayesian-MCMC / StatsBomb-Opta
   features in the marketing blurb are **already in the version we ship**; we
   are not behind a release. We deliberately use only a slice, and most of the
   rest is out-of-doctrine (prediction machinery) or behind **paid** event data.
2. The genuine, free, doctrine-positive gaps are **NOT penaltyblog features** —
   they are **unused odds columns we already have access to**:
   - **football-data.co.uk closing AH + OU + market-max/avg columns** — free, in
     the same CSV we already download; would ~triple the CLV-gradable backtest
     surface (1X2 → 1X2 + Asian-handicap + totals).
   - **Pinnacle Arcadia totals & spreads** — we already FETCH them
     (`primaryOnly=false`) and then **discard everything but moneyline**; a
     live sharp anchor for OU/AH is sitting in a response we throw away.

## 1. penaltyblog — used vs available

Installed/latest: **1.11.0** (current ceiling; nothing newer exists). 4 import
sites only.

| Area                                                                                           | Status                             | Notes                                                                                                                                                                                                                                                                                  |
| ---------------------------------------------------------------------------------------------- | ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `models` (Dixon-Coles)                                                                         | **USED**                           | `DixonColesGoalModel` + `FootballProbabilityGrid` price football (fair 1X2/OU/BTTS to devig against); `create_dixon_coles_grid` + `goal_expectancy_extended` bridge devigged 1X2+OU→AH. `app/models/football_dc.py`, `app/models/ah_bridge.py`. On-doctrine (pricing, not prediction). |
| `scrapers.Understat`                                                                           | **USED (offline)**                 | xG features for the ML value-filter **dataset** only (`scripts/ml/build_value_dataset.py:900`), not the live path.                                                                                                                                                                     |
| `implied` (7-method devig incl. SHIN/POWER)                                                    | **not imported — already have it** | We re-implemented the devig family in `app/probabilities/devig.py`, parity-tested to 1e-8 vs penaltyblog. Not a gap.                                                                                                                                                                   |
| `models`: Bayesian / Hierarchical Bayesian / Bivariate Poisson / ZIP / NegBin / Weibull-Copula | **NOT used**                       | Alternative _pricing_ engines. Only matter if a fancier pricer beats Dixon-Coles for the devig anchor — and the DC **goals** model already backtested to NEGATIVE CLV, so this is speculative, not a known win. Reference-only.                                                        |
| `ratings` (Elo/Massey/Colley/Pi)                                                               | **NOT used**                       | Team-strength predictors. Pricing-input-only at best; doctrine-skeptical.                                                                                                                                                                                                              |
| `metrics` (RPS / Brier / Ignorance)                                                            | **NOT used**                       | Calibration/eval tooling. Mildly useful for grading model calibration; low priority.                                                                                                                                                                                                   |
| `betting` (Kelly / value-bet)                                                                  | **NOT used (by design)**           | Kelly lives in `app/risk/staking.py`; penaltyblog betting is reference-only per doctrine.                                                                                                                                                                                              |
| `matchflow`, `xt`                                                                              | **NOT used**                       | Only useful on StatsBomb/Opta event JSON → **paid**.                                                                                                                                                                                                                                   |
| `fpl`, `viz`, `backtest`                                                                       | **NOT used**                       | FPL/fantasy = no devig/CLV value; viz cosmetic; we use our own walk-forward backtest.                                                                                                                                                                                                  |
| StatsBomb / Opta connectors                                                                    | **NOT used — PAID**                | Fail free-first; no free tier.                                                                                                                                                                                                                                                         |

**Verdict:** we use penaltyblog correctly and completely _for its doctrine role_
(pricing football to devig against). Nothing in penaltyblog is a "must-add."
The only mild candidate is `metrics` (RPS/Brier) for calibration grading.

## 2. Data sources — used vs unused fields

| Source                                                               | What we read                                                                                                     | What we drop (doctrine-relevant)                                                                                                                                                                                                                   |
| -------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **football-data.co.uk** (`app/ingestion/football_data.py`)           | 12 columns: Date/Home/Away/FTHG/FTAG/FTR, **B365H/D/A** (pre-match 1X2), **PSCH/PSCD/PSCA** (Pinnacle 1X2 close) | **The big reservoir.** Closing AH `PCAHH/PCAHA`+`AHCh`; closing OU `PC>2.5/PC<2.5`; closing market max/avg `MaxC*`/`AvgC*`; all pre-match Max/Avg + Pinnacle pre-match `PS*`. (Shots/corners/cards = prediction features, correctly out of scope.) |
| **Pinnacle Arcadia** (`app/ingestion/pinnacle_arcadia.py`)           | period-0 **moneyline only** (`s;0;m`)                                                                            | Fetches **all** straight markets (`primaryOnly=false`) then extracts only moneyline → **Pinnacle sharp totals/spreads fetched-and-discarded.**                                                                                                     |
| **The Odds API** (`app/ingestion/odds_api.py`)                       | `event.id`, book/market/price/point                                                                              | Drops `commence_time` + `home_team`/`away_team` (reads only `event.id`) → registers no team/kickoff context; markets fixed to `h2h,totals,spreads`.                                                                                                |
| **OddsPortal** (`app/ingestion/oddsportal.py`)                       | 1X2/ML/totals/half-line AH+EH, BTTS, DNB, DC                                                                     | Integer/quarter AH lines rejected (half-lines only); `ah_bridge` exists but not wired live; odds-history movement deduped away.                                                                                                                    |
| **martj42 international** (`app/ingestion/international_results.py`) | date/teams/score/tournament/neutral                                                                              | `city`/`country` (prediction features — out of scope).                                                                                                                                                                                             |

**Key nuance:** football-data's `PSCH/PSCD/PSCA` are parsed into `MatchRow` but
have **no live consumer** — live CLV true-up reads the Arcadia warehouse
namespace, not football-data. So football-data closing columns are a
**backtest/training substrate**, not a live-pipeline input. That's the right
place for them (strategy validation), but it means "adding AH/OU close" helps
_backtests_, not live alerts.

## 3. Recommendations (prioritized, doctrine-aligned)

### ADOPT

**A1 — Extract Pinnacle Arcadia totals + spreads (LIVE, highest leverage).**
We already fetch them and throw them away. Extracting the sharp OU/spread lines
gives a **live sharp anchor** for the totals/Asian-handicap markets we already
scrape soft prices for on OddsPortal — extending live +EV detection and CLV
grading from H2H-only to OU and AH. Read-only, contained (parse more of an
already-fetched response; the snapshot schema already supports TOTALS/SPREADS
with the line in the selection). Needs: map Pinnacle's spread/total market-key
scheme; persist line in selection like OddsPortal does.

**A2 — Harvest football-data.co.uk closing AH/OU + market-max/avg (BACKTEST).**
Same CSV, zero marginal fetch cost. Adds `PCAHH/PCAHA/AHCh`, `PC>2.5/PC<2.5`,
`MaxC*/AvgC*` to `MatchRow` + parser. Pay-off: backtests can CLV-grade
Asian-handicap and totals strategies (today only 1X2 is gradable), and gain a
**consensus closing fallback** (`MaxC/AvgC`) for fixtures where the single-book
Pinnacle close is missing — directly relevant to the ~31% Pinnacle-archive match
rate. Contingent on wanting to validate AH/totals strategies; low risk.

### REFERENCE / SKEPTICAL (not priorities)

- penaltyblog `metrics` (RPS/Brier/Ignorance) — calibration grading only.
- penaltyblog alternative pricers (Bayesian/Bivariate) and Club Elo / Understat
  xG — permissible **only** as inputs to a better football _pricing_ model to
  devig against, never as standalone signals. The DC goals model already
  backtested negative, so treat any "better predictor" as unproven. Club Elo is
  the cleanest to experiment with (official free CSV API, no scraping).

### REJECT

- **StatsBomb / Opta** (paid; MatchFlow/xT only useful on this paid data),
  **FPL** (no devig/CLV value), **bettingiscool / TheStatsAPI** (paid sharp
  APIs — fail free-first). **API-Football** stays SUSPENDED. penaltyblog upgrade
  (none exists). penaltyblog `implied` (we already have parity-tested devig).
- **OddsPapi** — one live, free-tier (250 req/mo) candidate with Pinnacle +
  historical snapshots that _would_ fit the doctrine; unverified ToS/limits.
  Note only — worth a small coverage spike vs Odds-API + Arcadia if live
  coverage becomes the bottleneck.

## Caveats / sourcing

- penaltyblog 1.11.0 = latest: confirmed from the PyPI project page. penaltyblog
  publishes no per-version feature changelog, so "feature X landed in version Y"
  is not citable — only "present in 1.11.x".
- football-data.co.uk `notes.txt` was read in full (column codes verbatim);
  `data.php`/`englandm.php`/`new_leagues.php` returned HTTP 429, so season-start
  years, the closing/AH-added timeline, and the exact reduced Extra-league column
  list are cross-sourced, not page-verbatim. Free-API endpoint specifics
  (Understat/Club Elo/FPL/OddsPapi) are from web search, not direct calls.
