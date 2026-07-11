"""picks anchor_book_count (Task 6 anchor-thinness telemetry, log-only)

Community evidence (arbusers): where the sharp anchor's own market is THIN,
a large "edge vs anchor" is usually fake. Exchange liquidity is already
floored; this adds the missing telemetry for Pinnacle/consensus anchors: the
count of distinct NON-SHARP books quoting the pick's devig market group at
mint — exactly the number the thin-coverage floor (value_policy.
distinct_book_count, sharp set excluded) gates on, persisted per pick by the
value pipeline. The AGE half of anchor thinness is already carried by
steam_anchor_age_seconds (f8a3c5d7e9b1), so only the count is added.

Observability ONLY: nothing gates, demotes, filters, or reorders on it —
telemetry accrues until a walk-forward review defines a threshold (never
tuned on the spent holdout). ADDITIVE + nullable, following the steam-verdict
pattern: existing rows and model-strategy picks stay NULL (no backfill).
Downgrade drops the column.

Revision ID: a9d2c4e6f8b1
Revises: c3d5e7f9a1b4
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9d2c4e6f8b1"
down_revision: str | Sequence[str] | None = "c3d5e7f9a1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("picks", sa.Column("anchor_book_count", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("picks", "anchor_book_count")
