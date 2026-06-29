"""H3: every value pick carries a POLICY FINGERPRINT of the live policy regime.

Picks were stamped a static model_version ("v3"); the policy that actually
minted each one (thresholds, devig, require-sharp-anchor, data-error ceiling,
ML manifest identity) was NOT captured, so CLV attribution silently mixed
policy regimes across config changes. These tests pin the compact, human-
debuggable fingerprint:

- the pure encoder is deterministic (same policy -> same string) and sensitive
  (any policy field change -> a different string),
- a freshly-minted value pick carries the fingerprint of the ACTIVE policy,
- the ORM column + Alembic migration are nullable / additive / chain off head.
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
    POLICY_FINGERPRINT_SCHEMA,
    PipelineDeps,
    policy_fingerprint,
    run_value_pipeline,
)
from app.probabilities.devig import DevigMethod
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Pick
from app.storage.repositories import persist_pick

# --------------------------------------------------------------------------- #
# Pure encoder
# --------------------------------------------------------------------------- #

_BASE_KWARGS: dict[str, Any] = dict(
    value_min_edge=0.015,
    value_volume_min_edge=0.015,
    value_min_odds=1.30,
    devig_method=DevigMethod.POWER,
    require_sharp_anchor=False,
    max_edge=float("inf"),
)


def test_fingerprint_is_schema_versioned_and_decodable() -> None:
    fp = policy_fingerprint(**_BASE_KWARGS)
    assert fp.startswith(f"{POLICY_FINGERPRINT_SCHEMA}|")
    # human-debuggable: the active knobs read straight out of the string
    assert "me=0.0150" in fp
    assert "vme=0.0150" in fp
    assert "mo=1.30" in fp
    assert "dv=power" in fp
    assert "rsa=0" in fp
    assert "mxe=inf" in fp
    assert "ml=off" in fp


def test_same_policy_yields_identical_fingerprint() -> None:
    assert policy_fingerprint(**_BASE_KWARGS) == policy_fingerprint(**_BASE_KWARGS)


def test_different_min_edge_yields_different_fingerprint() -> None:
    a = policy_fingerprint(**_BASE_KWARGS)
    b = policy_fingerprint(**{**_BASE_KWARGS, "value_min_edge": 0.03})
    assert a != b


def test_require_sharp_anchor_flip_yields_different_fingerprint() -> None:
    a = policy_fingerprint(**_BASE_KWARGS)
    b = policy_fingerprint(**{**_BASE_KWARGS, "require_sharp_anchor": True})
    assert a != b
    assert "rsa=1" in b


def test_devig_method_change_yields_different_fingerprint() -> None:
    a = policy_fingerprint(**_BASE_KWARGS)
    b = policy_fingerprint(**{**_BASE_KWARGS, "devig_method": DevigMethod.SHIN})
    assert a != b
    assert "dv=shin" in b


def test_ceiling_change_yields_different_fingerprint() -> None:
    a = policy_fingerprint(**_BASE_KWARGS)
    b = policy_fingerprint(**{**_BASE_KWARGS, "max_edge": 0.5})
    assert a != b
    assert "mxe=0.5000" in b


def test_ml_manifest_identity_changes_fingerprint_when_enforced() -> None:
    off = policy_fingerprint(**_BASE_KWARGS)
    enforced = policy_fingerprint(
        **_BASE_KWARGS,
        ml_manifest_created_utc="2026-06-12T00:00:00Z",
        ml_threshold=0.5,
    )
    assert off != enforced
    assert "ml=2026-06-12T00:00:00Z@0.500" in enforced
    # a DIFFERENT manifest (newer created_utc) is a different regime
    newer = policy_fingerprint(
        **_BASE_KWARGS,
        ml_manifest_created_utc="2026-07-01T00:00:00Z",
        ml_threshold=0.5,
    )
    assert newer != enforced


# --------------------------------------------------------------------------- #
# Pipeline stamping
# --------------------------------------------------------------------------- #


def _snap(book: str, sel: str, odds: float, age_s: float = 30.0) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-1",
        bookmaker=book,
        market=Market.H2H,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=age_s),
        ingested_at=now,
    )


def _market_snapshots() -> list[OddsSnapshotIn]:
    # Pinnacle anchors a tight 3-way; SoftBook over-prices Home -> a value pick.
    return [
        _snap("Pinnacle", "Home FC", 2.50),
        _snap("Pinnacle", "Draw", 3.30),
        _snap("Pinnacle", "Away FC", 3.10),
        _snap("SoftBook", "Home FC", 2.90),
        _snap("SoftBook", "Draw", 3.20),
        _snap("SoftBook", "Away FC", 2.95),
    ]


class _FakeLoader:
    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        self.snapshots = snapshots
        self.last_fetch_matches: dict[str, int] = {}
        self.last_fetch_event_ids: dict[str, tuple[str, ...]] = {}

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        self.last_fetch_matches[sport_key] = len({s.event_id for s in self.snapshots})
        return self.snapshots


class _Sink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def _deps(*, value_min_edge: float, value_policy: ValuePolicy | None = None) -> PipelineDeps:
    directory = EventDirectory()
    directory.register("evt-1", EventTeams(home="Home FC", away="Away FC"))
    return PipelineDeps(
        loader=_FakeLoader(_market_snapshots()),
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
        value_min_edge=value_min_edge,
        value_volume_min_edge=value_min_edge,
        value_min_odds=1.30,
        value_policy=value_policy or ValuePolicy(),
    )


async def test_minted_pick_carries_active_policy_fingerprint() -> None:
    deps = _deps(value_min_edge=0.015)
    picks = await run_value_pipeline(deps, "soccer")
    assert picks, "expected one premium value pick"
    fp = picks[0].policy_fingerprint
    assert fp is not None
    # reflects the ACTIVE policy, not a static version stamp
    assert fp == policy_fingerprint(
        value_min_edge=0.015,
        value_volume_min_edge=0.015,
        value_min_odds=1.30,
        devig_method=deps.devig_method,
        require_sharp_anchor=False,
        max_edge=float("inf"),
    )


async def test_different_policies_stamp_different_fingerprints() -> None:
    low = await run_value_pipeline(_deps(value_min_edge=0.015), "soccer")
    high_policy = ValuePolicy(require_sharp_anchor=True)
    # require_sharp_anchor only demotes; keep edge low so a pick is still minted
    high = await run_value_pipeline(_deps(value_min_edge=0.015, value_policy=high_policy), "soccer")
    assert low and high
    assert low[0].policy_fingerprint != high[0].policy_fingerprint


# --------------------------------------------------------------------------- #
# ORM column + migration
# --------------------------------------------------------------------------- #

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "c5e9a1f3b7d2_picks_policy_fingerprint.py"
)
PRIOR_HEAD = "b4f2a9c83d1e"


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_mig_c5e9a1f3b7d2", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pick_model_has_policy_fingerprint_column() -> None:
    col = Pick.__table__.columns["policy_fingerprint"]
    assert col.nullable is True
    assert isinstance(col.type, sa.Text)


def test_pick_policy_fingerprint_defaults_none() -> None:
    assert Pick().policy_fingerprint is None


def test_migration_chains_off_head() -> None:
    mod = _load_migration()
    assert mod.revision == "c5e9a1f3b7d2"
    assert mod.down_revision == PRIOR_HEAD
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_migration_adds_column_additively() -> None:
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

    original = mod.op
    mod.op = _RecordingOp
    try:
        mod.upgrade()
    finally:
        mod.op = original
    names = {name for name, _ in added}
    assert names == {"policy_fingerprint"}
    assert isinstance(dict(added)["policy_fingerprint"], sa.Text)


def test_migration_downgrade_drops_column() -> None:
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

    original = mod.op
    mod.op = _RecordingOp
    try:
        mod.downgrade()
    finally:
        mod.op = original
    assert dropped == ["policy_fingerprint"]


# --------------------------------------------------------------------------- #
# Persistence round-trip (DB-gated — skips when compose Postgres is absent)
# --------------------------------------------------------------------------- #

_DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"


def _db_pick(*, fingerprint: str | None, tier: str = "premium") -> PickOut:
    return PickOut(
        pick_id="p-fp",
        sport="soccer",
        league="test-league-fingerprint",
        event="Alpha FC vs Beta United",
        event_id="evt-fingerprint-test",
        market=Market.H2H,
        selection="Alpha FC",
        bookmaker="testbook",
        decimal_odds=2.10,
        model_probability=0.55,
        fair_probability=0.50,
        edge=0.05,
        ev=0.155,
        confidence=0.70,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary="fingerprint persistence test",
        tier=tier,
        policy_fingerprint=fingerprint,
        created_at=datetime(2026, 6, 29, 12, 0, tzinfo=UTC),
    )


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


async def test_fingerprint_round_trips_through_persist_pick(_session: Any) -> None:
    fp = policy_fingerprint(**_BASE_KWARGS)
    teams = EventTeams(home="Alpha FC", away="Beta United")
    outcome = await persist_pick(_session, _db_pick(fingerprint=fp), teams, "value", "v3")
    assert outcome == "inserted"
    stored = await _session.scalar(
        select(Pick).where(Pick.selection == "Alpha FC", Pick.tier == "premium")
    )
    assert stored is not None
    assert stored.policy_fingerprint == fp


async def test_none_fingerprint_tolerated_through_persist_pick(_session: Any) -> None:
    # A model-strategy pick stamps no fingerprint — NULL must persist cleanly.
    teams = EventTeams(home="Alpha FC", away="Beta United")
    outcome = await persist_pick(_session, _db_pick(fingerprint=None), teams, "model", "1")
    assert outcome == "inserted"
    stored = await _session.scalar(select(Pick).where(Pick.selection == "Alpha FC"))
    assert stored is not None
    assert stored.policy_fingerprint is None
