"""picks value_lost_at — premium value-lost transition timestamp

Operator item 2 (2026-08-04): "when premium lost its value it has to be
mentioned". Stamped by the revalidation loop when an ALERTED PREMIUM pick's
re-priced current_edge first crosses below its tier floor; cleared (NULL)
when a later re-price re-qualifies. Hysteresis is set/clear on the floor
crossing only — no additional flapping guard. Additive + nullable, no
backfill (historical picks stay NULL).

Revision ID: b7e1d4a9c3f6
Revises: d5a1c7f3e9b2
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e1d4a9c3f6"
down_revision: str | Sequence[str] | None = "d5a1c7f3e9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "picks",
        sa.Column("value_lost_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("picks", "value_lost_at")
