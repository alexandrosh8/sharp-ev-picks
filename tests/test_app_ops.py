"""App entrypoint ops seams (WP7 fixes 3/4/6): Redis socket timeouts, bounded
graceful scheduler shutdown, and the production API-docs lockdown.

No network, no DB: the Redis client is built but never connects; the scheduler
is a minimal fake exposing APScheduler's executor surface; create_app is
exercised without running the lifespan.
"""

import asyncio
import logging
from types import SimpleNamespace, TracebackType
from typing import Any

import httpx
import pytest
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import (
    SINGLE_INSTANCE_LOCK_ID,
    _acquire_single_instance_lock,
    _release_single_instance_lock,
    _run_cleanup_steps,
    _shutdown_scheduler_gracefully,
    build_redis_client,
    create_app,
    lifespan,
)

# --- fix 3: Redis client carries bounded socket timeouts -------------------- #


async def test_redis_client_built_with_socket_timeouts() -> None:
    settings = Settings.model_construct(
        redis_url="redis://localhost:6399/0",
        redis_socket_connect_timeout_seconds=3.5,
        redis_socket_timeout_seconds=7.0,
    )
    client = build_redis_client(settings)
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["socket_connect_timeout"] == 3.5
        assert kwargs["socket_timeout"] == 7.0
    finally:
        await client.aclose()


