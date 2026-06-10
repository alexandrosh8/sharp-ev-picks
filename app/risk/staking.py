"""Fractional-Kelly recommended staking with a transparent decomposition.

Pure module. Stakes are RECOMMENDATIONS ONLY — this platform never places
bets. Each step of the sizing path stays visible (raw Kelly -> fractional ->
cap) so picks can expose the full breakdown (risk-kelly-engineer mandate).
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


def kelly_fraction(probability: float, decimal_odds: float) -> float:
    """Full-Kelly bankroll fraction, clipped to >= 0 (never recommends laying).

    kelly = ((d - 1) * p - (1 - p)) / (d - 1)
    """
    if not 0.0 <= probability <= 1.0:
        raise ValueError(f"probability must be in [0, 1], got {probability}")
    if decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must exceed 1.0, got {decimal_odds}")
    b = decimal_odds - 1.0
    edge_per_unit = (b * probability - (1.0 - probability)) / b
    return max(edge_per_unit, 0.0)


@dataclass(frozen=True)
class StakePolicy:
    """Sizing policy; constructed from Settings at the composition root."""

    fractional_kelly: float = 0.25
    max_stake_fraction: float = 0.02


@dataclass(frozen=True)
class StakeBreakdown:
    """Transparent sizing path: raw -> fractional -> capped final fraction."""

    raw_kelly: float
    fractional: float
    capped: bool
    final: float


def recommended_stake(
    probability: float,
    decimal_odds: float,
    policy: StakePolicy,
) -> StakeBreakdown:
    raw = kelly_fraction(probability, decimal_odds)
    fractional = raw * policy.fractional_kelly
    capped = fractional > policy.max_stake_fraction
    final = min(fractional, policy.max_stake_fraction)
    return StakeBreakdown(raw_kelly=raw, fractional=fractional, capped=capped, final=final)


def stake_amount(fraction: float, bankroll: Decimal) -> Decimal:
    """Money amount for a bankroll fraction, quantized to cents."""
    if fraction < 0.0:
        raise ValueError(f"stake fraction must be >= 0, got {fraction}")
    return (Decimal(str(fraction)) * bankroll).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
