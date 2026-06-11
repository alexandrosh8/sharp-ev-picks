"""picks revalidation attempted at — off-window round-robin keys on attempts

revalidated_at stays success-only (dashboard "verified" badge);
revalidation_attempted_at advances on every match-page fetch so dead links
rotate to the back of the off-window queue instead of starving it.

Revision ID: e808241b136f
Revises: a3c9d1e7b2f4
Create Date: 2026-06-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e808241b136f"
down_revision: str | Sequence[str] | None = "a3c9d1e7b2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "picks",
        sa.Column("revalidation_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("picks", "revalidation_attempted_at")
