"""picks steam shadow-verdict columns (A5, observability only)

The steam gate (app/edge/steam.py) was walk-forward tested 2026-06 and found
NOT to beat baseline OOS, so it ships OFF — which means zero live evidence
accrues about what it WOULD have decided. These four nullable columns persist
the gate's SHADOW verdict per pick at mint time (app/pipeline.py evaluates it
over the same inputs the real gate would see): steam_tripped (NULL = never
evaluated — gate unconfigured / consensus anchor / eval error / pre-column
row; False = evaluated clean; True = would demote), steam_reasons (comma-
joined SteamVerdict slugs), steam_closed_fraction and steam_anchor_age_seconds
(numeric detail, NULL when uncomputable — never fabricated).

Observability ONLY: nothing gates, demotes, filters, or reorders on these.
ADDITIVE + nullable, following the close-provenance pattern (f6b2d8e4a1c3):
existing rows stay NULL (no backfill — evidence accrues forward). Downgrade
drops the columns.

Revision ID: f8a3c5d7e9b1
Revises: c9e4f2a7d5b3
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f8a3c5d7e9b1"
down_revision: str | Sequence[str] | None = "c9e4f2a7d5b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("picks", sa.Column("steam_tripped", sa.Boolean(), nullable=True))
    op.add_column("picks", sa.Column("steam_reasons", sa.String(length=64), nullable=True))
    op.add_column("picks", sa.Column("steam_closed_fraction", sa.Numeric(12, 6), nullable=True))
    op.add_column("picks", sa.Column("steam_anchor_age_seconds", sa.Numeric(12, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("picks", "steam_anchor_age_seconds")
    op.drop_column("picks", "steam_closed_fraction")
    op.drop_column("picks", "steam_reasons")
    op.drop_column("picks", "steam_tripped")
