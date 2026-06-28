"""Read-only Betfair Exchange BACK-odds capture from OddsPortal's JSON feed.

The capture now rides the SAME curl_cffi JSON/HTTP feed the main football scrape
uses (``app/ingestion/oddsportal_json.py``) instead of the retired Playwright DOM
reader: per targeted event it GETs the per-market ``.dat`` feed, decrypts it, and
reads the Betfair Exchange (provider id "44") BACK price + matched ``volume`` for
the liquidity gate. The pure parser (``parse_betfair_feed``) is exercised with the
recon's exact decrypted-JSON shapes (no network, no crypto); the reader is driven
through an INJECTED ``feed_loader`` (no curl_cffi); the price change-gate runs
without a DB; the capture_once integration test uses the compose Postgres (skip
when absent). NO live network, ever.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.ingestion.betfair_exchange import (
    BETFAIR_PROVIDER_ID,
    BOOKMAKER,
    SPORT_SEGMENTS,
    BetfairExchangeCapture,
    BetfairExchangeReader,
    FeedFeasible,
    MatchTarget,
    feed_markets_for_sport,
    parse_betfair_feed,
)
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.storage.models import Event, OddsSnapshot
from app.storage.repositories import persist_odds_snapshots

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"
NOW = datetime(2026, 6, 28, 18, 0, tzinfo=UTC)
# A real provider observation epoch (d.time-base) -> captured_at.
TIME_BASE = 1782352800


# --------------------------------------------------------------------------- #
# Feed payload builders — the EXACT decrypted-JSON shapes the read-only recon
# proved (soccer 1x2 = 3-way DICT, soccer OU 2.5 = 2-way LIST, basketball
# moneyline = 2-way LIST), each carrying odds["44"] + volume["44"] for Betfair.
# --------------------------------------------------------------------------- #
def _payload(
    feed_key: str,
    odds44: Any,
    volume44: Any | None,
    *,
    time_base: int | None = TIME_BASE,
    extra_books: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    odds: dict[str, Any] = {}
    if odds44 is not None:
        odds[BETFAIR_PROVIDER_ID] = odds44
    if extra_books:
        odds.update(extra_books)
    block: dict[str, Any] = {"odds": odds}
    if volume44 is not None:
        block["volume"] = {BETFAIR_PROVIDER_ID: volume44}
    d: dict[str, Any] = {"oddsdata": {"back": {feed_key: block}}}
    if time_base is not None:
        d["time-base"] = time_base
    return {"d": d}


def _soccer_1x2_payload(
    odds: Mapping[str, float] | None = None,
    volume: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    return _payload(
        "E-1-2-0-0-0",
        odds if odds is not None else {"0": 2.12, "1": 3.5, "2": 4.0},
        volume if volume is not None else {"0": 9052, "1": 3307, "2": 1307},
    )


def _soccer_ou_payload() -> dict[str, Any]:
    return _payload("E-2-2-0-2.5-0", [1.9, 2.0], [5000, 4000])


def _basketball_ml_payload() -> dict[str, Any]:
    return _payload("E-3-1-0-0-0", [1.8, 2.1], [8000, 7500])


def _teams() -> EventTeams:
    return EventTeams(home="Real Madrid", away="Al Hilal", league="FIFA Club World Cup")


def _parse_soccer_1x2(
    payload: Mapping[str, Any], *, min_liquidity: float = 0.0
) -> list[OddsSnapshotIn]:
    return parse_betfair_feed(
        payload,
        market_key="1x2",
        default_bet_id=0,
        default_scope_id=0,
        home="Real Madrid",
        away="Al Hilal",
        event_id="https://op/match",
        min_liquidity=min_liquidity,
        now=NOW,
    )


# --------------------------------------------------------------------------- #
# parse_betfair_feed — the load-bearing pure parser (recon JSON shapes).
# --------------------------------------------------------------------------- #
def test_parse_soccer_1x2_three_way_dict() -> None:
    snaps = _parse_soccer_1x2(_soccer_1x2_payload())
    assert [s.selection for s in snaps] == ["Real Madrid", "Draw", "Al Hilal"]
    assert all(s.bookmaker == BOOKMAKER for s in snaps)
    assert all(s.market is Market.H2H for s in snaps)
    assert all(s.market_detail == "1x2" for s in snaps)
    assert [s.decimal_odds for s in snaps] == [2.12, 3.5, 4.0]
    assert [s.liquidity for s in snaps] == [9052.0, 3307.0, 1307.0]
    # captured_at = the feed's provider observation time (d.time-base), UTC-aware.
    assert all(s.captured_at == datetime.fromtimestamp(TIME_BASE, tz=UTC) for s in snaps)
    assert all(s.captured_at.tzinfo is not None for s in snaps)
    assert all(s.ingested_at == NOW for s in snaps)


def test_parse_soccer_over_under_two_way_list() -> None:
    snaps = parse_betfair_feed(
        _soccer_ou_payload(),
        market_key="over_under_2_5",
        default_bet_id=0,
        default_scope_id=0,
        home="Real Madrid",
        away="Al Hilal",
        event_id="https://op/match",
        min_liquidity=0.0,
        now=NOW,
    )
    assert [s.selection for s in snaps] == ["Over 2.5", "Under 2.5"]
    assert all(s.market is Market.TOTALS for s in snaps)
    assert all(s.market_detail == "over_under_2_5" for s in snaps)
    assert [s.decimal_odds for s in snaps] == [1.9, 2.0]
    assert [s.liquidity for s in snaps] == [5000.0, 4000.0]


def test_parse_basketball_home_away_two_way_list() -> None:
    snaps = parse_betfair_feed(
        _basketball_ml_payload(),
        market_key="home_away",
        default_bet_id=3,
        default_scope_id=1,
        home="Lakers",
        away="Celtics",
        event_id="https://op/bball",
        min_liquidity=0.0,
        now=NOW,
    )
    assert [s.selection for s in snaps] == ["Lakers", "Celtics"]
    assert all(s.market is Market.H2H for s in snaps)
    assert all(s.market_detail == "home_away" for s in snaps)
    assert [s.decimal_odds for s in snaps] == [1.8, 2.1]
    assert [s.liquidity for s in snaps] == [8000.0, 7500.0]
    # No Draw is ever produced for a 2-way market.
    assert "Draw" not in [s.selection for s in snaps]


def test_parse_empty_back_block_is_benign_gap() -> None:
    # An empty oddsdata/back block -> no rows, no crash (recover next cycle).
    assert _parse_soccer_1x2({"d": {"oddsdata": {"back": {}}}}) == []
    assert _parse_soccer_1x2({"d": {}}) == []
    assert _parse_soccer_1x2({}) == []


def test_parse_missing_feed_key_is_benign_gap() -> None:
    # The feed carries OTHER markets but not this one's key -> benign gap.
    payload = {"d": {"oddsdata": {"back": {"E-99-9-0-0-0": {"odds": {"44": {"0": 2.0}}}}}}}
    assert _parse_soccer_1x2(payload) == []


def test_parse_totals_feed_without_betfair_id_yields_no_row() -> None:
    # CONFIRMED NEGATIVE FINDING: basketball totals/handicap feeds carry the
    # block but NO Betfair id 44 (only ~8 books quote them). The Betfair-only
    # parser reads odds["44"], finds it absent, and yields NO row — never guesses.
    payload = _payload("E-3-1-0-0-0", odds44=None, volume44=None, extra_books={"16": [1.9, 1.9]})
    snaps = parse_betfair_feed(
        payload,
        market_key="home_away",
        default_bet_id=3,
        default_scope_id=1,
        home="Lakers",
        away="Celtics",
        event_id="https://op/bball",
        min_liquidity=0.0,
        now=NOW,
    )
    assert snaps == []


def test_parse_liquidity_gate_drops_thin_outcomes() -> None:
    # Floor between the draw (3307) and away (1307) matched volumes.
    snaps = _parse_soccer_1x2(_soccer_1x2_payload(), min_liquidity=2000.0)
    assert [s.selection for s in snaps] == ["Real Madrid", "Draw"]  # away gated out


def test_parse_liquidity_gate_admits_real_obscure_market() -> None:
    # REGRESSION parity with the DOM reader: real obscure-match Betfair volumes
    # are small (£12-£23); the default floor must admit them.
    from app.config import get_settings

    floor = get_settings().betfair_exchange_min_liquidity
    payload = _soccer_1x2_payload(
        odds={"0": 1.33, "1": 4.5, "2": 5.5}, volume={"0": 16, "1": 16, "2": 14}
    )
    snaps = _parse_soccer_1x2(payload, min_liquidity=floor)
    assert [s.selection for s in snaps] == ["Real Madrid", "Draw", "Al Hilal"]
    assert [s.liquidity for s in snaps] == [16.0, 16.0, 14.0]


def test_parse_absent_volume_drops_outcome() -> None:
    # The liquidity gate is the DOM reader's one unique guarantee: an outcome with
    # NO matched volume is dropped even at a 0 floor (never an ungated price).
    payload = _payload("E-1-2-0-0-0", {"0": 2.12, "1": 3.5, "2": 4.0}, volume44=None)
    assert _parse_soccer_1x2(payload, min_liquidity=0.0) == []


def test_parse_partial_volume_keeps_only_funded_outcomes() -> None:
    # Only the draw carries matched volume -> only the draw survives the gate.
    payload = _soccer_1x2_payload(volume={"1": 3307})
    snaps = _parse_soccer_1x2(payload, min_liquidity=0.0)
    assert [s.selection for s in snaps] == ["Draw"]


def test_parse_captured_at_falls_back_to_now_without_time_base() -> None:
    payload = _payload(
        "E-1-2-0-0-0",
        {"0": 2.12, "1": 3.5, "2": 4.0},
        {"0": 9052, "1": 3307, "2": 1307},
        time_base=None,
    )
    snaps = _parse_soccer_1x2(payload)
    assert all(s.captured_at == NOW for s in snaps)


def test_parse_skips_unbackable_price() -> None:
    # A price <= 1.0 is not a backable BACK price and is dropped (with its leg).
    payload = _soccer_1x2_payload(odds={"0": 1.0, "1": 3.5, "2": 4.0})
    snaps = _parse_soccer_1x2(payload)
    assert [s.selection for s in snaps] == ["Draw", "Al Hilal"]


def test_bookmaker_name_is_a_sharp_book() -> None:
    # The persisted name must normalize to the SHARP_BOOKS / EXCHANGE_COMMISSION
    # key so the edge engine + CLV anchor can ever recognise it.
    from app.edge.value import EXCHANGE_COMMISSION, SHARP_BOOKS

    assert BOOKMAKER.lower() in SHARP_BOOKS
    assert BOOKMAKER.lower() in EXCHANGE_COMMISSION


def test_feed_markets_per_sport() -> None:
    # The three FEASIBLE Betfair-present feeds: soccer 1x2 + OU 2.5, basketball ML.
    assert feed_markets_for_sport("soccer") == ("1x2", "over_under_2_5")
    assert feed_markets_for_sport("basketball") == ("home_away",)
    assert feed_markets_for_sport("tennis") == ()


def test_match_target_is_frozen() -> None:
    t = MatchTarget(event_id="x", url="x", teams=_teams())
    with pytest.raises(FrozenInstanceError):
        t.url = "y"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# BetfairExchangeReader — injected feed_loader, NO network / NO curl_cffi.
# --------------------------------------------------------------------------- #
def _feed_loader(
    feeds_by_sport: Mapping[str, Sequence[FeedFeasible]],
) -> Callable[..., Awaitable[Sequence[FeedFeasible]]]:
    async def loader(match_url: str, sport: str) -> Sequence[FeedFeasible]:  # noqa: ARG001
        return list(feeds_by_sport.get(sport, ()))

    return loader


def _target(url: str = "https://op/rma-hil") -> MatchTarget:
    return MatchTarget(event_id=url, url=url, teams=_teams())


@pytest.mark.asyncio
async def test_reader_reads_all_feasible_soccer_markets() -> None:
    loader = _feed_loader(
        {
            "soccer": (
                FeedFeasible("1x2", 0, 0, _soccer_1x2_payload()),
                FeedFeasible("over_under_2_5", 0, 0, _soccer_ou_payload()),
            )
        }
    )
    reader = BetfairExchangeReader(min_liquidity=0.0, feed_loader=loader)
    snaps = await reader.read_snapshots(_target(), sport="soccer", now=NOW)
    assert {s.market_detail for s in snaps} == {"1x2", "over_under_2_5"}
    assert {s.selection for s in snaps} == {
        "Real Madrid",
        "Draw",
        "Al Hilal",
        "Over 2.5",
        "Under 2.5",
    }
    assert all(s.bookmaker == BOOKMAKER for s in snaps)
    assert all(s.event_id == _target().event_id for s in snaps)


@pytest.mark.asyncio
async def test_reader_basketball_moneyline_two_way() -> None:
    loader = _feed_loader(
        {"basketball": (FeedFeasible("home_away", 3, 1, _basketball_ml_payload()),)}
    )
    reader = BetfairExchangeReader(min_liquidity=0.0, feed_loader=loader)
    target = MatchTarget(
        event_id="https://op/lal-bos",
        url="https://op/lal-bos",
        teams=EventTeams(home="Lakers", away="Celtics", league="NBA"),
    )
    snaps = await reader.read_snapshots(target, sport="basketball", now=NOW)
    assert [s.selection for s in snaps] == ["Lakers", "Celtics"]
    assert all(s.market is Market.H2H for s in snaps)
    assert "Draw" not in [s.selection for s in snaps]


@pytest.mark.asyncio
async def test_reader_empty_feeds_returns_empty() -> None:
    # No Betfair rows this cycle (loader yields nothing) is a benign gap.
    reader = BetfairExchangeReader(min_liquidity=0.0, feed_loader=_feed_loader({}))
    assert await reader.read_snapshots(_target(), sport="soccer", now=NOW) == []


# --------------------------------------------------------------------------- #
# BetfairExchangeCapture — price change-gate + sport gating (no DB).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_capture_change_gate_emits_once_then_on_move() -> None:
    state: dict[str, Any] = {"odds": {"0": 2.12, "1": 3.5, "2": 4.0}}

    async def loader(match_url: str, sport: str) -> Sequence[FeedFeasible]:  # noqa: ARG001
        return [FeedFeasible("1x2", 0, 0, _soccer_1x2_payload(odds=dict(state["odds"])))]

    reader = BetfairExchangeReader(min_liquidity=0.0, feed_loader=loader)
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
    snaps = await reader.read_snapshots(_target(), sport="soccer", now=NOW)
    assert capture._select_fresh("soccer", _target().event_id, snaps) == []
    # A price move on the home BACK leg re-opens that selection only.
    state["odds"]["0"] = 2.5
    moved_snaps = await reader.read_snapshots(_target(), sport="soccer", now=NOW)
    moved = capture._select_fresh("soccer", _target().event_id, moved_snaps)
    assert [s.selection for s in moved] == ["Real Madrid"]


@pytest.mark.asyncio
async def test_capture_skips_unsupported_sport() -> None:
    reader = BetfairExchangeReader(
        min_liquidity=0.0,
        feed_loader=_feed_loader({"soccer": (FeedFeasible("1x2", 0, 0, _soccer_1x2_payload()),)}),
    )
    capture = BetfairExchangeCapture(
        reader,
        session_factory=None,
        targets_fn=lambda sport: [_target()],
        sports=("tennis",),  # not in SPORT_SEGMENTS (soccer + basketball only)
        now_fn=lambda: NOW,
    )
    assert "tennis" not in SPORT_SEGMENTS
    assert await capture.capture_once() == {}  # unsupported sport skipped


@pytest.mark.asyncio
async def test_capture_supports_soccer_and_basketball() -> None:
    assert SPORT_SEGMENTS["soccer"] == "football"
    assert SPORT_SEGMENTS["basketball"] == "basketball"


@pytest.mark.asyncio
async def test_capture_accepts_async_targets_fn() -> None:
    calls = {"n": 0}

    async def async_targets(sport: str) -> list[MatchTarget]:
        calls["n"] += 1
        assert sport == "soccer"
        return [_target()]

    reader = BetfairExchangeReader(
        min_liquidity=0.0,
        feed_loader=_feed_loader({"soccer": (FeedFeasible("1x2", 0, 0, _soccer_1x2_payload()),)}),
    )
    capture = BetfairExchangeCapture(
        reader,
        session_factory=None,
        targets_fn=async_targets,
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    assert await capture.capture_once() == {"soccer": 0}
    assert calls["n"] == 1
    snaps = await reader.read_snapshots(_target(), sport="soccer", now=NOW)
    assert capture._select_fresh("soccer", _target().event_id, snaps) == []


@pytest.mark.asyncio
async def test_capture_none_reader_writes_nothing() -> None:
    capture = BetfairExchangeCapture(
        None, session_factory=None, targets_fn=lambda sport: [], sports=("soccer",)
    )
    assert await capture.capture_once() == {"soccer": 0}


@pytest.mark.asyncio
async def test_capture_isolates_a_failing_read() -> None:
    # A reader exception for one match is logged (type only) and skipped — never
    # aborts the cycle.
    async def boom(match_url: str, sport: str) -> Sequence[FeedFeasible]:  # noqa: ARG001
        raise RuntimeError("transport")

    reader = BetfairExchangeReader(min_liquidity=0.0, feed_loader=boom)
    capture = BetfairExchangeCapture(
        reader,
        session_factory=None,
        targets_fn=lambda sport: [_target()],
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    assert await capture.capture_once() == {"soccer": 0}


# --------------------------------------------------------------------------- #
# DB integration: rows attach INLINE onto the canonical event (ADR-0015 v2) as
# bookmaker "Betfair Exchange" with NUMERIC liquidity. Skipped without Postgres.
# Isolation: one transaction on one connection, rolled back at teardown.
# --------------------------------------------------------------------------- #
@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:  # noqa: BLE001
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


def _soccer_reader() -> BetfairExchangeReader:
    return BetfairExchangeReader(
        min_liquidity=0.0,
        feed_loader=_feed_loader({"soccer": (FeedFeasible("1x2", 0, 0, _soccer_1x2_payload()),)}),
    )


async def test_capture_persists_inline_onto_canonical_event(factory) -> None:  # type: ignore[no-untyped-def]
    url = f"https://op/betfair-iso-{uuid4()}"
    # The MAIN scrape creates the canonical event first; Betfair attaches inline.
    await persist_odds_snapshots(
        factory,
        [
            OddsSnapshotIn(
                event_id=url,
                bookmaker="bet365",
                market=Market.H2H,
                selection="Real Madrid",
                decimal_odds=2.0,
                captured_at=NOW,
                ingested_at=NOW,
            )
        ],
        {url: _teams()},
        sport="soccer",
        default_league="Club WC",
    )

    capture = BetfairExchangeCapture(
        _soccer_reader(),
        session_factory=factory,
        targets_fn=lambda sport: [_target(url)],
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    written = await capture.capture_once()
    assert written["soccer"] == 3  # home/draw/away BACK rows
    async with factory() as session:
        rows = (
            await session.execute(
                select(OddsSnapshot.bookmaker, OddsSnapshot.selection, OddsSnapshot.liquidity)
                .join(Event, Event.id == OddsSnapshot.event_id)
                .where(Event.external_ref == url, OddsSnapshot.bookmaker == BOOKMAKER)
            )
        ).all()
        assert len(rows) == 3
        assert {r.bookmaker for r in rows} == {BOOKMAKER}
        assert {r.selection for r in rows} == {"Real Madrid", "Draw", "Al Hilal"}
        assert all(r.liquidity is not None for r in rows)  # NUMERIC liquidity persisted
        event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.external_ref == url)
        )
        assert event_count == 1  # exactly one canonical event; capture minted none
        legacy = await session.scalar(
            select(func.count()).select_from(Event).where(Event.external_ref == f"betfair:{url}")
        )
        assert legacy == 0
    # Re-capture with the SAME prices writes nothing new (change-gate + append-only key).
    assert (await capture.capture_once())["soccer"] == 0


async def test_capture_does_not_create_event_when_canonical_absent(factory) -> None:  # type: ignore[no-untyped-def]
    url = f"https://op/betfair-noevent-{uuid4()}"
    capture = BetfairExchangeCapture(
        _soccer_reader(),
        session_factory=factory,
        targets_fn=lambda sport: [_target(url)],
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    written = await capture.capture_once()
    assert written["soccer"] == 0  # nothing written: no canonical event to attach to
    async with factory() as session:
        canonical = await session.scalar(
            select(func.count()).select_from(Event).where(Event.external_ref == url)
        )
        snaps = await session.scalar(
            select(func.count())
            .select_from(OddsSnapshot)
            .join(Event, Event.id == OddsSnapshot.event_id)
            .where(Event.external_ref == url)
        )
        assert canonical == 0
        assert snaps == 0


async def test_db_tests_leave_no_committed_pollution() -> None:
    probe_engine = create_async_engine(DB_URL)
    try:
        async with probe_engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:  # noqa: BLE001
        await probe_engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")

    url = f"https://op/betfair-leak-{uuid4()}"
    soft = OddsSnapshotIn(
        event_id=url,
        bookmaker="bet365",
        market=Market.H2H,
        selection="Real Madrid",
        decimal_odds=2.0,
        captured_at=NOW,
        ingested_at=NOW,
    )

    inner_engine = create_async_engine(DB_URL)
    try:
        async with inner_engine.connect() as conn:
            trans = await conn.begin()
            maker = async_sessionmaker(
                bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            await persist_odds_snapshots(
                maker, [soft], {url: _teams()}, sport="soccer", default_league="Club WC"
            )
            capture = BetfairExchangeCapture(
                _soccer_reader(),
                session_factory=maker,
                targets_fn=lambda sport: [_target(url)],
                sports=("soccer",),
                now_fn=lambda: NOW,
            )
            assert (await capture.capture_once())["soccer"] == 3
            async with maker() as session:
                seen = await session.scalar(
                    select(func.count())
                    .select_from(OddsSnapshot)
                    .join(Event, Event.id == OddsSnapshot.event_id)
                    .where(Event.external_ref == url, OddsSnapshot.bookmaker == BOOKMAKER)
                )
                assert seen == 3
            await trans.rollback()
    finally:
        await inner_engine.dispose()

    try:
        async with async_sessionmaker(probe_engine, expire_on_commit=False)() as session:
            leftover_events = await session.scalar(
                select(func.count()).select_from(Event).where(Event.external_ref == url)
            )
            leftover_snaps = await session.scalar(
                select(func.count())
                .select_from(OddsSnapshot)
                .join(Event, Event.id == OddsSnapshot.event_id)
                .where(Event.external_ref == url)
            )
            assert leftover_events == 0
            assert leftover_snaps == 0
    finally:
        await probe_engine.dispose()
