"""Vig-stripping invariants and exact values.

Expected values are hand-computed from the published formulas:
multiplicative p_i = q_i / sum(q); additive p_i = q_i - B/n;
power: solve sum(q_i^k) = 1; Shin (1993): insider-trading z solve.
"""

import math

import pytest

from app.probabilities.devig import (
    EXPECTED_FALLBACKS,
    DevigFallbackReason,
    DevigMethod,
    devig,
    devig_fell_back,
    devig_with_diagnostics,
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
    DevigMethod.GOTO,
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


@pytest.mark.parametrize("method", [DevigMethod.POWER, DevigMethod.SHIN, DevigMethod.GOTO])
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


# --- fallback diagnostics as DATA (pure-math boundary, audit 2026-07-09) ---- #
# devig.py must not log (no side effects in app/probabilities/): the warning
# condition is returned as a DevigFallbackReason and the IO layer decides how
# to log it (EXPECTED_FALLBACKS -> debug doctrine, everything else -> warning).


def test_devig_module_has_no_logging_side_effect(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    import app.probabilities.devig as devig_module

    assert not hasattr(devig_module, "logging")
    assert not hasattr(devig_module, "logger")
    with caplog.at_level(logging.DEBUG):
        devig([1.02, 8.0, 81.0], method=DevigMethod.DIFFERENTIAL_MARGIN)  # falls back
        devig([2.6, 3.9, 3.4], method=DevigMethod.SHIN)  # underround, falls back
        devig(ADDITIVE_FALLBACK, method=DevigMethod.ADDITIVE)  # warning-grade fallback
    assert not [r for r in caplog.records if r.name.startswith("app.probabilities")]


def test_differential_margin_extreme_longshot_fallback_is_expected_data() -> None:
    # Longshot odds with a fat margin make Buchdahl's denominator
    # n - margin*odds_i non-positive: the multiplicative fallback IS the
    # design (same doctrine as Shin's underround fallback) — reported as an
    # EXPECTED reason (debug doctrine), not a warning per market per cycle.
    odds = [1.02, 8.0, 81.0]  # margin*81 >> n=3
    probs, reason = devig_with_diagnostics(odds, method=DevigMethod.DIFFERENTIAL_MARGIN)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert reason is DevigFallbackReason.DIFFERENTIAL_MARGIN_NON_POSITIVE
    assert reason in EXPECTED_FALLBACKS


def test_shin_underround_fallback_is_expected_data() -> None:
    # Max-of-books composite odds are routinely underround; Shin's fallback
    # there is documented-expected and must NOT be warning-grade (a backtest
    # produced 154k warning lines before this was demoted).
    probs, reason = devig_with_diagnostics([2.6, 3.9, 3.4], method=DevigMethod.SHIN)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert reason is DevigFallbackReason.SHIN_UNDERROUND
    assert reason in EXPECTED_FALLBACKS


def test_additive_fallback_reason_is_warning_grade() -> None:
    probs, reason = devig_with_diagnostics(ADDITIVE_FALLBACK, method=DevigMethod.ADDITIVE)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert reason is DevigFallbackReason.ADDITIVE_NON_POSITIVE
    assert reason not in EXPECTED_FALLBACKS  # anomaly: IO layer should warn


def test_diagnostics_reason_consistent_with_provenance_flag() -> None:
    for odds in (THREE_WAY, LONGSHOT_BOOK, UNDERROUND_TWO_WAY, ADDITIVE_FALLBACK):
        for method in ALL_METHODS:
            probs, reason = devig_with_diagnostics(odds, method=method)
            legacy_probs, fell = devig_with_provenance(odds, method=method)
            assert (reason is not None) is fell
            assert probs == pytest.approx(legacy_probs, abs=1e-15)


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


# --- goto_conversion (equal-units-of-standard-error shrink) ----------------- #
# Published algorithm (https://github.com/gotoConversion/goto_conversion, MIT):
#   q_i  = 1/odds_i
#   se_i = sqrt((q_i - q_i^2)/q_i) = sqrt(1 - q_i)
#   step = (sum(q) - 1) / sum(se)
#   p_i  = q_i - step*se_i          (sums to 1 exactly, by construction)
# Longshots (small q) carry the LARGER implied standard error, so they are
# shrunk by MORE absolute probability — the favourite-longshot-aware property.
# Golden vectors below are hand-derived from these formulas (arithmetic shown),
# independently of the implementation.


def test_goto_symmetric_two_way_is_half_half() -> None:
    # [1.9, 1.9]: q = [10/19, 10/19], booksum = 20/19 = 1.0526315789...
    # se_i = sqrt(1 - 10/19) = sqrt(9/19) = 0.6882472016...
    # step = (20/19 - 1) / (2*0.6882472016) = 0.0526315789/1.3764944032
    #      = 0.0382359556...
    # p_i = 10/19 - 0.0382359556*0.6882472016 = 0.5263157895 - 0.0263157895 = 0.5
    probs = devig([1.9, 1.9], method=DevigMethod.GOTO)
    assert probs[0] == pytest.approx(0.5, abs=1e-12)
    assert probs[1] == pytest.approx(0.5, abs=1e-12)


def test_goto_golden_vector_asymmetric_two_way() -> None:
    # [1.4, 3.2]: q = [0.7142857143, 0.3125], booksum = 1.0267857143
    # se = [sqrt(0.2857142857), sqrt(0.6875)] = [0.5345224838, 0.8291561976]
    # step = 0.0267857143 / (0.5345224838 + 0.8291561976)
    #      = 0.0267857143 / 1.3636786814 = 0.0196422476
    # p1 = 0.7142857143 - 0.0196422476*0.5345224838 = 0.7142857143 - 0.0104992230
    #    = 0.7037864913
    # p2 = 0.3125      - 0.0196422476*0.8291561976 = 0.3125 - 0.0162864913
    #    = 0.2962135087
    probs = devig([1.4, 3.2], method=DevigMethod.GOTO)
    assert probs[0] == pytest.approx(0.7037864913190693, abs=1e-9)
    assert probs[1] == pytest.approx(0.2962135086809305, abs=1e-9)
    # Favourite keeps MORE than multiplicative gives it (0.6956521739): the
    # margin is taken disproportionately from the longshot.
    mult = devig([1.4, 3.2], method=DevigMethod.MULTIPLICATIVE)
    assert probs[0] > mult[0] + 5e-3


def test_goto_golden_vector_strongly_asymmetric_three_way() -> None:
    # [1.2, 7.0, 15.0]: q = [0.8333333333, 0.1428571429, 0.0666666667],
    # booksum = 1.0428571429
    # se = [sqrt(0.1666666667), sqrt(0.8571428571), sqrt(0.9333333333)]
    #    = [0.4082482905, 0.9258200998, 0.9660917831]
    # step = 0.0428571429 / (0.4082482905 + 0.9258200998 + 0.9660917831)
    #      = 0.0428571429 / 2.3001601734 = 0.0186322428
    # p1 = 0.8333333333 - 0.0186322428*0.4082482905 = 0.8333333333 - 0.0076065813
    #    = 0.8257267521
    # p2 = 0.1428571429 - 0.0186322428*0.9258200998 = 0.1428571429 - 0.0172501049
    #    = 0.1256070380
    # p3 = 0.0666666667 - 0.0186322428*0.9660917831 = 0.0666666667 - 0.0180004567
    #    = 0.0486662100
    probs = devig([1.2, 7.0, 15.0], method=DevigMethod.GOTO)
    assert probs[0] == pytest.approx(0.8257267520575725, abs=1e-9)
    assert probs[1] == pytest.approx(0.1256070379573740, abs=1e-9)
    assert probs[2] == pytest.approx(0.0486662099850535, abs=1e-9)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert probs[0] > probs[1] > probs[2]  # order-preserving
    # Must differ MEASURABLY from multiplicative (which gives the 15.0 longshot
    # 0.0639269406): goto strips over 3x more margin from the tail.
    mult = devig([1.2, 7.0, 15.0], method=DevigMethod.MULTIPLICATIVE)
    assert abs(probs[2] - mult[2]) > 0.01
    assert probs[2] < mult[2]  # tail shrunk, not inflated


def test_goto_fair_book_is_identity() -> None:
    # [2.0, 4.0, 4.0]: booksum exactly 1.0 -> step = 0 -> p = q untouched.
    probs = devig([2.0, 4.0, 4.0], method=DevigMethod.GOTO)
    assert probs == pytest.approx([0.5, 0.25, 0.25], abs=1e-12)
    assert devig_fell_back([2.0, 4.0, 4.0], method=DevigMethod.GOTO) is False


def test_goto_underround_book_inflates_without_fallback() -> None:
    # goto is defined for underround books too (negative step INFLATES, with
    # the longshot inflated most): [2.2, 2.2] -> 0.5/0.5, no fallback.
    probs = devig(UNDERROUND_TWO_WAY, method=DevigMethod.GOTO)
    assert probs[0] == pytest.approx(0.5, abs=1e-12)
    assert devig_fell_back(UNDERROUND_TWO_WAY, method=DevigMethod.GOTO) is False


def test_goto_non_positive_tail_falls_back_to_multiplicative() -> None:
    # [1.05, 10.0, 100.0]: q = [0.9523809524, 0.1, 0.01], booksum = 1.0623809524
    # se = [0.2182178902, 0.9486832981, 0.9949874371], sum = 2.1618886254
    # step = 0.0623809524/2.1618886254 = 0.0288548409
    # p3 = 0.01 - 0.0288548409*0.9949874371 = 0.01 - 0.0287102042
    #    = -0.0187102042 <= 0  -> method does not apply; multiplicative fallback.
    probs, reason = devig_with_diagnostics([1.05, 10.0, 100.0], method=DevigMethod.GOTO)
    assert reason is DevigFallbackReason.GOTO_NON_POSITIVE
    assert probs == pytest.approx(
        devig([1.05, 10.0, 100.0], method=DevigMethod.MULTIPLICATIVE), abs=1e-12
    )
    # Fat-margin longshot books are structurally outside the method's domain
    # (same doctrine as differential-margin's non-positive denominator): the
    # reason is documented-EXPECTED, debug-grade — not a warning per market.
    assert reason in EXPECTED_FALLBACKS


def test_shin_matches_mberk_shin_golden_vectors() -> None:
    """Cross-library fixture from mberk/shin (MIT, tests/test_shin.py): odds
    [2.6, 2.4, 4.3] must devig to these probabilities under Shin. Locks our
    implementation against the reference implementation's golden output
    (research scan 2026-07-04 — the only adoptable artifact from the sweep)."""
    probs = devig([2.6, 2.4, 4.3], DevigMethod.SHIN)
    expected = [0.3729941, 0.4047794, 0.2222265]
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)
    for got, want in zip(probs, expected, strict=True):
        assert got == pytest.approx(want, abs=2e-4)
