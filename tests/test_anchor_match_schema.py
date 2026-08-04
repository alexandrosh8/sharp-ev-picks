"""Schema guards for the anchor-match-confidence observability batch.

Pure unit tests — no DB. They assert (a) the ``Pick`` ORM model carries the two
new nullable provenance columns, (b) the ``EventSourceLink`` /
``MatchReviewQueue`` tables exist with their idempotency constraints, (c) the
Alembic migration imports cleanly and chains off the prior head, and (d) the
migration graph still has EXACTLY ONE head (deploy auto-runs ``upgrade head``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.storage.models import EventSourceLink, MatchReviewQueue, Pick

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = (
    ROOT / "alembic" / "versions" / "e4f5a6b7c8d9_picks_anchor_match_confidence_source_links.py"
)
PRIOR_HEAD = "d3f4a5b6c7e8"


def _load_migration() -> Any:
    # alembic/versions has no __init__.py — load the revision module by path.
    spec = importlib.util.spec_from_file_location("_mig_e4f5a6b7c8d9", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pick_model_has_anchor_match_columns() -> None:
    confidence = Pick.__table__.columns["anchor_match_confidence"]
    assert confidence.nullable is True
    assert isinstance(confidence.type, sa.Numeric)
    method = Pick.__table__.columns["anchor_match_method"]
    assert method.nullable is True
    assert isinstance(method.type, sa.String)
    assert method.type.length == 32


def test_new_pick_columns_default_none() -> None:
    # Backward-compatible: a Pick without the new fields stays NULL — no
    # backfill; pre-column rows are feature-detected by the read paths.
    pick = Pick()
    assert pick.anchor_match_confidence is None
    assert pick.anchor_match_method is None


def test_event_source_links_model_shape() -> None:
    table = EventSourceLink.__table__
    assert isinstance(table, sa.Table)
    for name in (
        "canonical_event_id",
        "source",
        "source_event_id",
        "source_market_id",
        "confidence_score",
        "match_method",
        "matched_at",
        "active",
        "raw_sport",
        "raw_league",
        "raw_home",
        "raw_away",
        "raw_start_time_utc",
        "evidence_json",
    ):
        assert name in table.columns, f"missing column {name}"
    uniques = {
        c.name: tuple(col.name for col in c.columns)
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    assert uniques["uq_event_source_links_source_event"] == (
        "source",
        "source_event_id",
        "canonical_event_id",
    )
    assert any(ix.name == "idx_event_source_links_canonical" for ix in table.indexes)


def test_match_review_queue_model_shape() -> None:
    table = MatchReviewQueue.__table__
    assert isinstance(table, sa.Table)
    for name in (
        "source",
        "source_event_id",
        "source_market_id",
        "candidate_canonical_event_id",
        "confidence_score",
        "reason",
        "evidence_json",
        "created_at",
        "reviewed_at",
        "review_status",
    ):
        assert name in table.columns, f"missing column {name}"
    uniques = {
        c.name: tuple(col.name for col in c.columns)
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    # The idempotency key: re-running the matcher enqueues each reject once.
    assert uniques["uq_match_review_queue_dedupe"] == (
        "source",
        "source_event_id",
        "candidate_canonical_event_id",
        "reason",
    )


def test_migration_imports_cleanly_and_chains_off_prior_head() -> None:
    mod = _load_migration()
    assert mod.revision == "e4f5a6b7c8d9"
    assert mod.down_revision == PRIOR_HEAD
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_alembic_graph_has_single_head() -> None:
    # Deploy auto-runs `alembic upgrade head`; a forked graph would break it.
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["b7e1d4a9c3f6"]  # picks value_lost_at (2026-08-04)
