"""Clean-room Pinnacle (arcadia guest API) sharp-line capture.

Pure parser + odds-math tests need no network; the HTTP client uses
httpx.MockTransport; the version-gate is exercised without a DB; the
capture_once integration test uses the compose Postgres (skip when absent,
same pattern as tests/test_odds_snapshot_persistence.py). No live network,
ever.
"""

import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.pinnacle_arcadia import (
    BOOKMAKER,
    SPORT_IDS,
    MarketQuote,
    PinnacleArcadiaCapture,
    PinnacleArcadiaClient,
    PinnacleArcadiaError,
    _RoundRobinTransport,
    american_to_decimal,
    extract_market_quotes,
    extract_moneyline_quotes,
    extract_spread_quotes,
    extract_total_quotes,
    parse_matchups,
)
from app.schemas.base import Market
from app.storage.models import Event, OddsSnapshot

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai"
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
    """Returns canned matchups/markets; records call counts. No network."""

    def __init__(self, matchups: list[dict], markets: list[dict]) -> None:
        self._matchups = matchups
        self._markets = markets
        self.calls = 0

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
