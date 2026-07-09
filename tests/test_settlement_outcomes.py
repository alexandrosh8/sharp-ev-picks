"""Outcome mapping — every live market settles correctly from a final score.

Selection strings mirror app/ingestion/oddsportal.py::_selections exactly;
if those formats change, these tests must fail.
"""

from decimal import Decimal

import pytest

from app.schemas.base import Outcome
from app.settlement.outcomes import (
    TENNIS_SETTLEMENT_CONVENTION,
    pick_pnl,
    pick_roi,
    provisional_result,
    settle_selection,
    settle_selection_retired,
)

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


# --- h2h two-way moneyline tie (NFL) ------------------------------------------


def test_two_way_h2h_tie_pushes_for_american_football() -> None:
    # NFL h2h is a TWO-WAY moneyline (no Draw leg is offered/minted): a tied
    # final (possible after OT, e.g. 20-20) refunds the stake under standard
    # book rules — grading BOTH sides LOST records a phantom full-stake loss.
    assert (
        settle_selection("h2h", HOME, HOME, AWAY, 20, 20, sport_key="american_football")
        is Outcome.PUSH
    )
    assert (
        settle_selection("h2h", AWAY, HOME, AWAY, 20, 20, sport_key="american_football")
        is Outcome.PUSH
    )


def test_two_way_h2h_decisive_final_unchanged_for_american_football() -> None:
    assert (
        settle_selection("h2h", HOME, HOME, AWAY, 27, 20, sport_key="american_football")
        is Outcome.WON
    )
    assert (
        settle_selection("h2h", AWAY, HOME, AWAY, 27, 20, sport_key="american_football")
        is Outcome.LOST
    )


def test_three_way_h2h_tie_semantics_unchanged() -> None:
    # Soccer 1X2 keeps its Draw leg: team legs LOSE a draw, the Draw leg WINS —
    # and the sport-less default (every existing caller) is byte-identical.
    assert settle_selection("h2h", HOME, HOME, AWAY, 1, 1, sport_key="soccer") is Outcome.LOST
    assert settle_selection("h2h", "Draw", HOME, AWAY, 1, 1, sport_key="soccer") is Outcome.WON
    assert settle("h2h", HOME, 1, 1) is Outcome.LOST
    assert settle("h2h", "Draw", 1, 1) is Outcome.WON


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


def test_oddschecker_line_bearing_selections_settle() -> None:
    # DUAL-PROVIDER BRIDGE: the exact line-bearing selection strings the OddsChecker
    # parser now emits (app.ingestion.oddschecker._line_bearing_selection) must grade
    # here — previously bare "Over"/"Carolina Panthers" raised ValueError('unparseable').
    from app.ingestion.oddschecker import _line_bearing_selection
    from app.schemas.base import Market

    totals_sel = _line_bearing_selection("Over", "41.5", Market.TOTALS)
    assert totals_sel == "Over 41.5"
    assert settle("totals", totals_sel, 30, 12) is Outcome.WON  # 42 > 41.5

    spread_sel = _line_bearing_selection(HOME, "-1.5", Market.SPREADS)
    assert spread_sel == f"{HOME} -1.5"
    assert settle("spreads", spread_sel, 3, 1) is Outcome.WON  # margin 2 - 1.5 > 0


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


def test_double_chance_reversed_event_orientation() -> None:
    # A cross-source duplicate can mint the event with home/away SWAPPED relative
    # to the pick's stored selection text. DC must grade orientation-independently
    # (like h2h/dnb) — the selection names a team, not a physical side. Here the
    # event is reversed: physical home=AWAY(Beta), physical away=HOME(Alpha); the
    # (home_score, away_score) tuple is in that reversed frame.
    # "{HOME} or Draw" == Alpha not-lose. Alpha is physical away here.
    assert (
        settle_selection("double_chance", f"{HOME} or Draw", AWAY, HOME, 0, 1) is Outcome.WON
    )  # Alpha won
    assert (
        settle_selection("double_chance", f"{HOME} or Draw", AWAY, HOME, 1, 1) is Outcome.WON
    )  # draw
    assert (
        settle_selection("double_chance", f"{HOME} or Draw", AWAY, HOME, 2, 0) is Outcome.LOST
    )  # Alpha lost
    # "Draw or {AWAY}" == Beta not-lose. Beta is physical home here.
    assert (
        settle_selection("double_chance", f"Draw or {AWAY}", AWAY, HOME, 1, 0) is Outcome.WON
    )  # Beta won
    assert (
        settle_selection("double_chance", f"Draw or {AWAY}", AWAY, HOME, 0, 1) is Outcome.LOST
    )  # Beta lost
    # "{A} or {B}" (no Draw) == not-a-draw, orientation-independent.
    assert settle_selection("double_chance", f"{HOME} or {AWAY}", AWAY, HOME, 2, 1) is Outcome.WON
    assert settle_selection("double_chance", f"{HOME} or {AWAY}", AWAY, HOME, 1, 1) is Outcome.LOST


