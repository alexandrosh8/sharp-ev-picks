"""Synthetic-data tests for the tennis VALUE backtest helpers.

scripts/sports/tennis_backtest.py loads pandas/httpx lazily (only inside the
network-touching loaders), so its pure helpers import with no extras and need
no `importorskip`. We load the script by path (scripts/ is not a package) and
exercise: settlement, edge selection (one bet per match, odds floor,
threshold), the match-clustered bootstrap CI, and the COMPUTED verdict — most
importantly that the absence of a closing line forces a visibility-only
ceiling no matter how good held-out ROI is.

No network, no real dataset — synthetic rows only.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

from app.probabilities.devig import DevigMethod

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sports" / "tennis_backtest.py"
_spec = importlib.util.spec_from_file_location("tennis_backtest", _SCRIPT)
assert _spec is not None and _spec.loader is not None
tb: Any = importlib.util.module_from_spec(_spec)
# dataclasses resolves sys.modules[cls.__module__] at class creation — register
# the module BEFORE exec_module (importlib docs pattern).
sys.modules["tennis_backtest"] = tb
_spec.loader.exec_module(tb)


def _row(
    match_id: str,
    psw: float | None,
    psl: float | None,
    maxw: float | None,
    maxl: float | None,
    winner_idx: int = 0,
    completed: bool = True,
) -> Any:
    return tb.TennisRow(
        match_id=match_id,
        sharp=(psw, psl),
        best=(maxw, maxl),
        winner_idx=winner_idx,
        completed=completed,
    )


# --- settlement ------------------------------------------------------------
def test_settle_side_winner_is_index_zero() -> None:
    row = _row("m", 1.8, 2.1, 1.85, 2.2, winner_idx=0)
    assert tb.settle_side(row, 0) is True
    assert tb.settle_side(row, 1) is False


def test_settle_side_respects_explicit_winner_idx() -> None:
    # synthetic row where the SECOND side won — the helper must follow the flag
    row = _row("m", 1.8, 2.1, 1.85, 2.2, winner_idx=1)
    assert tb.settle_side(row, 1) is True
    assert tb.settle_side(row, 0) is False


# --- selection -------------------------------------------------------------
def test_non_completed_match_yields_no_bet() -> None:
    row = _row("m", 1.5, 3.0, 2.0, 4.0, completed=False)
    assert tb.select_bet(row, 0.0, DevigMethod.MULTIPLICATIVE) is None


def test_missing_price_yields_no_bet() -> None:
    assert tb.select_bet(_row("m", None, 3.0, 2.0, 4.0), 0.0, DevigMethod.MULTIPLICATIVE) is None
    assert tb.select_bet(_row("m", 1.5, 3.0, None, 4.0), 0.0, DevigMethod.MULTIPLICATIVE) is None


def test_picks_the_side_with_positive_edge() -> None:
    # Sharp Pinnacle is tight; the best book is generous on side 0 only.
    # fair(side0) from devig([2.0, 2.0]) == 0.5; best price 2.30 -> 1/2.30=0.4348
    # edge0 = 0.5 - 0.4348 = +0.0652 (>0). Side 1 best == sharp -> edge ~ 0.
    row = _row("m", 2.0, 2.0, 2.30, 1.95, winner_idx=0)
    bet = tb.select_bet(row, 0.01, DevigMethod.MULTIPLICATIVE)
    assert bet is not None
    assert bet.odds == 2.30  # took the value side, not the loser side
    assert bet.won is True  # winner_idx 0 == the side we bet
    assert bet.edge > 0.05


def test_threshold_filters_out_thin_edges() -> None:
    # tiny edge: best barely above sharp -> below a 5% threshold, no bet
    row = _row("m", 2.0, 2.0, 2.02, 2.02)
    assert tb.select_bet(row, 0.05, DevigMethod.MULTIPLICATIVE) is None


def test_odds_floor_excludes_short_prices() -> None:
    # devig([1.30, 5.0]) -> fair (0.7937, 0.2063).
    # side 0: edge +0.127 but price 1.50 < floor 1.80 -> EXCLUDED by the floor.
    # side 1: edge 0.2063 - 1/4.0 = -0.0437 < 0 -> no edge.
    # So with the floor the only positive-edge side is unbettable -> no bet.
    row = _row("m", 1.30, 5.0, 1.50, 4.0, winner_idx=0)
    assert tb.select_bet(row, 0.0, DevigMethod.MULTIPLICATIVE, min_odds=1.80) is None
    # sanity: WITHOUT the floor, side 0's value is taken at 1.50
    bet = tb.select_bet(row, 0.0, DevigMethod.MULTIPLICATIVE, min_odds=1.0)
    assert bet is not None and bet.odds == 1.50


def test_one_bet_per_match_even_when_both_sides_have_edge() -> None:
    # construct a row where BOTH best prices beat the sharp fair; helper must
    # return exactly ONE bet (the higher-edge side).
    row = _row("m", 2.0, 2.0, 2.20, 2.30, winner_idx=0)
    bet = tb.select_bet(row, 0.0, DevigMethod.MULTIPLICATIVE)
    assert bet is not None
    # side1 has the larger price -> larger edge -> selected; only one bet object
    assert bet.odds == 2.30
    rows = [row]
    assert len(tb.bets_for(rows, 0.0, DevigMethod.MULTIPLICATIVE)) == 1


def test_bet_has_no_clv_fields_populated() -> None:
    row = _row("m", 2.0, 2.0, 2.30, 1.95)
    bet = tb.select_bet(row, 0.0, DevigMethod.MULTIPLICATIVE)
    assert bet is not None
    assert bet.clv_close is None  # tennis source has no closing line
    assert bet.clv_max_close is None


# --- bootstrap CI ----------------------------------------------------------
def test_bootstrap_ci_is_deterministic_and_brackets_point_roi() -> None:
    # 200 winners at 2.0, 200 losers -> point ROI = 0; CI must bracket 0.
    bets = [tb.TBet(match_id=f"w{i}", won=True, odds=2.0, edge=0.05) for i in range(200)]
    bets += [tb.TBet(match_id=f"l{i}", won=False, odds=2.0, edge=0.05) for i in range(200)]
    lo1, hi1 = tb.bootstrap_roi_ci(bets, n_boot=500, seed=7)
    lo2, hi2 = tb.bootstrap_roi_ci(bets, n_boot=500, seed=7)
    assert (lo1, hi1) == (lo2, hi2)  # deterministic for a fixed seed
    assert lo1 < 0.0 < hi1  # CI brackets the true ROI (~0)


def test_bootstrap_ci_all_winners_is_strictly_positive() -> None:
    bets = [tb.TBet(match_id=f"w{i}", won=True, odds=2.0, edge=0.1) for i in range(100)]
    lo, hi = tb.bootstrap_roi_ci(bets, n_boot=500, seed=1)
    assert lo > 0.0 and hi > 0.0  # every resample wins -> ROI == +1.0 throughout
    assert abs(lo - 1.0) < 1e-9


def test_bootstrap_ci_empty_is_zero_zero() -> None:
    assert tb.bootstrap_roi_ci([], n_boot=10) == (0.0, 0.0)


def test_clustered_resampler_keeps_a_match_together() -> None:
    # two bets sharing one match_id form a single cluster; with one cluster the
    # only resample is that cluster repeated -> ROI is the cluster ROI exactly.
    bets = [
        tb.TBet(match_id="same", won=True, odds=3.0, edge=0.1),
        tb.TBet(match_id="same", won=False, odds=3.0, edge=0.1),
    ]
    lo, hi = tb.bootstrap_roi_ci(bets, n_boot=50, seed=3)
    # cluster ROI = ((3-1) + (-1)) / 2 = 0.5; every resample is identical.
    assert abs(lo - 0.5) < 1e-9 and abs(hi - 0.5) < 1e-9


# --- verdict (the doctrine gate) -------------------------------------------
def test_thin_sample_rejects() -> None:
    test = tb.Stats(
        n=40,
        hit=0.6,
        roi=0.2,
        roi_lo=0.05,
        roi_hi=0.35,
        clv_close=None,
        clv_close_se=None,
        inc_clv_se=None,
    )
    base = tb.Stats(
        n=40,
        hit=0.5,
        roi=0.0,
        roi_lo=-0.1,
        roi_hi=0.1,
        clv_close=None,
        clv_close_se=None,
        inc_clv_se=None,
    )
    v = tb.compute_verdict(test, base, has_closing_line=False, min_n=150)
    assert v.startswith("reject")


def test_no_closing_line_caps_at_visibility_only_even_with_positive_roi() -> None:
    # Strongly positive, statistically clean held-out ROI — but no closing line.
    # Doctrine: this can NEVER be "alerts"; the verdict must be visibility-only.
    test = tb.Stats(
        n=600,
        hit=0.55,
        roi=0.08,
        roi_lo=0.02,
        roi_hi=0.14,
        clv_close=None,
        clv_close_se=None,
        inc_clv_se=None,
    )
    base = tb.Stats(
        n=600,
        hit=0.5,
        roi=0.0,
        roi_lo=-0.04,
        roi_hi=0.04,
        clv_close=None,
        clv_close_se=None,
        inc_clv_se=None,
    )
    v = tb.compute_verdict(test, base, has_closing_line=False)
    assert "visibility-only" in v
    # the verdict PREFIX must not be "alerts" (the word "live alerts" appears
    # in the explanatory clause, so check the leading token, not a substring)
    assert not v.startswith("alerts")
    assert "UNDEFINED" in v  # explains WHY it cannot be promoted


def test_no_closing_line_negative_roi_is_visibility_only_unvalidated() -> None:
    test = tb.Stats(
        n=600,
        hit=0.45,
        roi=-0.03,
        roi_lo=-0.09,
        roi_hi=0.02,
        clv_close=None,
        clv_close_se=None,
        inc_clv_se=None,
    )
    base = tb.Stats(
        n=600,
        hit=0.5,
        roi=0.0,
        roi_lo=-0.04,
        roi_hi=0.04,
        clv_close=None,
        clv_close_se=None,
        inc_clv_se=None,
    )
    v = tb.compute_verdict(test, base, has_closing_line=False)
    assert "visibility-only" in v and "UNVALIDATED" in v
    assert "alerts" not in v


def test_with_closing_line_strong_clv_can_reach_alerts() -> None:
    # parity guard for the football-style branch: a sport WITH a closing line
    # and incremental CLV > 2 SE + positive ROI is allowed to reach "alerts".
    test = tb.Stats(
        n=600,
        hit=0.5,
        roi=0.04,
        roi_lo=0.0,
        roi_hi=0.08,
        clv_close=0.030,
        clv_close_se=None,
        inc_clv_se=0.005,
    )
    base = tb.Stats(
        n=600,
        hit=0.5,
        roi=0.0,
        roi_lo=-0.04,
        roi_hi=0.04,
        clv_close=0.005,
        clv_close_se=None,
        inc_clv_se=None,
    )
    v = tb.compute_verdict(test, base, has_closing_line=True)
    assert v.startswith("alerts")
