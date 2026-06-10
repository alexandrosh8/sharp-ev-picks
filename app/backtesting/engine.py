"""Backtest evaluation primitives. Pure module.

Full walk-forward orchestration (data slicing, model refits) arrives with the
first trained model (roadmap phase 3); the settlement math below is final:
sequential bankroll path with the SAME stake caps as live, ROI on turnover,
and max drawdown.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SettledPick:
    stake_fraction: float  # of bankroll at bet time
    decimal_odds: float
    won: bool | None  # None = void/push (stake returned)


@dataclass(frozen=True)
class BacktestSummary:
    n_picks: int
    roi: float  # profit / total turnover
    final_bankroll: float  # starting bankroll = 1.0
    max_drawdown: float  # peak-to-trough fraction


def bankroll_path(picks: Sequence[SettledPick], starting: float = 1.0) -> list[float]:
    """Sequential compounding path; stake is a fraction of CURRENT bankroll."""
    path = [starting]
    bankroll = starting
    for pick in picks:
        if not 0.0 <= pick.stake_fraction <= 1.0:
            raise ValueError(f"stake fraction out of range: {pick.stake_fraction}")
        stake = bankroll * pick.stake_fraction
        if pick.won is None:
            pass  # void: stake returned
        elif pick.won:
            bankroll += stake * (pick.decimal_odds - 1.0)
        else:
            bankroll -= stake
        path.append(bankroll)
    return path


def max_drawdown(path: Sequence[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for value in path:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def summarize(picks: Sequence[SettledPick], starting: float = 1.0) -> BacktestSummary:
    path = bankroll_path(picks, starting=starting)
    turnover = 0.0
    bankroll = starting
    profit = 0.0
    for pick in picks:
        stake = bankroll * pick.stake_fraction
        if pick.won is None:
            delta = 0.0
        elif pick.won:
            delta = stake * (pick.decimal_odds - 1.0)
        else:
            delta = -stake
        turnover += stake if pick.won is not None else 0.0
        profit += delta
        bankroll += delta
    roi = profit / turnover if turnover > 0 else 0.0
    return BacktestSummary(
        n_picks=len(picks),
        roi=roi,
        final_bankroll=path[-1],
        max_drawdown=max_drawdown(path),
    )
