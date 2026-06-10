"""Pick gate evaluation: a candidate becomes a pick only if EVERY gate passes.

Pure module. Thresholds arrive via GatePolicy (built from Settings at the
composition root). Each failing gate contributes a named reason so rejections
are auditable.

edge = p_model - p_fair (vig-free market probability)
EV   = p_model * (d - 1) - (1 - p_model)        [per unit stake]
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GatePolicy:
    min_edge: float
    min_ev: float
    min_confidence: float
    max_odds_age_seconds: float
    min_liquidity: float


@dataclass(frozen=True)
class PickCandidate:
    event_id: str
    market: str
    selection: str
    decimal_odds: float
    model_probability: float
    fair_probability: float
    confidence: float
    odds_age_seconds: float
    liquidity: float


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    reasons: tuple[str, ...]
    edge: float
    ev: float


def evaluate(candidate: PickCandidate, policy: GatePolicy) -> GateDecision:
    edge = candidate.model_probability - candidate.fair_probability
    ev = candidate.model_probability * (candidate.decimal_odds - 1.0) - (
        1.0 - candidate.model_probability
    )

    reasons: list[str] = []
    if edge < policy.min_edge:
        reasons.append("edge_below_threshold")
    # EV must be strictly positive AND clear the configured minimum.
    if ev <= 0.0 or ev < policy.min_ev:
        reasons.append("ev_below_threshold")
    if candidate.confidence < policy.min_confidence:
        reasons.append("confidence_below_threshold")
    if candidate.odds_age_seconds > policy.max_odds_age_seconds:
        reasons.append("odds_too_stale")
    if candidate.liquidity < policy.min_liquidity:
        reasons.append("insufficient_liquidity")

    return GateDecision(accepted=not reasons, reasons=tuple(reasons), edge=edge, ev=ev)
