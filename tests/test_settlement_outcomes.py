"""Outcome mapping — every live market settles correctly from a final score.

Selection strings mirror app/ingestion/oddsportal.py::_selections exactly;
if those formats change, these tests must fail.
"""

from decimal import Decimal

import pytest

from app.schemas.base import Outcome
from app.settlement.outcomes import pick_pnl, pick_roi, settle_selection

HOME = "Alpha FC"
AWAY = "Beta United"


def settle(market: str, selection: str, hs: int, as_: int) -> Outcome:
    return settle_selection(market, selection, HOME, AWAY, hs, as_)


# --- h2h (1x2 and basketball moneyline) --------------------------------------


def test_h2h_home_win() -> None:
    assert settle("h2h", HOME, 2, 1) is Outcome.WON
    assert settle("h2h", HOME, 1, 1) is Outcome.LOST
    assert settle("h2h", HOME, 0, 1) is Outcome.LOST


def test_h2h_draw() -> None:
    assert settle("h2h", "Draw", 1, 1) is Outcome.WON
    assert settle("h2h", "Draw", 2, 1) is Outcome.LOST


def test_h2h_away_win() -> None:
    assert settle("h2h", AWAY, 0, 1) is Outcome.WON
    assert settle("h2h", AWAY, 3, 1) is Outcome.LOST


# --- totals -------------------------------------------------------------------


def test_totals_over_under_half_line() -> None:
    assert settle("totals", "Over 2.5", 2, 1) is Outcome.WON
    assert settle("totals", "Over 2.5", 1, 1) is Outcome.LOST
    assert settle("totals", "Under 2.5", 1, 1) is Outcome.WON
    assert settle("totals", "Under 2.5", 2, 1) is Outcome.LOST


def test_totals_basketball_lines() -> None:
    assert settle("totals", "Over 215.5", 110, 106) is Outcome.WON
    assert settle("totals", "Under 225.5", 110, 106) is Outcome.WON


def test_totals_integer_line_pushes_on_exact() -> None:
    assert settle("totals", "Over 3", 2, 1) is Outcome.PUSH
    assert settle("totals", "Under 3", 2, 1) is Outcome.PUSH


# --- btts ---------------------------------------------------------------------


def test_btts() -> None:
    assert settle("btts", "BTTS Yes", 1, 1) is Outcome.WON
    assert settle("btts", "BTTS Yes", 2, 0) is Outcome.LOST
    assert settle("btts", "BTTS No", 2, 0) is Outcome.WON
    assert settle("btts", "BTTS No", 1, 2) is Outcome.LOST


# --- dnb ----------------------------------------------------------------------


def test_dnb_win_push_lose() -> None:
    assert settle("dnb", HOME, 2, 0) is Outcome.WON
    assert settle("dnb", HOME, 1, 1) is Outcome.PUSH
    assert settle("dnb", HOME, 0, 1) is Outcome.LOST
    assert settle("dnb", AWAY, 0, 1) is Outcome.WON
    assert settle("dnb", AWAY, 2, 2) is Outcome.PUSH


# --- double chance ------------------------------------------------------------


def test_double_chance_legs() -> None:
    assert settle("double_chance", f"{HOME} or Draw", 1, 1) is Outcome.WON
    assert settle("double_chance", f"{HOME} or Draw", 0, 1) is Outcome.LOST
    assert settle("double_chance", f"{HOME} or {AWAY}", 0, 1) is Outcome.WON
    assert settle("double_chance", f"{HOME} or {AWAY}", 2, 2) is Outcome.LOST
    assert settle("double_chance", f"Draw or {AWAY}", 0, 0) is Outcome.WON
    assert settle("double_chance", f"Draw or {AWAY}", 1, 0) is Outcome.LOST


# --- spreads: Asian half-lines ------------------------------------------------


def test_asian_handicap_half_lines() -> None:
    assert settle("spreads", f"{HOME} -1.5", 3, 1) is Outcome.WON
    assert settle("spreads", f"{HOME} -1.5", 2, 1) is Outcome.LOST
    assert settle("spreads", f"{AWAY} +1.5", 2, 1) is Outcome.WON
    assert settle("spreads", f"{AWAY} +1.5", 3, 1) is Outcome.LOST


def test_asian_handicap_basketball() -> None:
    assert settle("spreads", f"{HOME} -5.5", 110, 100) is Outcome.WON
    assert settle("spreads", f"{AWAY} +5.5", 110, 105) is Outcome.WON


# --- spreads: European handicap (3-way, integer lines) -------------------------


def test_european_handicap_team_leg() -> None:
    # EH -1 home: must win by 2+ — adjusted draw LOSES (3-way market).
    assert settle("spreads", f"{HOME} -1", 3, 1) is Outcome.WON
    assert settle("spreads", f"{HOME} -1", 2, 1) is Outcome.LOST
    assert settle("spreads", f"{HOME} -1", 1, 1) is Outcome.LOST
    assert settle("spreads", f"{AWAY} +1", 1, 1) is Outcome.WON
    assert settle("spreads", f"{AWAY} +1", 2, 1) is Outcome.LOST


def test_european_handicap_draw_leg() -> None:
    # "Draw (-1)" wins exactly when home wins by 1.
    assert settle("spreads", "Draw (-1)", 2, 1) is Outcome.WON
    assert settle("spreads", "Draw (-1)", 1, 1) is Outcome.LOST
    assert settle("spreads", "Draw (-1)", 3, 1) is Outcome.LOST


# --- errors --------------------------------------------------------------------


def test_unknown_selection_raises() -> None:
    with pytest.raises(ValueError):
        settle("h2h", "Gamma City", 1, 0)
    with pytest.raises(ValueError):
        settle("spreads", "Gamma City -1.5", 1, 0)
    with pytest.raises(ValueError):
        settle("correct_score", "2:1", 2, 1)  # market not settleable


def test_negative_scores_rejected() -> None:
    with pytest.raises(ValueError):
        settle("h2h", HOME, -1, 0)


# --- pnl / roi ------------------------------------------------------------------


def test_pnl_and_roi() -> None:
    stake = Decimal("20.00")
    assert pick_pnl(Outcome.WON, stake, Decimal("2.10")) == Decimal("22.00")
    assert pick_pnl(Outcome.LOST, stake, Decimal("2.10")) == Decimal("-20.00")
    assert pick_pnl(Outcome.PUSH, stake, Decimal("2.10")) == Decimal("0.00")
    assert pick_pnl(Outcome.VOID, stake, Decimal("2.10")) == Decimal("0.00")
    assert pick_roi(Decimal("22.00"), stake) == Decimal("1.1")
    assert pick_roi(Decimal("-20.00"), stake) == Decimal("-1")
    assert pick_roi(Decimal("0.00"), Decimal("0")) is None
