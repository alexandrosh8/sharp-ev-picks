"""Task 5: uncertainty-shrunk Kelly — SHADOW annotation only.

Pure property tests for ``uncertainty_shrink`` / ``uncertainty_phi`` (Baker &
McHale 2013 pattern: multiply the Kelly fraction by phi = n_eff/(n_eff+kappa),
half-weight at n_eff == kappa), the frozen ``UncertaintyShrinkPolicy``
dataclass, the ``Settings`` knobs (default OFF, kappa=50), and the pipeline
SHADOW annotation: ``stake_breakdown`` gains ``{"phi","n_eff",
"shrunk_fraction"}`` while the FINAL stake stays byte-identical with the flag
off (the default).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings, uncertainty_shrink_policy
from app.edge.gates import GatePolicy
from app.edge.value_policy import ValuePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import NullModel
from app.notifications.base import Alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import PipelineDeps, run_value_pipeline
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import (
    StakePolicy,
    UncertaintyShrinkPolicy,
    uncertainty_phi,
    uncertainty_shrink,
)
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

# --------------------------------------------------------------------------- #
# Pure math properties
# --------------------------------------------------------------------------- #


def test_shrink_half_weight_at_kappa() -> None:
    assert uncertainty_shrink(0.02, n_eff=50, kappa=50) == pytest.approx(0.01)


def test_shrink_monotone_in_n_eff() -> None:
    fs = [uncertainty_shrink(0.02, n, 50) for n in (0, 10, 50, 500, 50_000)]
    assert fs == sorted(fs)
    assert fs[0] == 0.0
    assert fs[-1] < 0.02


def test_phi_in_unit_interval_and_never_exceeds_one() -> None:
    for n in (0, 1, 7, 50, 10_000, 10**9):
        phi = uncertainty_phi(n, 50.0)
        assert 0.0 <= phi < 1.0  # phi -> 1 only in the n_eff -> inf limit


def test_shrunk_never_exceeds_original_and_never_negative() -> None:
    for fraction in (0.0, 0.005, 0.02, 0.25):
        for n in (0, 3, 50, 5_000):
            shrunk = uncertainty_shrink(fraction, n, 50.0)
            assert 0.0 <= shrunk <= fraction


def test_shrink_approaches_identity_as_n_eff_grows() -> None:
    assert uncertainty_shrink(0.02, 10**9, 50.0) == pytest.approx(0.02, rel=1e-6)


@pytest.mark.parametrize("kappa", [0.0, -1.0, -50.0])
def test_kappa_nonpositive_raises(kappa: float) -> None:
    with pytest.raises(ValueError):
        uncertainty_shrink(0.02, 50, kappa)
    with pytest.raises(ValueError):
        uncertainty_phi(50, kappa)


def test_negative_n_eff_raises() -> None:
    with pytest.raises(ValueError):
        uncertainty_shrink(0.02, -1, 50.0)


def test_negative_fraction_raises() -> None:
    with pytest.raises(ValueError):
        uncertainty_shrink(-0.01, 50, 50.0)


# --------------------------------------------------------------------------- #
# Policy dataclass + Settings knobs
# --------------------------------------------------------------------------- #


def test_policy_defaults_off_kappa_50() -> None:
    policy = UncertaintyShrinkPolicy()
    assert policy.enabled is False
    assert policy.kappa == 50.0


def test_policy_rejects_nonpositive_kappa() -> None:
    with pytest.raises(ValueError):
        UncertaintyShrinkPolicy(kappa=0.0)


def test_settings_default_off_and_builder() -> None:
    s = Settings(_env_file=None)
    assert s.stake_uncertainty_shrink_enabled is False
    assert s.stake_uncertainty_kappa == 50.0
    policy = uncertainty_shrink_policy(s)
    assert policy == UncertaintyShrinkPolicy(enabled=False, kappa=50.0)


def test_settings_rejects_nonpositive_kappa() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, stake_uncertainty_kappa=0.0)


# --------------------------------------------------------------------------- #
# Pipeline SHADOW annotation (value strategy)
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

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        return self.snapshots


class _Sink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def _deps(
    *,
    stake_shrink: UncertaintyShrinkPolicy | None = None,
    stake_neff_lookup: object = None,
) -> PipelineDeps:
    directory = EventDirectory()
    directory.register("evt-1", EventTeams(home="Home FC", away="Away FC"))
    kwargs: dict[str, object] = {}
    if stake_shrink is not None:
        kwargs["stake_shrink"] = stake_shrink
    if stake_neff_lookup is not None:
        kwargs["stake_neff_lookup"] = stake_neff_lookup
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
        value_min_edge=0.015,
        value_volume_min_edge=0.015,
        value_min_odds=1.30,
        value_policy=ValuePolicy(),
        **kwargs,  # type: ignore[arg-type]
    )


async def test_shadow_annotation_rides_stake_breakdown_flag_off() -> None:
    # n_eff == kappa (50) -> phi == 0.5, shrunk == fractional / 2 —
    # annotated ONLY; the final stake must stay the unshrunk value.
    deps = _deps(stake_neff_lookup=lambda strategy, sport, market: 50)
    picks = await run_value_pipeline(deps, "soccer")
    assert picks, "expected one premium value pick"
    dumped = picks[0].stake_breakdown.model_dump()
    assert dumped["n_eff"] == 50
    assert dumped["phi"] == pytest.approx(0.5)
    assert dumped["shrunk_fraction"] == pytest.approx(dumped["fractional"] * 0.5)
    # SHADOW: flag off (default) -> final is the plain capped fraction
    assert dumped["final"] == min(dumped["fractional"], StakePolicy().max_stake_fraction)
    assert picks[0].recommended_stake_fraction == dumped["final"]


async def test_annotation_none_when_n_eff_unavailable() -> None:
    # No lookup wired (production default until the composition root binds
    # one): the keys are present but honestly None — never fabricated.
    picks = await run_value_pipeline(_deps(), "soccer")
    assert picks
    dumped = picks[0].stake_breakdown.model_dump()
    assert dumped["n_eff"] is None
    assert dumped["phi"] is None
    assert dumped["shrunk_fraction"] is None
    assert dumped["final"] == min(dumped["fractional"], StakePolicy().max_stake_fraction)


async def test_lookup_failure_is_isolated_and_annotates_none() -> None:
    def _boom(strategy: str, sport: str, market: str) -> int:
        raise RuntimeError("cell aggregation unavailable")

    picks = await run_value_pipeline(_deps(stake_neff_lookup=_boom), "soccer")
    assert picks  # a lookup failure must never break minting
    dumped = picks[0].stake_breakdown.model_dump()
    assert dumped["n_eff"] is None
    assert dumped["phi"] is None


async def test_enabled_flag_applies_shrunk_fraction_to_final() -> None:
    # Enforcement lever (pre-registered review only — ADR gate): with the flag
    # ON the final stake becomes min(shrunk, plain final). Guarded here so
    # flipping the flag later has exactly one, tested meaning.
    deps = _deps(
        stake_shrink=UncertaintyShrinkPolicy(enabled=True, kappa=50.0),
        stake_neff_lookup=lambda strategy, sport, market: 50,
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert picks
    dumped = picks[0].stake_breakdown.model_dump()
    assert dumped["shrunk_fraction"] == pytest.approx(dumped["fractional"] * 0.5)
    assert dumped["final"] == pytest.approx(
        min(dumped["shrunk_fraction"], StakePolicy().max_stake_fraction)
    )
    assert picks[0].recommended_stake_fraction == dumped["final"]


async def test_lookup_receives_strategy_sport_market_cell() -> None:
    seen: list[tuple[str, str, str]] = []

    def _lookup(strategy: str, sport: str, market: str) -> int | None:
        seen.append((strategy, sport, market))
        return None

    await run_value_pipeline(_deps(stake_neff_lookup=_lookup), "soccer")
    assert ("value", "soccer", "h2h") in seen
