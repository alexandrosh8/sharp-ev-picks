"""Vig-stripping invariants and exact values.

Expected values are hand-computed from the published formulas:
multiplicative p_i = q_i / sum(q); additive p_i = q_i - B/n;
power: solve sum(q_i^k) = 1; Shin (1993): insider-trading z solve.
"""

import math

import pytest

from app.probabilities.devig import (
    DevigMethod,
    devig,
    devig_fell_back,
    devig_with_provenance,
)

THREE_WAY = [2.5, 3.2, 2.9]  # q = [0.4, 0.3125, 0.34483], overround ~5.73%
LONGSHOT_BOOK = [1.5, 4.0, 6.0]  # q = [0.66667, 0.25, 0.16667], overround ~8.33%

ALL_METHODS = [
    DevigMethod.MULTIPLICATIVE,
    DevigMethod.ADDITIVE,
    DevigMethod.POWER,
    DevigMethod.SHIN,
    DevigMethod.PROBIT,
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


def test_probit_symmetric_two_way_is_half_half() -> None:
    # build #2: Probit on a symmetric market (totals / Asian handicap) -> even.
    probs = devig([1.9, 1.9], method=DevigMethod.PROBIT)
    assert probs[0] == pytest.approx(0.5, abs=1e-9)
    assert probs[1] == pytest.approx(0.5, abs=1e-9)


def test_probit_devig_is_valid_and_order_preserving() -> None:
    # build #2: Probit yields a valid distribution preserving the odds order
    # (shorter odds -> higher fair probability).
    probs = devig(THREE_WAY, method=DevigMethod.PROBIT)  # [2.5, 3.2, 2.9]
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert probs[0] > probs[2] > probs[1]  # 2.5 > 2.9 > 3.2 in implied prob


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


# --- devig fallback provenance (P2-2) --------------------------------------- #

UNDERROUND_TWO_WAY = [2.2, 2.2]  # q = 0.4545 + 0.4545 = 0.909 < 1 (underround)
ADDITIVE_FALLBACK = [1.05, 10.0, 100.0]  # additive drives a prob negative


@pytest.mark.parametrize("method", ALL_METHODS)
@pytest.mark.parametrize("odds", [THREE_WAY, LONGSHOT_BOOK, [1.9, 1.9], [2.1, 1.75]])
def test_provenance_probs_are_identical_to_devig(method: DevigMethod, odds: list[float]) -> None:
    # The provenance variant must never change the probabilities — only add a flag.
    probs, _fell = devig_with_provenance(odds, method=method)
    assert probs == pytest.approx(devig(odds, method=method), abs=1e-15)


@pytest.mark.parametrize("method", ALL_METHODS)
def test_clean_overround_book_does_not_fall_back(method: DevigMethod) -> None:
    # THREE_WAY is a normal overround book: every method applies, none falls back.
    assert devig_fell_back(THREE_WAY, method=method) is False


def test_multiplicative_never_falls_back_even_on_degenerate_input() -> None:
    assert devig_fell_back(ADDITIVE_FALLBACK, method=DevigMethod.MULTIPLICATIVE) is False
    assert devig_fell_back(UNDERROUND_TWO_WAY, method=DevigMethod.MULTIPLICATIVE) is False


def test_additive_reports_fallback_on_negative_prob() -> None:
    probs, fell = devig_with_provenance(ADDITIVE_FALLBACK, method=DevigMethod.ADDITIVE)
    assert fell is True
    assert probs == pytest.approx(
        devig(ADDITIVE_FALLBACK, method=DevigMethod.MULTIPLICATIVE), abs=1e-12
    )


def test_shin_reports_fallback_on_underround_but_power_does_not() -> None:
    # Underround two-way: Shin is only defined on overround books -> falls back;
    # the power solver still brackets a root -> applies, no fallback.
    assert devig_fell_back(UNDERROUND_TWO_WAY, method=DevigMethod.SHIN) is True
    assert devig_fell_back(UNDERROUND_TWO_WAY, method=DevigMethod.POWER) is False


def test_shin_overround_two_way_applies_no_fallback() -> None:
    # [1.9, 1.9] is overround (q sum 1.0526): the exact 2-outcome Shin applies.
    assert devig_fell_back([1.9, 1.9], method=DevigMethod.SHIN) is False


def test_fell_back_predicate_matches_provenance_flag() -> None:
    for odds in (THREE_WAY, LONGSHOT_BOOK, UNDERROUND_TWO_WAY, ADDITIVE_FALLBACK):
        for method in ALL_METHODS:
            _probs, fell = devig_with_provenance(odds, method=method)
            assert devig_fell_back(odds, method=method) is fell


@pytest.mark.parametrize("bad", [[1.0, 2.0], [0.5, 3.0], [-2.0, 2.0], [2.0, 0.0]])
def test_odds_at_or_below_one_raise(bad: list[float]) -> None:
    with pytest.raises(ValueError):
        devig(bad)


@pytest.mark.parametrize("bad", [[], [2.0]])
def test_fewer_than_two_outcomes_raise(bad: list[float]) -> None:
    with pytest.raises(ValueError):
        devig(bad)


# --- Test oracle: mberk/shin reference implementation (MIT, inspected
# 2026-06-10). Exact expected values from its cross-validated Rust+Python
# test suite — our clean-room Shin must agree.


def test_shin_oracle_three_way() -> None:
    probs = devig([2.6, 2.4, 4.3], method=DevigMethod.SHIN)
    expected = [0.37299406033208965, 0.4047794109200184, 0.2222265287474275]
    assert probs == pytest.approx(expected, abs=1e-6)


def test_shin_oracle_two_way_matches_additive_equivalence() -> None:
    # For two outcomes, Shin reduces to p_i = 1/o_i - (booksum - 1)/2
    # (documented equivalence in the mberk/shin test suite).
    probs = devig([1.5, 2.74], method=DevigMethod.SHIN)
    expected = [0.6508515815085157, 0.3491484184914841]
    assert probs == pytest.approx(expected, abs=1e-6)


def test_differential_margin_extreme_longshot_falls_back_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Longshot odds with a fat margin make Buchdahl's denominator
    # n - margin*odds_i non-positive: the multiplicative fallback IS the
    # design (same doctrine as Shin's underround fallback above) — debug,
    # not a warning per market per cycle.
    import logging

    odds = [1.02, 8.0, 81.0]  # margin*81 >> n=3
    with caplog.at_level(logging.DEBUG, logger="app.probabilities.devig"):
        probs = devig(odds, method=DevigMethod.DIFFERENTIAL_MARGIN)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("denominator" in r.message for r in caplog.records)  # visible at debug


def test_shin_underround_falls_back_quietly(caplog: pytest.LogCaptureFixture) -> None:
    # Max-of-books composite odds are routinely underround; Shin's fallback
    # there is documented-expected and must NOT warn (a backtest produced
    # 154k warning lines before this was demoted to debug).
    import logging

    with caplog.at_level(logging.DEBUG, logger="app.probabilities.devig"):
        probs = devig([2.6, 3.9, 3.4], method=DevigMethod.SHIN)  # booksum ~0.94
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("underround" in r.message for r in caplog.records)  # still visible at debug


def test_odds_ratio_and_logarithmic_are_equivalent_methods() -> None:
    # ODDS_RATIO is a constant logit shift == LOGARITHMIC and is now implemented
    # by routing through the logarithmic solver (audit #2), so the two can never
    # diverge — including on the fat-margin many-outcome book where the old
    # odds-ratio bracket [1e-9, 100] would raise and silently fall back.
    for odds in (
        [2.6, 2.4, 4.3],
        [1.5, 2.74],
        [2.05, 3.6, 3.55, 8.0],
        [1.2, 5.0, 8.0, 15.0, 30.0],  # extreme overround (audit #2 divergence trigger)
    ):
        a = devig(odds, method=DevigMethod.ODDS_RATIO)
        b = devig(odds, method=DevigMethod.LOGARITHMIC)
        assert a == pytest.approx(b, abs=1e-9)
