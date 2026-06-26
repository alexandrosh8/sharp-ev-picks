"""End-to-end pipeline cycle with fakes: snapshots -> devig -> model -> gates
-> stake -> alert. No network, no DB."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.edge.gates import GatePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import PredictedProbability
from app.notifications.base import Alert, build_pick_alert
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
    assert "you place any bet" not in sink.sent[0].body  # footer removed per operator request


async def test_model_pipeline_alert_key_includes_strategy_identity() -> None:
    sink = RecordingSink()
    deps = make_deps(sink)
    deps.model_name = "football-dixon-coles"
    deps.model_version = "v4"

    picks = await run_pick_pipeline(deps, "soccer_epl")

    assert len(picks) == 1
    expected = build_pick_alert(
        picks[0],
        model_name=deps.model_name,
        model_version=deps.model_version,
    )
    assert sink.sent[0].dedupe_key == expected.dedupe_key


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


async def test_model_pipeline_stamps_polled_sport_not_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: run_pick_pipeline stamped persisted snapshots + picks with the
    # (now removed) deps.sport default "soccer" even for a basketball poll.
    import app.pipeline as pl

    captured: dict[str, str] = {}

    async def spy_persist(deps, snapshots, sport, league, now):  # type: ignore[no-untyped-def]
        captured["sport"] = sport
        return 0

    monkeypatch.setattr(pl, "_persist_snapshots", spy_persist)
    picks = await run_pick_pipeline(make_deps(RecordingSink()), "basketball")
    assert captured["sport"] == "basketball"  # the warehouse persist arg
    assert len(picks) == 1
    assert picks[0].sport == "basketball"  # PickOut.sport


class FakeSessionFactory:
    """Minimal async-contextmanager session for the persistence seam."""

    def __call__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
        return False

    async def commit(self) -> None:
        return None


def patch_persist_dedupe_after_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """persist_pick inserts on the FIRST call, dedupes every later call —
    the DB unique key (event, market, selection, model) ignores odds."""
    import app.storage.repositories as repos

    calls = {"n": 0}

    async def fake_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return "inserted" if calls["n"] == 1 else "duplicate"

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)


def make_persisting_deps(sink: RecordingSink) -> PipelineDeps:
    deps = make_deps(sink)
    directory = EventDirectory()
    directory.register("evt-1", EventTeams(home="Over Town", away="Under City"))
    deps.directory = directory
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    return deps


async def test_pick_pipeline_duplicate_releases_exposure_and_unchanged_odds_stay_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port of the value-pipeline H1 regression to run_pick_pipeline: a DB
    duplicate hands its exposure grant back (no daily-cap leak) but the alert
    is still dispatched — unchanged odds are suppressed by the idempotency
    store, so exactly one alert reaches the sink."""
    patch_persist_dedupe_after_first(monkeypatch)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)

    day = datetime.now(tz=UTC).date()
    first = await run_pick_pipeline(deps, "soccer_epl")
    assert len(first) == 1
    used_after_first = deps.ledger.used(day)
    assert used_after_first > 0.0

    second = await run_pick_pipeline(deps, "soccer_epl")
    assert second == []  # duplicate is not a new pick this cycle
    assert deps.ledger.used(day) == pytest.approx(used_after_first)  # grant returned
    assert len(sink.sent) == 1  # idempotency (key includes odds) suppressed it


