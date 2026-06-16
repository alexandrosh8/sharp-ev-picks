"""Application entrypoint. The safety validator runs before anything else:
importing settings with tampered safety flags aborts startup (ADR-0002)."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.auth import install_auth
from app.api.routes import router
from app.config import get_settings
from app.database import create_engine, create_session_factory
from app.risk.exposure import DailyExposureLedger
from app.scheduler import build_scheduler, seed_exposure_ledger

logger = logging.getLogger(__name__)


def _silence_url_logging() -> None:
    """Pin the HTTP-client loggers to WARNING — httpx logs the FULL request
    URL at INFO ('HTTP Request: ...'), and Telegram bot tokens ride in the
    URL path while Odds API keys ride in query strings. WARNING (not INFO)
    so no URL line is emitted at ANY configured LOG_LEVEL (secret-hygiene
    rule: never log HTTP-client URLs)."""
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # safety validator fires here, first
    logging.basicConfig(level=settings.log_level)
    _silence_url_logging()

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    http_client = httpx.AsyncClient()
    redis = Redis.from_url(settings.redis_url)
    # The exposure ledger is in-memory: seed it from today's persisted picks
    # BEFORE the scheduler starts, or a mid-day restart doubles the day's
    # recommendable exposure (re-detections reserve-then-release to ~0).
    ledger = DailyExposureLedger(max_daily_fraction=settings.max_daily_exposure_percent)
    try:
        await seed_exposure_ledger(ledger, session_factory)
    except Exception as exc:
        logger.error(
            "exposure ledger seeding failed: %s — daily cap restarts EMPTY; "
            "today's earlier picks are not counted against it",
            type(exc).__name__,
        )
    scheduler = build_scheduler(
        settings, http_client, redis, session_factory=session_factory, ledger=ledger
    )
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
    install_auth(app)
    return app


app = create_app()
