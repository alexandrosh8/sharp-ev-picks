"""Kelly staking: exact values, caps, decomposition transparency.

kelly_fraction = ((d - 1) * p - (1 - p)) / (d - 1), clipped to >= 0.
"""

from decimal import Decimal

import pytest

from app.risk.staking import StakePolicy, kelly_fraction, recommended_stake, stake_amount


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
