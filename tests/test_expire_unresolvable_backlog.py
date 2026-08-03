"""scripts/expire_unresolvable_backlog.py against compose Postgres —
rollback-isolated like tests/test_restate_tennis_spread_grades.py: every test
runs inside one outer transaction on ONE connection (the script's
``expire_backlog`` runs on an already-begun session) and rolls back.

The script must add NO policy of its own: eligibility is exactly the 8d67b61
bounded gap-expiry rule, reused by calling the engine function itself
(``report_and_expire_no_result_picks`` — book-gated; the cycle-only 15d hard
void is deliberately not invoked, see the script docstring). Covers:

- a past-bound pick with NO candidate result is voided with
  note='expired_no_result_source', pick 'settled', audit rows written;
- a past-bound pick WITH a candidate result in the union book is never
  touched (grading paths own it);
- an in-window pick (younger than every bound) is never touched and is
  reported as remaining-open;
- the operator scope carve-out: tennis market='spreads' picks whose
  market_detail does not prove the SETS axis are NEVER voided by this script
  (the separate re-grade task owns them) — even when the engine rules would
  expire them — while sets-axis tennis spreads expire normally;
- an EMPTY union book aborts (outage must not look like a quiet day).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.settlement.engine import EXPIRED_NO_RESULT_NOTE
from app.settlement.results import FinalScore, ScoreBook
from app.storage.models import Event, Pick, ResultTracking
from app.storage.repositories import persist_pick
from tests.database import TEST_DATABASE_URL

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "expire_unresolvable_backlog.py"
_spec = importlib.util.spec_from_file_location("expire_unresolvable_backlog", _SCRIPT)
assert _spec is not None and _spec.loader is not None
expire_mod: Any = importlib.util.module_from_spec(_spec)
sys.modules["expire_unresolvable_backlog"] = expire_mod
_spec.loader.exec_module(expire_mod)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
PAST_BOUND = NOW - timedelta(days=22)  # older than both the 15d void and 21d expiry
IN_WINDOW = NOW - timedelta(days=5)
EXPIRE_AFTER = timedelta(days=21)


def _pick_out(
    event_id: str,
    home: str,
    away: str,
    sport: str,
    league: str,
    **update: object,
) -> PickOut:
    base = PickOut(
        pick_id=f"p-{event_id}",
        sport=sport,
        league=league,
        event=f"{home} vs {away}",
        event_id=event_id,
        market=Market.H2H,
        selection=home,
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
        reason_summary="expiry-backlog test",
        tier="premium",
        created_at=NOW - timedelta(days=23),
    )
    return base.model_copy(update=update)


@pytest.fixture
async def conn_session():  # type: ignore[no-untyped-def]
    """(connection, session) sharing ONE outer transaction, rolled back."""
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield conn, session
        finally:
            await session.close()
            await trans.rollback()
    await engine.dispose()


async def _seed_alerted(
    session: AsyncSession,
    event_id: str,
    home: str,
    away: str,
    *,
    sport: str = "soccer",
    league: str = "expiry-test-league",
    starts_at: datetime = PAST_BOUND,
    **update: object,
) -> Pick:
    teams = EventTeams(home=home, away=away, league=league, starts_at=starts_at)
    out = _pick_out(event_id, home, away, sport, league, **update)
    assert await persist_pick(session, out, teams, "value", "test-v")
    pick = await session.scalar(
        select(Pick)
        .join(Event, Pick.event_id == Event.id)
        .where(Event.external_ref == event_id)
        .order_by(Pick.id.desc())
    )
    assert pick is not None
    assert pick.status == "alerted"
    await session.flush()
    return pick


def _book(*scores: FinalScore) -> ScoreBook:
    return ScoreBook(list(scores))


async def _result_row(session: AsyncSession, pick_id: int) -> ResultTracking | None:
    return await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick_id))


async def test_expires_exactly_the_engine_rule_set(conn_session) -> None:  # type: ignore[no-untyped-def]
    _, session = conn_session
    gap = await _seed_alerted(session, "exp-gap", "Alpha FC", "Beta FC")
    has_result = await _seed_alerted(session, "exp-hasres", "Gamma FC", "Delta FC")
    in_window = await _seed_alerted(
        session, "exp-young", "Epsilon FC", "Zeta FC", starts_at=IN_WINDOW
    )
    book = _book(
        FinalScore(
            home_team="Gamma FC",
            away_team="Delta FC",
            match_date=PAST_BOUND.date(),
            home_score=1,
            away_score=0,
        )
    )
    report = await expire_mod.expire_backlog(session, book, NOW, expire_after=EXPIRE_AFTER)

    await session.refresh(gap)
    await session.refresh(has_result)
    await session.refresh(in_window)
    assert gap.status == "settled"
    row = await _result_row(session, gap.id)
    assert row is not None
    assert row.outcome == "void"
    assert row.note == EXPIRED_NO_RESULT_NOTE
    assert row.pnl == Decimal("0.00")
    # candidate-result and in-window picks are untouched
    assert has_result.status == "alerted"
    assert await _result_row(session, has_result.id) is None
    assert in_window.status == "alerted"
    assert await _result_row(session, in_window.id) is None

    assert report.expired_total == 1
    assert ("soccer", "expiry-test-league", 1) in report.expired_by_league
    remaining = dict(((sport, league), n) for sport, league, n in report.remaining_by_league)
    # both untouched picks are past kickoff and still open
    assert remaining[("soccer", "expiry-test-league")] == 2

    audit = (
        await session.execute(
            text(
                "SELECT column_name, old_value, new_value, reason "
                "FROM settlement_restatements WHERE pick_id = :pid ORDER BY column_name"
            ),
            {"pid": gap.id},
        )
    ).all()
    assert report.audit_rows == len(audit) > 0
    reasons = {r for *_, r in audit}
    assert reasons == {expire_mod.RESTATEMENT_REASON}
    by_col = {c: (o, n) for c, o, n, _ in audit}
    assert by_col["picks.status"] == ("alerted", "settled")
    assert by_col["result_tracking.outcome"] == (None, "void")
    assert by_col["result_tracking.note"] == (None, EXPIRED_NO_RESULT_NOTE)


async def test_tennis_game_spread_carveout_never_voided(conn_session) -> None:  # type: ignore[no-untyped-def]
    _, session = conn_session
    game_spread = await _seed_alerted(
        session,
        "exp-ten-game",
        "Player A",
        "Player B",
        sport="tennis",
        league="ATP Test",
        market=Market.SPREADS,
        selection="Player A -1.5",
        market_detail="spreads_minus_1_5",
    )
    sets_spread = await _seed_alerted(
        session,
        "exp-ten-sets",
        "Player C",
        "Player D",
        sport="tennis",
        league="ATP Test",
        market=Market.SPREADS,
        selection="Player C -1.5",
        market_detail="spreads_sets_minus_1_5",
    )
    # unrelated score so the union book is non-empty (empty book = outage guard)
    book = _book(
        FinalScore(
            home_team="Someone",
            away_team="Else",
            match_date=PAST_BOUND.date(),
            home_score=2,
            away_score=0,
        )
    )
    report = await expire_mod.expire_backlog(session, book, NOW, expire_after=EXPIRE_AFTER)

    await session.refresh(game_spread)
    await session.refresh(sets_spread)
    # carve-out: the game-axis spread stays open for the re-grade task
    assert game_spread.status == "alerted"
    assert await _result_row(session, game_spread.id) is None
    audit_n = await session.scalar(
        text("SELECT count(*) FROM settlement_restatements WHERE pick_id = :pid"),
        {"pid": game_spread.id},
    )
    assert audit_n == 0
    # sets-axis spread follows the normal engine rules
    assert sets_spread.status == "settled"
    sets_row = await _result_row(session, sets_spread.id)
    assert sets_row is not None
    assert sets_row.note == EXPIRED_NO_RESULT_NOTE
    assert report.skipped_tennis_game_spreads == 1
    assert report.expired_total == 1


async def test_empty_union_book_aborts(conn_session) -> None:  # type: ignore[no-untyped-def]
    _, session = conn_session
    pick = await _seed_alerted(session, "exp-outage", "Eta FC", "Theta FC")
    with pytest.raises(expire_mod.EmptyBookError):
        await expire_mod.expire_backlog(session, _book(), NOW, expire_after=EXPIRE_AFTER)
    await session.refresh(pick)
    assert pick.status == "alerted"
    assert await _result_row(session, pick.id) is None
