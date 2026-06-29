"""Clean-room Pinnacle (arcadia guest API) sharp-line capture.

Pure parser + odds-math tests need no network; the HTTP client uses
httpx.MockTransport; the version-gate is exercised without a DB; the
capture_once integration test uses the compose Postgres (skip when absent,
same pattern as tests/test_odds_snapshot_persistence.py). No live network,
ever.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.pinnacle_arcadia import (
    BOOKMAKER,
    CONFIG_APP_JSON_URL,
    SPORT_IDS,
    ArcadiaConfig,
    MarketQuote,
    PinnacleArcadiaCapture,
    PinnacleArcadiaClient,
    PinnacleArcadiaError,
    _RoundRobinTransport,
    american_to_decimal,
    discover_arcadia_config,
    extract_market_quotes,
    extract_moneyline_quotes,
    extract_spread_quotes,
    extract_total_quotes,
    parse_matchups,
)
from app.schemas.base import Market
from app.storage.models import Event, OddsSnapshot

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"
NOW = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
HORIZON_END = NOW + timedelta(hours=72)


# --------------------------------------------------------------------------- #
# American -> decimal odds math (TDD core: failing test first, then code)
# --------------------------------------------------------------------------- #
def test_american_to_decimal_positive() -> None:
    assert american_to_decimal(204) == pytest.approx(3.04)
    assert american_to_decimal(113) == pytest.approx(2.13)
    assert american_to_decimal(250) == pytest.approx(3.50)


def test_american_to_decimal_negative() -> None:
    assert american_to_decimal(-107) == pytest.approx(1.934579, abs=1e-6)
    assert american_to_decimal(-115) == pytest.approx(1.869565, abs=1e-6)
    # Heavy favorite still yields decimal > 1.0
    assert american_to_decimal(-1997) == pytest.approx(1.050075, abs=1e-6)
    assert american_to_decimal(-320) == pytest.approx(1.3125)


def test_american_to_decimal_always_above_one() -> None:
    for price in (-5000, -1997, -101, 100, 101, 5000):
        assert american_to_decimal(price) > 1.0


def test_american_to_decimal_rejects_zero() -> None:
    with pytest.raises(ValueError):
        american_to_decimal(0)


# --------------------------------------------------------------------------- #
# Matchup parsing (event + team context + horizon/type filtering)
# --------------------------------------------------------------------------- #
def _tennis_matchup(mid: int = 1631935448, start: str = "2026-06-17T07:00:00Z") -> dict:
    return {
        "id": mid,
        "type": "matchup",
        "parentId": None,
        "parent": None,
        "participants": [
            {"alignment": "home", "name": "Ivan Marrero Curbelo", "order": 0},
            {"alignment": "away", "name": "Jan Choinski", "order": 1},
        ],
        "startTime": start,
        "isLive": False,
        "status": "pending",
        "league": {"id": 237981, "name": "ITF Men Kayseri - R1", "group": "Turkey"},
        "version": 672572676,
    }


def test_parse_matchups_extracts_teams_league_kickoff() -> None:
    parsed = parse_matchups([_tennis_matchup()], now=NOW, horizon_end=HORIZON_END)
    assert set(parsed) == {"1631935448"}
    m = parsed["1631935448"]
    assert m.home == "Ivan Marrero Curbelo"
    assert m.away == "Jan Choinski"
    assert m.league == "ITF Men Kayseri - R1"
    assert m.starts_at == datetime(2026, 6, 17, 7, 0, tzinfo=UTC)


def test_parse_matchups_filters_specials_children_and_window() -> None:
    rows = [
        _tennis_matchup(mid=1, start="2026-06-17T07:00:00Z"),  # keep
        {**_tennis_matchup(mid=2), "type": "special"},  # drop: prop/outright
        {**_tennis_matchup(mid=3), "parentId": 99, "parent": {"id": 99}},  # drop: child
        _tennis_matchup(mid=4, start="2026-06-10T07:00:00Z"),  # drop: already started
        _tennis_matchup(mid=5, start="2026-09-01T07:00:00Z"),  # drop: beyond horizon
        {**_tennis_matchup(mid=6), "participants": [{"alignment": "home", "name": "A"}]},  # drop
    ]
    parsed = parse_matchups(rows, now=NOW, horizon_end=HORIZON_END)
    assert set(parsed) == {"1"}


def test_parse_matchups_prefers_cutoff_at_over_start_time() -> None:
    # The period-0 cutoffAt is the TRUE betting cutoff; prefer it over startTime
    # when present so the latest pre-kickoff row IS the close.
    row = {
        **_tennis_matchup(mid=11, start="2026-06-17T07:00:00Z"),
        "cutoffAt": "2026-06-17T06:55:00Z",
    }
    parsed = parse_matchups([row], now=NOW, horizon_end=HORIZON_END)
    assert parsed["11"].starts_at == datetime(2026, 6, 17, 6, 55, tzinfo=UTC)


def test_parse_matchups_falls_back_to_start_time_when_cutoff_absent() -> None:
    # No cutoffAt (or unparseable) -> startTime is used unchanged.
    no_cutoff = _tennis_matchup(mid=12, start="2026-06-17T07:00:00Z")
    null_cutoff = {**_tennis_matchup(mid=13, start="2026-06-17T08:00:00Z"), "cutoffAt": None}
    junk_cutoff = {**_tennis_matchup(mid=14, start="2026-06-17T09:00:00Z"), "cutoffAt": "not-a-ts"}
    parsed = parse_matchups([no_cutoff, null_cutoff, junk_cutoff], now=NOW, horizon_end=HORIZON_END)
    assert parsed["12"].starts_at == datetime(2026, 6, 17, 7, 0, tzinfo=UTC)
    assert parsed["13"].starts_at == datetime(2026, 6, 17, 8, 0, tzinfo=UTC)
    assert parsed["14"].starts_at == datetime(2026, 6, 17, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Moneyline extraction (period-0 match/game winner only)
# --------------------------------------------------------------------------- #
def _ml_market(
    mid: int,
    prices: list[dict],
    *,
    key: str = "s;0;m",
    period: int = 0,
    mtype: str = "moneyline",
    status: str = "open",
    version: int = 10,
) -> dict:
    return {
        "matchupId": mid,
        "key": key,
        "type": mtype,
        "period": period,
        "isAlternate": False,
        "status": status,
        "cutoffAt": "2026-06-17T07:00:00+00:00",
        "prices": prices,
        "version": version,
    }


def test_extract_moneyline_two_way_tennis() -> None:
    matchups = parse_matchups([_tennis_matchup()], now=NOW, horizon_end=HORIZON_END)
    markets = [
        _ml_market(
            1631935448,
            [
                {"designation": "home", "price": -1997},
                {"designation": "away", "price": 890},
            ],
            version=42,
        ),
    ]
    quotes = extract_moneyline_quotes(matchups, markets, now=NOW)
    assert len(quotes) == 1
    q = quotes[0]
    assert q.event_id == "1631935448"
    assert q.version == 42
    by_sel = {s.selection: s for s in q.snapshots}
    assert set(by_sel) == {"Ivan Marrero Curbelo", "Jan Choinski"}
    home = by_sel["Ivan Marrero Curbelo"]
    assert home.bookmaker == BOOKMAKER
    assert home.market == Market.H2H
    assert home.market_detail is None
    assert home.decimal_odds == pytest.approx(american_to_decimal(-1997))
    assert home.captured_at == NOW


def _soccer_matchup(mid: int = 555) -> dict:
    return {
        "id": mid,
        "type": "matchup",
        "parentId": None,
        "participants": [
            {"alignment": "home", "name": "Alpha FC"},
            {"alignment": "away", "name": "Beta United"},
        ],
        "startTime": "2026-06-17T18:00:00Z",
        "league": {"name": "Test League"},
    }


def test_extract_moneyline_three_way_soccer_has_draw() -> None:
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    markets = [
        _ml_market(
            555,
            [
                {"designation": "home", "price": 150},
                {"designation": "draw", "price": 230},
                {"designation": "away", "price": 180},
            ],
        ),
    ]
    quotes = extract_moneyline_quotes(matchups, markets, now=NOW)
    sels = {s.selection for s in quotes[0].snapshots}
    assert sels == {"Alpha FC", "Draw", "Beta United"}


def test_extract_moneyline_skips_non_match_winner_markets() -> None:
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    markets = [
        _ml_market(555, [{"designation": "home", "price": 150}], key="s;1;m", period=1),  # 1st half
        _ml_market(555, [{"designation": "over", "price": -110}], mtype="total", key="s;0;ou;2.5"),
        _ml_market(555, [{"designation": "home", "price": 150}], status="suspended"),
        _ml_market(999, [{"designation": "home", "price": 150}]),  # no matchup context
        _ml_market(555, [{"participantId": 7, "price": 150}]),  # participantId-keyed multiway
    ]
    quotes = extract_moneyline_quotes(matchups, markets, now=NOW)
    assert quotes == []


def test_extract_skips_versionless_market() -> None:
    # No `version` -> not change-gateable -> skip (capturing once would freeze
    # it: a synthesized 0 would gate every later reprice).
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    market = _ml_market(
        555,
        [
            {"designation": "home", "price": 150},
            {"designation": "away", "price": 180},
        ],
    )
    del market["version"]
    assert extract_moneyline_quotes(matchups, [market], now=NOW) == []


# --------------------------------------------------------------------------- #
# Totals (s;0;ou) + Spreads/AH (s;0;s) extraction — main line only
# --------------------------------------------------------------------------- #
def _total_market(
    mid: int,
    line: float,
    *,
    over: int = -110,
    under: int = -110,
    key: str | None = None,
    period: int = 0,
    status: str = "open",
    is_alt: bool = False,
    version: int = 20,
) -> dict:
    return {
        "matchupId": mid,
        "key": key or f"s;0;ou;{line}",
        "type": "total",
        "period": period,
        "isAlternate": is_alt,
        "status": status,
        "prices": [
            {"designation": "over", "points": line, "price": over},
            {"designation": "under", "points": line, "price": under},
        ],
        "version": version,
    }


def _spread_market(
    mid: int,
    home_line: float,
    *,
    home: int = -110,
    away: int = -110,
    period: int = 0,
    status: str = "open",
    is_alt: bool = False,
    version: int = 20,
) -> dict:
    return {
        "matchupId": mid,
        "key": f"s;0;s;{home_line}",
        "type": "spread",
        "period": period,
        "isAlternate": is_alt,
        "status": status,
        "prices": [
            {"designation": "home", "points": home_line, "price": home},
            {"designation": "away", "points": -home_line, "price": away},
        ],
        "version": version,
    }


def test_extract_total_quotes_over_under_with_line() -> None:
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    quotes = extract_total_quotes(
        matchups, [_total_market(555, 2.5, over=-105, under=-115)], now=NOW
    )
    assert len(quotes) == 1
    q = quotes[0]
    assert q.market_key == "s;0;ou;2.5"
    by_sel = {s.selection: s for s in q.snapshots}
    assert set(by_sel) == {"Over 2.5", "Under 2.5"}
    over = by_sel["Over 2.5"]
    assert over.market == Market.TOTALS
    assert over.market_detail == "over_under_2_5"
    assert over.bookmaker == BOOKMAKER
    assert over.decimal_odds == pytest.approx(american_to_decimal(-105))


def test_extract_total_integer_line_token() -> None:
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    q = extract_total_quotes(matchups, [_total_market(555, 3.0)], now=NOW)[0]
    assert {s.selection for s in q.snapshots} == {"Over 3", "Under 3"}
    assert all(s.market_detail == "over_under_3_0" for s in q.snapshots)


def test_extract_spread_quotes_signed_handicap_shared_detail() -> None:
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    quotes = extract_spread_quotes(
        matchups, [_spread_market(555, -1.5, home=-115, away=-105)], now=NOW
    )
    assert len(quotes) == 1
    q = quotes[0]
    assert q.market_key == "s;0;s;-1.5"
    by_sel = {s.selection: s for s in q.snapshots}
    assert set(by_sel) == {"Alpha FC -1.5", "Beta United +1.5"}
    home = by_sel["Alpha FC -1.5"]
    assert home.market == Market.SPREADS
    # market_detail keyed on the HOME handicap; BOTH sides share it (one line).
    assert home.market_detail == "asian_handicap_-1_5"
    assert by_sel["Beta United +1.5"].market_detail == "asian_handicap_-1_5"
    assert home.decimal_odds == pytest.approx(american_to_decimal(-115))


def _basketball_matchup(mid: int = 900) -> dict:
    return {
        "id": mid,
        "type": "matchup",
        "parentId": None,
        "participants": [
            {"alignment": "home", "name": "Lakers"},
            {"alignment": "away", "name": "Celtics"},
        ],
        "startTime": "2026-06-17T18:00:00Z",
        "league": {"name": "NBA"},
    }


def test_extract_total_quotes_basketball_uses_games_namespace() -> None:
    # BUG FIX: basketball totals must share the OddsPortal SOFT source's "_games"
    # market_detail (over_under_games_<line>) so the sharp Pinnacle snapshot
    # GROUPS WITH — and anchors — the basketball soft pick. The bare
    # over_under_<line> namespace grouped them apart -> no Pinnacle anchor.
    matchups = parse_matchups([_basketball_matchup()], now=NOW, horizon_end=HORIZON_END)
    quotes = extract_total_quotes(
        matchups, [_total_market(900, 220.5, over=-105, under=-115)], now=NOW, sport="basketball"
    )
    assert len(quotes) == 1
    by_sel = {s.selection: s for s in quotes[0].snapshots}
    # Side preserved (Over stays Over, Under stays Under); line preserved.
    assert set(by_sel) == {"Over 220.5", "Under 220.5"}
    over = by_sel["Over 220.5"]
    assert over.market == Market.TOTALS
    # EXACT soft vocabulary: over_under_games_<token>, token = "220_5".
    assert over.market_detail == "over_under_games_220_5"
    assert by_sel["Under 220.5"].market_detail == "over_under_games_220_5"
    assert over.decimal_odds == pytest.approx(american_to_decimal(-105))


def test_extract_spread_quotes_basketball_uses_games_namespace() -> None:
    matchups = parse_matchups([_basketball_matchup()], now=NOW, horizon_end=HORIZON_END)
    quotes = extract_spread_quotes(
        matchups, [_spread_market(900, -7.5, home=-115, away=-105)], now=NOW, sport="basketball"
    )
    assert len(quotes) == 1
    by_sel = {s.selection: s for s in quotes[0].snapshots}
    # Side preserved (home keeps its negative handicap, away its mirror positive).
    assert set(by_sel) == {"Lakers -7.5", "Celtics +7.5"}
    home = by_sel["Lakers -7.5"]
    assert home.market == Market.SPREADS
    # EXACT soft vocabulary: asian_handicap_games_<signed-token>, "-" kept, no "+".
    assert home.market_detail == "asian_handicap_games_-7_5"
    assert by_sel["Celtics +7.5"].market_detail == "asian_handicap_games_-7_5"
    assert home.decimal_odds == pytest.approx(american_to_decimal(-115))


def test_extract_spread_quotes_basketball_positive_home_line_has_no_plus() -> None:
    # CRITICAL: the soft "_games" token signs negatives with "-" but positives
    # carry NO "+" (the feed key is `(-?\d+...)`). A home UNDERDOG line (+7.5)
    # must therefore key as asian_handicap_games_7_5 — NOT _+7_5 — or it groups
    # apart from the soft pick and never anchors. (Arcadia's bare-namespace
    # _signed_token adds a "+"; basketball must not.)
    matchups = parse_matchups([_basketball_matchup()], now=NOW, horizon_end=HORIZON_END)
    quotes = extract_spread_quotes(
        matchups, [_spread_market(900, 7.5)], now=NOW, sport="basketball"
    )
    by_sel = {s.selection: s for s in quotes[0].snapshots}
    assert set(by_sel) == {"Lakers +7.5", "Celtics -7.5"}
    assert by_sel["Lakers +7.5"].market_detail == "asian_handicap_games_7_5"
    assert by_sel["Celtics -7.5"].market_detail == "asian_handicap_games_7_5"


def test_extract_soccer_detail_unchanged_with_explicit_sport() -> None:
    # SOCCER must stay on the BARE namespace exactly as shipped — its soft source
    # emits over_under_<line> / asian_handicap_<signed+plus> — even when sport is
    # passed explicitly. (Regression guard against the basketball fix leaking.)
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    totals = extract_total_quotes(matchups, [_total_market(555, 2.5)], now=NOW, sport="soccer")
    assert all(s.market_detail == "over_under_2_5" for s in totals[0].snapshots)
    spreads = extract_spread_quotes(matchups, [_spread_market(555, -1.5)], now=NOW, sport="soccer")
    assert all(s.market_detail == "asian_handicap_-1_5" for s in spreads[0].snapshots)
    # Positive soccer home line keeps the bare-namespace "+" (_signed_token).
    pos = extract_spread_quotes(matchups, [_spread_market(555, 0.5)], now=NOW, sport="soccer")
    assert all(s.market_detail == "asian_handicap_+0_5" for s in pos[0].snapshots)


def test_extract_market_quotes_basketball_threads_games_namespace() -> None:
    # The public combiner must thread the sport so a basketball capture cycle
    # gets the "_games" details end-to-end.
    matchups = parse_matchups([_basketball_matchup()], now=NOW, horizon_end=HORIZON_END)
    markets = [_total_market(900, 220.5), _spread_market(900, -7.5)]
    quotes = extract_market_quotes(matchups, markets, now=NOW, sport="basketball")
    details = {s.market_detail for q in quotes for s in q.snapshots}
    assert details == {"over_under_games_220_5", "asian_handicap_games_-7_5"}


def test_extract_skips_alternate_and_period_total_spread() -> None:
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    markets = [
        _total_market(555, 2.5, is_alt=True),  # alternate -> skip
        _spread_market(555, -1.5, is_alt=True),  # alternate -> skip
        _total_market(555, 2.5, key="s;1;ou;2.5", period=1),  # 1st-half -> skip
    ]
    assert extract_total_quotes(matchups, markets, now=NOW) == []
    assert extract_spread_quotes(matchups, markets, now=NOW) == []


def test_extract_market_quotes_combines_ml_total_spread() -> None:
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    markets = [
        _ml_market(
            555,
            [
                {"designation": "home", "price": 150},
                {"designation": "draw", "price": 230},
                {"designation": "away", "price": 180},
            ],
        ),
        _total_market(555, 2.5),
        _spread_market(555, -0.5),
    ]
    quotes = extract_market_quotes(matchups, markets, now=NOW)
    assert {q.market_key for q in quotes} == {"s;0;m", "s;0;ou;2.5", "s;0;s;-0.5"}
    assert {s.market for q in quotes for s in q.snapshots} == {
        Market.H2H,
        Market.TOTALS,
        Market.SPREADS,
    }


def test_version_gate_is_per_market_key() -> None:
    # ML + total share an event but have distinct market_keys -> they gate
    # INDEPENDENTLY (one repricing must not freeze the other).
    matchups = parse_matchups([_soccer_matchup()], now=NOW, horizon_end=HORIZON_END)
    ml = [
        {"designation": "home", "price": 150},
        {"designation": "draw", "price": 230},
        {"designation": "away", "price": 180},
    ]
    quotes = extract_market_quotes(
        matchups, [_ml_market(555, ml, version=10), _total_market(555, 2.5, version=10)], now=NOW
    )
    cap = _capture(("soccer",))
    fresh1, _ = cap._select_fresh(quotes, matchups, "soccer")
    assert {s.market for s in fresh1} == {Market.H2H, Market.TOTALS}
    # same versions re-observed -> nothing fresh
    assert cap._select_fresh(quotes, matchups, "soccer")[0] == []
    # only the total reprices -> only the total re-emits
    reprice = extract_market_quotes(
        matchups, [_ml_market(555, ml, version=10), _total_market(555, 2.5, version=11)], now=NOW
    )
    fresh3, _ = cap._select_fresh(reprice, matchups, "soccer")
    assert {s.market for s in fresh3} == {Market.TOTALS}


# --------------------------------------------------------------------------- #
# HTTP client (MockTransport — no live network)
# --------------------------------------------------------------------------- #
def _make_client(
    handler: Callable[[httpx.Request], httpx.Response], guest_key: str = ""
) -> PinnacleArcadiaClient:
    return PinnacleArcadiaClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        guest_key=guest_key,
    )


async def test_client_fetches_and_parses_lists() -> None:
    payload = [_tennis_matchup()]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload))

    client = _make_client(handler)
    rows = await client.fetch_matchups(SPORT_IDS["tennis"])
    assert rows == payload
    markets = await client.fetch_straight_markets(SPORT_IDS["tennis"])
    assert markets == payload  # handler echoes; only asserting transport/parse


async def test_client_sends_key_only_when_set() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-api-key"))
        return httpx.Response(200, content="[]")

    await _make_client(handler).fetch_matchups(33)
    await _make_client(handler, guest_key="PUBLIC-CONST").fetch_matchups(33)
    assert seen == [None, "PUBLIC-CONST"]


async def test_client_sends_pinnacle_referer_on_every_request() -> None:
    # The public web client (and the operator-provided R reference) set a
    # Referer of the Pinnacle site; arcadia's blocked-egress 403s are less
    # likely with it present. Asserted with AND without a guest key, since the
    # header rides every GET regardless of key state.
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("referer"))
        return httpx.Response(200, content="[]")

    await _make_client(handler).fetch_matchups(33)
    await _make_client(handler, guest_key="PUBLIC-CONST").fetch_straight_markets(33)
    assert seen == ["https://www.pinnacle.com/", "https://www.pinnacle.com/"]


async def test_discover_arcadia_config_sends_pinnacle_referer() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("referer"))
        return httpx.Response(
            200,
            json={
                "api": {"haywire": {"apiKey": "PUBLIC-CONST"}},
                "routes": {"curacao": {"guestRoot": "https://guest.example/0.1"}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await discover_arcadia_config(client)
    assert seen == ["https://www.pinnacle.com/"]


class _NamedTransport(httpx.AsyncBaseTransport):
    def __init__(self, name: str, seen: list[str]) -> None:
        self._name = name
        self._seen = seen
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._seen.append(self._name)
        return httpx.Response(200, json=[{"proxy": self._name}], request=request)

    async def aclose(self) -> None:
        self.closed = True


async def test_round_robin_transport_rotates_and_closes_children() -> None:
    seen: list[str] = []
    first = _NamedTransport("first", seen)
    second = _NamedTransport("second", seen)
    transport = _RoundRobinTransport((first, second))

    async with httpx.AsyncClient(transport=transport) as client:
        for _ in range(3):
            response = await client.get("https://example.test/path")
            assert response.status_code == 200

    assert seen == ["first", "second", "first"]
    assert first.closed is True
    assert second.closed is True


async def test_client_non_200_raises_without_url_or_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _make_client(handler, guest_key="SECRET-LOOKING-KEY")
    with pytest.raises(PinnacleArcadiaError) as excinfo:
        await client.fetch_matchups(33)
    msg = str(excinfo.value)
    assert "SECRET-LOOKING-KEY" not in msg
    assert "arcadia.pinnacle" not in msg


async def test_client_non_json_200_raises_arcadia_error() -> None:
    # A 200 with an anti-bot/HTML body must raise PinnacleArcadiaError (not a
    # bare JSONDecodeError) so capture_once's per-sport isolation holds.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="<html>anti-bot interstitial</html>")

    client = _make_client(handler, guest_key="SECRET-LOOKING-KEY")
    with pytest.raises(PinnacleArcadiaError) as excinfo:
        await client.fetch_matchups(33)
    msg = str(excinfo.value)
    assert "SECRET-LOOKING-KEY" not in msg
    assert "arcadia.pinnacle" not in msg


# --------------------------------------------------------------------------- #
# Transient HTTP-status retry (429 / 5xx) — robustness, no live network.
# tenacity's backoff sleeps via asyncio.sleep; neutralize it so the retry
# attempts run instantly without any real wall-clock wait.
# --------------------------------------------------------------------------- #
@pytest.fixture
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.mark.usefixtures("_no_backoff_sleep")
@pytest.mark.parametrize("transient_status", [429, 500, 502, 503, 504])
async def test_client_retries_transient_status_then_succeeds(transient_status: int) -> None:
    # A momentary upstream 429/5xx must be retried (not turned into an immediate
    # "no data this cycle"): two transient responses, then a 200 -> success.
    payload = [_tennis_matchup()]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(transient_status)
        return httpx.Response(200, content=json.dumps(payload))

    client = _make_client(handler)
    rows = await client.fetch_matchups(SPORT_IDS["tennis"])
    assert rows == payload
    assert calls["n"] == 3  # two transient failures retried, third attempt won


@pytest.mark.usefixtures("_no_backoff_sleep")
@pytest.mark.parametrize("transient_status", [429, 503])
async def test_client_exhausts_transient_retries_then_raises_arcadia_error(
    transient_status: int,
) -> None:
    # A persistently-transient status must, after exhausting attempts, surface as
    # the normal PinnacleArcadiaError (so per-sport isolation/dedupe is unchanged)
    # — carrying status only, never the URL or key.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(transient_status)

    client = _make_client(handler, guest_key="SECRET-LOOKING-KEY")
    with pytest.raises(PinnacleArcadiaError) as excinfo:
        await client.fetch_matchups(33)
    assert calls["n"] == 6  # stop_after_attempt(6): the status WAS retried
    msg = str(excinfo.value)
    assert str(transient_status) in msg  # status is reported (honest)
    assert "SECRET-LOOKING-KEY" not in msg
    assert "arcadia.pinnacle" not in msg


@pytest.mark.usefixtures("_no_backoff_sleep")
@pytest.mark.parametrize("permanent_status", [400, 401, 404, 422])
async def test_client_does_not_retry_permanent_4xx(permanent_status: int) -> None:
    # A permanent 4xx is a real error, not a hiccup: it must NOT be retried
    # (retrying burns budget for nothing) and must raise immediately. 403 is the
    # exception — a blocked proxy egress, where a DIFFERENT proxy works — so it
    # rotates/retries (see test_client_retries_403_blocked_proxy_then_succeeds).
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(permanent_status)

    client = _make_client(handler, guest_key="SECRET-LOOKING-KEY")
    with pytest.raises(PinnacleArcadiaError) as excinfo:
        await client.fetch_matchups(33)
    assert calls["n"] == 1  # exactly one call — no retry on a permanent 4xx
    msg = str(excinfo.value)
    assert "SECRET-LOOKING-KEY" not in msg
    assert "arcadia.pinnacle" not in msg


@pytest.mark.usefixtures("_no_backoff_sleep")
async def test_client_retries_403_blocked_proxy_then_succeeds() -> None:
    # arcadia 403s a blocked/datacenter egress IP but serves a healthy proxy
    # normally; _RoundRobinTransport advances per request, so a 403 must be
    # RETRIED (rotating to the next proxy) — not turned into an immediate "no
    # data this cycle". Two 403s, then a 200 -> success.
    payload = [_tennis_matchup()]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(403)
        return httpx.Response(200, content=json.dumps(payload))

    client = _make_client(handler)
    rows = await client.fetch_matchups(SPORT_IDS["tennis"])
    assert rows == payload
    assert calls["n"] == 3  # two 403s rotated past; the third proxy won


@pytest.mark.usefixtures("_no_backoff_sleep")
async def test_client_exhausts_403_then_raises_arcadia_error() -> None:
    # Every proxy blocked (persistent 403): after exhausting attempts it surfaces
    # as the normal PinnacleArcadiaError — status only, never the URL or key.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403)

    client = _make_client(handler, guest_key="SECRET-LOOKING-KEY")
    with pytest.raises(PinnacleArcadiaError) as excinfo:
        await client.fetch_matchups(33)
    assert calls["n"] == 6  # 403 WAS retried (rotated 6x), not an immediate failure
    msg = str(excinfo.value)
    assert "403" in msg
    assert "SECRET-LOOKING-KEY" not in msg
    assert "arcadia.pinnacle" not in msg


@pytest.mark.usefixtures("_no_backoff_sleep")
async def test_fetch_sports_retries_transient_status() -> None:
    # The /sports discovery path shares the retry: a transient 503 then 200.
    payload = [{"id": 29, "name": "Soccer", "isHidden": False, "matchupCount": 5}]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, content=json.dumps(payload))

    live = await _make_client(handler).fetch_sports()
    assert live == {"soccer": 29}
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# Dynamic sport discovery (GET /sports — live sports only)
# --------------------------------------------------------------------------- #
def test_sport_ids_carry_the_verified_public_ids() -> None:
    # The widened coverage set: the original 4 + the verified public ids.
    for key, sport_id in (
        ("soccer", 29),
        ("tennis", 33),
        ("basketball", 4),
        ("american_football", 15),
        ("baseball", 3),
        ("hockey", 19),
        ("rugby", 27),
        ("handball", 18),
    ):
        assert SPORT_IDS[key] == sport_id


async def test_fetch_sports_keeps_only_live_unhidden() -> None:
    # /sports payload: keep isHidden==false AND matchupCount>0 only.
    payload = [
        {"id": 29, "name": "Soccer", "isHidden": False, "matchupCount": 412},
        {"id": 3, "name": "Baseball", "isHidden": False, "matchupCount": 31},
        {"id": 19, "name": "Hockey", "isHidden": True, "matchupCount": 50},  # hidden -> drop
        {"id": 27, "name": "Rugby Union", "isHidden": False, "matchupCount": 0},  # empty -> drop
        {"id": 99, "name": "Esports", "matchupCount": 5},  # isHidden absent -> treat as live
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sports")
        return httpx.Response(200, content=json.dumps(payload))

    live = await _make_client(handler).fetch_sports()
    assert live == {"soccer": 29, "baseball": 3, "esports": 99}


# --------------------------------------------------------------------------- #
# Public config discovery (GET /config/app.json — best-effort, fallback-safe)
# --------------------------------------------------------------------------- #
_APP_JSON = {
    "api": {"haywire": {"apiKey": "PUBLIC-WEBCLIENT-CONST"}},
    "routes": {"curacao": {"guestRoot": "https://guest.api.arcadia.pinnacle.com/0.1"}},
    "apiVersion": "0.1",
}


def _config_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_discover_arcadia_config_success_populates_key_and_base() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CONFIG_APP_JSON_URL
        return httpx.Response(200, content=json.dumps(_APP_JSON))

    cfg = await discover_arcadia_config(_config_client(handler))
    assert isinstance(cfg, ArcadiaConfig)
    assert cfg.base_url == "https://guest.api.arcadia.pinnacle.com/0.1"
    # The guest key is a SecretStr-style value: only revealed via the explicit
    # accessor, never in repr.
    assert cfg.guest_key.get_secret_value() == "PUBLIC-WEBCLIENT-CONST"
    assert "PUBLIC-WEBCLIENT-CONST" not in repr(cfg)


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda r: httpx.Response(503), id="non-200"),
        pytest.param(lambda r: httpx.Response(200, content="<html>nope</html>"), id="non-json"),
        pytest.param(lambda r: httpx.Response(200, content=json.dumps({})), id="missing-keys"),
        pytest.param(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom")), id="transport-error"
        ),
    ],
)
async def test_discover_arcadia_config_any_failure_falls_back_to_none(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    cfg = await discover_arcadia_config(_config_client(handler))
    assert cfg is None


async def test_discover_arcadia_config_never_logs_the_key(caplog) -> None:  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(_APP_JSON))

    with caplog.at_level(logging.DEBUG):
        cfg = await discover_arcadia_config(_config_client(handler))
    assert cfg is not None
    # CRITICAL secret hygiene: the public guest key must never appear in ANY
    # log record (message or args), even on the success path.
    for record in caplog.records:
        assert "PUBLIC-WEBCLIENT-CONST" not in record.getMessage()
        assert "PUBLIC-WEBCLIENT-CONST" not in str(record.args)


async def test_discover_arcadia_config_failure_never_logs_url_or_raises(caplog) -> None:  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with caplog.at_level(logging.DEBUG):
        cfg = await discover_arcadia_config(_config_client(handler))
    assert cfg is None
    # OUR module's records must never carry the source URL (it identifies the
    # source / would carry the key on key-bearing endpoints). httpx's own
    # request logger is framework behaviour and out of scope here; the key
    # value (asserted across ALL records elsewhere) is what must never leak.
    ours = [r for r in caplog.records if r.name == "app.ingestion.pinnacle_arcadia"]
    assert ours, "expected at least one log record from our module"
    for record in ours:
        assert "pinnacle.com" not in record.getMessage()
        assert "arcadia.pinnacle" not in record.getMessage()


# --------------------------------------------------------------------------- #
# Version change-gate (pure, no DB)
# --------------------------------------------------------------------------- #
def _capture(sports: tuple[str, ...] = ("tennis",)) -> PinnacleArcadiaCapture:
    return PinnacleArcadiaCapture(
        client=None,  # _select_fresh never touches the client
        session_factory=None,
        sports=sports,
        horizon=timedelta(hours=72),
    )


def test_version_gate_emits_once_until_reprice() -> None:
    matchups = parse_matchups([_tennis_matchup()], now=NOW, horizon_end=HORIZON_END)
    markets = [
        _ml_market(
            1631935448,
            [
                {"designation": "home", "price": -1997},
                {"designation": "away", "price": 890},
            ],
            version=42,
        )
    ]
    quotes = extract_moneyline_quotes(matchups, markets, now=NOW)
    cap = _capture()

    fresh1, teams1 = cap._select_fresh(quotes, matchups, "tennis")
    assert len(fresh1) == 2
    assert set(teams1) == {"1631935448"}

    # Same version re-observed -> nothing fresh
    fresh2, teams2 = cap._select_fresh(quotes, matchups, "tennis")
    assert fresh2 == []
    assert teams2 == {}

    # A reprice (higher version) -> emit again
    reprice = extract_moneyline_quotes(
        matchups,
        [
            _ml_market(
                1631935448,
                [
                    {"designation": "home", "price": -1500},
                    {"designation": "away", "price": 700},
                ],
                version=43,
            )
        ],
        now=NOW,
    )
    fresh3, _ = cap._select_fresh(reprice, matchups, "tennis")
    assert len(fresh3) == 2


def test_quote_is_frozen_dataclass() -> None:
    q = MarketQuote(event_id="1", market_key="s;0;m", version=1, snapshots=())
    with pytest.raises(FrozenInstanceError):
        q.version = 2  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# capture_once dynamic-discovery driving + isolation + fallback (no DB)
# --------------------------------------------------------------------------- #
class _RecordingSource:
    """Records the sport_ids matchups were fetched for; lets a chosen sport
    raise (per-sport isolation) and lets /sports raise (fallback). No network,
    no DB (session_factory stays None so persistence is skipped)."""

    def __init__(
        self,
        *,
        live_sports: dict[str, int] | None,
        raise_for_sport_id: int | None = None,
        sports_error: Exception | None = None,
    ) -> None:
        self._live = live_sports
        self._raise_for = raise_for_sport_id
        self._sports_error = sports_error
        self.fetched_sport_ids: list[int] = []
        self.sports_calls = 0

    async def fetch_sports(self) -> dict[str, int]:
        self.sports_calls += 1
        if self._sports_error is not None:
            raise self._sports_error
        assert self._live is not None
        return dict(self._live)

    async def fetch_matchups(self, sport_id: int) -> list[dict]:
        self.fetched_sport_ids.append(sport_id)
        if self._raise_for is not None and sport_id == self._raise_for:
            raise PinnacleArcadiaError(f"boom for sport={sport_id}")
        return []

    async def fetch_straight_markets(self, sport_id: int) -> list[dict]:
        return []


def _no_db_capture(source: _RecordingSource, sports: tuple[str, ...]) -> PinnacleArcadiaCapture:
    return PinnacleArcadiaCapture(
        client=source,
        session_factory=None,  # no persistence; we only assert iteration/isolation
        sports=sports,
        horizon=timedelta(hours=72),
        now_fn=lambda: NOW,
    )


async def test_capture_once_intersects_configured_with_live_sports() -> None:
    # Configured 3 sports; /sports reports only soccer+baseball live -> hockey
    # (matchupCount==0/hidden upstream) is SKIPPED, never fetched.
    source = _RecordingSource(live_sports={"soccer": 29, "baseball": 3})
    cap = _no_db_capture(source, ("soccer", "baseball", "hockey"))
    written = await cap.capture_once()
    assert source.sports_calls == 1
    # hockey id (19) must never be fetched; soccer (29) + baseball (3) are.
    assert set(source.fetched_sport_ids) == {29, 3}
    assert 19 not in source.fetched_sport_ids
    # Skipped sport still appears in the result with 0 rows (honest, not silent).
    assert written == {"soccer": 0, "baseball": 0, "hockey": 0}


async def test_capture_once_per_sport_isolation_one_raise_does_not_abort_rest() -> None:
    # soccer raises mid-cycle; baseball + hockey still run.
    source = _RecordingSource(
        live_sports={"soccer": 29, "baseball": 3, "hockey": 19},
        raise_for_sport_id=29,
    )
    cap = _no_db_capture(source, ("soccer", "baseball", "hockey"))
    written = await cap.capture_once()
    # All three live sports were attempted (isolation = the raise is contained).
    assert set(source.fetched_sport_ids) == {29, 3, 19}
    # soccer raised -> omitted from the written map; the others recorded 0.
    assert "soccer" not in written
    assert written == {"baseball": 0, "hockey": 0}


async def test_capture_once_falls_back_to_configured_when_fetch_sports_fails() -> None:
    # /sports raises -> fall back to capturing ALL configured sports as today;
    # the cycle is never aborted.
    source = _RecordingSource(
        live_sports=None,
        sports_error=httpx.ConnectError("sports endpoint down"),
    )
    cap = _no_db_capture(source, ("soccer", "baseball", "hockey"))
    written = await cap.capture_once()
    assert source.sports_calls == 1
    # Fallback = every configured sport's id is fetched regardless of /sports.
    assert set(source.fetched_sport_ids) == {29, 3, 19}
    assert written == {"soccer": 0, "baseball": 0, "hockey": 0}


async def test_capture_once_unknown_configured_sport_is_skipped() -> None:
    # A configured sport with no SPORT_IDS entry is skipped before any fetch.
    source = _RecordingSource(live_sports={"soccer": 29})
    cap = _no_db_capture(source, ("soccer", "quidditch"))
    written = await cap.capture_once()
    assert source.fetched_sport_ids == [29]
    assert written == {"soccer": 0}


# --------------------------------------------------------------------------- #
# capture_once integration (compose Postgres; skip when absent)
# --------------------------------------------------------------------------- #
@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


class _StubClient:
    """Returns canned matchups/markets; records call counts. No network.

    fetch_sports defaults to "every configured sport is live" so the existing
    integration tests are unaffected by the dynamic-discovery intersection.
    """

    def __init__(
        self,
        matchups: list[dict],
        markets: list[dict],
        *,
        live_sports: dict[str, int] | None = None,
    ) -> None:
        self._matchups = matchups
        self._markets = markets
        # None = return ALL known sport ids as live (intersection is a no-op).
        self._live = live_sports if live_sports is not None else dict(SPORT_IDS)
        self.calls = 0

    async def fetch_sports(self) -> dict[str, int]:
        return dict(self._live)

    async def fetch_matchups(self, sport_id: int) -> list[dict]:
        self.calls += 1
        return self._matchups

    async def fetch_straight_markets(self, sport_id: int) -> list[dict]:
        return self._markets


async def _pinnacle_rows(maker, sport_key: str) -> int:  # type: ignore[no-untyped-def]
    from app.storage.models import Sport

    async with maker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(OddsSnapshot)
            .join(Event, OddsSnapshot.event_id == Event.id)
            .join(Sport, Event.sport_id == Sport.id)
            .where(Sport.key == sport_key)
        )
    return int(count or 0)


async def test_capture_once_persists_pinnacle_namespace_and_change_gates(factory) -> None:  # type: ignore[no-untyped-def]
    matchups = [_tennis_matchup(mid=1631935448, start="2026-12-01T07:00:00Z")]
    markets = [
        _ml_market(
            1631935448,
            [
                {"designation": "home", "price": -1997},
                {"designation": "away", "price": 890},
            ],
            version=42,
        )
    ]
    stub = _StubClient(matchups, markets)
    cap = PinnacleArcadiaCapture(
        client=stub,
        session_factory=factory,
        sports=("tennis",),
        horizon=timedelta(days=365),
        now_fn=lambda: NOW,
    )

    # Delta-based: the compose DB may already hold live `tennis` rows from the
    # running app; assert the capture only TOUCHES the isolated namespace.
    pinnacle_before = await _pinnacle_rows(factory, "pinnacle_tennis")
    tennis_before = await _pinnacle_rows(factory, "tennis")

    written = await cap.capture_once()
    assert written == {"tennis": 2}
    assert await _pinnacle_rows(factory, "pinnacle_tennis") == pinnacle_before + 2
    # The isolated namespace must NOT touch the real tennis sport.
    assert await _pinnacle_rows(factory, "tennis") == tennis_before

    # Same version on the next cycle -> change-gated, zero new rows.
    again = await cap.capture_once()
    assert again == {"tennis": 0}
    assert await _pinnacle_rows(factory, "pinnacle_tennis") == pinnacle_before + 2


async def test_capture_once_uses_pinnacle_bookmaker(factory) -> None:  # type: ignore[no-untyped-def]
    from app.storage.models import Sport

    matchups = [_soccer_matchup(mid=777)]
    markets = [
        _ml_market(
            777,
            [
                {"designation": "home", "price": 150},
                {"designation": "draw", "price": 230},
                {"designation": "away", "price": 180},
            ],
        )
    ]
    cap = PinnacleArcadiaCapture(
        client=_StubClient(matchups, markets),
        session_factory=factory,
        sports=("soccer",),
        horizon=timedelta(days=365),
        now_fn=lambda: NOW,
    )
    await cap.capture_once()
    async with factory() as session:
        books = (
            (
                await session.execute(
                    select(OddsSnapshot.bookmaker)
                    .join(Event, OddsSnapshot.event_id == Event.id)
                    .join(Sport, Event.sport_id == Sport.id)
                    .where(Sport.key == "pinnacle_soccer")
                )
            )
            .scalars()
            .all()
        )
    assert set(books) == {BOOKMAKER}
