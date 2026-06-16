"""Confidence rating for a pick — a 1..5 star headline of edge QUALITY.

Pure module (numpy/stdlib-free here: plain arithmetic only — no env, DB, HTTP,
or logging side effects). It is deliberately a coarse function of the SAME
thresholds the value pipeline already gates on, so it cannot introduce a new
fitted constant and cannot overfit:

  * the premium edge floor ``threshold`` (Settings.value_min_edge, default
    0.03) and a 2x-floor "strong" band,
  * the fair-value anchor hierarchy (pinnacle > sharp > consensus), and
  * the held-out value-filter operating point q* = 0.725 (the manifest's
    frozen P(beats close) cut from the ML evaluation).

DOCTRINE — what the rating IS and IS NOT. The star count rates model confidence
in the EDGE (sharp-vs-soft line value), NOT a probability the bet wins, and
NEVER a profit guarantee. The only currency of real edge is held-out CLV. The
caller surfaces the framing copy; this module just scores.
"""

from dataclasses import dataclass
from typing import Literal

# Held-out value-filter operating point: the manifest's frozen q* from the ML
# evaluation (docs/research/ml-value-filter.md). Picks at/above it cleared the
# selection cut; below 0.60 they were the model's explicit low-quality region.
ML_OPERATING_POINT = 0.725
ML_WEAK_FLOOR = 0.60

AnchorType = Literal["pinnacle", "sharp", "consensus"]

# Anchor contribution: Pinnacle is the backtested primary (+2), a named sharp
# book is a tier below (+1), the >=3-book consensus median fallback and an
# absent anchor add nothing (0) — they are exactly the strata whose live CLV
# the pipeline is still accumulating, so they earn no confidence bonus.
_ANCHOR_POINTS: dict[str, int] = {"pinnacle": 2, "sharp": 1, "consensus": 0}


@dataclass(frozen=True)
class ConfidenceRating:
    """A 1..5 star confidence level with a short label and the bands that fired.

    ``reasons`` lists the human-readable bands that contributed (for the
    dashboard "why this rating" tooltip); ``score`` is the pre-clamp sum.
    """

    level: int
    label: str
    score: int
    reasons: tuple[str, ...]


_LABELS: dict[int, str] = {
    1: "minimal",
    2: "low",
    3: "moderate",
    4: "high",
    5: "very high",
}


def confidence_rating(
    edge: float,
    threshold: float,
    value_filter_score: float | None,
    anchor_type: str | None,
    book_count: int | None,
) -> ConfidenceRating:
    """Score a pick's edge-quality confidence as 1..5 stars.

    Args:
        edge: the live edge if available else the alert-time edge (the caller
            passes ``current_edge`` when present, otherwise ``edge``).
        threshold: the premium edge floor (Settings.value_min_edge, e.g. 0.03).
            Bands are multiples of THIS value, never a new constant: >= 2x
            floor is a strong edge (+2), >= floor is a baseline edge (+1),
            below the floor adds nothing.
        value_filter_score: calibrated P(beats close) from the value-filter
            meta-model, or None when the pick was outside the model's trained
            scope. None contributes 0 — out-of-scope picks are NEVER penalised.
        anchor_type: 'pinnacle' | 'sharp' | 'consensus' | None.
        book_count: number of books behind the anchor when known. There is no
            per-pick book-count field on the live row today, so this is
            informational/forward-compatible and does NOT change the score;
            book-count quality is already implied by ``anchor_type``
            (consensus = >=3-book median fallback).

    Returns:
        ConfidenceRating with level in [1, 5].
    """
    score = 0
    reasons: list[str] = []

    # EDGE band — coarse multiples of the premium floor.
    strong_edge = 2.0 * threshold
    if edge >= strong_edge:
        score += 2
        reasons.append(f"edge >= {strong_edge * 100:.1f}% (2x floor)")
    elif edge >= threshold:
        score += 1
        reasons.append(f"edge >= {threshold * 100:.1f}% (floor)")
    else:
        reasons.append(f"edge < {threshold * 100:.1f}% (below floor)")

    # ANCHOR band — fair-value anchor hierarchy.
    if anchor_type is not None:
        anchor_points = _ANCHOR_POINTS.get(anchor_type, 0)
        score += anchor_points
        if anchor_points:
            reasons.append(f"{anchor_type} anchor (+{anchor_points})")
        else:
            reasons.append(f"{anchor_type} anchor (+0)")

    # ML band — vs the held-out operating point. None = out of scope, +0.
    if value_filter_score is not None:
        if value_filter_score >= ML_OPERATING_POINT:
            score += 1
            reasons.append(f"ML score >= q* ({ML_OPERATING_POINT:.3f})")
        elif value_filter_score >= ML_WEAK_FLOOR:
            reasons.append(f"ML score in [{ML_WEAK_FLOOR:.2f}, q*)")
        else:
            score -= 1
            reasons.append(f"ML score < {ML_WEAK_FLOOR:.2f} (weak)")

    # +1 baseline maps a bare-minimum premium pick (edge>=floor, consensus
    # anchor, no ML) to 1 star; the strong path (edge>=2x floor +2, pinnacle
    # +2, ML>=q* +1 => score 5) clamps to the 5-star ceiling.
    level = max(1, min(5, score + 1))
    return ConfidenceRating(
        level=level,
        label=_LABELS[level],
        score=score,
        reasons=tuple(reasons),
    )
