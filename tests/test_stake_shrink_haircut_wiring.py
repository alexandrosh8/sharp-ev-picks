"""B6 edge-uncertainty shrink + B7 same-event correlation haircut wiring.

Both ship OFF: with the default knobs (StakePolicy.edge_uncertainty_coef None,
ValuePolicy.stake_same_event_rho 0.0) every recommended stake is bit-for-bit
identical to the plain path. Armed, each correction can only SHRINK stakes —
never raise them — and the same-event haircut can only reduce the
simultaneous-event stake sum. Stakes stay informational (picks-only platform).
"""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config import stake_policy, value_policy
from app.edge.gates import GatePolicy
from app.edge.value_policy import ValuePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import NullModel
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import PipelineDeps, _candidate_edge_variance, run_value_pipeline
from app.risk.exposure import DailyExposureLedger, same_event_stake_multipliers
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from tests.test_config import make_settings
from tests.test_value_pipeline import FakeLoader, RecordingSink

POLICY = GatePolicy(
    min_edge=0.0,
    min_ev=0.0,
    min_confidence=0.0,
    max_odds_age_seconds=300,
    min_liquidity=0.0,
)
BARE_STAKES = StakePolicy()  # frozen — safe as shared defaults
BARE_POLICY = ValuePolicy()


def snap(book: str, sel: str, odds: float, market: Market = Market.TOTALS) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-1",
        bookmaker=book,
        market=market,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=30),
        ingested_at=now,
    )


def totals_snapshots() -> list[OddsSnapshotIn]:
    return [
        snap("Pinnacle", "Over 2.5", 1.90),
        snap("Pinnacle", "Under 2.5", 1.90),
        snap("SoftBook", "Over 2.5", 2.20),
        snap("SoftBook", "Under 2.5", 1.75),
    ]


def h2h_snapshots() -> list[OddsSnapshotIn]:
    return [
        snap("Pinnacle", "Home FC", 2.50, Market.H2H),
        snap("Pinnacle", "Draw", 3.30, Market.H2H),
        snap("Pinnacle", "Away FC", 3.10, Market.H2H),
        snap("SoftBook", "Home FC", 2.90, Market.H2H),
        snap("SoftBook", "Draw", 3.20, Market.H2H),
        snap("SoftBook", "Away FC", 2.95, Market.H2H),
    ]


def make_deps(
    sink: RecordingSink,
    loader: FakeLoader,
    *,
    stakes: StakePolicy = BARE_STAKES,
    policy: ValuePolicy = BARE_POLICY,
) -> PipelineDeps:
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
        loader=loader,
        model=NullModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=POLICY,
        stake_policy=stakes,
        ledger=DailyExposureLedger(max_daily_fraction=0.5),
        bankroll=Decimal("1000"),
        directory=directory,
        value_min_edge=0.015,
        value_min_odds=1.30,
        value_policy=policy,
    )


# --- B6 edge-uncertainty shrink ----------------------------------------------


async def test_uncertainty_shrink_off_is_byte_identical() -> None:
    # Default StakePolicy (edge_uncertainty_coef None) reproduces the plain
    # fractional-Kelly stake exactly on the same slate.
    baseline_sink = RecordingSink()
    baseline = await run_value_pipeline(
        make_deps(baseline_sink, FakeLoader(totals_snapshots())), "soccer"
    )
    off_sink = RecordingSink()
    off = await run_value_pipeline(
        make_deps(
            off_sink,
            FakeLoader(totals_snapshots()),
            stakes=StakePolicy(edge_uncertainty_coef=None),
        ),
        "soccer",
    )
    assert len(baseline) == len(off) == 1
    assert off[0].recommended_stake_fraction == baseline[0].recommended_stake_fraction
    assert off[0].stake_breakdown.model_dump() == baseline[0].stake_breakdown.model_dump()


async def test_uncertainty_shrink_on_is_monotonic_never_raises() -> None:
    # Armed, a bigger coef can only shrink the stake further; nothing ever
    # exceeds the OFF stake. The slate carries real cross-book disagreement
    # (Pinnacle 1.90 vs SoftBook 2.20 on the Over), so variance > 0.
    fractions: list[float] = []
    for coef in (None, 10.0, 1000.0, 50000.0):
        sink = RecordingSink()
        picks = await run_value_pipeline(
            make_deps(
                sink,
                FakeLoader(totals_snapshots()),
                stakes=StakePolicy(edge_uncertainty_coef=coef),
            ),
            "soccer",
        )
        assert len(picks) == 1
        fractions.append(picks[0].recommended_stake_fraction)
    assert all(f >= 0.0 for f in fractions)
    for off, on in zip(fractions, fractions[1:], strict=False):
        assert on <= off  # monotonically non-increasing in coef
    assert fractions[-1] < fractions[0]  # the shrink actually bites at big coef


