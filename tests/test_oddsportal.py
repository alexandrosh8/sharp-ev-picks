"""OddsPortal adapter: OddsHarvester match dicts -> snapshots + directory.

Uses an injected fake scrape_fn — no oddsharvester import, no network.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal import OddsPortalLoader
from app.schemas.base import Market

MATCH = {
    "home_team": "Alpha FC",
    "away_team": "Beta United",
    "match_date": "2026-06-11",
    "league_name": "Testland League",
    "match_link": "https://www.oddsportal.com/football/testland/alpha-beta/",
    "scraped_date": "2026-06-10T12:00:00Z",
    "1x2_market": [
        {
            "1": "2.10",
            "X": "3.40",
            "2": "3.60",
            "bookmaker_name": "BookieOne",
            "period": "FullTime",
        },
        {
            "1": "2.05",
            "X": "3.45",
            "2": "3.70",
            "bookmaker_name": "BookieTwo",
            "period": "FullTime",
        },
    ],
    "over_under_2_5_market": [
        {
            "odds_over": "1.95",
            "odds_under": "1.95",
            "bookmaker_name": "BookieOne",
            "period": "FullTime",
        },
    ],
}


def make_loader(directory: EventDirectory, matches: list[dict[str, Any]]) -> OddsPortalLoader:
    async def fake_scrape(**kwargs: Any) -> Any:
        fake_scrape.calls.append(kwargs)  # type: ignore[attr-defined]
        return SimpleNamespace(success=matches, failed=[], partial=[])

    fake_scrape.calls = []  # type: ignore[attr-defined]
    return OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=fake_scrape,
    )


async def test_match_converts_to_snapshots_and_registers_teams() -> None:
    directory = EventDirectory()
    loader = make_loader(directory, [MATCH])
    snapshots = await loader.fetch_odds("soccer")

    # 2 bookmakers x 3 1x2 selections + 1 bookmaker x 2 totals = 8
    assert len(snapshots) == 8
    assert {s.market for s in snapshots} == {Market.H2H, Market.TOTALS}
    h2h_selections = {s.selection for s in snapshots if s.market is Market.H2H}
    assert h2h_selections == {"Alpha FC", "Draw", "Beta United"}
    totals = [s for s in snapshots if s.market is Market.TOTALS]
    assert {s.selection for s in totals} == {"Over 2.5", "Under 2.5"}
    assert all(s.decimal_odds > 1.0 for s in snapshots)

    teams = directory.lookup(str(MATCH["match_link"]))
    assert teams is not None
    assert teams.home == "Alpha FC"
    assert teams.away == "Beta United"


async def test_pacing_knobs_reach_scraper() -> None:
    # Upstream-sanctioned pacing config (concurrency/request_delay) must be
    # forwarded to OddsHarvester — silent defaults made cycle time untunable.
    async def fake_scrape(**kwargs: Any) -> Any:
        fake_scrape.calls.append(kwargs)  # type: ignore[attr-defined]
        return SimpleNamespace(success=[], failed=[], partial=[])

    fake_scrape.calls = []  # type: ignore[attr-defined]
    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=fake_scrape,
        concurrency_tasks=5,
        request_delay=0.8,
        locale="en-GB",
    )
    await loader.fetch_odds("soccer")
    (call,) = fake_scrape.calls  # type: ignore[attr-defined]
    assert call["concurrency_tasks"] == 5
    assert call["request_delay"] == 0.8
    # Coherent human fingerprint: locale forwarded, paired with UTC timezone.
    assert call["browser_locale_timezone"] == "en-GB"
    assert call["browser_timezone_id"] == "UTC"


async def test_unknown_sport_key_returns_empty() -> None:
    loader = make_loader(EventDirectory(), [MATCH])
    assert await loader.fetch_odds("basketball_nba") == []


async def test_malformed_entries_are_skipped() -> None:
    bad = dict(MATCH)
    bad["1x2_market"] = [
        {"1": "not-a-number", "X": "", "2": None, "bookmaker_name": "BadBook"},
        "garbage-entry",
    ]
    bad["over_under_2_5_market"] = []
    loader = make_loader(EventDirectory(), [bad])
    snapshots = await loader.fetch_odds("soccer")
    assert snapshots == []


async def test_match_without_teams_is_skipped() -> None:
    loader = make_loader(EventDirectory(), [{"home_team": "", "away_team": "X"}])
    assert await loader.fetch_odds("soccer") == []


def test_unsupported_market_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        OddsPortalLoader(
            directory=EventDirectory(),
            leagues_by_sport_key={},
            markets=("1x2", "correct_score_9_9"),
        )


def test_push_bearing_handicap_lines_rejected() -> None:
    # Integer/quarter AH lines carry PUSH outcomes (probs do not sum to 1) —
    # direct devig is invalid; only half-lines are accepted.
    for bad in ("asian_handicap_-1", "asian_handicap_+0_25", "asian_handicap_-1_75"):
        with pytest.raises(ValueError, match="half line"):
            OddsPortalLoader(
                directory=EventDirectory(),
                leagues_by_sport_key={},
                markets=("1x2", bad),
            )
    # half lines and European (3-way) handicaps are sound full markets
    OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={},
        markets=("asian_handicap_-1_5", "asian_handicap_games_-7_5_games", "european_handicap_-1"),
    )


def test_multiple_totals_lines_allowed_via_detail_grouping() -> None:
    # distinct lines group separately by market_detail — no devig mixing.
    OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={},
        markets=("over_under_2_5", "over_under_3_5"),
    )


async def test_football_extended_markets_parse() -> None:
    match = dict(MATCH)
    match["btts_market"] = [
        {"btts_yes": "1.80", "btts_no": "2.00", "bookmaker_name": "BookieOne"},
    ]
    match["dnb_market"] = [
        {"dnb_team1": "1.65", "dnb_team2": "2.30", "bookmaker_name": "BookieOne"},
    ]
    match["double_chance_market"] = [
        {"1X": "1.30", "12": "1.40", "X2": "1.85", "bookmaker_name": "BookieOne"},
    ]
    directory = EventDirectory()
    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        markets=("1x2", "over_under_2_5", "btts", "dnb", "double_chance"),
        scrape_fn=make_loader(directory, [match])._scrape,
    )
    snapshots = await loader.fetch_odds("soccer")
    by_market = {(s.market, s.selection) for s in snapshots}
    assert (Market.BTTS, "BTTS Yes") in by_market
    assert (Market.DNB, "Alpha FC") in by_market
    assert (Market.DNB, "Beta United") in by_market
    assert (Market.DOUBLE_CHANCE, "Alpha FC or Draw") in by_market
    assert (Market.DOUBLE_CHANCE, "Alpha FC or Beta United") in by_market
    assert (Market.DOUBLE_CHANCE, "Draw or Beta United") in by_market


async def test_handicap_markets_parse_with_line_in_selection_and_detail() -> None:
    match = dict(MATCH)
    match["asian_handicap_-1_5_market"] = [
        {"team1_handicap": "2.10", "team2_handicap": "1.75", "bookmaker_name": "BookieOne"},
    ]
    match["european_handicap_-1_market"] = [
        {
            "team1_handicap": "2.60",
            "draw_handicap": "3.50",
            "team2_handicap": "2.40",
            "bookmaker_name": "BookieOne",
        },
    ]
    directory = EventDirectory()
    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        markets=("asian_handicap_-1_5", "european_handicap_-1"),
        scrape_fn=make_loader(directory, [match])._scrape,
    )
    snapshots = await loader.fetch_odds("soccer")
    triples = {(s.market, s.selection, s.market_detail) for s in snapshots}
    assert (Market.SPREADS, "Alpha FC -1.5", "asian_handicap_-1_5") in triples
    assert (Market.SPREADS, "Beta United +1.5", "asian_handicap_-1_5") in triples
    assert (Market.SPREADS, "Alpha FC -1", "european_handicap_-1") in triples
    assert (Market.SPREADS, "Draw (-1)", "european_handicap_-1") in triples
    assert (Market.SPREADS, "Beta United +1", "european_handicap_-1") in triples


async def test_basketball_home_away_parses_as_two_way_h2h() -> None:
    match = {
        "home_team": "Test Hawks",
        "away_team": "Test Bulls",
        "match_date": "2026-06-12 01:00:00 UTC",
        "league_name": "NBA",
        "match_link": "https://www.oddsportal.com/basketball/usa/nba/hawks-bulls/",
        "scraped_date": "2026-06-10T12:00:00Z",
        "home_away_market": [
            {"1": "2.80", "2": "1.42", "bookmaker_name": "BookieOne", "period": "FullIncludingOT"},
        ],
    }

    async def fake_scrape(**kwargs: Any) -> Any:
        return SimpleNamespace(success=[match], failed=[], partial=[])

    directory = EventDirectory()
    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"basketball": ("basketball", ["nba"])},
        markets_by_sport_key={"basketball": ("home_away",)},
        scrape_fn=fake_scrape,
    )
    snapshots = await loader.fetch_odds("basketball")
    assert {(s.market, s.selection) for s in snapshots} == {
        (Market.H2H, "Test Hawks"),
        (Market.H2H, "Test Bulls"),
    }
    teams = directory.lookup(str(match["match_link"]))
    assert teams is not None
    assert teams.starts_at is not None  # kickoff parsed from match_date


async def test_days_ahead_scrapes_dated_pages_and_dedupes() -> None:
    # days_ahead=1 -> one scrape per date (today, tomorrow, UTC) so cycles
    # cover exactly the actionable games; the same match appearing on both
    # date pages must not double its snapshots.
    from datetime import UTC, datetime, timedelta

    dates_called: list[Any] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        dates_called.append(kwargs.get("date"))
        return SimpleNamespace(success=[MATCH], failed=[], partial=[])

    directory = EventDirectory()
    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland"])},
        scrape_fn=fake_scrape,
        days_ahead=1,
    )
    snapshots = await loader.fetch_odds("soccer")

    now = datetime.now(tz=UTC)
    assert dates_called == [
        now.strftime("%Y%m%d"),
        (now + timedelta(days=1)).strftime("%Y%m%d"),
    ]
    single = make_loader(EventDirectory(), [MATCH])
    baseline = await single.fetch_odds("soccer")
    assert len(snapshots) == len(baseline)  # deduped by match_link


async def test_days_ahead_none_keeps_general_upcoming_page() -> None:
    dates_called: list[Any] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        dates_called.append(kwargs.get("date"))
        return SimpleNamespace(success=[], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland"])},
        scrape_fn=fake_scrape,
    )
    await loader.fetch_odds("soccer")
    assert dates_called == [None]


async def test_all_leagues_sentinel_scrapes_daily_page_without_league_filter() -> None:
    # leagues=["all"] -> league-less dated scrape: oddsportal's daily
    # matches page covers EVERY league that day (user: no league filter).
    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(success=[MATCH], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["all"])},
        scrape_fn=fake_scrape,
        days_ahead=1,
    )
    snapshots = await loader.fetch_odds("soccer")
    assert len(calls) == 2  # today + tomorrow
    assert all(c["leagues"] is None for c in calls)
    assert all(c["date"] is not None for c in calls)
    assert snapshots  # MATCH converted normally


def test_all_leagues_requires_dated_scraping() -> None:
    # The league-less daily URL needs a date; without days_ahead/date the
    # config is a footgun and must fail at construction.
    with pytest.raises(ValueError):
        OddsPortalLoader(
            directory=EventDirectory(),
            leagues_by_sport_key={"soccer": ("football", ["all"])},
        )


async def test_fetch_match_odds_scrapes_specific_links_for_own_sport() -> None:
    # Open picks outside the dated window are re-priced via their match
    # pages directly; links from other sports are filtered out.
    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(success=[MATCH], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["all"])},
        scrape_fn=fake_scrape,
        days_ahead=1,
    )
    links = [
        "https://www.oddsportal.com/football/world/world-cup/a-vs-b/XYZ/",
        "https://www.oddsportal.com/basketball/usa/nba/c-vs-d/QRS/",
    ]
    snapshots = await loader.fetch_match_odds("soccer", links)
    assert len(calls) == 1
    assert calls[0]["match_links"] == [links[0]]  # basketball link filtered
    assert "leagues" not in calls[0] or calls[0].get("leagues") is None
    assert snapshots  # MATCH converted normally


async def test_fetch_match_odds_no_matching_links_skips_scrape() -> None:
    async def fake_scrape(**kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("should not scrape")

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["all"])},
        scrape_fn=fake_scrape,
        days_ahead=1,
    )
    assert await loader.fetch_match_odds("soccer", ["https://x/basketball/y/"]) == []
