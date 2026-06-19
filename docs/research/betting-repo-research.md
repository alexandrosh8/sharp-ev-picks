# Betting Repository Research (Phase B)

- **Date:** 2026-06-10
- **Method:** 17 parallel read-only research agents over the plugin GitHub MCP
  server (`mcp__plugin_everything-claude-code_github__*`), with authenticated
  `gh api` as fallback when the MCP hit upstream rate limits (recorded per
  repo). Every claim is from actually-fetched files — agents quoted at least
  one real function per core file as inspection proof. Nothing was cloned or
  executed.
- **Waves:** (1) the 5 mandated repos + LightGBM-vs-XGBoost docs comparison +
  the user-mandated free-odds deep-dive; (2) 4 discovery agents across ~30
  search terms + the `betting-odds` topic; (3) 6 top candidates inspected with
  the same schema. Uninspected candidates are listed in the appendix and are
  NOT recommended.
- **Clean-room policy:** application math in `app/` is written fresh;
  inspected repos serve as math references, design patterns, and TEST ORACLES
  only (user decision, 2026-06-10).

## Scoring table — mandated repos (Wave 1)

| Repository                           | Category           | Stars/activity                           | Core function                                                                                                                                                                                                      | Code quality                                                                                                                                                                          | Maintenance | Directly reusable         | Best file/function to adapt                                                                                                                                            | Security concern                                                                                                                                                                  | Auto-betting risk                                                                                | Final decision                                                                                                                                                                      |
| ------------------------------------ | ------------------ | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| jordantete/OddsHarvester             | Odds scraping      | 185★, push 2026-05-21                    | Playwright scraper of oddsportal.com: `upcoming` (pre-match) + `historic` (past seasons w/ closing odds + results), 10 sports/100+ leagues, per-bookmaker odds, opening odds + odds-movement history, JSON/CSV/S3  | 4/5 — CI + live scraper-canary workflow + codecov, ~30 unit tests + HAR-fixture integration suite, ruff+bandit; minus broad excepts, a year-boundary timestamp bug, string-typed odds | active      | yes (pip `oddsharvester`) | `core/market_extraction/odds_parser.py` (parse_market_odds, parse_odds_history_modal — pure HTML parsing, unit-tested, yields per-book odds + opening/closing history) | MIT, no malicious code; **ToS risk: oddsportal forbids scraping** — tool ships anti-blocking features (proxies, UA spoofing). Personal-use risk = IP bans/data gaps; never resell | **None** — 0 hits for order/login/bet-slip code; Playwright reads an aggregator, not a bookmaker | **adopt-pattern** (one-off historical NBA backfill only, accepting ToS risk; NOT a pipeline dependency)                                                                             |
| martineastwood/penaltyblog           | Football analytics | 171★, push 2026-06-09, v1.11.0           | Dixon-Coles/Poisson/bivariate models, **7 devig methods** (multiplicative, additive, power, Shin, margin-weighting, odds-ratio, logarithmic), Kelly, backtest, scrapers (football-data, Understat, ClubElo, FBref) | 4/5 — 58-file pytest suite (vcr-mocked scrapers), CI+codecov; minus heavy deps (plotly/pulp/Cython), compiled extensions                                                              | active      | partial                   | `penaltyblog/implied/implied.py` (7 self-contained scipy/numpy devig functions ~40 lines each) + `models/` Dixon-Coles likelihood w/ time decay                        | Low; scrapers hit only free public endpoints                                                                                                                                      | **None** — 0 hits for order endpoints/betfair/selenium                                           | **test-oracle** (DEV-only dependency for clean-room parity tests of devig + Dixon-Coles; NOT a runtime dep — football-only, dep-heavy, naive backtester)                            |
| nealmick/Sports-Betting-ML-Tools-NBA | NBA ML             | 88★, main tip 2025-03-16 (rebased dates) | TF/Keras MLP score regression on balldontlie.io stats; Django web UI; the-odds-api arbitrage module                                                                                                                | 2/5 — zero real tests, no CI, committed db.sqlite3 + artifacts, **hardcoded RapidAPI key in source**, HTTP request at import time                                                     | stale       | no                        | `predict/arb.py` (the-odds-api v4 ingestion + ~60-book BOOKMAKER_MAPPING + best-odds aggregation) — pattern only                                                       | **No license** (reuse legally barred); committed live-looking credential; committed user DB                                                                                       | None found (searched all modules incl. 3618-line views.py)                                       | **reference-only** (feature-list/API-mapping reference; its missing rest/B2B features confirm our NBA feature plan adds value)                                                      |
| mberk/shin                           | Vig stripping      | 101★, push 2026-03-24                    | Reference Shin (1993) implementation: Rust core + pure-Python fallback, dual-validated                                                                                                                             | 5/5 — parametrized tests covering both optimisers against identical expected values; maturin wheels for all majors                                                                    | maintained  | yes                       | `python/shin/__init__.py` (complete pure-Python algorithm: fixed-point `_optimise`, analytic 2-way z, probability mapping)                                             | None — zero runtime deps, no network/credentials                                                                                                                                  | **None** — full 22-file tree enumerated                                                          | **test-oracle** — its exact value pairs are now IN our test suite (`tests/test_devig.py::test_shin_oracle_*`) and our clean-room Shin matches to 1e-6                               |
| sedemmler/WagerBrain                 | Betting math       | 305★, push **2020-05-02 (6y stale)**     | Odds conversion, implied probs, payouts, Kelly, arb/value strats                                                                                                                                                   | 2/5 — **flagship `basic_kelly_criterion` has a p/q swap bug** (returns −0.10 where correct Kelly is +0.10 at p=0.55/evens); placeholder tests; unreviewable committed .db blobs       | stale       | no                        | `utils.py` vig()/bookmaker_margin() (correct 2-way/3-way overround cross-checks)                                                                                       | MIT; committed binary blobs — never load them                                                                                                                                     | **None** — every Python file inspected                                                           | **reference-only**; bankroll.py explicitly blacklisted as Kelly reference — its bug is now a regression guard in our `tests/test_kelly.py::test_kelly_sign_guards_against_p_q_swap` |

