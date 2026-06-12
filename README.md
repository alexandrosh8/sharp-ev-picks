# Manual-Betting +EV Picks Platform (betting-ai)

A professional **picks-only decision-support system** that detects Positive
Expected Value (+EV) betting opportunities for **Football/Soccer** and **NBA**.

It ingests sports data and **read-only** odds/market data, builds model
probabilities, removes bookmaker vig, detects +EV edges, computes recommended
stake sizing (fractional Kelly, informational only), sends alerts, and tracks
results, ROI, and Closing Line Value (CLV).

## Safety statement

> **This system does not place bets.** It generates picks for manual review.
> The user decides and places any bet personally on their own accounts.
> There is no bet-execution code path, no bookmaker login automation, and no
> auto-betting flag — by design. All market-data integrations are read-only.
> Betting involves risk; nothing here is a guarantee of profit.

## Running it

### Local dev (Mac)

The app runs on the host; only postgres/redis are containerized:

```bash
git clone <repo>
cd betting-ai
cp .env.example .env
docker compose up -d postgres redis
uv sync --extra football --extra backfill
uv run playwright install chromium
uv run alembic upgrade head
uv run uvicorn app.main:app
```

(Or `bash scripts/run_app.sh` — frees port 8000 first. Mac dev only.)

### Server (Ubuntu/OpenClaw)

The whole platform runs as the Docker Compose stack — the app service builds
its own image (Chromium baked in), runs migrations on every boot, and
`restart: unless-stopped` keeps it alive across crashes and reboots:

```bash
cd /opt/betting-ai
# one-time: create .env (0600) and uncomment COMPOSE_PROFILES=prod in it
docker compose up -d --build
```

Full step-by-step (prereqs, .env keys, verify, logs, updates, backups,
troubleshooting): **`docs/deployment/openclaw-ubuntu.md`**.

## The pick finder that actually works (backtested positive CLV)

The honest result of backtesting (`docs/backtesting/`): a goals model
(Dixon-Coles) does **not** beat the market — negative CLV. But **sharp-vs-soft
line shopping does**: price fair value from the sharpest book (Pinnacle), bet
another book whose price beats it. The v3 maximal-data run (18 leagues ×
7 seasons × two markets, 46k matches; devig × threshold swept on TRAIN only,
one-shot holdout) chose **shin devig, edge ≥ 0.03** — held-out 2024-26:
**n=62, +22.4% ROI, incremental CLV +0.107 (> 2SE), positive even vs the
Max-of-books close; both 1X2 and OU2.5 independently positive.** Those are
the live defaults (~2-3 high-conviction picks/week). Volume tier
`VALUE_MIN_EDGE=0.015`: n=379, +2.5% ROI, incremental CLV +0.019. The number
to trust is the CLV — small-sample ROI is noisy.

The strategy is wired into the running app (`PICK_STRATEGY=value`, the
default): the scheduler polls, persists picks, alerts, and a 30-minute CLV
true-up job refreshes each open pick's closing-line value — the live
discipline that proves (or disproves) edge over time.

```bash
uv run python scripts/value_backtest.py     # prove it (re-runnable)
uv run python scripts/value_picks.py --league world-cup --min-edge 0.015
```

`app/edge/value.py::find_value_bets` is the pure, tested core. Best live data
for it is The Odds API `regions=eu` (includes Pinnacle + many books);
OddsPortal's free scrape works where it lists enough books.
Caveat: real CLV is lower than the best-price backtest — soft books limit
winners. Manual review required; the system never places bets.

## Proven engines, bound together (the master app)

The live spine uses the proven open-source repos directly (ADR-0011/0012):

- **OddsHarvester** scrapes FREE pre-match odds from oddsportal.com →
  `app/ingestion/oddsportal.py` (read-only; oddsportal is an aggregator, not
  a bookmaker).
- **penaltyblog** Dixon-Coles prices football, fitted on free
  football-data.co.uk history → `app/models/football_dc.py`.
- These feed the existing devig → edge-gate → fractional-Kelly → alert
  pipeline, and picks persist to Postgres and serve via `GET /picks`.

See the full live loop on an in-season league (historical fit + live scrape +
real picks + DB persistence):

```bash
uv run python scripts/run_live.py --persist            # Brazil Serie A
uv run python scripts/run_live.py --code ARG --slug argentina-primera-division
```

Or run the whole app (scheduler polls live, persists, serves the API):

```bash
export ODDS_SOURCE=oddsportal
export ODDSPORTAL_FOOTBALL_LEAGUES=brazil-serie-a
export FOOTBALLDATA_NEW_LEAGUE_CODE=BRA          # train DC on Brazil history
uv run uvicorn app.main:app
open http://localhost:8000/                       # the picks dashboard
curl localhost:8000/picks
curl -X POST localhost:8000/picks/1/result -H 'content-type: application/json' \
  -d '{"pick_id":"1","outcome":"won","bet_placed":true,"actual_stake":"10","actual_odds":2.1,"settled_at":"2026-06-10T20:00:00Z"}'
```

`ODDS_SOURCE=oddsportal` (free, default) or `odds_api` (The Odds API).
Set `FOOTBALLDATA_NEW_LEAGUE_CODE` (BRA/ARG/USA/MEX/JPN/CHN) to train on an
in-season non-European league. Production target: Ubuntu Linux VPS (Docker
Compose, OpenClaw-compatible). See `docs/deployment/`.

**New here? Follow `docs/HOW_TO_RUN.md`** — exact commands to verify the
backtest, generate live picks, and run the full platform.

## Project status

- [x] Phase A — Claude Code environment (CLAUDE.md, agents, skills, hooks, memory)
- [x] Phase B — Repository-grounded research (odds sources, models, math)
- [x] Phase C — Architecture + ADRs 0000-0012
- [x] Phase D — Production scaffold: oracle-validated math core, schemas,
      14-table DB + alembic, read-only ingestion, idempotent alerts,
      APScheduler pipeline, FastAPI, CI + safety audit
- [x] Validated pick finder — sharp-vs-soft value strategy, v3 maximal-data
      backtest (46k matches, holdout incremental CLV > 2SE), wired as the
      default live pipeline with 30-min CLV true-up (173 tests)
- [x] Settlement engine (phase 4) — auto-settles from free results sources
      (World Cup, Brazil, European leagues), manual settle button for
      NBA/euroleague, ROI + stake-weighted CLV report on the dashboard
- [ ] Next: bankroll tracking (phase 6) + NBA model (phase 5)

## Documentation

- `docs/adr/` — architecture decision records
- `docs/research/` — repository & data-source research logs
- `docs/security/` — security notes and reviews
- `docs/backtesting/` — backtesting methodology and results
- `docs/deployment/` — Mac dev + Ubuntu/OpenClaw deployment guides
