"""Settlement feed TTL cache (ops audit WP7 fix 1).

The 30s settle cycle used to re-fetch every football-data CSV + ESPN feed on
EVERY cycle. `_load_feed_scores` now serves the feed scores from an in-process
TTL cache: two cycles inside the TTL hit upstream exactly once, an expired TTL
refetches, and an EMPTY fetch is never cached — a cached empty must not
masquerade as a fresh feed probe (silent-empty refusal semantics stay intact).

No network: httpx.MockTransport only. No DB: the helper is pure feed IO.
"""

from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings
from app.settlement.engine import _load_feed_scores, clear_feed_cache

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

_CSV = (
    "Country,League,Date,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA\n"
    f"Brazil,Serie A,{(NOW - timedelta(hours=6)).strftime('%d/%m/%Y')},"
    "Cache Alpha,Cache Beta,2,1,H,1.9,3.4,4.2\n"
)


def _settings(ttl: int = 1800) -> Settings:
    # model_construct: no .env dependency; unset fields keep their defaults.
    return Settings.model_construct(
        settle_feed_ttl_seconds=ttl,
        espn_settle_enabled=False,
    )


def _counting_transport(text: str | None = _CSV) -> tuple[httpx.MockTransport, dict[str, int]]:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if text is not None and request.url.path.endswith("/new/BRA.csv"):
            return httpx.Response(200, text=text)
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


async def test_second_cycle_inside_ttl_fetches_upstream_exactly_once() -> None:
    clear_feed_cache()
    transport, calls = _counting_transport()
    settings = _settings(ttl=1800)
    async with httpx.AsyncClient(transport=transport) as client:
        first = await _load_feed_scores(client, ["brazil-serie-a"], [], NOW, settings)
        assert first  # the feed really returned scores
        assert calls["n"] == 1
        second = await _load_feed_scores(
            client, ["brazil-serie-a"], [], NOW + timedelta(seconds=60), settings
        )
    assert second == first
    assert calls["n"] == 1  # served from cache — no second upstream fetch


async def test_expired_ttl_refetches_upstream() -> None:
    clear_feed_cache()
    transport, calls = _counting_transport()
    settings = _settings(ttl=1800)
    async with httpx.AsyncClient(transport=transport) as client:
        await _load_feed_scores(client, ["brazil-serie-a"], [], NOW, settings)
        assert calls["n"] == 1
        await _load_feed_scores(
            client, ["brazil-serie-a"], [], NOW + timedelta(seconds=1801), settings
        )
    assert calls["n"] == 2  # TTL expired -> a real refetch


async def test_empty_feed_result_is_never_cached() -> None:
    # A feed outage (all sources empty) must be RE-PROBED every cycle so the
    # silent-empty refusal keeps reflecting live reality — never a cached [].
    clear_feed_cache()
    transport, calls = _counting_transport(text=None)  # every source 404s
    settings = _settings(ttl=1800)
    async with httpx.AsyncClient(transport=transport) as client:
        first = await _load_feed_scores(client, ["brazil-serie-a"], [], NOW, settings)
        assert first == []
        second = await _load_feed_scores(
            client, ["brazil-serie-a"], [], NOW + timedelta(seconds=60), settings
        )
    assert second == []
    assert calls["n"] == 2  # empty was not cached — the outage was re-probed


async def test_different_feed_config_uses_a_distinct_cache_entry() -> None:
    clear_feed_cache()
    transport, calls = _counting_transport()
    settings = _settings(ttl=1800)
    async with httpx.AsyncClient(transport=transport) as client:
        await _load_feed_scores(client, ["brazil-serie-a"], [], NOW, settings)
        await _load_feed_scores(
            client, ["brazil-serie-a", "argentina-liga-profesional"], [], NOW, settings
        )
    # the second call has a different league set -> its own fetch, not a stale hit
    assert calls["n"] == 3  # 1 (BRA) + 2 (BRA + ARG)
