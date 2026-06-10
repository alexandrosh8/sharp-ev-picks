"""Odds snapshot contracts."""

from datetime import datetime
from typing import Annotated

from pydantic import Field, field_validator

from app.schemas.base import InternalModel, Market, to_utc

DecimalOdds = Annotated[float, Field(gt=1.0, description="European decimal odds, > 1.0")]


class OddsSnapshotIn(InternalModel):
    """One (event, bookmaker, market, selection) price observation."""

    event_id: str
    bookmaker: str
    market: Market
    selection: str
    decimal_odds: DecimalOdds
    liquidity: float | None = Field(default=None, ge=0.0)
    captured_at: datetime  # provider-reported time of the price
    ingested_at: datetime  # our wall-clock at ingestion

    _utc_captured = field_validator("captured_at")(to_utc)
    _utc_ingested = field_validator("ingested_at")(to_utc)

    def age_seconds(self, now: datetime) -> float:
        """Odds age relative to `now` (aware UTC), based on provider time."""
        return (to_utc(now) - self.captured_at).total_seconds()
