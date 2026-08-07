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
from datetime import UTC, date, datetime, timedelta
from typing import Literal

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


# How the match reached its result. "full" is a normally-completed game and
# the only value non-tennis providers ever emit; "retired" and "void" exist
# for the DECLARED tennis settlement convention (see
# app/settlement/outcomes.py::TENNIS_SETTLEMENT_CONVENTION).
Completion = Literal["full", "retired", "void"]


@dataclass(frozen=True)
class FinalScore:
    home_team: str
    away_team: str
    match_date: date
    home_score: int
    away_score: int
    # Tennis-convention fields (defaults keep every existing provider —
    # football-data CSVs, scraped finals, ESPN team sports — byte-identical).
    # completion="retired" REQUIRES winner_side: the settler grades h2h to the
    # advancing side and voids everything else. completion="void" voids all
    # markets (walkover / abandoned before one completed set).
    completion: Completion = "full"
    winner_side: Literal["home", "away"] | None = None


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
    # Scottish lower leagues (registered by register_extra_leagues, backlog
    # audit 2026-08-07: 30 Scottish League 2 picks had no results source).
    "scotland-league-one": ScoreSource(kind="season", code="SC2"),
    "scotland-league-two": ScoreSource(kind="season", code="SC3"),
}


def current_season_code(as_of: date) -> str:
    """football-data's 4-digit season code ("2627" = 2026-27) current at
    `as_of`. European seasons roll in July: the new files appear on
    football-data as soon as the season starts, while a configured
    FOOTBALLDATA_SEASONS list rots at every rollover (audit 2026-08-07:
    seasons=2425,2526 left the whole 2026-27 Scottish Premiership start
    unsettleable). Callers append this code to the configured list; a
    not-yet-published file 404s and is skipped quietly."""
    start_year = as_of.year if as_of.month >= 7 else as_of.year - 1
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


# Letters with NO NFKD ASCII decomposition — the encode("ascii","ignore") pass
# silently DROPPED them, so ESPN "FC Nordsjælland" could never match
# OddsChecker "FC Nordsjaelland" (2026-08-07 backlog audit: 94 stuck
# Conference-qual picks were dominated by this class). Applied BEFORE NFKD;
# decomposable accents (é, ö, ä, …) keep going through NFKD as before.
_TRANSLITERATE = str.maketrans(
    {
        "ø": "o",
        "Ø": "O",
        "æ": "ae",
        "Æ": "Ae",
        "đ": "d",
        "Đ": "D",
        "ð": "d",
        "Ð": "D",
        "þ": "th",
        "Þ": "Th",
        "ł": "l",
        "Ł": "L",
        "ß": "ss",
    }
)

# EXPLICIT settlement aliases: exonym/nickname/typo pairs no token rule can
# bridge, mapped on the FULL normalized name (never a token/substring, so a
# marked variant like "heart of midlothian w" can NOT alias onto the senior
# side — the marker veto in _markers_agree stays authoritative regardless).
# One real club/player per row; every row evidence-backed against live result
# payloads vs the stuck-pick backlog (audit 2026-08-07). Values are the
# normalized form our OddsChecker-scraped events carry.
_NAME_ALIASES: dict[str, str] = {
    "f c kobenhavn": "fc copenhagen",  # ESPN Danish exonym (after ø translit)
    "heart of midlothian": "hearts",
    "red star belgrade": "crvena zvezda",
    "the new saints": "tns",
    "din tbilisi": "dinamo tbilisi",  # our scrape abbreviates; ESPN is full
    "dinamo city": "dinamo tirana",  # FK Dinamo City = renamed Dinamo Tirana
    "ml vitebsk": "bc maxline",  # Maxline Vitebsk sponsor-vs-city naming
    "red bull new york": "new york red bulls",
    "abroath": "arbroath",  # OddsChecker typo observed in live events
    "hapoel be er": "hapoel beer sheva",  # ESPN truncates "Be'er"
    "pafos": "aep paphos",  # same Cypriot club, merged-era naming
    "botic van de zandschulo": "botic van de zandschulp",  # scrape typo
}


