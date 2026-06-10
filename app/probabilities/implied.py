"""Implied probability and overround from decimal odds. Pure module."""

from collections.abc import Sequence


def implied_probability(decimal_odds: float) -> float:
    """Return 1/odds, validating decimal odds exceed 1.0."""
    if decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must exceed 1.0, got {decimal_odds}")
    return 1.0 / decimal_odds


def overround(odds: Sequence[float]) -> float:
    """Book margin: sum of implied probabilities minus 1 (negative = underround)."""
    if len(odds) < 2:
        raise ValueError("a market needs at least two outcomes")
    return sum(implied_probability(d) for d in odds) - 1.0
