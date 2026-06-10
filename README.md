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

## Quickstart (Mac local development)

```bash
git clone <repo>
cd betting-ai
cp .env.example .env
docker compose up -d postgres redis
uv sync
uv run python -m app.main
```

Production target: Ubuntu Linux VPS (Docker Compose, OpenClaw-compatible).
See `docs/deployment/`.

## Project status

- [x] Phase A — Claude Code environment (CLAUDE.md, agents, skills, hooks, memory)
- [x] Phase B — Repository-grounded research (odds sources, models, math)
- [x] Phase C — Architecture + ADRs 0000-0011
- [x] Phase D — Production scaffold: oracle-validated math core, schemas,
      14-table DB + alembic, read-only ingestion, idempotent alerts,
      APScheduler pipeline, FastAPI, CI + safety audit (132 tests)
- [ ] Next: roadmap phase 2 — live ingestion + persistence (`docs/roadmap.md`)

## Documentation

- `docs/adr/` — architecture decision records
- `docs/research/` — repository & data-source research logs
- `docs/security/` — security notes and reviews
- `docs/backtesting/` — backtesting methodology and results
- `docs/deployment/` — Mac dev + Ubuntu/OpenClaw deployment guides
