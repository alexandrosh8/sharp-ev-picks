# Roadmap — 8 Phases

> Status legend: ✅ done · 🔜 next · ⏳ later. Updated 2026-06-10.

## Phase 1 — Claude environment + scaffold ✅ (this engagement)

**Delivered:** CLAUDE.md (hard safety rules), 15 project agents, 9 project
skills, tested hooks (fail-closed secret gate), markdown memory + ADRs
0000-0010, research logs (Claude env, betting repos, GBM, free odds), full
`app/` scaffold: oracle-validated pure math (devig×4/Kelly/edge/exposure/CLV),
pydantic schemas + safety-validated config, 14-table warehouse + alembic,
read-only ingestion (Odds API rotation + football-data loader), idempotent
Telegram/webhook alerts, APScheduler pipeline, FastAPI, backtest math, CI +
safety audit, 115+ tests.
**Acceptance:** CLAUDE.md loads; GitHub MCP works; gitleaks clean; safety
rules active in agents+hooks+config validator+CI grep. ✅

## Phase 2 — Live ingestion 🔜

**Deliverables:** event/entity resolution (external_ref ↔ events/teams,
normalization tables), persistence wiring for the pipeline (snapshots,
detected_edges, picks rows), kickoff-aware polling cadence + redundant
closing-snapshot job, data-quality gates (row counts, freshness, null rates).
**Acceptance:** odds snapshots accumulate for ≥2 football leagues + NBA
within the free credit budget; stale odds flagged and rejected; re-runs
idempotent; no automatic betting code (safety audit green).

## Phase 3 — Football MVP model ⏳

**Deliverables:** penaltyblog used directly (user direction 2026-06-10) for
Dixon-Coles fitting over football-data.co.uk history; score-matrix market
derivation (1X2/totals first); per-league calibration reports
(Brier/log-loss/reliability); walk-forward backtest with signal-time prices
(harness pattern from georgedouzas/sports-betting); model registered in
`model_versions`; parity tests of our devig vs penaltyblog's implied module.
**Acceptance:** calibrated 1X2 + totals probabilities for ≥2 leagues;
backtest report in docs/backtesting/ with CLV — no live picks until
calibration gates pass.

## Phase 4 — Result tracking + CLV loop ✅ (2026-06-10)

**Delivered:** settlement engine (`app/settlement/`): pure outcome mapping
for every live market (1X2/ML, totals, BTTS, DNB, DC, AH half-lines, EH
3-way), free results sources mapped from league slugs (martj42 international
CSV for the World Cup; football-data.co.uk new-league + season CSVs for
clubs) into a normalized-name ±1-day ScoreBook, hourly `settle_results` job
(silent-empty refusal, idempotent via `uq_result_tracking_pick`), manual
event-level settlement `POST /events/{id}/result` + dashboard settle button
(covers NBA/euroleague — no free feed), `GET /performance` ROI +
stake-weighted log-CLV report + dashboard cards. Live CLV true-up was
already running (`app/clv_trueup.py`); settling freezes a pick's CLV.
**Acceptance met:** auto- and user-recorded results produce ROI/CLV reports;
settled picks carry outcome/pnl/roi/settled_at and frozen clv_log/beat_close.

## Phase 5 — NBA MVP model ⏳

**Deliverables:** nba_api ingestion (direct dependency), schedule/form/
availability feature builders, LightGBM + isotonic calibration
(ADR-0005/0009; lightgbm+xgboost installed from the `models` group),
sportsbookreview 2011-2021 historical odds import + validation, walk-forward
evaluation with the XGBoost challenger.
**Acceptance:** calibrated moneyline/spread/totals probabilities with
rest/B2B/availability features; ECE-shrunk edges; backtest + CLV report.

## Phase 6 — Edge engine hardening ⏳

**Deliverables:** devig method sweep on real CLV data (revisit ADR-0006;
evaluate penaltyblog's odds-ratio/logarithmic methods + goto_conversion),
market-prior blending per league, optional Betfair read-only data (clean-room
client; historical BASIC files), football ensemble experiment (ADR-0004
phase-6 clause).
**Acceptance:** measured devig/blending choices documented in ADR revisions;
only +EV picks alert; duplicate alerts impossible (Redis + DB unique).

## Phase 7 — Dashboard ⏳

**Deliverables:** picks dashboard (model vs market probability, edge/EV/CLV
filters, reasoning display), performance views (ROI, CLV, drawdown), served
from the existing FastAPI app.
**Acceptance:** user can review picks and reasoning clearly; every view
carries the manual-betting reminder.

## Phase 8 — Ubuntu/OpenClaw deployment ⏳

**Deliverables:** production compose (app profile), systemd/Docker restart
policies, log collection, port dance vs OpenClaw, backup strategy for
Postgres volumes, `docs/deployment/ubuntu-openclaw.md` executed.
**Acceptance:** runs unattended on the VPS; identical behavior to Mac; CI
green; safety audit green in production image.
