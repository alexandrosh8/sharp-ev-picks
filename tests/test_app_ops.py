"""App entrypoint ops seams (WP7 fixes 3/4/6): Redis socket timeouts, bounded
graceful scheduler shutdown, and the production API-docs lockdown.

No network, no DB: the Redis client is built but never connects; the scheduler
is a minimal fake exposing APScheduler's executor surface; create_app is
exercised without running the lifespan.
"""

import asyncio
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import _shutdown_scheduler_gracefully, build_redis_client, create_app

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


# --- fix 4: bounded graceful shutdown ---------------------------------------- #


class _FakeExecutor:
    def __init__(self, futures: set[Any]) -> None:
        self._pending_futures = futures


class _FakeScheduler:
    """Duck-types the two APScheduler surfaces the graceful stop touches."""

    def __init__(self, futures: set[Any]) -> None:
        self._executors = {"default": _FakeExecutor(futures)}
        self.shutdown_calls: list[bool] = []

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_calls.append(wait)
        # mirrors AsyncIOExecutor.shutdown: pending futures are cleared
        for executor in self._executors.values():
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
    assert not task.done()  # timed out on the hard path, did not hang forever
    task.cancel()


async def test_graceful_shutdown_with_no_inflight_jobs_is_immediate() -> None:
    scheduler = _FakeScheduler(set())
    await _shutdown_scheduler_gracefully(scheduler, grace_seconds=5.0)
    assert scheduler.shutdown_calls == [False]


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
