"""Fence active source identities and include market detail in pick identity.

Revision ID: b1e4f7a9c2d5
Revises: a9d2c4e6f8b1
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1e4f7a9c2d5"
down_revision: str | Sequence[str] | None = "a9d2c4e6f8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("odds_snapshots", "bookmaker", type_=sa.String(length=512))
    op.alter_column("odds_snapshots", "market", type_=sa.String(length=512))
    op.alter_column("odds_snapshots", "selection", type_=sa.String(length=1024))
    op.add_column("picks", sa.Column("exposure_reserved_on", sa.Date(), nullable=True))
    op.add_column(
        "picks",
        sa.Column("exposure_reserved_fraction", sa.Numeric(8, 6), nullable=True),
    )
    op.add_column(
        "picks",
        sa.Column(
            "settlement_stake_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "result_tracking",
        sa.Column("settled_stake_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "result_tracking",
        sa.Column("settled_effective_odds", sa.Numeric(10, 4), nullable=True),
    )
    op.add_column(
        "picks",
        sa.Column(
            "settlement_raw_odds_stake",
            sa.Numeric(22, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "picks",
        sa.Column(
            "settlement_effective_odds_stake",
            sa.Numeric(22, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "picks",
        sa.Column("settlement_basis_bookmaker", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "picks",
        sa.Column(
            "settlement_basis_repriced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        """
        UPDATE picks
        SET exposure_reserved_on = (created_at AT TIME ZONE 'UTC')::date,
            exposure_reserved_fraction = recommended_stake_fraction
        WHERE tier = 'premium' AND recommended_stake_fraction > 0
        """
    )
    op.execute(
        """
        UPDATE result_tracking AS rt
        SET settled_stake_amount = COALESCE(
              actual.actual_stake,
              NULLIF(p.settlement_stake_amount, 0),
              p.recommended_stake_amount
            ),
            settled_effective_odds = CASE
              WHEN actual.actual_odds IS NOT NULL THEN
                1 + (actual.actual_odds - 1) * (
                  1 - CASE lower(btrim(COALESCE(actual.bookmaker_used, p.bookmaker)))
                        WHEN 'betfair exchange' THEN 0.05
                        WHEN 'betfair' THEN 0.05
                        WHEN 'smarkets' THEN 0.02
                        WHEN 'matchbook' THEN 0.02
                        ELSE 0
                      END
                )
              WHEN p.settlement_stake_amount > 0 THEN
                p.settlement_effective_odds_stake / p.settlement_stake_amount
              ELSE
                1 + (p.decimal_odds - 1) * (
                  1 - CASE lower(btrim(p.bookmaker))
                        WHEN 'betfair exchange' THEN 0.05
                        WHEN 'betfair' THEN 0.05
                        WHEN 'smarkets' THEN 0.02
                        WHEN 'matchbook' THEN 0.02
                        ELSE 0
                      END
                )
            END
        FROM picks AS p
        LEFT JOIN LATERAL (
          SELECT m.actual_stake, m.actual_odds, m.bookmaker_used
          FROM manual_bet_logs AS m
          WHERE m.pick_id = p.id
            AND m.bet_placed
            AND m.actual_stake IS NOT NULL
          ORDER BY m.id DESC
          LIMIT 1
        ) AS actual ON TRUE
        WHERE p.id = rt.pick_id
        """
    )
    # Historical rows have no tranche history. Preserve the previous behavior
    # exactly by treating their latest persisted recommendation as one fill;
    # all new reprices accumulate only their cap-granted delta from this basis.
    op.execute(
        """
        UPDATE picks
        SET settlement_stake_amount = recommended_stake_amount,
            settlement_raw_odds_stake = recommended_stake_amount * decimal_odds,
            settlement_effective_odds_stake = recommended_stake_amount * (
              1 + (decimal_odds - 1) * (
                1 - CASE lower(btrim(bookmaker))
                      WHEN 'betfair exchange' THEN 0.05
                      WHEN 'betfair' THEN 0.05
                      WHEN 'smarkets' THEN 0.02
                      WHEN 'matchbook' THEN 0.02
                      ELSE 0
                    END
              )
            ),
            settlement_basis_bookmaker = CASE
              WHEN recommended_stake_amount > 0 THEN bookmaker
              ELSE NULL
            END
        """
    )
    # A provider event may have been re-linked over time. Keep every row for
    # audit, but retire all except the newest active target before enforcing
    # one ACTIVE target per (source, source_event_id).
    op.execute(
        """
        WITH ranked AS (
          SELECT id,
                 row_number() OVER (
                   PARTITION BY source, source_event_id
                   ORDER BY matched_at DESC, id DESC
                 ) AS rn
          FROM event_source_links
          WHERE active
        )
        UPDATE event_source_links AS links
        SET active = false
        FROM ranked
        WHERE links.id = ranked.id AND ranked.rn > 1
        """
    )
    op.create_index(
        "uq_event_source_links_active_source_event",
        "event_source_links",
        ["source", "source_event_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.drop_constraint("uq_picks_event_market_selection_model", "picks", type_="unique")
    op.create_unique_constraint(
        "uq_picks_event_market_selection_model",
        "picks",
        ["event_id", "market", "market_detail", "selection", "model_version_id"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    # Refuse lossy downgrade rather than silently truncating identity values.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM odds_snapshots
            WHERE length(bookmaker) > 64 OR length(market) > 64 OR length(selection) > 64
          ) THEN
            RAISE EXCEPTION 'cannot downgrade: odds snapshot identity exceeds 64 characters';
          END IF;
        END $$
        """
    )
    # The old key cannot represent multiple line-qualified instruments. Refuse
    # the downgrade instead of deleting picks (and their dependent audit rows).
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM picks
            GROUP BY event_id, market, selection, model_version_id
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade: line-qualified picks collide under the old identity key';
          END IF;
        END $$
        """
    )
    op.drop_constraint("uq_picks_event_market_selection_model", "picks", type_="unique")
    op.create_unique_constraint(
        "uq_picks_event_market_selection_model",
        "picks",
        ["event_id", "market", "selection", "model_version_id"],
    )
    op.drop_index("uq_event_source_links_active_source_event", table_name="event_source_links")
    op.drop_column("result_tracking", "settled_effective_odds")
    op.drop_column("result_tracking", "settled_stake_amount")
    op.drop_column("picks", "settlement_basis_repriced")
    op.drop_column("picks", "settlement_basis_bookmaker")
    op.drop_column("picks", "settlement_effective_odds_stake")
    op.drop_column("picks", "settlement_raw_odds_stake")
    op.drop_column("picks", "settlement_stake_amount")
    op.drop_column("picks", "exposure_reserved_fraction")
    op.drop_column("picks", "exposure_reserved_on")
    op.alter_column("odds_snapshots", "selection", type_=sa.String(length=64))
    op.alter_column("odds_snapshots", "market", type_=sa.String(length=64))
    op.alter_column("odds_snapshots", "bookmaker", type_=sa.String(length=64))
