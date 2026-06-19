"""DB integration for the first-run credential store (compose Postgres; skip if
absent). Proves load/create round-trips and the one-shot guard at the real
storage layer. Everything runs inside a transaction that is rolled back, so the
live ``dashboard_credentials`` row (if any) is never disturbed.
"""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.storage.models import DashboardCredential
from app.storage.repositories import (
    create_dashboard_credentials,
    load_dashboard_credentials,
)

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai"
_HASH = "pbkdf2_sha256$600000$abcd1234abcd1234$deadbeefdeadbeefdeadbeefdeadbeef"
_SECRET = "synthetic-test-session-secret"


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


async def test_create_load_round_trip_and_one_shot(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session:
        # isolate from any real credential the live app may have set
        await session.execute(delete(DashboardCredential))
        await session.commit()

        assert await load_dashboard_credentials(session) is None

        created = await create_dashboard_credentials(
            session, username="admin", password_hash=_HASH, session_secret=_SECRET
        )
        assert created is True
        assert await load_dashboard_credentials(session) == ("admin", _HASH, _SECRET)

        # one-shot: a second create writes NOTHING and leaves the row unchanged
        again = await create_dashboard_credentials(
            session, username="intruder", password_hash="pbkdf2_sha256$1$ff$00", session_secret="x"
        )
        assert again is False
        assert await load_dashboard_credentials(session) == ("admin", _HASH, _SECRET)
