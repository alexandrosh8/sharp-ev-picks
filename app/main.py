"""Application entrypoint. The safety validator runs before anything else:
importing settings with tampered safety flags aborts startup (ADR-0002)."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.middleware.gzip import GZipMiddleware

from app.api.auth import install_auth, reset_active_credentials, set_active_credentials
from app.api.request_limits import RequestBodyLimitMiddleware
from app.api.routes import router
from app.api.security_headers import SecurityHeadersMiddleware
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
SCHEDULER_CANCELLATION_DRAIN_SECONDS = 5.0
# Fixed signed-bigint key: ASCII-ish "SHARPEVP". A PostgreSQL session lock on
# this ID makes the one-instance deployment invariant executable, not a comment.
SINGLE_INSTANCE_LOCK_ID = 0x5348415250455650

type AsyncCleanup = tuple[str, Callable[[], Awaitable[None]]]


async def _acquire_single_instance_lock(engine: AsyncEngine) -> AsyncConnection | None:
    """Hold the process-wide PostgreSQL advisory lock on a dedicated session."""

    if engine.dialect.name != "postgresql":
        return None

    connection: AsyncConnection | None = None
    try:
        connection = await engine.connect()
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": SINGLE_INSTANCE_LOCK_ID},
        )
        # End the implicit SELECT transaction while keeping the session-level
        # lock on this checked-out connection for the entire app lifespan.
        await connection.commit()
    except Exception as exc:
        if connection is not None:
            try:
                await connection.close()
            except Exception as close_exc:
                logger.error(
                    "single-instance lock connection close failed: %s",
                    type(close_exc).__name__,
                )
        logger.error("single-instance lock acquisition failed: %s", type(exc).__name__)
        raise RuntimeError("single-instance lock acquisition failed") from None

    if acquired is not True:
        try:
            await connection.close()
        except Exception as exc:
            logger.error(
                "contended single-instance connection close failed: %s",
                type(exc).__name__,
            )
        logger.critical("single-instance lock is already held")
        raise RuntimeError("another betting-ai application instance is already running")
    return connection


async def _release_single_instance_lock(connection: AsyncConnection) -> None:
    """Release the advisory lock and always return its dedicated connection."""

    release_error: BaseException | None = None
    try:
        released = await connection.scalar(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": SINGLE_INSTANCE_LOCK_ID},
        )
        await connection.commit()
        if released is not True:
            raise RuntimeError("single-instance advisory lock was not held")
    except BaseException as exc:
        release_error = exc
    try:
        await connection.close()
    except BaseException as exc:
        if release_error is None:
            release_error = exc
    if release_error is not None:
        raise RuntimeError(
            f"single-instance lock release failed: {type(release_error).__name__}"
        ) from None


def _shutdown_scheduler_now(scheduler: object) -> None:
    """Run APScheduler's loop-deferred shutdown synchronously on this loop.

    ``AsyncIOScheduler.shutdown()`` only queues its real work and returns, which
    both hides job-store/executor exceptions and lets dependent resources close
    before task cancellation is delivered. Lifespan already runs on the
    scheduler's event loop, so invoke the decorated implementation directly.
    Test doubles and non-AsyncIO schedulers retain their public synchronous API.
    """
    if not isinstance(scheduler, AsyncIOScheduler):
        scheduler.shutdown(wait=False)  # type: ignore[attr-defined]
        return

    implementation = getattr(AsyncIOScheduler._shutdown, "__wrapped__", None)
    if implementation is None:  # pragma: no cover - APScheduler 3.11 contract guard
        raise RuntimeError("AsyncIOScheduler shutdown implementation is not awaitable")
    try:
        implementation(scheduler, False)
    finally:
        # APScheduler's implementation skips these two lines when an executor
        # or job-store shutdown raises. Always disarm the timer and break the
        # loop reference while allowing the original failure to propagate into
        # lifespan cleanup aggregation.
        scheduler._stop_timer()
        scheduler._eventloop = None


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
    (or a future APScheduler) can still shut down non-blockingly.

    ``AsyncIOExecutor.shutdown()`` cancels every pending future even when its
    ``wait`` argument is true. Therefore shutdown must happen *after* the grace
    wait: pause job dispatch, snapshot the already-running futures, await them,
    then stop the scheduler (cancelling only jobs that exceeded the deadline).
    """
    pending: set[asyncio.Future[object]] = set()
    try:
        pause = getattr(scheduler, "pause", None)
        if callable(pause):
            pause()
        for executor in getattr(scheduler, "_executors", {}).values():
            pending |= set(getattr(executor, "_pending_futures", ()) or ())
        pending = {f for f in pending if not f.done()}
        if pending:
            _done, not_done = await asyncio.wait(pending, timeout=grace_seconds)
            if not_done:
                logger.warning(
                    "scheduler shutdown: %d job(s) still running after %.0fs grace — cancelling",
                    len(not_done),
                    grace_seconds,
                )
    finally:
        shutdown_error: BaseException | None = None
        try:
            _shutdown_scheduler_now(scheduler)
        except BaseException as exc:
            shutdown_error = exc
        # Preserve the duck-typed fallback contract for schedulers whose public
        # shutdown queues its work, and deliver the first cancellation turn for
        # APScheduler tasks before checking which futures still need draining.
        await asyncio.sleep(0)

        # AsyncIOExecutor.cancel() merely schedules CancelledError delivery.
        # Drain the captured tasks to a terminal state so their ``finally``
        # blocks finish before HTTP/Redis/DB teardown. A cancellation-resistant
        # job remains bounded; shutdown then continues with a loud warning.
        cancellation_pending = {future for future in pending if not future.done()}
        if cancellation_pending:
            _done, not_done = await asyncio.wait(
                cancellation_pending,
                timeout=SCHEDULER_CANCELLATION_DRAIN_SECONDS,
            )
            if not_done:
                logger.warning(
                    "scheduler shutdown: %d cancelled job(s) did not finish cleanup within %.0fs",
                    len(not_done),
                    SCHEDULER_CANCELLATION_DRAIN_SECONDS,
                )
        if shutdown_error is not None:
            raise shutdown_error


