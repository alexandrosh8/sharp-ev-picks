"""Closing-line capture from our OWN odds_snapshots (change-only history).

At settlement, the closing fair/CLV is recomputed from the warehouse's own
odds_snapshots rows when scrape coverage exists — preferring them over the
last match-page re-scrape write — and falls back to that re-scrape close
otherwise. Compose-Postgres fixture patterns from tests/test_clv_trueup.py
and tests/test_settlement_engine.py (skips when the DB is absent).
"""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clv_trueup import SNAPSHOT_CLOSE_MAX_GAP, finalize_closing_from_snapshots
from app.ingestion.base import EventTeams
from app.probabilities.devig import DevigMethod, devig
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.settlement.engine import settle_event_picks, settle_open_picks
from app.settlement.results import FinalScore, ScoreBook
from app.storage.models import Event, OddsSnapshot, Pick
from app.storage.repositories import (
    closing_odds_from_snapshots,
    market_from_snapshot_key,
    persist_pick,
)

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
KICKOFF = NOW - timedelta(hours=6)
HOME = "Snapclose Alpha"
AWAY = "Snapclose Beta"

# The 1X2 close book set. Pinnacle is the named-sharp anchor; its devigged
# (vig-free) probabilities ARE the closing fair, per the live-path rules.
PINNACLE_CLOSE = (2.20, 3.40, 3.30)
SOFTBOOK_CLOSE = (2.30, 3.35, 3.20)


def make_pick(event_id: str, bookmaker: str = "SoftBook", decimal_odds: float = 2.50) -> PickOut:
    return PickOut(
        pick_id="p-snapclose",
        sport="soccer",
        league="test-league-snapclose",
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
        reason_summary="snapshot close test",
        created_at=NOW - timedelta(days=3),
    )


def score_book(hs: int = 2, as_: int = 1) -> ScoreBook:
    return ScoreBook(
        [
            FinalScore(
                home_team=HOME,
                away_team=AWAY,
                match_date=KICKOFF.date(),
                home_score=hs,
                away_score=as_,
            )
        ]
    )


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.begin()
        try:
            yield s
        finally:
            await s.rollback()
    await engine.dispose()


async def seed_pick(session: AsyncSession, event_id: str, **kwargs) -> Pick:  # type: ignore[no-untyped-def]
    teams = EventTeams(home=HOME, away=AWAY, league="test-league-snapclose", starts_at=KICKOFF)
    assert await persist_pick(session, make_pick(event_id, **kwargs), teams, "value", "test-v")
    pick = await session.scalar(
        select(Pick).where(Pick.reason_summary == "snapshot close test").order_by(Pick.id.desc())
    )
    assert pick is not None
    return pick


async def seed_1x2_snaps(
    session: AsyncSession,
    db_event_id: int,
    book: str,
    odds: tuple[float, float, float],
    captured_at: datetime,
) -> None:
    """One full 1X2 observation, stored exactly as the pipeline persists it:
    odds_snapshots.market is the PROVIDER submarket key ('1x2'), not the
    Market enum value — the read path must map it back."""
    for sel, o in zip((HOME, "Draw", AWAY), odds, strict=True):
        session.add(
            OddsSnapshot(
                event_id=db_event_id,
                bookmaker=book,
                market="1x2",
                selection=sel,
                decimal_odds=Decimal(str(o)),
                captured_at=captured_at,
                ingested_at=captured_at,
            )
        )
    await session.flush()


async def preset_rescrape_close(session: AsyncSession, pick: Pick) -> None:
    """Simulate what the live/match-page re-scrape path last wrote: the
    de-facto close every pick carries before snapshot history existed."""
    pick.closing_fair_probability = Decimal("0.400000")
    pick.clv_log = Decimal("0.000000")
    pick.beat_close = False
    await session.flush()


async def event_ref_of(session: AsyncSession, pick: Pick) -> str:
    ref = await session.scalar(select(Event.external_ref).where(Event.id == pick.event_id))
    assert ref is not None
    return ref


# --- the mapping helper (pure) ------------------------------------------------


