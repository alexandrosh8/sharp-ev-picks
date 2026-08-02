"""Settlement engine against compose Postgres (rollback-isolated; skips
when the DB is absent) — mirrors tests/test_persistence.py."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.settlement.engine import (
    run_settlement_cycle,
    settle_event_picks,
    settle_open_picks,
)
from app.settlement.results import Completion, FinalScore, ScoreBook
from app.storage.models import Event, ManualBetLog, Pick, ResultTracking
from app.storage.repositories import persist_pick
from tests.database import TEST_DATABASE_URL

DB_URL = TEST_DATABASE_URL

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
KICKOFF = NOW - timedelta(hours=6)
HOME = "Settle Alpha"
AWAY = "Settle Beta"


def make_pick(
    event_id: str,
    market: Market = Market.TOTALS,
    selection: str = "Over 2.5",
    tier: str = "premium",
) -> PickOut:
    return PickOut(
        pick_id="p-settle",
        sport="soccer",
        league="test-league-settlement",
        event=f"{HOME} vs {AWAY}",
        event_id=event_id,
        market=market,
        selection=selection,
        bookmaker="testbook",
        decimal_odds=2.10,
        model_probability=0.55,
        fair_probability=0.50,
        edge=0.05,
        ev=0.155,
        confidence=0.70,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary="settlement test",
        tier=tier,
        created_at=NOW - timedelta(hours=8),
    )


def book_with_score(hs: int = 2, as_: int = 1) -> ScoreBook:
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


async def seed_pick(  # type: ignore[no-untyped-def]
    session, event_id: str, *, home: str = HOME, away: str = AWAY, **kwargs
) -> Pick:
    teams = EventTeams(home=home, away=away, league="test-league-settlement", starts_at=KICKOFF)
    assert await persist_pick(session, make_pick(event_id, **kwargs), teams, "value", "test-v")
    pick = await session.scalar(
        select(Pick).where(Pick.reason_summary == "settlement test").order_by(Pick.id.desc())
    )
    assert pick is not None
    return pick


async def test_settles_past_pick_with_result_row(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-settle-1")
    n = await settle_open_picks(session, book_with_score(2, 1), NOW)
    assert n == 1
    await session.refresh(pick)
    assert pick.status == "settled"
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == "won"  # Over 2.5 with 3 goals
    assert row.pnl == Decimal("22.00")  # 20 @ 2.10
    assert row.roi == Decimal("1.1")
    assert row.settled_stake_amount == Decimal("20.00")
    assert row.settled_effective_odds == Decimal("2.1000")
    assert row.settled_at == NOW


async def test_settled_event_status_transitions_to_finished(session) -> None:  # type: ignore[no-untyped-def]
    # Issue 2: Event.status was never transitioned, so a finished, settled game
    # stayed 'scheduled'. Settling a pick from a real final score (_settle_one)
    # is the canonical "event is over" trigger and must flip Event.status to
    # 'finished'. (A VOID — abandoned/TBD — is NOT finished and stays put.)
    pick = await seed_pick(session, "evt-status-finished")
    ev_before = await session.scalar(
        select(Event).where(Event.external_ref == "evt-status-finished")
    )
    assert ev_before is not None
    assert ev_before.status == "scheduled"  # baseline before settlement

    assert await settle_open_picks(session, book_with_score(2, 1), NOW) == 1
    await session.refresh(pick)
    assert pick.status == "settled"
    ev_after = await session.scalar(
        select(Event).where(Event.external_ref == "evt-status-finished")
    )
    assert ev_after is not None
    assert ev_after.status == "finished"  # settling the pick marked the event over


async def test_settlement_is_idempotent(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-settle-2")
    assert await settle_open_picks(session, book_with_score(), NOW) == 1
    assert await settle_open_picks(session, book_with_score(), NOW) == 0
    rows = (
        await session.scalars(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    ).all()
    assert len(rows) == 1


async def test_uses_manual_bet_log_stake_and_odds(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-settle-3")
    await session.execute(
        insert(ManualBetLog).values(
            pick_id=pick.id,
            bet_placed=True,
            actual_stake=Decimal("50.00"),
            actual_odds=Decimal("2.50"),
        )
    )
    await settle_open_picks(session, book_with_score(2, 1), NOW)
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.pnl == Decimal("75.00")  # 50 @ 2.50 won
    assert row.settled_stake_amount == Decimal("50.00")
    assert row.settled_effective_odds == Decimal("2.5000")


@pytest.mark.parametrize(
    ("bookmaker", "expected_pnl", "expected_effective_odds"),
    [
        ("Betfair", Decimal("71.25"), Decimal("2.4250")),
        ("Matchbook", Decimal("73.50"), Decimal("2.4700")),
    ],
)
async def test_manual_exchange_fill_is_commission_netted_once(
    session: AsyncSession,
    bookmaker: str,
    expected_pnl: Decimal,
    expected_effective_odds: Decimal,
) -> None:
    pick = await seed_pick(session, f"evt-settle-exchange-{bookmaker.lower()}")
    await session.execute(
        insert(ManualBetLog).values(
            pick_id=pick.id,
            bet_placed=True,
            actual_stake=Decimal("50.00"),
            actual_odds=Decimal("2.50"),
            bookmaker_used=bookmaker,
        )
    )
    await settle_open_picks(session, book_with_score(2, 1), NOW)
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.pnl == expected_pnl
    assert row.settled_stake_amount == Decimal("50.00")
    assert row.settled_effective_odds == expected_effective_odds


async def test_manual_bet_without_actual_odds_uses_blended_recommendation(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-settle-manual-blended")
    pick.settlement_stake_amount = Decimal("30.00")
    pick.settlement_raw_odds_stake = Decimal("66.000000")
    pick.settlement_effective_odds_stake = Decimal("66.000000")
    await session.execute(
        insert(ManualBetLog).values(
            pick_id=pick.id,
            bet_placed=True,
            actual_stake=Decimal("50.00"),
            actual_odds=None,
        )
    )
    await settle_open_picks(session, book_with_score(2, 1), NOW)
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.pnl == Decimal("60.00")  # 50 at blended 2.20, not latest 2.10
    assert row.settled_stake_amount == Decimal("50.00")
    assert row.settled_effective_odds == Decimal("2.2000")


async def test_lost_pick_settles_negative(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-settle-4")
    await settle_open_picks(session, book_with_score(1, 0), NOW)  # 1 goal -> Over 2.5 lost
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == "lost"
    assert row.pnl == Decimal("-20.00")


async def test_settles_football_ah_volume_pick(session) -> None:  # type: ignore[no-untyped-def]
    # A football Asian-Handicap volume/shadow pick (commit 706f87e) persists as
    # market=spreads, selection "<home> -1.5" (the _selections form). It must
    # settle through the SAME path as 1x2/totals — outcome + pnl + roi +
    # settled_at — so its realized result and CLV accrue (the one missing piece).
    pick = await seed_pick(
        session, "evt-ah-1", market=Market.SPREADS, selection=f"{HOME} -1.5", tier="volume"
    )
    assert await settle_open_picks(session, book_with_score(3, 1), NOW) == 1  # margin +2
    await session.refresh(pick)
    assert pick.status == "settled"
    assert pick.tier == "volume"  # never promoted by settlement
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == "won"
    assert row.pnl == Decimal("22.00")  # 20 @ 2.10
    assert row.roi == Decimal("1.1")
    assert row.settled_at == NOW


async def test_settles_football_ah_quarter_line_half_win(session) -> None:  # type: ignore[no-untyped-def]
    # Quarter line -0.75 with a 1-goal home win = HALF_WON: half the stake wins
    # at full odds, half is returned. The split-stake P&L must flow through
    # _settle_one/pick_pnl, not collapse to a plain win.
    pick = await seed_pick(
        session, "evt-ah-2", market=Market.SPREADS, selection=f"{HOME} -0.75", tier="volume"
    )
    assert (
        await settle_open_picks(session, book_with_score(2, 1), NOW) == 1
    )  # -0.5 wins / -1.0 push
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == "half_won"
    assert row.pnl == Decimal("11.00")  # half of 20 @ 2.10 -> 10 * 1.10
    assert row.settled_at == NOW


@pytest.mark.parametrize(
    ("market", "selection", "home_score", "away_score", "outcome", "expected_pnl"),
    [
        (Market.TOTALS, "Over 2.5", 2, 1, "won", Decimal("36.00")),
        (Market.TOTALS, "Over 2.5", 1, 0, "lost", Decimal("-30.00")),
        (Market.SPREADS, f"{HOME} -0.75", 2, 1, "half_won", Decimal("18.00")),
        (Market.SPREADS, f"{HOME} +0.75", 0, 1, "half_lost", Decimal("-15.00")),
    ],
)
async def test_two_tranche_basis_grades_exact_pnl(
    session: AsyncSession,
    market: Market,
    selection: str,
    home_score: int,
    away_score: int,
    outcome: str,
    expected_pnl: Decimal,
) -> None:
    """20 @ 2.10 plus 10 @ 2.40 grades as 30 @ blended 2.20."""
    pick = await seed_pick(
        session,
        f"evt-two-tranche-{outcome}",
        market=market,
        selection=selection,
    )
    pick.settlement_stake_amount = Decimal("30.00")
    pick.settlement_raw_odds_stake = Decimal("66.000000")
    pick.settlement_effective_odds_stake = Decimal("66.000000")
    assert (
        await settle_open_picks(
            session,
            book_with_score(home_score, away_score),
            NOW,
        )
        == 1
    )
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == outcome
    assert row.pnl == expected_pnl
    assert row.settled_stake_amount == Decimal("30.00")
    assert row.settled_effective_odds == Decimal("2.2000")


async def test_football_ah_unparseable_selection_skipped(session, caplog) -> None:  # type: ignore[no-untyped-def]
    # A spreads selection with no signed line cannot be graded -> skipped, not
    # guessed (refusal discipline); the pick stays open for manual settlement.
    pick = await seed_pick(
        session, "evt-ah-3", market=Market.SPREADS, selection=HOME, tier="volume"
    )
    with caplog.at_level("WARNING"):
        assert await settle_open_picks(session, book_with_score(2, 1), NOW) == 0
    await session.refresh(pick)
    assert pick.status == "alerted"
    assert any("not settleable" in r.message for r in caplog.records)


async def test_unsettleable_warns_once_across_cycles_with_summary(session, caplog) -> None:  # type: ignore[no-untyped-def]
    # Warning dedup (audit S): an unsettleable pick warns on FIRST sighting
    # only — repeat cycles emit the per-cycle summary line, not 167k per-pick
    # re-warns. The pick stays open throughout.
    from app.settlement.engine import reset_unsettleable_warning_state

    reset_unsettleable_warning_state()
    pick = await seed_pick(
        session, "evt-warn-dedup", market=Market.SPREADS, selection="Gamma Town -0.75"
    )
    with caplog.at_level("WARNING"):
        assert await settle_open_picks(session, book_with_score(2, 1), NOW) == 0
        assert await settle_open_picks(session, book_with_score(2, 1), NOW) == 0
        assert await settle_open_picks(session, book_with_score(2, 1), NOW) == 0
    await session.refresh(pick)
    assert pick.status == "alerted"
    per_pick = [r for r in caplog.records if "not settleable" in r.getMessage()]
    assert len(per_pick) == 1
    summaries = [r for r in caplog.records if "picks unsettleable (" in r.getMessage()]
    assert len(summaries) == 3
    assert "1 picks unsettleable (1 spreads)" in summaries[0].getMessage()


async def test_future_kickoff_stays_open(session) -> None:  # type: ignore[no-untyped-def]
    teams = EventTeams(
        home=HOME, away=AWAY, league="test-league-settlement", starts_at=NOW + timedelta(hours=3)
    )
    assert await persist_pick(session, make_pick("evt-settle-5"), teams, "value", "test-v")
    book = ScoreBook(
        [
            FinalScore(
                home_team=HOME,
                away_team=AWAY,
                match_date=NOW.date(),
                home_score=2,
                away_score=1,
            )
        ]
    )
    assert await settle_open_picks(session, book, NOW) == 0


async def test_missing_score_stays_open(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-settle-6")
    other = ScoreBook(
        [
            FinalScore(
                home_team="Unrelated FC",
                away_team="Nobody United",
                match_date=KICKOFF.date(),
                home_score=1,
                away_score=0,
            )
        ]
    )
    assert await settle_open_picks(session, other, NOW) == 0
    await session.refresh(pick)
    assert pick.status == "alerted"


async def test_empty_book_refuses_to_settle(session, caplog) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-settle-7")
    with caplog.at_level("ERROR"):
        assert await settle_open_picks(session, ScoreBook([]), NOW) == 0
    assert any("empty score book" in r.message for r in caplog.records)
    await session.refresh(pick)
    assert pick.status == "alerted"


async def test_unparseable_selection_skipped_not_guessed(session, caplog) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-settle-8", market=Market.CORRECT_SCORE, selection="2:1")
    with caplog.at_level("WARNING"):
        assert await settle_open_picks(session, book_with_score(), NOW) == 0
    await session.refresh(pick)
    assert pick.status == "alerted"


def test_settle_delay_for_applies_sport_floor() -> None:
    # Basketball/NFL/tennis picks must not be settle-eligible on the generic
    # 2h delay: an NBA back-to-back's game-2 is still in play then, and the
    # score book's ±1-day tolerance would settle it with game-1's final.
    from app.settlement.engine import SETTLE_DELAY, settle_delay_for

    assert settle_delay_for("soccer") == SETTLE_DELAY  # unchanged for soccer
    assert settle_delay_for("basketball") == timedelta(hours=4)
    assert settle_delay_for("american_football") == timedelta(hours=4, minutes=30)
    assert settle_delay_for("tennis") == timedelta(hours=6)
    assert settle_delay_for("unknown-sport") == SETTLE_DELAY
    # a caller-supplied LONGER base delay is never shortened by the map
    assert settle_delay_for("basketball", timedelta(hours=9)) == timedelta(hours=9)


async def test_basketball_pick_not_settle_eligible_before_sport_floor(session) -> None:  # type: ignore[no-untyped-def]
    # Back-to-back guard, half 2: at kickoff+2h an NBA game can still be in
    # play while YESTERDAY'S same-pairing final sits in the book — the pick
    # must not be settle-eligible until the sport floor (4h) passes, by which
    # time the exact-date final exists and is preferred by ScoreBook.lookup.
    home, away = "Backtoback Hawks", "Backtoback Bulls"
    kickoff = NOW - timedelta(hours=2, minutes=30)  # past the generic 2h delay
    teams = EventTeams(home=home, away=away, league="nba", starts_at=kickoff)
    pick_out = make_pick("evt-bball-floor", market=Market.H2H, selection=home).model_copy(
        update={"sport": "basketball", "event": f"{home} vs {away}"}
    )
    assert await persist_pick(session, pick_out, teams, "value", "test-v")
    yesterdays_final = ScoreBook(
        [FinalScore(home, away, kickoff.date() - timedelta(days=1), 110, 99)]
    )
    assert await settle_open_picks(session, yesterdays_final, NOW) == 0  # floor holds
    todays_final = ScoreBook([FinalScore(home, away, kickoff.date(), 120, 100)])
    assert await settle_open_picks(session, todays_final, NOW) == 0  # still < 4h

    # past the sport floor the exact-date final settles normally
    later = NOW + timedelta(hours=2)  # kickoff+4h30
    assert await settle_open_picks(session, todays_final, later) == 1
    pick = await session.scalar(
        select(Pick)
        .join(Event, Pick.event_id == Event.id)
        .where(Event.external_ref == "evt-bball-floor")
    )
    assert pick is not None
    assert pick.status == "settled"
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert (row.home_score, row.away_score) == (120, 100)  # game-2's score, not game-1's


# --- stale-TBD voiding (NULL kickoff older than 14 days) ----------------------


async def test_voids_stale_null_kickoff_pick_and_keeps_fresh_tbd(session, caplog) -> None:  # type: ignore[no-untyped-def]
    # A pick whose event NEVER gets a kickoff cannot auto-settle and would
    # revalidate forever; after STALE_NULL_KICKOFF_AGE it is voided via the
    # standard terminal shape (result row outcome='void' + status 'settled').
    from sqlalchemy import update as sa_update

    from app.settlement.engine import STALE_NULL_KICKOFF_AGE, void_stale_null_kickoff_picks

    now = datetime.now(tz=UTC)
    # the dev warehouse may hold real open (possibly TBD) picks; pause them
    # inside this rolled-back transaction so only the seeded picks count
    await session.execute(
        sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
    )
    tbd_teams = EventTeams(home=HOME, away=AWAY, league="test-league-settlement")  # no kickoff
    assert await persist_pick(session, make_pick("evt-void-stale"), tbd_teams, "value", "test-v")
    stale = await session.scalar(select(Pick).order_by(Pick.id.desc()))
    assert stale is not None
    assert await persist_pick(
        session, make_pick("evt-void-fresh", selection="Under 2.5"), tbd_teams, "value", "test-v"
    )
    fresh = await session.scalar(select(Pick).order_by(Pick.id.desc()))
    assert fresh is not None
    await session.execute(
        sa_update(Pick)
        .where(Pick.id == stale.id)
        .values(created_at=now - STALE_NULL_KICKOFF_AGE - timedelta(days=1))
    )

    with caplog.at_level("INFO"):
        assert await void_stale_null_kickoff_picks(session, now) == 1
    await session.refresh(stale)
    await session.refresh(fresh)
    assert stale.status == "settled"
    assert fresh.status == "alerted"  # TBD but younger than the deadline
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == stale.id))
    assert row is not None
    assert row.outcome == "void"
    assert row.pnl == Decimal("0.00")  # stake treated as returned
    assert row.settled_at == now
    assert any("kickoff still unknown" in r.message for r in caplog.records)

    # idempotent: a second pass finds nothing voidable
    assert await void_stale_null_kickoff_picks(session, now) == 0


async def test_void_leaves_known_kickoff_picks_alone(session) -> None:  # type: ignore[no-untyped-def]
    # Voiding is for kickoff-UNKNOWN picks only: an old pick whose event has
    # a real starts_at settles by score, never by the staleness deadline.
    from sqlalchemy import update as sa_update

    from app.settlement.engine import STALE_NULL_KICKOFF_AGE, void_stale_null_kickoff_picks

    now = datetime.now(tz=UTC)
    await session.execute(  # pause any real open picks (see test above)
        sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
    )
    pick = await seed_pick(session, "evt-void-known-kickoff")  # starts_at=KICKOFF
    await session.execute(
        sa_update(Pick)
        .where(Pick.id == pick.id)
        .values(created_at=now - STALE_NULL_KICKOFF_AGE - timedelta(days=30))
    )
    assert await void_stale_null_kickoff_picks(session, now) == 0
    await session.refresh(pick)
    assert pick.status == "alerted"


async def test_voids_unsettleable_known_kickoff_pick(session) -> None:  # type: ignore[no-untyped-def]
    # A KNOWN-kickoff pick whose game is older than the scrape window with NO
    # captured score can never settle (feed + scrape both exhausted) -> void it
    # so it cannot sit "awaiting result" forever. A still-in-window pick, or one
    # that already carries a scraped score, is left alone.
    from sqlalchemy import update as sa_update

    from app.settlement.engine import (
        STALE_UNSETTLEABLE_AGE,
        void_unsettleable_known_kickoff_picks,
    )
    from app.storage.models import Event

    now = datetime.now(tz=UTC)
    await session.execute(
        sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
    )
    old = STALE_UNSETTLEABLE_AGE + timedelta(days=1)

    # 1) old + no score -> voidable
    assert await persist_pick(
        session,
        make_pick("evt-unsettle-old"),
        EventTeams(home=HOME, away=AWAY, starts_at=now - old),
        "value",
        "test-v",
    )
    old_pick = await session.scalar(select(Pick).order_by(Pick.id.desc()))
    # 2) old but HAS a scraped score -> settles by score, not voided
    assert await persist_pick(
        session,
        make_pick("evt-unsettle-scored", selection="Under 2.5"),
        # Distinct fixture (own event) so the resolver keeps it separate from the
        # voidable pick above and the scraped-score UPDATE below lands on it.
        EventTeams(home=HOME, away="Scored Beta", starts_at=now - old),
        "value",
        "test-v",
    )
    scored = await session.scalar(select(Pick).order_by(Pick.id.desc()))
    await session.execute(
        sa_update(Event)
        .where(Event.external_ref == "evt-unsettle-scored")
        .values(scraped_home_score=1, scraped_away_score=0)
    )
    # 3) recent (still scrapeable) -> not voided
    assert await persist_pick(
        session,
        make_pick("evt-unsettle-recent", selection="Over 3.5"),
        EventTeams(home=HOME, away=AWAY, starts_at=now - timedelta(days=5)),
        "value",
        "test-v",
    )
    recent = await session.scalar(select(Pick).order_by(Pick.id.desc()))

    assert await void_unsettleable_known_kickoff_picks(session, now) == 1
    for p in (old_pick, scored, recent):
        assert p is not None
        await session.refresh(p)
    assert old_pick.status == "settled"
    assert scored.status == "alerted"  # has a score -> settles normally
    assert recent.status == "alerted"  # still in window
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == old_pick.id))
    assert row is not None and row.outcome == "void"
    # The 15d no-result void carries the same provenance note as the bounded
    # expiry policy: every provider-gap void stays distinguishable later.
    from app.settlement.engine import EXPIRED_NO_RESULT_NOTE

    assert row.note == EXPIRED_NO_RESULT_NOTE
    # idempotent
    assert await void_unsettleable_known_kickoff_picks(session, now) == 0


async def test_known_kickoff_void_supersedes_cross_source_duplicate(session) -> None:  # type: ignore[no-untyped-def]
    """The stale-void path must not mint two P&L rows for one fixture."""
    from sqlalchemy import update as sa_update

    from app.settlement.engine import (
        STALE_UNSETTLEABLE_AGE,
        void_unsettleable_known_kickoff_picks,
    )

    now = datetime.now(tz=UTC)
    await session.execute(
        sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
    )
    canonical = await seed_pick(session, "evt-stale-void-dedup-a")
    canonical.status = "alerted"
    event = await session.get(Event, canonical.event_id)
    assert event is not None
    event.starts_at = now - STALE_UNSETTLEABLE_AGE - timedelta(days=1)

    duplicate_event = Event(
        sport_id=event.sport_id,
        league_id=event.league_id,
        home_team_id=event.home_team_id,
        away_team_id=event.away_team_id,
        external_ref="evt-stale-void-dedup-b",
        starts_at=event.starts_at,
    )
    session.add(duplicate_event)
    await session.flush()
    values = {
        column.name: getattr(canonical, column.name)
        for column in Pick.__table__.columns
        if column.name not in {"id", "created_at"}
    }
    values["event_id"] = duplicate_event.id
    duplicate = Pick(**values)
    session.add(duplicate)
    await session.flush()

    assert await void_unsettleable_known_kickoff_picks(session, now) == 1
    await session.refresh(canonical)
    await session.refresh(duplicate)
    assert {canonical.status, duplicate.status} == {"settled", "superseded"}
    result_pick_ids = set(
        (
            await session.scalars(
                select(ResultTracking.pick_id).where(
                    ResultTracking.pick_id.in_((canonical.id, duplicate.id))
                )
            )
        ).all()
    )
    assert len(result_pick_ids) == 1


# --- full cycle (providers -> book -> settle), as the scheduler job runs it ----


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


CYCLE_CSV = (
    "Country,League,Date,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA\n"
    f"Brazil,Serie A,{KICKOFF.strftime('%d/%m/%Y')},{HOME},{AWAY},2,1,H,1.9,3.4,4.2\n"
)


async def test_run_settlement_cycle_end_to_end(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session:
        await seed_pick(session, "evt-settle-cycle")
        await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/new/BRA.csv"):
            return httpx.Response(200, text=CYCLE_CSV)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        settled = await run_settlement_cycle(
            client, factory, slugs=["brazil-serie-a"], seasons=[], now=NOW
        )
    assert settled == 1
    async with factory() as session:
        row = await session.scalar(
            select(ResultTracking)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .where(Pick.reason_summary == "settlement test")
        )
        assert row is not None
        assert row.outcome == "won"


def _nba_final(home: str, away: str, hs: int, a_s: int, d) -> dict:  # type: ignore[no-untyped-def]
    return {
        "events": [
            {
                "date": d.isoformat() + "T23:00Z",
                "competitions": [
                    {
                        "status": {"type": {"name": "STATUS_FINAL", "completed": True}},
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": str(hs),
                                "winner": hs > a_s,
                                "team": {"displayName": home},
                            },
                            {
                                "homeAway": "away",
                                "score": str(a_s),
                                "winner": a_s > hs,
                                "team": {"displayName": away},
                            },
                        ],
                    }
                ],
            }
        ]
    }


async def test_run_settlement_cycle_auto_settles_basketball_from_espn(factory) -> None:  # type: ignore[no-untyped-def]
    # The CLOSED-tab auto-result win: a basketball pick (no free CSV feed)
    # settles from ESPN scores through the SAME cycle, no manual entry.
    home, away = "Philadelphia 76ers", "Houston Rockets"
    async with factory() as session:
        teams = EventTeams(home=home, away=away, league="nba", starts_at=KICKOFF)
        pick = make_pick("evt-bball-espn", market=Market.H2H, selection=home).model_copy(
            update={"sport": "basketball", "event": f"{home} vs {away}"}
        )
        assert await persist_pick(session, pick, teams, "value", "test-v")
        await session.commit()

    nba = _nba_final(home, away, 124, 115, KICKOFF.date())

    def handler(request: httpx.Request) -> httpx.Response:
        if "basketball/nba" in request.url.path:
            return httpx.Response(200, json=nba)
        return httpx.Response(404)  # no soccer CSV; other ESPN feeds empty

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        settled = await run_settlement_cycle(client, factory, slugs=[], seasons=[], now=NOW)
    assert settled == 1
    async with factory() as session:
        row = await session.scalar(
            select(ResultTracking)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .join(Event, Pick.event_id == Event.id)
            .where(Event.external_ref == "evt-bball-espn")
        )
        assert row is not None
        assert row.outcome == "won"  # 124-115 home win, selection = home
        assert row.home_score == 124
        assert row.away_score == 115


async def test_run_settlement_cycle_auto_settles_from_scraped_score(factory) -> None:  # type: ignore[no-untyped-def]
    # No free results feed reaches this minor league, but OddsPortal scraped the
    # final score after the match -> it AUTO-settles from Event.scraped_* through
    # the same cycle, no manual entry (settle_from_scraped_scores).
    home, away = "Balcatta SC", "Perth Azzurri"
    async with factory() as session:
        teams = EventTeams(home=home, away=away, league="npl-wa", starts_at=KICKOFF)
        pick = make_pick("evt-scraped", market=Market.H2H, selection=home).model_copy(
            update={"sport": "soccer", "event": f"{home} vs {away}"}
        )
        assert await persist_pick(session, pick, teams, "value", "test-v")
        ev = await session.scalar(select(Event).where(Event.external_ref == "evt-scraped"))
        ev.scraped_home_score = 2  # OddsPortal scraped the final score post-match
        ev.scraped_away_score = 1
        await session.commit()

    # every feed 404s + ESPN empty -> the scraped score is the only source
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        settled = await run_settlement_cycle(client, factory, slugs=[], seasons=[], now=NOW)
    assert settled == 1
    async with factory() as session:
        row = await session.scalar(
            select(ResultTracking)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .join(Event, Pick.event_id == Event.id)
            .where(Event.external_ref == "evt-scraped")
        )
        assert row is not None
        assert row.outcome == "won"  # 2-1 home win, selection = home
        assert row.home_score == 2
        assert row.away_score == 1


async def test_run_settlement_cycle_drains_obscure_no_feed_league_via_scraped_score(  # type: ignore[no-untyped-def]
    factory,
) -> None:
    # THE cactusbets.cloud end-state regression: a FINISHED obscure-league
    # fixture (Real Banjul — no ESPN/CSV results feed) whose Event carries a
    # scraped final score must be DRAINED from 'alerted' to settled by
    # run_settlement_cycle through the existing scraped-score SECOND pass, with
    # the correct outcome — no manual entry, no feed. This is what the 24 stuck
    # "awaiting result" June 17-18 picks needed once their score was captured
    # (bug 2) and settle_results actually ran (bug 1's watchdog).
    home, away = "Real Banjul", "Gamtel FC"
    async with factory() as session:
        teams = EventTeams(home=home, away=away, league="gambia-gfa-league", starts_at=KICKOFF)
        pick = make_pick("evt-real-banjul", market=Market.H2H, selection=home).model_copy(
            update={"sport": "soccer", "event": f"{home} vs {away}"}
        )
        assert await persist_pick(session, pick, teams, "value", "test-v")
        ev = await session.scalar(select(Event).where(Event.external_ref == "evt-real-banjul"))
        # The score the finished-score scrape captured (bug 2 fix) lands here.
        ev.scraped_home_score = 2
        ev.scraped_away_score = 0  # Real Banjul win -> H2H on home = WON
        await session.commit()
        # Precondition: the pick is OPEN ("alerted") before settlement.
        open_pick = await session.scalar(
            select(Pick)
            .join(Event, Pick.event_id == Event.id)
            .where(Event.external_ref == "evt-real-banjul")
        )
        assert open_pick is not None
        assert open_pick.status == "alerted"

    # Every results feed 404s and ESPN is empty: this obscure GFA-league fixture
    # has NO free feed, so the scraped score is the ONLY source -> the SECOND
    # (scraped) settle pass must drain it.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        settled = await run_settlement_cycle(client, factory, slugs=[], seasons=[], now=NOW)
    assert settled == 1
    async with factory() as session:
        drained = await session.scalar(
            select(Pick)
            .join(Event, Pick.event_id == Event.id)
            .where(Event.external_ref == "evt-real-banjul")
        )
        assert drained is not None
        assert drained.status == "settled"  # drained off "awaiting result"
        row = await session.scalar(
            select(ResultTracking)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .join(Event, Pick.event_id == Event.id)
            .where(Event.external_ref == "evt-real-banjul")
        )
        assert row is not None
        assert row.outcome == "won"  # 2-0 Real Banjul win, picked home
        assert row.home_score == 2
        assert row.away_score == 0


async def test_settle_event_picks_settles_all_open_picks_of_event(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-manual-1")  # totals Over 2.5
    teams = EventTeams(home=HOME, away=AWAY, league="test-league-settlement", starts_at=KICKOFF)
    assert await persist_pick(
        session,
        make_pick("evt-manual-1", market=Market.H2H, selection=HOME),
        teams,
        "value",
        "test-v",
    )
    settled, skipped = await settle_event_picks(session, pick.event_id, 2, 1, NOW)
    assert (settled, skipped) == (2, 0)
    rows = (
        await session.scalars(
            select(ResultTracking)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .where(Pick.event_id == pick.event_id)
        )
    ).all()
    assert sorted(r.outcome for r in rows) == ["won", "won"]  # 2-1: Over 2.5 + home win


async def test_settle_event_picks_skips_unparseable(session) -> None:  # type: ignore[no-untyped-def]
    pick = await seed_pick(session, "evt-manual-2", market=Market.CORRECT_SCORE, selection="2:1")
    settled, skipped = await settle_event_picks(session, pick.event_id, 2, 1, NOW)
    assert (settled, skipped) == (0, 1)


async def test_performance_report_aggregates(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from app.storage.repositories import performance_report

    # MIN_HEADLINE_N=50 suppresses headline ratios below 50 settled picks (P2-1
    # min-n suppression). This test exercises the AGGREGATION MATH on a tiny
    # deterministic sample, so patch the threshold down to 1 for THIS test only
    # (production stays 50) — otherwise roi/clv would be nulled to "insufficient".
    monkeypatch.setattr("app.storage.repositories.MIN_HEADLINE_N", 1)

    # The dev warehouse may hold real picks/results; neutralize them inside
    # this rolled-back transaction so the aggregates are deterministic.
    await session.execute(sa_delete(ResultTracking))
    await session.execute(
        sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
    )

    won = await seed_pick(session, "evt-perf-1")  # Over 2.5, odds 2.10, stake 20
    won.clv_log = Decimal("0.05")
    won.beat_close = True
    # Genuine sharp close: a real SNAPSHOT close (has_snapshot_close — clv-1 gate)
    # anchored by Pinnacle. closing_odds is now just the optional soft display price.
    won.closing_anchor_type = "pinnacle"
    won.has_snapshot_close = True
    # The INDEPENDENCE guard (2026-06-28) admits a row to the trusted sharp subset
    # only when close_independent_of_fill is EXACTLY True (None no longer leaks in).
    # This test predates that column, so set it — else n_sharp_close is 0.
    won.close_independent_of_fill = True
    won.closing_odds = Decimal("2.1000")
    # A DISTINCT fixture (different teams) — not a second event row of the same
    # game. The settlement dedup guard collapses same-fixture/same-selection
    # duplicates, so an aggregation test of one won + one lost must use two
    # genuinely different fixtures.
    lost = await seed_pick(session, "evt-perf-2", home="Perf Gamma", away="Perf Delta")
    lost.clv_log = Decimal("-0.01")
    lost.beat_close = False
    # The lost pick has NO sharp close (no closing_odds / anchor) -> excluded
    # from the trusted sharp-close headline below.
    book = ScoreBook(
        [
            FinalScore(HOME, AWAY, KICKOFF.date(), 2, 1),  # settles the WON pick (evt-perf-1)
        ]
    )
    # The lost pick is a different fixture -> settle it manually as 1-0 (Over 2.5
    # with 1 goal = LOST); the won pick settles from the 2-1 score above.
    settled, _ = await settle_event_picks(session, lost.event_id, 1, 0, NOW)
    assert settled == 1
    assert await settle_open_picks(session, book, NOW) == 1

    report = await performance_report(session)
    assert report["n_settled"] == 2
    assert report["won"] == 1
    assert report["lost"] == 1
    assert report["total_staked"] == "40.00"
    assert report["total_pnl"] == "2.00"  # +22 - 20
    assert report["roi"] == "0.05"  # 2/40
    # stake-weighted clv: equal stakes -> mean of 0.05 and -0.01 = 0.02
    assert report["stake_weighted_clv_log"] == "0.02"
    assert report["beat_close_rate"] == "0.5"
    # Trusted sharp-close subset: only the WON pick has a snapshot-sourced
    # Pinnacle close, so the honest sharp-close headline reflects it alone
    # (the blended headline above mixes in the non-sharp-close lost pick).
    assert report["n_sharp_close"] == 1
    assert report["sharp_stake_weighted_clv_log"] == "0.05"
    assert report["sharp_beat_close_rate"] == "1"
    assert report["n_pending"] == 0
    # headline numbers are PREMIUM-scoped, and the payload says so
    assert report["tier_scope"] == "premium"
    assert report["volume"]["n_settled"] == 0
    # D4 evidence-quality diagnostics ride the same payload (tier-agnostic scope
    # + per-stratum tautology tallies) — shape contract for the dashboard panel.
    quality = report["clv_quality"]
    assert quality["scope"] == "all_tiers"
    assert quality["n_settled"] == 2
    assert quality["clv_missing"] == 0
    assert quality["clv_excluded_tautological"] == 0
    assert quality["n_snapshot_close"] >= 1  # the WON pick's genuine snapshot close
    assert isinstance(quality["strata"], list)  # Q2-shape strata (read-only SQL)


async def test_performance_report_keeps_volume_out_of_headline(session) -> None:  # type: ignore[no-untyped-def]
    """A settled VOLUME pick must not move any headline number — it lands in
    the 'volume' breakdown instead (that accumulating evidence is the shadow
    tier's purpose; mixing it in would mask the alerted strategy's ROI)."""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from app.storage.repositories import performance_report

    await session.execute(sa_delete(ResultTracking))
    await session.execute(
        sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
    )

    premium = await seed_pick(session, "evt-perf-tier-p")  # Over 2.5 @ 2.10
    # Distinct fixture so the resolver keeps a separate event (same teams+kickoff
    # would merge, and its pick would collide on the premium row's unique key).
    volume = await seed_pick(session, "evt-perf-tier-v", tier="volume", away="Volume Beta")
    settled, _ = await settle_event_picks(session, premium.event_id, 2, 1, NOW)  # won
    assert settled == 1
    settled, _ = await settle_event_picks(session, volume.event_id, 0, 0, NOW)  # lost
    assert settled == 1

    report = await performance_report(session)
    assert report["tier_scope"] == "premium"
    assert report["n_settled"] == 1  # the lost volume pick is NOT here
    assert report["won"] == 1
    assert report["lost"] == 0
    assert report["total_pnl"] == "22.00"  # premium win only
    assert report["n_pending"] == 0
    vol = report["volume"]
    assert vol["n_settled"] == 1
    assert vol["lost"] == 1
    assert vol["total_pnl"] == "-20.00"
    assert vol["n_pending"] == 0


async def test_performance_report_close_coverage_sla(session) -> None:  # type: ignore[no-untyped-def]
    """Audit #8 CLOSE/FRESHNESS SLA: a sport-market whose settled picks carry a
    trusted independent sharp close meets the SLA; one without trusted close is
    flagged below-SLA (its CLV/ROI claim is unreliable). REPORT ANNOTATION ONLY —
    no pick is hidden and no selection/stake/threshold changes."""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from app.storage.repositories import performance_report

    await session.execute(sa_delete(ResultTracking))
    await session.execute(
        sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
    )

    # WELL-COVERED sport-market (soccer/totals): a GENUINE independent Pinnacle
    # snapshot close — the trust guard admits it to n_sharp_close.
    covered = await seed_pick(
        session, "evt-sla-covered", market=Market.TOTALS, selection="Over 2.5", away="Covered Beta"
    )
    covered.clv_log = Decimal("0.05")
    covered.beat_close = True
    covered.closing_anchor_type = "pinnacle"
    covered.has_snapshot_close = True
    covered.close_independent_of_fill = True
    # POORLY-COVERED sport-market (soccer/btts): a CLV exists but from an
    # untrusted close (no snapshot / no sharp anchor) -> n_sharp_close 0.
    # Distinct fixture (own event) so the resolver does not merge it into the
    # covered pick's event (same teams+kickoff would collapse to one).
    thin = await seed_pick(
        session, "evt-sla-thin", market=Market.BTTS, selection="BTTS Yes", away="Thin Beta"
    )
    thin.clv_log = Decimal("0.01")
    # Both settle from the same 2-1 scoreline (Over 2.5 wins; both teams scored).
    assert (await settle_event_picks(session, covered.event_id, 2, 1, NOW))[0] == 1
    assert (await settle_event_picks(session, thin.event_id, 2, 1, NOW))[0] == 1

    report = await performance_report(session, close_coverage_sla=0.85)
    panel = {row["market"]: row for row in report["close_coverage_sla"]}

    covered_row = panel["totals"]
    assert covered_row["sport"] == "soccer"
    assert covered_row["n_settled"] == 1
    assert covered_row["n_trusted_close"] == 1
    assert covered_row["close_coverage"] == 1.0
    assert covered_row["sla_threshold"] == 0.85
    assert covered_row["below_sla"] is False
    assert covered_row["verdict"] == "ok"

    thin_row = panel["btts"]
    assert thin_row["n_settled"] == 1
    assert thin_row["n_trusted_close"] == 0
    assert thin_row["close_coverage"] == 0.0
    assert thin_row["below_sla"] is True
    assert thin_row["verdict"] == "coverage below SLA — CLV unreliable"


async def test_run_settlement_cycle_refuses_when_providers_empty(factory, caplog) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session:
        pick = await seed_pick(session, "evt-settle-cycle-2")
        await session.commit()
        pick_id = pick.id

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as client:
        with caplog.at_level("ERROR"):
            settled = await run_settlement_cycle(
                client, factory, slugs=["brazil-serie-a"], seasons=[], now=NOW
            )
    assert settled == 0
    assert any("no scores" in r.message for r in caplog.records)
    async with factory() as session:
        refreshed = await session.get(Pick, pick_id)
        assert refreshed is not None
        assert refreshed.status == "alerted"


# --- tennis retirement/walkover convention (pinnacle_one_set) ------------------

# Past the tennis settle floor (6h) so the picks are settle-eligible at NOW.
TENNIS_KICKOFF = NOW - timedelta(hours=7)


async def seed_tennis_pick(  # type: ignore[no-untyped-def]
    session, event_id: str, home: str, away: str, market, selection, market_detail=None
):
    teams = EventTeams(home=home, away=away, league="atp-test", starts_at=TENNIS_KICKOFF)
    pick_out = make_pick(event_id, market=market, selection=selection).model_copy(
        update={"sport": "tennis", "event": f"{home} vs {away}", "market_detail": market_detail}
    )
    assert await persist_pick(session, pick_out, teams, "value", "test-v")
    pick = await session.scalar(
        select(Pick)
        .join(Event, Pick.event_id == Event.id)
        .where(Event.external_ref == event_id, Pick.market == str(market))
        .order_by(Pick.id.desc())
    )
    assert pick is not None
    return pick


async def test_tennis_clean_final_settles_h2h_normally(session) -> None:  # type: ignore[no-untyped-def]
    # A normally-completed match (completion defaults to "full") grades
    # through the unchanged score path: 2-0 sets -> home h2h WON.
    home, away = "Clean Aces", "Clean Rally"
    pick = await seed_tennis_pick(session, "evt-ten-clean", home, away, Market.H2H, home)
    book = ScoreBook([FinalScore(home, away, TENNIS_KICKOFF.date(), 2, 0)])
    assert await settle_open_picks(session, book, NOW) == 1
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == "won"
    assert row.pnl == Decimal("22.00")  # 20 @ 2.10


async def test_tennis_retirement_grades_advancing_player_and_voids_totals(session) -> None:  # type: ignore[no-untyped-def]
    # pinnacle_one_set: retirement after >=1 completed set -> h2h graded to
    # the ADVANCING player (never from the partial set score), totals VOID.
    home, away = "Ret Alpha", "Ret Beta"
    p_win = await seed_tennis_pick(session, "evt-ten-ret", home, away, Market.H2H, home)
    p_tot = await seed_tennis_pick(session, "evt-ten-ret", home, away, Market.TOTALS, "Over 2.5")
    book = ScoreBook(
        [
            FinalScore(
                home, away, TENNIS_KICKOFF.date(), 1, 0, completion="retired", winner_side="home"
            )
        ]
    )
    assert await settle_open_picks(session, book, NOW) == 2
    won = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == p_win.id))
    assert won is not None
    assert won.outcome == "won"
    assert won.pnl == Decimal("22.00")
    assert (won.home_score, won.away_score) == (1, 0)  # completed sets at retirement
    void = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == p_tot.id))
    assert void is not None
    assert void.outcome == "void"
    assert void.pnl == Decimal("0.00")
    ev = await session.scalar(select(Event).where(Event.external_ref == "evt-ten-ret"))
    assert ev is not None
    assert ev.status == "finished"  # the match IS over — retirement ends it


async def test_tennis_retirement_grades_pick_on_retiring_player_lost(session) -> None:  # type: ignore[no-untyped-def]
    # The pick backed the player who retired: LOST — even though that player
    # led the completed sets 1-0 (the advancing flag decides, not the score).
    home, away = "Ret Gamma", "Ret Delta"
    pick = await seed_tennis_pick(session, "evt-ten-ret-lost", home, away, Market.H2H, home)
    book = ScoreBook(
        [
            FinalScore(
                home, away, TENNIS_KICKOFF.date(), 1, 0, completion="retired", winner_side="away"
            )
        ]
    )
    assert await settle_open_picks(session, book, NOW) == 1
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == "lost"
    assert row.pnl == Decimal("-20.00")


async def test_tennis_walkover_voids_all_markets_and_leaves_event_unfinished(session) -> None:  # type: ignore[no-untyped-def]
    # Walkover / abandoned before one completed set: EVERY market voids with
    # pnl 0, no score is persisted, and the never-played event is NOT flipped
    # to 'finished' (mirrors the stale-void paths). A walkover must never
    # grade as a win.
    home, away = "WO Echo", "WO Foxtrot"
    pick = await seed_tennis_pick(session, "evt-ten-wo", home, away, Market.H2H, home)
    book = ScoreBook([FinalScore(home, away, TENNIS_KICKOFF.date(), 0, 0, completion="void")])
    assert await settle_open_picks(session, book, NOW) == 1
    await session.refresh(pick)
    assert pick.status == "settled"
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == "void"
    assert row.pnl == Decimal("0.00")
    assert row.home_score is None
    assert row.away_score is None
    ev = await session.scalar(select(Event).where(Event.external_ref == "evt-ten-wo"))
    assert ev is not None
    assert ev.status == "scheduled"


async def test_tennis_game_line_pick_left_unsettled_from_set_score(session, caplog) -> None:  # type: ignore[no-untyped-def]
    # Reported + DB-confirmed 2026-07-10: game-line tennis totals/spreads picks
    # ("Over 22.5", "-4.5") were graded against the SET score (2-1 read as
    # "3 total, margin 1") — 106 mis-graded settled picks. The guard leaves
    # them OPEN for manual entry (never void, never guessed); set-plausible
    # lines on the same event still settle normally.
    home, away = "Guard Golf", "Guard Hotel"
    p_games = await seed_tennis_pick(
        session, "evt-ten-gameline", home, away, Market.TOTALS, "Over 22.5"
    )
    p_spread = await seed_tennis_pick(
        session, "evt-ten-gameline", home, away, Market.SPREADS, f"{home} -4.5"
    )
    p_sets = await seed_tennis_pick(
        session, "evt-ten-gameline", home, away, Market.TOTALS, "Over 2.5"
    )
    book = ScoreBook([FinalScore(home, away, TENNIS_KICKOFF.date(), 2, 1)])
    with caplog.at_level("INFO", logger="app.settlement.engine"):
        assert await settle_open_picks(session, book, NOW) == 1  # only the sets total
    await session.refresh(p_games)
    await session.refresh(p_spread)
    await session.refresh(p_sets)
    assert p_games.status == "alerted"  # left open — manual settlement only
    assert p_spread.status == "alerted"
    assert p_sets.status == "settled"
    for unsettled in (p_games, p_spread):
        row = await session.scalar(
            select(ResultTracking).where(ResultTracking.pick_id == unsettled.id)
        )
        assert row is None  # never a guessed (or void) result row
    sets_row = await session.scalar(
        select(ResultTracking).where(ResultTracking.pick_id == p_sets.id)
    )
    assert sets_row is not None
    assert sets_row.outcome == "won"  # 2+1 = 3 sets > 2.5
    summaries = [
        record.message
        for record in caplog.records
        if "settlement refusal summary" in record.message
    ]
    assert len(summaries) == 1
    assert "reason=tennis_game_line_set_score count=2" in summaries[0]
    assert f"sample_pick_ids={[p_games.id, p_spread.id]}" in summaries[0]
    assert not any("not settled: tennis" in record.message for record in caplog.records)


async def test_tennis_spread_axis_guard_defers_non_sets_details(session, caplog) -> None:  # type: ignore[no-untyped-def]
    # AXIS GUARD (audit 2026-08-02): a set-plausible |line| <= 2.5 is NOT
    # enough — game handicaps are quoted at -0.5/-1.5/-2.5 too and were graded
    # against 2-0/2-1 SET scores (spreads_minus_2_5: 0W/102L). Only a pick
    # whose market_detail proves the SETS axis may settle from a set score;
    # plain and NULL-detail spreads stay OPEN for manual entry.
    home, away = "Axis India", "Axis Juliett"
    p_plain = await seed_tennis_pick(
        session,
        "evt-ten-axis",
        home,
        away,
        Market.SPREADS,
        f"{home} -1.5",
        market_detail="spreads_minus_1_5",
    )
    # Distinct player pairs per event: same-pair-same-kickoff events are
    # cross-source-merged by the dedup resolver, which would fold these picks
    # onto one event and confound the per-pick assertions.
    null_home, null_away = "Axis Kebab", "Axis Lambda"
    p_null = await seed_tennis_pick(
        session, "evt-ten-axis-null", null_home, null_away, Market.SPREADS, f"{null_home} -2.5"
    )
    sets_home, sets_away = "Axis Mango", "Axis Nectar"
    p_sets = await seed_tennis_pick(
        session,
        "evt-ten-axis-sets",
        sets_home,
        sets_away,
        Market.SPREADS,
        f"{sets_home} -1.5",
        market_detail="spreads_sets_1_5",
    )
    book = ScoreBook(
        [
            FinalScore(home, away, TENNIS_KICKOFF.date(), 2, 0),
            FinalScore(null_home, null_away, TENNIS_KICKOFF.date(), 2, 0),
            FinalScore(sets_home, sets_away, TENNIS_KICKOFF.date(), 2, 0),
        ]
    )
    with caplog.at_level("INFO", logger="app.settlement.engine"):
        assert await settle_open_picks(session, book, NOW) == 1  # only the sets detail
    await session.refresh(p_plain)
    await session.refresh(p_null)
    await session.refresh(p_sets)
    assert p_plain.status == "alerted"  # deferred — game handicap on a set score
    assert p_null.status == "alerted"  # deferred — axis unprovable
    assert p_sets.status == "settled"
    for deferred in (p_plain, p_null):
        assert (
            await session.scalar(
                select(ResultTracking).where(ResultTracking.pick_id == deferred.id)
            )
            is None
        )
    sets_row = await session.scalar(
        select(ResultTracking).where(ResultTracking.pick_id == p_sets.id)
    )
    assert sets_row is not None
    assert sets_row.outcome == "won"  # -1.5 sets covered by the 2-0 sweep
    summaries = [
        record.message
        for record in caplog.records
        if "settlement refusal summary" in record.message
    ]
    assert len(summaries) == 1
    assert "reason=tennis_game_line_set_score count=2" in summaries[0]


async def test_manual_tennis_spread_axis_guard_defers_plain_detail(session, caplog) -> None:  # type: ignore[no-untyped-def]
    # The manual/direct settle path (settle_event_picks -> _settle_one) applies
    # the SAME axis refusal — a plain-detail set-plausible spread never grades
    # from a set score entered by hand either.
    home, away = "Axis Kilo", "Axis Lima"
    pick = await seed_tennis_pick(
        session,
        "evt-ten-axis-manual",
        home,
        away,
        Market.SPREADS,
        f"{home} -2.5",
        market_detail="spreads_minus_2_5",
    )
    with caplog.at_level("INFO", logger="app.settlement.engine"):
        assert await settle_event_picks(session, pick.event_id, 2, 0, NOW) == (0, 1)
    await session.refresh(pick)
    assert pick.status == "alerted"
    assert any("left open for manual settlement" in record.message for record in caplog.records)


async def test_scraped_finals_exclude_tennis_fail_closed(session) -> None:  # type: ignore[no-untyped-def]
    # _load_scraped_finals (audit 2026-08-02): a scraped tennis score carries
    # no completion info — it must NOT enter the ScoreBook as completion="full"
    # (a missed "ret." marker could grade markets that must VOID under
    # pinnacle_one_set). Non-tennis scraped finals keep flowing unchanged.
    from app.settlement.engine import _load_scraped_finals

    home, away = "Scrape Mike", "Scrape November"
    tennis_pick = await seed_tennis_pick(session, "evt-ten-scraped", home, away, Market.H2H, home)
    ev = await session.scalar(select(Event).where(Event.id == tennis_pick.event_id))
    ev.scraped_home_score = 2
    ev.scraped_away_score = 0
    soccer_pick = await seed_pick(
        session, "evt-soccer-scraped", home="Scrape Oscar FC", away="Scrape Papa FC"
    )
    sev = await session.scalar(select(Event).where(Event.id == soccer_pick.event_id))
    sev.scraped_home_score = 2
    sev.scraped_away_score = 1
    await session.flush()
    finals = await _load_scraped_finals(session, NOW)
    names = {(f.home_team, f.away_team) for f in finals}
    assert ("Scrape Oscar FC", "Scrape Papa FC") in names  # non-tennis unchanged
    assert (home, away) not in names  # tennis fail-closed: defer to ESPN/manual


async def test_manual_tennis_set_score_guard_remains_request_local(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Auto aggregation must not weaken the manual/direct _settle_one guard."""
    home, away = "Manual Guard Home", "Manual Guard Away"
    pick = await seed_tennis_pick(
        session,
        "evt-ten-manual-guard",
        home,
        away,
        Market.TOTALS,
        "Over 22.5",
    )
    with caplog.at_level("INFO", logger="app.settlement.engine"):
        assert await settle_event_picks(session, pick.event_id, 2, 1, NOW) == (0, 1)
    await session.refresh(pick)
    assert pick.status == "alerted"
    assert any("left open for manual settlement" in record.message for record in caplog.records)
    assert not any("settlement refusal summary" in record.message for record in caplog.records)


