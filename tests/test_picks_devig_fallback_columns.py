"""Schema-level guards for the P2-2 devig-fallback provenance columns.

Pure unit tests — no DB. Assert (a) the ``Pick`` ORM model carries the two new
nullable BOOLEAN columns, and (b) the Alembic migration imports cleanly, chains
off the prior head, and is additive with a working downgrade. Mirrors
tests/test_picks_schema_columns.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from app.storage.models import Pick

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "b2c3d4e5f6a7_picks_devig_fallback_provenance.py"
)
PRIOR_HEAD = "a1b2c3d4e5f6"
NEW_COLUMNS = {"mint_devig_fell_back", "close_devig_fell_back"}


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_mig_b2c3d4e5f6a7", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pick_model_has_both_devig_fallback_columns() -> None:
    for name in NEW_COLUMNS:
        col = Pick.__table__.columns[name]
        assert col.nullable is True
        assert isinstance(col.type, sa.Boolean)


def test_new_devig_columns_default_none() -> None:
    pick = Pick()
    assert pick.mint_devig_fell_back is None
    assert pick.close_devig_fell_back is None


def test_migration_imports_cleanly_and_chains_off_prior_head() -> None:
    mod = _load_migration()
    assert mod.revision == "b2c3d4e5f6a7"
    assert mod.down_revision == PRIOR_HEAD
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_migration_adds_both_columns_additively() -> None:
    mod = _load_migration()
    added: list[tuple[str, object]] = []

    class _RecordingOp:
        @staticmethod
        def add_column(table: str, column: sa.Column) -> None:
            assert table == "picks"
            added.append((column.name, column.type))

        @staticmethod
        def drop_column(table: str, name: str) -> None:
            raise AssertionError("upgrade must not drop columns")

    original_op = mod.op
    mod.op = _RecordingOp
    try:
        mod.upgrade()
    finally:
        mod.op = original_op

    assert {name for name, _ in added} == NEW_COLUMNS
    for _name, col_type in added:
        assert isinstance(col_type, sa.Boolean)


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
    mod.op = _RecordingOp
    try:
        mod.downgrade()
    finally:
        mod.op = original_op

    assert set(dropped) == NEW_COLUMNS
