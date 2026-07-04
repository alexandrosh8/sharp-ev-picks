"""picks close_exclusion_reason column (A4, close-evidence package)

The untrusted-close rate was one opaque boolean (close_independent_of_fill).
This adds the SPECIFIC exclusion reason beside it — a closed vocabulary
(app/edge/value.py CLOSE_EXCLUSION_REASONS): 'fabricated' |
'circular_self_priced' | 'stale_echo' | 'tautological' |
'asymmetric_devig_fallback' — or 'trusted' when no guard trips. Stamped by
BOTH close writers (revalidate_open_picks and finalize_closing_from_snapshots
in app/clv_trueup.py) wherever close_independent_of_fill is written; counted
per reason under the /performance "clv_quality" payload.

ADDITIVE + nullable, following the close-provenance pattern (f6b2d8e4a1c3):
existing rows stay NULL (no backfill — provenance accrues forward) and nothing
gates or reclassifies on the reason, so headline / trusted CLV are unchanged.
Downgrade drops the column.

Revision ID: c9e4f2a7d5b3
Revises: a7d3c9e5f1b8
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9e4f2a7d5b3"
down_revision: str | Sequence[str] | None = "a7d3c9e5f1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("picks", sa.Column("close_exclusion_reason", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("picks", "close_exclusion_reason")
