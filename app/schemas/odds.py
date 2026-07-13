"""Odds snapshot contracts."""

import math
from datetime import datetime
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from app.identity import (
    BOOKMAKER_MAX_BYTES,
    EVENT_REF_MAX_BYTES,
    MARKET_DETAIL_MAX_BYTES,
    SELECTION_MAX_BYTES,
    require_bounded_identity,
)
from app.schemas.base import InternalModel, Market, to_utc

# Betfair's exchange ladder tops out at 1,000.  Treat larger values as provider
# sentinels/schema drift rather than actionable prices.  Pydantic's numeric
# bounds alone do not reject NaN, so the model also performs explicit finite
# validation below.
MAX_DECIMAL_ODDS = 1_000.0
# Matches odds_snapshots.liquidity NUMERIC(12,2). Reject at the schema boundary
# rather than letting one oversized provider sentinel roll back its event savepoint.
MAX_LIQUIDITY = 9_999_999_999.99
MAX_PROVIDER_CLOCK_SKEW_SECONDS = 300.0

DecimalOdds = Annotated[
    float,
    Field(gt=1.0, le=MAX_DECIMAL_ODDS, description="European decimal odds, (1, 1000]"),
]


class OddsSnapshotIn(InternalModel):
    """One (event, bookmaker, market, selection) price observation."""

    event_id: str
    bookmaker: str
    market: Market
    selection: str
    decimal_odds: DecimalOdds
    liquidity: float | None = Field(default=None, ge=0.0, le=MAX_LIQUIDITY)
    captured_at: datetime  # provider-reported time of the price
    ingested_at: datetime  # our wall-clock at ingestion
    # Provider submarket key (e.g. "asian_handicap_-1_5", "over_under_215_5").
    # Distinct lines of the same Market MUST group separately for devig.
    market_detail: str | None = None

    _utc_captured = field_validator("captured_at")(to_utc)
    _utc_ingested = field_validator("ingested_at")(to_utc)

    @field_validator("event_id")
    @classmethod
    def _bounded_event_id(cls, value: str) -> str:
        return require_bounded_identity(value, maximum_bytes=EVENT_REF_MAX_BYTES, field="event_id")

    @field_validator("bookmaker")
    @classmethod
    def _bounded_bookmaker(cls, value: str) -> str:
        return require_bounded_identity(value, maximum_bytes=BOOKMAKER_MAX_BYTES, field="bookmaker")

    @field_validator("selection")
    @classmethod
    def _bounded_selection(cls, value: str) -> str:
        return require_bounded_identity(value, maximum_bytes=SELECTION_MAX_BYTES, field="selection")

    @field_validator("market_detail")
    @classmethod
    def _bounded_market_detail(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_bounded_identity(
            value,
            maximum_bytes=MARKET_DETAIL_MAX_BYTES,
            field="market_detail",
            allow_empty=True,
        )

    @field_validator("decimal_odds", "liquidity")
    @classmethod
    def _finite_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("numeric odds fields must be finite")
        return value

    @model_validator(mode="after")
    def _captured_not_materially_in_future(self) -> "OddsSnapshotIn":
        skew = (self.captured_at - self.ingested_at).total_seconds()
        if skew > MAX_PROVIDER_CLOCK_SKEW_SECONDS:
            raise ValueError(
                "captured_at cannot be more than "
                f"{MAX_PROVIDER_CLOCK_SKEW_SECONDS:g}s after ingested_at"
            )
        return self

    def age_seconds(self, now: datetime) -> float:
        """Odds age relative to `now` (aware UTC), based on provider time."""
        return (to_utc(now) - self.captured_at).total_seconds()
