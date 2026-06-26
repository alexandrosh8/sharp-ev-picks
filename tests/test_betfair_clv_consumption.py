"""Gated Betfair Exchange BACK close consumption for closing CLV (ADR-0015).

CLV_USE_BETFAIR_EXCHANGE (default OFF) lets the captured Betfair Exchange BACK
close anchor a pick's closing fair, mirroring CLV_USE_PINNACLE_ARCHIVE but with
EXACT resolution: the betfair event's external_ref is deterministically
"betfair:"+pick_ref (app/ingestion/betfair_exchange._namespace_event_ref), so the
lookup is a single unique-key match — no alias table, no kickoff fuzz.

Proves: (a) _betfair_exchange_close returns the betfair BACK snaps when a
betfair:<ref> event with a close exists, [] when absent; (b) finalize_closing
with use_betfair_exchange=True injects them and the closing fair/CLV anchors on
the commission-netted Betfair price, vs =False leaving behaviour unchanged;
(c) the no-betfair-event case changes nothing. Compose-Postgres fixture (:5433);
skips when the DB is absent. No live network.
"""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clv_trueup import _betfair_exchange_close, finalize_closing_from_snapshots
from app.ingestion.base import EventTeams
from app.probabilities.devig import DevigMethod, devig
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import OddsSnapshot, Pick
from app.storage.repositories import persist_odds_snapshots, persist_pick

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
KICKOFF = NOW - timedelta(hours=6)
HOME = "Betfair Alpha"
AWAY = "Betfair Beta"
LEAGUE = "test-league-betfair"

# A 1X2 BACK close set. The Betfair Exchange row is a SHARP_BOOK with 5%
# commission, so its odds are netted before devig (and the fill is netted too).
BETFAIR_CLOSE = (2.20, 3.40, 3.30)  # home, draw, away
# A softbook-only close set: one non-sharp book is neither a named anchor nor a
# >=3-book consensus, so it yields NO anchorable fair on its own.
SOFTBOOK_CLOSE = (2.30, 3.35, 3.20)


def make_pick(event_id: str, bookmaker: str = "SoftBook", decimal_odds: float = 2.50) -> PickOut:
    return PickOut(
        pick_id="p-betfair-clv",
        sport="soccer",
        league=LEAGUE,
        event=f"{HOME} vs {AWAY}",
        event_id=event_id,
        market=Market.H2H,
        selection=HOME,
        bookmaker=bookmaker,
        decimal_odds=decimal_odds,
        model_probability=0.45,
        fair_probability=0.40,
        edge=0.05,
        ev=0.125,
        confidence=0.9,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary="betfair clv consumption test",
        created_at=NOW - timedelta(days=3),
    )


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    # Bind a sessionmaker to ONE connection inside a transaction rolled back at
    # the end, so persist_pick/finalize (which take a session) and
    # persist_odds_snapshots (which takes a factory) all share the same isolated
    # transaction — exactly the tests/test_resolution_db.py pattern.
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


async def seed_pick(maker, event_id: str, **kwargs) -> int:  # type: ignore[no-untyped-def]
    """Create the LIVE pick + its event (external_ref == event_id); return its
    id. Commits so it is visible to later sessions on the shared connection (the
    fixture's outer transaction is rolled back at teardown)."""
    teams = EventTeams(home=HOME, away=AWAY, league=LEAGUE, starts_at=KICKOFF)
    async with maker() as session:
        assert await persist_pick(session, make_pick(event_id, **kwargs), teams, "value", "test-v")
        pick = await session.scalar(
            select(Pick)
            .where(Pick.reason_summary == "betfair clv consumption test")
            .order_by(Pick.id.desc())
        )
        assert pick is not None
        pick_id = pick.id
        await session.commit()
        return pick_id


def _betfair_snaps(event_ref: str, odds: tuple[float, float, float]) -> list[OddsSnapshotIn]:
    """One full 1X2 Betfair Exchange BACK observation, shaped exactly as
    app/ingestion/betfair_exchange.back_quotes_to_snapshots emits it: H2H,
    detail-less, bookmaker="Betfair Exchange", captured pre-kickoff."""
    captured = KICKOFF - timedelta(hours=1)
    snaps: list[OddsSnapshotIn] = []
    for sel, o in zip((HOME, "Draw", AWAY), odds, strict=True):
        snaps.append(
            OddsSnapshotIn(
                event_id=event_ref,
                bookmaker="Betfair Exchange",
                market=Market.H2H,
                selection=sel,
                decimal_odds=o,
                liquidity=5000.0,
                captured_at=captured,
                ingested_at=captured,
            )
        )
    return snaps


