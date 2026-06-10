"""Closing Line Value math. Pure module.

clv_log = ln(fill_odds × p_close_fair) = ln(fill_odds / fair_closing_odds).
Positive means the pick beat the (vig-free) close. The closing fair
probability MUST come from the same devig method used elsewhere (odds-math
skill rule); this module just does the arithmetic.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass


def clv_log(fill_decimal_odds: float, closing_fair_probability: float) -> float:
    if fill_decimal_odds <= 1.0:
        raise ValueError(f"fill odds must exceed 1.0, got {fill_decimal_odds}")
    if not 0.0 < closing_fair_probability < 1.0:
        raise ValueError(
            f"closing fair probability must be in (0, 1), got {closing_fair_probability}"
        )
    return math.log(fill_decimal_odds * closing_fair_probability)


def beat_close(fill_decimal_odds: float, closing_fair_probability: float) -> bool:
    return clv_log(fill_decimal_odds, closing_fair_probability) > 0.0


@dataclass(frozen=True)
class ClvRecord:
    pick_id: str
    stake: float
    clv: float  # log CLV


def stake_weighted_clv(records: Sequence[ClvRecord]) -> float:
    total_stake = sum(r.stake for r in records)
    if total_stake <= 0.0:
        raise ValueError("stake-weighted CLV requires positive total stake")
    return sum(r.stake * r.clv for r in records) / total_stake
