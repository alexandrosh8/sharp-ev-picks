# ADR-0007: Scheduler — APScheduler AsyncIOScheduler

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

The platform needs recurring jobs: odds polling (~5 min), closing-line
capture (pre-kickoff), settlement/CLV true-up (hourly), bankroll snapshots
(daily). Deployment is a single-process asyncio app on one box (Mac dev →
one Ubuntu VPS). Candidates: APScheduler, Celery, Prefect.

## Decision

**APScheduler 3.x `AsyncIOScheduler`**, running inside the FastAPI process
(started/stopped by the app lifespan). All jobs are registered in ONE place —
`app/scheduler.py::build_scheduler` — so any future orchestrator swap touches
one module.

## Justification

- Jobs are plain `async def` sharing the app's event loop, engine, and HTTP
  client — zero serialization, zero extra processes.
- Celery requires a broker + worker fleet and is sync-first; pure operational
  overhead for one process. Prefect drags a server/agent runtime.
- Redis stays what it already is here: cache + idempotency, not a broker.
- `max_instances=1, coalesce=True` on the poll job prevents overlap pile-ups.

## Alternatives considered

- Celery — rejected (broker/worker overhead, sync-first).
- Prefect — rejected (server/agent runtime for a single box).
- Cron + scripts — rejected: no shared process state, per-job cold starts,
  and the kickoff-aware dynamic cadence (phase 2) needs in-process logic.

## Consequences

- Horizontal scaling would require moving jobs out (the registry function is
  the seam). Accepted: the workload is one box by design.
- Job schedules are code-reviewed config in `app/scheduler.py`; UTC timezone
  pinned explicitly.