async def seed_betfair_event(  # type: ignore[no-untyped-def]
    maker, pick_ref: str, odds: tuple[float, float, float] = BETFAIR_CLOSE
) -> None:
    """Create the ISOLATED betfair:<ref> event with Betfair Exchange BACK snaps,
    exactly as BetfairExchangeCapture persists it (namespaced external_ref)."""
    betfair_ref = f"betfair:{pick_ref}"
    snaps = _betfair_snaps(betfair_ref, odds)
    teams = {
        betfair_ref: EventTeams(home=HOME, away=AWAY, league="betfair_soccer", starts_at=KICKOFF)
    }
    written = await persist_odds_snapshots(maker, snaps, teams, "betfair_soccer", "betfair_soccer")
    assert written == 3


async def seed_soft_close(session: AsyncSession, pick: Pick) -> None:
    """The pick's OWN event carries only a single soft book (not anchorable):
    so only an injected Betfair sharp close can produce a fair."""
    captured = KICKOFF - timedelta(hours=1)
    for sel, o in zip((HOME, "Draw", AWAY), SOFTBOOK_CLOSE, strict=True):
        session.add(
            OddsSnapshot(
                event_id=pick.event_id,
                bookmaker="SoftBook",
                market="1x2",
                selection=sel,
                decimal_odds=Decimal(str(o)),
                captured_at=captured,
                ingested_at=captured,
            )
        )
    await session.flush()


# --- the resolver helper (EXACT match) ----------------------------------------


async def test_betfair_close_returns_back_snaps_when_event_exists(factory) -> None:  # type: ignore[no-untyped-def]
    ref = "evt-betfair-present"
    pick_id = await seed_pick(factory, ref)
    await seed_betfair_event(factory, ref)
    async with factory() as session:
        pick = await session.get(Pick, pick_id)
        assert pick is not None
        snaps = await _betfair_exchange_close(session, pick, ref, KICKOFF)
    assert len(snaps) == 3
    # Re-keyed to the PICK's external_ref (the "betfair:" prefix stripped) so the
    # close groups with the pick's own market, not a namespaced one.
    assert {s.event_id for s in snaps} == {ref}
    assert all(s.bookmaker == "Betfair Exchange" for s in snaps)
    assert all(s.market is Market.H2H for s in snaps)
    by_sel = {s.selection: s.decimal_odds for s in snaps}
    assert by_sel[HOME] == pytest.approx(2.20)
    assert by_sel["Draw"] == pytest.approx(3.40)
    assert by_sel[AWAY] == pytest.approx(3.30)


async def test_betfair_close_returns_empty_when_no_event(factory) -> None:  # type: ignore[no-untyped-def]
    ref = "evt-betfair-absent"
    pick_id = await seed_pick(factory, ref)  # NO betfair:<ref> event seeded
    async with factory() as session:
        pick = await session.get(Pick, pick_id)
        assert pick is not None
        snaps = await _betfair_exchange_close(session, pick, ref, KICKOFF)
    assert snaps == []


# --- finalize_closing_from_snapshots gating -----------------------------------


async def test_flag_on_anchors_on_betfair_close(factory) -> None:  # type: ignore[no-untyped-def]
    # The pick's own event has near-kickoff coverage (a single soft book) that
    # PASSES the scrape-coverage gate but is NOT anchorable on its own. With the
    # flag ON, the injected Betfair sharp close supplies the anchor: the closing
    # fair is the commission-netted Betfair devig. Same effective-odds netting
    # as the live path — the close anchor is netted before devig.
    ref = "evt-betfair-on"
    pick_id = await seed_pick(factory, ref)
    await seed_betfair_event(factory, ref)
    async with factory() as session:
        pick = await session.get(Pick, pick_id)
        assert pick is not None
        await seed_soft_close(session, pick)  # coverage so the gate passes
        applied = await finalize_closing_from_snapshots(
            session, pick, ref, KICKOFF, DevigMethod.SHIN, use_betfair_exchange=True
        )
        assert applied is True
        assert pick.closing_fair_probability is not None
        assert pick.clv_log is not None
        # 5% commission netted on the Betfair anchor BEFORE devig.
        eff_close = tuple(1.0 + (o - 1.0) * 0.95 for o in BETFAIR_CLOSE)
        fair = devig(eff_close, method=DevigMethod.SHIN)[0]
        assert float(pick.closing_fair_probability) == pytest.approx(fair, abs=1e-6)
        # The fair is NOT the soft-book's own devig — proves the Betfair sharp
        # close (not the soft 1x2 group) anchored.
        soft_fair = devig(SOFTBOOK_CLOSE, method=DevigMethod.SHIN)[0]
        assert abs(float(pick.closing_fair_probability) - soft_fair) > 1e-4
        # Fill is a commission-free book (SoftBook), so the fill side is raw 2.50.
        assert float(pick.clv_log) == pytest.approx(math.log(2.50 * fair), abs=1e-5)
        # closing_odds is the pick's OWN book's close-row price (SoftBook, 2.30) —
        # provenance marker that a snapshot-sourced close was written.
        assert pick.closing_odds == Decimal("2.3000")


