"""Final scores from the free results sources, matchable to our events.

Sources are the loaders already in app/ingestion (football-data.co.uk +
martj42 international CSV); this module maps OddsPortal league slugs to
those sources and indexes scores by normalized team names + date so picks
(whose team names come from OddsPortal scrapes) can find their result.

Matching is deterministic: exact normalized names first, then a containment
fallback ("flamengo" ~ "flamengo rj") that must be UNIQUE on the date —
ambiguity returns no match (the pick stays open for manual settlement). The
containment fallback is wrong-game-vetoed by the strict resolution matcher's
distinguishing markers (women/youth/reserve/B): a men's pick can never settle
from a women's/youth/reserve score that merely contains its base name.
"""

import csv
import logging
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from app.ingestion.football_data import (
    MatchRow,
    fetch_new_league_csv,
    fetch_season_csv,
    parse_new_league_csv,
    parse_season_csv,
)
from app.ingestion.international_results import (
    InternationalMatch,
    fetch_results_csv,
    parse_results,
)
from app.resolution.matching import distinguishing_markers

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinalScore:
    home_team: str
    away_team: str
    match_date: date
    home_score: int
    away_score: int


@dataclass(frozen=True)
class ScoreSource:
    kind: str  # "international" | "new_league" | "season"
    code: str | None = None  # football-data code for the non-international kinds


INTERNATIONAL = ScoreSource(kind="international")

# OddsPortal league slug -> results source. KEYS MUST BE OddsHarvester
# league keys (oddsharvester.utils.sport_league_constants, verified against
# 0.3.0 on 2026-06-11) — the config slugs and this map share that registry.
# Slugs absent here (e.g. nba, euroleague, champions-league) have no free
# results feed — those picks settle manually via the dashboard/API.
_SLUG_SOURCES: dict[str, ScoreSource] = {
    "world-cup": INTERNATIONAL,
    "brazil-serie-a": ScoreSource(kind="new_league", code="BRA"),
    "argentina-liga-profesional": ScoreSource(kind="new_league", code="ARG"),
    "mexico-liga-mx": ScoreSource(kind="new_league", code="MEX"),
    "england-premier-league": ScoreSource(kind="season", code="E0"),
    "england-championship": ScoreSource(kind="season", code="E1"),
    "scotland-premiership": ScoreSource(kind="season", code="SC0"),
    "scotland-championship": ScoreSource(kind="season", code="SC1"),
    "germany-bundesliga": ScoreSource(kind="season", code="D1"),
    "germany-bundesliga-2": ScoreSource(kind="season", code="D2"),
    "italy-serie-a": ScoreSource(kind="season", code="I1"),
    "italy-serie-b": ScoreSource(kind="season", code="I2"),
    "spain-laliga": ScoreSource(kind="season", code="SP1"),
    "spain-laliga2": ScoreSource(kind="season", code="SP2"),
    "france-ligue-1": ScoreSource(kind="season", code="F1"),
    "liga-portugal": ScoreSource(kind="season", code="P1"),
    # Registered by app/ingestion/oddsportal.py::register_extra_leagues
    # (OddsPortal carries them; OddsHarvester 0.3.0's registry omits them).
    "netherlands-eredivisie": ScoreSource(kind="season", code="N1"),
    "belgium-jupiler-pro-league": ScoreSource(kind="season", code="B1"),
    "turkey-super-lig": ScoreSource(kind="season", code="T1"),
    "greece-super-league": ScoreSource(kind="season", code="G1"),
}


