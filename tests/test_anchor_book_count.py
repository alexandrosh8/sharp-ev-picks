"""Task 6: anchor-thinness telemetry — picks.anchor_book_count (log-only).

The AGE half of anchor thinness is already persisted (steam_anchor_age_seconds,
f8a3c5d7e9b1), so only the COUNT is added: the distinct NON-SHARP books quoting
the pick's devig market group at mint — exactly the thin-coverage floor's
number (value_policy.distinct_book_count, sharp set excluded), hoisted at the
group boundary in run_value_pipeline, NOT recomputed. Nothing gates on it.

Covers: ORM column shape, migration chain (additive up / drop down, single
head), the value-pipeline mint stamp, and the persist_pick round-trip
(DB-gated — skips without the compose Postgres) including the Task 5
stake_breakdown shadow keys riding the same row.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.edge.gates import GatePolicy
from app.edge.value_policy import ValuePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import NullModel
from app.notifications.base import Alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import (
    PipelineDeps,
    ShrinkAnnotatedStakeBreakdownOut,
    ValuePickOut,
    run_value_pipeline,
)
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Pick
from app.storage.repositories import persist_pick
from tests.database import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = ROOT / "alembic" / "versions" / "a9d2c4e6f8b1_picks_anchor_book_count.py"
PRIOR_HEAD = "c3d5e7f9a1b4"

# --------------------------------------------------------------------------- #
# ORM column
# --------------------------------------------------------------------------- #


def test_pick_model_has_anchor_book_count_column() -> None:
    col = Pick.__table__.columns["anchor_book_count"]
    assert col.nullable is True
    assert isinstance(col.type, sa.SmallInteger)


def test_pick_anchor_book_count_defaults_none() -> None:
    # Additive + nullable: pre-column and model-strategy rows stay NULL.
    assert Pick().anchor_book_count is None


# --------------------------------------------------------------------------- #
# Migration (additive up, drop down, chains off prior head, single head)
# --------------------------------------------------------------------------- #


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_mig_a9d2c4e6f8b1", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_chains_off_prior_head() -> None:
    mod = _load_migration()
    assert mod.revision == "a9d2c4e6f8b1"
    assert mod.down_revision == PRIOR_HEAD
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_migration_up_adds_only_the_count_column_down_drops_it() -> None:
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
    assert [name for name, _ in added] == ["anchor_book_count"]
    assert isinstance(dict(added)["anchor_book_count"], sa.SmallInteger)
    assert dropped == ["anchor_book_count"]


def test_alembic_graph_has_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["d5a1c7f3e9b2"]


# --------------------------------------------------------------------------- #
# Value-pipeline mint stamp
# --------------------------------------------------------------------------- #


def _snap(book: str, sel: str, odds: float) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-1",
        bookmaker=book,
        market=Market.H2H,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=30),
        ingested_at=now,
    )


class _FakeLoader:
    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        self.snapshots = snapshots

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        return self.snapshots


class _Sink:
    name = "recording"

    async def send(self, alert: Alert) -> bool:
        return True


def _deps(snapshots: list[OddsSnapshotIn]) -> PipelineDeps:
    directory = EventDirectory()
    directory.register(
        "evt-1",
        EventTeams(
            home="Home FC",
            away="Away FC",
            starts_at=datetime.now(tz=UTC) + timedelta(hours=6),
        ),
    )
    return PipelineDeps(
        loader=_FakeLoader(snapshots),
        model=NullModel(),
        dispatcher=AlertDispatcher([_Sink()], InMemoryIdempotencyStore()),
        gate_policy=GatePolicy(
            min_edge=0.0,
            min_ev=0.0,
            min_confidence=0.0,
            max_odds_age_seconds=300,
            min_liquidity=0.0,
        ),
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        directory=directory,
        value_min_edge=0.015,
        value_volume_min_edge=0.015,
        value_min_odds=1.30,
        value_policy=ValuePolicy(),
    )


async def test_minted_value_pick_carries_soft_book_count() -> None:
    # Pinnacle (sharp anchor) + TWO soft books quoting the market: the stamp
    # is the SOFT count (sharp excluded — the thin-coverage floor's number).
    snapshots = [
        _snap("Pinnacle", "Home FC", 2.50),
        _snap("Pinnacle", "Draw", 3.30),
        _snap("Pinnacle", "Away FC", 3.10),
        _snap("SoftBook", "Home FC", 2.90),
        _snap("SoftBook", "Draw", 3.20),
        _snap("SoftBook", "Away FC", 2.95),
        _snap("OtherSoft", "Home FC", 2.80),
        _snap("OtherSoft", "Draw", 3.25),
        _snap("OtherSoft", "Away FC", 3.00),
    ]
    picks = await run_value_pipeline(_deps(snapshots), "soccer")
    assert picks, "expected one premium value pick"
    pick = picks[0]
    assert isinstance(pick, ValuePickOut)
    assert pick.anchor_book_count == 2  # SoftBook + OtherSoft; Pinnacle excluded


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


def _db_pick_kwargs(selection: str) -> dict[str, Any]:
    return dict(
        pick_id=f"p-abc-{selection}",
        sport="soccer",
        league="test-league-anchor-count",
        event="Alpha FC vs Beta United",
        event_id="evt-anchor-count-test",
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
        reason_summary="anchor-thinness telemetry persistence test",
        tier="premium",
        created_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )


async def test_anchor_book_count_and_shadow_keys_round_trip(_session: Any) -> None:
    pick = ValuePickOut(
        **_db_pick_kwargs("Alpha FC"),
        anchor_book_count=4,
        stake_breakdown=ShrinkAnnotatedStakeBreakdownOut(
            raw_kelly=0.1,
            fractional=0.025,
            capped=True,
            final=0.02,
            phi=0.5,
            n_eff=50,
            shrunk_fraction=0.0125,
        ),
    )
    teams = EventTeams(home="Alpha FC", away="Beta United")
    outcome = await persist_pick(_session, pick, teams, "value", "v3")
    assert outcome == "inserted"
    stored = await _session.scalar(select(Pick).where(Pick.selection == "Alpha FC"))
    assert stored is not None
    assert stored.anchor_book_count == 4
    # Task 5: the shadow annotation rides the persisted stake_breakdown JSON.
    assert stored.stake_breakdown["phi"] == 0.5
    assert stored.stake_breakdown["n_eff"] == 50
    assert stored.stake_breakdown["shrunk_fraction"] == 0.0125


async def test_model_strategy_pick_stays_null(_session: Any) -> None:
    # A plain PickOut (model strategy / pre-column mint path) has no
    # anchor_book_count attribute: persist_pick feature-detects -> NULL.
    pick = PickOut(
        **_db_pick_kwargs("Beta United"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
    )
    teams = EventTeams(home="Alpha FC", away="Beta United")
    outcome = await persist_pick(_session, pick, teams, "model", "1")
    assert outcome == "inserted"
    stored = await _session.scalar(select(Pick).where(Pick.selection == "Beta United"))
    assert stored is not None
    assert stored.anchor_book_count is None
    assert "phi" not in stored.stake_breakdown
