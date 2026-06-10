"""Application entrypoint. The safety validator runs before anything else:
importing settings with tampered safety flags aborts startup (ADR-0002)."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.routes import router
from app.config import get_settings
from app.database import create_engine, create_session_factory
from app.scheduler import build_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # safety validator fires here, first
    logging.basicConfig(level=settings.log_level)

    engine = create_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    http_client = httpx.AsyncClient()
    redis = Redis.from_url(settings.redis_url)
    scheduler = build_scheduler(settings, http_client, redis)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await http_client.aclose()
        await redis.aclose()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="betting-ai — manual-betting +EV picks (decision support)",
        description=("Generates +EV picks for manual review. This system NEVER places bets."),
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
