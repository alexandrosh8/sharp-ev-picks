"""Schema-level guards for the picks columns added to unblock CLV findings.

Pure unit tests — no DB. They assert (a) the ``Pick`` ORM model carries the new
nullable columns with the right SQL types, and (b) the Alembic migration that
introduces them imports cleanly, chains off the prior head, and is additive with
a working downgrade. Batch 1 is schema-only; population/read logic lands later.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa

from app.storage.models import Pick

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "2d37faf2d3fd_picks_has_snapshot_close_anchor_book_.py"
)
PRIOR_HEAD = "e1a4d9c7b3f5"


def _load_migration() -> ModuleType:
    # alembic/versions has no __init__.py, so it is not an importable package —
    # load the revision module straight from its file path.
    spec = importlib.util.spec_from_file_location("_mig_2d37faf2d3fd", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pick_model_has_anchor_book_column() -> None:
    col = Pick.__table__.columns["anchor_book"]
    assert col.nullable is True
    assert isinstance(col.type, sa.String)
    assert col.type.length == 64


def test_pick_model_has_snapshot_close_column() -> None:
    col = Pick.__table__.columns["has_snapshot_close"]
    assert col.nullable is True
    assert isinstance(col.type, sa.Boolean)


def test_new_pick_columns_are_nullable_and_default_none() -> None:
    # Backward-compatible: instantiating a Pick without the new fields leaves
    # them unset (NULL) — no backfill required for historical rows.
    pick = Pick()
    assert pick.anchor_book is None
    assert pick.has_snapshot_close is None


def test_migration_imports_cleanly_and_chains_off_prior_head() -> None:
    mod = _load_migration()
    assert mod.revision == "2d37faf2d3fd"
    assert mod.down_revision == PRIOR_HEAD
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_migration_adds_both_columns_additively() -> None:
    mod = _load_migration()
    added: list[tuple[str, object]] = []

    class _RecordingOp:
        @staticmethod
        def add_column(table: str, column: sa.Column) -> None:  # type: ignore[type-arg]
            assert table == "picks"
            added.append((column.name, column.type))

        @staticmethod
        def drop_column(table: str, name: str) -> None:
            raise AssertionError("upgrade must not drop columns")

    original_op = mod.op
    mod.op = _RecordingOp  # type: ignore[assignment]
    try:
        mod.upgrade()
    finally:
        mod.op = original_op

    names = {name for name, _ in added}
    assert names == {"has_snapshot_close", "anchor_book"}
    by_name = dict(added)
    assert isinstance(by_name["has_snapshot_close"], sa.Boolean)
    assert isinstance(by_name["anchor_book"], sa.String)
    assert by_name["anchor_book"].length == 64  # type: ignore[attr-defined]


def test_migration_downgrade_drops_both_columns() -> None:
    mod = _load_migration()
    dropped: list[str] = []

    class _RecordingOp:
        @staticmethod
        def add_column(table: str, column: object) -> None:
            raise AssertionError("downgrade must not add columns")

        @staticmethod
        def drop_column(table: str, name: str) -> None:
            assert table == "picks"
            dropped.append(name)

    original_op = mod.op
    mod.op = _RecordingOp  # type: ignore[assignment]
    try:
        mod.downgrade()
    finally:
        mod.op = original_op

    assert set(dropped) == {"has_snapshot_close", "anchor_book"}
