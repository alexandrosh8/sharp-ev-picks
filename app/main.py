"""Application entrypoint. The safety validator runs before anything else:
importing settings with tampered safety flags aborts startup (ADR-0002)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.auth import install_auth, set_active_credentials
from app.api.routes import router
from app.config import Settings, exposure_ledger, get_settings
from app.database import create_engine, create_session_factory
from app.ingestion.oddsportal import install_scrape_future_handler
from app.scheduler import build_scheduler, seed_exposure_ledger
from app.storage.repositories import load_dashboard_credentials

logger = logging.getLogger(__name__)

#: Bounded SIGTERM grace (ops audit WP7): scheduling stops immediately, then
#: in-flight jobs get this long to finish before shutdown proceeds anyway —
#: never an unbounded wait, never an instant teardown under an in-flight cycle.
SCHEDULER_SHUTDOWN_GRACE_SECONDS = 20.0


def build_redis_client(settings: Settings) -> Redis:
    """Redis client with EXPLICIT socket timeouts (ops audit WP7): without
    them a blackholed Redis (dropped packets, hung server) stalls the event
    loop's dedupe/poll paths indefinitely; with them a dead Redis surfaces as
    a bounded TimeoutError the jobs already log-and-survive."""
    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_socket_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )


async def _shutdown_scheduler_gracefully(scheduler: object, grace_seconds: float) -> None:
    """Stop scheduling NEW runs immediately, then wait (bounded) for in-flight
    job futures so SIGTERM does not tear the HTTP/Redis/DB clients out from
    under a mid-cycle job (ops audit WP7). APScheduler's AsyncIOExecutor keeps
    its running futures in `_pending_futures`; duck-typed so a bare test fake
    (or a future APScheduler) degrades to a plain non-blocking shutdown."""
    pending: set[asyncio.Future[object]] = set()
    for executor in getattr(scheduler, "_executors", {}).values():
        pending |= set(getattr(executor, "_pending_futures", ()) or ())
    scheduler.shutdown(wait=False)  # type: ignore[attr-defined]
    pending = {f for f in pending if not f.done()}
    if not pending:
        return
    _done, not_done = await asyncio.wait(pending, timeout=grace_seconds)
    if not_done:
        logger.warning(
            "scheduler shutdown: %d job(s) still running after %.0fs grace — proceeding",
            len(not_done),
            grace_seconds,
        )


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
    # Retrieve + honestly log Playwright wait futures orphaned when a scrape tab
    # closes on a DOM miss, instead of letting asyncio dump them as ERROR
    # ("Future exception was never retrieved"). Real bugs still surface loudly.
    install_scrape_future_handler(asyncio.get_running_loop())

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    # Load the admin credential (if first-run /setup already created one) into
    # the in-memory auth holder; auth falls back to the .env trio otherwise, and
    # to the first-run /setup screen if neither exists.
    try:
        async with session_factory() as cred_session:
            stored_credentials = await load_dashboard_credentials(cred_session)
        if stored_credentials is not None:
            set_active_credentials(*stored_credentials)
    except Exception as exc:
        logger.error("dashboard credential load failed: %s", type(exc).__name__)

    http_client = httpx.AsyncClient()
    redis = build_redis_client(settings)
    # The exposure ledger is in-memory: seed it from today's persisted picks
    # BEFORE the scheduler starts, or a mid-day restart doubles the day's
    # recommendable exposure (re-detections reserve-then-release to ~0).
    ledger = exposure_ledger(settings)
    try:
        await seed_exposure_ledger(ledger, session_factory)
    except Exception as exc:
        logger.error(
            "exposure ledger seeding failed: %s — daily cap restarts EMPTY; "
            "today's earlier picks are not counted against it",
            type(exc).__name__,
        )
    arcadia_http_client: httpx.AsyncClient | None = None
    arcadia_proxy_urls = settings.arcadia_proxies()
    if arcadia_proxy_urls:
        from app.ingestion.pinnacle_arcadia import build_arcadia_proxy_http_client

        arcadia_http_client = build_arcadia_proxy_http_client(arcadia_proxy_urls)
        logger.info("arcadia outbound proxy rotation enabled: %d proxies", len(arcadia_proxy_urls))
    scheduler = build_scheduler(
        settings,
        http_client,
        redis,
        session_factory=session_factory,
        ledger=ledger,
        arcadia_http_client=arcadia_http_client,
    )
    scheduler.start()
    try:
        yield
    finally:
        # Bounded graceful stop BEFORE closing the clients the jobs depend on:
        # tearing down httpx/Redis/engine under an in-flight cycle turns a
        # routine SIGTERM into spurious mid-cycle errors (ops audit WP7).
        await _shutdown_scheduler_gracefully(scheduler, SCHEDULER_SHUTDOWN_GRACE_SECONDS)
        if arcadia_http_client is not None:
            await arcadia_http_client.aclose()
        await http_client.aclose()
        await redis.aclose()
        await engine.dispose()


def create_app() -> FastAPI:
    # Ops audit WP7: no public API schema in production — /docs, /redoc and
    # /openapi.json enumerate every endpoint (incl. manual-settlement routes)
    # to anonymous visitors behind the reverse proxy. Local/dev keeps them.
    production = get_settings().app_env == "production"
    app = FastAPI(
        title="betting-ai — manual-betting +EV picks (decision support)",
        description=("Generates +EV picks for manual review. This system NEVER places bets."),
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.include_router(router)
    install_auth(app)
    return app


app = create_app()
