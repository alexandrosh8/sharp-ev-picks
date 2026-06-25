"""Pure-helper tests for the model-agnostic penaltyblog goal-model bake-off.

Only the leakage-safe pure helpers are TDD'd here (no network, no model fit):
  - grid_to_picks: turn one fixture's model fair probs + a football-data.co.uk
    closing-odds row into per-selection observations (fair_prob, fill_odds, won,
    CLV vs devig(Pinnacle close)).
  - roi: per-bet realized ROI over a list of picks.
Devig, clv_log and calibration_report are already covered by their own suites.
"""

import math

import pytest

from app.probabilities.devig import DevigMethod
from scripts.penaltyblog_bakeoff import ModelGrid, ModelPick, grid_to_picks, roi


def _row(**over: str) -> dict[str, str]:
    """A football-data.co.uk row with full 1x2 + ou25 pre-match + closing cols."""
    base = {
        "FTR": "H",
        "FTHG": "2",
        "FTAG": "0",
        # Pinnacle PRE-MATCH (fill side)
        "PSH": "2.00",
        "PSD": "3.50",
        "PSA": "4.00",
        "P>2.5": "1.90",
        "P<2.5": "1.90",
        # Pinnacle CLOSING (CLV + fair-close side)
        "PSCH": "2.10",
        "PSCD": "3.40",
        "PSCA": "3.90",
        "PC>2.5": "1.95",
        "PC<2.5": "1.85",
    }
    base.update(over)
    return base


def test_grid_to_picks_emits_one_pick_per_selection_for_each_market() -> None:
    grid = ModelGrid(home_win=0.55, draw=0.25, away_win=0.20, over25=0.50, under25=0.50)
    picks = grid_to_picks(_row(), grid, DevigMethod.POWER, markets=("1x2", "ou25"))
    # 3 (1x2) + 2 (ou25) selections
    assert len(picks) == 5
    assert {p.market for p in picks} == {"1x2", "ou25"}
    assert all(isinstance(p, ModelPick) for p in picks)


def test_grid_to_picks_settles_winner_and_loser_correctly() -> None:
    # FTR=H, FTHG+FTAG = 2 -> home wins, under 2.5 wins
    grid = ModelGrid(home_win=0.55, draw=0.25, away_win=0.20, over25=0.40, under25=0.60)
    picks = grid_to_picks(_row(), grid, DevigMethod.POWER, markets=("1x2", "ou25"))
    by_sel = {(p.market, p.selection): p for p in picks}
    assert by_sel[("1x2", "home")].won is True
    assert by_sel[("1x2", "draw")].won is False
    assert by_sel[("1x2", "away")].won is False
    assert by_sel[("ou25", "over")].won is False
    assert by_sel[("ou25", "under")].won is True


def test_grid_to_picks_fill_odds_are_pinnacle_prematch() -> None:
    grid = ModelGrid(home_win=0.55, draw=0.25, away_win=0.20, over25=0.50, under25=0.50)
    picks = grid_to_picks(_row(), grid, DevigMethod.POWER, markets=("1x2",))
    home = next(p for p in picks if p.selection == "home")
    assert home.fill_odds == pytest.approx(2.00)
    assert home.model_prob == pytest.approx(0.55)


def test_grid_to_picks_clv_is_log_ratio_vs_devigged_pinnacle_close() -> None:
    grid = ModelGrid(home_win=0.55, draw=0.25, away_win=0.20, over25=0.50, under25=0.50)
    picks = grid_to_picks(_row(), grid, DevigMethod.MULTIPLICATIVE, markets=("1x2",))
    home = next(p for p in picks if p.selection == "home")
    # devig(PSCH,PSCD,PSCA) multiplicative -> close fair prob for home
    q = [1 / 2.10, 1 / 3.40, 1 / 3.90]
    close_home = q[0] / sum(q)
    assert home.clv_pinn == pytest.approx(math.log(2.00 * close_home), abs=1e-9)


def test_grid_to_picks_skips_market_with_missing_prematch_or_close() -> None:
    grid = ModelGrid(home_win=0.55, draw=0.25, away_win=0.20, over25=0.50, under25=0.50)
    # blank a pre-match Pinnacle price -> the whole 1x2 market is unusable
    picks = grid_to_picks(_row(PSH=""), grid, DevigMethod.POWER, markets=("1x2", "ou25"))
    assert {p.market for p in picks} == {"ou25"}


def test_grid_to_picks_no_close_yields_null_clv_but_keeps_calibration() -> None:
    grid = ModelGrid(home_win=0.55, draw=0.25, away_win=0.20, over25=0.50, under25=0.50)
    picks = grid_to_picks(
        _row(PSCH="", PSCD="", PSCA=""), grid, DevigMethod.POWER, markets=("1x2",)
    )
    assert len(picks) == 3  # still settled + priced for calibration/ROI
    assert all(p.clv_pinn is None for p in picks)
    assert all(p.won is not None for p in picks)


def test_roi_realized_profit_per_bet() -> None:
    # one win at 2.0 (+1.0), one loss (-1.0) -> ROI 0.0 over 2 bets
    won = ModelPick("1x2", "home", model_prob=0.5, fill_odds=2.0, won=True, clv_pinn=None)
    lost = ModelPick("1x2", "away", model_prob=0.5, fill_odds=2.0, won=False, clv_pinn=None)
    assert roi([won, lost]) == pytest.approx(0.0)
    assert roi([won]) == pytest.approx(1.0)  # +1.0 / 1
    assert roi([lost]) == pytest.approx(-1.0)


def test_roi_empty_is_zero() -> None:
    assert roi([]) == 0.0
