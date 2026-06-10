"""Backtest the Dixon-Coles value strategy on real historical results.

Walk-forward (no leakage): fit on results strictly before each match, bet the
1X2 market at Bet365 pre-match odds when the model shows edge, settle on the
real result, measure ROI and CLV vs the Pinnacle close. Sweeps edge thresholds.

    uv run python scripts/backtest.py                       # E0, 5 seasons
    uv run python scripts/backtest.py --league SP1 --seasons 2223,2324,2425,2526

Honest by design: prints REAL numbers, including when the strategy does NOT
beat the closing line. This is decision-support validation, not a profit claim.

Known sample bias: newly promoted teams are unpriceable until they accumulate
history inside the rolling window, so early-season matches involving them are
skipped — the evaluated sample slightly over-represents established teams.
"""

import argparse
import asyncio
import math
from datetime import date

import httpx

from app.backtesting.walkforward import bankroll_path_from_bets, run_walkforward
from app.ingestion.football_data import MatchRow, fetch_season_csv, parse_season_csv


def make_fit_fn(xi: float):  # noqa: ANN201 - returns a closure
    """Returns a FitFn that fits penaltyblog Dixon-Coles and yields a
    (home, away) -> (p_home, p_draw, p_away) pricer."""
    import math as _math

    def fit_fn(history, as_of: date):  # noqa: ANN001, ANN202
        from penaltyblog.models import DixonColesGoalModel

        weights = [_math.exp(-xi * (as_of - h.match_date).days) for h in history]
        model = DixonColesGoalModel(
            goals_home=[h.home_goals for h in history],
            goals_away=[h.away_goals for h in history],
            teams_home=[h.home_team for h in history],
            teams_away=[h.away_team for h in history],
            weights=weights,
        )
        model.fit(minimizer_options={"maxiter": 1000})
        trained = {t for h in history for t in (h.home_team, h.away_team)}

        def priced(home: str, away: str):  # noqa: ANN202
            if home not in trained or away not in trained:
                return None
            try:
                grid = model.predict(home, away)
            except ValueError:
                return None
            return (float(grid.home_win), float(grid.draw), float(grid.away_win))

        return priced

    return fit_fn


async def load(league: str, seasons: list[str]) -> list[MatchRow]:
    rows: list[MatchRow] = []
    async with httpx.AsyncClient() as client:
        for s in seasons:
            try:
                rows.extend(parse_season_csv(await fetch_season_csv(client, league, s)))
            except httpx.HTTPError as exc:
                print(f"  WARN {league}/{s}: {type(exc).__name__}")
    return rows


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", default="E0")
    parser.add_argument("--seasons", default="2122,2223,2324,2425,2526")
    parser.add_argument("--xi", type=float, default=0.0018, help="time-decay rate/day")
    args = parser.parse_args()
    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]

    print(f"\nBACKTEST — {args.league}, seasons {seasons}, Dixon-Coles (penaltyblog), xi={args.xi}")
    print("Bet at Bet365 pre-match; CLV vs Pinnacle close; flat 1u stakes.\n")
    matches = await load(args.league, seasons)
    print(f"loaded {len(matches)} matches; running walk-forward (weekly refit)...")

    report = run_walkforward(matches, make_fit_fn(args.xi))
    print(f"evaluated {report.n_eval_matches} matches, priced {report.n_priced}\n")

    print(
        f"{'min_edge':>9} | {'bets':>5} | {'hit%':>6} | {'ROI%':>7} | "
        f"{'avgCLV':>7} | {'beat_close%':>11} | {'profit_u':>9}"
    )
    print("-" * 76)
    for thr in (0.02, 0.03, 0.05, 0.08, 0.10, 0.15):
        s = report.at_threshold(thr)
        clv = f"{s.avg_clv:+.4f}" if s.avg_clv is not None else "   n/a"
        beat = f"{s.pct_beat_close * 100:.1f}" if s.pct_beat_close is not None else "n/a"
        print(
            f"{thr:>9.2f} | {s.n:>5} | {s.hit_rate * 100:>5.1f} | "
            f"{s.roi * 100:>+6.2f} | {clv:>7} | {beat:>11} | {s.profit_units:>+9.1f}"
        )

    chosen = [b for b in report.bets if b.edge >= 0.05 and b.ev > 0]
    final, dd = bankroll_path_from_bets(chosen)
    print(
        f"\nFractional-Kelly (0.25x, 2% cap) bankroll over edge>=0.05 bets: "
        f"x{final:.3f} (max drawdown {dd * 100:.1f}%), n={len(chosen)}"
    )

    clv_bets = [b for b in report.bets if b.edge >= 0.05 and b.clv is not None]
    if clv_bets:
        mean_clv = sum(b.clv for b in clv_bets) / len(clv_bets)  # type: ignore[misc]
        var = sum((b.clv - mean_clv) ** 2 for b in clv_bets) / len(clv_bets)  # type: ignore[operator]
        se = math.sqrt(var) / math.sqrt(len(clv_bets))
        if mean_clv - 2 * se > 0:
            verdict = "POSITIVE — real edge signal"
        elif abs(mean_clv) < 2 * se:
            verdict = "NOT distinguishable from zero (no proven edge)"
        else:
            verdict = "NEGATIVE — the market closes against these picks"
        print(
            f"\nCLV verdict (edge>=0.05, n={len(clv_bets)}): mean {mean_clv:+.4f} "
            f"+/-{2 * se:.4f} (95%) -> {verdict}"
        )
    print("\nManual review required. This system does not place bets.")


if __name__ == "__main__":
    asyncio.run(main())
