"""Async database engine and session factory."""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    # A bounded per-statement timeout keeps a wedged/partitioned Postgres from
    # blackholing a shielded persist forever (see db_command_timeout_seconds).
    # asyncpg-only: the arg is meaningless to other drivers (e.g. aiosqlite).
    connect_args: dict[str, object] = {}
    timeout = settings.db_command_timeout_seconds
    if timeout > 0 and make_url(settings.database_url).get_driver_name() == "asyncpg":
        connect_args["command_timeout"] = timeout
    return create_async_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
