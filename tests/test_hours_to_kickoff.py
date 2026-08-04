"""TASK G item 4 (2026-07-26): picks.hours_to_kickoff mint-timing telemetry.

Covers: ORM column shape, migration chain (additive up / drop down, single
head), and the persist_pick round-trip (DB-gated — skips without the compose
Postgres) including the NULL (unknown kickoff) case. The mint-side stamp and
the INERT premium_max_hours_to_kickoff ceiling are covered in
tests/test_value_pipeline.py; the pure helper in tests/test_pipeline.py.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.pipeline import ShrinkAnnotatedStakeBreakdownOut, ValuePickOut
from app.schemas.base import Market
from app.storage.models import Pick
from app.storage.repositories import persist_pick
from tests.database import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = ROOT / "alembic" / "versions" / "d5a1c7f3e9b2_picks_hours_to_kickoff.py"
PRIOR_HEAD = "c4d8e2f6a1b3"

# --------------------------------------------------------------------------- #
# ORM column
# --------------------------------------------------------------------------- #


def test_pick_model_has_hours_to_kickoff_column() -> None:
    col = Pick.__table__.columns["hours_to_kickoff"]
    assert col.nullable is True
    assert isinstance(col.type, sa.Numeric)


def test_pick_hours_to_kickoff_defaults_none() -> None:
    # Additive + nullable: pre-column rows stay NULL (no backfill).
    assert Pick().hours_to_kickoff is None


# --------------------------------------------------------------------------- #
# Migration (additive up, drop down, chains off prior head, single head)
# --------------------------------------------------------------------------- #


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_mig_d5a1c7f3e9b2", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_chains_off_prior_head() -> None:
    mod = _load_migration()
    assert mod.revision == "d5a1c7f3e9b2"
    assert mod.down_revision == PRIOR_HEAD
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_migration_up_adds_only_the_column_down_drops_it() -> None:
    mod = _load_migration()
    added: list[tuple[str, object]] = []
    dropped: list[str] = []

    class _RecordingOp:
        @staticmethod
        def add_column(table: str, column: sa.Column) -> None:
            assert table == "picks"
            added.append((column.name, column.type))

        @staticmethod
        def drop_column(table: str, name: str) -> None:
            assert table == "picks"
            dropped.append(name)

    original = mod.op
    mod.op = _RecordingOp
    try:
        mod.upgrade()
        assert dropped == []  # upgrade is purely additive
        mod.downgrade()
    finally:
        mod.op = original
    assert [name for name, _ in added] == ["hours_to_kickoff"]
    assert isinstance(dict(added)["hours_to_kickoff"], sa.Numeric)
    assert dropped == ["hours_to_kickoff"]


def test_alembic_graph_has_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["b7e1d4a9c3f6"]


# --------------------------------------------------------------------------- #
# persist_pick round-trip (DB-gated — skips when compose Postgres is absent)
# --------------------------------------------------------------------------- #

_DB_URL = TEST_DATABASE_URL


@pytest.fixture
async def _session():  # type: ignore[no-untyped-def]
    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.begin()
        try:
            yield s
        finally:
            await s.rollback()
    await engine.dispose()


def _db_pick_kwargs(selection: str, hours_to_kickoff: float | None) -> dict[str, Any]:
    return dict(
        pick_id=f"p-htk-{selection}",
        sport="soccer",
        league="test-league-hours-to-kickoff",
        event="Alpha FC vs Beta United",
        event_id="evt-hours-to-kickoff-test",
        market=Market.H2H,
        selection=selection,
        bookmaker="testbook",
        decimal_odds=2.10,
        model_probability=0.55,
        fair_probability=0.50,
        edge=0.05,
        ev=0.155,
        confidence=0.70,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary="hours_to_kickoff telemetry persistence test",
        tier="premium",
        hours_to_kickoff=hours_to_kickoff,
        stake_breakdown=ShrinkAnnotatedStakeBreakdownOut(
            raw_kelly=0.1, fractional=0.025, capped=True, final=0.02
        ),
        created_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
    )


async def test_hours_to_kickoff_round_trips(_session: Any) -> None:
    pick = ValuePickOut(**_db_pick_kwargs("Alpha FC", 41.25))
    teams = EventTeams(home="Alpha FC", away="Beta United")
    outcome = await persist_pick(_session, pick, teams, "value", "v3")
    assert outcome == "inserted"
    stored = await _session.scalar(select(Pick).where(Pick.selection == "Alpha FC"))
    assert stored is not None
    assert stored.hours_to_kickoff == Decimal("41.250000")


async def test_unknown_kickoff_persists_null(_session: Any) -> None:
    # Missing starts_at at mint -> the stamp is honestly NULL, never fabricated.
    pick = ValuePickOut(**_db_pick_kwargs("Beta United", None))
    teams = EventTeams(home="Alpha FC", away="Beta United")
    outcome = await persist_pick(_session, pick, teams, "value", "v3")
    assert outcome == "inserted"
    stored = await _session.scalar(select(Pick).where(Pick.selection == "Beta United"))
    assert stored is not None
    assert stored.hours_to_kickoff is None