## Scoring table — discovery candidates (Wave 3)

| Repository                                   | Category              | Stars/activity                          | Core function                                                                                                                       | Maintenance | Directly reusable | Best file/pattern                                                                                              | Auto-betting risk                                                                              | Final decision                                                                                                                                                                                    |
| -------------------------------------------- | --------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ----------- | ----------------- | -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| swar/nba_api                                 | NBA data client       | 3,680★, push 2026-04-06, MIT            | Canonical NBA.com stats wrapper: PlayerGameLogs, LeagueGameLog, box scores v3, live ScoreBoard, static ID maps, read-only odds feed | active      | yes (pip dep)     | consume as dependency; `live/nba/endpoints/odds.py` pattern for read-only odds feeds                           | None (0 hits)                                                                                  | **adopt-pattern → planned pip dependency for NBA ingestion (roadmap phase 5)**                                                                                                                    |
| kyleskom/NBA-Machine-Learning-Sports-Betting | NBA picks ML          | 1,661★, push 2026-01-09, **NO LICENSE** | XGBoost ML pipeline for NBA moneyline+totals with EV and Kelly outputs                                                              | active      | no                | `Tests/test_kelly_criterion.py` exact-value assertions as independent Kelly oracle                             | None found                                                                                     | **test-oracle** (numeric cross-checks only; no license → zero verbatim reuse)                                                                                                                     |
| georgedouzas/sports-betting                  | Backtesting library   | 712★, push 2026-01-21, MIT              | scikit-learn-style "bettors" for soccer 1X2/OU with TimeSeriesSplit-only walk-forward backtest                                      | maintained  | partial           | `evaluation/_model_selection.py` backtest harness pattern (date-sorted folds, per-market yield/ROI/final-cash) | None (sweep clean)                                                                             | **adopt-pattern** (backtest harness design for roadmap phase 3; soccer-only, column-coupled — not importable)                                                                                     |
| betcode-org/betfair (betfairlightweight)     | Exchange API client   | push 2026-03-16, MIT                    | Betfair API-NG wrapper incl. streaming + historical data                                                                            | maintained  | partial           | `endpoints/historic.py` (~220-line READ-ONLY historical-data client)                                           | **HIGH if imported whole — ships place_orders/cancel_orders + bot login**; flagged per mandate | **adopt-pattern**: clean-room re-implementation of the read-only historic/market-data parts ONLY, if/when Betfair data is added (roadmap phase 6+); the package itself must never be a dependency |
| probberechts/soccerdata                      | Football data scraper | 1.8k★, push 2026-06-01, Apache-2.0      | Multi-source scraper: ClubElo, ESPN, FBref, football-data.co.uk, Sofascore, Understat, WhoScored                                    | active      | partial           | `_common.py` cache-first BaseReader with max_age invalidation + season normalization                           | None found                                                                                     | **adopt-pattern** (cache-first reader design for our loaders; candidate pip dep for xG/Elo enrichment in phase 3)                                                                                 |
| kochlisGit/ProphitBet-Soccer-Bets-Predictor  | Soccer ML app         | 535★, push 2026-04-16, MIT              | PyQt6 GUI 1X2/OU predictor with NN/RF/XGBoost zoo                                                                                   | active      | no                | `preprocessing/statistics.py` shift(1).rolling(n) leakage-safe idiom (already our standard)                    | None found                                                                                     | **reference-only** (no tests, leaky StratifiedKFold CV, accuracy-driven tuning — a catalogue of the mistakes our ADRs forbid)                                                                     |

