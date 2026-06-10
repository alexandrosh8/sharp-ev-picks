"""Vig-stripping invariants and exact values.

Expected values are hand-computed from the published formulas:
multiplicative p_i = q_i / sum(q); additive p_i = q_i - B/n;
power: solve sum(q_i^k) = 1; Shin (1993): insider-trading z solve.
"""

import math

import pytest

from app.probabilities.devig import DevigMethod, devig

THREE_WAY = [2.5, 3.2, 2.9]  # q = [0.4, 0.3125, 0.34483], overround ~5.73%
LONGSHOT_BOOK = [1.5, 4.0, 6.0]  # q = [0.66667, 0.25, 0.16667], overround ~8.33%

ALL_METHODS = [
    DevigMethod.MULTIPLICATIVE,
    DevigMethod.ADDITIVE,
    DevigMethod.POWER,
    DevigMethod.SHIN,
]


@pytest.mark.parametrize("method", ALL_METHODS)
@pytest.mark.parametrize("odds", [THREE_WAY, LONGSHOT_BOOK, [1.9, 1.9], [2.1, 1.75]])
def test_devig_sums_to_one(method: DevigMethod, odds: list[float]) -> None:
    probs = devig(odds, method=method)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9), f"{method}: sum={sum(probs)}"


@pytest.mark.parametrize("method", ALL_METHODS)
def test_devig_preserves_order(method: DevigMethod) -> None:
    # Shorter odds => larger probability, strictly preserved.
    probs = devig(LONGSHOT_BOOK, method=method)
    assert probs[0] > probs[1] > probs[2]


def test_multiplicative_even_two_way_is_half_half() -> None:
    probs = devig([2.0, 2.0], method=DevigMethod.MULTIPLICATIVE)
    assert probs[0] == pytest.approx(0.5, abs=1e-12)
    assert probs[1] == pytest.approx(0.5, abs=1e-12)


def test_multiplicative_exact_three_way() -> None:
    # q = [0.4, 0.3125, 0.344827586]; sum = 1.057327586
    probs = devig(THREE_WAY, method=DevigMethod.MULTIPLICATIVE)
    assert probs[0] == pytest.approx(0.4 / 1.0573275862068966, rel=1e-12)


@pytest.mark.parametrize("method", [DevigMethod.POWER, DevigMethod.SHIN])
def test_longshot_methods_favour_the_favourite(method: DevigMethod) -> None:
    # Power and Shin both correct longshot bias: the favourite keeps MORE
    # probability than multiplicative normalization gives it.
    mult = devig(LONGSHOT_BOOK, method=DevigMethod.MULTIPLICATIVE)
    other = devig(LONGSHOT_BOOK, method=method)
    assert other[0] >= mult[0] - 1e-12


@pytest.mark.parametrize("method", [DevigMethod.SHIN, DevigMethod.POWER])
def test_symmetric_two_way_is_half_half(method: DevigMethod) -> None:
    probs = devig([1.9, 1.9], method=method)
    assert probs[0] == pytest.approx(0.5, abs=1e-9)


def test_additive_negative_prob_falls_back_to_multiplicative() -> None:
    # q = [0.952381, 0.1, 0.01], B = 0.062381, B/3 = 0.020794 > 0.01
    # => additive would drive the longest shot negative; implementation must
    # fall back to multiplicative rather than emit a negative probability.
    odds = [1.05, 10.0, 100.0]
    probs = devig(odds, method=DevigMethod.ADDITIVE)
    assert all(p > 0 for p in probs)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    mult = devig(odds, method=DevigMethod.MULTIPLICATIVE)
    assert probs == pytest.approx(mult, abs=1e-12)


@pytest.mark.parametrize("bad", [[1.0, 2.0], [0.5, 3.0], [-2.0, 2.0], [2.0, 0.0]])
def test_odds_at_or_below_one_raise(bad: list[float]) -> None:
    with pytest.raises(ValueError):
        devig(bad)


@pytest.mark.parametrize("bad", [[], [2.0]])
def test_fewer_than_two_outcomes_raise(bad: list[float]) -> None:
    with pytest.raises(ValueError):
        devig(bad)
