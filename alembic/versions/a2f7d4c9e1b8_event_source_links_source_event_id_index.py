"""event_source_links — dedicated source_event_id index (audit: seq-scan on hot upsert path)

Additive-only: _get_or_create_event's Stage-0b redirect consult and Stage-1
link fast-path filter event_source_links on source_event_id ALONE, once per
event per persist cycle. The only covering index was the unique key
(source, source_event_id, canonical_event_id), whose leading column `source`
makes it unusable for a source_event_id-only predicate on PG16 (no skip
scan) — so every event upsert seq-scanned a table that grows monotonically
with each confirmed cross-source link and is never pruned. This index turns
both lookups into index scans. if_not_exists guards a DB where the ORM
metadata (tests/dev create_all) already created it. Downgrade drops it.

Revision ID: a2f7d4c9e1b8
Revises: d1e2f3a4b5c6
Create Date: 2026-07-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a2f7d4c9e1b8"
down_revision: str | Sequence[str] | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_event_source_links_source_event_id",
        "event_source_links",
        ["source_event_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_event_source_links_source_event_id",
        table_name="event_source_links",
        if_exists=True,
    )
