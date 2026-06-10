"""World Cup 2026 top picks — Dixon-Coles on international results vs live odds.

Fits penaltyblog Dixon-Coles on martj42 international results (neutral-venue
aware), scrapes live World Cup 1X2 odds via OddsHarvester, devigs, computes
model edge per selection, and ranks the top picks.

    uv run python scripts/world_cup_picks.py
    uv run python scripts/world_cup_picks.py --top 15 --min-edge 0.0

HONESTY (read this): our walk-forward backtest shows a goals-only Dixon-Coles
model does NOT beat the closing line (negative CLV) even in club leagues with
far more data. International/tournament football has thinner per-team data, a
brand-new 48-team format, and NO injury/lineup input in this model. Treat
these as a model-vs-market SCREEN for manual review, not proven +EV edges.
Decision-support only — nothing here places bets.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from app.ingestion.base import EventDirectory
from app.ingestion.international_results import (
    fetch_results_csv,
    parse_fixtures,
    parse_results,
    to_match_rows,
)
from app.models.football_dc import DixonColesFootballModel
from app.probabilities.devig import DevigMethod, devig
from app.schemas.base import Market

# oddsportal name (normalized) -> martj42 dataset name
WC_ALIASES = {
    "usa": "United States",
    "south korea": "South Korea",
    "ivory coast": "Ivory Coast",
    "iran": "Iran",
    "cape verde": "Cape Verde",
    "dr congo": "DR Congo",
}


@dataclass
class WCPick:
    match_date: str
    home: str
    away: str
    selection: str
    bookmaker: str
    odds: float
    model_prob: float
    fair_prob: float
    edge: float
    ev: float
    neutral: bool


async def scrape_wc(markets: tuple[str, ...] = ("1x2",)) -> list[dict]:
    from oddsharvester.core.scraper_app import run_scraper
    from oddsharvester.utils.command_enum import CommandEnum

    res = await run_scraper(
        command=CommandEnum.UPCOMING_MATCHES,
        sport="football",
        leagues=["world-cup"],
        markets=list(markets),
        headless=True,
        max_pages=2,
    )
    return getattr(res, "success", []) or []


def _norm(s: str) -> str:
    import re

    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-edge", type=float, default=0.03)
    parser.add_argument("--since", default="2015-01-01", help="train on results on/after")
    parser.add_argument("--xi", type=float, default=0.0006, help="decay/day (intl: gentle)")
    args = parser.parse_args()

    logging.basicConfig(level="ERROR")
    print(__doc__.split("\n\n")[2])  # print the HONESTY paragraph up front
    print()

    directory = EventDirectory()
    async with httpx.AsyncClient() as client:
        text = await fetch_results_csv(client)
    since = datetime.strptime(args.since, "%Y-%m-%d").date()
    results = [m for m in parse_results(text) if m.match_date >= since]
    fixtures = parse_fixtures(text, on_or_after=date(2026, 6, 10))
    fixture_neutral = {(_norm(f.home_team), _norm(f.away_team)): f.neutral for f in fixtures}
    print(f"[1] trained on {len(results)} international matches since {since}")
    print(f"[2] {len(fixtures)} World Cup 2026 fixtures in the schedule")

    rows, neutral = to_match_rows(results)
    model = DixonColesFootballModel(directory, xi=args.xi, confidence=0.60, aliases=WC_ALIASES)
    await asyncio.to_thread(model.fit, rows, datetime.now(tz=UTC).date(), neutral)
    print(f"[3] Dixon-Coles fitted on {len(model._trained)} national teams (neutral-aware)\n")

    print("[4] scraping live World Cup odds (oddsportal via OddsHarvester)...")
    matches = await scrape_wc()
    print(f"    {len(matches)} matches with odds scraped\n")

    picks: list[WCPick] = []
    unresolved = 0
    for m in matches:
        home, away = (m.get("home_team") or "").strip(), (m.get("away_team") or "").strip()
        if not home or not away:
            continue
        if model.resolve_team(home) is None or model.resolve_team(away) is None:
            unresolved += 1
            continue
        neutral_flag = fixture_neutral.get((_norm(home), _norm(away)), True)
        preds = {
            p.selection: p.probability
            for p in model.predict_match(home, away, neutral=neutral_flag)
            if p.market is Market.H2H
        }
        if not preds:
            continue
        for entry in m.get("1x2_market") or []:
            if not isinstance(entry, dict):
                continue
            book = str(entry.get("bookmaker_name") or "?")
            triples = [
                (entry.get("1"), home),
                (entry.get("X"), "Draw"),
                (entry.get("2"), away),
            ]
            odds_list = [_f(o) for o, _ in triples]
            if any(o is None for o in odds_list):
                continue
            fair = devig([o for o in odds_list], method=DevigMethod.POWER)  # type: ignore[misc]
            for (raw_odds, sel_name), fair_p in zip(triples, fair, strict=True):
                odds = _f(raw_odds)
                model_p = preds.get(sel_name)
                if odds is None or model_p is None:
                    continue
                edge = model_p - fair_p
                ev = model_p * (odds - 1.0) - (1.0 - model_p)
                if edge >= args.min_edge and ev > 0:
                    picks.append(
                        WCPick(
                            match_date=str(m.get("match_date", "")),
                            home=home,
                            away=away,
                            selection=sel_name,
                            bookmaker=book,
                            odds=odds,
                            model_prob=model_p,
                            fair_prob=fair_p,
                            edge=edge,
                            ev=ev,
                            neutral=neutral_flag,
                        )
                    )

    picks.sort(key=lambda p: p.edge, reverse=True)
    print(
        f"[5] {unresolved} matches unresolved to trained teams; "
        f"{len(picks)} model-vs-market screens (edge>={args.min_edge})\n"
    )
    print(f"TOP {min(args.top, len(picks))} PICKS (ranked by model edge — SCREEN ONLY):\n")
    print(f"{'edge':>6} {'EV':>6} {'odds':>5} {'model':>6} {'fair':>6}  selection / match / book")
    print("-" * 88)
    for p in picks[: args.top]:
        venue = "" if p.neutral else " (host home adv.)"
        print(
            f"{p.edge:>+6.3f} {p.ev:>+6.3f} {p.odds:>5.2f} {p.model_prob:>6.3f} "
            f"{p.fair_prob:>6.3f}  {p.selection} | {p.home} v {p.away}{venue} | {p.bookmaker}"
        )
    print("\nManual review required. This system does not place bets.")
    print(
        "Reminder: backtest shows this model does not beat the closing line — "
        "screens only, no proven edge. Injuries/lineups are NOT modeled."
    )


def _f(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        v = float(str(raw).strip())
    except ValueError:
        return None
    return v if v > 1.0 else None


if __name__ == "__main__":
    asyncio.run(main())
