"""Tests for the OddsPapi NBA historical-odds loader (app/ingestion/oddspapi).

Synthetic fixtures only — no network, no real key. Validates the mapping of the
DOCUMENTED OddsPapi schema (https://oddspapi.io/us/docs/get-historical-odds,
fetched 2026-06-29):

* ``GET /v4/historical-odds?fixtureId=..&bookmakers=pinnacle,..`` returns
  ``{"fixtureId", "bookmakers": {slug: {"markets": {marketId: {"outcomes":
  {outcomeId: {"players": {playerId: [ {"createdAt","price","limit","active",
  "exchangeMeta"} ]}}}}}}}}`` — the price-history array is chronological, so the
  FIRST entry is the opening price and the LAST is the closing price.
* Authentication is the ``apiKey`` query parameter.

Honest scope: Pinnacle is the sharp anchor (pre-match open + close); the free
tier is shallow, so soft-book and some price points may be missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.ingestion.oddspapi import (
    HISTORICAL_ODDS_PATH,
    OddsPapiClient,
    OddsPapiGame,
    load_oddspapi_dir,
    outcome_open_close,
    parse_fixture_bundle,
    price_history_open_close,
)


def _entry(ts: str, price: float, active: bool = True) -> dict:
    return {"createdAt": ts, "price": price, "limit": 5000, "active": active, "exchangeMeta": None}


# --- price-history open/close ----------------------------------------------
def test_first_entry_is_open_last_is_close() -> None:
    entries = [
        _entry("2022-10-18T18:00:00Z", 1.80),
        _entry("2022-10-18T21:00:00Z", 1.74),
        _entry("2022-10-18T23:25:00Z", 1.71),  # last pre-tip => close
    ]
    opening, closing = price_history_open_close(entries)
    assert opening == Decimal("1.80")
    assert closing == Decimal("1.71")


def test_inactive_and_invalid_entries_ignored() -> None:
    entries = [
        _entry("2022-10-18T18:00:00Z", 1.0),  # <=1.0 -> not a price
        _entry("2022-10-18T19:00:00Z", 1.90, active=False),  # suspended
        _entry("2022-10-18T20:00:00Z", 1.85),
        _entry("2022-10-18T22:00:00Z", 1.83),
    ]
    opening, closing = price_history_open_close(entries)
    assert opening == Decimal("1.85") and closing == Decimal("1.83")


def test_empty_or_all_invalid_history_is_none() -> None:
    assert price_history_open_close([]) == (None, None)
    assert price_history_open_close([_entry("t", 1.0, active=False)]) == (None, None)


def test_entries_sorted_by_createdat_not_input_order() -> None:
    entries = [
        _entry("2022-10-18T23:25:00Z", 1.71),  # latest, listed first
        _entry("2022-10-18T18:00:00Z", 1.80),  # earliest
        _entry("2022-10-18T21:00:00Z", 1.74),
    ]
    opening, closing = price_history_open_close(entries)
    assert opening == Decimal("1.80") and closing == Decimal("1.71")


# --- nested outcome navigation ---------------------------------------------
def _bookmaker_node() -> dict:
    return {
        "markets": {
            "ml": {
                "outcomes": {
                    "home": {"players": {"0": [_entry("t1", 1.60), _entry("t3", 1.55)]}},
                    "away": {"players": {"0": [_entry("t1", 2.50), _entry("t3", 2.62)]}},
                }
            }
        }
    }


def test_outcome_open_close_navigates_markets_outcomes_players() -> None:
    node = _bookmaker_node()
    home_open, home_close = outcome_open_close(node, "ml", "home")
    away_open, away_close = outcome_open_close(node, "ml", "away")
    assert home_open == Decimal("1.60") and home_close == Decimal("1.55")
    assert away_open == Decimal("2.50") and away_close == Decimal("2.62")


def test_outcome_open_close_missing_market_returns_none() -> None:
    assert outcome_open_close(_bookmaker_node(), "spread", "home") == (None, None)
    assert outcome_open_close(_bookmaker_node(), "ml", "nope") == (None, None)


# --- full fixture bundle ----------------------------------------------------
def _bundle() -> dict:
    return {
        "fixtureId": "id1000000758265379",
        "home_team": "Boston Celtics",
        "away_team": "Philadelphia 76ers",
        "startTime": "2022-10-18T23:30:00Z",
        "home_score": 126,
        "away_score": 117,
        "moneyline": {"marketId": "ml", "home_outcomeId": "home", "away_outcomeId": "away"},
        "historical_odds": {
            "fixtureId": "id1000000758265379",
            "bookmakers": {
                "pinnacle": _bookmaker_node(),
                "bet365": {
                    "markets": {
                        "ml": {
                            "outcomes": {
                                "home": {
                                    "players": {"0": [_entry("t1", 1.65), _entry("t3", 1.57)]}
                                },
                                "away": {
                                    "players": {"0": [_entry("t1", 2.55), _entry("t3", 2.60)]}
                                },
                            }
                        }
                    }
                },
            },
        },
    }


def test_bundle_maps_pinnacle_anchor_best_soft_and_result() -> None:
    game = parse_fixture_bundle(_bundle(), sharp="pinnacle", soft=("bet365",))
    assert game is not None
    assert game.home_team == "Boston Celtics" and game.away_team == "Philadelphia 76ers"
    assert game.commence_utc == datetime(2022, 10, 18, 23, 30, tzinfo=UTC)
    assert game.result == "H"  # 126 > 117
    # Pinnacle (sharp) pre-match open + close
    assert game.home_pinnacle_open == Decimal("1.60")
    assert game.home_pinnacle_close == Decimal("1.55")
    # best soft pre-match = max across soft books (bet365 home 1.65 > pinnacle)
    assert game.home_best_soft_open == Decimal("1.65")
    assert game.away_best_soft_open == Decimal("2.55")
    assert isinstance(game.home_pinnacle_open, Decimal)  # NUMERIC discipline


def test_bundle_commence_is_utc_aware() -> None:
    game = parse_fixture_bundle(_bundle(), sharp="pinnacle", soft=("bet365",))
    assert game is not None and game.commence_utc is not None
    assert game.commence_utc.tzinfo is not None  # never naive


def test_bundle_without_pinnacle_anchor_is_skipped() -> None:
    bundle = _bundle()
    del bundle["historical_odds"]["bookmakers"]["pinnacle"]
    assert parse_fixture_bundle(bundle, sharp="pinnacle", soft=("bet365",)) is None


def test_bundle_explicit_result_field_overrides_scores() -> None:
    bundle = _bundle()
    del bundle["home_score"]
    del bundle["away_score"]
    bundle["result"] = "A"
    game = parse_fixture_bundle(bundle, sharp="pinnacle", soft=("bet365",))
    assert game is not None and game.result == "A"


# --- directory loader -------------------------------------------------------
def test_absent_dir_returns_empty_clean_skip(tmp_path: Path) -> None:
    assert load_oddspapi_dir(tmp_path / "nope") == []


def test_dir_loads_bundles_sorted_by_commence(tmp_path: Path) -> None:
    import json

    later = _bundle()
    earlier = _bundle()
    earlier["fixtureId"] = "early"
    earlier["startTime"] = "2022-10-17T23:30:00Z"
    (tmp_path / "g_later.json").write_text(json.dumps(later), encoding="utf-8")
    (tmp_path / "g_early.json").write_text(json.dumps(earlier), encoding="utf-8")
    games = load_oddspapi_dir(tmp_path, sharp="pinnacle", soft=("bet365",))
    assert len(games) == 2
    first, second = games[0].commence_utc, games[1].commence_utc
    assert first is not None and second is not None
    assert first < second


def test_unreadable_bundle_skipped_not_fatal(tmp_path: Path) -> None:
    import json

    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "ok.json").write_text(json.dumps(_bundle()), encoding="utf-8")
    games = load_oddspapi_dir(tmp_path, sharp="pinnacle", soft=("bet365",))
    assert len(games) == 1


# --- read-only GET client ---------------------------------------------------
def test_client_is_get_only_and_uses_apikey_param() -> None:
    import asyncio

    import httpx

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["apikey"] = request.url.params.get("apiKey")
        seen["fixtureId"] = request.url.params.get("fixtureId")
        return httpx.Response(200, json={"fixtureId": "x", "bookmakers": {}})

    transport = httpx.MockTransport(handler)

    async def run() -> dict:
        async with httpx.AsyncClient(transport=transport, base_url="https://api.oddspapi.io") as c:
            client = OddsPapiClient(api_key="SECRET", client=c)
            return await client.historical_odds("fix123", bookmakers=("pinnacle",))

    payload = asyncio.run(run())
    assert payload["fixtureId"] == "x"
    assert seen["method"] == "GET"  # read-only
    assert seen["path"] == HISTORICAL_ODDS_PATH
    assert seen["apikey"] == "SECRET"  # auth via apiKey query param (documented)
    assert seen["fixtureId"] == "fix123"


def test_client_never_logs_the_key(caplog) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"fixtureId": "x", "bookmakers": {}})

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.oddspapi.io"
        ) as c:
            client = OddsPapiClient(api_key="TOPSECRETKEY", client=c)
            with caplog.at_level("DEBUG"):
                await client.historical_odds("fix123", bookmakers=("pinnacle",))

    asyncio.run(run())
    assert "TOPSECRETKEY" not in caplog.text  # key must never reach the logs


def test_game_carries_pinnacle_anchor_fields() -> None:
    fields = set(OddsPapiGame.__annotations__)
    assert {"home_pinnacle_open", "home_pinnacle_close", "home_best_soft_open"} <= fields


# --- STAGED cross-check client (OFF by default; no account, no live calls) ---
#
# Fixtures below are SYNTHETIC-PER-DOCS: built from the endpoint map in
# docs/research/2026-07-05-oddspapi-crosscheck-evaluation.md §F2 (documented
# names /v4/tournaments and /v4/fixtures, fixtureId + startTime fields) — the
# note carries no concrete tournaments/fixtures payload example, so shapes are
# plausible camelCase and scripts/research/verify_oddspapi.py confirms them at
# signup.

import asyncio  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import app.ingestion.oddspapi as oddspapi_module  # noqa: E402
from app.ingestion.oddspapi import (  # noqa: E402
    MAX_CROSSCHECK_REQUESTS_PER_RUN,
    OddsPapiBudgetExceededError,
    OddsPapiCrosscheckClient,
    OddsPapiDisabledError,
    OddsPapiPolicy,
    build_oddspapi_crosscheckclient,
    parse_fixtures,
    parse_tournaments,
    price_history_open_close_before,
)


def _counting_transport(counter: dict[str, int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] = counter.get("n", 0) + 1
        counter["method"] = request.method  # type: ignore[assignment]
        return httpx.Response(200, json=[])

    return httpx.MockTransport(handler)


def _enabled_policy(cap: int = MAX_CROSSCHECK_REQUESTS_PER_RUN) -> OddsPapiPolicy:
    return OddsPapiPolicy(enabled=True, api_key="k", max_requests_per_run=cap)


def _mock_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="https://api.oddspapi.io")


# --- inertness ---------------------------------------------------------------
def test_policy_defaults_are_inert() -> None:
    policy = OddsPapiPolicy()
    assert policy.enabled is False
    assert policy.api_key == ""
    assert policy.max_requests_per_run == MAX_CROSSCHECK_REQUESTS_PER_RUN


def test_factory_returns_none_when_disabled_or_keyless() -> None:
    assert build_oddspapi_crosscheckclient(OddsPapiPolicy()) is None
    assert build_oddspapi_crosscheckclient(OddsPapiPolicy(enabled=True)) is None  # no key
    assert build_oddspapi_crosscheckclient(OddsPapiPolicy(api_key="k")) is None  # flag off
    built = build_oddspapi_crosscheckclient(OddsPapiPolicy(enabled=True, api_key="k"))
    assert isinstance(built, OddsPapiCrosscheckClient)


def test_disabled_client_never_touches_transport() -> None:
    counter: dict[str, int] = {}

    async def run() -> None:
        async with _mock_client(_counting_transport(counter)) as c:
            client = OddsPapiCrosscheckClient(OddsPapiPolicy(api_key="k"), client=c)
            with pytest.raises(OddsPapiDisabledError):
                await client.tournaments()
            with pytest.raises(OddsPapiDisabledError):
                await client.historical_odds("f1")

    asyncio.run(run())
    assert counter.get("n", 0) == 0  # NO request was made


def test_keyless_enabled_client_is_also_inert() -> None:
    counter: dict[str, int] = {}

    async def run() -> None:
        async with _mock_client(_counting_transport(counter)) as c:
            client = OddsPapiCrosscheckClient(OddsPapiPolicy(enabled=True), client=c)
            with pytest.raises(OddsPapiDisabledError):
                await client.fixtures("t1")

    asyncio.run(run())
    assert counter.get("n", 0) == 0


# --- request budget ----------------------------------------------------------
def test_policy_cannot_raise_the_hard_cap() -> None:
    with pytest.raises(ValueError):
        OddsPapiPolicy(max_requests_per_run=MAX_CROSSCHECK_REQUESTS_PER_RUN + 1)
    with pytest.raises(ValueError):
        OddsPapiPolicy(max_requests_per_run=0)
    assert MAX_CROSSCHECK_REQUESTS_PER_RUN <= 50  # a bug can never burn the month


def test_request_cap_enforced_before_transport() -> None:
    counter: dict[str, int] = {}

    async def run() -> OddsPapiCrosscheckClient:
        async with _mock_client(_counting_transport(counter)) as c:
            client = OddsPapiCrosscheckClient(_enabled_policy(cap=2), client=c)
            await client.tournaments()
            await client.fixtures("t1")
            with pytest.raises(OddsPapiBudgetExceededError):
                await client.historical_odds("f1")
            return client

    client = asyncio.run(run())
    assert counter["n"] == 2  # the third call never reached the wire
    assert client.requests_used == 2


# --- GET-only ----------------------------------------------------------------
def test_crosscheck_client_only_issues_get() -> None:
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        return httpx.Response(200, json={"fixtureId": "f1", "bookmakers": {}})

    async def run() -> None:
        async with _mock_client(httpx.MockTransport(handler)) as c:
            client = OddsPapiCrosscheckClient(_enabled_policy(), client=c)
            await client.tournaments(sport_id=1)
            await client.fixtures("t1")
            await client.historical_odds("f1", bookmakers=("pinnacle",))

    asyncio.run(run())
    assert seen_methods == ["GET", "GET", "GET"]


def test_module_source_has_no_non_get_http_verb() -> None:
    """No non-GET HTTP method exists anywhere in the module (read-only audit)."""
    import inspect

    src = inspect.getsource(oddspapi_module)
    for verb in (".post(", ".put(", ".patch(", ".delete(", ".request(", ".send("):
        assert verb not in src, f"non-GET HTTP call {verb!r} found in oddspapi module"


def test_crosscheck_client_never_logs_the_key(caplog) -> None:  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("apiKey") == "TOPSECRETKEY"  # rides the query only
        return httpx.Response(200, json=[])

    async def run() -> None:
        async with _mock_client(httpx.MockTransport(handler)) as c:
            client = OddsPapiCrosscheckClient(
                OddsPapiPolicy(enabled=True, api_key="TOPSECRETKEY"), client=c
            )
            with caplog.at_level("DEBUG"):
                await client.tournaments()

    asyncio.run(run())
    assert "TOPSECRETKEY" not in caplog.text


# --- pre-kickoff close cutoff -------------------------------------------------
def test_pre_ko_cutoff_excludes_in_play_points() -> None:
    kickoff = datetime(2026, 6, 20, 19, 0, tzinfo=UTC)
    entries = [
        _entry("2026-06-20T10:00:00Z", 2.10),
        _entry("2026-06-20T18:40:00Z", 2.04),  # last PRE-KO point => the close
        _entry("2026-06-20T19:25:00Z", 3.50),  # in-play — must never be the close
    ]
    opening, closing = price_history_open_close_before(entries, kickoff)
    assert opening == Decimal("2.10")
    assert closing == Decimal("2.04")
    # sanity: the raw reducer WOULD have taken the in-play point
    assert price_history_open_close(entries)[1] == Decimal("3.50")


def test_pre_ko_cutoff_drops_undatable_entries_fail_closed() -> None:
    kickoff = datetime(2026, 6, 20, 19, 0, tzinfo=UTC)
    entries = [
        {"createdAt": None, "price": 2.5, "active": True},  # undatable -> excluded
        _entry("not-a-timestamp", 2.4),  # unparseable -> excluded
        _entry("2026-06-20T18:00:00Z", 2.02),
    ]
    assert price_history_open_close_before(entries, kickoff) == (
        Decimal("2.02"),
        Decimal("2.02"),
    )


def test_pre_ko_cutoff_rejects_naive_kickoff() -> None:
    with pytest.raises(ValueError):
        price_history_open_close_before([], datetime(2026, 6, 20, 19, 0))  # noqa: DTZ001


# --- fixture-resolution parsing (synthetic-per-docs payloads) ------------------
def test_parse_tournaments_tolerates_list_and_wrapper_and_extras() -> None:
    rows = [
        {"tournamentId": 101, "name": "Premier League", "sportId": 10, "zzz": "ignored"},
        {"id": "t-2", "name": "LaLiga"},
        {"name": "no-id -> skipped"},
        "not-a-mapping",
    ]
    for payload in (rows, {"tournaments": rows}, {"data": rows}):
        parsed = parse_tournaments(payload)
        assert [t.tournament_id for t in parsed] == ["101", "t-2"]
        assert parsed[0].name == "Premier League"
        assert parsed[0].sport_id == 10
    assert parse_tournaments({"unexpected": 1}) == []
    assert parse_tournaments(None) == []


def test_parse_fixtures_start_time_is_aware_utc_never_naive() -> None:
    rows = [
        {
            "fixtureId": "fx1",
            "startTime": "2026-06-20T19:00:00Z",
            "homeTeam": "Arsenal",
            "awayTeam": "Chelsea",
        },
        {"fixtureId": "fx2", "startTime": "2026-06-21T17:30:00"},  # naive ISO -> assumed UTC
        {"fixtureId": 12345},  # id coerced to str; no time
        {"startTime": "2026-06-22T12:00:00Z"},  # no id -> skipped
    ]
    parsed = parse_fixtures(rows)
    assert [f.fixture_id for f in parsed] == ["fx1", "fx2", "12345"]
    fx1 = parsed[0]
    assert fx1.start_time == datetime(2026, 6, 20, 19, 0, tzinfo=UTC)
    assert fx1.start_time.tzinfo is not None
    assert (fx1.home_team, fx1.away_team) == ("Arsenal", "Chelsea")
    # naive ISO string -> assumed UTC (the module's _parse_time convention);
    # the result is ALWAYS tz-aware, never naive.
    fx2 = parsed[1]
    assert fx2.start_time == datetime(2026, 6, 21, 17, 30, tzinfo=UTC)
    assert fx2.start_time.tzinfo is not None
    assert parsed[2].start_time is None  # absent startTime stays None