def test_double_chance_unparseable_token_still_fails_loud() -> None:
    # A token matching NEITHER team is genuinely unparseable — never silently pass.
    with pytest.raises(ValueError, match="matches neither"):
        settle("double_chance", "Gamma or Draw", 1, 1)
    with pytest.raises(ValueError, match="matches neither"):
        settle("double_chance", f"{HOME} or Gamma", 1, 1)
    with pytest.raises(ValueError, match="unparseable"):
        settle("double_chance", "just one token", 1, 1)


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


# --- spreads: quarter lines (Asian split stakes) --------------------------------


def test_quarter_line_splits_into_adjacent_half_stakes() -> None:
    # -0.25 = half stake at 0.0 + half at -0.5
    assert settle("spreads", f"{HOME} -0.25", 2, 1) is Outcome.WON
    assert settle("spreads", f"{HOME} -0.25", 1, 1) is Outcome.HALF_LOST  # 0.0 pushes, -0.5 loses
    assert settle("spreads", f"{HOME} -0.25", 0, 1) is Outcome.LOST
    # -0.75 = half at -0.5 + half at -1.0
    assert settle("spreads", f"{HOME} -0.75", 2, 1) is Outcome.HALF_WON  # -0.5 wins, -1.0 pushes
    assert settle("spreads", f"{HOME} -0.75", 3, 1) is Outcome.WON
    assert settle("spreads", f"{HOME} -0.75", 1, 1) is Outcome.LOST
    # -1.25 = half at -1.0 + half at -1.5
    assert settle("spreads", f"{HOME} -1.25", 2, 1) is Outcome.HALF_LOST
    assert settle("spreads", f"{HOME} -1.25", 3, 1) is Outcome.WON


def test_quarter_line_receiving_side() -> None:
    # away +0.25 = half at 0.0 + half at +0.5
    assert settle("spreads", f"{AWAY} +0.25", 1, 1) is Outcome.HALF_WON  # 0.0 pushes, +0.5 wins
    assert settle("spreads", f"{AWAY} +0.25", 0, 1) is Outcome.WON
    assert settle("spreads", f"{AWAY} +0.25", 2, 1) is Outcome.LOST
    # quarter-line integer components PUSH on tie (Asian), unlike whole-line
    # selections which are European handicap (3-way) by loader config
    assert settle("spreads", f"{AWAY} -0.25", 1, 1) is Outcome.HALF_LOST


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


def test_pnl_half_stakes() -> None:
    stake = Decimal("20.00")
    # half the stake wins at the odds, half is returned
    assert pick_pnl(Outcome.HALF_WON, stake, Decimal("2.10")) == Decimal("11.00")
    # half the stake is lost, half is returned
    assert pick_pnl(Outcome.HALF_LOST, stake, Decimal("2.10")) == Decimal("-10.00")


# --- provisional_result (CLOSED-tab read-time grading) -----------------------


def test_provisional_result_won_with_pnl() -> None:
    # home win, h2h pick on HOME -> WON; pnl = stake*(odds-1)
    outcome, pnl = provisional_result(
        "h2h", HOME, HOME, AWAY, 2, 1, Decimal("10.00"), Decimal("1.65")
    )
    assert outcome == "won"
    assert pnl == "6.50"


def test_provisional_result_lost() -> None:
    outcome, pnl = provisional_result(
        "h2h", HOME, HOME, AWAY, 0, 1, Decimal("10.00"), Decimal("1.65")
    )
    assert outcome == "lost"
    assert pnl == "-10.00"


def test_provisional_result_missing_score_is_none() -> None:
    # in-play / not yet scraped -> no guess
    assert provisional_result("h2h", HOME, HOME, AWAY, None, None) == (None, None)
    assert provisional_result("h2h", HOME, HOME, AWAY, 2, None) == (None, None)


def test_provisional_result_unmappable_selection_is_none() -> None:
    # an ungradeable market never guesses an outcome
    assert provisional_result("nonsense", "whatever", HOME, AWAY, 2, 1) == (None, None)


def test_provisional_result_outcome_without_stake_has_no_pnl() -> None:
    outcome, pnl = provisional_result("h2h", AWAY, HOME, AWAY, 0, 2)
    assert outcome == "won"
    assert pnl is None


