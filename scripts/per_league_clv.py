"""Per-LEAGUE CLV/ROI breakdown of the VALUE strategy — to rank leagues by
selection skill and inform the premium-tier league allowlist.

Reuses bets_for()/Stats from value_backtest.py (so the per-league numbers cannot
diverge from the pooled backtest). For each league code it fetches all available
seasons, runs the value strategy at a fixed edge threshold, and reports CLV (vs
Pinnacle close) + ROI. CLV is the stable per-league signal; ROI at these sample
sizes is noisy. Ranked by CLV descending.

    uv run python scripts/per_league_clv.py
    uv run python scripts/per_league_clv.py --thr 0.02 --min-odds 1.6

Decision-support only — nothing here places bets.
"""

import argparse
import asyncio
import csv
import io
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(__file__))  # import the sibling backtest module
from value_backtest import Stats, bets_for  # noqa: E402

from app.ingestion.football_data import LEAGUES, fetch_season_csv  # noqa: E402
from app.probabilities.devig import DevigMethod  # noqa: E402


async def _league_stats(
    client: httpx.AsyncClient,
    league: str,
    seasons: list[str],
    thr: float,
    devig_method: DevigMethod,
    markets: tuple[str, ...],
    min_odds: float,
) -> Stats:
    rows: list[dict] = []
    for s in seasons:
        for _ in range(4):
            try:
                txt = await fetch_season_csv(client, league, s)
                rows.extend(csv.DictReader(io.StringIO(txt)))
                break
            except httpx.HTTPError:
                await asyncio.sleep(1.5)
        await asyncio.sleep(0.2)
    return Stats.from_bets(bets_for(rows, thr, devig_method, markets, min_odds))


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--leagues", default=",".join(LEAGUES))
    p.add_argument("--seasons", default="1920,2021,2122,2223,2324,2425,2526")
    p.add_argument("--thr", type=float, default=0.02, help="edge threshold (lower = more bets)")
    p.add_argument("--markets", default="1x2,ou25")
    p.add_argument("--min-odds", type=float, default=1.6)
    p.add_argument("--devig", default="power")
    args = p.parse_args()

    leagues = [x.strip() for x in args.leagues.split(",") if x.strip()]
    seasons = [x.strip() for x in args.seasons.split(",") if x.strip()]
    markets = tuple(x.strip() for x in args.markets.split(",") if x.strip())
    devig_method = DevigMethod(args.devig)

    print(
        f"PER-LEAGUE CLV — value strategy, thr {args.thr}, markets {markets}, "
        f"min_odds {args.min_odds}, seasons {seasons[0]}..{seasons[-1]} (all, CLV is the signal)"
    )
    rows: list[tuple[str, Stats]] = []
    async with httpx.AsyncClient() as client:
        for lg in leagues:
            rows.append(
                (
                    lg,
                    await _league_stats(
                        client, lg, seasons, args.thr, devig_method, markets, args.min_odds
                    ),
                )
            )

    # Rank by CLV (vs Pinnacle close) descending — the stable selection signal.
    rows.sort(key=lambda kv: kv[1].clv_pinn if kv[1].clv_pinn is not None else -9.9, reverse=True)
    print(f"\n{'league':<26} {'n':>4} {'hit':>6} {'ROI':>8} {'CLVpinn (±2SE)':>20} {'beat%':>6}")
    for lg, s in rows:
        name = LEAGUES.get(lg, lg)
        if s.n == 0:
            print(f"{name:<26} {0:>4}  (no bets)")
            continue
        clv = (
            f"{s.clv_pinn:+.4f}±{2 * (s.clv_pinn_se or 0):.4f}" if s.clv_pinn is not None else "n/a"
        )
        beat = f"{(s.beat_pinn or 0) * 100:.0f}%" if s.beat_pinn is not None else "n/a"
        print(f"{name:<26} {s.n:>4} {s.hit * 100:>5.1f}% {s.roi * 100:>+7.2f}% {clv:>20} {beat:>6}")


if __name__ == "__main__":
    asyncio.run(main())
