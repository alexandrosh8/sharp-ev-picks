"""picks has_snapshot_close + anchor_book — unblock CLV findings clv-1 / CLV-3

Two additive, nullable columns on ``picks``:

- ``has_snapshot_close`` (Boolean) — True whenever
  ``clv_trueup.finalize_closing_from_snapshots`` anchored a close fair from our
  own ``odds_snapshots`` history, INDEPENDENT of whether a SOFT book also priced
  the selection (``closing_odds``). When only sharp books quote the close,
  ``closing_odds`` stays NULL yet the close fair is real; deriving the snapshot-
  close flag from ``closing_odds IS NOT NULL`` then false-negatives those rows
  (finding clv-1). NULL = no snapshot close computed yet / pre-column row.
- ``anchor_book`` (String) — the pick-time sharp anchor BOOK NAME (the concrete
  book behind ``anchor_type``, which collapses every named sharp book to
  'sharp'). Keeps the actual book so per-book anchor analysis (finding CLV-3) is
  possible without re-deriving it. NULL = model-strategy pick or pre-column row.

Additive + nullable — no backfill required; rows written before these columns
stay NULL and are feature-detected by the read paths.

Revision ID: 2d37faf2d3fd
Revises: e1a4d9c7b3f5
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2d37faf2d3fd"
down_revision: str | Sequence[str] | None = "e1a4d9c7b3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "picks",
        sa.Column("has_snapshot_close", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "picks",
        sa.Column("anchor_book", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("picks", "anchor_book")
    op.drop_column("picks", "has_snapshot_close")