async def test_flag_off_leaves_behaviour_unchanged(factory) -> None:  # type: ignore[no-untyped-def]
    # SAME seed as the flag-ON case, but use_betfair_exchange=False: the betfair
    # close is NOT injected, the pick's own event has no anchorable close, so
    # finalize falls back (returns False) and writes nothing.
    ref = "evt-betfair-off"
    pick_id = await seed_pick(factory, ref)
    await seed_betfair_event(factory, ref)
    async with factory() as session:
        pick = await session.get(Pick, pick_id)
        assert pick is not None
        await seed_soft_close(session, pick)
        applied = await finalize_closing_from_snapshots(
            session, pick, ref, KICKOFF, DevigMethod.SHIN, use_betfair_exchange=False
        )
        assert applied is False
        # Default OFF -> no behaviour change: nothing was written.
        assert pick.closing_fair_probability is None
        assert pick.clv_log is None
        assert pick.closing_odds is None


async def test_flag_on_but_no_betfair_event_is_neutral(factory) -> None:  # type: ignore[no-untyped-def]
    # Flag ON but NO betfair:<ref> event captured: _betfair_exchange_close -> []
    # -> nothing injected -> same outcome as flag OFF (the pick's own soft-only
    # close is unanchorable, so finalize falls back).
    ref = "evt-betfair-on-nomatch"
    pick_id = await seed_pick(factory, ref)  # NO betfair event
    async with factory() as session:
        pick = await session.get(Pick, pick_id)
        assert pick is not None
        await seed_soft_close(session, pick)
        applied = await finalize_closing_from_snapshots(
            session, pick, ref, KICKOFF, DevigMethod.SHIN, use_betfair_exchange=True
        )
        assert applied is False
        assert pick.closing_fair_probability is None
        assert pick.clv_log is None
        assert pick.closing_odds is None


# --- basketball: the EXACT bridge is sport-agnostic (2-way moneyline) ----------

# A 2-way basketball BACK close (home/away, NO draw). Betfair Exchange nets 5%
# commission before devig (and the fill is netted too).
BETFAIR_BBALL_CLOSE = (1.80, 2.10)  # home, away
SOFT_BBALL_CLOSE = (1.85, 2.05)


def _betfair_bball_snaps(event_ref: str, odds: tuple[float, float]) -> list[OddsSnapshotIn]:
    """One full 2-way basketball Betfair Exchange BACK observation (home/away),
    exactly as back_quotes_to_snapshots emits it for a 2-way sport: H2H,
    detail-less, NO "Draw" selection, captured pre-kickoff."""
    captured = KICKOFF - timedelta(hours=1)
    return [
        OddsSnapshotIn(
            event_id=event_ref,
            bookmaker="Betfair Exchange",
            market=Market.H2H,
            selection=sel,
            decimal_odds=o,
            liquidity=5000.0,
            captured_at=captured,
            ingested_at=captured,
        )
        for sel, o in zip((HOME, AWAY), odds, strict=True)
    ]


