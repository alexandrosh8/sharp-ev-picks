"""A8 bankroll-ledger sync — manual starting balance + running settled P&L.

Informational ONLY (picks-only platform, ADR-0002): the ledger tracks a
HYPOTHETICAL bankroll the operator seeded via BANKROLL_STARTING_BALANCE. It
never moves money, never places bets, and is deliberately NOT wired into live
staking — ``Settings.bankroll_base`` stays the recommended-stake basis and the
drawdown-constrained Kelly extensions stay OFF until evaluated.

P&L basis: ``result_tracking.pnl`` exactly as settlement computed it — the
user's actual logged stake when one exists, else the recommended
fractional-Kelly stake (app/settlement/engine._stake_and_odds). The ledger
never re-derives P&L, so it can never disagree with /performance.

Idempotent by construction: one ledger row per settled pick
(uq_bankroll_ledger_pick); each sync appends only picks not yet in the ledger,
in settlement order, so it is a catch-up that also absorbs manual settles.
Ships OFF: ``starting_balance is None`` (the default) writes nothing, ever.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.storage.models import BankrollLedgerEntry, ResultTracking

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

#: pg_advisory_xact_lock key: balance_after is a running total, so appends must
#: be serial even when the settle-cycle hook and the daily catch-up job overlap.
#: Arbitrary stable constant, unique to this ledger.
_LEDGER_LOCK_KEY = 0x_A8_BA_4C_01


async def sync_bankroll_ledger(
    session_factory: async_sessionmaker,
    *,
    starting_balance: float | None,
    now: datetime | None = None,
) -> int:
    """Append ledger entries for newly settled picks; returns rows appended
    (including the one-time starting-balance seed row).

    ``starting_balance is None`` = ledger INACTIVE (the shipped default):
    returns 0 immediately without touching the database — zero behavior change.
    """
    if starting_balance is None:
        return 0
    now = now or datetime.now(tz=UTC)
    appended = 0
    async with session_factory() as session:
        # Serialize concurrent syncs (transaction-scoped advisory lock).
        await session.execute(select(func.pg_advisory_xact_lock(_LEDGER_LOCK_KEY)))
        last = (
            await session.execute(
                select(BankrollLedgerEntry).order_by(BankrollLedgerEntry.id.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if last is None:
            balance = Decimal(str(starting_balance)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            session.add(
                BankrollLedgerEntry(
                    occurred_at=now,
                    entry_type="starting_balance",
                    amount=balance,
                    balance_after=balance,
                    note="manual starting balance (BANKROLL_STARTING_BALANCE)",
                )
            )
            await session.flush()
            appended += 1
        else:
            balance = last.balance_after
        # Settled picks with a P&L not yet in the ledger, in settlement order.
        already = select(BankrollLedgerEntry.pick_id).where(
            BankrollLedgerEntry.pick_id.is_not(None)
        )
        rows = (
            await session.execute(
                select(ResultTracking.pick_id, ResultTracking.pnl, ResultTracking.settled_at)
                .where(ResultTracking.pnl.is_not(None), ResultTracking.pick_id.not_in(already))
                .order_by(ResultTracking.settled_at, ResultTracking.id)
            )
        ).all()
        for pick_id, pnl, settled_at in rows:
            balance = balance + pnl
            inserted = await session.execute(
                pg_insert(BankrollLedgerEntry)
                .values(
                    occurred_at=settled_at,
                    entry_type="settled_pnl",
                    amount=pnl,
                    balance_after=balance,
                    pick_id=pick_id,
                    note="",
                )
                .on_conflict_do_nothing(constraint="uq_bankroll_ledger_pick")
                .returning(BankrollLedgerEntry.id)
            )
            if inserted.scalar_one_or_none() is None:
                balance = balance - pnl  # belt-and-braces: lost a race, don't advance
                continue
            appended += 1
        await session.commit()
    if appended:
        logger.info("bankroll ledger: %d entries appended", appended)
    return appended
