"""Pick output contract — what alerts, the API, and the dashboard consume."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.schemas.base import InternalModel, Market, to_utc

# Formal safety statement. The literal "This system does not place bets" is
# asserted by scripts/safety_audit.sh (CI gate) and is the platform's picks-only
# guarantee — kept here even though pick alerts render the compact ALERT_FOOTER.
MANUAL_BETTING_REMINDER = "Manual review required. This system does not place bets."

# Compact one-line disclaimer at the foot of every pick alert: informational
# only, the user places any bet themselves (the system never does), no guarantee.
ALERT_FOOTER = "ℹ️ Informational only — you place any bet. No profit guaranteed."


class StakeBreakdownOut(InternalModel):
    raw_kelly: float
    fractional: float
    capped: bool  # per-bet cap hit (fractional > max_stake_fraction)
    final: float  # the GRANTED fraction (after the daily-exposure ledger clip)
    # True when the daily-exposure ledger clipped `final` below the per-bet-capped
    # fraction (granted < breakdown.final). Distinguishes a daily clip from the
    # per-bet cap (`capped`) so `final` is reproducible from the inputs.
    daily_clipped: bool = False


class PickOut(InternalModel):
    pick_id: str
    sport: str
    league: str
    event: str
    event_id: str
    market: Market
    selection: str
    bookmaker: str
    decimal_odds: float = Field(gt=1.0)
    model_probability: float = Field(ge=0.0, le=1.0)
    fair_probability: float = Field(ge=0.0, le=1.0)
    edge: float
    ev: float
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_stake_fraction: float = Field(ge=0.0)
    recommended_stake_amount: Decimal = Field(ge=0)
    stake_breakdown: StakeBreakdownOut
    odds_age_seconds: float = Field(ge=0.0)
    liquidity: float | None = None
    reason_summary: str
    # "premium" (edge >= VALUE_MIN_EDGE: alerted + exposure-reserved) or
    # "volume" (shadow tier: persisted + CLV-tracked, alerted ONCE as 🔵 VOLUME
    # on first detection but NEVER exposure-reserved — see app/pipeline.py).
    tier: str = "premium"
    # Calibrated meta-model score P(candidate beats the vig-free Max close)
    # from app/models/value_filter.py — None when the artifact is absent or
    # the candidate is outside the model's trained scope. Informational
    # unless VALUE_ML_FILTER is on (then sub-threshold premium candidates
    # are demoted to the volume tier before alerting).
    value_filter_score: float | None = Field(default=None, ge=0.0, le=1.0)
    # Fair-value anchor that produced this pick: "pinnacle" | "sharp" |
    # "consensus" (app/edge/value.py::anchor_type_for). None for the model
    # strategy. Persisted so live CLV can be stratified by anchor — the
    # consensus fallback's live verdict mechanism.
    anchor_type: str | None = None
    # Final score of the settled game ("HOME-AWAY", e.g. "2-1"). None until the
    # pick settles (or when no score was recorded). Surfaced in the dashboard
    # SETTLED view; /picks serializes the repo dict, so this keeps the contract
    # model in step with the served payload.
    score: str | None = None
    created_at: datetime
    risk_warning: str = "Betting involves risk. Nothing here is guaranteed profit."
    manual_betting_reminder: str = MANUAL_BETTING_REMINDER

    _utc_created = field_validator("created_at")(to_utc)