def normalize_team(name: str) -> str:
    """Casefold, strip accents, keep alphanumerics, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    cleaned = "".join(ch if ch.isalnum() else " " for ch in ascii_only.casefold())
    return " ".join(cleaned.split())


def _names_match(ours: str, theirs: str) -> bool:
    return ours == theirs or ours in theirs or theirs in ours


def _markers_agree(ours: str, theirs: str) -> bool:
    """WRONG-GAME VETO. Reject a containment bind when the two RAW names disagree
    on a women/youth/reserve/B distinguishing marker (one present, the other
    absent, or different). Reuses the strict CLV matcher's marker set so the
    highest-stakes operation (settlement) is never weaker than the close matcher:
    "Arsenal" must not settle from "Arsenal Women"/"Arsenal U21"/"Arsenal B".
    Senior-vs-senior containment ("Flamengo" ~ "Flamengo RJ") is untouched —
    neither side carries a marker, so the sets are equal."""
    return distinguishing_markers(ours) == distinguishing_markers(theirs)


class ScoreBook:
    """Final scores indexed for lookup by (team names, kickoff datetime)."""

    def __init__(self, scores: Iterable[FinalScore]) -> None:
        self._exact: dict[tuple[str, str, date], FinalScore] = {}
        self._by_date: dict[date, list[FinalScore]] = {}
        count = 0
        for score in scores:
            key = (normalize_team(score.home_team), normalize_team(score.away_team))
            self._exact[(*key, score.match_date)] = score
            self._by_date.setdefault(score.match_date, []).append(score)
            count += 1
        self._count = count

    def __len__(self) -> int:
        return self._count

    def lookup(self, home: str, away: str, kickoff_utc: datetime) -> FinalScore | None:
        """Score for the fixture, tolerating ±1 day of CSV/UTC date skew."""
        h, a = normalize_team(home), normalize_team(away)
        dates = [kickoff_utc.date() + timedelta(days=delta) for delta in (0, -1, 1)]
        for d in dates:
            found = self._exact.get((h, a, d))
            if found is not None:
                return found
        candidates = [
            score
            for d in dates
            for score in self._by_date.get(d, [])
            if _names_match(h, normalize_team(score.home_team))
            and _names_match(a, normalize_team(score.away_team))
            and _markers_agree(home, score.home_team)
            and _markers_agree(away, score.away_team)
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logger.warning("ambiguous score match for %s vs %s — leaving open", home, away)
        return None


def scores_from_match_rows(rows: Iterable[MatchRow]) -> list[FinalScore]:
    return [
        FinalScore(
            home_team=r.home_team,
            away_team=r.away_team,
            match_date=r.match_date,
            home_score=r.home_goals,
            away_score=r.away_goals,
        )
        for r in rows
    ]


def scores_from_international(matches: Iterable[InternationalMatch]) -> list[FinalScore]:
    return [
        FinalScore(
            home_team=m.home_team,
            away_team=m.away_team,
            match_date=m.match_date,
            home_score=m.home_goals,
            away_score=m.away_goals,
        )
        for m in matches
    ]


def league_score_sources(slugs: Iterable[str]) -> list[ScoreSource]:
    """Map configured OddsPortal slugs to results sources (deduped, ordered).

    The "all" sentinel (league-less daily scraping) expands to every known
    source — leagues without one still settle manually via the dashboard.
    """
    sources: list[ScoreSource] = []
    for slug in slugs:
        if slug == "all":
            for known in _SLUG_SOURCES.values():
                if known not in sources:
                    sources.append(known)
            continue
        source = _SLUG_SOURCES.get(slug)
        if source is None:
            logger.info("league %r has no free results source; manual settlement only", slug)
        elif source not in sources:
            sources.append(source)
    return sources


async def load_scores(
    client: httpx.AsyncClient,
    slugs: Sequence[str],
    seasons: Sequence[str],
    on_or_after: date,
) -> list[FinalScore]:
    """Fetch final scores for every mapped league source.

    A failing source is logged and skipped — settlement runs hourly, so the
    next cycle retries. Scores older than `on_or_after` are dropped to keep
    the book small.
    """
    scores: list[FinalScore] = []
    for source in league_score_sources(slugs):
        try:
            if source.kind == "international":
                text = await fetch_results_csv(client)
                scores.extend(scores_from_international(parse_results(text)))
            elif source.kind == "new_league" and source.code is not None:
                text = await fetch_new_league_csv(client, source.code)
                scores.extend(scores_from_match_rows(parse_new_league_csv(text)))
            elif source.kind == "season" and source.code is not None:
                for season in seasons:
                    text = await fetch_season_csv(client, source.code, season)
                    scores.extend(scores_from_match_rows(parse_season_csv(text)))
        except httpx.HTTPError as exc:
            logger.error("results source %s failed: %s", source.kind, type(exc).__name__)
        except (csv.Error, ValueError, UnicodeError) as exc:
            # A malformed/undecodable CSV on ONE source must not abort the whole
            # settlement load — log the type (never the payload) and skip it so
            # the remaining sources still load. Scoped to parse/decode/value
            # errors (UnicodeError ⊂, but kept explicit); real programming bugs
            # (TypeError, AttributeError, ...) still propagate.
            logger.error("results source %s unparseable: %s", source.kind, type(exc).__name__)
    return [s for s in scores if s.match_date >= on_or_after]
