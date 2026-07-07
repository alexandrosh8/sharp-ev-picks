"""Wiring test: run_value_pipeline persists a candidate_evaluations row per
candidate (external-audit #3 / fill #2).

Drives the real value pipeline over a small synthetic slate against the compose
Postgres (:5433, ``betting_ai_test``) and asserts the audit rows land with the
right tier + reason slugs: one premium-KEPT (empty reasons) and one demoted
(no_sharp_anchor). Skipped when Postgres is absent; runs inside ONE rolled-back
transaction (the tests/test_candidate_audit.py ``factory`` pattern) so nothing
commits to the shared test DB. No network, ever.

Pure MEASUREMENT: this exercises the audit tap only. The selection/tier/alert
behavior is already covered unchanged by tests/test_value_pipeline.py.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.edge.gates import GatePolicy
from app.edge.value_policy import ValuePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import NullModel
from app.notifications.base import Alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import PipelineDeps, run_value_pipeline
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.storage.models import CandidateEvaluation, Event

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"

POLICY = GatePolicy(
    min_edge=0.0,
    min_ev=0.0,
    min_confidence=0.0,
    max_odds_age_seconds=300,
    min_liquidity=0.0,
)


def _snap(event_id: str, book: str, sel: str, odds: float) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id=event_id,
        bookmaker=book,
        market=Market.H2H,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=30),
        ingested_at=now,
    )


def _slate() -> list[OddsSnapshotIn]:
    # evt-prem: Pinnacle anchors (named sharp) — Home FC premium-KEPT.
    # evt-demo: three SOFT books only — consensus anchor => no_sharp_anchor demote.
    return [
        _snap("evt-prem", "Pinnacle", "Home FC", 2.50),
        _snap("evt-prem", "Pinnacle", "Draw", 3.30),
        _snap("evt-prem", "Pinnacle", "Away FC", 3.10),
        _snap("evt-prem", "SoftBook", "Home FC", 2.90),
        _snap("evt-prem", "SoftBook", "Draw", 3.20),
        _snap("evt-prem", "SoftBook", "Away FC", 2.95),
        _snap("evt-demo", "SoftA", "Home FC", 2.45),
        _snap("evt-demo", "SoftA", "Draw", 3.30),
        _snap("evt-demo", "SoftA", "Away FC", 3.10),
        _snap("evt-demo", "SoftB", "Home FC", 2.50),
        _snap("evt-demo", "SoftB", "Draw", 3.25),
        _snap("evt-demo", "SoftB", "Away FC", 3.05),
        _snap("evt-demo", "SoftC", "Home FC", 2.95),
        _snap("evt-demo", "SoftC", "Draw", 3.20),
        _snap("evt-demo", "SoftC", "Away FC", 2.95),
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


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:  # noqa: BLE001
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


def _make_deps(sink: _Sink, factory: async_sessionmaker) -> PipelineDeps:
    directory = EventDirectory()
    directory.register("evt-prem", EventTeams(home="Home FC", away="Away FC", league="Premier"))
    directory.register("evt-demo", EventTeams(home="Home FC", away="Away FC", league="GFA League"))
    return PipelineDeps(
        loader=_FakeLoader(_slate()),
        model=NullModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=POLICY,
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        directory=directory,
        session_factory=factory,
        value_min_edge=0.015,
        value_min_odds=1.30,
        # require_sharp_anchor => the consensus-anchored evt-demo demotes to volume.
        value_policy=ValuePolicy(require_sharp_anchor=True),
    )


async def _rows_for(factory: async_sessionmaker, external_ref: str) -> list[CandidateEvaluation]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(CandidateEvaluation)
                    .join(Event, CandidateEvaluation.event_id == Event.id)
                    .where(Event.external_ref == external_ref)
                    .order_by(CandidateEvaluation.id)
                )
            )
            .scalars()
            .all()
        )


async def test_pipeline_writes_candidate_audit_rows(factory: async_sessionmaker) -> None:
    sink = _Sink()
    deps = _make_deps(sink, factory)

    await run_value_pipeline(deps, "soccer")

    # premium-KEPT: one row, tier=premium, NO reasons (clean keep), fill provenance.
    prem = await _rows_for(factory, "evt-prem")
    assert [r.tier for r in prem] == ["premium"]
    kept = prem[0]
    assert kept.reasons is None
    assert kept.selection == "Home FC"
    assert kept.anchor_type == "pinnacle"
    assert kept.best_book == "SoftBook"
    assert kept.best_odds == Decimal("2.9000")
    assert kept.edge is not None and kept.edge > 0

    # demoted-to-shadow: one row, tier=volume, no_sharp_anchor reason slug.
    demo = await _rows_for(factory, "evt-demo")
    assert [r.tier for r in demo] == ["volume"]
    dem = demo[0]
    assert dem.reasons == {"reasons": ["no_sharp_anchor"]}
    assert dem.anchor_type == "consensus"
