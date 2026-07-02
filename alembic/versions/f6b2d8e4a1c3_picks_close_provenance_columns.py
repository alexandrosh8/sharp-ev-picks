"""picks close-provenance columns (D3, close-evidence package)

The CLV tautology audit (2026-07) showed ~45% of settled picks carry a close
fair identical to the pick-time fair. Without persisted close provenance the
guard cannot tell an ECHO of the mint anchor row (close_snapshot_captured_at
<= created_at — fake close evidence) from a FRESH observation of a genuinely
unmoved line (> created_at — an honest "line didn't move" close).

This adds two nullable columns to picks, stamped by BOTH close writers
(revalidate_open_picks and finalize_closing_from_snapshots):

- close_anchor_book: the concrete book that anchored the close fair (e.g.
  'Pinnacle', 'Betfair Exchange', or the consensus sentinel) — the close-side
  twin of the mint-side anchor_book.
- close_snapshot_captured_at: the capture time of the anchor rows behind the
  close fair — what finally separates echo from fresh-but-unmoved.

ADDITIVE + nullable: existing rows stay NULL (no backfill — provenance was not
recorded for them) and NO aggregate changes behavior on NULL, so headline /
trusted CLV are unchanged until new closes populate the columns. The
tautology guard itself is deliberately NOT changed here (reclassification is
deferred to single-shot validation, ADR-0019 discipline). Downgrade drops both.

Revision ID: f6b2d8e4a1c3
Revises: e4f5a6b7c8d9
Create Date: 2026-07-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6b2d8e4a1c3"
down_revision: str | Sequence[str] | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("picks", sa.Column("close_anchor_book", sa.String(length=64), nullable=True))
    op.add_column(
        "picks",
        sa.Column("close_snapshot_captured_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("picks", "close_snapshot_captured_at")
    op.drop_column("picks", "close_anchor_book")
