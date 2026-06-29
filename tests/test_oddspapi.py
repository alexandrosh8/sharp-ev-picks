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