async def test_scoreless_and_tennis_refusals_skip_per_row_dedup_work(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No-score/manual-only backlog rows do one bulk hint query, not 2N locks."""
    from sqlalchemy import update as sa_update

    await session.execute(
        sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
    )
    scoreless: list[Pick] = []
    for index in range(6):
        scoreless.append(
            await seed_pick(
                session,
                f"evt-bulk-scoreless-{index}",
                home=f"Scoreless Home {index}",
                away=f"Scoreless Away {index}",
            )
        )

    manual: list[Pick] = []
    scores: list[FinalScore] = []
    for index in range(8):
        home = f"Manual Tennis Home {index}"
        away = f"Manual Tennis Away {index}"
        manual.append(
            await seed_tennis_pick(
                session,
                f"evt-bulk-tennis-{index}",
                home,
                away,
                Market.TOTALS,
                "Over 22.5",
            )
        )
        scores.append(FinalScore(home, away, TENNIS_KICKOFF.date(), 2, 1))

    async def unexpected_lock(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"deferred row acquired advisory lock: {args!r} {kwargs!r}")

    async def unexpected_recheck(*args: object, **kwargs: object) -> bool:
        raise AssertionError(f"deferred row ran exact sibling query: {args!r} {kwargs!r}")

    monkeypatch.setattr("app.settlement.engine._lock_settlement_instrument", unexpected_lock)
    monkeypatch.setattr("app.settlement.engine._settled_sibling_exists", unexpected_recheck)
    with caplog.at_level("INFO", logger="app.settlement.engine"):
        assert await settle_open_picks(session, ScoreBook(scores), NOW) == 0

    for pick in [*scoreless, *manual]:
        await session.refresh(pick)
        assert pick.status == "alerted"
    summaries = [
        record.message
        for record in caplog.records
        if "settlement refusal summary" in record.message
    ]
    assert len(summaries) == 1
    assert "reason=tennis_game_line_set_score count=8" in summaries[0]
    assert f"sample_pick_ids={[pick.id for pick in manual[:3]]}" in summaries[0]
    assert not any("not settled: tennis" in record.message for record in caplog.records)


async def test_scoreless_backlog_queries_are_one_scan_plus_bounded_bulk_chunks(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Query growth is O(ceil(N/chunk)), with no per-pick advisory/check query."""
    from sqlalchemy import update as sa_update

    await session.execute(
        sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
    )
    for index in range(9):
        await seed_pick(
            session,
            f"evt-query-count-{index}",
            home=f"Query Count Home {index}",
            away=f"Query Count Away {index}",
        )
    monkeypatch.setattr("app.settlement.engine._SETTLED_SIBLING_PREFETCH_CHUNK_SIZE", 4)

    statements: list[str] = []

    def count_statement(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(statement)

    connection = await session.connection()
    sa_event.listen(connection.sync_connection, "before_cursor_execute", count_statement)
    try:
        unrelated = ScoreBook([FinalScore("No Match Home", "No Match Away", KICKOFF.date(), 1, 0)])
        assert await settle_open_picks(session, unrelated, NOW) == 0
    finally:
        sa_event.remove(connection.sync_connection, "before_cursor_execute", count_statement)

    assert len(statements) == 4  # one open-row scan + ceil(9 / 4) bulk fingerprint queries
    assert sum("FROM result_tracking JOIN picks" in statement for statement in statements) == 3
    assert not any("pg_advisory_xact_lock" in statement for statement in statements)


@pytest.mark.parametrize(
    ("completion", "home_score", "away_score", "market", "selection", "outcome"),
    [
        ("retired", 1, 0, Market.TOTALS, "Over 22.5", "void"),
        ("void", 0, 0, Market.SPREADS, "Terminal Home -4.5", "void"),
        ("full", 12, 10, Market.TOTALS, "Over 20.5", "won"),
    ],
)
async def test_tennis_terminal_and_game_score_paths_bypass_auto_refusal(
    session: AsyncSession,
    completion: Completion,
    home_score: int,
    away_score: int,
    market: Market,
    selection: str,
    outcome: str,
) -> None:
    """Retirement/walkover remain gradeable; actual game totals are not refused."""
    home, away = "Terminal Home", "Terminal Away"
    event_id = f"evt-ten-terminal-{completion}-{market}"
    pick = await seed_tennis_pick(session, event_id, home, away, market, selection)
    book = ScoreBook(
        [
            FinalScore(
                home,
                away,
                TENNIS_KICKOFF.date(),
                home_score,
                away_score,
                completion=completion,
                winner_side="home" if completion == "retired" else None,
            )
        ]
    )
    assert await settle_open_picks(session, book, NOW) == 1
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == outcome


async def test_soccer_settlement_regression_unchanged_by_completion_fields(session) -> None:  # type: ignore[no-untyped-def]
    # Byte-identical regression: a provider that never sets the new fields
    # (all CSV/scraped/team-sport paths) settles exactly as before —
    # completion defaults to "full" and the score path is untouched.
    pick = await seed_pick(session, "evt-completion-default")
    assert await settle_open_picks(session, book_with_score(2, 1), NOW) == 1
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == "won"  # Over 2.5 with 3 goals — same as test_settles_past_pick
    assert row.pnl == Decimal("22.00")
    assert (row.home_score, row.away_score) == (2, 1)


# --- bounded no-result expiry + per-league provider-gap report (task SB) -------


def _unrelated_book(now: datetime) -> ScoreBook:
    """A NON-EMPTY book that matches none of the seeded fixtures — providers
    are alive (so the outage guard passes) but carry no candidate result."""
    return ScoreBook(
        [
            FinalScore(
                home_team="Unrelated Alpha",
                away_team="Unrelated Beta",
                match_date=now.date(),
                home_score=1,
                away_score=0,
            )
        ]
    )


async def test_expires_old_no_result_pick_as_void_with_note(session, caplog) -> None:  # type: ignore[no-untyped-def]
    # A 22-day-old alerted pick for which NO provider has any candidate result
    # expires: void, stake returned (pnl 0), note='expired_no_result_source'.
    from app.settlement.engine import (
        EXPIRED_NO_RESULT_NOTE,
        report_and_expire_no_result_picks,
    )

    now = datetime.now(tz=UTC)
    assert await persist_pick(
        session,
        make_pick("evt-expire-old"),
        EventTeams(home=HOME, away=AWAY, starts_at=now - timedelta(days=22)),
        "value",
        "test-v",
    )
    pick = await session.scalar(select(Pick).order_by(Pick.id.desc()))
    assert pick is not None
    with caplog.at_level("INFO"):
        expired = await report_and_expire_no_result_picks(
            session, _unrelated_book(now), now, expire_after=timedelta(days=21)
        )
    assert expired == 1
    await session.refresh(pick)
    assert pick.status == "settled"
    row = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
    assert row is not None
    assert row.outcome == "void"
    assert row.pnl == Decimal("0.00")
    assert row.note == EXPIRED_NO_RESULT_NOTE
    per_pick = [r for r in caplog.records if "expired pick" in r.getMessage()]
    assert len(per_pick) == 1
    # idempotent: a second run finds nothing alerted
    assert (
        await report_and_expire_no_result_picks(
            session, _unrelated_book(now), now, expire_after=timedelta(days=21)
        )
        == 0
    )


async def test_old_pick_with_gradeable_result_is_never_expired(session) -> None:  # type: ignore[no-untyped-def]
    # A candidate result exists (even if grading later fails/needs manual
    # settlement) -> the expiry policy must NEVER touch the pick.
    from app.settlement.engine import report_and_expire_no_result_picks

    now = datetime.now(tz=UTC)
    kickoff = now - timedelta(days=22)
    assert await persist_pick(
        session,
        make_pick("evt-expire-scored"),
        EventTeams(home=HOME, away=AWAY, starts_at=kickoff),
        "value",
        "test-v",
    )
    pick = await session.scalar(select(Pick).order_by(Pick.id.desc()))
    assert pick is not None
    book = ScoreBook(
        [
            FinalScore(
                home_team=HOME,
                away_team=AWAY,
                match_date=kickoff.date(),
                home_score=2,
                away_score=1,
            )
        ]
    )
    assert (
        await report_and_expire_no_result_picks(session, book, now, expire_after=timedelta(days=21))
        == 0
    )
    await session.refresh(pick)
    assert pick.status == "alerted"
    assert (
        await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
        is None
    )


async def test_expiry_disabled_still_reports_provider_gaps(session, caplog) -> None:  # type: ignore[no-untyped-def]
    # expire_after=None (settlement_expire_days=0) disables voiding, but the
    # per-league provider-gap INFO line still fires.
    from app.settlement.engine import report_and_expire_no_result_picks

    now = datetime.now(tz=UTC)
    assert await persist_pick(
        session,
        make_pick("evt-expire-disabled"),
        EventTeams(home=HOME, away=AWAY, starts_at=now - timedelta(days=22)),
        "value",
        "test-v",
    )
    pick = await session.scalar(select(Pick).order_by(Pick.id.desc()))
    assert pick is not None
    with caplog.at_level("INFO"):
        assert (
            await report_and_expire_no_result_picks(
                session, _unrelated_book(now), now, expire_after=None
            )
            == 0
        )
    await session.refresh(pick)
    assert pick.status == "alerted"
    reports = [r for r in caplog.records if "no result source" in r.getMessage()]
    assert len(reports) == 1


async def test_no_result_report_aggregates_by_sport_and_league(session, caplog) -> None:  # type: ignore[no-untyped-def]
    # Two no-result picks in one league + one in another -> a SINGLE INFO line
    # aggregated by (sport, league), largest gap first.
    from app.settlement.engine import report_and_expire_no_result_picks

    now = datetime.now(tz=UTC)
    # persist_pick derives the league row from PickOut.league (teams.league is
    # loader metadata), so override the PickOut for distinct leagues.
    assert await persist_pick(
        session,
        make_pick("evt-gap-a1").model_copy(update={"league": "gap-league-big"}),
        EventTeams(home=HOME, away=AWAY, starts_at=now - timedelta(days=2)),
        "value",
        "test-v",
    )
    assert await persist_pick(
        session,
        make_pick("evt-gap-a2", selection="Under 2.5").model_copy(
            update={"league": "gap-league-big"}
        ),
        EventTeams(home=HOME, away="Gap Other Away", starts_at=now - timedelta(days=3)),
        "value",
        "test-v",
    )
    assert await persist_pick(
        session,
        make_pick("evt-gap-b1").model_copy(update={"league": "gap-league-small"}),
        EventTeams(home="Gap Home C", away="Gap Away C", starts_at=now - timedelta(days=2)),
        "value",
        "test-v",
    )
    with caplog.at_level("INFO"):
        await report_and_expire_no_result_picks(
            session, _unrelated_book(now), now, expire_after=None
        )
    reports = [r for r in caplog.records if "no result source" in r.getMessage()]
    assert len(reports) == 1
    message = reports[0].getMessage()
    assert "3 picks past kickoff with no result source" in message
    assert "soccer/gap-league-big 2" in message
    assert "soccer/gap-league-small 1" in message
    assert message.index("gap-league-big") < message.index("gap-league-small")


async def test_empty_book_never_expires(session) -> None:  # type: ignore[no-untyped-def]
    # A total provider outage (empty book) must not mass-expire the backlog —
    # mirror of the silent-empty settle guard.
    from app.settlement.engine import report_and_expire_no_result_picks

    now = datetime.now(tz=UTC)
    assert await persist_pick(
        session,
        make_pick("evt-expire-outage"),
        EventTeams(home=HOME, away=AWAY, starts_at=now - timedelta(days=22)),
        "value",
        "test-v",
    )
    pick = await session.scalar(select(Pick).order_by(Pick.id.desc()))
    assert pick is not None
    assert (
        await report_and_expire_no_result_picks(
            session, ScoreBook([]), now, expire_after=timedelta(days=21)
        )
        == 0
    )
    await session.refresh(pick)
    assert pick.status == "alerted"


async def test_run_settlement_cycle_wires_expiry_from_settings(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Composition-root wiring: settlement_expire_days drives the expiry inside
    # run_settlement_cycle. 5 days (< the 15d hard void) proves THIS path fired.
    import app.config as app_config
    from app.settlement.engine import EXPIRED_NO_RESULT_NOTE, clear_feed_cache

    clear_feed_cache()
    async with factory() as session:
        assert await persist_pick(
            session,
            make_pick("evt-expire-cycle"),
            EventTeams(
                home="Cycle Gap Home", away="Cycle Gap Away", starts_at=NOW - timedelta(days=6)
            ),
            "value",
            "test-v",
        )
        await session.commit()

    settings = app_config.get_settings().model_copy(update={"settlement_expire_days": 5})
    monkeypatch.setattr(app_config, "get_settings", lambda: settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/new/BRA.csv"):
            return httpx.Response(200, text=CYCLE_CSV)
        return httpx.Response(404)

    with caplog.at_level("INFO"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_settlement_cycle(
                client, factory, slugs=["brazil-serie-a"], seasons=[], now=NOW
            )

    async with factory() as session:
        row = await session.scalar(
            select(ResultTracking)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .join(Event, Pick.event_id == Event.id)
            .where(Event.external_ref == "evt-expire-cycle")
        )
        assert row is not None
        assert row.outcome == "void"
        assert row.note == EXPIRED_NO_RESULT_NOTE
    clear_feed_cache()