def test_postgres_engine_built_with_command_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded per-statement timeout is passed to asyncpg so a wedged DB cannot
    blackhole a shielded persist forever (and thus wedge the poll watchdog). The
    arg is asyncpg-only — omitted for other drivers and when disabled (0)."""
    import app.database as database

    captured: dict[str, Any] = {}

    def _fake_create(url: str, **kwargs: Any) -> str:
        captured.clear()
        captured.update(kwargs)
        return "engine"

    monkeypatch.setattr(database, "create_async_engine", _fake_create)

    database.create_engine(
        Settings.model_construct(
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            db_command_timeout_seconds=25.0,
        )
    )
    assert captured["connect_args"] == {"command_timeout": 25.0}

    database.create_engine(
        Settings.model_construct(
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            db_command_timeout_seconds=0.0,
        )
    )
    assert captured["connect_args"] == {}  # disabled

    database.create_engine(
        Settings.model_construct(
            database_url="sqlite+aiosqlite:///:memory:",
            db_command_timeout_seconds=25.0,
        )
    )
    assert captured["connect_args"] == {}  # non-asyncpg driver never gets the arg


# --- fix 4: bounded graceful shutdown ---------------------------------------- #


class _FakeExecutor:
    def __init__(self, futures: set[Any]) -> None:
        self._pending_futures = futures


class _FakeScheduler:
    """Duck-types the two APScheduler surfaces the graceful stop touches."""

    def __init__(self, futures: set[Any]) -> None:
        self._executors = {"default": _FakeExecutor(futures)}
        self.pause_calls = 0
        self.shutdown_calls: list[bool] = []

    def pause(self) -> None:
        self.pause_calls += 1

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_calls.append(wait)
        # Mirrors AsyncIOExecutor.shutdown: every unfinished future is
        # cancelled regardless of the ``wait`` argument, then tracking clears.
        for executor in self._executors.values():
            for future in executor._pending_futures:
                if not future.done():
                    future.cancel()
            executor._pending_futures = set()


async def test_graceful_shutdown_waits_for_inflight_jobs() -> None:
    finished = asyncio.Event()

    async def job() -> None:
        await asyncio.sleep(0.05)
        finished.set()

    task = asyncio.create_task(job())
    scheduler = _FakeScheduler({task})
    await _shutdown_scheduler_gracefully(scheduler, grace_seconds=5.0)
    assert finished.is_set()  # the in-flight job completed BEFORE we returned
    assert scheduler.pause_calls == 1
    assert scheduler.shutdown_calls == [False]  # scheduling stopped, non-blocking


async def test_graceful_shutdown_gives_up_after_grace_timeout() -> None:
    async def hung_job() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(hung_job())
    scheduler = _FakeScheduler({task})
    await asyncio.wait_for(
        _shutdown_scheduler_gracefully(scheduler, grace_seconds=0.05),
        timeout=5.0,
    )
    assert scheduler.pause_calls == 1
    assert task.cancelled()  # timed out, then APScheduler shutdown cancelled it


async def test_graceful_shutdown_with_no_inflight_jobs_is_immediate() -> None:
    scheduler = _FakeScheduler(set())
    await _shutdown_scheduler_gracefully(scheduler, grace_seconds=5.0)
    assert scheduler.pause_calls == 1
    assert scheduler.shutdown_calls == [False]


async def test_graceful_shutdown_waits_for_deferred_scheduler_stop() -> None:
    class DeferredShutdownScheduler(_FakeScheduler):
        def __init__(self) -> None:
            super().__init__(set())
            self.stopped = False

        def shutdown(self, wait: bool = True) -> None:
            self.shutdown_calls.append(wait)
            asyncio.get_running_loop().call_soon(self._finish_shutdown)

        def _finish_shutdown(self) -> None:
            self.stopped = True

    scheduler = DeferredShutdownScheduler()
    await _shutdown_scheduler_gracefully(scheduler, grace_seconds=5.0)
    assert scheduler.stopped is True


async def test_real_asyncio_scheduler_drains_cancelled_job_finalizer() -> None:
    entered = asyncio.Event()
    cleanup_finished = asyncio.Event()
    blocker = asyncio.Event()

    async def job() -> None:
        entered.set()
        try:
            await blocker.wait()
        finally:
            # Two loop turns model real ingestion cleanup that awaits before it
            # releases its DB/HTTP lease.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            cleanup_finished.set()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(job)
    scheduler.start()
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    await _shutdown_scheduler_gracefully(scheduler, grace_seconds=0.0)

    assert cleanup_finished.is_set()
    assert scheduler.running is False
    assert scheduler._eventloop is None


async def test_real_asyncio_scheduler_shutdown_error_reaches_caller() -> None:
    class FailingJobStore(MemoryJobStore):
        def shutdown(self) -> None:
            super().shutdown()
            raise RuntimeError("jobstore shutdown failed")

    scheduler = AsyncIOScheduler(jobstores={"default": FailingJobStore()})
    scheduler.start(paused=True)

    with pytest.raises(RuntimeError, match="jobstore shutdown failed"):
        await _shutdown_scheduler_gracefully(scheduler, grace_seconds=0.0)

    assert scheduler.running is False
    assert scheduler._eventloop is None


class _TrackedClosable:
    def __init__(
        self,
        name: str,
        calls: list[str],
        error: BaseException | None = None,
    ) -> None:
        self.name = name
        self.calls = calls
        self.error = error
        self.closed = False

    async def aclose(self) -> None:
        self.calls.append(self.name)
        self.closed = True
        if self.error is not None:
            raise self.error


class _TrackedEngine:
    def __init__(
        self,
        calls: list[str],
        error: BaseException | None = None,
    ) -> None:
        self.calls = calls
        self.error = error
        self.disposed = False
        self.dialect = SimpleNamespace(name="sqlite")

    async def dispose(self) -> None:
        self.calls.append("engine")
        self.disposed = True
        if self.error is not None:
            raise self.error


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False


class _SessionFactory:
    def __call__(self) -> _SessionContext:
        return _SessionContext()


class _TrackedScheduler:
    def __init__(
        self,
        calls: list[str],
        owned_clients: list[_TrackedClosable],
        *,
        start_error: BaseException | None = None,
        shutdown_error: BaseException | None = None,
    ) -> None:
        self.calls = calls
        self._executors: dict[str, _FakeExecutor] = {}
        self._owned_http_clients = owned_clients
        self.start_error = start_error
        self.shutdown_error = shutdown_error

    def start(self) -> None:
        self.calls.append("scheduler.start")
        if self.start_error is not None:
            raise self.start_error

    def pause(self) -> None:
        self.calls.append("scheduler.pause")

    def shutdown(self, wait: bool = True) -> None:
        self.calls.append("scheduler.shutdown")
        if self.shutdown_error is not None:
            raise self.shutdown_error


async def _no_credentials(session: object) -> None:
    return None


async def _seed_ok(ledger: object, factory: object) -> None:
    return None


class _AdvisoryConnection:
    def __init__(self, results: list[bool], calls: list[str]) -> None:
        self.results = iter(results)
        self.calls = calls

    async def scalar(self, statement: object, parameters: dict[str, int]) -> bool:
        self.calls.append(str(statement))
        assert parameters == {"lock_id": SINGLE_INSTANCE_LOCK_ID}
        return next(self.results)

    async def commit(self) -> None:
        self.calls.append("commit")

    async def close(self) -> None:
        self.calls.append("close")


class _AdvisoryEngine:
    def __init__(self, connection: _AdvisoryConnection, calls: list[str]) -> None:
        self.connection = connection
        self.calls = calls
        self.dialect = SimpleNamespace(name="postgresql")

    async def connect(self) -> _AdvisoryConnection:
        self.calls.append("connect")
        return self.connection


async def test_postgres_single_instance_lock_is_session_scoped_and_released() -> None:
    calls: list[str] = []
    connection = _AdvisoryConnection([True, True], calls)
    engine = _AdvisoryEngine(connection, calls)

    acquired = await _acquire_single_instance_lock(engine)  # type: ignore[arg-type]
    assert acquired is connection
    assert calls == ["connect", "SELECT pg_try_advisory_lock(:lock_id)", "commit"]

    await _release_single_instance_lock(connection)  # type: ignore[arg-type]
    assert calls[-3:] == ["SELECT pg_advisory_unlock(:lock_id)", "commit", "close"]


async def test_postgres_single_instance_lock_refuses_second_process() -> None:
    calls: list[str] = []
    connection = _AdvisoryConnection([False], calls)
    engine = _AdvisoryEngine(connection, calls)

    with pytest.raises(RuntimeError, match="another betting-ai application instance"):
        await _acquire_single_instance_lock(engine)  # type: ignore[arg-type]

    assert calls == ["connect", "SELECT pg_try_advisory_lock(:lock_id)", "commit", "close"]


def _patch_lifespan_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: Settings,
    engine: _TrackedEngine,
    http_client: _TrackedClosable,
    redis_client: _TrackedClosable,
    scheduler: _TrackedScheduler,
    arcadia_client: _TrackedClosable | None = None,
) -> None:
    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "install_scrape_future_handler", lambda loop: None)
    monkeypatch.setattr(main, "create_engine", lambda _settings: engine)
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: _SessionFactory())
    monkeypatch.setattr(main, "load_dashboard_credentials", _no_credentials)
    monkeypatch.setattr(main, "exposure_ledger", lambda _settings: object())
    monkeypatch.setattr(main, "seed_exposure_ledger", _seed_ok)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda: http_client)
    monkeypatch.setattr(main, "build_redis_client", lambda _settings: redis_client)
    monkeypatch.setattr(main, "build_scheduler", lambda *args, **kwargs: scheduler)
    if arcadia_client is not None:
        from app.ingestion import pinnacle_arcadia

        monkeypatch.setattr(
            pinnacle_arcadia,
            "build_arcadia_proxy_http_client",
            lambda proxy_urls: arcadia_client,
        )


async def test_lifespan_holds_instance_lock_until_before_engine_disposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    calls: list[str] = []
    settings = Settings(_env_file=None)
    engine = _TrackedEngine(calls)
    http = _TrackedClosable("http", calls)
    redis = _TrackedClosable("redis", calls)
    scheduler = _TrackedScheduler(calls, [])
    lock = object()
    _patch_lifespan_dependencies(
        monkeypatch,
        settings=settings,
        engine=engine,
        http_client=http,
        redis_client=redis,
        scheduler=scheduler,
    )

    async def acquire(created_engine: object) -> object:
        assert created_engine is engine
        calls.append("lock.acquire")
        return lock

    async def release(acquired_lock: object) -> None:
        assert acquired_lock is lock
        calls.append("lock.release")

    monkeypatch.setattr(main, "_acquire_single_instance_lock", acquire)
    monkeypatch.setattr(main, "_release_single_instance_lock", release)

    async with lifespan(FastAPI()):
        assert calls[:2] == ["lock.acquire", "scheduler.start"]

    assert calls == [
        "lock.acquire",
        "scheduler.start",
        "scheduler.pause",
        "scheduler.shutdown",
        "http",
        "redis",
        "lock.release",
        "engine",
    ]


async def test_lifespan_resets_stale_credentials_before_failed_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main
    from app.api.auth import current_credentials, reset_active_credentials

    reset_active_credentials()
    settings = Settings(_env_file=None)
    calls: list[str] = []
    first_engine = _TrackedEngine(calls)
    first_http = _TrackedClosable("first-http", calls)
    first_redis = _TrackedClosable("first-redis", calls)
    first_scheduler = _TrackedScheduler(calls, [])
    _patch_lifespan_dependencies(
        monkeypatch,
        settings=settings,
        engine=first_engine,
        http_client=first_http,
        redis_client=first_redis,
        scheduler=first_scheduler,
    )
    session_secret = "lifespan-test-session-" + ("q" * 32)

    async def stored_credentials(session: object) -> tuple[str, str, str]:
        return "db-admin", "stored-password-hash", session_secret

    monkeypatch.setattr(main, "load_dashboard_credentials", stored_credentials)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    async with lifespan(FastAPI()):
        credentials = current_credentials()
        assert credentials is not None
        assert credentials.username == "db-admin"

    second_settings = settings.model_copy(
        update={
            "dashboard_auth_enabled": True,
            "dashboard_auth_password_hash": SecretStr(""),
            "dashboard_session_secret": SecretStr(""),
        }
    )
    second_engine = _TrackedEngine(calls)
    _patch_lifespan_dependencies(
        monkeypatch,
        settings=second_settings,
        engine=second_engine,
        http_client=_TrackedClosable("unused-http", calls),
        redis_client=_TrackedClosable("unused-redis", calls),
        scheduler=_TrackedScheduler(calls, []),
    )

    async def failed_credentials(session: object) -> None:
        raise RuntimeError("https://operator:credential@example.invalid/database")

    monkeypatch.setattr(main, "load_dashboard_credentials", failed_credentials)
    monkeypatch.setattr("app.config.get_settings", lambda: second_settings)
    with pytest.raises(RuntimeError, match="^dashboard credential load failed$"):
        async with lifespan(FastAPI()):
            pytest.fail("lifespan yielded after credential read failure")
    assert current_credentials() is None


async def test_lifespan_partial_startup_failure_disposes_engine_without_masking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    calls: list[str] = []
    settings = Settings(_env_file=None)
    dispose_error = RuntimeError("engine dispose failed")
    engine = _TrackedEngine(calls, dispose_error)
    startup_error = RuntimeError("session factory failed")

    def fail_session_factory(created_engine: object) -> object:
        assert created_engine is engine
        raise startup_error

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "install_scrape_future_handler", lambda loop: None)
    monkeypatch.setattr(main, "create_engine", lambda _settings: engine)
    monkeypatch.setattr(main, "create_session_factory", fail_session_factory)

    with pytest.raises(RuntimeError, match="session factory failed") as caught:
        async with lifespan(FastAPI()):
            pytest.fail("lifespan yielded after session factory failure")

    assert caught.value is startup_error
    assert calls == ["engine"]
    assert startup_error.__notes__ == ["cleanup also failed for database engine: RuntimeError"]


async def test_lifespan_start_failure_closes_every_constructed_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    settings = Settings(_env_file=None)
    engine = _TrackedEngine(calls)
    http = _TrackedClosable("http", calls)
    redis = _TrackedClosable("redis", calls)
    owned_close_error = RuntimeError("owned close failed")
    owned = _TrackedClosable("owned", calls, owned_close_error)
    start_error = RuntimeError("scheduler start failed")
    scheduler = _TrackedScheduler(calls, [owned], start_error=start_error)
    _patch_lifespan_dependencies(
        monkeypatch,
        settings=settings,
        engine=engine,
        http_client=http,
        redis_client=redis,
        scheduler=scheduler,
    )

    with pytest.raises(RuntimeError, match="scheduler start failed") as caught:
        async with lifespan(FastAPI()):
            pytest.fail("lifespan yielded after scheduler start failure")

    assert caught.value is start_error
    assert calls == ["scheduler.start", "owned", "http", "redis", "engine"]
    assert any("scheduler-owned HTTP client #1" in note for note in start_error.__notes__)
    assert owned.closed is True
    assert http.closed is True
    assert redis.closed is True
    assert engine.disposed is True


async def test_lifespan_cleanup_failures_do_not_mask_primary_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    settings = Settings(
        _env_file=None,
        arcadia_proxy_urls="http://127.0.0.1:8888",
    )
    cleanup_errors = [RuntimeError(f"cleanup failure {index}") for index in range(1, 6)]
    engine = _TrackedEngine(calls)
    arcadia = _TrackedClosable("arcadia", calls, cleanup_errors[1])
    owned = _TrackedClosable("owned", calls, cleanup_errors[2])
    http = _TrackedClosable("http", calls, cleanup_errors[3])
    redis = _TrackedClosable("redis", calls, cleanup_errors[4])
    scheduler = _TrackedScheduler(
        calls,
        [owned],
        shutdown_error=cleanup_errors[0],
    )
    _patch_lifespan_dependencies(
        monkeypatch,
        settings=settings,
        engine=engine,
        http_client=http,
        redis_client=redis,
        scheduler=scheduler,
        arcadia_client=arcadia,
    )
    primary_error = LookupError("request handler failed")

    with pytest.raises(LookupError, match="request handler failed") as caught:
        async with lifespan(FastAPI()):
            raise primary_error

    assert caught.value is primary_error
    assert calls == [
        "scheduler.start",
        "scheduler.pause",
        "scheduler.shutdown",
        "arcadia",
        "owned",
        "http",
        "redis",
        "engine",
    ]
    assert len(primary_error.__notes__) == len(cleanup_errors)
    assert engine.disposed is True


async def test_lifespan_clean_shutdown_reports_failures_after_all_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    settings = Settings(
        _env_file=None,
        arcadia_proxy_urls="http://127.0.0.1:8888",
    )
    cleanup_errors = [RuntimeError(f"cleanup failure {index}") for index in range(1, 7)]
    engine = _TrackedEngine(calls, cleanup_errors[5])
    arcadia = _TrackedClosable("arcadia", calls, cleanup_errors[1])
    owned = _TrackedClosable("owned", calls, cleanup_errors[2])
    http = _TrackedClosable("http", calls, cleanup_errors[3])
    redis = _TrackedClosable("redis", calls, cleanup_errors[4])
    scheduler = _TrackedScheduler(
        calls,
        [owned],
        shutdown_error=cleanup_errors[0],
    )
    _patch_lifespan_dependencies(
        monkeypatch,
        settings=settings,
        engine=engine,
        http_client=http,
        redis_client=redis,
        scheduler=scheduler,
        arcadia_client=arcadia,
    )

    with pytest.raises(BaseExceptionGroup, match="multiple lifespan cleanup failures") as caught:
        async with lifespan(FastAPI()):
            pass

    assert [str(exc) for exc in caught.value.exceptions] == [
        "scheduler cleanup failed: RuntimeError",
        "Arcadia HTTP client cleanup failed: RuntimeError",
        "scheduler-owned HTTP client #1 cleanup failed: RuntimeError",
        "shared HTTP client cleanup failed: RuntimeError",
        "Redis client cleanup failed: RuntimeError",
        "database engine cleanup failed: RuntimeError",
    ]
    assert calls == [
        "scheduler.start",
        "scheduler.pause",
        "scheduler.shutdown",
        "arcadia",
        "owned",
        "http",
        "redis",
        "engine",
    ]


async def test_cleanup_error_never_logs_or_reraises_secret_url(caplog) -> None:  # type: ignore[no-untyped-def]
    secret = "url-secret-sentinel"
    request = httpx.Request("GET", f"https://example.invalid/path?token={secret}")
    response = httpx.Response(500, request=request)
    source_error = httpx.HTTPStatusError(
        f"failed request {request.url}",
        request=request,
        response=response,
    )

    async def fail() -> None:
        raise source_error

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(RuntimeError, match="HTTPStatusError") as caught,
    ):
        await _run_cleanup_steps([("shared HTTP client", fail)], primary_error=None)
    assert secret not in caplog.text
    assert secret not in str(caught.value)


# --- fix 6: API docs disabled in production ---------------------------------- #


def test_docs_disabled_in_production(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings.model_construct(app_env="production")
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_docs_available_outside_production(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings.model_construct(app_env="local")
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
