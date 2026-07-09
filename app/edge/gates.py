"""Pick gate evaluation: a candidate becomes a pick only if EVERY gate passes.

Pure module. Thresholds arrive via GatePolicy (built from Settings at the
composition root). Each failing gate contributes a named reason so rejections
are auditable.

edge  = p_model - p_fair (vig-free market probability)
d_eff = 1 + (d - 1) * (1 - c)   [decimal odds net of exchange commission]
EV    = p_model * (d_eff - 1) - (1 - p_model)       [per unit stake]

Commission netting (audit 2026-07-09): exchange (commissioned) books pay out
winnings net of commission, so EV — and any Kelly stake sized downstream —
must use d_eff, exactly like the value strategy (app/edge/value.py
effective_odds). Commission is a PAYOUT cost, not a probability signal
(edge-ev-devig-P2-1): the model/fair probabilities and hence the edge stay on
gross prices. Books without a commission entry keep c = 0 (d_eff == d), so
non-exchange behavior is bit-identical.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GatePolicy:
    min_edge: float
    min_ev: float
    min_confidence: float
    max_odds_age_seconds: float
    min_liquidity: float
    # Net commission on winnings by NORMALIZED (strip+lower) bookmaker name.
    # Flows in from the composition root ONLY (app/config.gate_policy, which
    # threads the value strategy's EXCHANGE_COMMISSION table) — this pure
    # module never reads env/config. Empty (default) = no netting anywhere.
    commission_by_book: tuple[tuple[str, float], ...] = ()


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
    # Book offering the price — consulted ONLY for commission netting; the
    # default "" (unknown book) nets nothing, preserving legacy call sites.
    bookmaker: str = ""


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    reasons: tuple[str, ...]
    edge: float
    ev: float
    # Decimal odds net of exchange commission — the price `ev` was computed
    # on. Callers MUST size Kelly stakes on THIS, never the gross odds.
    effective_odds: float


def _commission_rate(policy: GatePolicy, bookmaker: str) -> float:
    norm = bookmaker.strip().lower()
    for book, rate in policy.commission_by_book:
        if book == norm:
            return rate
    return 0.0


def evaluate(candidate: PickCandidate, policy: GatePolicy) -> GateDecision:
    edge = candidate.model_probability - candidate.fair_probability
    effective_odds = 1.0 + (candidate.decimal_odds - 1.0) * (
        1.0 - _commission_rate(policy, candidate.bookmaker)
    )
    ev = candidate.model_probability * (effective_odds - 1.0) - (1.0 - candidate.model_probability)

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

    return GateDecision(
        accepted=not reasons,
        reasons=tuple(reasons),
        edge=edge,
        ev=ev,
        effective_odds=effective_odds,
    )
