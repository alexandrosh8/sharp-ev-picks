"""Walk-forward backtest engine: settlement, ROI/CLV stats, no-leakage."""

from datetime import date, timedelta

import pytest

from app.backtesting.walkforward import (
    Bet,
    ThresholdStats,
    bankroll_path_from_bets,
    run_walkforward,
)
from app.ingestion.football_data import MatchRow


def row(
    d: date, home: str, away: str, hg: int, ag: int, oh: float, od: float, oa: float
) -> MatchRow:
    res = "H" if hg > ag else ("A" if ag > hg else "D")
    return MatchRow(
        match_date=d,
        home_team=home,
        away_team=away,
        home_goals=hg,
        away_goals=ag,
        result=res,
        b365_home=oh,
        b365_draw=od,
        b365_away=oa,
        pinnacle_closing_home=oh,
        pinnacle_closing_draw=od,
        pinnacle_closing_away=oa,
    )


def test_threshold_stats_settlement_exact() -> None:
    # one winning home bet @2.0 and one losing away bet @3.0, both edge 0.10
    bets = [
        Bet(date(2026, 1, 1), "A", "B", "H", 0.6, 0.5, 0.10, 0.2, 2.0, True, 0.05),
        Bet(date(2026, 1, 2), "C", "D", "A", 0.4, 0.3, 0.10, 0.2, 3.0, False, -0.05),
    ]
    s = ThresholdStats.from_bets(0.05, bets)
    assert s.n == 2
    assert s.hit_rate == pytest.approx(0.5)
    # profit = (2.0-1) win + (-1) loss = 0.0 over 2 units => ROI 0
    assert s.profit_units == pytest.approx(0.0)
    assert s.roi == pytest.approx(0.0)
    assert s.avg_clv == pytest.approx(0.0)
    assert s.pct_beat_close == pytest.approx(0.5)


def test_empty_threshold_is_zeroed() -> None:
    s = ThresholdStats.from_bets(0.5, [])
    assert s.n == 0
    assert s.roi == 0.0
    assert s.avg_clv is None


def test_bankroll_path_compounds_and_tracks_drawdown() -> None:
    bets = [
        Bet(date(2026, 1, 1), "A", "B", "H", 0.6, 0.5, 0.10, 0.2, 2.0, True, None),
        Bet(date(2026, 1, 2), "C", "D", "H", 0.6, 0.5, 0.10, 0.2, 2.0, False, None),
    ]
    final, dd = bankroll_path_from_bets(bets, fractional_kelly=0.25, cap=0.02)
    assert 0.0 < final < 2.0
    assert 0.0 <= dd <= 1.0


def test_walkforward_no_leakage_and_settles() -> None:
    # Synthetic league where home always wins 2-0; a model that knows this
    # should still only be fit on PRIOR matches. We pass a fit_fn that prices
    # home at 0.8 always — bets home when edge exists.
    base = date(2024, 1, 1)
    teams = [f"T{i}" for i in range(6)]
    matches = []
    for w in range(220):  # enough so the >=100-match history window fills
        h, a = teams[w % 6], teams[(w + 1) % 6]
        matches.append(row(base + timedelta(days=w * 2), h, a, 2, 0, 1.5, 4.0, 6.0))

    def fit_fn(history, as_of):  # noqa: ANN001, ANN202
        # no leakage: the fit window must never include the as_of match or later
        assert all(h.match_date < as_of for h in history)
        return lambda home, away: (0.80, 0.13, 0.07)

    report = run_walkforward(
        matches, fit_fn, warmup_matches=120, training_window_days=600, refit_every_days=1
    )
    assert report.n_eval_matches > 0
    # home @1.5 with model 0.80: devig fair ~0.62, edge ~0.18 -> bets, and home wins
    s = report.at_threshold(0.05)
    assert s.n > 0
    assert s.hit_rate == pytest.approx(1.0)  # home always wins in this synthetic world
