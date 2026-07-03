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
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# HERMETIC .env OVERRIDE (ADR-0019 H4 guard): app.main constructs Settings() at
# module import, which reads the operator's LIVE .env — the one suite path that
# is not hermetic. The per-market devig guard now (correctly) hard-fails a live
# 'multiplicative' override; that is a DEPLOY concern, never a test outcome. Env
# vars take precedence over .env in pydantic-settings, so pinning the key to its
# safe default ('') keeps every Settings() construction in the suite guard-clean
# WITHOUT weakening the guard (explicit kwargs in tests still override this, so
# the guard's own rejection tests still trip it).
os.environ.setdefault("VALUE_DEVIG_PER_MARKET", "")

from app.storage.models import Base  # noqa: E402

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


@pytest.fixture(autouse=True)
def _isolate_proxy_health_registry() -> None:
    """The shared proxy-health registry (app/ingestion/proxy_health.py) is
    module-global in-process state; failover tests that fail the same pool
    index in several tests would otherwise accumulate consecutive failures
    across tests, quarantine the slot, and change later tests' rotation."""
    from app.ingestion.proxy_health import reset_registry_for_tests

    reset_registry_for_tests()


@pytest.fixture(autouse=True)
def _isolate_settlement_feed_cache() -> None:
    """The settle-feed TTL cache (WP7) is module-global in-process state keyed
    by feed config; an earlier test's SUCCESSFUL fetch would otherwise be
    served from cache to a later test that mocks the feeds down (e.g. the
    refuses-when-providers-empty invariant settled a pick from cached scores
    in CI, where the DB-backed settlement tests actually run)."""
    from app.settlement.engine import clear_feed_cache

    clear_feed_cache()
