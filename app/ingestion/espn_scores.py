"""Free, read-only ESPN scoreboard -> FinalScore for multi-sport auto-settlement.

ESPN's public "site API" needs no key and no login. We GET only completed
SCORES/fixtures — NEVER odds (ESPN prices are soft; doctrine forbids using them
as a close). The output is app.settlement.results.FinalScore so the existing
ScoreBook + settler resolve basketball / NFL / tennis with no change to the
sport-agnostic outcome math.

  GET https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates=YYYYMMDD

Team sports (basketball/football) nest competitions[0].competitors[]; tennis
nests events[].groupings[].competitions[].competitors[] with per-set linescores
(no aggregate score), so the tennis parser derives a SET score (sets won by
each player) — which settles BOTH match_winner (h2h) and over_under_sets
(totals: sets_home + sets_away) through the same settle_selection.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import httpx

from app.settlement.results import FinalScore

logger = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_TIMEOUT = httpx.Timeout(15.0)


@dataclass(frozen=True)
class EspnSource:
    """One ESPN scoreboard feed; kind 'team' (basketball/football) or 'tennis'."""

    sport: str
    league: str
    kind: str = "team"


def _match_date(iso: str) -> date | None:
    """Date portion of an ESPN ISO timestamp ('2024-01-15T23:00Z' -> 2024-01-15).

    The ScoreBook tolerates +/-1 day of UTC skew, so the date alone is enough.
    """
    try:
        return date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None


def _is_final(competition: dict) -> bool:
    return bool(competition.get("status", {}).get("type", {}).get("completed"))


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_team_scoreboard(data: dict) -> list[FinalScore]:
    """FinalScores from a team-sport ESPN scoreboard (basketball / football).

    Only FINAL competitions with both a home and away competitor carrying an
    integer score and a team displayName are emitted; anything else is skipped.
    """
    scores: list[FinalScore] = []
    for event in data.get("events") or []:
        for comp in event.get("competitions") or []:
            if not _is_final(comp):
                continue
            md = _match_date(comp.get("date") or event.get("date") or "")
            sides: dict[str, tuple[str, int]] = {}
            for c in comp.get("competitors") or []:
                ha = c.get("homeAway")
                name = (c.get("team") or {}).get("displayName")
                pts = _int_or_none(c.get("score"))
                if ha in ("home", "away") and name and pts is not None:
                    sides[ha] = (str(name), pts)
            if md is not None and "home" in sides and "away" in sides:
                (hn, hs), (an, a_s) = sides["home"], sides["away"]
                scores.append(FinalScore(hn, an, md, hs, a_s))
    return scores


# A tennis competition whose status text carries any of these was NOT decided
# by normal play (retirement / walkover / default). ESPN still flags them
# completed=True, so the text is the only gate; matched case-insensitively on
# status.type name/detail/shortDetail/description. "\bret\b\.?" catches "Ret."
# without firing on ordinary words containing "ret".
_ABNORMAL_COMPLETION_RE = re.compile(
    r"(?i)(retired|\bret\b\.?|walkover|\bw/o\b|default)",
)


def _is_abnormal_completion(competition: dict) -> bool:
    status_type = competition.get("status", {}).get("type", {})
    return any(
        _ABNORMAL_COMPLETION_RE.search(str(status_type.get(key) or ""))
        for key in ("name", "detail", "shortDetail", "description")
    )


def _set_complete(winner_games: int, loser_games: int) -> bool:
    """A set is WON only when finished: >=6 games with a >=2 margin (covers
    6-x, 7-5 and advantage sets, and a >=10 match tiebreak) or a 7-6 tiebreak.
    A leading PARTIAL set (play stopped mid-set) is never counted."""
    if winner_games == 7 and loser_games == 6:
        return True
    return winner_games >= 6 and winner_games - loser_games >= 2


def _sets_won(home_lines: Sequence[float], away_lines: Sequence[float]) -> tuple[int, int]:
    """COMPLETE sets won by (home, away) from per-set game counts."""
    hw = aw = 0
    for h, a in zip(home_lines, away_lines, strict=False):
        if h > a and _set_complete(int(h), int(a)):
            hw += 1
        elif a > h and _set_complete(int(a), int(h)):
            aw += 1
    return hw, aw


def parse_tennis_scoreboard(data: dict) -> list[FinalScore]:
    """FinalScores (as SET scores) from an ESPN tennis scoreboard.

    Tennis nests events[].groupings[].competitions[]; each competition's
    competitors carry an athlete displayName and per-set `linescores` (games
    won per set) but no aggregate score. We emit FinalScore(home_player,
    away_player, date, sets_home, sets_away) — settling match_winner (h2h) AND
    over_under_sets (totals: sets_home + sets_away) through the same outcome math.
    """
    scores: list[FinalScore] = []
    for event in data.get("events") or []:
        for grouping in event.get("groupings") or []:
            for comp in grouping.get("competitions") or []:
                if not _is_final(comp):
                    continue
                if _is_abnormal_completion(comp):
                    continue  # retirement/walkover/default -> never a set score
                md = _match_date(comp.get("date") or event.get("date") or "")
                sides: dict[str, tuple[str, list[float]]] = {}
                for c in comp.get("competitors") or []:
                    ha = c.get("homeAway")
                    name = (c.get("athlete") or {}).get("displayName")
                    lines = [
                        float(x["value"])
                        for x in (c.get("linescores") or [])
                        if x.get("value") is not None
                    ]
                    if ha in ("home", "away") and name:
                        sides[ha] = (str(name), lines)
                if md is None or "home" not in sides or "away" not in sides:
                    continue
                (hn, hl), (an, al) = sides["home"], sides["away"]
                hs, a_s = _sets_won(hl, al)
                # Emit only a COMPLETE best-of pattern: 2 winner sets (Bo3) or
                # 3 (Bo5). Anything else (0 sets, 1 set, a tie) is a partial
                # match — emit nothing, the pick stays pending and ages into
                # the existing void path rather than settling on a fragment.
                if max(hs, a_s) not in (2, 3) or hs == a_s:
                    continue
                scores.append(FinalScore(hn, an, md, hs, a_s))
    return scores


async def fetch_espn_scores(
    client: httpx.AsyncClient, source: EspnSource, dates: Sequence[date]
) -> list[FinalScore]:
    """GET the ESPN scoreboard for each date and parse to FinalScores.

    A failing date is logged (exception TYPE only — never the URL) and skipped;
    the hourly settle cycle retries. Read-only GET, no key, no login.
    """
    parse = parse_tennis_scoreboard if source.kind == "tennis" else parse_team_scoreboard
    out: list[FinalScore] = []
    for d in dates:
        url = f"{ESPN_BASE}/{source.sport}/{source.league}/scoreboard"
        try:
            resp = await client.get(url, params={"dates": d.strftime("%Y%m%d")}, timeout=_TIMEOUT)
            if resp.status_code == 404:
                continue  # no scoreboard for this league/date (off-season etc.) — normal, quiet
            resp.raise_for_status()
            out.extend(parse(resp.json()))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "ESPN %s/%s scoreboard %s failed: %s",
                source.sport,
                source.league,
                d.isoformat(),
                type(exc).__name__,
            )
    return out


# Our warehouse sport prefix (Pick.sport) -> the ESPN feeds carrying its
# results. Soccer is intentionally absent (it auto-settles via football-data
# CSVs in app/settlement/results.py). EuroLeague is NOT on ESPN (separate
# official API) — basketball ESPN coverage here is NBA + WNBA + NBL.
SPORT_ESPN_SOURCES: dict[str, tuple[EspnSource, ...]] = {
    "basketball": (
        EspnSource("basketball", "nba"),
        EspnSource("basketball", "wnba"),
        EspnSource("basketball", "nbl"),
    ),
    "american_football": (
        EspnSource("football", "nfl"),
        EspnSource("football", "college-football"),
    ),
    "tennis": (
        EspnSource("tennis", "atp", kind="tennis"),
        EspnSource("tennis", "wta", kind="tennis"),
    ),
}


async def load_espn_scores(
    client: httpx.AsyncClient, sport_keys: Sequence[str], dates: Sequence[date]
) -> list[FinalScore]:
    """All ESPN FinalScores for the configured sport keys over the date window.

    Unknown sport keys (no ESPN feed) contribute nothing and make no request.
    Per-feed failures are already swallowed inside fetch_espn_scores.
    """
    out: list[FinalScore] = []
    for sport_key in sport_keys:
        for source in SPORT_ESPN_SOURCES.get(sport_key, ()):
            out.extend(await fetch_espn_scores(client, source, dates))
    return out
