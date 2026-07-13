"""Schema contracts for qualified pick/snapshot/source-link identity."""

from pathlib import Path
from typing import cast

import sqlalchemy as sa

from app.storage.models import (
    BetfairAnchorVerdict,
    CandidateEvaluation,
    Event,
    EventSourceLink,
    League,
    MatchReviewQueue,
    OddsSnapshot,
    Pick,
    Sport,
    Team,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "b1e4f7a9c2d5_identity_and_pick_instrument_keys.py"
WIDTH_MIGRATION = ROOT / "alembic" / "versions" / "e7f1a9c3b5d2_provider_identity_widths.py"


def test_pick_unique_key_includes_market_detail_with_nulls_not_distinct() -> None:
    table = cast(sa.Table, Pick.__table__)
    constraint = next(
        item
        for item in table.constraints
        if isinstance(item, sa.UniqueConstraint)
        and item.name == "uq_picks_event_market_selection_model"
    )
    assert tuple(column.name for column in constraint.columns) == (
        "event_id",
        "market",
        "market_detail",
        "selection",
        "model_version_id",
    )
    assert constraint.dialect_options["postgresql"]["nulls_not_distinct"] is True


def test_snapshot_identity_columns_are_wide_and_exposure_is_durable() -> None:
    snapshots = cast(sa.Table, OddsSnapshot.__table__)
    picks = cast(sa.Table, Pick.__table__)
    assert cast(sa.String, snapshots.columns["bookmaker"].type).length == 512
    assert cast(sa.String, snapshots.columns["market"].type).length == 512
    assert cast(sa.String, snapshots.columns["selection"].type).length == 1024
    assert picks.columns["exposure_reserved_on"].nullable is True
    assert picks.columns["exposure_reserved_fraction"].nullable is True


def test_source_event_has_one_partial_unique_active_target() -> None:
    table = cast(sa.Table, EventSourceLink.__table__)
    index = next(
        item for item in table.indexes if item.name == "uq_event_source_links_active_source_event"
    )
    assert index.unique is True
    assert tuple(column.name for column in index.columns) == ("source", "source_event_id")
    assert str(index.dialect_options["postgresql"]["where"]) == "active"


def test_migration_downgrade_refuses_identity_data_loss() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "DELETE FROM picks" not in source
    assert "line-qualified picks collide under the old identity key" in source
    assert "odds snapshot identity exceeds 64 characters" in source


def _string_width(model: type[object], column: str) -> int | None:
    table = cast(sa.Table, model.__table__)  # type: ignore[attr-defined]
    return cast(sa.String, table.columns[column].type).length


def test_provider_identities_fit_every_live_consumer() -> None:
    assert _string_width(Sport, "key") == 128
    assert _string_width(League, "key") == 256
    assert _string_width(Team, "name") == 256
    assert _string_width(Event, "external_ref") == 512
    assert _string_width(EventSourceLink, "source_event_id") == 512
    assert _string_width(MatchReviewQueue, "source_event_id") == 512
    assert _string_width(BetfairAnchorVerdict, "event_ref") == 512

    assert _string_width(Pick, "selection") == 1024
    for column in (
        "bookmaker",
        "settlement_basis_bookmaker",
        "anchor_book",
        "close_anchor_book",
        "current_bookmaker",
    ):
        assert _string_width(Pick, column) == 512

    assert _string_width(CandidateEvaluation, "sport_key") == 128
    assert _string_width(CandidateEvaluation, "market_detail") == 512
    assert _string_width(CandidateEvaluation, "selection") == 1024
    assert _string_width(CandidateEvaluation, "anchor_book") == 512
    assert _string_width(CandidateEvaluation, "best_book") == 512


def test_width_migration_refuses_every_lossy_identity_shrink() -> None:
    source = WIDTH_MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision: str | Sequence[str] | None = "b1e4f7a9c2d5"' in source
    assert "canonical provider identity exceeds legacy width" in source
    assert "cross-source identity exceeds legacy width" in source
    assert "pick instrument identity exceeds legacy width" in source
    assert "substring" not in source.lower()
