"""Hermetic pytest bootstrap, including one Postgres database per worker."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
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

_DEFAULT_TEST_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"
_EXPLICIT_TEST_URL = os.environ.get("TEST_DB_URL")
_DATABASE_NAME = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class _DatabaseSpec:
    test_url: str
    maintenance_url: str
    database_name: str
    managed: bool


def _database_spec(test_url: str, *, managed: bool) -> _DatabaseSpec:
    parsed = make_url(test_url)
    database_name = parsed.database or ""
    if (
        not database_name
        or not _DATABASE_NAME.fullmatch(database_name)
        or "test" not in database_name
    ):
        raise pytest.UsageError(
            "TEST_DB_URL must name a lowercase alphanumeric database containing 'test'"
        )
    return _DatabaseSpec(
        test_url=parsed.render_as_string(hide_password=False),
        maintenance_url=parsed.set(database="postgres").render_as_string(hide_password=False),
        database_name=database_name,
        managed=managed,
    )


def pytest_configure(config: pytest.Config) -> None:
    """Select a unique managed DB before workers collect/import test modules."""
    is_worker = hasattr(config, "workerinput")
    processes = getattr(config.option, "numprocesses", None)
    is_xdist_controller = not is_worker and processes not in (None, 0, "0")
    if is_xdist_controller:
        # Workers inherit the original shell environment and select their own
        # database. Setting TEST_DB_URL here would make them share one DB.
        return

    worker_id = "main"
    if is_worker:
        worker_id = str(config.workerinput.get("workerid", "worker"))  # type: ignore[attr-defined]
    safe_worker = re.sub(r"[^a-z0-9]+", "_", worker_id.lower()).strip("_")[:16] or "worker"

    if _EXPLICIT_TEST_URL is not None and not is_worker:
        # A serial developer run may target a pre-provisioned test DB. It is
        # never dropped by the suite.
        spec = _database_spec(_EXPLICIT_TEST_URL, managed=False)
    else:
        # Parallel workers always derive disposable sibling DBs. When a custom
        # TEST_DB_URL is supplied it acts as the host/credential/name template;
        # the explicitly named database itself remains untouched.
        base = make_url(_EXPLICIT_TEST_URL or _DEFAULT_TEST_URL)
        prefix = base.database or "betting_ai_test"
        if _EXPLICIT_TEST_URL is None:
            prefix = "betting_ai_test"
        suffix = f"_{os.getpid()}_{safe_worker}"
        database_name = f"{prefix[: 63 - len(suffix)]}{suffix}"
        spec = _database_spec(
            base.set(database=database_name).render_as_string(hide_password=False),
            managed=True,
        )

    os.environ["TEST_DB_URL"] = spec.test_url
    # Transitional alias for the historical second environment name.
    os.environ["BETTING_AI_TEST_DB_URL"] = spec.test_url
    config._betting_ai_database_spec = spec  # type: ignore[attr-defined]


def _quote_database(name: str) -> str:
    if not _DATABASE_NAME.fullmatch(name):
        raise ValueError("unsafe generated test database name")
    return f'"{name}"'


async def _bootstrap(spec: _DatabaseSpec) -> None:
    if spec.managed:
        # CREATE DATABASE cannot run inside a transaction.
        admin = create_async_engine(spec.maintenance_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": spec.database_name},
                )
                if exists:
                    await conn.execute(
                        text(f"DROP DATABASE {_quote_database(spec.database_name)} WITH (FORCE)")
                    )
                await conn.execute(text(f"CREATE DATABASE {_quote_database(spec.database_name)}"))
        finally:
            await admin.dispose()

    # Managed DBs are new. For an explicit developer URL, create missing tables
    # without dropping the user-owned database or schema.
    engine = create_async_engine(spec.test_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _drop_managed_database(spec: _DatabaseSpec) -> None:
    admin = create_async_engine(spec.maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(
                text(f"DROP DATABASE IF EXISTS {_quote_database(spec.database_name)} WITH (FORCE)")
            )
    finally:
        await admin.dispose()


def _database_server_reachable(spec: _DatabaseSpec) -> bool:
    parsed = make_url(spec.maintenance_url)
    if parsed.host is None:
        return False
    try:
        with socket.create_connection((parsed.host, parsed.port or 5432), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_database(request: pytest.FixtureRequest) -> Iterator[None]:
    """Provision and later remove this process/xdist worker's database."""
    spec = getattr(request.config, "_betting_ai_database_spec", None)
    provisioned = False
    if isinstance(spec, _DatabaseSpec):
        try:
            asyncio.run(_bootstrap(spec))
            provisioned = True
        except Exception:
            # DB tests retain their own probes and skip when compose is absent;
            # the database-free majority of the suite remains runnable.
            if spec.managed:
                with contextlib.suppress(Exception):
                    asyncio.run(_drop_managed_database(spec))
            if _database_server_reachable(spec):
                raise
    try:
        yield
    finally:
        if provisioned and isinstance(spec, _DatabaseSpec) and spec.managed:
            with contextlib.suppress(Exception):
                asyncio.run(_drop_managed_database(spec))


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
