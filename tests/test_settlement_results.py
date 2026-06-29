"""Score book + free results sources (no network — httpx.MockTransport)."""

from datetime import UTC, date, datetime

import httpx
import pytest

from app.ingestion.football_data import MatchRow
from app.ingestion.international_results import InternationalMatch
from app.settlement.results import (
    INTERNATIONAL,
    FinalScore,
    ScoreBook,
    ScoreSource,
    league_score_sources,
    load_scores,
    normalize_team,
    scores_from_international,
    scores_from_match_rows,
)


def fs(home: str = "Alpha FC", away: str = "Beta United", d: date | None = None) -> FinalScore:
    return FinalScore(
        home_team=home,
        away_team=away,
        match_date=d or date(2026, 6, 9),
        home_score=2,
        away_score=1,
    )


def test_normalize_team_strips_accents_case_punctuation() -> None:
    assert normalize_team("  São Paulo FC ") == "sao paulo fc"
    assert normalize_team("Atlético-MG") == "atletico mg"
    assert normalize_team("ALPHA  FC") == "alpha fc"


def test_lookup_exact_names_on_kickoff_date() -> None:
    book = ScoreBook([fs()])
    found = book.lookup("Alpha FC", "Beta United", datetime(2026, 6, 9, 18, 0, tzinfo=UTC))
    assert found is not None
    assert (found.home_score, found.away_score) == (2, 1)


def test_lookup_tolerates_one_day_offset() -> None:
    # Kickoff stored late-evening UTC can land on the next local date in CSVs.
    book = ScoreBook([fs(d=date(2026, 6, 9))])
    assert book.lookup("Alpha FC", "Beta United", datetime(2026, 6, 10, 1, 0, tzinfo=UTC))
    assert book.lookup("Alpha FC", "Beta United", datetime(2026, 6, 8, 23, 0, tzinfo=UTC))
    assert book.lookup("Alpha FC", "Beta United", datetime(2026, 6, 12, 18, 0, tzinfo=UTC)) is None


def test_lookup_unique_containment_fallback() -> None:
    # OddsPortal says "Flamengo RJ"; football-data says "Flamengo".
    book = ScoreBook([fs(home="Flamengo", away="Palmeiras")])
    found = book.lookup("Flamengo RJ", "Palmeiras", datetime(2026, 6, 9, 22, 0, tzinfo=UTC))
    assert found is not None


def test_lookup_ambiguous_containment_returns_none() -> None:
    # Two source entries both containment-match the pick name -> refuse, stay open.
    same_day = [
        fs(home="Santos FC", away="Palmeiras"),
        fs(home="Santos Laguna", away="Palmeiras"),
    ]
    book = ScoreBook(same_day)
    assert book.lookup("Santos", "Palmeiras", datetime(2026, 6, 9, 22, 0, tzinfo=UTC)) is None


def test_adapters_from_existing_loaders() -> None:
    row = MatchRow(
        match_date=date(2026, 6, 1),
        home_team="Alpha FC",
        away_team="Beta United",
        home_goals=3,
        away_goals=0,
        result="H",
        b365_home=None,
        b365_draw=None,
        b365_away=None,
        pinnacle_closing_home=None,
        pinnacle_closing_draw=None,
        pinnacle_closing_away=None,
    )
    intl = InternationalMatch(
        match_date=date(2026, 6, 2),
        home_team="Atlantis",
        away_team="Wakanda",
        home_goals=1,
        away_goals=1,
        tournament="FIFA World Cup",
        neutral=True,
    )
    (a,) = scores_from_match_rows([row])
    assert (a.home_team, a.home_score, a.away_score) == ("Alpha FC", 3, 0)
    (b,) = scores_from_international([intl])
    assert (b.away_team, b.home_score, b.away_score) == ("Wakanda", 1, 1)


def test_league_score_sources_maps_known_slugs_and_skips_unknown() -> None:
    sources = league_score_sources(["world-cup", "brazil-serie-a", "nba"])
    assert INTERNATIONAL in sources
    assert ScoreSource(kind="new_league", code="BRA") in sources
    assert len(sources) == 2  # nba has no free source -> manual settlement


def test_league_score_sources_maps_european_seasons() -> None:
    sources = league_score_sources(["england-premier-league"])
    assert sources == [ScoreSource(kind="season", code="E0")]


