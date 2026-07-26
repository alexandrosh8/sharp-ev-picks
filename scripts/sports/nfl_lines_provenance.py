"""Cross-check nflverse consensus lines vs Arcadia-captured Pinnacle closes.

READ-ONLY MEASUREMENT ONLY. This script registers NOTHING in code: nflverse
lines remain a consensus close (never a sharp anchor — ADR-0017), and the
number it reports (median |logit diff| of devigged home moneyline probability,
nflverse vs our own Pinnacle close capture) is evidence for a FUTURE decision
about whether nflverse lines are provenance-close to Pinnacle. No bets, no
writes, no source registration.

Inputs are FILES (no network, no direct DB dependency):

  1. --games  : nflverse games.csv (FREE — nflverse/nfldata), e.g.
       curl -sL -o /tmp/games.csv \
         "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
  2. --closes : JSON array of our held Pinnacle h2h snapshots, exported
     READ-ONLY from the warehouse (last pre-kickoff capture per selection):

       docker exec -i betting-ai-postgres-1 psql -U betting_ai -d betting_ai \
         -At -c "SELECT json_agg(row_to_json(r)) FROM (
            SELECT t1.name AS home, t2.name AS away, e.starts_at,
                   os.selection, os.decimal_odds, os.captured_at
            FROM odds_snapshots os
            JOIN events e ON e.id = os.event_id
            JOIN sports sp ON sp.id = e.sport_id
            JOIN teams t1 ON t1.id = e.home_team_id
            JOIN teams t2 ON t2.id = e.away_team_id
            WHERE sp.key = 'pinnacle_american_football'
              AND os.bookmaker ILIKE '%pinn%' AND os.market = 'h2h'
              AND os.captured_at <= e.starts_at) r" > /tmp/nfl_closes.json

Usage:
  uv run python scripts/sports/nfl_lines_provenance.py \
      --games /tmp/games.csv --closes /tmp/nfl_closes.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

# nflverse gameday/gametime are US Eastern wall-clock.
_ET = ZoneInfo("America/New_York")
# Kickoff tolerance when pairing a games.csv row with a captured event —
# mirrors the matcher's accept drift (never the wide fetch window).
_KICKOFF_TOLERANCE = timedelta(hours=6)

# nflverse tricode -> Pinnacle long form. Kept HERE deliberately: word-like
# tricodes (NO/NE/CAR/TEN/...) are never seeded into the global alias table
# (collision surface, not a scrape surface form). Legacy codes map to the
# franchise's current name so old seasons still pair.
TRICODE_TO_NAME: dict[str, str] = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAR": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
    # legacy franchise codes
    "OAK": "Las Vegas Raiders",
    "SD": "Los Angeles Chargers",
    "STL": "Los Angeles Rams",
}


def _norm(name: str) -> str:
    return " ".join(name.casefold().split())


@dataclass(frozen=True)
class GameLine:
    home: str  # Pinnacle long form
    away: str
    kickoff_utc: datetime
    home_moneyline: float  # American odds
    away_moneyline: float


@dataclass(frozen=True)
class CloseProb:
    home: str
    away: str
    kickoff_utc: datetime
    home_prob: float  # devigged (multiplicative 2-way)


def american_to_decimal(american: float) -> float:
    if american >= 100.0:
        return 1.0 + american / 100.0
    if american <= -100.0:
        return 1.0 + 100.0 / -american
    raise ValueError(f"invalid American odds {american}")


def devig_two_way(decimal_a: float, decimal_b: float) -> float:
    """Multiplicative devig: fair prob of side A from a 2-way decimal pair."""
    if decimal_a <= 1.0 or decimal_b <= 1.0:
        raise ValueError("decimal odds must exceed 1.0")
    inv_a, inv_b = 1.0 / decimal_a, 1.0 / decimal_b
    return inv_a / (inv_a + inv_b)


def logit(p: float) -> float:
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability {p} outside (0, 1)")
    return math.log(p / (1.0 - p))


def load_nflverse_lines(csv_path: Path) -> list[GameLine]:
    games: list[GameLine] = []
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                home = TRICODE_TO_NAME[row["home_team"].strip()]
                away = TRICODE_TO_NAME[row["away_team"].strip()]
                hml = float(row["home_moneyline"])
                aml = float(row["away_moneyline"])
                gameday, gametime = row["gameday"], row["gametime"]
                if not gameday or not gametime:
                    continue
                local = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
            except (KeyError, TypeError, ValueError):
                continue  # lineless / TBD-kickoff / non-NFL tricode rows
            games.append(GameLine(home, away, local.replace(tzinfo=_ET).astimezone(UTC), hml, aml))
    return games


def load_arcadia_close_probs(json_path: Path) -> list[CloseProb]:
    """Collapse exported h2h snapshots to one devigged close prob per event.

    Close = the LAST pre-kickoff capture per selection (the export is already
    kickoff-bounded; we keep max captured_at per side)."""
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    if raw is None:
        return []
    by_event: dict[tuple[str, str, str], dict[str, tuple[datetime, float]]] = {}
    kickoffs: dict[tuple[str, str, str], datetime] = {}
    for row in raw:
        starts_at = datetime.fromisoformat(row["starts_at"])
        captured_at = datetime.fromisoformat(row["captured_at"])
        key = (_norm(row["home"]), _norm(row["away"]), starts_at.isoformat())
        kickoffs[key] = starts_at
        sides = by_event.setdefault(key, {})
        sel = _norm(row["selection"])
        prev = sides.get(sel)
        if prev is None or captured_at > prev[0]:
            sides[sel] = (captured_at, float(row["decimal_odds"]))
    closes: list[CloseProb] = []
    for key, sides in by_event.items():
        home_n, away_n, _ = key
        if home_n not in sides or away_n not in sides:
            continue  # need BOTH sides captured to devig — never one-legged
        home_prob = devig_two_way(sides[home_n][1], sides[away_n][1])
        closes.append(CloseProb(home_n, away_n, kickoffs[key], home_prob))
    return closes


def pair_and_diff(games: list[GameLine], closes: list[CloseProb]) -> list[float]:
    """|logit diff| of devigged home prob for every pairable fixture."""
    diffs: list[float] = []
    for close in closes:
        for game in games:
            if (
                _norm(game.home) == close.home
                and _norm(game.away) == close.away
                and abs(game.kickoff_utc - close.kickoff_utc) <= _KICKOFF_TOLERANCE
            ):
                nflverse_prob = devig_two_way(
                    american_to_decimal(game.home_moneyline),
                    american_to_decimal(game.away_moneyline),
                )
                diffs.append(abs(logit(nflverse_prob) - logit(close.home_prob)))
                break
    return diffs


def run(games_path: Path, closes_path: Path) -> dict[str, float | int]:
    games = load_nflverse_lines(games_path)
    closes = load_arcadia_close_probs(closes_path)
    diffs = pair_and_diff(games, closes)
    report: dict[str, float | int] = {
        "nflverse_games_with_moneylines": len(games),
        "arcadia_close_events": len(closes),
        "paired_fixtures": len(diffs),
    }
    if diffs:
        s = sorted(diffs)
        report["median_abs_logit_diff"] = median(s)
        report["p25_abs_logit_diff"] = s[len(s) // 4]
        report["p75_abs_logit_diff"] = s[(3 * len(s)) // 4]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=Path, required=True, help="nflverse games.csv")
    parser.add_argument("--closes", type=Path, required=True, help="exported closes JSON")
    args = parser.parse_args()
    report = run(args.games, args.closes)
    for key, value in report.items():
        print(f"{key}: {value}")
    if report["paired_fixtures"] == 0:
        print("no pairable fixtures yet (NFL Arcadia capture accrues in-season) — n=0 is OK")


if __name__ == "__main__":
    main()
