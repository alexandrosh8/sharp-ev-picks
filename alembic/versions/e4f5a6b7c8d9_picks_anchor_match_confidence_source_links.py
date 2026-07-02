"""picks: anchor-match confidence provenance + event_source_links / match_review_queue

Observability batch (R1 design, 2026-07-02) — three additive pieces, none of
which changes which picks mint or which anchors are accepted:

1) picks.anchor_match_confidence / picks.anchor_match_method — per-pick MATCH
   CONFIDENCE of the sharp anchor that produced the pick. The Pinnacle anchor is
   attached by a cross-source fuzzy matcher (repositories.resolve_pinnacle_close_
   snaps -> match_event_hardened_scored) whose per-candidate score previously
   died inside the matcher; these columns persist the accepted candidate's
   min-side Jaro-Winkler (NUMERIC in [0,1]) and the accept method
   ('exact_canonical' / 'jw_two_tier', 'slug_' prefix on the OddsPortal
   slug-fallback path, 'inline_betfair_canonical' for the same-canonical-event
   Betfair/Smarkets anchor, 'unscored' when a pinnacle-typed pick's provenance
   was unavailable — NULL confidence then, never a fabricated 1.0). NULL/NULL =
   consensus anchor, model-strategy pick, or pre-column row.

2) event_source_links — persisted cross-source identity: one row per confirmed
   (canonical event <-> per-source stable id) link, seeded from live matcher
   confirmations (source='pinnacle_arcadia' with the arcadia external_ref;
   source='betfair_api' with the Betfair event id + market_id). Previously the
   link was recomputed on every resolve call and the Betfair API ids were thrown
   away. Upsert-in-place on (source, source_event_id, canonical_event_id).

3) match_review_queue — the hardened matcher's silently-discarded borderline
   bands (JW review band 0.84<=JW<0.92, token-sort misses, ambiguity-margin /
   kickoff-window rejects), captured as reviewable rows. A TAP, never a gate:
   the match still fails exactly as before. Unique on (source, source_event_id,
   candidate_canonical_event_id, reason) keeps re-runs idempotent (writers use
   ON CONFLICT DO NOTHING).

All additive + nullable — picks rows written before these columns stay NULL and
are feature-detected by the read paths; the two tables start empty.

Revision ID: e4f5a6b7c8d9
Revises: d3f4a5b6c7e8
Create Date: 2026-07-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: str | Sequence[str] | None = "d3f4a5b6c7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("picks", sa.Column("anchor_match_confidence", sa.Numeric(8, 6), nullable=True))
    op.add_column("picks", sa.Column("anchor_match_method", sa.String(length=32), nullable=True))
    op.create_table(
        "event_source_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "canonical_event_id", sa.BigInteger(), sa.ForeignKey("events.id"), nullable=False
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=False),
        sa.Column("source_market_id", sa.String(length=64), nullable=True),
        sa.Column("confidence_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("match_method", sa.String(length=32), nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("raw_sport", sa.String(length=64), nullable=True),
        sa.Column("raw_league", sa.String(length=128), nullable=True),
        sa.Column("raw_home", sa.String(length=128), nullable=True),
        sa.Column("raw_away", sa.String(length=128), nullable=True),
        sa.Column("raw_start_time_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_json", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "source",
            "source_event_id",
            "canonical_event_id",
            name="uq_event_source_links_source_event",
        ),
    )
    op.create_index(
        "idx_event_source_links_canonical", "event_source_links", ["canonical_event_id"]
    )
    op.create_table(
        "match_review_queue",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=False),
        sa.Column("source_market_id", sa.String(length=64), nullable=True),
        sa.Column(
            "candidate_canonical_event_id",
            sa.BigInteger(),
            sa.ForeignKey("events.id"),
            nullable=True,
        ),
        sa.Column("confidence_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("evidence_json", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.UniqueConstraint(
            "source",
            "source_event_id",
            "candidate_canonical_event_id",
            "reason",
            name="uq_match_review_queue_dedupe",
        ),
    )


def downgrade() -> None:
    op.drop_table("match_review_queue")
    op.drop_index("idx_event_source_links_canonical", table_name="event_source_links")
    op.drop_table("event_source_links")
    op.drop_column("picks", "anchor_match_method")
    op.drop_column("picks", "anchor_match_confidence")