def test_provisional_fields_null_for_superseded_pick_with_score() -> None:
    # A superseded (deduplicated) pick that happens to carry a scraped final
    # score must NOT surface a provisional result: the dashboard count tiles key
    # on provisional_outcome, so a graded superseded row inflates "Settled
    # (loaded)" as if the dedup'd bet had landed (the exact dedup mislead).
    from app.storage.models import Pick
    from app.storage.repositories import _provisional_result_fields

    pick = Pick(
        market="totals",
        selection="Under 170.5",  # total 167 -> would grade WON if not suppressed
        status="superseded",
        recommended_stake_amount=Decimal("10.00"),
        decimal_odds=Decimal("1.90"),
    )
    assert _provisional_result_fields(pick, HOME, AWAY, 80, 87) == {
        "provisional_outcome": None,
        "provisional_pnl": None,
    }


def test_provisional_fields_graded_for_alerted_pick_with_score() -> None:
    # The real (non-superseded) open pick still gets its CLOSED-tab read-time
    # provisional grade — the fix must not stop counting genuine settled picks.
    from app.storage.models import Pick
    from app.storage.repositories import _provisional_result_fields

    pick = Pick(
        market="totals",
        selection="Under 170.5",
        status="alerted",
        recommended_stake_amount=Decimal("10.00"),
        decimal_odds=Decimal("1.90"),
    )
    fields = _provisional_result_fields(pick, HOME, AWAY, 80, 87)
    assert fields["provisional_outcome"] == "won"
    assert fields["provisional_pnl"] == "9.00"


# --- football Asian Handicap (visibility-only volume market, commit 706f87e) ---
#
# A football AH pick persists with market=spreads and the human-readable
# selection app/ingestion/oddsportal.py::_selections emits — "Arsenal -1.5"
# (home, feed idx0) / "Chelsea +1.5" (away, feed idx1, the NEGATED line) — NOT
# the raw feed label "team1_handicap". These lock that the settlement engine
# grades that exact persisted form so the new volume/shadow AH picks realise an
# outcome + P&L and their CLV accrues. The grading reuses the SPREADS
# quarter-line split-stake path the rows above already exercise.

AH_HOME = "Arsenal"
AH_AWAY = "Chelsea"


def ah(selection: str, hs: int, as_: int) -> Outcome:
    return settle_selection("spreads", selection, AH_HOME, AH_AWAY, hs, as_)


def test_football_ah_home_half_line_settles() -> None:
    assert ah(f"{AH_HOME} -1.5", 3, 1) is Outcome.WON  # margin +2 beats -1.5
    assert ah(f"{AH_HOME} -1.5", 2, 1) is Outcome.LOST  # margin +1 short of -1.5
    assert ah(f"{AH_HOME} +0.5", 1, 1) is Outcome.WON  # tie covered by +0.5


def test_football_ah_away_half_line_settles() -> None:
    # away idx1 persists the negated line: a home -1.5 line is "Chelsea +1.5".
    assert ah(f"{AH_AWAY} +1.5", 2, 1) is Outcome.WON
    assert ah(f"{AH_AWAY} +1.5", 3, 1) is Outcome.LOST
    assert ah(f"{AH_AWAY} +0.5", 1, 1) is Outcome.WON


def test_football_ah_quarter_line_split_stake_both_sides() -> None:
    # home -0.75 = half stake at -0.5 + half at -1.0
    assert ah(f"{AH_HOME} -0.75", 2, 1) is Outcome.HALF_WON  # -0.5 wins, -1.0 pushes
    assert ah(f"{AH_HOME} -0.75", 3, 1) is Outcome.WON
    assert ah(f"{AH_HOME} -0.75", 1, 1) is Outcome.LOST
    # away +0.25 = half at 0.0 + half at +0.5
    assert ah(f"{AH_AWAY} +0.25", 1, 1) is Outcome.HALF_WON  # 0.0 pushes, +0.5 wins
    assert ah(f"{AH_AWAY} -0.25", 1, 1) is Outcome.HALF_LOST  # 0.0 pushes, -0.5 loses


def test_football_ah_missing_or_unparseable_line_refused() -> None:
    # No signed line / a non-numeric line / a bad score must RAISE so the
    # settler skips + logs (never silently grades a wrong outcome).
    with pytest.raises(ValueError):
        ah(AH_HOME, 2, 1)  # bare team, no handicap
    with pytest.raises(ValueError):
        ah(f"{AH_HOME} x", 2, 1)  # non-numeric handicap
    with pytest.raises(ValueError):
        settle_selection("spreads", f"{AH_HOME} -1.5", AH_HOME, AH_AWAY, -1, 0)  # negative score


