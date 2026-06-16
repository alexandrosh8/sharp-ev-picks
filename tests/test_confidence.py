"""Unit tests for app.edge.confidence — the 1..5 star edge-quality rating.

Boundaries are asserted at the exact band edges (>= is inclusive), the
missing-ML-score path must never penalise, anchor tiers must rank
pinnacle > sharp > consensus, and the level must always clamp into [1, 5].
"""

from app.edge.confidence import (
    ML_OPERATING_POINT,
    ML_WEAK_FLOOR,
    ConfidenceRating,
    confidence_rating,
)

FLOOR = 0.03  # Settings.value_min_edge default — the premium edge floor


def rate(
    edge: float = FLOOR,
    value_filter_score: float | None = None,
    anchor_type: str | None = "consensus",
) -> ConfidenceRating:
    """Typed convenience wrapper: fixed threshold (FLOOR), no book count."""
    return confidence_rating(edge, FLOOR, value_filter_score, anchor_type, None)


def test_bare_minimum_premium_pick_is_one_star() -> None:
    # Spec's score=0 starting point: edge below floor, consensus/no anchor,
    # no ML -> score 0 -> 1 star (the +1 baseline floor).
    r = confidence_rating(FLOOR - 0.001, FLOOR, None, "consensus", None)
    assert isinstance(r, ConfidenceRating)
    assert r.level == 1
    assert r.score == 0
    assert r.label == "minimal"


def test_edge_at_floor_adds_one() -> None:
    # >= floor is inclusive: edge exactly at the floor earns the +1 band.
    r = confidence_rating(FLOOR, FLOOR, None, "consensus", None)
    assert r.score == 1
    assert r.level == 2


def test_edge_strong_band_at_two_times_floor() -> None:
    # >= 2x floor is inclusive (+2); just under it is the +1 band.
    assert confidence_rating(2 * FLOOR, FLOOR, None, "consensus", None).score == 2
    assert confidence_rating(2 * FLOOR - 1e-9, FLOOR, None, "consensus", None).score == 1


def test_anchor_tiers_rank_pinnacle_over_sharp_over_consensus() -> None:
    pin = rate(anchor_type="pinnacle").score
    sharp = rate(anchor_type="sharp").score
    cons = rate(anchor_type="consensus").score
    none = rate(anchor_type=None).score
    assert pin == sharp + 1 == cons + 2
    # null anchor contributes the same as consensus (both +0)
    assert none == cons


def test_missing_ml_score_never_penalises() -> None:
    # None value_filter_score == out of model scope: contributes exactly 0,
    # never the -1 weak penalty.
    with_none = confidence_rating(FLOOR, FLOOR, None, "sharp", None)
    weak = confidence_rating(FLOOR, FLOOR, ML_WEAK_FLOOR - 0.01, "sharp", None)
    assert with_none.score == 2  # +1 edge, +1 sharp, ML absent => 0
    assert weak.score == 1  # same, minus 1 for the weak ML band


def test_ml_score_bands_at_operating_point() -> None:
    # >= q* is inclusive (+1)
    assert rate(value_filter_score=ML_OPERATING_POINT).score == 2
    # in [weak floor, q*) contributes 0
    assert rate(value_filter_score=ML_OPERATING_POINT - 1e-9).score == 1
    assert rate(value_filter_score=ML_WEAK_FLOOR).score == 1
    # below the weak floor is the -1 penalty
    assert rate(value_filter_score=ML_WEAK_FLOOR - 1e-9).score == 0


def test_maximum_pick_clamps_to_five_stars() -> None:
    # strong edge (+2) + pinnacle (+2) + ML>=q* (+1) => score 5 => 5 stars.
    r = confidence_rating(2 * FLOOR, FLOOR, 0.80, "pinnacle", None)
    assert r.score == 5
    assert r.level == 5
    assert r.label == "very high"


def test_level_never_exceeds_five_even_with_extra_headroom() -> None:
    # An edge well past the strong band can't push score past the +2 cap, so
    # the level stays clamped at 5.
    r = confidence_rating(0.25, FLOOR, 0.99, "pinnacle", None)
    assert r.score == 5
    assert r.level == 5


def test_level_never_below_one() -> None:
    # Everything wrong: below floor (+0), no anchor, weak ML (-1) => score -1
    # => clamped to 1.
    r = confidence_rating(0.0, FLOOR, 0.0, None, None)
    assert r.score == -1
    assert r.level == 1


def test_book_count_does_not_change_score() -> None:
    # book_count is informational/forward-compatible only; it must not move
    # the rating (book-count quality is implied by anchor_type today).
    a = confidence_rating(FLOOR, FLOOR, None, "consensus", None)
    b = confidence_rating(FLOOR, FLOOR, None, "consensus", 7)
    assert a.score == b.score == 1
    assert a.level == b.level


def test_reasons_list_the_bands_that_fired() -> None:
    r = confidence_rating(2 * FLOOR, FLOOR, 0.80, "pinnacle", None)
    joined = " | ".join(r.reasons)
    assert "2x floor" in joined
    assert "pinnacle anchor" in joined
    assert "q*" in joined


def test_unknown_anchor_string_contributes_zero() -> None:
    # An unexpected anchor label must default to +0, never raise.
    r = confidence_rating(FLOOR, FLOOR, None, "exchange", None)
    assert r.score == 1  # only the +1 edge band
