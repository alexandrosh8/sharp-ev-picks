"""Event and manual result-tracking contracts."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.schemas.base import InternalModel, Outcome, to_utc


class EventIn(InternalModel):
    external_ref: str
    sport: str
    league: str
    home_team: str
    away_team: str
    starts_at: datetime

    _utc_starts = field_validator("starts_at")(to_utc)


class ResultIn(InternalModel):
    """User-entered settlement of a pick they bet manually (or skipped).

    Never contains account credentials — only what the user chooses to log.
    """

    pick_id: str
    outcome: Outcome
    bet_placed: bool = False
    actual_stake: Decimal | None = Field(default=None, ge=0)
    actual_odds: float | None = Field(default=None, gt=1.0)
    bookmaker_used: str | None = None
    settled_at: datetime
    notes: str = ""

    _utc_settled = field_validator("settled_at")(to_utc)
