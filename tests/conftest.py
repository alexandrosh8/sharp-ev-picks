"""Pytest bootstrap for the ISOLATED test database.

DB-touching tests connect to a SEPARATE ``betting_ai_test`` database (the
``DB_URL`` in each ``tests/test_*.py``), never the live ``betting_ai`` warehouse.
This closes the isolation gap that let a fixture's ``commit()`` (e.g. the
snapshot-close ``Snapclose``/``SoftBook`` pick) leak into the running app's
Results view: a stray commit now lands in the throwaway test DB, never live.

This session-scoped, autouse fixture creates that database once and rebuilds its
schema from the ORM metadata (drop+create -> a clean slate each run, so
committing tests can't accumulate across runs). If Postgres is unreachable it is
a silent no-op — the per-test DB fixtures already ``pytest.skip`` on their own
connection probe, and the majority of the suite needs no database at all.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.storage.models import Base

_BASE = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433"
_MAINTENANCE_URL = f"{_BASE}/betting_ai"  # existing DB, used only to CREATE the test DB
_TEST_DB = "betting_ai_test"
_TEST_URL = f"{_BASE}/{_TEST_DB}"


async def _bootstrap() -> None:
    # 1) Create the test database if absent (CREATE DATABASE cannot run inside a
    #    transaction -> AUTOCOMMIT connection to the maintenance database).
    admin = create_async_engine(_MAINTENANCE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": _TEST_DB}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{_TEST_DB}"'))
    finally:
        await admin.dispose()
    # 2) Rebuild a clean schema in the test database from the ORM metadata.
    engine = create_async_engine(_TEST_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_database() -> None:
    """Provision the isolated test DB before any DB-touching test runs."""
    # Postgres absent/unreachable -> DB tests skip themselves; non-DB tests
    # (the majority of the suite) are unaffected.
    with contextlib.suppress(Exception):
        asyncio.run(_bootstrap())


@pytest.fixture(autouse=True)
def _isolate_login_throttle() -> None:
    """The /login throttle (WP7) is module-global in-process state keyed by the
    client address; TestClient defaults every test to the same 'testclient'
    peer, so failed-login tests would otherwise bleed 429s into each other."""
    from app.api.routes import reset_login_throttle

    reset_login_throttle()
