"""widen odds_snapshots.market VARCHAR(32) -> VARCHAR(64)

snapshot_market_key stored the provider submarket key in odds_snapshots.market,
clamped to the column's 32 chars. Real quarter-line handicap-games keys exceed
32 ("asian_handicap_games_-10_25_games" is 33): the clamp dropped the trailing
axis token, so two DISTINCT lines collapsed into one key — devig then pooled
them as a fake multi-leg book — and the reverse mapping mis-parsed the line.

Widen the column to 64 so no realistic key is ever truncated. A VARCHAR length
INCREASE is a catalog-only change in PostgreSQL (no table rewrite, no reindex),
safe on the large append-only odds_snapshots table. The clamp in
snapshot_market_key is widened to [:64] in lockstep. Already-truncated historical
rows are NOT rewritten (their original key is unrecoverable); this fixes forward
correctness only.

Revision ID: a1b2c3d4e5f6
Revises: c5e9a1f3b7d2
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "c5e9a1f3b7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "odds_snapshots",
        "market",
        existing_type=sa.String(32),
        type_=sa.String(64),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Narrowing back to 32 truncates any keys longer than 32; this only runs
    # before long keys are written. USING ::varchar(32) makes the cast explicit.
    op.alter_column(
        "odds_snapshots",
        "market",
        existing_type=sa.String(64),
        type_=sa.String(32),
        existing_nullable=False,
        postgresql_using="market::varchar(32)",
    )
