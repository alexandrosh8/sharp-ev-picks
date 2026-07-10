"""Ingestion: key rotation, secret scrubbing, CSV parsing. No live network."""

import json

import httpx
import pytest

from app.ingestion.football_data import parse_season_csv, season_url
from app.ingestion.odds_api import OddsApiClient, OddsApiError
from app.schemas.base import Market

VALID_PAYLOAD = [
    {
        "id": "evt-abc",
        "commence_time": "2026-06-11T19:00:00Z",
        "home_team": "Alpha FC",
        "away_team": "Beta United",
        "bookmakers": [
            {
                "key": "bookie_one",
                "last_update": "2026-06-10T12:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Alpha FC", "price": 2.1},
                            {"name": "Draw", "price": 3.4},
                            {"name": "Beta United", "price": 3.6},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": 1.95, "point": 2.5},
                            {"name": "Under", "price": 1.95, "point": 2.5},
                        ],
                    },
                    {
                        "key": "exotic_unsupported",
                        "outcomes": [{"name": "X", "price": 5.0}],
                    },
                ],
            }
        ],
    }
]


def make_client(handler: httpx.MockTransport, keys: tuple[str, ...]) -> OddsApiClient:
    return OddsApiClient(api_keys=keys, client=httpx.AsyncClient(transport=handler))


async def test_parse_canonicalizes_betfair_exchange_keys() -> None:
    # The Odds API exposes Betfair Exchange as betfair_ex_uk / betfair_ex_eu;
    # fold both to "betfair exchange" so the value engine's SHARP_BOOKS /
    # EXCHANGE_COMMISSION recognise it as the sharp exchange anchor. Pinnacle
    # ("pinnacle") already matches and must pass through unchanged.
    payload = [
        {
            "id": "evt-bf",
            "bookmakers": [
                {
                    "key": "betfair_ex_uk",
                    "last_update": "2026-06-10T12:00:00Z",
                    "markets": [{"key": "h2h", "outcomes": [{"name": "Alpha", "price": 2.5}]}],
                },
                {
                    "key": "betfair_ex_eu",
                    "last_update": "2026-06-10T12:00:00Z",
                    "markets": [{"key": "h2h", "outcomes": [{"name": "Alpha", "price": 2.6}]}],
                },
                {
                    "key": "pinnacle",
                    "last_update": "2026-06-10T12:00:00Z",
                    "markets": [{"key": "h2h", "outcomes": [{"name": "Alpha", "price": 2.4}]}],
                },
            ],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload))

    client = make_client(httpx.MockTransport(handler), ("k",))
    snaps = await client.fetch_odds("soccer_epl")
    assert {s.bookmaker for s in snaps} == {"betfair exchange", "pinnacle"}


async def test_parse_line_qualifies_totals_and_spreads_market_detail() -> None:
    # audit #1: distinct totals/spreads LINES must land in distinct devig groups.
    # Totals share the point across Over/Under; spreads are ±point on the SAME
    # line and must normalize to one group.
    payload = [
        {
            "id": "evt-lines",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "last_update": "2026-06-10T12:00:00Z",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 1.9, "point": 2.5},
                                {"name": "Under", "price": 1.9, "point": 2.5},
                                {"name": "Over", "price": 2.6, "point": 3.5},
                                {"name": "Under", "price": 1.5, "point": 3.5},
                            ],
                        },
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Alpha", "price": 1.9, "point": -1.5},
                                {"name": "Beta", "price": 1.9, "point": 1.5},
                            ],
                        },
                    ],
                }
            ],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload))

    client = make_client(httpx.MockTransport(handler), ("k",))
    snaps = await client.fetch_odds("soccer_epl")
    detail = {s.selection: s.market_detail for s in snaps}
    assert detail["Over 2.5"] == detail["Under 2.5"]  # one line -> one group
    assert detail["Over 3.5"] == detail["Under 3.5"]
    assert detail["Over 2.5"] != detail["Over 3.5"]  # distinct lines -> distinct groups
    assert detail["Alpha -1.5"] == detail["Beta +1.5"]  # ±1.5 are one spread line


async def test_regions_param_is_configurable() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["regions"] = request.url.params.get("regions")
        return httpx.Response(200, content=json.dumps([]))

    client = OddsApiClient(
        api_keys=("k",),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        regions="uk,eu",
    )
    await client.fetch_odds("soccer_epl")
    assert seen["regions"] == "uk,eu"  # Betfair Exchange lives in uk + eu


async def test_key_rotation_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.params.get("apiKey")
        if key == "test-key-dead":
            return httpx.Response(401)
        return httpx.Response(200, content=json.dumps(VALID_PAYLOAD))

    client = make_client(httpx.MockTransport(handler), ("test-key-dead", "test-key-live"))
    snapshots = await client.fetch_odds("soccer_epl")
    # 3 h2h + 2 totals outcomes; the unsupported market is skipped
    assert len(snapshots) == 5
    assert {s.market for s in snapshots} == {Market.H2H, Market.TOTALS}
    assert snapshots[0].event_id == "evt-abc"
    assert snapshots[0].bookmaker == "bookie_one"


