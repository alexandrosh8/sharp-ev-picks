"""International football results — martj42/international_results (CC0).

49k+ international matches (1872-present) with a `neutral` venue flag, plus
scheduled fixtures (NA scores) for upcoming tournaments incl. the 2026 World
Cup. Read-only GET of a public CSV. CC0 public domain.

Schema: date,home_team,away_team,home_score,away_score,tournament,city,country,neutral
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

from app.ingestion.football_data import MatchRow

logger = logging.getLogger(__name__)

RESULTS_URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"


@dataclass(frozen=True)
class InternationalMatch:
    match_date: date
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    tournament: str
    neutral: bool


@dataclass(frozen=True)
class Fixture:
    """A scheduled match (no result yet)."""

    match_date: date
    home_team: str
    away_team: str
    tournament: str
    neutral: bool


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=8.0),
    reraise=True,
)
async def fetch_results_csv(client: httpx.AsyncClient) -> str:
    response = await client.get(RESULTS_URL, timeout=30.0)
    response.raise_for_status()
    return response.text


def parse_results(text: str) -> list[InternationalMatch]:
    """Completed matches only (rows with numeric scores)."""
    matches: list[InternationalMatch] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for raw in reader:
        hs, as_ = raw.get("home_score", ""), raw.get("away_score", "")
        if hs in ("", "NA") or as_ in ("", "NA"):
            continue
        parsed = _parse_iso_date(raw.get("date", ""))
        if parsed is None:
            continue
        try:
            matches.append(
                InternationalMatch(
                    match_date=parsed,
                    home_team=raw["home_team"].strip(),
                    away_team=raw["away_team"].strip(),
                    home_goals=int(hs),
                    away_goals=int(as_),
                    tournament=raw.get("tournament", "").strip(),
                    neutral=str(raw.get("neutral", "")).strip().upper() == "TRUE",
                )
            )
        except (KeyError, ValueError):
            continue
    return matches


def parse_fixtures(
    text: str, tournament: str = "FIFA World Cup", on_or_after: date | None = None
) -> list[Fixture]:
    """Scheduled (unplayed) matches for a tournament — NA scores in the feed."""
    fixtures: list[Fixture] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for raw in reader:
        if raw.get("tournament", "").strip() != tournament:
            continue
        if raw.get("home_score", "") not in ("", "NA"):
            continue  # already played
        parsed = _parse_iso_date(raw.get("date", ""))
        if parsed is None or (on_or_after and parsed < on_or_after):
            continue
        home, away = raw.get("home_team", "").strip(), raw.get("away_team", "").strip()
        if not home or not away:
            continue
        fixtures.append(
            Fixture(
                match_date=parsed,
                home_team=home,
                away_team=away,
                tournament=tournament,
                neutral=str(raw.get("neutral", "")).strip().upper() == "TRUE",
            )
        )
    return fixtures


def to_match_rows(
    matches: list[InternationalMatch],
) -> tuple[list[MatchRow], list[bool]]:
    """Adapt to the model's MatchRow plus a parallel neutral-venue list.

    International results carry no odds, so the odds fields are None.
    """
    rows: list[MatchRow] = []
    neutral: list[bool] = []
    for m in matches:
        if m.home_goals > m.away_goals:
            res = "H"
        elif m.away_goals > m.home_goals:
            res = "A"
        else:
            res = "D"
        rows.append(
            MatchRow(
                match_date=m.match_date,
                home_team=m.home_team,
                away_team=m.away_team,
                home_goals=m.home_goals,
                away_goals=m.away_goals,
                result=res,
                b365_home=None,
                b365_draw=None,
                b365_away=None,
                pinnacle_closing_home=None,
                pinnacle_closing_draw=None,
                pinnacle_closing_away=None,
            )
        )
        neutral.append(m.neutral)
    return rows, neutral


def _parse_iso_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
