"""Background /resolution/match-rate cache warmer (TASK MR, 2026-07-26).

The match-rate payload costs 9-11s cold (three >7s repo queries). A scheduler
job recomputes it every MATCH_RATE_REFRESH_INTERVAL_S and stores it in the
route's per-process TTL cache so authenticated dashboard loads always serve
warm data. No network: the direct client is MockTransport-backed and the
refresh entry point itself is stubbed at the routes module.
"""

import fakeredis.aioredis as fakeredis
import httpx
import pytest

from app.config import Settings
from app.scheduler import build_scheduler


def make_settings(**overrides: object) -> Settings:
    overrides.setdefault("odds_source", "oddsportal")
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _mock_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})))


async def test_match_rate_refresh_job_registered_and_calls_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a session factory the warmer is registered (interval + startup
    primer) and the job drives routes.refresh_match_rate_cache with that
    factory — the same cache the route reads."""
    from app.api import routes

    refreshed: list[object] = []

    async def fake_refresh(session_factory: object) -> None:
        refreshed.append(session_factory)

    monkeypatch.setattr(routes, "refresh_match_rate_cache", fake_refresh)
    client = _mock_client()
    sentinel_factory = object()
    scheduler = build_scheduler(
        make_settings(),
        client,
        fakeredis.FakeRedis(),
        session_factory=sentinel_factory,  # type: ignore[arg-type]  # only stored/passed through
    )
    try:
        job = next(j for j in scheduler.get_jobs() if j.id == "match_rate_cache_refresh")
        # Startup primer: the first dashboard hit after boot is already warm.
        assert any(j.id == "match_rate_cache_refresh_initial" for j in scheduler.get_jobs())
        await job.func()
        assert refreshed == [sentinel_factory]
    finally:
        for owned in scheduler._owned_http_clients:
            await owned.aclose()
        await client.aclose()


async def test_match_rate_refresh_job_guards_failures_without_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing refresh never raises out of the job and never logs the
    stringified exception (DB/driver text can embed connection URLs) — only
    the exception type name."""
    from app.api import routes

    secret_url = "postgresql://operator:credential@db.invalid/private"

    async def fake_refresh(session_factory: object) -> None:
        raise RuntimeError(secret_url)

    monkeypatch.setattr(routes, "refresh_match_rate_cache", fake_refresh)
    client = _mock_client()
    scheduler = build_scheduler(
        make_settings(),
        client,
        fakeredis.FakeRedis(),
        session_factory=object(),  # type: ignore[arg-type]
    )
    try:
        job = next(j for j in scheduler.get_jobs() if j.id == "match_rate_cache_refresh")
        with caplog.at_level("WARNING"):
            await job.func()  # must not raise
        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert "RuntimeError" in log_text
        assert secret_url not in log_text
    finally:
        for owned in scheduler._owned_http_clients:
            await owned.aclose()
        await client.aclose()


async def test_match_rate_refresh_job_absent_without_session_factory() -> None:
    """No DB (session_factory=None) -> the warmer is not registered at all."""
    client = _mock_client()
    scheduler = build_scheduler(
        make_settings(), client, fakeredis.FakeRedis(), session_factory=None
    )
    try:
        assert all(not j.id.startswith("match_rate_cache_refresh") for j in scheduler.get_jobs())
    finally:
        for owned in scheduler._owned_http_clients:
            await owned.aclose()
        await client.aclose()