async def test_pick_pipeline_duplicate_price_move_realerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A price move on a pick the DB already knows must re-alert (the alert
    dedupe key includes decimal_odds by design) while the exposure grant for
    the duplicate is still released."""
    patch_persist_dedupe_after_first(monkeypatch)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)

    day = datetime.now(tz=UTC).date()
    first = await run_pick_pipeline(deps, "soccer_epl")
    assert len(first) == 1
    assert len(sink.sent) == 1
    used_after_first = deps.ledger.used(day)

    # the book moves both totals prices 2.10 -> 2.20: same DB row, new state
    deps.loader.snapshots = [  # type: ignore[attr-defined]
        OddsSnapshotIn(
            event_id="evt-1",
            bookmaker="bookie",
            market=Market.TOTALS,
            selection=name,
            decimal_odds=2.20,
            captured_at=NOW - timedelta(seconds=30),
            ingested_at=NOW,
        )
        for name in ("Over 2.5", "Under 2.5")
    ]
    second = await run_pick_pipeline(deps, "soccer_epl")
    assert second == []  # still not a NEW pick
    assert deps.ledger.used(day) == pytest.approx(used_after_first)  # grant returned
    assert len(sink.sent) == 2  # price move re-alerted
    assert "2.20" in sink.sent[1].title


async def test_unpersisted_premium_pick_does_not_accumulate_exposure() -> None:
    """kelly-risk-r2-1: with persistence unavailable (no session factory),
    _maybe_persist returns 'unpersisted'. A premium pick re-detected every
    cycle must NOT accumulate standing daily exposure — otherwise a sustained-
    unpersisted pick silently exhausts the 5% cap and suppresses later alerts.
    The pick still flows (alerted) but reserves nothing it can never release."""
    sink = RecordingSink()
    deps = make_deps(sink)  # no session_factory -> outcome == "unpersisted"
    day = datetime.now(tz=UTC).date()

    first = await run_pick_pipeline(deps, "soccer_epl")
    assert len(first) == 1
    assert deps.ledger.used(day) == 0.0  # unpersisted reserves NOTHING

    second = await run_pick_pipeline(deps, "soccer_epl")
    assert len(second) == 1
    assert deps.ledger.used(day) == 0.0  # still zero -> no cross-cycle accumulation


async def test_duplicate_realert_uses_persisted_stake_even_when_cap_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kr-1: an already-persisted pick re-detected as a DB duplicate must
    re-alert with the stake from its persisted row (breakdown.final, never
    daily-clipped) and must NOT be skipped just because the daily cap is now
    exhausted (a fresh reserve would grant 0)."""
    patch_persist_dedupe_after_first(monkeypatch)

    import app.pipeline as pl

    real_build = pl.build_pick_alert
    captured: list[float] = []

    def spy_build(pick, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(pick.recommended_stake_fraction)
        return real_build(pick, *args, **kwargs)

    monkeypatch.setattr(pl, "build_pick_alert", spy_build)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)
    day = datetime.now(tz=UTC).date()

    first = await run_pick_pipeline(deps, "soccer_epl")
    assert len(first) == 1
    persisted_stake = first[0].recommended_stake_fraction
    assert persisted_stake > 0.0
    assert first[0].stake_breakdown.daily_clipped is False

    # Exhaust the rest of the daily cap so a fresh reserve would grant ~0.
    deps.ledger.reserve(day, deps.ledger.remaining(day))
    assert deps.ledger.remaining(day) == pytest.approx(0.0)

    second = await run_pick_pipeline(deps, "soccer_epl")
    assert second == []  # a duplicate is not a NEW pick this cycle
    assert len(captured) == 2  # re-alert NOT skipped despite the exhausted cap
    # the re-alert stake equals the persisted row's stake, not a daily-clipped 0
    assert captured[1] == pytest.approx(persisted_stake)


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


def _line_snap(detail: str, selection: str, odds: float) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id="evt-1",
        bookmaker="bookie",
        market=Market.TOTALS,
        selection=selection,
        decimal_odds=odds,
        captured_at=NOW - timedelta(seconds=30),
        ingested_at=NOW,
        market_detail=detail,
    )


def test_fair_probabilities_devig_each_line_separately() -> None:
    """Two totals lines at ONE book are TWO 2-leg markets, never one 4-leg
    book — mixing lines corrupts devig (the value pipeline already groups by
    market_detail; the model pipeline must apply the same rule)."""
    from app.pipeline import _fair_probabilities

    snapshots = [
        _line_snap("over_under_2_5", "Over 2.5", 2.0),
        _line_snap("over_under_2_5", "Under 2.5", 2.0),
        _line_snap("over_under_3_5", "Over 3.5", 2.60),
        _line_snap("over_under_3_5", "Under 3.5", 1.55),
    ]
    fair = _fair_probabilities(snapshots, DevigMethod.MULTIPLICATIVE)

    # 2.0/2.0 devigs to exactly 0.5 within its OWN line; pooled with the
    # 3.5-line legs it would come out ~0.246.
    assert fair[("evt-1", "bookie", Market.TOTALS, "Over 2.5")] == pytest.approx(0.5)
    assert fair[("evt-1", "bookie", Market.TOTALS, "Under 2.5")] == pytest.approx(0.5)
    line_35 = [
        fair[("evt-1", "bookie", Market.TOTALS, "Over 3.5")],
        fair[("evt-1", "bookie", Market.TOTALS, "Under 3.5")],
    ]
    assert sum(line_35) == pytest.approx(1.0)


def test_fair_probabilities_single_leg_line_is_skipped() -> None:
    """A line with only one priced side cannot be devigged — per-line
    grouping must not let another line's legs make it look complete."""
    from app.pipeline import _fair_probabilities

    snapshots = [
        _line_snap("over_under_2_5", "Over 2.5", 2.0),
        _line_snap("over_under_2_5", "Under 2.5", 2.0),
        _line_snap("over_under_3_5", "Over 3.5", 2.60),  # Under 3.5 missing
    ]
    fair = _fair_probabilities(snapshots, DevigMethod.MULTIPLICATIVE)
    assert ("evt-1", "bookie", Market.TOTALS, "Over 3.5") not in fair
    assert fair[("evt-1", "bookie", Market.TOTALS, "Over 2.5")] == pytest.approx(0.5)
