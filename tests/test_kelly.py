"""Kelly staking: exact values, caps, decomposition transparency.

kelly_fraction = ((d - 1) * p - (1 - p)) / (d - 1), clipped to >= 0.
"""

from decimal import Decimal

import pytest

from app.risk.staking import (
    StakePolicy,
    drawdown_constrained_multiplier,
    effective_kelly_multiplier,
    kelly_fraction,
    recommended_stake,
    stake_amount,
)


def test_full_kelly_exact_value() -> None:
    # p=0.55 @ d=2.0: ((1)(0.55) - 0.45) / 1 = 0.10
    assert kelly_fraction(0.55, 2.0) == pytest.approx(0.10, abs=1e-12)


def test_kelly_sign_guards_against_p_q_swap() -> None:
    # Regression guard: sedemmler/WagerBrain (inspected 2026-06-10) ships
    # (b*q - p)/b — a p/q swap returning -0.10 here. A positive-edge bet
    # must NEVER produce a non-positive Kelly fraction.
    assert kelly_fraction(0.55, 2.0) > 0.0


def test_no_edge_gives_zero() -> None:
    assert kelly_fraction(0.50, 2.0) == 0.0


def test_negative_edge_clips_to_zero_never_negative() -> None:
    assert kelly_fraction(0.40, 2.0) == 0.0


@pytest.mark.parametrize("p", [-0.1, 1.1])
def test_probability_out_of_range_raises(p: float) -> None:
    with pytest.raises(ValueError):
        kelly_fraction(p, 2.0)


@pytest.mark.parametrize("d", [1.0, 0.9, 0.0, -2.0])
def test_decimal_odds_at_or_below_one_raise(d: float) -> None:
    with pytest.raises(ValueError):
        kelly_fraction(0.55, d)


def test_quarter_kelly_below_cap() -> None:
    # raw 0.10 -> quarter 0.025 -> cap 0.03 leaves it at 0.025
    policy = StakePolicy(fractional_kelly=0.25, max_stake_fraction=0.03)
    breakdown = recommended_stake(0.55, 2.0, policy)
    assert breakdown.raw_kelly == pytest.approx(0.10, abs=1e-12)
    assert breakdown.fractional == pytest.approx(0.025, abs=1e-12)
    assert breakdown.final == pytest.approx(0.025, abs=1e-12)


def test_quarter_kelly_capped_at_default_two_percent() -> None:
    # raw 0.10 -> quarter 0.025 -> default cap 0.02 binds
    policy = StakePolicy()  # fractional_kelly=0.25, max_stake_fraction=0.02
    breakdown = recommended_stake(0.55, 2.0, policy)
    assert breakdown.fractional == pytest.approx(0.025, abs=1e-12)
    assert breakdown.capped is True
    assert breakdown.final == pytest.approx(0.02, abs=1e-12)


def test_no_edge_recommends_zero_stake() -> None:
    breakdown = recommended_stake(0.45, 2.0, StakePolicy())
    assert breakdown.raw_kelly == 0.0
    assert breakdown.final == 0.0
    assert breakdown.capped is False


def test_stake_amount_quantizes_to_cents() -> None:
    assert stake_amount(0.02, Decimal("1000")) == Decimal("20.00")
    assert stake_amount(0.0333, Decimal("1000")) == Decimal("33.30")
    assert stake_amount(0.0, Decimal("1000")) == Decimal("0.00")


# --- drawdown-constrained fractional Kelly (OPTIONAL knob, default OFF) ------
# Continuous-Kelly drawdown law: Pr(dip below 1-d) = (1-d)^(2/lambda - 1).


def test_default_policy_keeps_drawdown_knobs_off() -> None:
    policy = StakePolicy()
    assert policy.max_drawdown is None
    assert policy.max_drawdown_probability is None
    # OFF means the multiplier is exactly the configured fraction.
    assert effective_kelly_multiplier(policy) == 0.25


def test_drawdown_multiplier_reproduces_published_anchors() -> None:
    # Full Kelly has ~50% chance of a 50% drawdown (MacLean-Thorp-Ziemba):
    # allowing exactly that risk returns the full-Kelly multiplier.
    assert drawdown_constrained_multiplier(0.5, 0.5) == pytest.approx(1.0, abs=1e-12)
    # Quarter Kelly: Pr(50% drawdown) = 0.5 ** (2/0.25 - 1) = 0.5**7.
    assert drawdown_constrained_multiplier(0.5, 0.5**7) == pytest.approx(0.25, abs=1e-12)


def test_drawdown_multiplier_never_exceeds_full_kelly() -> None:
    # A loose constraint must cap at 1.0, never recommend above full Kelly.
    assert drawdown_constrained_multiplier(0.5, 0.9) == 1.0


def test_binding_drawdown_constraint_tightens_the_stake() -> None:
    # Pr(50% drawdown) <= 0.1%: lambda* = 2/(1 + ln(.001)/ln(.5)) ~= 0.1824
    policy = StakePolicy(max_drawdown=0.5, max_drawdown_probability=0.001)
    lam = effective_kelly_multiplier(policy)
    assert lam == pytest.approx(0.18238, abs=1e-4)
    breakdown = recommended_stake(0.55, 2.0, policy)
    assert breakdown.raw_kelly == pytest.approx(0.10, abs=1e-12)
    assert breakdown.fractional == pytest.approx(0.10 * lam, abs=1e-12)
    assert breakdown.fractional < 0.025  # tighter than plain quarter Kelly


