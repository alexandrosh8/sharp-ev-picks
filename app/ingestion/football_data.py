"""football-data.co.uk loader — free historical football results + odds CSVs.

The CSVs include final scores and bookmaker odds; the PSC* columns are
Pinnacle CLOSING odds, which make this source suitable for CLV-aware
backtesting and model training. Read-only GET of public CSV files.
"""

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.football-data.co.uk/mmz4281"
# "New leagues" feed — one all-seasons CSV per country, different schema
# (Home/Away/HG/AG/Res + PSCH/PSCD/PSCA). Covers in-season non-European
# leagues (Brazil, Argentina, USA, ...) the European mmz4281 files don't.
NEW_LEAGUES_BASE_URL = "https://www.football-data.co.uk/new"

# League code -> human name (football-data.co.uk codes)
LEAGUES = {
    "E0": "England Premier League",
    "E1": "England Championship",
    "D1": "Germany Bundesliga",
    "I1": "Italy Serie A",
    "SP1": "Spain La Liga",
    "F1": "France Ligue 1",
    "N1": "Netherlands Eredivisie",
    "P1": "Portugal Primeira Liga",
}

# New-leagues country code -> human name
NEW_LEAGUES = {
    "BRA": "Brazil Serie A",
    "ARG": "Argentina Primera Division",
    "USA": "USA MLS",
    "MEX": "Mexico Liga MX",
    "JPN": "Japan J-League",
    "CHN": "China Super League",
}


@dataclass(frozen=True)
class MatchRow:
    """One historical match with results and (closing) odds."""

    match_date: date
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    result: str  # H | D | A
    b365_home: float | None
    b365_draw: float | None
    b365_away: float | None
    pinnacle_closing_home: float | None
    pinnacle_closing_draw: float | None
    pinnacle_closing_away: float | None


def season_url(league_code: str, season: str) -> str:
    """`season` is football-data's 4-digit form, e.g. '2425' for 2024/25."""
    if league_code not in LEAGUES:
        raise ValueError(f"unknown league code: {league_code}")
    return f"{BASE_URL}/{season}/{league_code}.csv"


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=8.0),
    reraise=True,
)
async def fetch_season_csv(client: httpx.AsyncClient, league_code: str, season: str) -> str:
    response = await client.get(season_url(league_code, season), timeout=30.0)
    response.raise_for_status()
    return response.text


def parse_season_csv(text: str) -> list[MatchRow]:
    rows: list[MatchRow] = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        if not raw.get("HomeTeam") or not raw.get("Date"):
            continue
        parsed_date = _parse_date(raw["Date"])
        if parsed_date is None:
            logger.warning("skipping row with unparseable date %r", raw.get("Date"))
            continue
        try:
            rows.append(
                MatchRow(
                    match_date=parsed_date,
                    home_team=raw["HomeTeam"].strip(),
                    away_team=raw["AwayTeam"].strip(),
                    home_goals=int(raw["FTHG"]),
                    away_goals=int(raw["FTAG"]),
                    result=raw["FTR"].strip(),
                    b365_home=_opt_float(raw.get("B365H")),
                    b365_draw=_opt_float(raw.get("B365D")),
                    b365_away=_opt_float(raw.get("B365A")),
                    pinnacle_closing_home=_opt_float(raw.get("PSCH")),
                    pinnacle_closing_draw=_opt_float(raw.get("PSCD")),
                    pinnacle_closing_away=_opt_float(raw.get("PSCA")),
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning("skipping malformed row: %s", type(exc).__name__)
    return rows


def new_league_url(country_code: str) -> str:
    """All-seasons CSV for a non-European league (e.g. 'BRA' -> brazil)."""
    if country_code not in NEW_LEAGUES:
        raise ValueError(f"unknown new-league code: {country_code}")
    return f"{NEW_LEAGUES_BASE_URL}/{country_code}.csv"


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=8.0),
    reraise=True,
)
async def fetch_new_league_csv(client: httpx.AsyncClient, country_code: str) -> str:
    response = await client.get(new_league_url(country_code), timeout=30.0)
    response.raise_for_status()
    return response.text


def parse_new_league_csv(text: str) -> list[MatchRow]:
    """Parse the new-leagues schema: Home/Away/HG/AG/Res + PSCH/PSCD/PSCA."""
    rows: list[MatchRow] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for raw in reader:
        if not raw.get("Home") or not raw.get("Date") or not raw.get("HG"):
            continue
        parsed_date = _parse_date(raw["Date"])
        if parsed_date is None:
            continue
        try:
            rows.append(
                MatchRow(
                    match_date=parsed_date,
                    home_team=raw["Home"].strip(),
                    away_team=raw["Away"].strip(),
                    home_goals=int(raw["HG"]),
                    away_goals=int(raw["AG"]),
                    result=raw["Res"].strip(),
                    b365_home=_opt_float(raw.get("B365CH")),
                    b365_draw=_opt_float(raw.get("B365CD")),
                    b365_away=_opt_float(raw.get("B365CA")),
                    pinnacle_closing_home=_opt_float(raw.get("PSCH")),
                    pinnacle_closing_draw=_opt_float(raw.get("PSCD")),
                    pinnacle_closing_away=_opt_float(raw.get("PSCA")),
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning("skipping malformed new-league row: %s", type(exc).__name__)
    return rows


def _parse_date(raw: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _opt_float(raw: str | None) -> float | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None