async def test_flag_on_anchors_on_betfair_basketball_two_way_close(factory) -> None:  # type: ignore[no-untyped-def]
    # The EXACT betfair:<ref> bridge is sport-agnostic: a basketball pick whose
    # captured Betfair close is a 2-way (home/away) market anchors the closing
    # fair on the commission-netted Betfair devig — no "Draw" leg required. This
    # proves _betfair_exchange_close + event_fair_probs handle a 2-way H2H close.
    ref = "evt-betfair-bball"
    pick_id = await seed_pick(factory, ref)  # SoftBook pick on HOME @ 2.50
    # Seed the 2-way betfair event (home/away) under the namespaced ref.
    betfair_ref = f"betfair:{ref}"
    teams = {
        betfair_ref: EventTeams(
            home=HOME, away=AWAY, league="betfair_basketball", starts_at=KICKOFF
        )
    }
    written = await persist_odds_snapshots(
        factory,
        _betfair_bball_snaps(betfair_ref, BETFAIR_BBALL_CLOSE),
        teams,
        "betfair_basketball",
        "betfair_basketball",
    )
    assert written == 2  # home/away only, NO draw
    async with factory() as session:
        pick = await session.get(Pick, pick_id)
        assert pick is not None
        # A 2-way soft close on the pick's own event: passes the coverage gate.
        captured = KICKOFF - timedelta(hours=1)
        for sel, o in zip((HOME, AWAY), SOFT_BBALL_CLOSE, strict=True):
            session.add(
                OddsSnapshot(
                    event_id=pick.event_id,
                    bookmaker="SoftBook",
                    market="1x2",
                    selection=sel,
                    decimal_odds=Decimal(str(o)),
                    captured_at=captured,
                    ingested_at=captured,
                )
            )
        await session.flush()
        applied = await finalize_closing_from_snapshots(
            session, pick, ref, KICKOFF, DevigMethod.SHIN, use_betfair_exchange=True
        )
        assert applied is True
        # 5% commission netted on the 2-way Betfair anchor BEFORE devig.
        eff_close = tuple(1.0 + (o - 1.0) * 0.95 for o in BETFAIR_BBALL_CLOSE)
        fair = devig(eff_close, method=DevigMethod.SHIN)[0]
        assert pick.closing_fair_probability is not None
        assert float(pick.closing_fair_probability) == pytest.approx(fair, abs=1e-6)
        # Fill is commission-free SoftBook (raw 2.50).
        assert float(pick.clv_log) == pytest.approx(math.log(2.50 * fair), abs=1e-5)


async def test_fresh_sharp_close_rescues_coverage_when_soft_scrape_dropped(factory) -> None:  # type: ignore[no-untyped-def]
    # clv-2: the pick's own event FELL OUT of the soft OddsPortal scrape (no soft
    # close snapshot at all), so the soft-coverage gate is NOT satisfied. But a
    # FRESH matched Betfair sharp close exists. Coverage must be satisfied by the
    # sharp side alone — finalize anchors on the Betfair close and returns True
    # (the OLD gate returned False before ever resolving the sharp archive).
    ref = "evt-sharp-rescues"
    pick_id = await seed_pick(factory, ref)
    await seed_betfair_event(factory, ref)  # fresh Betfair close, NO soft close seeded
    async with factory() as session:
        pick = await session.get(Pick, pick_id)
        assert pick is not None
        applied = await finalize_closing_from_snapshots(
            session, pick, ref, KICKOFF, DevigMethod.SHIN, use_betfair_exchange=True
        )
        assert applied is True  # sharp close alone satisfied coverage
        eff_close = tuple(1.0 + (o - 1.0) * 0.95 for o in BETFAIR_CLOSE)
        fair = devig(eff_close, method=DevigMethod.SHIN)[0]
        assert pick.closing_fair_probability is not None
        assert float(pick.closing_fair_probability) == pytest.approx(fair, abs=1e-6)
        # A genuine snapshot close was anchored even though NO soft book quoted it,
        # so closing_odds (the soft display price) stays NULL while the trusted-CLV
        # marker is set — exactly the clv-1 sharp-only-close case.
        assert pick.has_snapshot_close is True
        assert pick.closing_odds is None
        assert pick.closing_anchor_type == "sharp"  # Betfair Exchange -> sharp
        assert pick.close_independent_of_fill is True  # Betfair close != SoftBook fill


