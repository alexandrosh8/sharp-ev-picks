"""LIVE value picks — the solid pick finder (backtested positive CLV).

Scrapes a league's per-bookmaker 1X2 odds via OddsHarvester, treats the
sharpest book (Pinnacle by preference, else lowest-overround) as fair value,
and flags every selection where another book's price beats that fair value by
>= the edge threshold. Ranks the picks. No goals model — this is the strategy
that beats the closing line (docs/backtesting/value-findings.md).

    uv run python scripts/value_picks.py                       # Brazil Serie A
    uv run python scripts/value_picks.py --league world-cup --min-edge 0.015
    uv run python scripts/value_picks.py --league argentina-primera-division

Caveat: edges assume you can line-shop across books and bet promptly; soft
books limit/close winning accounts, so real CLV is lower than the backtest's
best-price ideal. Decision-support only — nothing here places bets.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass

from app.edge.value import find_value_bets


@dataclass
class LivePick:
    match_date: str
    match: str
    selection: str
    book: str
    odds: float
    sharp_book: str
    fair_prob: float
    edge: float
    ev: float


async def scrape(league: str, max_pages: int) -> list[dict]:
    from oddsharvester.core.scraper_app import run_scraper
    from oddsharvester.utils.command_enum import CommandEnum

    res = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        leagues=[league],
        markets=["1x2"],
        headless=True,
        max_pages=max_pages,
    )
    return getattr(res, "success", []) or []


def _f(x: object) -> float | None:
    try:
        v = float(str(x).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--league", default="brazil-serie-a", help="oddsportal slug")
    p.add_argument("--min-edge", type=float, default=0.015)
    p.add_argument("--min-odds", type=float, default=1.30, help="ignore ultra-short prices")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--max-pages", type=int, default=2)
    args = p.parse_args()
    logging.basicConfig(level="ERROR")

    print(f"\nLIVE VALUE PICKS — {args.league} (min edge {args.min_edge})")
    print("Fair = sharpest book; pick = another book beating it. Backtested +CLV.\n")
    matches = await scrape(args.league, args.max_pages)
    print(f"scraped {len(matches)} matches with multi-book odds\n")

    picks: list[LivePick] = []
    for m in matches:
        home = (m.get("home_team") or "").strip()
        away = (m.get("away_team") or "").strip()
        if not home or not away:
            continue
        # degenerate names would collapse the 3-way market into 2 keys
        if home == away or "draw" in (home.lower(), away.lower()):
            continue
        prices: dict[str, dict[str, float]] = {home: {}, "Draw": {}, away: {}}
        for entry in m.get("1x2_market") or []:
            if not isinstance(entry, dict):
                continue
            book = str(entry.get("bookmaker_name") or "?")
            for key, sel in (("1", home), ("X", "Draw"), ("2", away)):
                odds = _f(entry.get(key))
                if odds is not None:
                    prices[sel][book] = odds
        if any(len(v) < 1 for v in prices.values()):
            continue
        for v in find_value_bets(prices, min_edge=args.min_edge, min_odds=args.min_odds):
            picks.append(
                LivePick(
                    match_date=str(m.get("match_date", "")),
                    match=f"{home} v {away}",
                    selection=v.selection,
                    book=v.best_book,
                    odds=v.best_odds,
                    sharp_book=v.sharp_book,
                    fair_prob=v.sharp_fair_prob,
                    edge=v.edge,
                    ev=v.ev,
                )
            )

    picks.sort(key=lambda x: x.edge, reverse=True)
    print(f"{len(picks)} value picks found.  TOP {min(args.top, len(picks))}:\n")
    print(
        f"{'edge':>6} {'EV':>6} {'odds':>5} {'fair':>6}  "
        f"bet @ book | selection | match | (fair via)"
    )
    print("-" * 92)
    for p_ in picks[: args.top]:
        print(
            f"{p_.edge:>+6.3f} {p_.ev:>+6.3f} {p_.odds:>5.2f} {p_.fair_prob:>6.3f}  "
            f"{p_.book} | {p_.selection} | {p_.match} | (fair via {p_.sharp_book})"
        )
    print("\nManual review required. This system does not place bets.")
    print(
        "Backtest: edge>=0.015 -> +9.25% ROI, CLV +0.043 (conclusive), beats close 77% "
        "(EPL+5 leagues, 5 seasons). Real CLV lower — soft books limit winners."
    )


if __name__ == "__main__":
    asyncio.run(main())
