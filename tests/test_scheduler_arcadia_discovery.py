"""Arcadia config-discovery transport routing (audit 2026-07-10 L-scheduler-1013).

The opt-in ``ARCADIA_DISCOVER_CONFIG`` fetch used the DIRECT ``http_client``
even when the capture rode the proxy pool — a config bypass that 403s on
datacenter egress and leaks the egress IP. Discovery must go through the SAME
client (and therefore the same proxy pool) the capture uses, and stay
fail-closed (any discovery failure keeps the configured key/base and never
aborts the capture cycle). No network: both clients are MockTransport-backed.
"""

from collections.abc import Callable

import fakeredis.aioredis as fakeredis
import httpx
import pytest

from app.config import Settings
from app.ingestion import pinnacle_arcadia
from app.scheduler import build_scheduler


def make_settings(**overrides: object) -> Settings:
    overrides.setdefault("odds_source", "oddsportal")
    overrides.setdefault("arcadia_enabled", True)
    overrides.setdefault("arcadia_discover_config", True)
    overrides.setdefault("arcadia_proxy_urls", "http://user:secret@proxy.test:8080")
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _recording_client(
    hits: list[str], respond: Callable[[httpx.Request], httpx.Response]
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return respond(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _run_capture_job_once(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proxy_response: Callable[[httpx.Request], httpx.Response],
) -> tuple[list[str], list[str]]:
    """Build the scheduler with a proxy pool configured, run the arcadia
    capture job once, and return (proxy_hits, direct_hits)."""
    proxy_hits: list[str] = []
    direct_hits: list[str] = []

    def fake_build_proxy_client(proxy_urls: object) -> httpx.AsyncClient:
        return _recording_client(proxy_hits, proxy_response)

    monkeypatch.setattr(
        pinnacle_arcadia, "build_arcadia_proxy_http_client", fake_build_proxy_client
    )

    captured = {"ran": False}

    async def noop_capture_once(self: object) -> None:
        captured["ran"] = True

    monkeypatch.setattr(pinnacle_arcadia.PinnacleArcadiaCapture, "capture_once", noop_capture_once)

    direct_client = _recording_client(direct_hits, lambda _r: httpx.Response(200, json={}))
    scheduler = build_scheduler(
        make_settings(),
        direct_client,
        fakeredis.FakeRedis(),
        session_factory=object(),  # type: ignore[arg-type]  # only stored, never called here
    )
    job = next(j for j in scheduler.get_jobs() if j.id == "capture_pinnacle_arcadia")
    await job.func()
    assert captured["ran"] is True  # discovery failure must never abort the capture
    owned_clients = list(scheduler._owned_http_clients)
    assert len(owned_clients) == 1  # internally-created proxy transport is lifecycle-owned
    for owned in owned_clients:
        await owned.aclose()
    await direct_client.aclose()
    return proxy_hits, direct_hits


async def test_discovery_goes_through_the_capture_proxy_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "api": {"haywire": {"apiKey": "public-guest-key"}},
                "routes": {"curacao": {"guestRoot": "https://guest.example/"}},
            },
        )

    proxy_hits, direct_hits = await _run_capture_job_once(monkeypatch, proxy_response=ok)
    assert any(url == pinnacle_arcadia.CONFIG_APP_JSON_URL for url in proxy_hits)
    assert direct_hits == []  # the direct client must NEVER carry discovery


async def test_discovery_failure_is_fail_closed_and_still_proxied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 403 through the pool (the datacenter-egress signature) returns None
    # from discover_arcadia_config: configured key/base stand, the capture
    # still runs, and the direct client is still never consulted.
    proxy_hits, direct_hits = await _run_capture_job_once(
        monkeypatch, proxy_response=lambda _r: httpx.Response(403)
    )
    assert any(url == pinnacle_arcadia.CONFIG_APP_JSON_URL for url in proxy_hits)
    assert direct_hits == []