async def _run_cleanup_steps(
    cleanup_steps: Iterable[AsyncCleanup],
    *,
    primary_error: BaseException | None,
) -> None:
    """Run every lifecycle cleanup without replacing the triggering failure.

    A cleanup failure must never prevent later resources from closing. When the
    lifespan is already unwinding an application/startup exception, cleanup
    failures are logged and attached as notes to that primary exception. On a
    clean shutdown they are surfaced after every cleanup has been attempted.
    """
    failures: list[tuple[str, BaseException]] = []
    for resource_name, cleanup in cleanup_steps:
        try:
            await cleanup()
        except BaseException as exc:
            failures.append((resource_name, exc))
            logger.error(
                "lifespan cleanup failed for %s: %s",
                resource_name,
                type(exc).__name__,
            )

    if not failures:
        return
    if primary_error is not None:
        for resource_name, cleanup_error in failures:
            primary_error.add_note(
                f"cleanup also failed for {resource_name}: {type(cleanup_error).__name__}"
            )
        return

    errors = [
        RuntimeError(f"{resource_name} cleanup failed: {type(cleanup_error).__name__}")
        for resource_name, cleanup_error in failures
    ]
    if len(errors) == 1:
        raise errors[0] from None
    raise BaseExceptionGroup("multiple lifespan cleanup failures", errors) from None


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

    # Lifespan can run more than once in-process (reload/tests). Clear the
    # process-global credential before any startup operation can fail, so a
    # credential loaded from an earlier database is never retained implicitly.
    reset_active_credentials()

    engine = create_engine(settings)
    http_client: httpx.AsyncClient | None = None
    redis: Redis | None = None
    arcadia_http_client: httpx.AsyncClient | None = None
    scheduler: AsyncIOScheduler | None = None
    instance_lock: AsyncConnection | None = None
    scheduler_started = False
    primary_error: BaseException | None = None
    try:
        instance_lock = await _acquire_single_instance_lock(engine)
        session_factory = create_session_factory(engine)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.started_at = asyncio.get_running_loop().time()
        app.state.exposure_seeded = False

        # Load the admin credential (if first-run /setup already created one)
        # into the in-memory auth holder. Auth falls back to the .env trio, then
        # to the first-run /setup screen when neither exists.
        try:
            async with session_factory() as cred_session:
                stored_credentials = await load_dashboard_credentials(cred_session)
            if stored_credentials is not None:
                set_active_credentials(*stored_credentials)
        except Exception as exc:
            logger.error("dashboard credential load failed: %s", type(exc).__name__)
            if settings.dashboard_auth_enabled:
                raise RuntimeError("dashboard credential load failed") from None

        # The exposure ledger is in-memory: seed it from today's persisted picks
        # BEFORE the scheduler starts, or a mid-day restart doubles the day's
        # recommendable exposure (re-detections reserve-then-release to ~0).
        ledger = exposure_ledger(settings)
        try:
            await seed_exposure_ledger(ledger, session_factory)
        except Exception as exc:
            # Starting with an empty ledger after a DB read failure defeats every
            # daily/per-event risk cap until process restart. Abort startup; the
            # orchestrator will keep readiness red and retry instead of serving
            # unaccounted recommendations.
            logger.critical("exposure ledger seeding failed: %s", type(exc).__name__)
            raise RuntimeError("exposure ledger could not be seeded") from exc
        app.state.exposure_seeded = True

        http_client = httpx.AsyncClient()
        redis = build_redis_client(settings)
        app.state.redis = redis
        arcadia_proxy_urls = settings.arcadia_proxies()
        if arcadia_proxy_urls:
            from app.ingestion.pinnacle_arcadia import build_arcadia_proxy_http_client

            arcadia_http_client = build_arcadia_proxy_http_client(arcadia_proxy_urls)
            logger.info(
                "arcadia outbound proxy rotation enabled: %d proxies",
                len(arcadia_proxy_urls),
            )
        scheduler = build_scheduler(
            settings,
            http_client,
            redis,
            session_factory=session_factory,
            ledger=ledger,
            arcadia_http_client=arcadia_http_client,
        )
        app.state.scheduler = scheduler
        app.state.expected_poll_sports = tuple(
            getattr(scheduler, "_expected_poll_sports", ()) or ()
        )
        scheduler.start()
        scheduler_started = True
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        # Bounded graceful stop BEFORE closing the clients the jobs depend on:
        # tearing down httpx/Redis/engine under an in-flight cycle turns a
        # routine SIGTERM into spurious mid-cycle errors (ops audit WP7). A
        # scheduler.start() failure still reaches this cleanup block, but must
        # not call shutdown on a scheduler that never entered the running state.
        cleanup_steps: list[AsyncCleanup] = []
        if scheduler_started and scheduler is not None:
            cleanup_steps.append(
                (
                    "scheduler",
                    lambda: _shutdown_scheduler_gracefully(
                        scheduler,
                        SCHEDULER_SHUTDOWN_GRACE_SECONDS,
                    ),
                )
            )
        if arcadia_http_client is not None:
            cleanup_steps.append(("Arcadia HTTP client", arcadia_http_client.aclose))
        if scheduler is not None:
            for index, owned_client in enumerate(
                getattr(scheduler, "_owned_http_clients", ()) or ()
            ):
                cleanup_steps.append(
                    (f"scheduler-owned HTTP client #{index + 1}", owned_client.aclose)
                )
        if http_client is not None:
            cleanup_steps.append(("shared HTTP client", http_client.aclose))
        if redis is not None:
            cleanup_steps.append(("Redis client", redis.aclose))
        if instance_lock is not None:
            cleanup_steps.append(
                (
                    "single-instance lock",
                    lambda: _release_single_instance_lock(instance_lock),
                )
            )
        cleanup_steps.append(("database engine", engine.dispose))
        await _run_cleanup_steps(cleanup_steps, primary_error=primary_error)


def create_app() -> FastAPI:
    # Ops audit WP7: no public API schema in production — /docs, /redoc and
    # /openapi.json enumerate every endpoint (incl. manual-settlement routes)
    # to anonymous visitors behind the reverse proxy. Local/dev keeps them.
    production = get_settings().is_production
    app = FastAPI(
        title="betting-ai — manual-betting +EV picks (decision support)",
        description=("Generates +EV picks for manual review. This system NEVER places bets."),
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(router)
    install_auth(app)
    return app


app = create_app()
