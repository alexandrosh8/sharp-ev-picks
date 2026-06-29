"""Settlement engine against compose Postgres (rollback-isolated; skips
when the DB is absent) — mirrors tests/test_persistence.py."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.settlement.engine import (
    run_settlement_cycle,
    settle_event_picks,
    settle_open_picks,
)
from app.settlement.results import FinalScore, ScoreBook
from app.storage.models import Event, ManualBetLog, Pick, ResultTracking
from app.storage.repositories import persist_pick

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"

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


async def seed_pick(session, event_id: str, **kwargs) -> Pick:  # type: ignore[no-untyped-def]
    teams = EventTeams(home=HOME, away=AWAY, league="test-league-settlement", starts_at=KICKOFF)
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
        EventTeams(home=HOME, away=AWAY, starts_at=now - old),
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
    # idempotent
    assert await void_unsettleable_known_kickoff_picks(session, now) == 0


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


async def test_performance_report_aggregates(session) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from app.storage.repositories import performance_report

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
    won.closing_odds = Decimal("2.1000")
    lost = await seed_pick(session, "evt-perf-2")
    lost.clv_log = Decimal("-0.01")
    lost.beat_close = False
    # The lost pick has NO sharp close (no closing_odds / anchor) -> excluded
    # from the trusted sharp-close headline below.
    book = ScoreBook(
        [
            FinalScore(HOME, AWAY, KICKOFF.date(), 2, 1),  # only evt-perf-1's event? no —
        ]
    )
    # Both picks share team names/date, so both settle from one score (2-1):
    # but evt-perf-2 needs a loss -> settle it manually as 1-0 first.
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
    volume = await seed_pick(session, "evt-perf-tier-v", tier="volume")
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
