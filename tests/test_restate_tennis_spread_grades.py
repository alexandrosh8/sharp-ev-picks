"""Restatement script (scripts/restate_tennis_spread_grades.py) against compose
Postgres — rollback-isolated like tests/test_settlement_engine.py: every test
runs inside one outer transaction on ONE connection (the script's ``restate``
is written to run on an already-begun connection) and rolls back, so nothing
persists.

Covers (settlement audit 2026-08-02):
- selection: exactly the defective family — tennis spreads settled
  won/lost/half_* from a SET-score source with a non-sets/NULL market_detail;
- action: result_tracking row deleted (re-null), pick re-opened to 'alerted',
  one audit row per column in settlement_restatements;
- non-targets untouched: sets-detail grades, non-tennis spreads, games-score
  settlements, push/void rows;
- INTERLOCK: a re-nulled pick fed back through settle_open_picks with a set
  score is NOT re-settled — the axis-aware guard defers it (status stays
  'alerted', no result row). This is the invariant that makes the restatement
  safe to apply while the settler keeps running.
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
from app.settlement.engine import settle_open_picks
from app.settlement.results import FinalScore, ScoreBook
from app.storage.models import Event, Pick, ResultTracking
from app.storage.repositories import persist_pick
from tests.database import TEST_DATABASE_URL

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restate_tennis_spread_grades.py"
_spec = importlib.util.spec_from_file_location("restate_tennis_spread_grades", _SCRIPT)
assert _spec is not None and _spec.loader is not None
restate_mod: Any = importlib.util.module_from_spec(_spec)
sys.modules["restate_tennis_spread_grades"] = restate_mod
_spec.loader.exec_module(restate_mod)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
KICKOFF = NOW - timedelta(hours=48)


def _pick_out(event_id: str, home: str, away: str, sport: str, **update: object) -> PickOut:
    base = PickOut(
        pick_id=f"p-{event_id}",
        sport=sport,
        league="restate-test",
        event=f"{home} vs {away}",
        event_id=event_id,
        market=Market.SPREADS,
        selection=f"{home} -2.5",
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
        reason_summary="restate test",
        tier="premium",
        created_at=NOW - timedelta(hours=50),
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


async def _seed_settled(
    session: AsyncSession,
    event_id: str,
    home: str,
    away: str,
    *,
    sport: str = "tennis",
    selection: str | None = None,
    market_detail: str | None = None,
    outcome: str = "lost",
    home_score: int | None = 2,
    away_score: int | None = 0,
    status: str = "settled",
) -> Pick:
    teams = EventTeams(home=home, away=away, league="restate-test", starts_at=KICKOFF)
    out = _pick_out(
        event_id,
        home,
        away,
        sport,
        market_detail=market_detail,
        selection=selection or f"{home} -2.5",
    )
    assert await persist_pick(session, out, teams, "value", "test-v")
    pick = await session.scalar(
        select(Pick)
        .join(Event, Pick.event_id == Event.id)
        .where(Event.external_ref == event_id)
        .order_by(Pick.id.desc())
    )
    assert pick is not None
    pick.status = status
    session.add(
        ResultTracking(
            pick_id=pick.id,
            outcome=outcome,
            pnl=Decimal("-20.00") if outcome == "lost" else Decimal("22.00"),
            roi=Decimal("-1") if outcome == "lost" else Decimal("1.1"),
            settled_stake_amount=Decimal("20.00"),
            settled_effective_odds=Decimal("2.10"),
            home_score=home_score,
            away_score=away_score,
            settled_at=NOW - timedelta(hours=40),
        )
    )
    await session.flush()
    return pick


async def test_restatement_targets_only_the_defective_family_and_interlocks(  # type: ignore[no-untyped-def]
    conn_session,
) -> None:
    conn, session = conn_session
    # DEFECTIVE: plain-detail game handicap graded LOST from a 2-0 SET score.
    p_bad = await _seed_settled(
        session,
        "evt-rst-bad",
        "Rst Alpha",
        "Rst Beta",
        market_detail="spreads_minus_2_5",
    )
    # DEFECTIVE: NULL-detail (pre-vocabulary) spread graded from a set score.
    p_bad_null = await _seed_settled(
        session, "evt-rst-null", "Rst Gamma", "Rst Delta", market_detail=None, outcome="won"
    )
    # NOT targeted: sets-axis detail — a legitimate set-handicap grade.
    p_sets = await _seed_settled(
        session,
        "evt-rst-sets",
        "Rst Echo",
        "Rst Foxtrot",
        market_detail="spreads_sets_1_5",
        selection="Rst Echo -1.5",
    )
    # NOT targeted: non-tennis spread with a small (soccer) score.
    p_soccer = await _seed_settled(
        session,
        "evt-rst-soccer",
        "Rst Golf FC",
        "Rst Hotel FC",
        sport="soccer",
        market_detail="spreads_minus_2_5",
    )
    # NOT targeted: tennis spread settled from a GAMES-sized score (sum > 5).
    p_games = await _seed_settled(
        session,
        "evt-rst-games",
        "Rst India",
        "Rst Juliett",
        market_detail="spreads_minus_2_5",
        home_score=13,
        away_score=10,
    )
    # NOT targeted: push grade (not won/lost/half_*).
    p_push = await _seed_settled(
        session,
        "evt-rst-push",
        "Rst Kilo",
        "Rst Lima",
        market_detail="spreads_minus_2_5",
        outcome="push",
    )
    await session.flush()

    counts, breakdown = await restate_mod.restate(conn)
    by_label = dict(counts)
    assert by_label["family rows matched (defective set-score spread grades)"] == 2
    assert by_label["  of which NULL market_detail"] == 1
    assert by_label["result_tracking rows re-nulled (deleted)"] == 2
    assert by_label["picks re-opened: status 'settled' -> 'alerted'"] == 2
    # One audit row per column, 10 columns per pick.
    assert by_label["settlement_restatements audit rows written (one per column)"] == 20
    families = {family for family, _n, _wins in breakdown}
    assert families == {"spreads_minus_2_5", "<NULL>"}

    renull_ids = (p_bad.id, p_bad_null.id)
    untouched_ids = (p_sets.id, p_soccer.id, p_games.id, p_push.id)
    session.expire_all()
    for renulled_id in renull_ids:
        pick = await session.get(Pick, renulled_id)
        assert pick is not None
        assert pick.status == "alerted"
        assert (
            await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
            is None
        )
    for untouched_id in untouched_ids:
        pick = await session.get(Pick, untouched_id)
        assert pick is not None
        assert pick.status == "settled"
        assert (
            await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
            is not None
        )
    audit = (
        await conn.execute(
            text(
                "SELECT pick_id, column_name, old_value, new_value, reason "
                "FROM settlement_restatements WHERE reason = :r ORDER BY id"
            ),
            {"r": restate_mod.RESTATEMENT_REASON},
        )
    ).all()
    assert {row.pick_id for row in audit} == set(renull_ids)
    outcome_rows = {
        row.pick_id: row.old_value for row in audit if row.column_name == "result_tracking.outcome"
    }
    assert outcome_rows == {renull_ids[0]: "lost", renull_ids[1]: "won"}
    status_rows = [row for row in audit if row.column_name == "picks.status"]
    assert all(row.old_value == "settled" and row.new_value == "alerted" for row in status_rows)

    # INTERLOCK (docstring contract): feeding the re-nulled picks straight back
    # through the settler with their SET scores must NOT re-settle them — the
    # axis-aware guard defers (status stays 'alerted', no result row), so the
    # restatement cannot be undone wrongly by the very next settlement cycle.
    book = ScoreBook(
        [
            FinalScore("Rst Alpha", "Rst Beta", KICKOFF.date(), 2, 0),
            FinalScore("Rst Gamma", "Rst Delta", KICKOFF.date(), 2, 0),
        ]
    )
    assert await settle_open_picks(session, book, NOW) == 0
    renull_ids = (p_bad.id, p_bad_null.id)
    untouched_ids = (p_sets.id, p_soccer.id, p_games.id, p_push.id)
    session.expire_all()
    for renulled_id in renull_ids:
        pick = await session.get(Pick, renulled_id)
        assert pick is not None
        assert pick.status == "alerted"
        assert (
            await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
            is None
        )


async def test_restatement_reports_non_settled_family_rows_untouched(  # type: ignore[no-untyped-def]
    conn_session,
) -> None:
    conn, session = conn_session
    # A family row whose pick moved on (superseded) is REPORTED, never touched.
    p_sup = await _seed_settled(
        session,
        "evt-rst-sup",
        "Rst Mike",
        "Rst November",
        market_detail="spreads_minus_1_5",
        selection="Rst Mike -1.5",
        status="superseded",
    )
    await session.flush()
    sup_id = p_sup.id
    counts, _breakdown = await restate_mod.restate(conn)
    by_label = dict(counts)
    assert by_label["family rows matched (defective set-score spread grades)"] == 1
    assert by_label["  of which pick no longer 'settled' (reported, untouched)"] == 1
    assert by_label["result_tracking rows re-nulled (deleted)"] == 0
    assert by_label["picks re-opened: status 'settled' -> 'alerted'"] == 0
    session.expire_all()
    pick = await session.get(Pick, sup_id)
    assert pick is not None
    assert pick.status == "superseded"
    assert (
        await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
        is not None
    )
