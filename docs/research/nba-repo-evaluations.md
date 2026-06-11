# NBA repo evaluations — phase-5 inputs (2026-06-11)

Three user-suggested repositories inspected file-by-file (repo-researcher
agents, GitHub MCP / gh api). Full scoring tables in the session transcript;
verdicts and the parts that survive scrutiny below. Baseline for judgment: ADR-0005 /
ADR-0009 (LightGBM + isotonic, walk-forward CLV evaluation, leakage-safe
point-in-time features, nba_api ingestion).

## kyleskom/NBA-Machine-Learning-Sports-Betting — REFERENCE-ONLY (reject code)

1,663★, pushed 2026-01. **No LICENSE (`license: null`) — code may not be
adapted, period.** Methodology fails our bar anyway:

- **Leakage twice over**: per-day team stats fetched with `DateTo=` the game
  date itself (stats.nba.com is inclusive → the game's own box score is in
  its features); the totals model keeps the (closing) `OU` line as a feature.
- **Accuracy-only evaluation**: no CLV, no ROI backtest, no reliability
  diagrams; models literally named by test accuracy.
- **No devig** in EV math; **full Kelly** with no fraction/caps.
- Inference bug: UO feature columns silently misaligned vs training order.
- Safety: clean — GET-only ingestion, display-only Flask; no placement code.

Ideas (not code) worth keeping: `sbrscrape`/SBR as a free per-day NBA odds
source for recent seasons (needs its own ToS review); pushes as an explicit
third class in totals models.

## NBA-Betting/NBA_Betting — MINE-FOR-PARTS (MIT)

206★, ARCHIVED (author moved to NBA-Betting/NBA_AI — inspected below). Modeling/eval layers are what ADR-0005 was
written to avoid (AutoGluon, accuracy metric, zero calibration, single
season split, flat −110 pseudo-Kelly, no closing lines → CLV-blind, naive
Mountain-time datetimes). Safety: clean (no placement code).

**Re-implement these patterns in our stack (MIT permits; credit here):**

1. **Point-in-time nba_api snapshots** — `src/data_sources/team/
nbastats_fetcher.py`: `LeagueDashTeamStats(date_to_nullable=D)` per
   calendar date in dual windows (season-to-date + last-2-weeks), 0.6s
   rate-limit with exponential backoff + jitter, DB-resume on restart.
   The answer to "leakage-free historical team stats" for phase 5.
2. **Point-in-time join rule** — `src/etl/main_etl.py`: `merge_date =
stats.to_date + 1 day` then backward `merge_asof` — stats through D−1
   only ever attach to games on D. Adopt as the canonical stats→game join.
3. **Feature catalogue cross-check** — `src/etl/feature_creation.py`:
   shift(1)-disciplined rolling/expanding features, rest-diff; also a
   negative example (`win_pct` mislabeled net-result rate) for our tests.
4. **Model-cutoff registry rule** — `src/predictions/generator.py`:
   never let a model score a game dated on/before its training-end.

**Do not port:** AutoGluon trainer, accuracy evaluation, covers.com spider,
flat −110 ROI/pseudo-Kelly, naive local datetimes.

## NBA-Betting/NBA_AI — PARTIAL: mine the data loaders, skip the models (MIT) — 2026-06-11

112★/30 forks, MIT, NOT archived but de facto frozen: README says "personal
side project ... no guarantees"; commit 2026-04-08 sets status "stable
release, no active development"; pushes since are Dependabot bumps + a
season_type bugfix (last 2026-04-14). 0 open issues. No setup.py (deliberately
removed), no install-time code execution. Score for THIS project: **4/10**.

Files inspected (gh api, raw contents): README.md, requirements.txt,
src/database_updater/{betting,pbp,covers,nba_official_injuries,prior_states}.py,
src/predictions/{features.py,prediction_utils.py}, scripts/train_legacy_models.py,
src/phase5/model.py, src/web_app/dashboard.py, full git tree, 30 commits.

**Safety: clean.** GET-only ingestion (NBA live CDN + stats.nba.com PBP, ESPN
scoreboard/summary, Covers.com HTML, NBA injury PDFs); display-only Flask.
Code-search 0 hits for place_bet/selenium/betslip/bookmaker login. No
credentials beyond optional .env paths.

**Leakage: materially better than both siblings.** `prior_states.py::
determine_prior_states_needed` uses a strict `date_time < game_datetime`
cutoff (same season, RS+PS only) — no same-day stats. The 43-feature set
(`features.py`) contains NO betting lines; lines live in a separate Betting
table used only for ATS evaluation. `train_legacy_models.py` filters
`g.date_time_utc < cutoff_date` with current-season holdout (temporal, though
not walk-forward). Dashboard even separates leakage-free live predictions
(`LIVE_PREDICTIONS_START`) from retrospective ones.

**Why the modeling core is still rejected for us:** (a) it is ML
winner/spread prediction — the approach our own backtests showed loses
(negative CLV); strategy is line shopping, not model-vs-market; (b) zero
calibration — margin→win-prob is a hardcoded logistic
(`calculate_home_win_prob`, a=−0.2504, b=0.1949), no isotonic, no
Brier/log-loss, evaluation is spread-MAE/accuracy/ATS only — no CLV, no
devig, no staking; (c) Phase5 (Neural-Kalman player hierarchy, PyTorch
~1.4M params) and Phase3 (transformer ~25M) are GPU-trained, checkpoints not
shipped, orthogonal to our LightGBM-first ADR-0005.

**Re-implement these patterns in our stack (MIT permits; credit here):**

1. **NBA official injury-report PDF loader** —
   `src/database_updater/nba_official_injuries.py`: free OFFICIAL source
   (`ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{time}.pdf`,
   multiple intraday editions, 7PM ET most complete), pdfplumber parse to
   body_part/injury_type/side/status, handles the Dec-2025 URL-format change
   and the CDN's 403-means-missing quirk, retries "not_found" within 7 days
   (PDFs publish 7pm ET). THE missing piece for phase-5 injury features.
   Best file in the repo for us.
2. **ESPN scoreboard/summary as free NBA lines source** —
   `src/database_updater/betting.py` 3-tier strategy: ESPN JSON API gives
   DraftKings spread/total/ML with prices in a ~[−7,+2]-day window;
   status-aware caching (lines lock at tipoff → fetch once, finalize, cache
   forever; `lines_finalized` flag, one row per game). Free NBA odds
   cross-check/backfill alongside OddsPortal.
3. **Feature catalogue cross-check** — `features.py`: exp time-decay
   (half-life 10d) team form, rest days, game-frequency over 5/10/30-day
   windows ("rest_play_count"), day-of-season — validates our planned
   rest/B2B/fatigue features; pure pre-game inputs.
4. **PBP dual-endpoint fetch** — `pbp.py::fetch_game_data`: NBA live CDN
   primary, stats.nba.com fallback, period/clock/actionId sort, low
   concurrency (3 threads) after a real throttling incident.

**Do not port / ToS-grey:** Covers.com scraper (`covers.py` — browser-UA
spoofing, bs4; closing-line backfill; reference-only, same posture as
sbrscrape), the entire Phase3/Phase5 PyTorch stack, hardcoded logistic
win-prob, accuracy/ATS-only evaluation, Flask app, SQLite single-file design.
Integration effort for items 1–2: ~1 day each as async httpx + pydantic
clients with tests; items 3–4 are design-time references.
