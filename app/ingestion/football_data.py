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
    "E2": "England League One",
    "E3": "England League Two",
    "EC": "England National League",
    "SC0": "Scotland Premiership",
    "SC1": "Scotland Championship",
    "SC2": "Scotland League One",
    "SC3": "Scotland League Two",
    "D1": "Germany Bundesliga",
    "D2": "Germany 2. Bundesliga",
    "I1": "Italy Serie A",
    "I2": "Italy Serie B",
    "SP1": "Spain La Liga",
    "SP2": "Spain Segunda",
    "F1": "France Ligue 1",
    "F2": "France Ligue 2",
    "N1": "Netherlands Eredivisie",
    "B1": "Belgium Pro League",
    "P1": "Portugal Primeira Liga",
    "T1": "Turkey Super Lig",
    "G1": "Greece Super League",
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
    # Betfair Exchange 1X2 OPEN + CLOSE (BFEH.. / BFECH..): verified populated
    # ~94% on 2025-26 files with open != close on 93% of rows (audit
    # 2026-07-10). With the Pinnacle columns dead since ~2026-01-15 this pair
    # is the FREE sharp anchor+close for the football backtest spine
    # (exchange prices are gross — net commission on the EV side, not here).
    betfair_exchange_open_home: float | None = None
    betfair_exchange_open_draw: float | None = None
    betfair_exchange_open_away: float | None = None
    betfair_exchange_closing_home: float | None = None
    betfair_exchange_closing_draw: float | None = None
    betfair_exchange_closing_away: float | None = None
    # Match statistics (HS/AS/HST/AST/HC/AC) — present in the main-league
    # CSVs (~2000s onward), absent in older files and the new-leagues feed.
    # Inputs to the Wheatcroft GAP shots/corners OU2.5 shadow screen.
    home_shots: int | None = None
    away_shots: int | None = None
    home_shots_on_target: int | None = None
    away_shots_on_target: int | None = None
    home_corners: int | None = None
    away_corners: int | None = None


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
                    betfair_exchange_open_home=_opt_float(raw.get("BFEH")),
                    betfair_exchange_open_draw=_opt_float(raw.get("BFED")),
                    betfair_exchange_open_away=_opt_float(raw.get("BFEA")),
                    betfair_exchange_closing_home=_opt_float(raw.get("BFECH")),
                    betfair_exchange_closing_draw=_opt_float(raw.get("BFECD")),
                    betfair_exchange_closing_away=_opt_float(raw.get("BFECA")),
                    home_shots=_opt_int(raw.get("HS")),
                    away_shots=_opt_int(raw.get("AS")),
                    home_shots_on_target=_opt_int(raw.get("HST")),
                    away_shots_on_target=_opt_int(raw.get("AST")),
                    home_corners=_opt_int(raw.get("HC")),
                    away_corners=_opt_int(raw.get("AC")),
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
                    # The new-leagues feed carries CLOSING odds only (B365C*/
                    # PSC*); MatchRow.b365_* means PRE-MATCH prices, so they
                    # stay None here — backtests must not bet at the close.
                    b365_home=None,
                    b365_draw=None,
                    b365_away=None,
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


def _opt_int(raw: str | None) -> int | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        # some files carry stats as "7.0" — accept but keep integer semantics
        return int(float(raw))
    except (ValueError, OverflowError):
        # OverflowError: 'inf'/'1e999' cells — degrade to None, never escape
        # the caller's (KeyError, ValueError) per-row catch.
        return None


def _opt_float(raw: str | None) -> float | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None
