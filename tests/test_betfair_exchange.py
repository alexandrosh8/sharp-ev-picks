"""Read-only Betfair Exchange BACK-odds capture (OddsPortal DOM row).

Pure parser + odds-math tests need no network; the page reader is exercised
through an INJECTED ``page_loader`` (static token list, no browser/network); the
price change-gate runs without a DB; the capture_once integration test uses the
compose Postgres (skip when absent, same pattern as the arcadia/persistence
tests). NO live network, ever.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams, ScraperProxy
from app.ingestion.betfair_exchange import (
    BOOKMAKER,
    SPORT_SEGMENTS,
    BackQuote,
    BetfairExchangeCapture,
    BetfairExchangeError,
    BetfairExchangeReader,
    MatchTarget,
    _namespace_event_ref,
    _pair_tokens,
    back_quotes_to_snapshots,
    extract_back_quotes,
    fractional_to_decimal,
    parse_liquidity,
)
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.storage.models import Event, OddsSnapshot
from app.storage.repositories import persist_odds_snapshots

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai"
NOW = datetime(2026, 6, 19, 18, 0, tzinfo=UTC)

# The Betfair Exchange row text the user verified live (2026-06-19, UK proxy):
#   "Betfair Exchange ... Back Lay 28/25 (9052) 5/2 (3307) 3/1 (1307) 99.3%
#    57/50 (11317) 51/20 (41) 31/10 (2683) 100"
# -> BACK home/draw/away = 28/25, 5/2, 3/1 ; LAY = 57/50, 51/20, 31/10.
# Our DOM extractor keeps only fractional-odds + parenthesised-liquidity tokens
# (overround %s are dropped), so the ordered token list it returns is:
_LIVE_ROW_TOKENS = (
    "28/25",
    "(9052)",
    "5/2",
    "(3307)",
    "3/1",
    "(1307)",
    "57/50",
    "(11317)",
    "51/20",
    "(41)",
    "31/10",
    "(2683)",
)


# --------------------------------------------------------------------------- #
# Fractional -> decimal odds math (TDD core: failing test first, then code)
# --------------------------------------------------------------------------- #
def test_fractional_to_decimal_user_examples() -> None:
    # The exact conversions the user quoted from the live row.
    assert fractional_to_decimal("28/25") == pytest.approx(2.12)
    assert fractional_to_decimal("5/2") == pytest.approx(3.5)
    assert fractional_to_decimal("3/1") == pytest.approx(4.0)
    assert fractional_to_decimal("57/50") == pytest.approx(2.14)


def test_fractional_to_decimal_evens_and_odds_on() -> None:
    assert fractional_to_decimal("1/1") == pytest.approx(2.0)  # evens
    assert fractional_to_decimal("1/2") == pytest.approx(1.5)  # odds-on
    assert fractional_to_decimal(" 10/3 ") == pytest.approx(4.3333, abs=1e-3)


def test_fractional_to_decimal_rejects_garbage() -> None:
    for bad in ("", "2.12", "5-2", "abc", "5/", "/2", "(9052)"):
        assert fractional_to_decimal(bad) is None


def test_fractional_to_decimal_rejects_zero_denominator() -> None:
    assert fractional_to_decimal("5/0") is None


def test_fractional_to_decimal_always_above_one() -> None:
    # Even the shortest odds-on price stays a valid decimal (> 1.0).
    assert (fractional_to_decimal("1/100") or 0.0) > 1.0


# --------------------------------------------------------------------------- #
# Liquidity token parsing
# --------------------------------------------------------------------------- #
def test_parse_liquidity_plain_and_thousands() -> None:
    assert parse_liquidity("(9052)") == 9052.0
    assert parse_liquidity("(11,317)") == 11317.0
    assert parse_liquidity(" (41) ") == 41.0


def test_parse_liquidity_rejects_non_liquidity() -> None:
    for bad in ("9052", "28/25", "99.3%", "()", "(abc)", ""):
        assert parse_liquidity(bad) is None


# --------------------------------------------------------------------------- #
# Token pairing: ordered DOM tokens -> (odds, liquidity) cells
# --------------------------------------------------------------------------- #
def test_pair_tokens_back_then_lay() -> None:
    cells = _pair_tokens(_LIVE_ROW_TOKENS)
    # 6 cells: 3 BACK then 3 LAY, each (fraction, liquidity).
    assert cells == [
        ("28/25", 9052.0),
        ("5/2", 3307.0),
        ("3/1", 1307.0),
        ("57/50", 11317.0),
        ("51/20", 41.0),
        ("31/10", 2683.0),
    ]


def test_pair_tokens_odds_without_liquidity_pairs_none() -> None:
    cells = _pair_tokens(("28/25", "5/2", "(3307)"))
    assert cells == [("28/25", None), ("5/2", 3307.0)]


# --------------------------------------------------------------------------- #
# BACK-vs-LAY selection + liquidity gate
# --------------------------------------------------------------------------- #
def test_extract_back_quotes_takes_back_side_only() -> None:
    cells = _pair_tokens(_LIVE_ROW_TOKENS)
    quotes = extract_back_quotes(cells, min_liquidity=0.0)
    # Exactly the 3 BACK outcomes (home/draw/away) — never the LAY triple.
    assert [q.designation for q in quotes] == ["home", "draw", "away"]
    assert [round(q.decimal_odds, 2) for q in quotes] == [2.12, 3.5, 4.0]
    assert [q.liquidity for q in quotes] == [9052.0, 3307.0, 1307.0]
    # The LAY prices (57/50 -> 2.14, etc.) must NOT appear.
    assert all(q.decimal_odds != pytest.approx(2.14) for q in quotes)


def test_liquidity_gate_drops_thin_outcomes() -> None:
    cells = _pair_tokens(_LIVE_ROW_TOKENS)
    # Floor between the draw (3307) and away (1307) liquidities.
    quotes = extract_back_quotes(cells, min_liquidity=2000.0)
    assert [q.designation for q in quotes] == ["home", "draw"]  # away (1307) gated out


def test_liquidity_gate_can_empty_a_thin_market() -> None:
    cells = _pair_tokens(_LIVE_ROW_TOKENS)
    assert extract_back_quotes(cells, min_liquidity=1_000_000.0) == []


def test_extract_back_quotes_skips_missing_liquidity() -> None:
    # An odds cell whose liquidity is absent is dropped even with a 0 floor.
    cells: list[tuple[str, float | None]] = [("28/25", None), ("5/2", 3307.0), ("3/1", 1307.0)]
    quotes = extract_back_quotes(cells, min_liquidity=0.0)
    assert [q.designation for q in quotes] == ["draw", "away"]


def test_extract_back_quotes_two_way_outcomes() -> None:
    # Forward-compat: a 2-way market (tennis) keys home/away only.
    cells: list[tuple[str, float | None]] = [("5/4", 5000.0), ("13/8", 4000.0)]
    quotes = extract_back_quotes(cells, outcomes=("home", "away"), min_liquidity=0.0)
    assert [q.designation for q in quotes] == ["home", "away"]


# --------------------------------------------------------------------------- #
# Snapshot construction (selection naming + liquidity carried through)
# --------------------------------------------------------------------------- #
def _teams() -> EventTeams:
    return EventTeams(home="Real Madrid", away="Al Hilal", league="FIFA Club World Cup")


def test_back_quotes_to_snapshots_names_selections() -> None:
    quotes = [
        BackQuote("home", 2.12, 9052.0),
        BackQuote("draw", 3.5, 3307.0),
        BackQuote("away", 4.0, 1307.0),
    ]
    snaps = back_quotes_to_snapshots("https://op/match", quotes, _teams(), now=NOW)
    assert [s.selection for s in snaps] == ["Real Madrid", "Draw", "Al Hilal"]
    assert all(s.bookmaker == BOOKMAKER for s in snaps)
    assert all(s.market is Market.H2H for s in snaps)
    assert [s.decimal_odds for s in snaps] == [2.12, 3.5, 4.0]
    assert [s.liquidity for s in snaps] == [9052.0, 3307.0, 1307.0]
    assert all(s.captured_at == NOW and s.ingested_at == NOW for s in snaps)
    # Aware-UTC timestamps (naive would be a bug).
    assert all(s.captured_at.tzinfo is not None for s in snaps)


def test_bookmaker_name_is_a_sharp_book() -> None:
    # The persisted name must normalize to the SHARP_BOOKS / EXCHANGE_COMMISSION
    # key so the edge engine can ever recognise it (v1 mints nothing, but the
    # name must stay aligned).
    from app.edge.value import EXCHANGE_COMMISSION, SHARP_BOOKS

    assert BOOKMAKER.lower() in SHARP_BOOKS
    assert BOOKMAKER.lower() in EXCHANGE_COMMISSION


def test_backquote_is_frozen() -> None:
    q = BackQuote("home", 2.12, 9052.0)
    with pytest.raises(FrozenInstanceError):
        q.decimal_odds = 9.9  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Reader: injected page_loader -> NO network. End-to-end token -> BACK quote.
# --------------------------------------------------------------------------- #
def _static_loader(
    tokens: Sequence[str] | None,
) -> Callable[..., Awaitable[list[str] | None]]:
    async def loader(*, url: str, proxy: ScraperProxy | None) -> list[str] | None:  # noqa: ARG001
        return list(tokens) if tokens is not None else None

    return loader


@pytest.mark.asyncio
async def test_reader_parses_live_row_via_injected_loader() -> None:
    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(_LIVE_ROW_TOKENS))
    quotes = await reader.read_back_quotes("https://op/match")
    assert [q.designation for q in quotes] == ["home", "draw", "away"]
    assert [round(q.decimal_odds, 2) for q in quotes] == [2.12, 3.5, 4.0]


@pytest.mark.asyncio
async def test_reader_no_betfair_row_returns_empty() -> None:
    # A thin/obscure match with no Betfair Exchange row (loader returns None) is
    # an expected gap, not an error.
    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(None))
    assert await reader.read_back_quotes("https://op/thin") == []


@pytest.mark.asyncio
async def test_reader_proxy_failover_then_raises() -> None:
    attempts = {"n": 0}

    async def boom(*, url: str, proxy: ScraperProxy | None) -> list[str] | None:  # noqa: ARG001
        attempts["n"] += 1
        raise TimeoutError("transport")

    pool = (
        ScraperProxy(url="http://h1:1", username="u", password="p"),
        ScraperProxy(url="http://h2:2", username="u", password="p"),
    )
    reader = BetfairExchangeReader(min_liquidity=0.0, proxy_pool=pool, page_loader=boom)
    with pytest.raises(BetfairExchangeError) as exc:
        await reader.read_back_quotes("https://op/match")
    assert attempts["n"] == 2  # tried every proxy
    # Error message carries the exception TYPE only — never the URL or creds.
    assert "https://op/match" not in str(exc.value)
    assert "TimeoutError" in str(exc.value)
    for secret in ("h1", "h2", "pass", "://u"):
        assert secret not in str(exc.value)


# --------------------------------------------------------------------------- #
# Capture: price change-gate + isolation (no picks/alerts; betfair_ namespace)
# --------------------------------------------------------------------------- #
def _target(url: str = "https://op/rma-hil") -> MatchTarget:
    return MatchTarget(event_id=url, url=url, teams=_teams())


@pytest.mark.asyncio
async def test_capture_change_gate_emits_once_then_on_move() -> None:
    # Build a reader whose tokens we can swap between cycles.
    state = {"tokens": list(_LIVE_ROW_TOKENS)}

    async def loader(*, url: str, proxy: ScraperProxy | None) -> list[str]:  # noqa: ARG001
        return list(state["tokens"])

    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=loader)
    capture = BetfairExchangeCapture(
        reader,
        session_factory=None,  # no DB: exercise the gate only
        targets_fn=lambda sport: [_target()],
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    # First cycle persists nothing (session_factory None) but PRIMES the gate.
    assert await capture.capture_once() == {"soccer": 0}
    # Re-reading the SAME prices yields no fresh snapshots (all gated).
    fresh = capture._select_fresh(
        "soccer",
        _namespace_event_ref(_target().event_id),
        back_quotes_to_snapshots(
            _target().event_id,
            await reader.read_back_quotes(_target().url),
            _teams(),
            now=NOW,
        ),
    )
    assert fresh == []
    # A price move on the home BACK leg (28/25 -> 6/5) re-opens that selection.
    state["tokens"][0] = "6/5"
    moved = capture._select_fresh(
        "soccer",
        _namespace_event_ref(_target().event_id),
        back_quotes_to_snapshots(
            _target().event_id,
            await reader.read_back_quotes(_target().url),
            _teams(),
            now=NOW,
        ),
    )
    assert [s.selection for s in moved] == ["Real Madrid"]


@pytest.mark.asyncio
async def test_capture_skips_unsupported_sport() -> None:
    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(_LIVE_ROW_TOKENS))
    capture = BetfairExchangeCapture(
        reader,
        session_factory=None,
        targets_fn=lambda sport: [_target()],
        sports=("basketball",),  # not in SPORT_SEGMENTS (v1 = soccer only)
        now_fn=lambda: NOW,
    )
    assert "basketball" not in SPORT_SEGMENTS
    assert await capture.capture_once() == {}  # unsupported sport skipped


@pytest.mark.asyncio
async def test_capture_none_reader_writes_nothing() -> None:
    capture = BetfairExchangeCapture(
        None, session_factory=None, targets_fn=lambda sport: [], sports=("soccer",)
    )
    assert await capture.capture_once() == {"soccer": 0}


# --------------------------------------------------------------------------- #
# DB integration: rows land under the ISOLATED betfair_<sport> namespace and
# carry NO picks (capture writes odds_snapshots only). Skipped without Postgres.
# --------------------------------------------------------------------------- #
async def _engine_or_skip() -> AsyncEngine:
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect():
            pass
    except Exception:  # noqa: BLE001
        await engine.dispose()
        pytest.skip("compose Postgres not reachable")
    return engine


@pytest.mark.asyncio
async def test_capture_persists_under_isolated_namespace() -> None:
    engine = await _engine_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Unique per run: the append-only unique key would dedupe a re-run's rows
    # against a fixed external_ref, so a deterministic URL makes the test pass
    # only on the FIRST run against a given DB. uuid keeps it idempotent.
    url = f"https://op/betfair-iso-{uuid4()}"

    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(_LIVE_ROW_TOKENS))
    capture = BetfairExchangeCapture(
        reader,
        session_factory=factory,
        targets_fn=lambda sport: [_target(url)],
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    try:
        written = await capture.capture_once()
        assert written["soccer"] == 3  # home/draw/away BACK rows
        async with factory() as session:
            rows = (
                await session.execute(
                    select(OddsSnapshot.bookmaker, OddsSnapshot.selection, OddsSnapshot.liquidity)
                    .join(Event, Event.id == OddsSnapshot.event_id)
                    .where(Event.external_ref == f"betfair:{url}")
                )
            ).all()
            assert len(rows) == 3
            assert {r.bookmaker for r in rows} == {BOOKMAKER}
            assert {r.selection for r in rows} == {"Real Madrid", "Draw", "Al Hilal"}
            # Liquidity persisted as NUMERIC (Decimal at the boundary).
            assert all(r.liquidity is not None for r in rows)
            # Exactly one event row exists for this isolated-namespace external_ref.
            event_count = await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.external_ref == f"betfair:{url}")
            )
            assert event_count == 1
        # Re-capture with the SAME prices writes nothing new (change-gate + the
        # append-only unique key both hold).
        assert (await capture.capture_once())["soccer"] == 0
    finally:
        await engine.dispose()


async def test_capture_does_not_graft_onto_live_event_with_same_url() -> None:
    # Regression for the isolation breach the review caught: a live soccer event
    # and the Betfair capture share the SAME OddsPortal match URL. Events are
    # keyed by external_ref ALONE (globally unique, not sport-scoped), so without
    # the "betfair:" namespace the capture would REUSE the soccer event row and
    # its "Betfair Exchange" sharp BACK price would leak into that event closing
    # CLV anchor. This pins true event-level isolation.
    engine = await _engine_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    url = f"https://op/betfair-collision-{uuid4()}"
    teams = EventTeams(home="Real Madrid", away="Al Hilal", league="Club WC")

    soft = OddsSnapshotIn(
        event_id=url,
        bookmaker="bet365",
        market=Market.H2H,
        selection="Real Madrid",
        decimal_odds=2.0,
        captured_at=NOW,
        ingested_at=NOW,
    )
    await persist_odds_snapshots(
        factory, [soft], {url: teams}, sport="soccer", default_league="Club WC"
    )

    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(_LIVE_ROW_TOKENS))
    capture = BetfairExchangeCapture(
        reader,
        session_factory=factory,
        targets_fn=lambda sport: [_target(url)],
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    try:
        written = await capture.capture_once()
        assert written["soccer"] == 3
        async with factory() as session:
            soccer_books = (
                (
                    await session.execute(
                        select(OddsSnapshot.bookmaker)
                        .join(Event, Event.id == OddsSnapshot.event_id)
                        .where(Event.external_ref == url)
                    )
                )
                .scalars()
                .all()
            )
            assert set(soccer_books) == {"bet365"}
            assert BOOKMAKER not in soccer_books
            bf_books = (
                (
                    await session.execute(
                        select(OddsSnapshot.bookmaker)
                        .join(Event, Event.id == OddsSnapshot.event_id)
                        .where(Event.external_ref == f"betfair:{url}")
                    )
                )
                .scalars()
                .all()
            )
            assert set(bf_books) == {BOOKMAKER}
            assert len(bf_books) == 3
            live_event_count = await session.scalar(
                select(func.count()).select_from(Event).where(Event.external_ref == url)
            )
            betfair_event_count = await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.external_ref == f"betfair:{url}")
            )
            assert live_event_count == 1
            assert betfair_event_count == 1
    finally:
        await engine.dispose()
