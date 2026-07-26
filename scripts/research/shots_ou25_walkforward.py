"""Walk-forward OOS eval: Wheatcroft GAP shots/corners OU2.5 screen vs a
goals-only baseline, on free football-data.co.uk history (major leagues
where HS/AS/HST/AST/HC/AC populate).

Leakage discipline mirrors app/backtesting/walkforward.py (strictly-past
fits, chronological order); the OU2.5 log-loss/RPS comparison itself runs
through the pure predict-then-update harness in app.models.football_shots
because the 1X2/Bet365 bet loop in run_walkforward cannot express a
probability-scoring eval. Read-only GETs; no odds are used as features.

Usage: uv run python scripts/research/shots_ou25_walkforward.py
"""

import asyncio
import math

import httpx

from app.ingestion.football_data import MatchRow, fetch_season_csv, parse_season_csv
from app.models.football_shots import (
    MatchStats,
    ShotsPolicy,
    evaluate_walkforward_ou25,
)

LEAGUES = ["E0", "D1", "SP1", "I1", "F1"]
SEASONS = ["1819", "1920", "2021", "2122", "2223", "2324", "2425"]
WARMUP = 380  # one full season per league before scoring


def to_stats(rows: list[MatchRow]) -> list[MatchStats]:
    return [
        MatchStats(
            home_team=r.home_team,
            away_team=r.away_team,
            home_goals=r.home_goals,
            away_goals=r.away_goals,
            home_shots=r.home_shots,
            away_shots=r.away_shots,
            home_shots_on_target=r.home_shots_on_target,
            away_shots_on_target=r.away_shots_on_target,
            home_corners=r.home_corners,
            away_corners=r.away_corners,
        )
        for r in rows
    ]


async def load_league(client: httpx.AsyncClient, league: str) -> list[MatchRow]:
    rows: list[MatchRow] = []
    for season in SEASONS:
        try:
            text = await fetch_season_csv(client, league, season)
        except httpx.HTTPError as exc:
            print(f"  {league} {season}: fetch failed ({type(exc).__name__})")
            continue
        rows.extend(parse_season_csv(text))
    rows.sort(key=lambda r: r.match_date)
    return rows


async def main() -> None:
    policy = ShotsPolicy()
    print(f"policy: {policy}")
    totals = {"n": 0, "s_ll": 0.0, "s_br": 0.0, "b_ll": 0.0, "b_br": 0.0}
    async with httpx.AsyncClient() as client:
        for league in LEAGUES:
            rows = await load_league(client, league)
            stats = to_stats(rows)
            n_with_shots = sum(1 for s in stats if s.home_shots_on_target is not None)
            res = evaluate_walkforward_ou25(stats, policy, warmup=WARMUP)
            if res.n_evaluated == 0:
                print(f"{league}: no evaluable matches (rows={len(rows)})")
                continue
            print(
                f"{league}: rows={len(rows)} with_sot={n_with_shots} "
                f"n_eval={res.n_evaluated} | shots ll={res.shots_log_loss:.4f} "
                f"brier={res.shots_brier:.4f} | goals-baseline "
                f"ll={res.baseline_log_loss:.4f} brier={res.baseline_brier:.4f} "
                f"| beats_baseline={res.beats_baseline}"
            )
            totals["n"] += res.n_evaluated
            totals["s_ll"] += res.shots_log_loss * res.n_evaluated
            totals["s_br"] += res.shots_brier * res.n_evaluated
            totals["b_ll"] += res.baseline_log_loss * res.n_evaluated
            totals["b_br"] += res.baseline_brier * res.n_evaluated
    if totals["n"]:
        n = totals["n"]
        print(
            f"POOLED (n={n}): shots ll={totals['s_ll'] / n:.4f} "
            f"brier={totals['s_br'] / n:.4f} | baseline ll={totals['b_ll'] / n:.4f} "
            f"brier={totals['b_br'] / n:.4f} | "
            f"delta_ll={(totals['s_ll'] - totals['b_ll']) / n:+.4f} "
            f"(negative = shots screen better)"
        )
        assert math.isfinite(totals["s_ll"])


if __name__ == "__main__":
    asyncio.run(main())
