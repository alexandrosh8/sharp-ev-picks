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


def test_lookup_prefers_exact_date_over_adjacent() -> None:
    # Back-to-back pairing: yesterday's final must NEVER shadow today's game.
    book = ScoreBook(
        [
            FinalScore("Alpha FC", "Beta United", date(2026, 6, 8), 5, 0),
            FinalScore("Alpha FC", "Beta United", date(2026, 6, 9), 2, 1),
        ]
    )
    found = book.lookup("Alpha FC", "Beta United", datetime(2026, 6, 9, 18, 0, tzinfo=UTC))
    assert found is not None
    assert (found.home_score, found.away_score) == (2, 1)  # the exact-date game


def test_lookup_refuses_ambiguous_adjacent_exact_hits() -> None:
    # NBA-style back-to-back: the same pairing played on D-1 AND D+1 with no
    # entry on the pick's own date — either could be "the" game, so refuse
    # (the pick stays open and settles once the exact-date score lands).
    book = ScoreBook(
        [
            FinalScore("Alpha FC", "Beta United", date(2026, 6, 8), 5, 0),
            FinalScore("Alpha FC", "Beta United", date(2026, 6, 10), 2, 1),
        ]
    )
    assert book.lookup("Alpha FC", "Beta United", datetime(2026, 6, 9, 18, 0, tzinfo=UTC)) is None


def test_lookup_containment_prefers_exact_date_and_refuses_adjacent_ambiguity() -> None:
    # The containment fallback follows the same date discipline as exact hits.
    exact_day = ScoreBook(
        [
            fs(home="Flamengo", away="Palmeiras", d=date(2026, 6, 8)),
            FinalScore("Flamengo", "Palmeiras", date(2026, 6, 9), 3, 3),
        ]
    )
    found = exact_day.lookup("Flamengo RJ", "Palmeiras", datetime(2026, 6, 9, 22, 0, tzinfo=UTC))
    assert found is not None
    assert (found.home_score, found.away_score) == (3, 3)  # exact date wins
    both_adjacent = ScoreBook(
        [
            fs(home="Flamengo", away="Palmeiras", d=date(2026, 6, 8)),
            fs(home="Flamengo", away="Palmeiras", d=date(2026, 6, 10)),
        ]
    )
    assert (
        both_adjacent.lookup("Flamengo RJ", "Palmeiras", datetime(2026, 6, 9, 22, 0, tzinfo=UTC))
        is None
    )


def test_lookup_unique_containment_fallback() -> None:
    # OddsPortal says "Flamengo RJ"; football-data says "Flamengo".
    book = ScoreBook([fs(home="Flamengo", away="Palmeiras")])
    found = book.lookup("Flamengo RJ", "Palmeiras", datetime(2026, 6, 9, 22, 0, tzinfo=UTC))
    assert found is not None


