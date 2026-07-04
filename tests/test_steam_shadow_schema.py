"""Schema guards for the A5 steam shadow-verdict persistence batch.

Pure unit tests — no DB. They assert (a) the ``Pick`` ORM model carries the
four new nullable steam_* observability columns, (b) a bare ``Pick`` defaults
them to NULL (no backfill; pre-column rows are honest NULLs), and (c) the
Alembic migration imports cleanly and chains off the prior head c9e4f2a7d5b3
(the single-head invariant itself is pinned in tests/test_anchor_match_schema.py).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from app.storage.models import Pick

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = ROOT / "alembic" / "versions" / "f8a3c5d7e9b1_picks_steam_shadow_verdict.py"
PRIOR_HEAD = "c9e4f2a7d5b3"  # picks_close_exclusion_reason (A4)


def _load_migration() -> Any:
    # alembic/versions has no __init__.py — load the revision module by path.
    spec = importlib.util.spec_from_file_location("_mig_f8a3c5d7e9b1", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pick_model_has_steam_shadow_columns() -> None:
    tripped = Pick.__table__.columns["steam_tripped"]
    assert tripped.nullable is True
    assert isinstance(tripped.type, sa.Boolean)
    reasons = Pick.__table__.columns["steam_reasons"]
    assert reasons.nullable is True
    assert isinstance(reasons.type, sa.String)
    assert reasons.type.length == 64
    closed_fraction = Pick.__table__.columns["steam_closed_fraction"]
    assert closed_fraction.nullable is True
    assert isinstance(closed_fraction.type, sa.Numeric)
    anchor_age = Pick.__table__.columns["steam_anchor_age_seconds"]
    assert anchor_age.nullable is True
    assert isinstance(anchor_age.type, sa.Numeric)


def test_new_steam_columns_default_none() -> None:
    # Backward-compatible: a Pick without the new fields stays NULL — no
    # backfill; pre-column rows read as "never evaluated".
    pick = Pick()
    assert pick.steam_tripped is None
    assert pick.steam_reasons is None
    assert pick.steam_closed_fraction is None
    assert pick.steam_anchor_age_seconds is None


def test_migration_imports_cleanly_and_chains_off_prior_head() -> None:
    mod = _load_migration()
    assert mod.revision == "f8a3c5d7e9b1"
    assert mod.down_revision == PRIOR_HEAD
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)