def normalize_team(name: str) -> str:
    """Casefold, strip accents, keep alphanumerics, collapse whitespace.

    Non-decomposable letters transliterate first (see _TRANSLITERATE); the
    finished form then passes through the explicit full-name alias table so
    both sides of any lookup speak one canonical spelling.
    """
    decomposed = unicodedata.normalize("NFKD", name.translate(_TRANSLITERATE))
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    cleaned = "".join(ch if ch.isalnum() else " " for ch in ascii_only.casefold())
    normalized = " ".join(cleaned.split())
    return _NAME_ALIASES.get(normalized, normalized)


# Club-type prefix/suffix tokens that differ between result providers for the SAME
# club (OddsChecker "FK Riga" vs ESPN "Riga FC"; "CE Europa" vs "Europa FC"). Dropped
# only for the token-set comparison below — never for the wrong-game marker veto.
_CLUB_TOKENS = frozenset(
    {
        "fc",
        "fk",
        "cf",
        "sc",
        "ce",
        "ss",
        "bc",
        "kf",
        "cs",
        "sk",
        "ac",
        "cd",
        "if",
        "bk",
        "afc",
        "nk",
        "hnk",
        "us",
        "as",
        "rc",
        "sv",
        "sd",
        "ud",
        "ca",
        "ki",
        "club",
        "fci",
    }
)


# Same-club spelling variants canonicalized at token level ("Dundee Utd" vs
# ESPN "Dundee United"; "Queens Park FC" vs ESPN "Queen's Park", whose
# apostrophe splits to a dropped single letter). A rewrite never REMOVES a
# distinguishing token — it only respells it — so two distinct clubs can never
# be merged by this table.
_TOKEN_REWRITES = {"utd": "united", "queens": "queen"}


def _core_tokens(normalized: str) -> frozenset[str]:
    """Distinguishing tokens of an already-normalized name: drop club-type tokens
    and single letters (the ``d`` of "d'Escaldes"), then canonicalize known
    same-club spellings (_TOKEN_REWRITES). Multi-digit tokens are KEPT —
    they are frequently the only thing separating two clubs ("1860 Munich" vs
    "Bayern Munich"); a one-sided trailing number ("Shkendija 79" vs "Shkendija")
    still recovers through the subset relation in _names_match."""
    return frozenset(
        _TOKEN_REWRITES.get(t, t)
        for t in normalized.split()
        if t not in _CLUB_TOKENS and len(t) > 1
    )


def _names_match(ours: str, theirs: str) -> bool:
    """True when two normalized team names denote the same club. Containment first
    (the historical behaviour), then an ORDER-INDEPENDENT token-set comparison that
    tolerates club-token / word-order / accent / trailing-number differences between
    result providers ("FC 03 Differdange" == "FC Differdange 03"; "CE Europa" ==
    "Europa FC"). Safe: the caller still applies the wrong-game marker veto, requires
    BOTH teams to match on the SAME date, and refuses ambiguous multi-hits."""
    if ours == theirs or ours in theirs or theirs in ours:
        return True
    o, t = _core_tokens(ours), _core_tokens(theirs)
    return bool(o) and bool(t) and (o == t or o <= t or t <= o)


def _markers_agree(ours: str, theirs: str) -> bool:
    """WRONG-GAME VETO. Reject a containment bind when the two RAW names disagree
    on a women/youth/reserve/B distinguishing marker (one present, the other
    absent, or different). Reuses the strict CLV matcher's marker set so the
    highest-stakes operation (settlement) is never weaker than the close matcher:
    "Arsenal" must not settle from "Arsenal Women"/"Arsenal U21"/"Arsenal B".
    Senior-vs-senior containment ("Flamengo" ~ "Flamengo RJ") is untouched —
    neither side carries a marker, so the sets are equal."""
    return distinguishing_markers(ours) == distinguishing_markers(theirs)


