"""result_tracking.note — settlement-provenance note for policy voids.

The bounded no-result expiry (settlement_expire_days) settles provider-gap
picks as VOID; the note ('expired_no_result_source') keeps those policy voids
distinguishable from score-based results. Additive + nullable: existing rows
stay NULL.

Revision ID: c4d8e2f6a1b3
Revises: e7f1a9c3b5d2
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4d8e2f6a1b3"
down_revision: str | Sequence[str] | None = "e7f1a9c3b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("result_tracking", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("result_tracking", "note")
