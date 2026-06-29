"""picks policy_fingerprint column — live policy regime stamped per pick (H3)

Picks carried only a static model_version ("v3"); the LIVE value-strategy policy
that produced each pick — value_min_edge / value_volume_min_edge / value_min_odds,
the devig method, require-sharp-anchor on/off, the data-error edge ceiling, and
the enforced ML value-filter manifest identity (created_utc @ q*) — was NOT
captured per row, so CLV attribution silently MIXED policy regimes across config
changes and a pick could not be replayed against the policy that minted it.

This adds a nullable TEXT column holding a compact, human-debuggable fingerprint
of that policy (app/pipeline.policy_fingerprint). ADDITIVE + nullable: existing
rows stay NULL (no backfill — the policy that minted them is not reconstructable),
the downgrade simply drops the column, and reads tolerate NULL everywhere.

Revision ID: c5e9a1f3b7d2
Revises: b4f2a9c83d1e
Create Date: 2026-06-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5e9a1f3b7d2"
down_revision: str | Sequence[str] | None = "b4f2a9c83d1e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "picks",
        sa.Column("policy_fingerprint", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("picks", "policy_fingerprint")
