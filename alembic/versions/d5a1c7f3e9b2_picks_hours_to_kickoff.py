"""picks hours_to_kickoff (mint-timing telemetry, observability first)

TASK G item 4 (2026-07-26): stamp how long before kickoff each pick was
minted — hours between the pick's created_at and the event's best-known
starts_at (positive = minted pre-kickoff), computed by the pipeline at mint
and written at INSERT only (a re-price/upgrade never rewrites the original
mint's timing). NULL = kickoff unknown at mint (never fabricated) or a
pre-column row.

Observability FIRST: nothing gates on the stored value today. The matching
ValuePolicy.premium_max_hours_to_kickoff ceiling ships INERT (math.inf /
Settings 0 = OFF) — a future config flip arms the already-implemented
demote-not-drop branch (named reason 'premium_mint_too_early') against the
forward evidence this column accrues. ADDITIVE + nullable, following the
anchor_book_count telemetry pattern: existing rows stay NULL (no backfill).
Downgrade drops the column.

Revision ID: d5a1c7f3e9b2
Revises: c4d8e2f6a1b3
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5a1c7f3e9b2"
down_revision: str | Sequence[str] | None = "c4d8e2f6a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # METRIC (Numeric(12, 6)) — the same type every other pick metric column
    # uses (app/storage/models.py); hours fit with sub-second resolution.
    op.add_column("picks", sa.Column("hours_to_kickoff", sa.Numeric(12, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("picks", "hours_to_kickoff")