# Ambiguous-match warning rate-limit (verified audit 2026-08-06): the 30s
# settlement cycle re-warned the same stuck fixture every cycle (~180k
# lines/48h). Warn once per (fixture, UTC day); repeats stay silent until the
# day rolls over. Mirrors the engine's unsettleable-warning dedup (c780c1a).
# In-memory only — a restart re-warns each fixture once, which is acceptable.
_AMBIGUOUS_WARNED: dict[tuple[str, str, date], date] = {}


def reset_ambiguous_warning_state() -> None:
    """Forget which ambiguous fixtures were already warned about (tests +
    operator tooling)."""
    _AMBIGUOUS_WARNED.clear()


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
        """Score for the fixture, tolerating ±1 day of CSV/UTC date skew.

        Date discipline (wrong-game guard): the pick's OWN kickoff date always
        wins; an adjacent-date (±1) hit is accepted ONLY when the exact date has
        nothing AND exactly one adjacent date matches. Same pairing on BOTH
        adjacent dates (an NBA back-to-back) is ambiguous — either could be
        "the" game — so refuse and leave the pick open.
        """
        h, a = normalize_team(home), normalize_team(away)
        d0 = kickoff_utc.date()
        exact = self._exact.get((h, a, d0))
        if exact is not None:
            return exact
        prev = self._exact.get((h, a, d0 - timedelta(days=1)))
        nxt = self._exact.get((h, a, d0 + timedelta(days=1)))
        if prev is not None and nxt is not None:
            logger.warning(
                "back-to-back pairing %s vs %s on both adjacent dates — leaving open", home, away
            )
            return None
        if prev is not None or nxt is not None:
            return prev if prev is not None else nxt

        def _contained(d: date) -> list[FinalScore]:
            return [
                score
                for score in self._by_date.get(d, [])
                if _names_match(h, normalize_team(score.home_team))
                and _names_match(a, normalize_team(score.away_team))
                and _markers_agree(home, score.home_team)
                and _markers_agree(away, score.away_team)
            ]

        candidates = _contained(d0)
        if not candidates:  # same exact-date-first / adjacent-ambiguity discipline
            before, after = _contained(d0 - timedelta(days=1)), _contained(d0 + timedelta(days=1))
            if before and after:
                logger.warning(
                    "back-to-back pairing %s vs %s on both adjacent dates — leaving open",
                    home,
                    away,
                )
                return None
            candidates = before or after
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # Duplicate feed rows are NOT ambiguous: when every candidate
            # carries the same score/outcome payload, grading is identical
            # whichever row is "the" game — settle with it. Only genuinely
            # conflicting payloads stay open (fail-closed unchanged).
            payloads = {
                (s.home_score, s.away_score, s.completion, s.winner_side) for s in candidates
            }
            if len(payloads) == 1:
                return candidates[0]
            key = (h, a, d0)
            today = datetime.now(tz=UTC).date()
            if _AMBIGUOUS_WARNED.get(key) != today:
                # Once per (fixture, UTC day) — not per 30s cycle.
                _AMBIGUOUS_WARNED[key] = today
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
                # ninety_minute_only: martj42 scores INCLUDE extra time, so only
                # tournaments whose format can never reach ET are settleable —
                # an ET-inclusive score would corrupt 90-minute 1X2/totals.
                scores.extend(
                    scores_from_international(parse_results(text, ninety_minute_only=True))
                )
            elif source.kind == "new_league" and source.code is not None:
                text = await fetch_new_league_csv(client, source.code)
                scores.extend(scores_from_match_rows(parse_new_league_csv(text)))
            elif source.kind == "season" and source.code is not None:
                # The CURRENT season is always fetched too (config lists rot at
                # the July rollover); dict.fromkeys dedupes, order preserved.
                for season in dict.fromkeys([*seasons, current_season_code(on_or_after)]):
                    try:
                        text = await fetch_season_csv(client, source.code, season)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 404:
                            continue  # season file not published yet — normal
                        raise
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