def _ref_settle_ah(result: int, handicap: float, odds: float) -> float:
    """Verified standalone reference (scratchpad ah_backtest.py::settle_ah):
    profit per 1u for a bet whose goal margin is `result` at `handicap`.
    Quarter lines split the stake across the two adjacent half/whole lines;
    push/half handled. The engine's outcome->P&L MUST match this for every
    half/quarter line on both sides — that parity is the AH settlement
    contract, so this reference is embedded here as the oracle."""
    if abs(handicap * 2 - round(handicap * 2)) > 1e-9:  # quarter line
        return 0.5 * _ref_settle_ah(result, handicap - 0.25, odds) + 0.5 * _ref_settle_ah(
            result, handicap + 0.25, odds
        )
    net = result + handicap
    if net > 1e-9:
        return odds - 1.0
    if net < -1e-9:
        return -1.0
    return 0.0  # push


def test_football_ah_engine_matches_verified_reference() -> None:
    # Half + quarter lines only — the loader rejects integer (PUSH) Asian lines,
    # so those never reach a pick; whole lines that DO appear are European
    # handicaps (3-way), graded separately above. Both sides, goal margins 0..5
    # each way. Zero mismatches == the engine reproduces the audited backtest.
    odds = Decimal("2.0")
    stake = Decimal("1")
    lines = [s * 0.25 for s in range(-20, 21) if abs(s * 0.25 - round(s * 0.25)) > 1e-9]
    checked = 0
    for line in lines:
        for hs in range(6):
            for as_ in range(6):
                margin = hs - as_
                home_pnl = float(pick_pnl(ah(f"{AH_HOME} {line:+g}", hs, as_), stake, odds))
                assert abs(home_pnl - _ref_settle_ah(margin, line, 2.0)) < 1e-9
                away_pnl = float(pick_pnl(ah(f"{AH_AWAY} {-line:+g}", hs, as_), stake, odds))
                assert abs(away_pnl - _ref_settle_ah(-margin, -line, 2.0)) < 1e-9
                checked += 2
    assert checked == len(lines) * 36 * 2


# --- tennis retirement convention (TENNIS_SETTLEMENT_CONVENTION) --------------


def test_tennis_convention_is_declared() -> None:
    assert TENNIS_SETTLEMENT_CONVENTION == "pinnacle_one_set"


def test_retired_h2h_grades_advancing_player() -> None:
    # Pinnacle-style: >=1 completed set + a player advanced -> moneyline
    # graded to the advancing side, regardless of the partial set score.
    assert settle_selection_retired("h2h", HOME, HOME, AWAY, "home") is Outcome.WON
    assert settle_selection_retired("h2h", HOME, HOME, AWAY, "away") is Outcome.LOST
    assert settle_selection_retired("h2h", AWAY, HOME, AWAY, "away") is Outcome.WON
    assert settle_selection_retired("h2h", AWAY, HOME, AWAY, "home") is Outcome.LOST


def test_retired_non_h2h_markets_void() -> None:
    # The match total is undefined after a retirement — totals (and any other
    # market) return the stake rather than grade from a fragment.
    assert settle_selection_retired("totals", "Over 2.5", HOME, AWAY, "home") is Outcome.VOID
    assert settle_selection_retired("spreads", f"{HOME} -1.5", HOME, AWAY, "away") is Outcome.VOID


def test_oddschecker_tennis_totals_selection_grades_as_total_sets() -> None:
    # DUAL-PROVIDER BRIDGE (tennis): the OddsChecker parser now emits a
    # line-bearing totals selection identical to OddsPortal ("Over 2.5"). For
    # tennis the score is SETS won, so settle_selection grades the exact produced
    # string against the total SETS played — no settler change was needed.
    from app.ingestion.oddschecker import _line_bearing_selection
    from app.schemas.base import Market

    sel = _line_bearing_selection("Over", "2.5", Market.TOTALS)
    assert sel == "Over 2.5"
    # best-of-3 gone the distance: 2 + 1 = 3 sets > 2.5
    assert settle("totals", sel, 2, 1) is Outcome.WON
    assert settle("totals", "Under 2.5", 2, 1) is Outcome.LOST
    # straight-sets: 2 + 0 = 2 sets < 2.5
    assert settle("totals", sel, 2, 0) is Outcome.LOST
    assert settle("totals", "Under 2.5", 2, 0) is Outcome.WON
    # void-before-a-decided-total (pinnacle_one_set): on retirement/abnormal
    # completion the match total is undefined and is never inferred from a
    # fragment — the same produced string VOIDs, it does not grade.
    assert settle_selection_retired("totals", sel, HOME, AWAY, "home") is Outcome.VOID


def test_retired_refuses_unknown_winner_or_selection() -> None:
    with pytest.raises(ValueError, match="advancing side"):
        settle_selection_retired("h2h", HOME, HOME, AWAY, None)
    with pytest.raises(ValueError, match="advancing side"):
        settle_selection_retired("h2h", HOME, HOME, AWAY, "either")
    with pytest.raises(ValueError, match="neither player"):
        settle_selection_retired("h2h", "Somebody Else", HOME, AWAY, "home")
