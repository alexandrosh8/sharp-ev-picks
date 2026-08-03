"""Games-level re-grade script (scripts/regrade_tennis_game_spreads.py).

Pure unit tests cover the exact-canonical resolution + completion classifier;
the DB-backed tests run against compose Postgres, rollback-isolated like
tests/test_restate_tennis_spread_grades.py (the script's ``regrade`` runs on an
already-begun transaction the fixture rolls back).

Covers:
- grading goes through the ENGINE path (_settle_one): half-line win/loss with
  orientation mapped by canonical name, integer-line PUSH (two-way handicap
  convention), commission-convention P&L, result provenance note;
- retirement rule: Retired -> VOID (partial games stored), Walkover -> VOID
  (NULL scores) — TENNIS_SETTLEMENT_CONVENTION pinnacle_one_set;
- selection: only picks anchored in settlement_restatements
  (reason set_score_axis_mislabel) and still 'alerted' are touched; unmatched
  picks stay 'alerted' with a reported reason;
- audit: one games_level_regrade row per changed column;
- INTERLOCK: a pick settled here is immune to the live set-score axis guard —
  feeding the same fixture's SET score back through settle_open_picks changes
  nothing (status != 'alerted' excludes it; uq_result_tracking_pick would
  block a duplicate row anyway).
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
from app.ingestion.tennis_data import TennisMatchRow
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.settlement.engine import settle_open_picks
from app.settlement.results import FinalScore, ScoreBook
from app.storage.models import Event, Pick, ResultTracking
from app.storage.repositories import persist_pick
from tests.database import TEST_DATABASE_URL

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "regrade_tennis_game_spreads.py"
_spec = importlib.util.spec_from_file_location("regrade_tennis_game_spreads", _SCRIPT)
assert _spec is not None and _spec.loader is not None
regrade_mod: Any = importlib.util.module_from_spec(_spec)
sys.modules["regrade_tennis_game_spreads"] = regrade_mod
_spec.loader.exec_module(regrade_mod)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
KICKOFF = NOW - timedelta(hours=48)


def _row(
    winner: str,
    loser: str,
    *,
    when: datetime = KICKOFF,
    games: tuple[int | None, int | None] = (12, 7),
    sets: tuple[int | None, int | None] = (2, 0),
    comment: str | None = "Completed",
) -> TennisMatchRow:
    return TennisMatchRow(
        match_date=datetime(when.year, when.month, when.day, tzinfo=UTC),
        tournament="Regrade Open",
        surface="Clay",
        round="QF",
        winner=winner,
        loser=loser,
        completed=(comment or "").casefold() == "completed",
        psw=None,
        psl=None,
        maxw=None,
        maxl=None,
        avgw=None,
        avgl=None,
        comment=comment,
        winner_games=games[0],
        loser_games=games[1],
        winner_sets=sets[0],
        loser_sets=sets[1],
    )


# --- pure resolution / classification --------------------------------------


def test_resolve_match_exact_canonical_and_orientation_agnostic() -> None:
    index = regrade_mod.build_result_index([_row("Alpha A.", "Beta B.")])
    # event stores full "Firstname Surname" forms; both orders resolve
    row, reason = regrade_mod.resolve_match(index, "Anna Alpha", "Betty Beta", KICKOFF.date())
    assert reason == "matched" and row is not None and row.winner == "Alpha A."
    row2, reason2 = regrade_mod.resolve_match(index, "Betty Beta", "Anna Alpha", KICKOFF.date())
    assert reason2 == "matched" and row2 is row


def test_resolve_match_adjacent_date_unique_only() -> None:
    d0 = KICKOFF.date()
    index = regrade_mod.build_result_index([_row("Alpha A.", "Beta B.", when=KICKOFF)])
    # +1 day off is tolerated when unique
    row, reason = regrade_mod.resolve_match(
        index, "Anna Alpha", "Betty Beta", d0 + timedelta(days=1)
    )
    assert reason == "matched" and row is not None
    # the same pair on BOTH adjacent dates (none on the exact date) -> refuse
    both = regrade_mod.build_result_index(
        [
            _row("Alpha A.", "Beta B.", when=KICKOFF - timedelta(days=1)),
            _row("Alpha A.", "Beta B.", when=KICKOFF + timedelta(days=1), games=(13, 10)),
        ]
    )
    row3, reason3 = regrade_mod.resolve_match(both, "Anna Alpha", "Betty Beta", d0)
    assert row3 is None and reason3 == "ambiguous_adjacent_dates"


def test_resolve_match_refusals() -> None:
    index = regrade_mod.build_result_index([_row("Alpha A.", "Beta B.")])
    d0 = KICKOFF.date()
    # different initial = different player (tennis initial veto via exact canonical)
    row, reason = regrade_mod.resolve_match(index, "Zoe Alpha", "Betty Beta", d0)
    assert row is None and reason == "no_counterpart_in_tennis_data"
    # degenerate: both sides collapse to one canonical name
    row2, reason2 = regrade_mod.resolve_match(index, "Anna Alpha", "Amy Alpha", d0)
    assert row2 is None and reason2 == "degenerate_canonical_names"
    # two DIFFERENT results for the pair on one date -> ambiguous
    dup = regrade_mod.build_result_index(
        [_row("Alpha A.", "Beta B."), _row("Beta B.", "Alpha A.", games=(13, 11))]
    )
    row3, reason3 = regrade_mod.resolve_match(dup, "Anna Alpha", "Betty Beta", d0)
    assert row3 is None and reason3 == "ambiguous_same_date"


def test_classify_completion_variants() -> None:
    assert regrade_mod.classify_completion(_row("A A.", "B B.")) == ("full", "matched")
    assert regrade_mod.classify_completion(_row("A A.", "B B.", comment="Retired")) == (
        "retired",
        "matched",
    )
    assert regrade_mod.classify_completion(
        _row("A A.", "B B.", comment="Walkover", games=(None, None), sets=(None, None))
    ) == ("void", "matched")
    completion, reason = regrade_mod.classify_completion(_row("A A.", "B B.", games=(None, None)))
    assert completion is None and reason == "missing_set_game_scores"
    completion, reason = regrade_mod.classify_completion(_row("A A.", "B B.", sets=(0, 2)))
    assert completion is None and reason == "inconsistent_sets_columns"
    completion, reason = regrade_mod.classify_completion(_row("A A.", "B B.", games=(2, 1)))
    assert completion is None and reason == "implausible_games_total"
    completion, reason = regrade_mod.classify_completion(_row("A A.", "B B.", comment=None))
    assert completion is None and reason == "no_completion_info"


# --- DB-backed engine-path grading ------------------------------------------


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


def _pick_out(event_id: str, home: str, away: str, **update: object) -> PickOut:
    base = PickOut(
        pick_id=f"p-{event_id}",
        sport="tennis",
        league="regrade-test",
        event=f"{home} vs {away}",
        event_id=event_id,
        market=Market.SPREADS,
        selection=f"{home} -1.5",
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
        reason_summary="regrade test",
        tier="premium",
        created_at=NOW - timedelta(hours=50),
    )
    return base.model_copy(update=update)


async def _seed_reopened(
    session: AsyncSession,
    event_id: str,
    home: str,
    away: str,
    *,
    selection: str | None = None,
    market_detail: str | None = "spreads_minus_1_5",
    anchored: bool = True,
) -> Pick:
    """A re-opened pick: status='alerted', no result row, optionally anchored
    in settlement_restatements under the set_score_axis_mislabel reason."""
    teams = EventTeams(home=home, away=away, league="regrade-test", starts_at=KICKOFF)
    out = _pick_out(
        event_id, home, away, market_detail=market_detail, selection=selection or f"{home} -1.5"
    )
    assert await persist_pick(session, out, teams, "value", "test-v")
    pick = await session.scalar(
        select(Pick)
        .join(Event, Pick.event_id == Event.id)
        .where(Event.external_ref == event_id)
        .order_by(Pick.id.desc())
    )
    assert pick is not None
    pick.status = "alerted"
    await session.flush()
    if anchored:
        await session.execute(text(regrade_mod._CREATE_AUDIT))
        await session.execute(
            text(
                f"INSERT INTO {regrade_mod.AUDIT_TABLE} "
                "(pick_id, column_name, old_value, new_value, reason) "
                "VALUES (:pid, 'picks.status', 'settled', 'alerted', :reason)"
            ),
            {"pid": pick.id, "reason": regrade_mod.SOURCE_REASON},
        )
    return pick


async def _result(session: AsyncSession, pick_id: int) -> ResultTracking | None:
    return await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick_id))


async def test_engine_path_grades_win_loss_and_orientation(conn_session) -> None:  # type: ignore[no-untyped-def]
    _conn, session = conn_session
    p_win = await _seed_reopened(
        session, "evt-rg-1", "Anna Alpha", "Betty Beta", selection="Anna Alpha -1.5"
    )
    p_loss = await _seed_reopened(
        session, "evt-rg-1b", "Cara Gamma", "Dana Delta", selection="Cara Gamma -1.5"
    )
    rows = [
        _row("Alpha A.", "Beta B.", games=(12, 7)),  # home wins by 5 -> -1.5 covers
        _row("Delta D.", "Gamma C.", games=(13, 11)),  # AWAY wins by 2 -> home -1.5 loses
    ]
    report = await regrade_mod.regrade(session, rows, NOW)
    assert report.targets == 2 and report.settled == 2
    assert report.outcomes == {"won": 1, "lost": 1}
    assert report.decided == 2 and report.wins == 1
    assert report.pnl_sum == Decimal("2.00")  # +22.00 (won @2.10, stake 20) - 20.00

    win_id, loss_id = p_win.id, p_loss.id
    session.expire_all()
    for pick_id, outcome, pnl, scores in (
        (win_id, "won", Decimal("22.00"), (12, 7)),
        (loss_id, "lost", Decimal("-20.00"), (11, 13)),
    ):
        pick = await session.get(Pick, pick_id)
        assert pick is not None and pick.status == "settled"
        rt = await _result(session, pick_id)
        assert rt is not None
        assert rt.outcome == outcome and rt.pnl == pnl
        assert (rt.home_score, rt.away_score) == scores
        assert rt.note == regrade_mod.RESULT_NOTE
        assert rt.settled_stake_amount == Decimal("20.00")
        assert rt.settled_effective_odds == Decimal("2.10")  # non-exchange book: gross


async def test_integer_line_pushes_two_way_convention(conn_session) -> None:  # type: ignore[no-untyped-def]
    _conn, session = conn_session
    p_push = await _seed_reopened(
        session,
        "evt-rg-2",
        "Ella Epsilon",
        "Zara Zeta",
        selection="Ella Epsilon -2",
        market_detail="spreads_minus_2",
    )
    report = await regrade_mod.regrade(
        session, [_row("Epsilon E.", "Zeta Z.", games=(12, 10))], NOW
    )
    assert report.settled == 1 and report.outcomes == {"push": 1}
    rt = await _result(session, p_push.id)
    assert rt is not None and rt.outcome == "push" and rt.pnl == Decimal("0.00")
    assert report.decided == 0  # push is not a decided outcome


async def test_retired_and_walkover_void_the_spread(conn_session) -> None:  # type: ignore[no-untyped-def]
    _conn, session = conn_session
    p_ret = await _seed_reopened(
        session, "evt-rg-3", "Ida Iota", "Kara Kappa", selection="Ida Iota -1.5"
    )
    p_wo = await _seed_reopened(
        session, "evt-rg-4", "Lena Lambda", "Mona Mu", selection="Lena Lambda -1.5"
    )
    rows = [
        _row("Iota I.", "Kappa K.", games=(8, 5), sets=(1, 0), comment="Retired"),
        _row("Mu M.", "Lambda L.", games=(None, None), sets=(None, None), comment="Walkover"),
    ]
    report = await regrade_mod.regrade(session, rows, NOW)
    assert report.settled == 2 and report.outcomes == {"void": 2}
    rt_ret = await _result(session, p_ret.id)
    assert rt_ret is not None and rt_ret.outcome == "void" and rt_ret.pnl == Decimal("0.00")
    assert (rt_ret.home_score, rt_ret.away_score) == (8, 5)  # games at stoppage, provenance
    rt_wo = await _result(session, p_wo.id)
    assert rt_wo is not None and rt_wo.outcome == "void"
    assert rt_wo.home_score is None and rt_wo.away_score is None  # walkover: no score


async def test_unmatched_and_unanchored_left_alone(conn_session) -> None:  # type: ignore[no-untyped-def]
    _conn, session = conn_session
    p_gap = await _seed_reopened(
        session, "evt-rg-5", "Nora Nu", "Orla Omicron", selection="Nora Nu -1.5"
    )
    # family-shaped but NEVER re-nulled (not in the audit anchor) -> not a target
    p_foreign = await _seed_reopened(
        session, "evt-rg-6", "Pia Pi", "Rhea Rho", selection="Pia Pi -1.5", anchored=False
    )
    report = await regrade_mod.regrade(session, [_row("Sana S.", "Tara T.")], NOW)
    assert report.targets == 1 and report.settled == 0
    assert report.unmatched == {"no_counterpart_in_tennis_data": 1}
    gap_id, foreign_id = p_gap.id, p_foreign.id
    session.expire_all()
    for pick_id in (gap_id, foreign_id):
        pick = await session.get(Pick, pick_id)
        assert pick is not None and pick.status == "alerted"
        assert await _result(session, pick_id) is None


async def test_audit_rows_written_per_column(conn_session) -> None:  # type: ignore[no-untyped-def]
    conn, session = conn_session
    p = await _seed_reopened(
        session, "evt-rg-7", "Uma Upsilon", "Vera Vau", selection="Uma Upsilon -1.5"
    )
    report = await regrade_mod.regrade(session, [_row("Upsilon U.", "Vau V.")], NOW)
    assert report.settled == 1
    audit = (
        await conn.execute(
            text(
                "SELECT column_name, old_value, new_value FROM settlement_restatements "
                "WHERE reason = :r AND pick_id = :p ORDER BY id"
            ),
            {"r": regrade_mod.REGRADE_REASON, "p": p.id},
        )
    ).all()
    by_column = {row.column_name: (row.old_value, row.new_value) for row in audit}
    assert by_column["picks.status"] == ("alerted", "settled")
    assert by_column["result_tracking.outcome"] == (None, "won")
    assert by_column["result_tracking.pnl"] == (None, "22.00")
    assert by_column["result_tracking.note"] == (None, regrade_mod.RESULT_NOTE)
    assert len(by_column) == 1 + len(regrade_mod._RT_AUDIT_COLUMNS)


async def test_interlock_settled_pick_immune_to_set_score_resettle(conn_session) -> None:  # type: ignore[no-untyped-def]
    _conn, session = conn_session
    p = await _seed_reopened(session, "evt-rg-8", "Wilma Wu", "Xena Xi", selection="Wilma Wu -1.5")
    report = await regrade_mod.regrade(session, [_row("Wu W.", "Xi X.", games=(12, 7))], NOW)
    assert report.settled == 1
    # The very failure mode the re-null fixed: a SET score for the same fixture
    # arriving through the automatic settler. The settled pick is not even a
    # candidate (status != 'alerted'); nothing changes.
    pick_id = p.id
    book = ScoreBook([FinalScore("Wilma Wu", "Xena Xi", KICKOFF.date(), 2, 0)])
    assert await settle_open_picks(session, book, NOW) == 0
    session.expire_all()
    pick = await session.get(Pick, pick_id)
    assert pick is not None and pick.status == "settled"
    rt = await _result(session, pick_id)
    assert rt is not None and rt.outcome == "won"
    assert (rt.home_score, rt.away_score) == (12, 7)  # the games score, untouched