def test_lookup_ambiguous_containment_returns_none() -> None:
    # Two source entries both containment-match the pick name WITH conflicting
    # scores -> refuse, stay open. (Identical payloads are NOT ambiguous —
    # grading is the same either way — see test_results_ambiguous_dedup.py.)
    same_day = [
        fs(home="Santos FC", away="Palmeiras"),
        FinalScore("Santos Laguna", "Palmeiras", date(2026, 6, 9), 0, 3),
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
    "2026-06-09,Atlantis,Wakanda,3,1,Friendly,Nicosia,Cyprus,TRUE\n"
    # ET-capable knockout competition: the score may include extra time, so the
    # settlement load must EXCLUDE it (90-minute markets would settle wrong).
    "2026-06-09,Etland,Penaltia,2,1,FIFA World Cup,Dallas,United States,TRUE\n"
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
    # ET-capable knockout tournament: score may include extra time -> excluded
    assert ("Etland", "Penaltia") not in names


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


# --- 2026-08-07 backlog audit: cross-source name forms + season rollover -----


def test_normalize_team_transliterates_non_decomposable_chars() -> None:
    """ø/æ/đ/þ/ł have NO NFKD ASCII decomposition — they were silently DROPPED,
    so ESPN "FC Nordsjælland" could never match OddsChecker "FC Nordsjaelland"
    (94 Conference-qual picks stuck unsettled, audit 2026-08-07)."""
    assert normalize_team("FC Nordsjælland") == "fc nordsjaelland"
    assert normalize_team("Tromsø") == "tromso"
    assert normalize_team("Łódź") == "lodz"
    assert normalize_team("Þór Akureyri") == "thor akureyri"
    assert normalize_team("Đurđevac") == "durdevac"


def test_normalize_team_applies_explicit_settlement_aliases() -> None:
    """Exonym/nickname pairs that no token rule can bridge live in the explicit
    alias table (one club per row, evidence: 2026-08-07 backlog vs live ESPN
    payloads). Both sides of a lookup normalize through the same table."""
    assert normalize_team("F.C. København") == normalize_team("FC Copenhagen")
    assert normalize_team("Heart of Midlothian") == normalize_team("Hearts")
    assert normalize_team("Red Star Belgrade") == normalize_team("Crvena zvezda")
    assert normalize_team("The New Saints") == normalize_team("TNS")
    assert normalize_team("Din Tbilisi") == normalize_team("Dinamo Tbilisi")
    assert normalize_team("Dinamo City") == normalize_team("Dinamo Tirana")
    assert normalize_team("ML Vitebsk") == normalize_team("BC Maxline")
    assert normalize_team("Red Bull New York") == normalize_team("New York Red Bulls")
    assert normalize_team("Abroath") == normalize_team("Arbroath")
    assert normalize_team("Hapoel Be'er") == normalize_team("Hapoel Beer Sheva")
    assert normalize_team("Pafos") == normalize_team("AEP Paphos")
    assert normalize_team("Botic Van de Zandschulo") == normalize_team("Botic van de Zandschulp")


def test_alias_does_not_bleed_onto_marked_teams() -> None:
    """The alias table maps FULL normalized names only — a marked variant
    ("Heart of Midlothian W") must NOT alias onto the senior side."""
    assert normalize_team("Heart of Midlothian W") == "heart of midlothian w"


def test_names_match_canonicalizes_utd_and_queens_spellings() -> None:
    from app.settlement.results import _names_match

    def m(a: str, b: str) -> bool:
        return _names_match(normalize_team(a), normalize_team(b))

    assert m("Dundee Utd", "Dundee United")  # 5 Scottish Prem picks, 2026-08-07
    assert m("Minnesota Utd", "Minnesota United FC")  # 15 MLS picks
    assert m("Queens Park FC", "Queen's Park")  # 11 Scottish League Cup picks
    assert m("Queen of South", "Queen of the South")
    assert not m("Manchester United", "Manchester City")
    assert not m("Newcastle Utd", "Newcastle Jets")  # distinct clubs stay distinct


def test_lookup_recovers_espn_name_forms_but_keeps_marker_veto() -> None:
    """End-to-end: an ESPN-named final settles the OddsChecker-named fixture;
    the women/youth marker veto stays authoritative over any alias/containment."""
    d = date(2026, 8, 6)
    book = ScoreBook(
        [
            FinalScore("Valur Reykjavik", "FC Nordsjælland", d, 0, 2),
            FinalScore("Benfica", "Heart of Midlothian", d, 6, 1),
        ]
    )
    kickoff = datetime(2026, 8, 6, 18, 30, tzinfo=UTC)
    hit = book.lookup("Valur", "FC Nordsjaelland", kickoff)
    assert hit is not None and (hit.home_score, hit.away_score) == (0, 2)
    assert book.lookup("Benfica", "Hearts", kickoff) is not None
    # marker veto: a women's pick must not settle from the senior final
    assert book.lookup("Benfica W", "Hearts W", kickoff) is None


def test_current_season_code_rolls_in_july() -> None:
    from app.settlement.results import current_season_code

    assert current_season_code(date(2026, 8, 7)) == "2627"
    assert current_season_code(date(2026, 7, 1)) == "2627"
    assert current_season_code(date(2026, 6, 30)) == "2526"
    assert current_season_code(date(2025, 12, 31)) == "2526"


def test_slug_sources_cover_scottish_lower_leagues() -> None:
    from app.settlement.results import _SLUG_SOURCES

    assert _SLUG_SOURCES["scotland-league-one"] == ScoreSource(kind="season", code="SC2")
    assert _SLUG_SOURCES["scotland-league-two"] == ScoreSource(kind="season", code="SC3")


SEASON_CSV_2526 = (
    "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\nSC0,10/05/2026,Old Season FC,Stale Rovers,1,0,H\n"
)
SEASON_CSV_2627 = (
    "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\nSC0,01/08/2026,Dundee United,Rangers,2,2,D\n"
)


async def test_load_scores_auto_appends_current_season_and_tolerates_404() -> None:
    """FOOTBALLDATA_SEASONS rots at season rollover (=2425,2526 while the
    2026-27 SC0 file carries the new Scottish Premiership results — 43 picks
    stuck, audit 2026-08-07). load_scores must fetch the CURRENT season code
    too, and a 404 on a not-yet-published season file must skip quietly
    without killing the source's other seasons."""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fetched.append(path)
        if path.endswith("/2627/SC0.csv"):
            return httpx.Response(200, text=SEASON_CSV_2627)
        if path.endswith("/2526/SC0.csv"):
            return httpx.Response(200, text=SEASON_CSV_2526)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scores = await load_scores(
            client,
            slugs=["scotland-premiership"],
            seasons=["2425", "2526"],  # stale config: current season absent
            on_or_after=date(2026, 7, 24),
        )
    assert any(p.endswith("/2627/SC0.csv") for p in fetched)  # auto-appended
    assert any(p.endswith("/2425/SC0.csv") for p in fetched)  # 404 tolerated
    names = {(s.home_team, s.away_team) for s in scores}
    assert ("Dundee United", "Rangers") in names
