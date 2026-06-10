"""Pick output contract — what alerts, the API, and the dashboard consume."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.schemas.base import InternalModel, Market, to_utc

MANUAL_BETTING_REMINDER = "Manual review required. This system does not place bets."


class StakeBreakdownOut(InternalModel):
    raw_kelly: float
    fractional: float
    capped: bool
    final: float


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
    created_at: datetime
    risk_warning: str = "Betting involves risk. Nothing here is guaranteed profit."
    manual_betting_reminder: str = MANUAL_BETTING_REMINDER

    _utc_created = field_validator("created_at")(to_utc)
