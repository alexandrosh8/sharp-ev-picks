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


# Tournament FORMATS that can never reach extra time, casefolded. martj42
# scores INCLUDE extra time (documented upstream) and the CSV carries no
# stage/shootout column, so the tournament name is the ONLY 90-minute signal:
# a knockout-capable competition's row may be an ET-inclusive score that would
# missettle 90-minute 1X2/totals. Conservative ALLOWLIST (fail-closed —
# unknown tournaments are excluded; those picks settle manually or void):
# international friendlies always end at 90'. Qualifications are NOT listed —
# their playoff rounds (UEFA WC/Euro paths, inter-confederation) go to ET and
# the rows are indistinguishable from the group-stage ones.
NINETY_MINUTE_TOURNAMENTS = frozenset({"friendly"})


def parse_results(text: str, *, ninety_minute_only: bool = False) -> list[InternationalMatch]:
    """Completed matches only (rows with numeric scores).

    `ninety_minute_only=True` (the SETTLEMENT path) additionally keeps only
    tournaments in NINETY_MINUTE_TOURNAMENTS, because these scores include
    extra time. The default keeps everything (model training wants all rows).
    """
    matches: list[InternationalMatch] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for raw in reader:
        # truncated rows surface as None via DictReader restval — never abort
        # the whole parse over one malformed line
        hs = raw.get("home_score") or ""
        as_ = raw.get("away_score") or ""
        if hs in ("", "NA") or as_ in ("", "NA"):
            continue
        tournament = (raw.get("tournament") or "").strip()
        if ninety_minute_only and tournament.casefold() not in NINETY_MINUTE_TOURNAMENTS:
            continue
        parsed = _parse_iso_date(raw.get("date") or "")
        if parsed is None:
            continue
        try:
            matches.append(
                InternationalMatch(
                    match_date=parsed,
                    home_team=(raw.get("home_team") or "").strip(),
                    away_team=(raw.get("away_team") or "").strip(),
                    home_goals=int(hs),
                    away_goals=int(as_),
                    tournament=tournament,
                    neutral=str(raw.get("neutral") or "").strip().upper() == "TRUE",
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return matches


def parse_fixtures(
    text: str, tournament: str = "FIFA World Cup", on_or_after: date | None = None
) -> list[Fixture]:
    """Scheduled (unplayed) matches for a tournament — NA scores in the feed."""
    fixtures: list[Fixture] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for raw in reader:
        if (raw.get("tournament") or "").strip() != tournament:
            continue
        if (raw.get("home_score") or "") not in ("", "NA"):
            continue  # already played
        parsed = _parse_iso_date(raw.get("date") or "")
        if parsed is None or (on_or_after and parsed < on_or_after):
            continue
        home = (raw.get("home_team") or "").strip()
        away = (raw.get("away_team") or "").strip()
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
