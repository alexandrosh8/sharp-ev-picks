"""Shared pydantic bases: frozen models, UTC-required datetimes, market enum."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


def to_utc(value: datetime) -> datetime:
    """Reject naive datetimes; convert any aware datetime to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(UTC)


class InternalModel(BaseModel):
    """Internal contract: immutable, unknown fields are an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class UpstreamModel(BaseModel):
    """Upstream payload contract: immutable, unknown provider fields ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class Market(StrEnum):
    H2H = "h2h"  # 1X2 / moneyline
    DOUBLE_CHANCE = "double_chance"
    DNB = "dnb"  # draw no bet
    SPREADS = "spreads"  # incl. Asian handicap
    TOTALS = "totals"
    TEAM_TOTALS = "team_totals"
    BTTS = "btts"
    CORRECT_SCORE = "correct_score"


class Outcome(StrEnum):
    WON = "won"
    LOST = "lost"
    VOID = "void"
    PUSH = "push"
    # Asian quarter-line split stakes: half wins/loses, half is returned.
    HALF_WON = "half_won"
    HALF_LOST = "half_lost"
