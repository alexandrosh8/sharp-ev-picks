"""Property-based invariants for Kelly staking (P1-hole H4).

Example-based coverage lives in ``test_kelly.py``; this file fuzzes the staking
contract over hypothesis-generated (edge, odds, bankroll, fraction, caps)
inputs — including the optional drawdown / edge-uncertainty / correlation knobs
— so a sizing path that misbehaves OUTSIDE the literal example region is caught.

Invariants asserted:
  * stake is NEVER negative (raw, fractional, final, money),
  * stake NEVER exceeds the configured per-bet cap (and full Kelly never
    exceeds the whole bankroll),
  * zero / negative edge -> exactly zero stake.

Note: ``app.risk.staking`` exposes only a per-bet cap (``max_stake_fraction``);
there is no daily-cap knob in this module, so "daily cap" is not exercised here.
"""

import math
from decimal import Decimal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.risk.staking import StakePolicy, kelly_fraction, recommended_stake, stake_amount

_PROB = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_ODDS = st.floats(min_value=1.01, max_value=1000.0, allow_nan=False, allow_infinity=False)
_FRACTION = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_CAP = st.floats(min_value=1e-4, max_value=1.0, allow_nan=False, allow_infinity=False)


@st.composite
def _policies(draw: st.DrawFn) -> StakePolicy:
    """Policies spanning the default path AND every optional shrink knob.

    The knobs (drawdown constraint, edge-uncertainty, correlation) can only
    tighten the Kelly multiplier, so the non-negative / cap invariants must hold
    across all of them — that is exactly what we want to stress.
    """
    fractional_kelly = draw(_FRACTION)
    max_stake_fraction = draw(_CAP)
    max_drawdown: float | None = None
    max_drawdown_probability: float | None = None
    if draw(st.booleans()):
        max_drawdown = draw(st.floats(0.05, 0.95))
        max_drawdown_probability = draw(st.floats(0.001, 0.999))
    edge_uncertainty_coef: float | None = None
    if draw(st.booleans()):
        edge_uncertainty_coef = draw(st.floats(0.0, 100.0))
    return StakePolicy(
        fractional_kelly=fractional_kelly,
        max_stake_fraction=max_stake_fraction,
        max_drawdown=max_drawdown,
        max_drawdown_probability=max_drawdown_probability,
        edge_uncertainty_coef=edge_uncertainty_coef,
    )


@given(probability=_PROB, decimal_odds=_ODDS)
@settings(max_examples=300, deadline=None)
@example(probability=1.0, decimal_odds=2.0)  # full edge -> Kelly == 1.0 (whole bankroll)
@example(probability=0.0, decimal_odds=2.0)  # no chance -> Kelly == 0
@example(probability=0.5, decimal_odds=2.0)  # break-even -> Kelly == 0 exactly
def test_kelly_fraction_in_unit_interval(probability: float, decimal_odds: float) -> None:
    frac = kelly_fraction(probability, decimal_odds)
    assert frac >= 0.0, f"Kelly fraction negative: {frac!r} (p={probability}, d={decimal_odds})"
    assert frac <= 1.0 + 1e-12, (
        f"Kelly fraction {frac!r} exceeds full bankroll (p={probability}, d={decimal_odds})"
    )


@given(
    probability=_PROB,
    decimal_odds=_ODDS,
    policy=_policies(),
    edge_variance=st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
    correlated_legs=st.integers(1, 10),
    avg_correlation=st.floats(0.0, 0.99, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=300, deadline=None)
def test_recommended_stake_never_negative_never_exceeds_cap(
    probability: float,
    decimal_odds: float,
    policy: StakePolicy,
    edge_variance: float,
    correlated_legs: int,
    avg_correlation: float,
) -> None:
    breakdown = recommended_stake(
        probability,
        decimal_odds,
        policy,
        edge_variance=edge_variance,
        correlated_legs=correlated_legs,
        avg_correlation=avg_correlation,
    )
    ctx = (
        f"p={probability}, d={decimal_odds}, cap={policy.max_stake_fraction}, "
        f"frac={policy.fractional_kelly}, var={edge_variance}, legs={correlated_legs}, "
        f"rho={avg_correlation}"
    )
    assert breakdown.raw_kelly >= 0.0, f"raw_kelly negative ({breakdown.raw_kelly!r}); {ctx}"
    assert breakdown.fractional >= 0.0, f"fractional negative ({breakdown.fractional!r}); {ctx}"
    assert breakdown.final >= 0.0, f"final stake negative ({breakdown.final!r}); {ctx}"
    assert breakdown.final <= policy.max_stake_fraction + 1e-12, (
        f"final stake {breakdown.final!r} exceeds per-bet cap {policy.max_stake_fraction!r}; {ctx}"
    )
    # final is min(fractional, cap): an uncapped fractional must reduce to the cap.
    assert math.isclose(
        breakdown.final, min(breakdown.fractional, policy.max_stake_fraction), abs_tol=1e-12
    ), f"final is not min(fractional, cap); {ctx}"


@st.composite
def _no_edge_inputs(draw: st.DrawFn) -> tuple[float, float]:
    """A probability strictly at/below the break-even prob 1/d -> no +EV edge."""
    decimal_odds = draw(_ODDS)
    breakeven = 1.0 / decimal_odds
    probability = draw(st.floats(0.0, breakeven, allow_nan=False, allow_infinity=False))
    return probability, decimal_odds


@given(inputs=_no_edge_inputs(), policy=_policies())
@settings(max_examples=300, deadline=None)
@example(inputs=(0.5, 2.0), policy=StakePolicy())  # exact break-even
@example(inputs=(0.0, 5.0), policy=StakePolicy())  # zero probability
def test_non_positive_edge_gives_zero_stake(
    inputs: tuple[float, float], policy: StakePolicy
) -> None:
    probability, decimal_odds = inputs
    raw = kelly_fraction(probability, decimal_odds)
    assert raw == 0.0, (
        f"non-positive edge produced stake {raw!r} (p={probability}, d={decimal_odds}, "
        f"break-even={1.0 / decimal_odds})"
    )
    breakdown = recommended_stake(probability, decimal_odds, policy)
    assert breakdown.raw_kelly == 0.0
    assert breakdown.fractional == 0.0
    assert breakdown.final == 0.0
    assert breakdown.capped is False


@given(
    fraction=st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
    bankroll=st.integers(min_value=0, max_value=10_000_000),
)
@settings(max_examples=200, deadline=None)
def test_stake_amount_non_negative_and_bounded_by_bankroll(fraction: float, bankroll: int) -> None:
    money = stake_amount(fraction, Decimal(bankroll))
    assert money >= Decimal("0"), f"stake amount negative: {money} (fraction={fraction})"
    # fraction <= 1.0, so the staked money can never exceed the bankroll;
    # half-up cent rounding can add at most one cent.
    assert money <= Decimal(bankroll) + Decimal("0.01"), (
        f"stake amount {money} exceeds bankroll {bankroll} (fraction={fraction})"
    )


def test_stake_amount_rejects_negative_fraction() -> None:
    with pytest.raises(ValueError):
        stake_amount(-0.01, Decimal("1000"))
