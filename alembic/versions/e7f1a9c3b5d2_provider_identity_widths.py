"""Align provider identity widths across ingestion, picks, and audit tables.

Revision ID: e7f1a9c3b5d2
Revises: b1e4f7a9c2d5
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7f1a9c3b5d2"
down_revision: str | Sequence[str] | None = "b1e4f7a9c2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Event identity accepted by live loaders (notably the Odds API) must fit
    # every canonical-entity and cross-source audit consumer without truncation.
    op.alter_column("sports", "key", type_=sa.String(length=128))
    op.alter_column("sports", "name", type_=sa.String(length=256))
    op.alter_column("leagues", "key", type_=sa.String(length=256))
    op.alter_column("leagues", "name", type_=sa.String(length=256))
    op.alter_column("leagues", "country", type_=sa.String(length=128))
    op.alter_column("teams", "name", type_=sa.String(length=256))
    op.alter_column("teams", "normalized_name", type_=sa.String(length=256))
    op.alter_column("events", "external_ref", type_=sa.String(length=512))

    op.alter_column("event_source_links", "source_event_id", type_=sa.String(length=512))
    op.alter_column("event_source_links", "raw_sport", type_=sa.String(length=128))
    op.alter_column("event_source_links", "raw_league", type_=sa.String(length=256))
    op.alter_column("event_source_links", "raw_home", type_=sa.String(length=256))
    op.alter_column("event_source_links", "raw_away", type_=sa.String(length=256))
    op.alter_column("match_review_queue", "source_event_id", type_=sa.String(length=512))
    op.alter_column("betfair_anchor_verdicts", "event_ref", type_=sa.String(length=512))

    # A snapshot that passes the 512/1024-byte boundary must remain mintable.
    # Book provenance columns carry the same provider identity through repricing,
    # settlement, and CLV attribution.
    op.alter_column("picks", "selection", type_=sa.String(length=1024))
    op.alter_column("picks", "bookmaker", type_=sa.String(length=512))
    op.alter_column("picks", "settlement_basis_bookmaker", type_=sa.String(length=512))
    op.alter_column("picks", "anchor_book", type_=sa.String(length=512))
    op.alter_column("picks", "close_anchor_book", type_=sa.String(length=512))
    op.alter_column("picks", "current_bookmaker", type_=sa.String(length=512))

    # Candidate audit is a tap on the same live candidate and may never lose it
    # solely because its identity columns lag the pick/snapshot schema.
    op.alter_column("candidate_evaluations", "sport_key", type_=sa.String(length=128))
    op.alter_column("candidate_evaluations", "market_detail", type_=sa.String(length=512))
    op.alter_column("candidate_evaluations", "selection", type_=sa.String(length=1024))
    op.alter_column("candidate_evaluations", "anchor_book", type_=sa.String(length=512))
    op.alter_column("candidate_evaluations", "best_book", type_=sa.String(length=512))


def downgrade() -> None:
    # Refuse every lossy shrink. Operators must clean or map oversized provider
    # identities explicitly; silently truncating any identity can alias rows.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM sports WHERE length(key) > 64 OR length(name) > 128)
             OR EXISTS (
               SELECT 1 FROM leagues
               WHERE length(key) > 64 OR length(name) > 128 OR length(country) > 64
             )
             OR EXISTS (
               SELECT 1 FROM teams
               WHERE length(name) > 128 OR length(normalized_name) > 128
             )
             OR EXISTS (SELECT 1 FROM events WHERE length(external_ref) > 128)
          THEN
            RAISE EXCEPTION 'cannot downgrade: canonical provider identity exceeds legacy width';
          END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM event_source_links
            WHERE length(source_event_id) > 128
               OR length(raw_sport) > 64
               OR length(raw_league) > 128
               OR length(raw_home) > 128
               OR length(raw_away) > 128
          )
             OR EXISTS (
               SELECT 1 FROM match_review_queue WHERE length(source_event_id) > 128
             )
             OR EXISTS (
               SELECT 1 FROM betfair_anchor_verdicts WHERE length(event_ref) > 128
             )
          THEN
            RAISE EXCEPTION 'cannot downgrade: cross-source identity exceeds legacy width';
          END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM picks
            WHERE length(selection) > 64
               OR length(bookmaker) > 64
               OR length(settlement_basis_bookmaker) > 64
               OR length(anchor_book) > 64
               OR length(close_anchor_book) > 64
               OR length(current_bookmaker) > 64
          )
             OR EXISTS (
               SELECT 1 FROM candidate_evaluations
               WHERE length(sport_key) > 64
                  OR length(market_detail) > 64
                  OR length(selection) > 64
                  OR length(anchor_book) > 64
                  OR length(best_book) > 64
             )
          THEN
            RAISE EXCEPTION 'cannot downgrade: pick instrument identity exceeds legacy width';
          END IF;
        END $$
        """
    )

    op.alter_column("candidate_evaluations", "best_book", type_=sa.String(length=64))
    op.alter_column("candidate_evaluations", "anchor_book", type_=sa.String(length=64))
    op.alter_column("candidate_evaluations", "selection", type_=sa.String(length=64))
    op.alter_column("candidate_evaluations", "market_detail", type_=sa.String(length=64))
    op.alter_column("candidate_evaluations", "sport_key", type_=sa.String(length=64))

    op.alter_column("picks", "current_bookmaker", type_=sa.String(length=64))
    op.alter_column("picks", "close_anchor_book", type_=sa.String(length=64))
    op.alter_column("picks", "anchor_book", type_=sa.String(length=64))
    op.alter_column("picks", "settlement_basis_bookmaker", type_=sa.String(length=64))
    op.alter_column("picks", "bookmaker", type_=sa.String(length=64))
    op.alter_column("picks", "selection", type_=sa.String(length=64))

    op.alter_column("betfair_anchor_verdicts", "event_ref", type_=sa.String(length=128))
    op.alter_column("match_review_queue", "source_event_id", type_=sa.String(length=128))
    op.alter_column("event_source_links", "raw_away", type_=sa.String(length=128))
    op.alter_column("event_source_links", "raw_home", type_=sa.String(length=128))
    op.alter_column("event_source_links", "raw_league", type_=sa.String(length=128))
    op.alter_column("event_source_links", "raw_sport", type_=sa.String(length=64))
    op.alter_column("event_source_links", "source_event_id", type_=sa.String(length=128))

    op.alter_column("events", "external_ref", type_=sa.String(length=128))
    op.alter_column("teams", "normalized_name", type_=sa.String(length=128))
    op.alter_column("teams", "name", type_=sa.String(length=128))
    op.alter_column("leagues", "country", type_=sa.String(length=64))
    op.alter_column("leagues", "name", type_=sa.String(length=128))
    op.alter_column("leagues", "key", type_=sa.String(length=64))
    op.alter_column("sports", "name", type_=sa.String(length=128))
    op.alter_column("sports", "key", type_=sa.String(length=64))
