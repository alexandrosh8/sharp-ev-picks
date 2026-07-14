"""Event and manual result-tracking contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Final

from pydantic import Field, field_validator, model_validator

from app.schemas.base import InternalModel, Outcome, to_utc
from app.schemas.odds import MAX_DECIMAL_ODDS

MAX_MONEY_AMOUNT: Final = Decimal("9999999999.99")
MAX_MANUAL_ODDS: Final = Decimal(str(MAX_DECIMAL_ODDS))


class EventIn(InternalModel):
    external_ref: str
    sport: str
    league: str
    home_team: str
    away_team: str
    starts_at: datetime

    _utc_starts = field_validator("starts_at")(to_utc)


class EventResultIn(InternalModel):
    """User-entered final score for one event — settles all its open picks.

    The manual path for leagues without a free results feed (e.g. NBA).
    """

    home_score: int = Field(ge=0, le=250)
    away_score: int = Field(ge=0, le=250)


class ResultIn(InternalModel):
    """User-entered settlement of a pick they bet manually (or skipped).

    Never contains account credentials — only what the user chooses to log.
    """

    pick_id: str = Field(min_length=1, max_length=64)
    outcome: Outcome
    bet_placed: bool = False
    actual_stake: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
        allow_inf_nan=False,
    )
    actual_odds: Decimal | None = Field(
        default=None,
        gt=1,
        le=MAX_MANUAL_ODDS,
        max_digits=10,
        decimal_places=4,
        allow_inf_nan=False,
    )
    bookmaker_used: str | None = Field(default=None, min_length=1, max_length=64)
    settled_at: datetime
    notes: str = Field(default="", max_length=4096)

    _utc_settled = field_validator("settled_at")(to_utc)

    @model_validator(mode="after")
    def _potential_pnl_fits_money_column(self) -> "ResultIn":
        has_fill_metadata = any(
            value is not None
            for value in (self.actual_stake, self.actual_odds, self.bookmaker_used)
        )
        if not self.bet_placed and has_fill_metadata:
            raise ValueError("actual fill fields require bet_placed=true")
        if self.actual_stake is None and (
            self.actual_odds is not None or self.bookmaker_used is not None
        ):
            raise ValueError("actual_odds/bookmaker_used require actual_stake")
        if self.actual_stake == 0:
            raise ValueError("actual_stake must be greater than zero")
        if self.actual_stake is None:
            return self
        # When actual odds are omitted the route uses the persisted recommended
        # basis, so reserve against the full accepted odds domain. This keeps
        # both a win's profit and a loss's stake within NUMERIC(12,2) before any
        # database work begins.
        odds = self.actual_odds or MAX_MANUAL_ODDS
        multiplier = max(odds - Decimal(1), Decimal(1))
        if self.actual_stake * multiplier > MAX_MONEY_AMOUNT:
            raise ValueError("actual stake and odds can exceed the persisted money range")
        return self
