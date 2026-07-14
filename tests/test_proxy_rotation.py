"""Rotation sites × the shared proxy-health registry (audit 2026-07-03 §5):
a quarantined index is SKIPPED at every rotation site while healthy indices
keep rotating; the Betfair per-target sweep is CAPPED; the curl_cffi sessions
carry the explicit (connect, read) timeout. Synthetic proxies only — no
network, no DB, no env."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.ingestion.base import EventDirectory, ScraperProxy
from app.ingestion.betfair_exchange import BetfairExchangeError, BetfairExchangeReader
from app.ingestion.oddsportal import _MAX_PROXY_FAILOVER, OddsPortalLoader
from app.ingestion.proxy_health import ProxyHealthRegistry

MATCH = {
    "home_team": "Alpha FC",
    "away_team": "Beta United",
    "match_date": "2026-07-11",
    "league_name": "Testland League",
    "match_link": "https://www.oddsportal.com/football/testland/alpha-beta/",
}


def make_pool(n: int) -> tuple[ScraperProxy, ...]:
    return tuple(
        ScraperProxy(url=f"http://h{i}:1", username=f"u{i}", password=f"p{i}") for i in range(n)
    )


def quarantine(registry: ProxyHealthRegistry, index: int) -> None:
    for _ in range(registry.threshold):
        registry.record_failure(index, "TimeoutError")


def make_loader(
    pool: tuple[ScraperProxy, ...],
    registry: ProxyHealthRegistry,
    scrape_fn: Any = None,
    **kwargs: Any,
) -> OddsPortalLoader:
    return OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=scrape_fn,
        proxy_pool=pool,
        proxy_health=registry,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Site 1: oddsportal per-match round-robin (_next_proxy)
# --------------------------------------------------------------------------- #
def test_next_proxy_skips_quarantined_and_covers_healthy() -> None:
    registry = ProxyHealthRegistry()
    quarantine(registry, 1)
    loader = make_loader(make_pool(3), registry)
    picked = [loader._next_proxy() for _ in range(6)]
    urls = [p.url for p in picked if p is not None]
    assert "http://h1:1" not in urls  # the quarantined slot never rotates in
    assert set(urls) == {"http://h0:1", "http://h2:1"}  # both healthy slots used


def test_next_proxy_fails_open_when_all_quarantined() -> None:
    registry = ProxyHealthRegistry()
    for index in range(3):
        quarantine(registry, index)
    loader = make_loader(make_pool(3), registry)
    proxy = loader._next_proxy()
    assert proxy is not None  # fail-open: never worse than blind round-robin


# --------------------------------------------------------------------------- #
# Site 2: oddsportal listing/score failover sweep (_scrape_with_failover)
# --------------------------------------------------------------------------- #
async def test_failover_sweep_skips_quarantined_index() -> None:
    registry = ProxyHealthRegistry()
    quarantine(registry, 0)
    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(success=[MATCH], failed=[], partial=[])

    loader = make_loader(make_pool(3), registry, scrape_fn=fake_scrape)
    await loader.fetch_odds("soccer")
    # ONE scrape (success is final), via the first HEALTHY proxy — not #0.
    (call,) = calls
    assert call["proxy_url"] == "http://h1:1"  # #0 quarantined -> #1 first


async def test_failover_records_failures_and_next_sweep_moves_on() -> None:
    # First sweep burns _MAX_PROXY_FAILOVER dead proxies (recorded, threshold=1
    # -> quarantined); the SECOND sweep must try the NEXT healthy indices, not
    # re-enter the dead slots ("the dead slot re-enters next lap" — audit §5).
    registry = ProxyHealthRegistry(threshold=1)
    calls: list[dict[str, Any]] = []

    async def dead_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise TimeoutError("proxy dead")

    loader = make_loader(make_pool(8), registry, scrape_fn=dead_scrape)
    await loader.fetch_odds("soccer")
    assert [c["proxy_url"] for c in calls] == ["http://h0:1", "http://h1:1", "http://h2:1"]
    assert len(calls) == _MAX_PROXY_FAILOVER  # existing cap still holds
    calls.clear()
    await loader.fetch_odds("soccer")
    # quarantined 0/1/2 skipped; rotation continues over the healthy tail
    assert [c["proxy_url"] for c in calls] == ["http://h3:1", "http://h4:1", "http://h5:1"]


async def test_failover_reraises_import_error_not_masked_as_proxy_failure() -> None:
    # A missing scrape dependency (ModuleNotFoundError/ImportError, e.g.
    # pycryptodome for the JSON-feed AES decrypt) is INFRASTRUCTURAL — it must
    # RAISE, not be retried across the whole pool and masked as a silent-empty
    # "0 matches" scrape that reads like "no games".
    registry = ProxyHealthRegistry()
    calls: list[dict[str, Any]] = []

    async def missing_dep_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise ModuleNotFoundError("No module named 'Crypto'")

    loader = make_loader(make_pool(5), registry, scrape_fn=missing_dep_scrape)
    with pytest.raises(ImportError):
        await loader.fetch_odds("soccer")
    # aborted on the FIRST attempt — never burned the pool
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Site 3: oddsportal JSON per-match fan-out (attribution by pool index)
# --------------------------------------------------------------------------- #
async def test_json_scrape_raw_attributes_outcome_to_pool_index() -> None:
    registry = ProxyHealthRegistry()
    now = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)

    async def failing_json(match_url: str, **kwargs: Any) -> list[Any]:
        raise ConnectionError("feed down")

    loader = make_loader(make_pool(2), registry, json_scrape_fn=failing_json, use_json_feed=True)
    with pytest.raises(ConnectionError):
        await loader._json_scrape_raw(
            "https://www.oddsportal.com/football/testland/alpha-beta/", now, ("1x2",), None
        )
    assert registry._slots[0].failures == 1
    assert registry._slots[0].last_error_class == "ConnectionError"

    async def ok_json(match_url: str, **kwargs: Any) -> list[Any]:
        return []

    loader_ok = make_loader(make_pool(2), registry, json_scrape_fn=ok_json, use_json_feed=True)
    await loader_ok._json_scrape_raw(
        "https://www.oddsportal.com/football/testland/alpha-beta/", now, ("1x2",), None
    )
    # transport-clean (even if empty) -> success on the slot it used
    assert registry._slots[0].successes == 1
    assert registry._slots[0].consecutive_failures == 0


# --------------------------------------------------------------------------- #
# Site 4: Betfair feed sweep — quarantine skip, CAP, explicit timeout
# --------------------------------------------------------------------------- #
class _FakeAsyncSession:
    """Stands in for curl_cffi AsyncSession; records constructor kwargs."""

    constructed: list[dict[str, Any]] = []
    fail = True

    def __init__(self, **kwargs: Any) -> None:
        type(self).constructed.append(kwargs)
        self.request_count = 0

    async def __aenter__(self) -> "_FakeAsyncSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        if type(self).fail:
            raise ConnectionError("dead proxy")
        self.request_count += 1
        if self.request_count == 1:
            # A schema-valid bootstrap followed by absent optional feeds is a
            # benign [] gap. Missing bootstrap identity is intentionally a
            # failover-worthy schema error, not a transport-clean success.
            html = (
                '<div id="react-event-header" '
                "data='{"  # noqa: ISC003
                '"eventData":{"id":"event-1","sportId":1,'
                '"defaultBetId":1,"defaultScopeId":1}'
                "}'></div>"
            )
            return SimpleNamespace(status_code=200, text=html)
        return SimpleNamespace(status_code=404, text="")


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncSession]:
    import curl_cffi.requests

    _FakeAsyncSession.constructed = []
    _FakeAsyncSession.fail = True
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", _FakeAsyncSession)
    return _FakeAsyncSession


async def test_betfair_sweep_capped_and_timeout_explicit(
    fake_session: type[_FakeAsyncSession],
) -> None:
    # Audit §5: "the Betfair sweep tries all 14 slots (betfair_exchange.py:325)
    # with no cap" — with 10 dead proxies the sweep must stop at the global
    # three-total-attempt self-heal ceiling, and every session must carry the
    # explicit (connect, read) timeout.
    registry = ProxyHealthRegistry()
    reader = BetfairExchangeReader(
        min_liquidity=0.0, proxy_pool=make_pool(10), proxy_health=registry
    )
    with pytest.raises(BetfairExchangeError):
        await reader._network_load("https://www.oddsportal.com/football/t/a-b/", "soccer")
    assert len(fake_session.constructed) == 3  # global self-heal ceiling
    assert all(kw["timeout"] == (8.0, 25.0) for kw in fake_session.constructed)
    # every failed attempt was attributed to its pool index
    assert all(registry._slots[i].failures == 1 for i in range(3))
    assert all(registry._slots.get(i) is None for i in range(3, 10))


async def test_betfair_sweep_respects_custom_cap(
    fake_session: type[_FakeAsyncSession],
) -> None:
    reader = BetfairExchangeReader(
        min_liquidity=0.0,
        proxy_pool=make_pool(10),
        proxy_health=ProxyHealthRegistry(),
        max_failover=2,
    )
    with pytest.raises(BetfairExchangeError):
        await reader._network_load("https://www.oddsportal.com/football/t/a-b/", "soccer")
    assert len(fake_session.constructed) == 2


async def test_betfair_sweep_skips_quarantined_slot(
    fake_session: type[_FakeAsyncSession],
) -> None:
    registry = ProxyHealthRegistry()
    quarantine(registry, 0)
    fake_session.fail = False
    reader = BetfairExchangeReader(
        min_liquidity=0.0, proxy_pool=make_pool(3), proxy_health=registry
    )
    feeds = await reader._network_load("https://www.oddsportal.com/football/t/a-b/", "soccer")
    assert feeds == []  # valid bootstrap; optional feeds absent on first healthy slot
    (kwargs,) = fake_session.constructed
    assert "h1" in kwargs["proxies"]["https"]  # slot 0 quarantined -> slot 1 used
    assert registry._slots[1].successes == 1
