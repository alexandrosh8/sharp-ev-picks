"""betfair_anchor_verdicts + picks.anchor_staleness_decision (staleness guard, P3/P4)

The Betfair-API compare cycle already measures inline-scrape vs fresh-API
disagreement per selection (live 2026-07-02 evidence: api_fresher=100%,
freshness gaps 2.3-9.1h, ~40% of compared selections > 1 tick apart). This
adds the ONE table the staleness guard needs:

- betfair_anchor_verdicts: keep-latest-per-(event_ref, market, selection_role)
  upsert of each compared selection's verdict (inline price, API price, API
  best-back size, tick distance at the coarser price, both capture times, and
  the write-time decision pass|demote|no_api_match|no_api_price). stale_api is
  a READ-time classification (age > VALUE_BETFAIR_STALENESS_VERDICT_TTL) and
  is never stored. A retention sweep in the sink keeps the table at ~slate
  size. The row is simultaneously the compare persistence, the mint-time
  input (verdict_loader — mint NEVER calls the Betfair API), and the
  provenance/diagnostic record.

- picks.anchor_staleness_decision (TEXT NULL): observability-only mint stamp
  of the effective verdict the guard read for the pick's event (would-demote
  under SHADOW; actual demotion under enforce). Never gates anything.

ADDITIVE + nullable on picks; the new table starts empty. Behavior is
byte-identical until VALUE_BETFAIR_API_ENABLED produces verdicts AND
VALUE_BETFAIR_STALENESS_GUARD reads them. Downgrade drops both.

Revision ID: a7d3c9e5f1b8
Revises: f6b2d8e4a1c3
Create Date: 2026-07-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d3c9e5f1b8"
down_revision: str | Sequence[str] | None = "f6b2d8e4a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "betfair_anchor_verdicts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_ref", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=64), server_default="h2h", nullable=False),
        sa.Column("selection_role", sa.String(length=16), nullable=False),
        sa.Column("inline_price", sa.Numeric(10, 4), nullable=True),
        sa.Column("api_price", sa.Numeric(10, 4), nullable=True),
        sa.Column("api_best_back_size", sa.Numeric(12, 2), nullable=True),
        sa.Column("tick_diff", sa.Numeric(12, 6), nullable=True),
        sa.Column("inline_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("api_captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "event_ref",
            "market",
            "selection_role",
            name="uq_betfair_anchor_verdicts_selection",
        ),
    )
    op.create_index(
        "idx_betfair_anchor_verdicts_api_captured",
        "betfair_anchor_verdicts",
        ["api_captured_at"],
    )
    op.add_column(
        "picks", sa.Column("anchor_staleness_decision", sa.String(length=16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("picks", "anchor_staleness_decision")
    op.drop_index("idx_betfair_anchor_verdicts_api_captured", table_name="betfair_anchor_verdicts")
    op.drop_table("betfair_anchor_verdicts")