## Appendix — surfaced but NOT inspected (never recommended)

- gotoConversion/goto_conversion (110★, 2026-05-30) — alternative devig method; candidate for a future ADR-0006 revision.
- mhaythornthwaite/Football_Prediction_Project (100★, 2026-03-25) — EPL pipeline.
- HintikkaKimmo/surebet (84★, 2026-02-06) — arbitrage math library.
- iliyasone/ps3838api (6★, 2026-03-30) — Pinnacle/PS3838 odds API client.
- neeljshah/court-vision (1★, 2026-06-09) — NBA props walk-forward stack.

## What flows into the build

1. **Shin oracle values** from mberk/shin → already added to `tests/test_devig.py`; our clean-room Shin matches to 1e-6. ✅
2. **Kelly p/q-swap regression guard** (WagerBrain's bug) → `tests/test_kelly.py`. ✅
3. **penaltyblog** → DEV-only parity oracle for Dixon-Coles when phase 3 lands; its `implied.py` documents 3 extra devig methods (odds-ratio, logarithmic, margin-weighting) for a future ADR-0006 revision.
4. **nba_api** → planned NBA ingestion dependency (phase 5). **sportsbookreview bundled dataset** → NBA historical odds base (see free-odds-sources.md).
5. **georgedouzas/sports-betting** → walk-forward harness pattern for phase 3 backtesting.
6. **betfairlightweight** → never a dependency (ships bet execution); its read-only historic endpoint shape informs a clean-room Betfair-data client later.

## Fetch-honesty log

- Plugin GitHub MCP hit unauthenticated rate limits on `get_file_contents` for
  several agents mid-run; all affected fetches fell back to authenticated
  read-only `gh api` (alexandrosh8). No contents were invented; every repo row
  carries a files-inspected list in the raw workflow output.
- Stars for two repos were read from repo HTML (API field unavailable) and are
  labeled as such above.

## Wave 4 — user-mandated repos (2026-06-11)

Six repos requested by the user, inspected file-by-file by parallel
repo-researcher agents (same method/schema as Waves 1-3). The two NBA repos
(kyleskom/NBA-Machine-Learning-Sports-Betting, NBA-Betting/NBA_Betting) were
independently re-evaluated and the verdicts CONFIRM the existing entries in
`nba-repo-evaluations.md` (reference-only / mine-for-parts) — see that file,
which also gained the NBA_AI successor evaluation the same day. The four
non-NBA repos:

| Repository                                  | Category         | Stars/activity                 | Core function                                                                                                                                        | Code quality                                                                                                                              | Maintenance                                                        | Directly reusable | Best file to adapt                                                                                                                    | Security concern                                                                                                                                                                                                                                                                                                                                           | Auto-betting risk                                                           | Final decision                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------------------------------------------- | ---------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| kochlisGit/ProphitBet-Soccer-Bets-Predictor | Soccer ML GUI    | 535★, push 2026-04-16          | PyQt6 desktop app: per-league 1X2/OU classifiers (RF/XGB/Keras) on football-data.co.uk CSVs, Optuna+SHAP+Boruta, Selenium FootyStats fixture scraper | 2/5 — copy-paste bugs in feature fallbacks (HAWGD/HALGD reference wrong frame), committed `__pycache__`, chained-assignment anti-patterns | active-bursty, 82 open issues, solo                                | no                | nothing clears the bar — its one correct idiom (`shift(1).rolling`) is already our standard                                           | MIT; none                                                                                                                                                                                                                                                                                                                                                  | **None** (FootyStats scrape is read-only)                                   | **reject (1/10)** — evaluation is leakage-inflated three ways: `StratifiedKFold(shuffle=True)` on time-ordered matches, closing-average odds (AvgC\*) kept as features, isotonic calibration fit on SMOTE-resampled data; accuracy/F1 only, no Brier/CLV/ROI. Never cite its numbers.                                                                                                                                                                                                             |
| ScottfreeLLC/AlphaPy                        | AutoML framework | 1,728★, last real code 2023-04 | YAML-driven AutoML wrapper (sklearn/XGB/LGBM/Keras) with a SportFlow pipeline (bring-your-own-CSV; no sports data loaders)                           | 2/5 — zero tests in 109 files; SportFlow crashes on pandas 2.x (`pd.datetime`); bare excepts; global mutable Model                        | abandoned (final commit steers to paid AlphaPy Pro)                | no                | `sport_flow.py` sports_dict feature checklist (rest days, ATS/cover streaks, rolling cover margins) — 10-minute idea read for phase 5 | Apache-2.0; dead data deps (iexfinance)                                                                                                                                                                                                                                                                                                                    | **None**                                                                    | **idea-only (2/10)** — core CV is random KFold on time-ordered data; calibration is stock `CalibratedClassifierCV` behind a YAML flag; nothing beats direct sklearn/LightGBM.                                                                                                                                                                                                                                                                                                                     |
| georgedouzas/sports-betting                 | Betting toolbox  | 714★, code 2025-12-07          | sklearn-style soccer dataloaders + bettor estimators + TimeSeriesSplit-enforced backtest + BettorGridSearchCV                                        | 4/5 — typed, ruff/mypy/pytest, py.typed; but nptyping (archived) dep, cloudpickle `load_bettor` (unsafe unpickle)                         | slow solo (12-month-old open bug; data branch stale since 2026-01) | no                | `_model_selection.py` backtest() TypeError-unless-TimeSeriesSplit guard — assertion style worth mirroring in `app/backtesting/`       | MIT; DOM-scrapes GitHub HTML for param grids                                                                                                                                                                                                                                                                                                               | **None**                                                                    | **idea-only (2.5/10), DOWNGRADE of the Wave-3 'walk-forward harness pattern' note**: its backtester settles bets at the same near-closing decision odds (no signal-time prices, no slippage, flat stake) — weaker than our walkforward-backtest skill; dataloader serves stale maintainer-hosted CSVs with market-average odds only (no per-book, no Pinnacle → useless for the CLV anchor gap). OddsComparisonBettor (Kaunitz-style consensus) is a ~30-line sanity-baseline idea for backtests. |
| GastonDeMichele/Polymarket-Sports-Bot       | (nonexistent)    | —                              | **Repo and user 404 on GitHub** — no rename redirect, no cache trace                                                                                 | —                                                                                                                                         | —                                                                  | no                | —                                                                                                                                     | The name sits in an SEO-spam cluster of near-identical "Polymarket sports bot" repos from throwaway accounts (known scam/malware vector). The only exact-name match (benedict-anokye-davies/, 0★) was inspected as proxy: proprietary, abandoned, **fully auto-executing** Kalshi momentum bot with committed `.env.bak` and zero devig/fair-value content | Proxy: **YES — auto-betting throughout** (place/cancel/close order scripts) | **reject (0/10)** — uninspectable target dropped permanently; cluster warning recorded in pitfalls.md.                                                                                                                                                                                                                                                                                                                                                                                            |

Verdict pattern across all seven (incl. NBA_AI): every ML-prediction repo
either leaks (shuffled CV, same-day stats, closing odds as features) or skips
calibration entirely, and none evaluates CLV — independent confirmation that
our edge stays line shopping + CLV discipline (see project-status memory).
What survives for phase 5: NBA_AI's injury-PDF loader + ESPN lines client
patterns, the NBA_Betting/kyleskom feature checklists and negative examples,
and `sbrscrape` (flagged for its own future evaluation as a free NBA odds
source).

## 2026-06-19 — penaltyblog + OddsHarvester unused-feature scan (our two spine deps)

Asked: do penaltyblog (`martineastwood/penaltyblog`, MIT) or OddsHarvester
(`jordantete/OddsHarvester`, 0.3.0) have UNUSED features that would help the
picks/dashboard? Judged against the data gate (free sharp anchor + closing
line; outcome models are wrong-shape). Two parallel Explore agents read the
actual module trees.

**penaltyblog → nothing new helps the picks.** It has no odds source / no
Pinnacle, so it cannot touch the only edge that matters. Unused modules are
all **wrong-shape** (BivariatePoisson / ZeroInflated / NegativeBinomial /
WeibullCopula / Bayesian goal models; Elo/Massey/Colley/Pi ratings; xT; FPL —
outcome predictors that lose on CLV) or **redundant** (its `implied` devig =
our `app/probabilities/devig.py`; its `betting.kelly` = our
`app/risk/staking.py`; its scrapers = our ingestion). Only unused bits worth
anything are **dashboard diagnostics**: `metrics.rps_array` (Ranked
Probability Score, a strictly-proper rule, better than Brier for 1X2) and the
Dixon-Coles `create_dixon_coles_grid` score heatmap. Verdict: transparency
candy only — **no pick edge**. (Deferred; not built.)

**OddsHarvester → one genuinely valuable unused feature: HISTORIC mode +
`--odds-history`.** We only use `UPCOMING_MATCHES`
(`app/ingestion/oddsportal.py`). The historic path scrapes, for FREE, each
past match's **opening AND closing odds per bookmaker** from OddsPortal
results pages — the project's biggest data gap (real closing lines vs our
re-priced proxy; a backtestable open→close line-shopping dataset).
**Decisive caveat:** it only closes the _sharp_ gap IF OddsPortal historic
exposes **Pinnacle's** open+close; if only a soft consensus close, it's a real
closing line but not a sharp anchor. Also a heavy, DOM-fragile, ToS-sensitive
per-match scrape. Verdict: **USE — but gate a full backfill on a small
validation probe first** (confirm Pinnacle coverage); not yet run.

