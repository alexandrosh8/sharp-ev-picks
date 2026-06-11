"""events.starts_at nullable — NULL is the real "kickoff unknown" signal

Events whose source never reported a kickoff used to store the pick's own
created_at as a placeholder; the dashboard could not distinguish that from a
real kickoff (Pick.created_at was a separate clock read), so TBD games
rendered as started matches with live settle buttons. NULL is unambiguous:
the dashboard shows "kickoff TBD" (no countdown, no settle) and the kickoff
refresh upgrades the row once a scrape reports the true start.

Legacy placeholder rows are left as-is — they are indistinguishable from
real kickoffs by construction and self-heal via refresh_event_kickoffs.

Revision ID: f3a1c2d4e5b6
Revises: e808241b136f
Create Date: 2026-06-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a1c2d4e5b6"
down_revision: str | Sequence[str] | None = "e808241b136f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "events",
        "starts_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )


def downgrade() -> None:
    # Restore NOT NULL: backfill unknown kickoffs with the row's created_at
    # (the legacy placeholder semantics) so the constraint can be re-applied.
    op.execute("UPDATE events SET starts_at = created_at WHERE starts_at IS NULL")
    op.alter_column(
        "events",
        "starts_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
