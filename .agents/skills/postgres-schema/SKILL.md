---
name: postgres-schema
description: "PostgreSQL schema conventions for the betting warehouse. Use when designing tables, writing Alembic migrations, or reviewing app/storage/ changes."
allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
---

# Postgres Schema Conventions

## Purpose

Keep the 14-table warehouse (docs/db-schema.md) consistent, migration-safe,
and free of precision/timezone bugs.

## Procedure

1. Naming: snake*case tables (plural) and columns; indexes
   `idx*<table>\_<cols>`; FKs `<entity>\_id`.
2. Types: `TIMESTAMPTZ` for every timestamp (never naive TIMESTAMP);
   `NUMERIC` for odds, stakes, money, probabilities persisted for audit;
   UUID or BIGINT identity PKs — pick one per table family and stay
   consistent.
3. Append-only tables (odds_snapshots): UNIQUE
   (event_id, bookmaker, market, selection, captured_at); ingestion uses
   ON CONFLICT DO NOTHING; no UPDATE/DELETE paths in code.
4. picks: UNIQUE (event_id, market, selection, model_version_id); CLV
   columns (closing_odds, closing_fair_prob, clv_log, beat_close) nullable
   until settlement.
5. Every change = Alembic migration with downgrade; autogenerate then
   hand-review the diff (server defaults and constraint names drift).
6. Hot-path inserts via SQLAlchemy Core bulk `insert().values([...])`.

## Checklist

- [ ] TIMESTAMPTZ everywhere; no naive datetime columns
- [ ] NUMERIC for odds/money; no FLOAT/REAL
- [ ] Uniqueness constraints match docs/db-schema.md
- [ ] Migration has a working downgrade
- [ ] manual_bet_logs has no credential-shaped columns

## Gotchas

- **`alembic autogenerate` misses server-default changes and check
  constraints** — always read the generated migration before applying.
- **UNIQUE with nullable columns**: Postgres treats NULLs as distinct —
  uniqueness keys must use NOT NULL columns (or NULLS NOT DISTINCT on
  PG15+, document the choice).
- **`captured_at` from providers vs our ingest time** — store both
  (`captured_at`, `ingested_at`); CLV correctness depends on provider time.
- **asyncpg requires `postgresql+asyncpg://` URLs** — a plain
  `postgresql://` URL silently selects psycopg and breaks the async engine.

## Forbidden mistakes

- Editing an already-applied migration.
- Mutating raw snapshot rows to "fix" data.
- Storing credentials, cookies, or session tokens in any table.