def test_loose_drawdown_constraint_is_numerically_a_noop() -> None:
    # Pr(50% dd) <= 10% allows lambda ~0.463 > 0.25 -> the configured quarter
    # fraction binds and the breakdown is IDENTICAL to the default policy.
    constrained = StakePolicy(max_drawdown=0.5, max_drawdown_probability=0.1)
    assert recommended_stake(0.55, 2.0, constrained) == recommended_stake(0.55, 2.0, StakePolicy())


def test_per_bet_cap_still_binds_under_drawdown_constraint() -> None:
    # Big edge: raw Kelly 0.40 -> 0.40 * lambda* still exceeds the 2% cap.
    policy = StakePolicy(max_drawdown=0.5, max_drawdown_probability=0.01)
    breakdown = recommended_stake(0.70, 2.0, policy)
    assert breakdown.capped is True
    assert breakdown.final == pytest.approx(0.02, abs=1e-12)


@pytest.mark.parametrize("d", [0.0, 1.0, -0.1, 1.5])
def test_out_of_range_drawdown_raises(d: float) -> None:
    with pytest.raises(ValueError):
        drawdown_constrained_multiplier(d, 0.1)


@pytest.mark.parametrize("beta", [0.0, 1.0, -0.5, 2.0])
def test_out_of_range_drawdown_probability_raises(beta: float) -> None:
    with pytest.raises(ValueError):
        drawdown_constrained_multiplier(0.5, beta)


def test_uncertainty_multiplier_no_shrink_at_zero_variance() -> None:
    # build #4: zero edge variance -> no shrink (== base).
    from app.risk.staking import uncertainty_multiplier

    assert uncertainty_multiplier(0.25, 0.0, coef=5.0) == pytest.approx(0.25)


def test_uncertainty_multiplier_monotone_decreasing_and_bounded() -> None:
    from app.risk.staking import uncertainty_multiplier

    base = 0.25
    m_lo = uncertainty_multiplier(base, 0.001, coef=10.0)
    m_hi = uncertainty_multiplier(base, 0.010, coef=10.0)
    assert base >= m_lo > m_hi > 0.0  # shrinks as variance grows, never above base


def test_uncertainty_multiplier_rejects_bad_inputs() -> None:
    from app.risk.staking import uncertainty_multiplier

    with pytest.raises(ValueError):
        uncertainty_multiplier(0.25, -0.1, coef=1.0)
    with pytest.raises(ValueError):
        uncertainty_multiplier(0.25, 0.1, coef=-1.0)


def test_recommended_stake_unchanged_when_uncertainty_off() -> None:
    # default policy (coef None) -> edge_variance ignored, bit-for-bit unchanged.
    policy = StakePolicy()
    assert recommended_stake(0.60, 2.10, policy, edge_variance=0.05) == recommended_stake(
        0.60, 2.10, policy
    )


def test_recommended_stake_shrinks_with_edge_uncertainty() -> None:
    # build #4: coef set + positive variance -> smaller stake; raw edge unchanged.
    policy = StakePolicy(edge_uncertainty_coef=20.0, max_stake_fraction=1.0)
    sharp = recommended_stake(0.60, 2.10, policy, edge_variance=0.0)
    noisy = recommended_stake(0.60, 2.10, policy, edge_variance=0.05)
    assert noisy.fractional < sharp.fractional
    assert noisy.final <= sharp.final
    assert noisy.raw_kelly == pytest.approx(sharp.raw_kelly)  # only sizing shrinks


def test_correlation_haircut_no_cut_when_independent_or_single() -> None:
    # build #5: single leg OR zero correlation -> no haircut (== 1.0).
    from app.risk.staking import correlation_haircut

    assert correlation_haircut(1, 0.5) == 1.0
    assert correlation_haircut(5, 0.0) == 1.0


def test_correlation_haircut_monotone_and_matches_closed_form() -> None:
    import math

    from app.risk.staking import correlation_haircut

    # 5 legs at rho=0.4 -> 1/sqrt(1 + 4*0.4) = 1/sqrt(2.6)
    assert correlation_haircut(5, 0.4) == pytest.approx(1.0 / math.sqrt(2.6))
    # shrinks as either correlation or leg-count grows; always in (0, 1]
    assert 1.0 > correlation_haircut(3, 0.4) > correlation_haircut(5, 0.4) > 0.0
    assert correlation_haircut(5, 0.2) > correlation_haircut(5, 0.6)


def test_correlation_haircut_rejects_bad_inputs() -> None:
    from app.risk.staking import correlation_haircut

    with pytest.raises(ValueError):
        correlation_haircut(0, 0.3)
    with pytest.raises(ValueError):
        correlation_haircut(3, -0.1)
    with pytest.raises(ValueError):
        correlation_haircut(3, 1.0)  # rho must be < 1


def test_recommended_stake_unchanged_when_correlation_off() -> None:
    # default (1 leg, 0 corr) -> bit-for-bit unchanged.
    policy = StakePolicy()
    assert recommended_stake(0.60, 2.10, policy, correlated_legs=1, avg_correlation=0.5) == (
        recommended_stake(0.60, 2.10, policy)
    )


def test_recommended_stake_shrinks_for_correlated_slate() -> None:
    # build #5: >1 correlated leg + positive correlation -> smaller stake; raw edge unchanged.
    policy = StakePolicy(max_stake_fraction=1.0)
    solo = recommended_stake(0.60, 2.10, policy)
    slate = recommended_stake(0.60, 2.10, policy, correlated_legs=5, avg_correlation=0.4)
    assert slate.fractional < solo.fractional
    assert slate.raw_kelly == pytest.approx(solo.raw_kelly)
