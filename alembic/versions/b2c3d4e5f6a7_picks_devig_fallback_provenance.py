"""picks mint/close devig-fallback provenance columns (P2-2)

The trusted sharp-CLV subset must drop rows whose MINT and CLOSE fairs were
devigged ASYMMETRICALLY — one side's configured devig method fell back to
multiplicative (underround book / solver failure) while the other did not — so
a devig-method artifact cannot be counted as genuine Closing Line Value.

This adds two nullable BOOLEAN columns to picks recording whether the devig fell
back at mint time and at close time. ADDITIVE + nullable: existing rows stay
NULL (no backfill — provenance was not recorded for them) and NULL is treated as
symmetric (not excluded), so the headline/trusted CLV is unchanged until new
picks populate the flags. The downgrade drops both columns.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("picks", sa.Column("mint_devig_fell_back", sa.Boolean(), nullable=True))
    op.add_column("picks", sa.Column("close_devig_fell_back", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("picks", "close_devig_fell_back")
    op.drop_column("picks", "mint_devig_fell_back")
