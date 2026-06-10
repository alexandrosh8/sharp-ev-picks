"""End-to-end pipeline cycle with fakes: snapshots -> devig -> model -> gates
-> stake -> alert. No network, no DB."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.edge.gates import GatePolicy
from app.models.base import PredictedProbability
from app.notifications.base import Alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import PipelineDeps, run_pick_pipeline
from app.probabilities.devig import DevigMethod
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

NOW = datetime.now(tz=UTC)

POLICY = GatePolicy(
    min_edge=0.03,
    min_ev=0.01,
    min_confidence=0.60,
    max_odds_age_seconds=300,
    min_liquidity=0.0,
)


class FakeLoader:
    """Two-way totals book: Over/Under both at 2.10 (fair ~0.50 each)."""

    def __init__(self) -> None:
        self.snapshots = [
            OddsSnapshotIn(
                event_id="evt-1",
                bookmaker="bookie",
                market=Market.TOTALS,
                selection=name,
                decimal_odds=2.10,
                captured_at=NOW - timedelta(seconds=30),
                ingested_at=NOW,
            )
            for name in ("Over 2.5", "Under 2.5")
        ]

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        return self.snapshots


class FakeModel:
    name = "fake"
    version = "1"

    async def predict(self, event_id: str) -> Sequence[PredictedProbability]:
        return (
            PredictedProbability(
                market=Market.TOTALS, selection="Over 2.5", probability=0.58, confidence=0.8
            ),
        )


class RecordingSink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def make_deps(sink: RecordingSink) -> PipelineDeps:
    return PipelineDeps(
        loader=FakeLoader(),
        model=FakeModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=POLICY,
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        devig_method=DevigMethod.MULTIPLICATIVE,
    )


async def test_pipeline_produces_pick_and_alert() -> None:
    sink = RecordingSink()
    picks = await run_pick_pipeline(make_deps(sink), "soccer_epl")
    # fair prob = 0.5 each; model 0.58 -> edge 0.08, EV = 0.58*1.1-0.42 = 0.218
    assert len(picks) == 1
    pick = picks[0]
    assert pick.selection == "Over 2.5"
    assert pick.edge > 0.03
    assert pick.ev > 0.01
    assert pick.recommended_stake_fraction <= 0.02
    assert len(sink.sent) == 1
    assert "does not place bets" in sink.sent[0].body


async def test_pipeline_rerun_suppresses_duplicate_alert() -> None:
    sink = RecordingSink()
    deps = make_deps(sink)
    first = await run_pick_pipeline(deps, "soccer_epl")
    second = await run_pick_pipeline(deps, "soccer_epl")
    # the edge is re-detected each cycle (pick produced) but the alert key is
    # stable market state, so only ONE alert reaches the sink
    assert len(first) == 1
    assert len(second) == 1
    assert len(sink.sent) == 1


async def test_pipeline_no_model_predictions_no_picks() -> None:
    sink = RecordingSink()
    deps = make_deps(sink)

    class SilentModel:
        name = "silent"
        version = "0"

        async def predict(self, event_id: str) -> Sequence[PredictedProbability]:
            return ()

    deps.model = SilentModel()
    picks = await run_pick_pipeline(deps, "soccer_epl")
    assert picks == []
    assert sink.sent == []