async def test_all_keys_exhausted_raises_without_leaking_keys() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = make_client(httpx.MockTransport(handler), ("test-key-one", "test-key-two"))
    with pytest.raises(OddsApiError) as excinfo:
        await client.fetch_odds("soccer_epl")
    message = str(excinfo.value)
    assert "test-key-one" not in message
    assert "test-key-two" not in message


async def test_non_auth_error_raises_without_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = make_client(httpx.MockTransport(handler), ("test-key-one",))
    with pytest.raises(OddsApiError) as excinfo:
        await client.fetch_odds("soccer_epl")
    assert "apiKey" not in str(excinfo.value)
    assert "test-key-one" not in str(excinfo.value)


def test_requires_at_least_one_key() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    with pytest.raises(ValueError):
        OddsApiClient(api_keys=(), client=httpx.AsyncClient(transport=transport))


FIXTURE_CSV = """Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,B365H,B365D,B365A,PSCH,PSCD,PSCA
E0,16/08/2025,Alpha FC,Beta United,2,1,H,2.10,3.40,3.60,2.05,3.45,3.70
E0,17/08/2025,Gamma City,Delta Town,0,0,D,1.80,3.60,4.50,1.85,3.55,4.40
E0,18/08/2025,Epsilon Rovers,Zeta Athletic,1,3,A,3.00,3.30,2.40,,,
"""


def test_parse_season_csv_values() -> None:
    rows = parse_season_csv(FIXTURE_CSV)
    assert len(rows) == 3
    first = rows[0]
    assert first.match_date.isoformat() == "2025-08-16"
    assert first.home_team == "Alpha FC"
    assert first.home_goals == 2
    assert first.result == "H"
    assert first.pinnacle_closing_home == pytest.approx(2.05)
    # Missing closing odds parse as None, not zero
    assert rows[2].pinnacle_closing_home is None


def test_parse_skips_blank_lines() -> None:
    rows = parse_season_csv(FIXTURE_CSV + "\n,,,\n")
    assert len(rows) == 3


def test_season_url_validates_league() -> None:
    assert season_url("E0", "2425").endswith("/2425/E0.csv")
    with pytest.raises(ValueError):
        season_url("XX9", "2425")


# --------------------------------------------------------------------------- #
# Parser audit 2026-07-03 — F2 / F9 / F10 + the tennis market-key fix
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_odds_api_details_are_reverse_mappable_market_keys() -> None:
    """F2: details must be the OddsPortal key vocabulary — a bare number
    ("2.5") persists via snapshot_market_key but market_from_snapshot_key
    cannot reverse it, silently dropping the row from every close/devig
    reconstruction."""
    payload = [
        {
            "id": "evt-keys",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "last_update": "2026-06-10T12:00:00Z",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 1.9, "point": 2.5},
                                {"name": "Under", "price": 1.9, "point": 2.5},
                            ],
                        },
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Alpha", "price": 1.9, "point": -1.5},
                                {"name": "Beta", "price": 1.9, "point": 1.5},
                            ],
                        },
                    ],
                }
            ],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload))

    client = make_client(httpx.MockTransport(handler), ("k",))
    snaps = await client.fetch_odds("soccer_epl")
    detail = {s.selection: s.market_detail for s in snaps}
    assert detail["Over 2.5"] == "over_under_2_5"
    assert detail["Alpha -1.5"] == "asian_handicap_1_5"

    from app.storage.repositories import market_from_snapshot_key

    assert market_from_snapshot_key("over_under_2_5") == (Market.TOTALS, "over_under_2_5")
    reversed_spread = market_from_snapshot_key("asian_handicap_1_5")
    assert reversed_spread is not None and reversed_spread[0] is Market.SPREADS


def test_odds_api_parse_ts_coerces_naive_to_utc() -> None:
    """F10: an offset-less timestamp must never flow naive into captured_at."""
    from datetime import UTC, datetime

    from app.ingestion.odds_api import _parse_ts

    parsed = _parse_ts("2026-06-10T12:00:00")
    assert parsed == datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    assert parsed is not None and parsed.tzinfo is not None
    assert _parse_ts("2026-06-10T12:00:00Z") == datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def test_oddsportal_parse_ts_never_reads_digit_dates_as_epoch() -> None:
    """F9: an all-digit DATE string must not parse as a 1970s epoch."""
    from datetime import UTC, datetime

    from app.ingestion.oddsportal import _parse_ts

    # falls through the epoch guard and parses as the compact ISO DATE it is
    # (pre-fix: epoch-first read it as 1970-08-23)
    assert _parse_ts("20260703") == datetime(2026, 7, 3, tzinfo=UTC)
    assert _parse_ts("1751500000") == datetime.fromtimestamp(1_751_500_000, tz=UTC)
    assert _parse_ts("2026-07-03 12:00:00 UTC") == datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


def test_pick_market_keys_tennis_maps_to_match_winner() -> None:
    """Matching audit 2026-07-03: tennis snapshots only carry match_winner;
    returning "1x2" narrowed the off-window re-scrape to a nonexistent key."""
    from app.clv_trueup import _pick_market_keys

    assert _pick_market_keys("tennis", "h2h", "Player One") == ("match_winner",)
    assert _pick_market_keys("soccer", "h2h", "Alpha FC") == ("1x2",)
    assert _pick_market_keys("basketball", "h2h", "Team A") == ("home_away",)
