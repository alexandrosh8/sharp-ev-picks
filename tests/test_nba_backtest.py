"""Unit + leakage LOCK for scripts/nba_backtest.py (NBA total-line CLV/value).

The SBR free data ships only a CLOSING moneyline (no opening ML anywhere) plus
OPENING and CLOSING *total* lines that carry no price. So the football recipe
"devig opening ML, bet it, CLV vs closing ML" is impossible here. This script
instead runs the only defensible test the data supports: bet the OPENING total
line at assumed -110 juice, settle Over/Under on real final scores, and measure
line value against the (soft, consensus) CLOSING total line.

These tests pin the pure helpers (no I/O, numpy/stdlib only) and lock the
leakage invariant: the bet DECISION (side + edge + price) must depend only on
the opening line and the TRAIN-fit line->prob map; the closing line may feed the
CLV label and nothing else.
"""

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


nb: Any = _load(_SCRIPTS / "nba_backtest.py", "nba_backtest")


# --- american_to_decimal -----------------------------------------------------
def test_american_to_decimal_negative() -> None:
    # -110 -> 1 + 100/110
    assert math.isclose(nb.american_to_decimal(-110), 1.0 + 100.0 / 110.0, rel_tol=1e-12)


def test_american_to_decimal_positive() -> None:
    assert math.isclose(nb.american_to_decimal(150), 2.5, rel_tol=1e-12)


def test_american_to_decimal_rejects_pushy_values() -> None:
    # |odds| < 100 is impossible American odds -> None (filtered upstream)
    assert nb.american_to_decimal(50) is None
    assert nb.american_to_decimal(0) is None
    assert nb.american_to_decimal(None) is None


# --- total_result (settlement on real final scores) --------------------------
def test_total_result_over() -> None:
    # 110 + 105 = 215 vs line 209.5 -> OVER
    assert nb.total_result(110, 105, 209.5) == "over"


def test_total_result_under() -> None:
    assert nb.total_result(95, 100, 209.5) == "under"


def test_total_result_push_returns_none() -> None:
    # integer line that exactly matches the total is a push -> no settle
    assert nb.total_result(100, 109, 209.0) is None


def test_total_result_rejects_garbage() -> None:
    # scrape garbage (negative / absurd) -> None, never a silent wrong settle
    assert nb.total_result(-1, 100, 209.5) is None
    assert nb.total_result(100, 100, 9999.0) is None


# --- LineOverModel: TRAIN-fit logistic, monotone, in (0,1) -------------------
def test_line_to_over_prob_in_unit_interval() -> None:
    f = nb.LineOverModel.fit([(200.0, "over"), (220.0, "under"), (210.0, "over")])
    p = f.over_prob(210.0)
    assert 0.0 < p < 1.0


def test_line_to_over_prob_monotone_decreasing_in_line() -> None:
    # a higher total line must imply a LOWER probability of going over
    samples = []
    for line, total in [(200, 215), (205, 215), (210, 200), (215, 230), (220, 205)]:
        samples.append((float(line), "over" if total > line else "under"))
    f = nb.LineOverModel.fit(samples)
    assert f.over_prob(200.0) > f.over_prob(230.0)


# --- Stats / CLV aggregation -------------------------------------------------
def test_stats_empty_is_zero() -> None:
    s = nb.Stats.from_bets([])
    assert s.n == 0
    assert s.roi == 0.0
    assert s.clv is None


def test_stats_roi_and_clv_signs() -> None:
    # one winning over bet at -110 (dec 1.9091), clv positive
    b = nb.NBet(won=True, odds=nb.american_to_decimal(-110), edge=0.05, clv=0.02)
    s = nb.Stats.from_bets([b])
    assert s.n == 1
    assert s.roi > 0  # winner at +0.909 units
    assert s.clv is not None and abs(s.clv - 0.02) < 1e-12


# --- LEAKAGE LOCK: closing line feeds CLV label only, never the decision ------
def _train_model() -> Any:
    # training set with a REAL slope: lower lines go over more often than higher
    # lines, so over_prob(line) is strictly monotone and varies across the range
    # (a flat/balanced set would give b=0 and make every CLV identical, which
    # would hide rather than test the closing-line dependence).
    samples = []
    for line in range(195, 225):
        n_over = max(0, 224 - line)  # 29 overs at 195 down to 0 at 224
        n_under = max(0, line - 195)
        samples += [(float(line), "over")] * n_over
        samples += [(float(line), "under")] * n_under
    return nb.LineOverModel.fit(samples)


def _row(open_ou: float, close_ou: float) -> dict[str, Any]:
    return {
        "season": 2015,
        "home_final": "120",
        "away_final": "118",  # total 238 -> OVER any sane line
        "open_over_under": open_ou,
        "close_over_under": close_ou,
        "home_close_ml": -150,
        "away_close_ml": 130,
    }


def test_closing_line_does_not_change_the_bet_decision() -> None:
    model = _train_model()
    base = nb.bets_for([_row(210.0, 209.0)], thr=0.0, model=model)
    moved = nb.bets_for([_row(210.0, 250.0)], thr=0.0, model=model)
    assert len(base) == 1 and len(moved) == 1
    # decision fields identical; only the CLV label may move
    assert base[0].won == moved[0].won
    assert math.isclose(base[0].odds, moved[0].odds, rel_tol=1e-12)
    assert math.isclose(base[0].edge, moved[0].edge, rel_tol=1e-12)
    assert base[0].clv != moved[0].clv  # closing change DID move the label


def test_garbage_total_rows_are_dropped_not_settled_wrong() -> None:
    model = _train_model()
    bad = {
        "season": 2015,
        "home_final": "120",
        "away_final": "118",
        "open_over_under": 1955.5,  # scrape garbage
        "close_over_under": 209.0,
        "home_close_ml": -150,
        "away_close_ml": 130,
    }
    assert nb.bets_for([bad], thr=0.0, model=model) == []
