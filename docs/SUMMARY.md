# Engagement Summary — Manual-Betting +EV Picks Platform (2026-06-10)

The 12 required output sections, with pointers to the full artifacts.

## 1. Claude environment setup result

Delta pass over an already-rich global setup (superpowers v5.0.7 + 10
VoltAgent packs pre-installed). Created project-locally: custom CLAUDE.md
(hard safety rules first), 15 agents (every one carries the no-autobet
forbidden-action), 9 skills (all with "Use when" triggers + Gotchas), 5
tested hooks (bash guard blocks rm -rf/&&/curl|sh; **fail-closed gitleaks
staged-secret gate**; ruff format; scoped pytest; memory reminder), markdown
memory (ADR-0001 — claude-mem and claude-memory-compiler deep-inspected and
rejected: raw-transcript capture without secret redaction, compiled-bundle
installs, no license respectively). Full log:
`docs/research/claude-environment-research-log.md`.

## 2. MCP discovery log (betting system)

17 research agents, file-level inspection, zero hallucinated contents
(fetch failures recorded). Verdicts: OddsHarvester adopt-pattern/backfill
tool; penaltyblog direct dependency (ADR-0011); Sports-Betting-ML-Tools-NBA
reference-only (no license, hardcoded API key); mberk/shin test-oracle (its
exact values now pass against our Shin to 1e-6); WagerBrain reference-only
(**flagship Kelly has a p/q-swap bug** — now a regression guard in our
tests). Wave 3: nba_api adopted (dependency), kyleskom Kelly tests as oracle,
georgedouzas backtest-harness pattern, betfairlightweight BANNED as import
(ships bet execution), soccerdata cache-pattern, ProphitBet cautionary.
Full tables: `docs/research/betting-repo-research.md`.

## 3. Recommended architecture

Async monolith: read-only ingestion → 14-table Postgres warehouse →
models → calibration → devig → gated edge engine → fractional-Kelly
recommender → idempotent Telegram/webhook alerts → FastAPI; Redis for
idempotency; APScheduler in-process. Mermaid diagram + data-flow narrative:
`docs/architecture.md`.

## 4. Football model decision

Dixon-Coles MVP (time-decay `exp(−ξt)`, ρ low-score correction, score-matrix
derivation of 1X2/totals/AH/BTTS, xG-blended ratings), implemented via
penaltyblog directly (ADR-0011) with parity tests; ML ensemble deferred to
phase 6. Full math: `docs/adr/adr-0004-football-model.md`.

## 5. NBA model decision

LightGBM-first with isotonic calibration; rest/B2B/travel/pace/Four-Factors/
availability features (leakage-safe, as-of pre-tipoff); variance handled by
pricing markets (not winners) and ECE-shrunk edges; nba_api ingestion.
`docs/adr/adr-0005-nba-model.md`.

## 6. Vig-free probability method

Power devig default for 2-way and 3-way; Shin for insider/longshot-suspect
books; multiplicative universal fallback; additive never-default (negative-
prob risk); exchange midpoint deferred; same method on both sides of any CLV.
`docs/adr/adr-0006-devig-per-market.md`. Implementation oracle-validated
against mberk/shin and penaltyblog (1e-8).

## 7. Edge & stake mathematics

`q=1/d`; `p_fair = devig(d)`; `edge = p_model − p_fair`;
`EV = p_model(d−1) − (1−p_model)`; gates: EV>0 ∧ EV≥0.01 ∧ edge≥0.03 ∧
confidence≥0.60 ∧ age≤300s ∧ liquidity≥min; stake =
min(0.25·Kelly, 2%) then 5% daily ledger — **informational only**.
Spec: `docs/architecture.md` §Edge engine; code: `app/probabilities`,
`app/edge`, `app/risk` (oracle-tested).

## 8. Production repository structure

Full tree implemented: `app/` (main, config, database, scheduler, pipeline,
schemas/, ingestion/, features/, models/, probabilities/, edge/, risk/,
notifications/, backtesting/, api/, storage/), `alembic/`, `tests/` (132
passing), `scripts/`, `docs/`, `.claude/` (agents/skills/hooks/memory),
Docker + CI.

## 9. Python scaffolding

Working, typed, tested: pydantic odds/picks/result schemas (UTC-enforced,
frozen); 4-method devig; Kelly with transparent decomposition + caps;
gate evaluator with named reject reasons; daily exposure ledger; Odds API
client (3-key rotation, secret-scrubbed errors); football-data CSV loader
(Pinnacle closing columns); Telegram/webhook sinks (never-raise) + Redis
idempotency; APScheduler pipeline; FastAPI (/picks, /picks/{id}/result,
/health); CLV + bankroll-path/ROI/drawdown math. **No execution code
anywhere** (CI-enforced).

## 10. Database schema

14 tables with uniqueness/indexes/CLV+ROI fields, applied to live Postgres
via alembic `bc9e18be0148`: `docs/db-schema.md`.

## 11. Docker deployment plan

compose (postgres:16 + redis:7 on host ports 5433/6380 + prod app profile),
python:3.12-slim non-root Dockerfile, Mac runbook
(`docs/deployment/mac-local.md`), Ubuntu/OpenClaw runbook with port
coexistence, backups, restart policies
(`docs/deployment/ubuntu-openclaw.md`).

## 12. Implementation roadmap

8 phases with deliverables + acceptance criteria (`docs/roadmap.md`).
Phase 1 ✅ (this engagement). Next: phase 2 — live ingestion + persistence
wiring + kickoff-aware closing-snapshot capture.

---

### Verification evidence (2026-06-10)

132 tests passed · ruff clean · mypy clean (41 files) · compose valid ·
alembic at head on live Postgres · safety audit PASSED (8 checks) ·
gitleaks: no leaks · 10 checkpoint commits.

> Manual review required. This system does not place bets.
