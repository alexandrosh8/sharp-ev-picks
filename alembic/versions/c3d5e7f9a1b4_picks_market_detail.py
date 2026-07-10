"""picks — mint-time canonical market_detail (exact CLV close matching)

Additive-only: nullable TEXT column. New picks are stamped with the CANONICAL
devig-group detail at mint (app/pipeline.py::canonical_market_detail, e.g.
"totals_2_5", "asian_handicap_-1_0") so the CLV true-up matches the close on
the EXACT group instead of the line-blind (event, market, selection) key —
the key the cross-provider vocabulary collisions (live picks 62270/74637,
'asian_handicap_-1_0' vs 'spreads_minus_1') kept fail-closed every cycle.
NULL = lineless market (h2h/1x2/btts canonicalize to None) or a pre-column
row; those keep the legacy fail-closed behavior. No backfill — a mint-time
stamp cannot be reconstructed honestly after the fact. Downgrade drops it.

Revision ID: c3d5e7f9a1b4
Revises: a2f7d4c9e1b8
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d5e7f9a1b4"
down_revision: str | Sequence[str] | None = "a2f7d4c9e1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("picks", sa.Column("market_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("picks", "market_detail")
