"""picks close_independent_of_fill — fake-CLV independence guard (P0-1/P0-3)

A "sharp" close whose anchor book IS the pick's own fill book is CIRCULAR — the
pick's own book pricing its own close (closing == fill, |clv_log|~0). Counting
it as genuine CLV is the fake-CLV that masked the -EV. ``Pick.closing_anchor_type``
records WHICH KIND of book closed it (pinnacle/sharp/consensus) but not whether
that book is INDEPENDENT of the fill. This nullable boolean records exactly that:
True = close anchor book != fill book (genuine); False = close anchored by the
fill book itself (circular); NULL = no snapshot close yet / pre-column row. The
trusted sharp-CLV subset (n_sharp / sharp_stake_weighted_clv_log) excludes rows
where this is False. Additive + nullable — rows closed before this column stay
NULL (feature-detected by the read path, and never treated as circular).

Revision ID: e1a4d9c7b3f5
Revises: d3b8f1c7a9e2
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1a4d9c7b3f5"
down_revision: str | Sequence[str] | None = "d3b8f1c7a9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "picks",
        sa.Column("close_independent_of_fill", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("picks", "close_independent_of_fill")
