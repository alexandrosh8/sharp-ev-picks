"""Withdrawal script (scripts/withdraw_artifact_picks.py) against compose
Postgres — rollback-isolated like tests/test_restate_tennis_spread_grades.py:
every test runs inside one outer transaction on ONE connection (the script's
``withdraw`` is written to run on an already-begun connection) and rolls
back, so nothing persists.

Covers (audit 2026-08-02, Defect 2):
- an 'alerted' audited pick flips to the canonical terminal 'superseded'
  status with ONE pick_withdrawals audit row carrying its defect reason;
- a pick with a result_tracking row (money already landed) is REFUSED;
- an already-terminal pick is skipped, a missing id is reported;
- idempotence: a second run withdraws nothing and writes no new audit rows;
- rows are never deleted.
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
from app.storage.models import Event, Pick, ResultTracking
from app.storage.repositories import persist_pick
from tests.database import TEST_DATABASE_URL

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "withdraw_artifact_picks.py"
_spec = importlib.util.spec_from_file_location("withdraw_artifact_picks", _SCRIPT)
assert _spec is not None and _spec.loader is not None
withdraw_mod: Any = importlib.util.module_from_spec(_spec)
sys.modules["withdraw_artifact_picks"] = withdraw_mod
_spec.loader.exec_module(withdraw_mod)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
KICKOFF = NOW + timedelta(hours=24)


def _pick_out(event_id: str, home: str, away: str) -> PickOut:
    return PickOut(
        pick_id=f"p-{event_id}",
        sport="soccer",
        league="withdraw-test",
        event=f"{home} vs {away}",
        event_id=event_id,
        market=Market.SPREADS,
        selection=f"{home} -1",
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
        reason_summary="withdraw test",
        tier="volume",
        created_at=NOW - timedelta(hours=1),
    )


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


async def _seed_pick(
    session: AsyncSession,
    event_id: str,
    home: str,
    away: str,
    *,
    status: str = "alerted",
    settled: bool = False,
) -> Pick:
    teams = EventTeams(home=home, away=away, league="withdraw-test", starts_at=KICKOFF)
    assert await persist_pick(session, _pick_out(event_id, home, away), teams, "value", "test-v")
    pick = await session.scalar(
        select(Pick)
        .join(Event, Pick.event_id == Event.id)
        .where(Event.external_ref == event_id)
        .order_by(Pick.id.desc())
    )
    assert pick is not None
    pick.status = status
    if settled:
        session.add(
            ResultTracking(
                pick_id=pick.id,
                outcome="lost",
                pnl=Decimal("-20.00"),
                roi=Decimal("-1"),
                settled_stake_amount=Decimal("20.00"),
                settled_effective_odds=Decimal("2.10"),
                settled_at=NOW - timedelta(minutes=30),
            )
        )
    await session.flush()
    return pick


async def test_withdrawal_transitions_audits_and_refuses(  # type: ignore[no-untyped-def]
    conn_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, session = conn_session
    # TARGET: open pick — must flip to 'superseded' with an audit row.
    p_open = await _seed_pick(session, "evt-wdr-open", "Wdr Alpha", "Wdr Beta")
    # REFUSED: money already landed (result_tracking row exists).
    p_settled = await _seed_pick(
        session, "evt-wdr-settled", "Wdr Gamma", "Wdr Delta", status="settled", settled=True
    )
    # SKIPPED: already terminal.
    p_term = await _seed_pick(
        session, "evt-wdr-term", "Wdr Echo", "Wdr Foxtrot", status="superseded"
    )
    open_id, settled_id, term_id = p_open.id, p_settled.id, p_term.id
    missing_id = 999_999_901
    monkeypatch.setattr(
        withdraw_mod,
        "WITHDRAWAL_REASONS",
        {
            open_id: "product_mismatch_artifact",
            settled_id: "stale_quote_artifact",
            term_id: "product_mismatch_artifact",
            missing_id: "stale_quote_artifact",
        },
    )

    lines, withdrawn = await withdraw_mod.withdraw(conn)
    assert withdrawn == 1
    joined = "\n".join(lines)
    assert f"pick {missing_id}: NOT FOUND" in joined
    assert "REFUSED" in joined.upper()
    assert "already terminal" in joined

    session.expire_all()
    # Canonical terminal transition — bare status flip, row NOT deleted.
    open_after = await session.get(Pick, open_id)
    assert open_after is not None
    assert open_after.status == "superseded"
    # Refused/skipped rows untouched.
    settled_after = await session.get(Pick, settled_id)
    assert settled_after is not None and settled_after.status == "settled"
    term_after = await session.get(Pick, term_id)
    assert term_after is not None and term_after.status == "superseded"
    # Exactly one audit row, for the withdrawn pick, old/new/reason preserved.
    audit = (
        await conn.execute(
            text(
                "SELECT pick_id, column_name, old_value, new_value, reason "
                "FROM pick_withdrawals ORDER BY id"
            )
        )
    ).all()
    assert [tuple(r) for r in audit] == [
        (open_id, "picks.status", "alerted", "superseded", "product_mismatch_artifact")
    ]

    # Idempotence: a second run finds nothing 'alerted' — no-op, no new audit.
    lines2, withdrawn2 = await withdraw_mod.withdraw(conn)
    assert withdrawn2 == 0
    assert any("already terminal" in line for line in lines2)
    audit_count = await conn.scalar(text("SELECT count(*) FROM pick_withdrawals"))
    assert int(audit_count or 0) == 1
