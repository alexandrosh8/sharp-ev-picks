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
    MoneylineQuote,
    PinnacleArcadiaCapture,
    PinnacleArcadiaClient,
    PinnacleArcadiaError,
    american_to_decimal,
    extract_moneyline_quotes,
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
    q = MoneylineQuote(event_id="1", version=1, snapshots=())
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