def test_candidate_edge_variance_properties() -> None:
    prices = {
        "Over 2.5": {"Pinnacle": 1.90, "SoftBook": 2.20},
        "Under 2.5": {"Pinnacle": 1.90, "SoftBook": 1.75},
    }
    fair = {"Over 2.5": 0.5, "Under 2.5": 0.5}
    var = _candidate_edge_variance(prices, fair, "Pinnacle", "Over 2.5")
    assert var > 0.0  # cross-book disagreement is real on this slate
    # Fail-soft shapes contribute zero, never an error:
    assert _candidate_edge_variance({}, {}, "Pinnacle", "Over 2.5") == 0.0
    one_book = {"Over 2.5": {"Pinnacle": 1.90}, "Under 2.5": {"Pinnacle": 1.90}}
    assert _candidate_edge_variance(one_book, fair, "NoSuchBook", "Over 2.5") == 0.0


def test_config_defaults_keep_both_knobs_inert() -> None:
    s = make_settings()
    assert stake_policy(s).edge_uncertainty_coef is None  # 0.0 sentinel -> None
    assert value_policy(s).stake_same_event_rho == 0.0
    assert value_policy(s).shots_totals_veto is False
    armed = make_settings(
        stake_edge_uncertainty_coef=25.0,
        stake_same_event_rho=0.4,
        value_shots_totals_veto=True,
    )
    assert stake_policy(armed).edge_uncertainty_coef == 25.0
    assert value_policy(armed).stake_same_event_rho == 0.4
    assert value_policy(armed).shots_totals_veto is True


# --- B7 same-event correlation haircut ----------------------------------------


def test_same_event_multipliers_shrink_only() -> None:
    ids = ["a", "a", "a", "b", "b", "c"]
    mults = same_event_stake_multipliers(ids, 0.5)
    assert mults["c"] == 1.0  # single leg untouched
    assert mults["b"] == 1.0 / math.sqrt(1.5)
    assert mults["a"] == 1.0 / math.sqrt(2.0)
    assert all(0.0 < m <= 1.0 for m in mults.values())
    # rho == 0 is the bit-identical default path
    assert set(same_event_stake_multipliers(ids, 0.0).values()) == {1.0}


async def test_correlation_haircut_reduces_simultaneous_event_stake_sum() -> None:
    # Two premium picks on the SAME event in one cycle (H2H + totals). With
    # rho armed each stake shrinks by 1/sqrt(1+rho); the event stake sum can
    # only go DOWN, and no individual stake ever rises.
    slate = h2h_snapshots() + totals_snapshots()

    off_sink = RecordingSink()
    off = await run_value_pipeline(make_deps(off_sink, FakeLoader(list(slate))), "soccer")
    assert len(off) == 2
    off_by_sel = {p.selection: p.recommended_stake_fraction for p in off}

    on_sink = RecordingSink()
    on = await run_value_pipeline(
        make_deps(
            on_sink,
            FakeLoader(list(slate)),
            policy=ValuePolicy(stake_same_event_rho=0.75),
        ),
        "soccer",
    )
    assert len(on) == 2
    on_by_sel = {p.selection: p.recommended_stake_fraction for p in on}

    assert sum(on_by_sel.values()) < sum(off_by_sel.values())
    mult = 1.0 / math.sqrt(1.75)
    for sel, off_stake in off_by_sel.items():
        assert on_by_sel[sel] <= off_stake  # never raises
        assert on_by_sel[sel] == off_stake * mult
    haircut_pick = on[0]
    assert "same-event correlation haircut" in haircut_pick.reason_summary


async def test_correlation_haircut_single_event_leg_untouched() -> None:
    # One premium pick only: rho armed changes nothing (N == 1 multiplier 1.0).
    off_sink = RecordingSink()
    off = await run_value_pipeline(make_deps(off_sink, FakeLoader(totals_snapshots())), "soccer")
    on_sink = RecordingSink()
    on = await run_value_pipeline(
        make_deps(
            on_sink,
            FakeLoader(totals_snapshots()),
            policy=ValuePolicy(stake_same_event_rho=0.75),
        ),
        "soccer",
    )
    assert len(off) == len(on) == 1
    assert on[0].recommended_stake_fraction == off[0].recommended_stake_fraction
    assert "haircut" not in on[0].reason_summary