async def test_loader_drops_stale_pinnacle_but_keeps_fresh_betfair_per_source(factory) -> None:  # type: ignore[no-untyped-def]
    # temporal-leakage-1: with a FRESH Betfair capture and a STALE Pinnacle capture
    # for the SAME event, the per-source freshness gate must drop ONLY the stale
    # Pinnacle rows. The old event-wide max(captured_at) clock would let the fresh
    # Betfair row vouch for the stale Pinnacle line — temporal leakage that anchors
    # the live pick on an outdated sharp price.
    from app.clv_trueup import build_sharp_anchor_loader
    from app.ingestion.base import EventDirectory

    now = datetime.now(UTC)
    kickoff = now + timedelta(hours=2)
    ref = "evt-mixed-freshness"
    home, away = "Mixed Home FC", "Mixed Away FC"

    # FRESH Betfair close (1h old) under the namespaced betfair:<ref> event.
    bref = f"betfair:{ref}"
    fresh = now - timedelta(hours=1)
    betfair_snaps = [
        OddsSnapshotIn(
            event_id=bref,
            bookmaker="Betfair Exchange",
            market=Market.H2H,
            selection=sel,
            decimal_odds=o,
            captured_at=fresh,
            ingested_at=fresh,
        )
        for sel, o in ((home, 2.40), ("Draw", 3.50), (away, 3.20))
    ]
    await persist_odds_snapshots(
        factory,
        betfair_snaps,
        {bref: EventTeams(home=home, away=away, starts_at=kickoff)},
        "betfair_soccer",
        "betfair_soccer",
    )
    # STALE Pinnacle archive (10h old) under pinnacle_soccer, EXACT team names so the
    # strict matcher resolves it without relying on the alias table.
    stale = now - timedelta(hours=10)
    pinnacle_snaps = [
        OddsSnapshotIn(
            event_id=ref,
            bookmaker="Pinnacle",
            market=Market.H2H,
            selection=sel,
            decimal_odds=o,
            captured_at=stale,
            ingested_at=stale,
        )
        for sel, o in ((home, 2.45), ("Draw", 3.45), (away, 3.15))
    ]
    await persist_odds_snapshots(
        factory,
        pinnacle_snaps,
        {ref: EventTeams(home=home, away=away, starts_at=kickoff)},
        "pinnacle_soccer",
        "pinnacle_soccer",
    )

    directory = EventDirectory()
    directory.register(ref, EventTeams(home=home, away=away, starts_at=kickoff))
    loader = build_sharp_anchor_loader(
        factory, directory, use_betfair=True, use_pinnacle=True, max_age_seconds=14400.0
    )
    scrape = [
        OddsSnapshotIn(
            event_id=ref,
            bookmaker="SoftBook",
            market=Market.H2H,
            selection=home,
            decimal_odds=2.90,
            captured_at=now,
            ingested_at=now,
        )
    ]
    out = await loader("soccer", scrape)
    books = {s.bookmaker for s in out}
    assert "Betfair Exchange" in books  # fresh source kept
    assert "Pinnacle" not in books  # stale source dropped per-source (no leakage)


async def test_sharp_anchor_loader_event_wide_freshness(factory) -> None:  # type: ignore[no-untyped-def]
    # REGRESSION (review 2026-06-21): the pick-time sharp-anchor freshness gate is
    # EVENT-WIDE — a recently-captured event keeps its Betfair anchor; an event
    # that FELL OUT of capture (most-recent row older than max_age) is dropped.
    # Per-event, NOT per-row (change-only persistence: a steady price's row may be
    # old yet still current — it must NOT be dropped).
    from app.clv_trueup import build_sharp_anchor_loader
    from app.ingestion.base import EventDirectory

    now = datetime.now(UTC)
    kickoff = now + timedelta(hours=2)

    async def seed(ref: str, captured: datetime) -> None:
        bref = f"betfair:{ref}"
        snaps = [
            OddsSnapshotIn(
                event_id=bref,
                bookmaker="Betfair Exchange",
                market=Market.H2H,
                selection=sel,
                decimal_odds=o,
                captured_at=captured,
                ingested_at=captured,
            )
            for sel, o in (("Home FC", 2.40), ("Draw", 3.50), ("Away FC", 3.20))
        ]
        teams = {bref: EventTeams(home="Home FC", away="Away FC", starts_at=kickoff)}
        await persist_odds_snapshots(factory, snaps, teams, "betfair_soccer", "betfair_soccer")

    await seed("evt-fresh", now - timedelta(hours=1))  # still being captured
    await seed("evt-stale", now - timedelta(hours=10))  # fell out of capture

    directory = EventDirectory()
    for ref in ("evt-fresh", "evt-stale"):
        directory.register(ref, EventTeams(home="Home FC", away="Away FC", starts_at=kickoff))

    loader = build_sharp_anchor_loader(
        factory, directory, use_betfair=True, use_pinnacle=False, max_age_seconds=14400.0
    )
    scrape = [
        OddsSnapshotIn(
            event_id=ref,
            bookmaker="SoftBook",
            market=Market.H2H,
            selection="Home FC",
            decimal_odds=2.90,
            captured_at=now,
            ingested_at=now,
        )
        for ref in ("evt-fresh", "evt-stale")
    ]
    out = await loader("soccer", scrape)
    refs = {s.event_id for s in out}
    assert "evt-fresh" in refs  # fresh -> anchor kept
    assert "evt-stale" not in refs  # fell out of capture -> dropped
