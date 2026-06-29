"""Property-based invariants for every devig method (P1-hole H4).

Example-based coverage lives in ``test_devig.py``; this file fuzzes the same
invariants over hypothesis-generated VALID decimal-odds vectors (2-way and
3-way, favourites/longshots, fair books through fat-margin overrounds) so a
method that breaks OUTSIDE the literal example region is caught.

Invariants asserted for EVERY ``DevigMethod`` (iterated dynamically so a new
method is covered automatically):
  * probabilities sum to 1.0 within 1e-9,
  * every probability lies strictly in (0, 1),
  * no NaN / inf (the degenerate-booksum fallback must hold),
  * order-preserving: shorter odds -> higher implied probability.
"""

import math

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.probabilities.devig import DevigMethod, devig

ALL_METHODS = list(DevigMethod)

# Realistic decimal-odds: heavy favourite (1.01) through 100.0 longshot. The
# 1.01 floor keeps a favourite's implied prob clear of the 1.0 boundary (there
# is always >=1 other positive 1/o term in the denominator), so strict (0, 1)
# holds without depending on solver luck.
_ODDS = st.floats(min_value=1.01, max_value=100.0, allow_nan=False, allow_infinity=False)


def _markets() -> st.SearchStrategy[list[float]]:
    """2-way and 3-way books; independent legs span underround -> fat overround."""
    return st.one_of(
        st.lists(_ODDS, min_size=2, max_size=2),
        st.lists(_ODDS, min_size=3, max_size=3),
    )


# Edge cases woven into every parametrized method run.
_MINIMAL_FAVOURITE = [1.01, 100.0]  # extreme favourite + extreme longshot
_FAIR_TWO_WAY = [2.0, 2.0]  # overround == 0 (booksum exactly 1.0)
_FAIR_THREE_WAY = [3.0, 3.0, 3.0]  # 3-way fair book (booksum exactly 1.0)
_FAT_TWO_WAY = [1.2, 1.2]  # booksum ~1.667, heavy vig
_LONGSHOT_THREE_WAY = [1.5, 4.0, 6.0]  # asymmetric 3-way


@pytest.mark.parametrize("method", ALL_METHODS)
@settings(max_examples=100, deadline=None)
@example(odds=_MINIMAL_FAVOURITE)
@example(odds=_FAIR_TWO_WAY)
@example(odds=_FAIR_THREE_WAY)
@example(odds=_FAT_TWO_WAY)
@example(odds=_LONGSHOT_THREE_WAY)
@given(odds=_markets())
def test_devig_is_a_valid_probability_distribution(method: DevigMethod, odds: list[float]) -> None:
    probs = devig(odds, method=method)
    total = math.fsum(probs)
    assert math.isclose(total, 1.0, abs_tol=1e-9), f"{method}: sum={total!r} for odds={odds}"
    for p in probs:
        assert math.isfinite(p), f"{method}: non-finite probability {p!r} for odds={odds}"
        assert 0.0 < p < 1.0, f"{method}: probability {p!r} outside (0, 1) for odds={odds}"


@pytest.mark.parametrize("method", ALL_METHODS)
@settings(max_examples=100, deadline=None)
@example(odds=_MINIMAL_FAVOURITE)
@example(odds=_FAIR_TWO_WAY)
@example(odds=_FAIR_THREE_WAY)
@example(odds=_FAT_TWO_WAY)
@example(odds=_LONGSHOT_THREE_WAY)
@given(odds=_markets())
def test_devig_preserves_odds_order(method: DevigMethod, odds: list[float]) -> None:
    # Every method is a monotone-decreasing map from odds to probability.
    # Sort by ascending odds: probabilities must come out non-increasing.
    # Non-strict (with tolerance) tolerates ties and solver float dust; strict
    # ordering on distinct odds is covered example-based in test_devig.py.
    probs = devig(odds, method=method)
    paired = sorted(zip(odds, probs, strict=True), key=lambda pair: pair[0])
    for (odd_a, prob_a), (odd_b, prob_b) in zip(paired, paired[1:], strict=False):
        assert prob_a >= prob_b - 1e-9, (
            f"{method}: order violated — odds {odd_a}->{prob_a!r} then "
            f"{odd_b}->{prob_b!r} (shorter odds must not get less probability)"
        )
