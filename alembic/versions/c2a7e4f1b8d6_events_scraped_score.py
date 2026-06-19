"""events scraped final score — best-effort scraped_home_score / scraped_away_score

OddsHarvester already surfaces a finished match's score in its match dict
(``home_score`` / ``away_score`` as strings); the OddsPortal adapter ignored
it. These two nullable Integer columns capture that best-effort score on the
event so the manual settle prompt can be PRE-FILLED and the CLOSED tab can show
a result hint. This is a CONVENIENCE pre-fill only — it never auto-settles; the
hybrid settlement path (soccer auto-settles from results feeds; NBA/euroleague
stay manual) is unchanged. Nullable + additive: the score is present only when
we actually scraped the match after it finished, and rows persisted before this
column stay NULL.

Revision ID: c2a7e4f1b8d6
Revises: b1d4f7a9c2e6
Create Date: 2026-06-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2a7e4f1b8d6"
down_revision: str | Sequence[str] | None = "b1d4f7a9c2e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("scraped_home_score", sa.Integer(), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("scraped_away_score", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("events", "scraped_away_score")
    op.drop_column("events", "scraped_home_score")
