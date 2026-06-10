"""Backtest settlement math: bankroll path, ROI, drawdown — exact values."""

import pytest

from app.backtesting.engine import SettledPick, bankroll_path, max_drawdown, summarize


def test_win_then_loss_path() -> None:
    picks = [
        SettledPick(stake_fraction=0.02, decimal_odds=2.0, won=True),
        SettledPick(stake_fraction=0.02, decimal_odds=2.0, won=False),
    ]
    path = bankroll_path(picks)
    # 1.0 -> +0.02 -> 1.02; stake 0.0204 lost -> 0.9996
    assert path == pytest.approx([1.0, 1.02, 1.02 - 1.02 * 0.02])


def test_void_returns_stake() -> None:
    path = bankroll_path([SettledPick(stake_fraction=0.05, decimal_odds=3.0, won=None)])
    assert path == pytest.approx([1.0, 1.0])


def test_max_drawdown_exact() -> None:
    # peak 1.05, trough 0.95 -> (1.05-0.95)/1.05
    assert max_drawdown([1.0, 1.05, 0.95, 1.10]) == pytest.approx(0.10 / 1.05, abs=1e-12)


def test_max_drawdown_monotonic_growth_is_zero() -> None:
    assert max_drawdown([1.0, 1.1, 1.2]) == 0.0


def test_summarize_roi_and_drawdown() -> None:
    picks = [
        SettledPick(stake_fraction=0.10, decimal_odds=2.0, won=True),
        SettledPick(stake_fraction=0.10, decimal_odds=2.0, won=False),
        SettledPick(stake_fraction=0.10, decimal_odds=2.0, won=None),
    ]
    summary = summarize(picks)
    # stake1 = 0.10 won +0.10 -> 1.10; stake2 = 0.11 lost -> 0.99; void no-op
    assert summary.n_picks == 3
    assert summary.final_bankroll == pytest.approx(0.99)
    # turnover = 0.10 + 0.11 (void excluded); profit = 0.10 - 0.11 = -0.01
    assert summary.roi == pytest.approx(-0.01 / 0.21)
    assert summary.max_drawdown == pytest.approx(0.11 / 1.10)


def test_invalid_stake_fraction_raises() -> None:
    with pytest.raises(ValueError):
        bankroll_path([SettledPick(stake_fraction=1.5, decimal_odds=2.0, won=True)])
