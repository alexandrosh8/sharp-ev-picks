"""Schema-level guards for the D3 close-provenance columns (close-evidence).

Pure unit tests — no DB. Assert (a) the ``Pick`` ORM model carries the two new
nullable columns (``close_anchor_book`` VARCHAR(64), ``close_snapshot_captured_at``
TIMESTAMPTZ), and (b) the Alembic migration imports cleanly, chains off the
prior head, and is additive with a working downgrade. Mirrors
tests/test_picks_devig_fallback_columns.py.
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
    / "f6b2d8e4a1c3_picks_close_provenance_columns.py"
)
PRIOR_HEAD = "e4f5a6b7c8d9"
NEW_COLUMNS = {"close_anchor_book", "close_snapshot_captured_at"}


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_mig_f6b2d8e4a1c3", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pick_model_has_both_close_provenance_columns() -> None:
    book = Pick.__table__.columns["close_anchor_book"]
    assert book.nullable is True
    assert isinstance(book.type, sa.String)
    assert book.type.length == 64
    captured = Pick.__table__.columns["close_snapshot_captured_at"]
    assert captured.nullable is True
    assert isinstance(captured.type, sa.DateTime)
    assert captured.type.timezone is True  # TIMESTAMPTZ — UTC-aware, never naive


def test_new_close_provenance_columns_default_none() -> None:
    pick = Pick()
    assert pick.close_anchor_book is None
    assert pick.close_snapshot_captured_at is None


def test_migration_imports_cleanly_and_chains_off_prior_head() -> None:
    mod = _load_migration()
    assert mod.revision == "f6b2d8e4a1c3"
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

    types = dict(added)
    assert set(types) == NEW_COLUMNS
    assert isinstance(types["close_anchor_book"], sa.String)
    assert types["close_anchor_book"].length == 64
    assert isinstance(types["close_snapshot_captured_at"], sa.DateTime)
    assert types["close_snapshot_captured_at"].timezone is True


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
