"""bankroll_ledger — hypothetical bankroll ledger (A8, informational only)

Minimal ledger behind the previously-placeholder snapshot_bankroll job: a
manual starting balance (BANKROLL_STARTING_BALANCE, ships OFF/unset) plus one
running settled-P&L row per settled pick. NO money movement, NO bet placement,
and NOT an input to live staking — the drawdown-constrained Kelly extensions
in app/risk/staking.py stay off until evaluated (ADR-0002 discipline).

UNIQUE(pick_id) (NULLs exempt) is the idempotency key: re-running the sync can
never double-append a settled pick. Downgrade drops the table.

Revision ID: b8e5d2f7a4c1
Revises: f8a3c5d7e9b1
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8e5d2f7a4c1"
down_revision: str | Sequence[str] | None = "f8a3c5d7e9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bankroll_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("pick_id", sa.BigInteger(), sa.ForeignKey("picks.id"), nullable=True),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("pick_id", name="uq_bankroll_ledger_pick"),
    )
    op.create_index("idx_bankroll_ledger_occurred", "bankroll_ledger", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("idx_bankroll_ledger_occurred", table_name="bankroll_ledger")
    op.drop_table("bankroll_ledger")