Acted on instead (doctrine-safe, ships now): the dashboard **CLOSED tab** (the
proof-of-edge ledger — every kicked-off pick with its close + CLV) + a **CLV
scorecard** (% beat the close, mean CLV). Real OddsHarvester closes would plug
straight into it once the probe passes.

### PROBE RESULT (2026-06-19, `scripts/research/probe_historic_odds.py`)

Ran a bounded read-only HISTORIC scrape — England Premier League 2023-2024,
1 results page, market `1x2`, OddsHarvester's own pacing (no anti-bot bypass).
Outcome:

- **HISTORIC mode works** — 50 matches returned, all 50 with per-bookmaker 1X2
  **closing** odds (`period: FullTime`). So free real closing lines DO exist.
- **Pinnacle is ABSENT.** The 8 books returned were `1xBet, 22Bet, 888sport,
BetInAsia, Betsson, GGBET, N1 Bet, bet365` — the soft books we already pick
  from, **no Pinnacle / no recognized sharp**.
- `odds_history` (opening odds) not exercised in this pass (closing-only run).

**Verdict — the data gate is NOT cleared.** OddsHarvester HISTORIC gives a free
real **closing line at the same soft books we bet** (genuinely useful: grade a
pick against its book's _actual_ close instead of our re-priced proxy), but it
does **not** surface a free **sharp (Pinnacle) anchor** for past matches — the
project's biggest gap stays open. A full historic backfill is therefore **not
worth building for edge**; its only payoff is better soft-book close grading,
which the live re-price loop already approximates. NOT building it.

Residual uncertainty (didn't chase, to avoid more ToS-sensitive load): the run
used the default results-list book set; a per-match-detail scrape or an
explicit `target_bookmaker="Pinnacle"` / `bookies_filter` pass _might_ surface
Pinnacle. First-pass strongly suggests it isn't in the easy/default path. Probe
kept (`scripts/research/probe_historic_odds.py`) so the follow-up is one command.

## 2026-06-19 — 5-stream ultracode sweep: live odds, NBA/tennis/NFL data, scrapers, skills

16-agent workflow (5 parallel research streams + adversarial verify on every
"free sharp source" claim). Net: **no new repo/scraper/source helps; the free
Pinnacle source is already fully ours; the only lever is the Arcadia↔picks
match rate, which is an off-season + alias artifact.**

- **Free live Pinnacle — REAL and already fully implemented.** The
  `guest.api.arcadia.pinnacle.com` GET-only/no-auth endpoint was re-verified
  live (200s on `/sports/{id}/matchups` and `/markets/straight`) and is exactly
  what `app/ingestion/pinnacle_arcadia.py` runs. Correction to the research
  agent's over-claim (caught on grounding): we do **not** discard totals/spreads
  — `extract_total_quotes` (l.275) and `extract_spread_quotes` (l.331) already
  capture moneyline **+ totals + spreads** for soccer/basketball/tennis. There
  is **no market-expansion win left to build.**
- **The real limiter — match rate, measured live: 28/98 = 28.6%.** Breakdown
  from `GET /resolution/match-rate`: 34 `no_archive_candidates` (Pinnacle
  doesn't list these obscure off-season leagues — UNFIXABLE coverage) + 36
  `unmatched_with_candidates` (Pinnacle has the event, team-name join failed —
  FIXABLE via aliases, but for summer-league teams we won't pick in-season).
  `CLV_USE_PINNACLE_ARCHIVE` correctly stays OFF until this is healthy on
  MAJOR leagues; nothing to do now but let coverage accrue.
- **Alternative scrapers — REFUTED.** `whodeanie/live-odds-aggregator` and
  `aqsmith02/paper-betting-tracker` were USE-claimed but the verifier refuted
  both: they default to The Odds API `regions=us` (no Pinnacle) and average all
  books (no sharp isolation). No free scraper beats OddsHarvester+Arcadia.
- **NBA/tennis/NFL data gate — unchanged.** Pinnacle (via Arcadia, forward) is
  available for NBA/tennis, so the sharp anchor exists going forward; the gate
  to MINTING picks is still (a) being in-season so Pinnacle covers the league,
  and (b) a held-out forward CLV validation > 2SE before flipping the flag.
  `trading_alpha_tennis` rejected (outcome-prediction model). No new free
  source clears it; the path is the existing evidence-gated plan, not a model.
- **roundproxies blog — nothing safe & new.** ~8 techniques; the doctrine-safe
  ones (GET hidden JSON APIs, networkidle waits, polite pacing, UA rotation,
  WebSocket monitor) we already use; the rest (2Captcha, `playwright_stealth`,
  login automation, residential-proxy block-evasion) are OFF-LIMITS. The
  verifier confirmed it names no free Pinnacle/closing source.
- **Web/mobile skills.** Top picks: `ce-frontend-design` (compound-engineering),
  `bencium-impact-designer`, `ui-design-system` (mega-skills) — all already in
  the operator's local skill repos (`~/.claude/skills/`), no install needed.
  `sleek-design-mobile-apps` (skills.sh) installs via `npx skillsadd` (unvetted
  executor — not blind-run). React-only skills (shadcn) are reference-only for
  this vanilla-HTML dashboard. Used the installed `frontend-design` skill.
