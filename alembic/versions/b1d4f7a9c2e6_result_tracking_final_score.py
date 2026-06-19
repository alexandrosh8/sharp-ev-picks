"""result_tracking final score — home_score / away_score of the settled game

Settlement already KNOWS the final score (``_settle_one`` receives it), but it
was never persisted: result_tracking carried only outcome/pnl/roi/settled_at.
These two nullable Integer columns store the game's final score (HOME first) so
the dashboard SETTLED view can show "2-1" alongside the recorded result.
Nullable + additive: void settlements may carry no score, and rows persisted
before this column stay NULL.

Revision ID: b1d4f7a9c2e6
Revises: a7e3c1b9f204
Create Date: 2026-06-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1d4f7a9c2e6"
down_revision: str | Sequence[str] | None = "a7e3c1b9f204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "result_tracking",
        sa.Column("home_score", sa.Integer(), nullable=True),
    )
    op.add_column(
        "result_tracking",
        sa.Column("away_score", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("result_tracking", "away_score")
    op.drop_column("result_tracking", "home_score")