def test_league_score_sources_use_oddsharvester_registry_keys() -> None:
    # Map keys must be REAL OddsHarvester league keys (incl. our registered
    # extensions), or live config slugs never match and auto-settlement
    # silently skips the league.
    constants = pytest.importorskip(
        "oddsharvester.utils.sport_league_constants",
        reason="cross-checks the real registry — uv sync --extra backfill",
    )
    SPORTS_LEAGUES_URLS_MAPPING = constants.SPORTS_LEAGUES_URLS_MAPPING

    from app.ingestion.oddsportal import register_extra_leagues
    from app.settlement.results import _SLUG_SOURCES

    register_extra_leagues()  # production does this before any scrape
    football_keys = set()
    for sport, leagues in SPORTS_LEAGUES_URLS_MAPPING.items():
        if str(getattr(sport, "value", sport)) == "football":
            football_keys = set(leagues)
    unknown = set(_SLUG_SOURCES) - football_keys
    assert not unknown, f"slug map keys missing from OddsHarvester registry: {unknown}"
    # the user's target leagues all resolve to a results source
    targets = [
        "argentina-liga-profesional",
        "mexico-liga-mx",
        "brazil-serie-a",
        "netherlands-eredivisie",
        "belgium-jupiler-pro-league",
        "turkey-super-lig",
        "greece-super-league",
    ]
    assert len(league_score_sources(targets)) == len(targets)


NEW_LEAGUE_CSV = (
    "Country,League,Date,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA\n"
    "Brazil,Serie A,08/06/2026,Flamengo,Palmeiras,2,0,H,1.95,3.4,4.1\n"
    "Brazil,Serie A,09/06/2026,Santos,Gremio,1,1,D,2.5,3.1,2.9\n"
)
INTL_CSV = (
    "date,home_team,away_team,home_score,away_score,tournament,city,country,neutral\n"
    "2026-06-09,Atlantis,Wakanda,3,1,FIFA World Cup,Nicosia,Cyprus,TRUE\n"
    "1950-01-01,Oldland,Pastville,1,0,Friendly,X,Y,FALSE\n"
)


async def test_load_scores_fetches_mapped_sources_and_filters_by_date() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/new/BRA.csv"):
            return httpx.Response(200, text=NEW_LEAGUE_CSV)
        if "international_results" in str(request.url):
            return httpx.Response(200, text=INTL_CSV)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scores = await load_scores(
            client,
            slugs=["world-cup", "brazil-serie-a"],
            seasons=[],
            on_or_after=date(2026, 6, 1),
        )
    names = {(s.home_team, s.away_team) for s in scores}
    assert ("Flamengo", "Palmeiras") in names
    assert ("Atlantis", "Wakanda") in names
    assert ("Oldland", "Pastville") not in names  # filtered: before on_or_after


async def test_load_scores_survives_a_failing_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "international_results" in str(request.url):
            return httpx.Response(500)
        if request.url.path.endswith("/new/BRA.csv"):
            return httpx.Response(200, text=NEW_LEAGUE_CSV)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scores = await load_scores(
            client,
            slugs=["world-cup", "brazil-serie-a"],
            seasons=[],
            on_or_after=date(2026, 6, 1),
        )
    assert {s.home_team for s in scores} == {"Flamengo", "Santos"}


async def test_load_scores_survives_a_malformed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A PARSE error on one source (not just an HTTP error) must be isolated:
    # one malformed CSV cannot abort the whole settlement load. The Brazil
    # parser raises csv.Error mid-iteration; the international source must still
    # load.
    import csv as _csv

    from app.settlement import results as results_mod

    def _boom(_text: str) -> list[object]:
        raise _csv.Error("unterminated quote in CSV")

    monkeypatch.setattr(results_mod, "parse_new_league_csv", _boom)

    def handler(request: httpx.Request) -> httpx.Response:
        if "international_results" in str(request.url):
            return httpx.Response(200, text=INTL_CSV)
        if request.url.path.endswith("/new/BRA.csv"):
            return httpx.Response(200, text=NEW_LEAGUE_CSV)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scores = await results_mod.load_scores(
            client,
            slugs=["world-cup", "brazil-serie-a"],
            seasons=[],
            on_or_after=date(2026, 6, 1),
        )
    # international survived; the malformed Brazil source was skipped, not fatal
    assert {s.home_team for s in scores} == {"Atlantis"}


def test_league_score_sources_all_expands_to_every_source() -> None:
    # leagues="all" (league-less daily scraping) -> settlement must load
    # every free results source it knows, not zero.
    from app.settlement.results import _SLUG_SOURCES

    sources = league_score_sources(["all"])
    assert INTERNATIONAL in sources
    assert len(sources) == len({s for s in _SLUG_SOURCES.values()})
