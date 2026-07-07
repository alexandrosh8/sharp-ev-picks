"""candidate_evaluations — per-candidate/rejection audit trail (external-audit #3)

Additive-only: one new table recording, per candidate evaluation per cycle, the
decision tier (premium kept / volume demoted), the demotion/rejection reason
slugs, and the anchor/fill provenance behind the decision. Pure measurement
infrastructure for later ROI diagnosis — never gates minting, never writes a
pick. Standalone tap (no FK to model_versions/model_predictions); the RESERVED
DetectedEdge table is left untouched. Idempotent by (event, market,
market_detail, selection, evaluated_at) — the per-cycle timestamp is the
discriminator; market_detail is NOT NULL ('' default) so NULLs cannot defeat the
dedup. Downgrade drops the table.

Revision ID: d1e2f3a4b5c6
Revises: b8e5d2f7a4c1
Create Date: 2026-07-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | Sequence[str] | None = "b8e5d2f7a4c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_evaluations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.BigInteger(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("sport_key", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=64), nullable=False),
        sa.Column("market_detail", sa.String(length=64), server_default="", nullable=False),
        sa.Column("selection", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("anchor_book", sa.String(length=64), nullable=True),
        sa.Column("anchor_type", sa.String(length=16), nullable=True),
        sa.Column("anchor_age_seconds", sa.Numeric(12, 6), nullable=True),
        sa.Column("anchor_liquidity", sa.Numeric(12, 2), nullable=True),
        sa.Column("best_book", sa.String(length=64), nullable=True),
        sa.Column("best_odds", sa.Numeric(10, 4), nullable=True),
        sa.Column("edge", sa.Numeric(12, 6), nullable=True),
        sa.Column("fair_probability", sa.Numeric(8, 6), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "event_id",
            "market",
            "market_detail",
            "selection",
            "evaluated_at",
            name="uq_candidate_evaluations_cycle",
        ),
    )
    op.create_index(
        "idx_candidate_evaluations_event",
        "candidate_evaluations",
        ["event_id", "evaluated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_candidate_evaluations_event", table_name="candidate_evaluations")
    op.drop_table("candidate_evaluations")