def test_market_from_snapshot_key_roundtrip() -> None:
    # Enum values were stored detail-less; provider keys map through the
    # loader's table and KEEP the key as market_detail (distinct lines must
    # rebuild as distinct devig groups). Unknown keys: None, never a guess.
    assert market_from_snapshot_key("h2h") == (Market.H2H, None)
    assert market_from_snapshot_key("totals") == (Market.TOTALS, None)
    assert market_from_snapshot_key("1x2") == (Market.H2H, "1x2")
    assert market_from_snapshot_key("home_away") == (Market.H2H, "home_away")
    assert market_from_snapshot_key("over_under_2_5") == (Market.TOTALS, "over_under_2_5")
    assert market_from_snapshot_key("asian_handicap_-1_5") == (
        Market.SPREADS,
        "asian_handicap_-1_5",
    )
    assert market_from_snapshot_key("frobnicate") is None


# --- snapshot close preferred when coverage is good ---------------------------


async def test_settlement_prefers_snapshot_close_when_coverage_good(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-snapclose-good")
    await preset_rescrape_close(session, pick)
    # Early observation (line moved later) must NOT win; the close set at
    # KICKOFF-1h must; the post-kickoff row must be EXCLUDED from the close.
    early, close, post = (
        KICKOFF - timedelta(hours=30),
        KICKOFF - timedelta(hours=1),
        KICKOFF + timedelta(minutes=10),
    )
    await seed_1x2_snaps(session, pick.event_id, "Pinnacle", (2.60, 3.20, 3.00), early)
    await seed_1x2_snaps(session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, close)
    await seed_1x2_snaps(session, pick.event_id, "SoftBook", SOFTBOOK_CLOSE, close)
    await seed_1x2_snaps(session, pick.event_id, "Pinnacle", (2.05, 3.60, 3.50), post)

    settled = await settle_open_picks(session, score_book(2, 1), NOW, devig_method=DevigMethod.SHIN)
    assert settled == 1
    await session.refresh(pick)
    assert pick.status == "settled"
    assert pick.closing_fair_probability is not None
    assert pick.clv_log is not None
    fair = devig(PINNACLE_CLOSE, method=DevigMethod.SHIN)[0]  # ~0.4359962
    assert float(pick.closing_fair_probability) == pytest.approx(fair, abs=1e-6)
    assert float(pick.clv_log) == pytest.approx(math.log(2.50 * fair), abs=1e-5)
    assert pick.beat_close is True
    # Provenance: closing_odds is written ONLY by the snapshot path — the
    # pick's own book's close-row price.
    assert pick.closing_odds == Decimal("2.3000")
    # Close-anchor provenance: the close was anchored by Pinnacle (a named sharp
    # book pricing the full market) — so this is a genuine sharp close.
    assert pick.closing_anchor_type == "pinnacle"


async def test_snapshot_close_stamps_independence_when_close_book_differs_from_fill(  # type: ignore[no-untyped-def]
    session,
) -> None:
    # P0-1/P0-3 WRITE side: a pick FILLED at a SOFT book whose close is anchored
    # by a DIFFERENT (sharp) book is a GENUINE, independent close — the close was
    # NOT priced by the pick's own fill book. close_independent_of_fill -> True.
    pick = await seed_pick(session, "evt-snapclose-indep", bookmaker="SoftBook")
    close = KICKOFF - timedelta(hours=1)
    await seed_1x2_snaps(session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, close)
    await seed_1x2_snaps(session, pick.event_id, "SoftBook", SOFTBOOK_CLOSE, close)
    ref = await event_ref_of(session, pick)

    assert await finalize_closing_from_snapshots(session, pick, ref, KICKOFF, DevigMethod.SHIN)
    assert pick.closing_anchor_type == "pinnacle"  # close anchored by a sharp book
    assert pick.close_independent_of_fill is True  # Pinnacle close != SoftBook fill


async def test_snapshot_close_flags_circular_close_anchored_by_fill_book(session) -> None:  # type: ignore[no-untyped-def]
    # P0-1/P0-3 WRITE side, the CORE bug: a pick FILLED at Pinnacle whose close is
    # ALSO anchored by Pinnacle is CIRCULAR — the pick's own book pricing its own
    # close. Even though the anchor TYPE is "pinnacle" (sharp), it must be flagged
    # NOT independent so the trusted sharp-CLV subset excludes it. This is the
    # fake CLV that masked the -EV.
    pick = await seed_pick(session, "evt-snapclose-circular", bookmaker="Pinnacle")
    close = KICKOFF - timedelta(hours=1)
    await seed_1x2_snaps(session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, close)
    ref = await event_ref_of(session, pick)

    assert await finalize_closing_from_snapshots(session, pick, ref, KICKOFF, DevigMethod.SHIN)
    assert pick.closing_anchor_type == "pinnacle"  # type alone would call it sharp
    assert pick.close_independent_of_fill is False  # ...but it is CIRCULAR


async def test_manual_event_settle_finalizes_snapshot_close(session) -> None:  # type: ignore[no-untyped-def]
    # audit #4: the MANUAL event-settle path must finalize the snapshot close too,
    # else a manually-settled pick never enters the sharp-CLV subset.
    pick = await seed_pick(session, "evt-snapclose-manual")
    await preset_rescrape_close(session, pick)
    close = KICKOFF - timedelta(hours=1)
    await seed_1x2_snaps(session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, close)
    await seed_1x2_snaps(session, pick.event_id, "SoftBook", SOFTBOOK_CLOSE, close)

    settled, _ = await settle_event_picks(
        session, pick.event_id, 2, 1, NOW, devig_method=DevigMethod.SHIN
    )
    assert settled == 1
    await session.refresh(pick)
    assert pick.status == "settled"
    fair = devig(PINNACLE_CLOSE, method=DevigMethod.SHIN)[0]
    assert pick.closing_fair_probability is not None
    assert float(pick.closing_fair_probability) == pytest.approx(fair, abs=1e-6)
    assert pick.closing_anchor_type == "pinnacle"  # entered the sharp-CLV subset


async def test_manual_event_settle_without_devig_keeps_rescrape_close(session) -> None:  # type: ignore[no-untyped-def]
    # Backward-compatible: no devig_method -> the snapshot close is NOT finalized;
    # the pick keeps whatever the re-scrape path last wrote (0.400000).
    pick = await seed_pick(session, "evt-snapclose-nodevig")
    await preset_rescrape_close(session, pick)
    await seed_1x2_snaps(
        session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, KICKOFF - timedelta(hours=1)
    )
    settled, _ = await settle_event_picks(session, pick.event_id, 2, 1, NOW)
    assert settled == 1
    await session.refresh(pick)
    assert pick.closing_fair_probability is not None
    assert float(pick.closing_fair_probability) == pytest.approx(0.400000)  # unchanged


async def test_stale_coverage_falls_back_to_rescrape_close(session) -> None:  # type: ignore[no-untyped-def]
    # The event FELL OUT of the scrape 3 days before kickoff (> 4h window):
    # those snapshots are not a close; the re-scrape values must survive.
    pick = await seed_pick(session, "evt-snapclose-stale")
    await preset_rescrape_close(session, pick)
    stale_at = KICKOFF - SNAPSHOT_CLOSE_MAX_GAP - timedelta(days=3)
    await seed_1x2_snaps(session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, stale_at)
    await seed_1x2_snaps(session, pick.event_id, "SoftBook", SOFTBOOK_CLOSE, stale_at)

    settled = await settle_open_picks(session, score_book(2, 1), NOW, devig_method=DevigMethod.SHIN)
    assert settled == 1
    await session.refresh(pick)
    assert pick.status == "settled"  # settlement itself is unaffected
    assert pick.closing_fair_probability == Decimal("0.400000")  # re-scrape close kept
    assert pick.clv_log == Decimal("0.000000")
    assert pick.beat_close is False
    assert pick.closing_odds is None  # snapshot provenance marker absent


async def test_change_only_old_unmoved_row_is_still_the_close(session) -> None:  # type: ignore[no-untyped-def]
    # THE change-only subtlety: SoftBook's price never moved after KICKOFF-2d,
    # so its last row is 2 days old — but the EVENT kept being scraped
    # (Pinnacle row at KICKOFF-1h), so that old row IS SoftBook's true close.
    # Per-book row age must never gate validity; only event-wide coverage does.
    pick = await seed_pick(session, "evt-snapclose-unmoved")
    await seed_1x2_snaps(
        session, pick.event_id, "SoftBook", SOFTBOOK_CLOSE, KICKOFF - timedelta(days=2)
    )
    await seed_1x2_snaps(
        session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, KICKOFF - timedelta(hours=1)
    )

    settled = await settle_open_picks(session, score_book(2, 1), NOW, devig_method=DevigMethod.SHIN)
    assert settled == 1
    await session.refresh(pick)
    assert pick.closing_fair_probability is not None
    fair = devig(PINNACLE_CLOSE, method=DevigMethod.SHIN)[0]
    assert float(pick.closing_fair_probability) == pytest.approx(fair, abs=1e-6)
    assert pick.closing_odds == Decimal("2.3000")  # the 2-day-old unmoved row


# --- devig-method and effective-odds consistency -------------------------------


async def test_snapshot_close_uses_the_pick_pipelines_devig_method(session) -> None:  # type: ignore[no-untyped-def]
    # The closing fair MUST come from the same devig method the pick pipeline
    # ran with (odds-math skill rule); SHIN and POWER give measurably
    # different fairs on the same close set — each call must match its own.
    pick = await seed_pick(session, "evt-snapclose-devig")
    await seed_1x2_snaps(
        session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, KICKOFF - timedelta(hours=1)
    )
    ref = await event_ref_of(session, pick)

    fairs: dict[DevigMethod, float] = {}
    for method in (DevigMethod.SHIN, DevigMethod.POWER):
        assert await finalize_closing_from_snapshots(session, pick, ref, KICKOFF, method)
        assert pick.closing_fair_probability is not None
        assert pick.clv_log is not None
        expected = devig(PINNACLE_CLOSE, method=method)[0]
        got = float(pick.closing_fair_probability)
        assert got == pytest.approx(expected, abs=1e-6)
        assert float(pick.clv_log) == pytest.approx(math.log(2.50 * expected), abs=1e-5)
        fairs[method] = got
    assert abs(fairs[DevigMethod.SHIN] - fairs[DevigMethod.POWER]) > 5e-6


async def test_snapshot_close_nets_commission_on_both_sides(session) -> None:  # type: ignore[no-untyped-def]
    # Effective-odds symmetry: a pick FILLED at a commissioned exchange whose
    # close set is also exchange-anchored must net 5% on BOTH sides — the
    # close anchor's odds are netted before devig, and the fill is netted in
    # clv_log. Feeding either side gross inflates CLV by ~ln(1/(1-c)).
    pick = await seed_pick(session, "evt-snapclose-eff", bookmaker="Betfair Exchange")
    await seed_1x2_snaps(
        session, pick.event_id, "Betfair Exchange", PINNACLE_CLOSE, KICKOFF - timedelta(hours=1)
    )

    settled = await settle_open_picks(session, score_book(2, 1), NOW, devig_method=DevigMethod.SHIN)
    assert settled == 1
    await session.refresh(pick)
    assert pick.closing_fair_probability is not None
    assert pick.clv_log is not None
    eff_close = tuple(1.0 + (o - 1.0) * 0.95 for o in PINNACLE_CLOSE)  # 5% netted
    fair = devig(eff_close, method=DevigMethod.SHIN)[0]  # ~0.4363809
    assert float(pick.closing_fair_probability) == pytest.approx(fair, abs=1e-6)
    eff_fill = 1.0 + (2.50 - 1.0) * 0.95  # 2.425
    assert float(pick.clv_log) == pytest.approx(math.log(eff_fill * fair), abs=1e-5)
    # explicitly NOT the gross-fill CLV (gap is ln(2.5/2.425) ~ 0.0305)
    assert abs(float(pick.clv_log) - math.log(2.50 * fair)) > 0.01
    # closing_odds stays the RAW displayed price, like every odds column
    assert pick.closing_odds == Decimal("2.2000")


# --- coverage edge cases -------------------------------------------------------


async def test_no_kickoff_or_no_snapshots_returns_false(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-snapclose-none")
    ref = await event_ref_of(session, pick)
    # kickoff unknown -> "close" undefined
    assert not await finalize_closing_from_snapshots(session, pick, ref, None, DevigMethod.SHIN)
    # no snapshot history at all -> no coverage
    assert not await finalize_closing_from_snapshots(session, pick, ref, KICKOFF, DevigMethod.SHIN)
    assert pick.closing_fair_probability is None
    assert pick.closing_odds is None


async def test_single_soft_book_close_is_not_anchorable(session) -> None:  # type: ignore[no-untyped-def]
    # Same min-book rules as the live path: one NON-sharp book is neither a
    # named sharp anchor nor a >=3-book consensus — no fair, fall back.
    pick = await seed_pick(session, "evt-snapclose-thin")
    await preset_rescrape_close(session, pick)
    await seed_1x2_snaps(
        session, pick.event_id, "SoftBook", SOFTBOOK_CLOSE, KICKOFF - timedelta(hours=1)
    )
    ref = await event_ref_of(session, pick)
    assert not await finalize_closing_from_snapshots(session, pick, ref, KICKOFF, DevigMethod.SHIN)
    assert pick.closing_fair_probability == Decimal("0.400000")  # untouched
    assert pick.closing_odds is None


async def test_sharp_only_close_sets_snapshot_flag_with_null_closing_odds(session) -> None:  # type: ignore[no-untyped-def]
    # clv-1: a close anchored ONLY by a sharp book that NO soft book quoted is a
    # GENUINE snapshot close. The pick's SoftBook fill is absent from the close set
    # (Pinnacle-only), so closing_odds (the soft display price) is NULL — but
    # has_snapshot_close must be True so the trusted sharp-CLV subset, now gated on
    # that flag (not on closing_odds), still admits the close.
    pick = await seed_pick(session, "evt-snapclose-sharponly", bookmaker="SoftBook")
    await seed_1x2_snaps(
        session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, KICKOFF - timedelta(hours=1)
    )
    ref = await event_ref_of(session, pick)
    assert await finalize_closing_from_snapshots(session, pick, ref, KICKOFF, DevigMethod.SHIN)
    assert pick.has_snapshot_close is True  # genuine snapshot close anchored
    assert pick.closing_odds is None  # ...but no soft book priced it -> no display price
    assert pick.closing_anchor_type == "pinnacle"
    fair = devig(PINNACLE_CLOSE, method=DevigMethod.SHIN)[0]
    assert float(pick.closing_fair_probability) == pytest.approx(fair, abs=1e-6)


async def test_closing_odds_from_snapshots_last_row_per_book(session) -> None:  # type: ignore[no-untyped-def]
    # The read helper itself: last pre-kickoff row per (market, book,
    # selection), provider market keys mapped back, post-kickoff excluded,
    # and the event-wide last-capture clock reported.
    pick = await seed_pick(session, "evt-snapclose-helper")
    await seed_1x2_snaps(
        session, pick.event_id, "Pinnacle", (2.60, 3.20, 3.00), KICKOFF - timedelta(hours=30)
    )
    await seed_1x2_snaps(
        session, pick.event_id, "Pinnacle", PINNACLE_CLOSE, KICKOFF - timedelta(hours=1)
    )
    await seed_1x2_snaps(
        session, pick.event_id, "Pinnacle", (2.05, 3.60, 3.50), KICKOFF + timedelta(minutes=10)
    )
    ref = await event_ref_of(session, pick)

    snaps, last_capture = await closing_odds_from_snapshots(session, pick.event_id, ref, KICKOFF)
    assert last_capture == KICKOFF - timedelta(hours=1)
    assert len(snaps) == 3  # one row per selection, the close row only
    by_sel = {s.selection: s for s in snaps}
    assert by_sel[HOME].decimal_odds == 2.20  # not 2.60 (early), not 2.05 (post-KO)
    assert by_sel[HOME].market is Market.H2H  # "1x2" mapped back
    assert by_sel[HOME].market_detail == "1x2"
    assert by_sel[HOME].event_id == ref
