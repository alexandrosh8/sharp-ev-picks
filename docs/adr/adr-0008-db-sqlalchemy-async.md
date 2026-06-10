# ADR-0008: Data Access — SQLAlchemy 2.0 Async + asyncpg + Alembic

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

14-table PostgreSQL warehouse whose CLV/ROI columns will iterate; async app;
typed codebase (mypy strict-ish). Candidates: SQLAlchemy 2.0 (async) + asyncpg

- Alembic; raw asyncpg SQL; other ORMs.

## Decision

SQLAlchemy 2.0 typed ORM (`Mapped[]` declarative models) over **asyncpg**,
with **Alembic** migrations (async template). Hot-path bulk inserts
(odds_snapshots) use Core `insert().values([...])` with
ON CONFLICT DO NOTHING — never per-row ORM flushes.

## Justification

- Schema WILL evolve (CLV fields, calibration metrics, props later):
  migrations with downgrades are non-negotiable; Alembic autogenerate against
  the live dev DB is already proven in this repo (initial migration
  `bc9e18be0148`).
- `Mapped[]` typing keeps the schema mypy-checked end to end and in one
  reviewable file (`app/storage/models.py`).
- asyncpg is the fastest maintained async Postgres driver; the
  `postgresql+asyncpg://` URL is pinned in config (a plain `postgresql://`
  URL silently selects psycopg — encoded as a postgres-schema skill gotcha).

## Alternatives considered

- Raw asyncpg SQL — rejected: stringly-typed, no migration story, hand-rolled
  mapping for 14 tables.
- Lightweight ORMs (tortoise, piccolo) — rejected: smaller ecosystems, weaker
  Alembic-grade migration tooling.

## Consequences

- Append-only tables are enforced by convention + uniqueness constraints;
  code review (database-architect agent) guards against UPDATE paths on
  odds_snapshots.
- Tests that need a DB use the compose Postgres (ports 5433) or aiosqlite for
  pure-ORM shape checks.
