"""OddsPortal adapter: OddsHarvester match dicts -> snapshots + directory.

Uses an injected fake scrape_fn — no oddsharvester import, no network.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal import (
    OddsPortalLoader,
    _line_from_key,
    _market_for_key,
    _parse_score,
    _selections,
)
from app.schemas.base import Market

# The exact config-default market lists (class defaults — no .env read):
# every key must validate at loader construction and map to the expected
# canonical market + outcome layout. Config drift breaks here, loudly.
CONFIGURED_FOOTBALL_KEYS = tuple(
    Settings.model_fields["oddsportal_football_markets"].default.split(",")
)
CONFIGURED_BASKETBALL_KEYS = tuple(
    Settings.model_fields["oddsportal_basketball_markets"].default.split(",")
)

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


async def test_zero_snapshots_with_matches_warns_and_records_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matches listed but ZERO odds rows parsed = selector/DOM break or
    anti-bot wall (0 rows + 0 parse errors -> suspect anti-bot). The cycle
    still completes, so this must be LOUD (WARNING) and the listing count
    must be recorded for the poll-liveness payload."""
    import logging

    bare = {k: v for k, v in MATCH.items() if not str(k).endswith("_market")}
    loader = make_loader(EventDirectory(), [bare])
    with caplog.at_level(logging.INFO, logger="app.ingestion.oddsportal"):
        snapshots = await loader.fetch_odds("soccer")
    assert snapshots == []
    assert loader.last_fetch_matches["soccer"] == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("0 odds snapshots" in r.getMessage() for r in warnings)

    # healthy fetch: counts recorded, NO warning
    caplog.clear()
    loader_ok = make_loader(EventDirectory(), [MATCH])
    with caplog.at_level(logging.INFO, logger="app.ingestion.oddsportal"):
        assert await loader_ok.fetch_odds("soccer")
    assert loader_ok.last_fetch_matches["soccer"] == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    # zero matches (empty slate / listing-level block) is NOT this warning's
    # job — the matches_found=0 liveness record covers it
    caplog.clear()
    loader_empty = make_loader(EventDirectory(), [])
    with caplog.at_level(logging.INFO, logger="app.ingestion.oddsportal"):
        assert await loader_empty.fetch_odds("soccer") == []
    assert loader_empty.last_fetch_matches["soccer"] == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


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


async def test_fetch_match_odds_trims_markets_to_configured_intersection() -> None:
    # Off-window revalidation passes only the markets its open picks need —
    # each key costs one tab per match page. The trim may only SELECT from
    # the validated configured list: unknown keys are dropped, and an empty
    # intersection falls back to the full list (never worse coverage).
    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(success=[MATCH], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["all"])},
        markets=("1x2", "over_under_2_5"),
        scrape_fn=fake_scrape,
        days_ahead=1,
    )
    link = "https://www.oddsportal.com/football/world/world-cup/a-vs-b/XYZ/"

    await loader.fetch_match_odds("soccer", [link], markets=["1x2", "asian_handicap_-1_5"])
    assert calls[-1]["markets"] == ["1x2"]  # unknown-to-config key dropped

    await loader.fetch_match_odds("soccer", [link], markets=["not_configured"])
    assert calls[-1]["markets"] == ["1x2", "over_under_2_5"]  # fallback: full list

    await loader.fetch_match_odds("soccer", [link])  # no trim requested
    assert calls[-1]["markets"] == ["1x2", "over_under_2_5"]


def test_normalize_match_link_strips_inplay_segment() -> None:
    from app.ingestion.oddsportal import normalize_match_link

    # the live fork shape: same fixture, same #fragment, extra path segment
    assert (
        normalize_match_link(
            "https://www.oddsportal.com/basketball/x/team-hjC2gcCJ/inplay-odds/#x4T1bBXi"
        )
        == "https://www.oddsportal.com/basketball/x/team-hjC2gcCJ/#x4T1bBXi"
    )
    assert (
        normalize_match_link("https://www.oddsportal.com/football/a/b-vs-c/inplay-odds")
        == "https://www.oddsportal.com/football/a/b-vs-c"
    )
    # only the exact path segment is stripped — never a slug containing it
    untouched = "https://www.oddsportal.com/football/a/inplay-oddsmatch/b/"
    assert normalize_match_link(untouched) == untouched


async def test_inplay_url_fork_collapses_to_one_event() -> None:
    """The same fixture listed under BOTH its pre-match URL and the
    '/inplay-odds' fork must become ONE event under the pre-match ref —
    two events meant double premium exposure, a forked snapshot history,
    and a tier dedupe blind spot (live picks 2026-06-12)."""
    base_link = "https://www.oddsportal.com/football/testland/alpha-beta/#frag1"
    fork = dict(MATCH)
    fork["match_link"] = (
        "https://www.oddsportal.com/football/testland/alpha-beta/inplay-odds/#frag1"
    )
    base = dict(MATCH)
    base["match_link"] = base_link

    directory = EventDirectory()
    loader = make_loader(directory, [base, fork])
    snapshots = await loader.fetch_odds("soccer")

    assert {s.event_id for s in snapshots} == {base_link}
    assert len(snapshots) == 8  # the fork deduped at listing time, not doubled
    assert loader.last_fetch_matches["soccer"] == 1
    assert directory.lookup(base_link) is not None


def _expected_market_and_outcomes(key: str) -> tuple[Market, int]:
    """Doctrine layout per market family: 2-way vs 3-way full outcome sets."""
    exact = {
        "1x2": (Market.H2H, 3),
        "home_away": (Market.H2H, 2),
        "btts": (Market.BTTS, 2),
        "dnb": (Market.DNB, 2),
        "double_chance": (Market.DOUBLE_CHANCE, 3),
    }
    if key in exact:
        return exact[key]
    if key.startswith("over_under_"):
        return Market.TOTALS, 2
    if key.startswith("european_handicap_"):
        return Market.SPREADS, 3
    if key.startswith("asian_handicap_"):
        return Market.SPREADS, 2
    raise AssertionError(f"unexpected configured key: {key}")


@pytest.mark.parametrize("key", CONFIGURED_FOOTBALL_KEYS + CONFIGURED_BASKETBALL_KEYS)
def test_every_configured_default_market_key_validates_and_maps(key: str) -> None:
    # (b) passes loader validation at construction...
    OddsPortalLoader(directory=EventDirectory(), leagues_by_sport_key={}, markets=(key,))
    # ...with the expected canonical market and outcome layout.
    expected_market, n_outcomes = _expected_market_and_outcomes(key)
    assert _market_for_key(key) is expected_market
    selections = _selections(key, "Alpha", "Beta")
    assert len(selections) == n_outcomes
    assert len({label for label, _ in selections}) == n_outcomes
    assert len({sel for _, sel in selections}) == n_outcomes
    if key.startswith(("over_under_", "asian_handicap_", "european_handicap_")):
        line = _line_from_key(key)
        assert line is not None
        # Every line-bearing selection embeds its line, so distinct lines of
        # one family can never collide in (event, market, selection) keys
        # (picks dedupe/supersede/revalidation all key on selection).
        assert all(f"{abs(line):g}" in sel for _, sel in selections)
    if key.startswith("asian_handicap"):
        line = _line_from_key(key)
        assert line is not None
        assert abs(line % 1.0) == 0.5  # half-line: no push outcome


def test_configured_basketball_games_suffix_lines_parse() -> None:
    # The basketball AH key format carries a _games SUFFIX too — the line
    # parser must strip both ends.
    assert _line_from_key("asian_handicap_games_-10_5_games") == -10.5
    assert _line_from_key("asian_handicap_games_+1_5_games") == 1.5
    assert _line_from_key("over_under_games_245_5") == 245.5


@pytest.mark.parametrize(
    "bad_key",
    [
        "asian_handicap_-2",  # integer line, football
        "asian_handicap_+1_25",  # quarter line, football
        "asian_handicap_0",  # zero line (pure push-shape)
        "asian_handicap_games_-7_games",  # integer line, basketball suffix format
        "asian_handicap_games_+2_25_games",  # quarter line, basketball suffix format
    ],
)
def test_push_bearing_handicap_lines_rejected_in_both_key_formats(bad_key: str) -> None:
    # Same gate as test_push_bearing_handicap_lines_rejected, extended over
    # the expanded config families incl. the basketball _games-suffix format.
    with pytest.raises(ValueError, match="half line"):
        OddsPortalLoader(
            directory=EventDirectory(),
            leagues_by_sport_key={},
            markets=(bad_key,),
        )


async def test_basketball_totals_and_handicap_games_markets_parse() -> None:
    match = {
        "home_team": "Test Hawks",
        "away_team": "Test Bulls",
        "match_date": "2026-06-12 01:00:00 UTC",
        "league_name": "NBA",
        "match_link": "https://www.oddsportal.com/basketball/usa/nba/hawks-bulls/",
        "scraped_date": "2026-06-10T12:00:00Z",
        "over_under_games_220_5_market": [
            {"odds_over": "1.90", "odds_under": "1.92", "bookmaker_name": "BookieOne"},
        ],
        "asian_handicap_games_-7_5_games_market": [
            {"handicap_team_1": "1.88", "handicap_team_2": "1.94", "bookmaker_name": "BookieOne"},
        ],
    }

    async def fake_scrape(**kwargs: Any) -> Any:
        return SimpleNamespace(success=[match], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"basketball": ("basketball", ["nba"])},
        markets_by_sport_key={
            "basketball": ("over_under_games_220_5", "asian_handicap_games_-7_5_games"),
        },
        scrape_fn=fake_scrape,
    )
    snapshots = await loader.fetch_odds("basketball")
    triples = {(s.market, s.selection, s.market_detail) for s in snapshots}
    assert triples == {
        (Market.TOTALS, "Over 220.5", "over_under_games_220_5"),
        (Market.TOTALS, "Under 220.5", "over_under_games_220_5"),
        (Market.SPREADS, "Test Hawks -7.5", "asian_handicap_games_-7_5_games"),
        (Market.SPREADS, "Test Bulls +7.5", "asian_handicap_games_-7_5_games"),
    }


async def test_proxy_pool_rotates_and_fails_over() -> None:
    # Proxy #0 returns 0 matches (the throttle signature) -> failover to #1,
    # which returns the slate. Credentials must travel via the separate
    # proxy_user/proxy_pass kwargs, NEVER embedded in proxy_url.
    from app.ingestion.base import ScraperProxy

    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        success = [] if len(calls) == 1 else [MATCH]
        return SimpleNamespace(success=success, failed=[], partial=[])

    pool = (
        ScraperProxy(url="http://h0:1", username="u0", password="p0"),
        ScraperProxy(url="http://h1:2", username="u1", password="p1"),
    )
    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=fake_scrape,
        proxy_pool=pool,
    )
    snapshots = await loader.fetch_odds("soccer")
    assert len(calls) == 2
    assert calls[0]["proxy_url"] == "http://h0:1"
    assert calls[0]["proxy_user"] == "u0"
    assert calls[0]["proxy_pass"] == "p0"
    assert "u0" not in calls[0]["proxy_url"]  # creds never live in the URL
    assert calls[1]["proxy_url"] == "http://h1:2"
    assert snapshots  # the 2nd proxy's slate was parsed


async def test_empty_pool_scrapes_without_proxy_kwargs() -> None:
    # Default (no pool) -> exactly one scrape, no proxy_* kwargs (host IP).
    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(success=[MATCH], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=fake_scrape,
    )
    await loader.fetch_odds("soccer")
    (call,) = calls
    assert "proxy_url" not in call
    assert "proxy_user" not in call
    assert "proxy_pass" not in call


async def test_proxy_failover_capped_on_empty_slate() -> None:
    # A genuinely-empty slate (every proxy returns 0 matches) must NOT burn the
    # whole pool — the failover sweep is capped so empty/slow sports can't starve
    # the rest of the scrape cycle.
    from app.ingestion.base import ScraperProxy
    from app.ingestion.oddsportal import _MAX_PROXY_FAILOVER

    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(success=[], failed=[], partial=[])  # always empty

    pool = tuple(
        ScraperProxy(url=f"http://h{i}:1", username=f"u{i}", password=f"p{i}") for i in range(8)
    )
    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=fake_scrape,
        proxy_pool=pool,
    )
    snaps = await loader.fetch_odds("soccer")
    assert snaps == []
    assert len(calls) == _MAX_PROXY_FAILOVER  # capped, NOT all 8 proxies


# ---------------------------------------------------------------------------
# Best-effort scraped final score (convenience settle-prompt pre-fill)
# ---------------------------------------------------------------------------


def test_parse_score_digits_only() -> None:
    # Finished match: upstream emits the score as a digit string.
    assert _parse_score("2") == 2
    assert _parse_score("0") == 0
    assert _parse_score("12") == 12
    assert _parse_score(" 3 ") == 3  # surrounding whitespace tolerated
    # Not-yet-finished / non-numeric: never guess a score.
    assert _parse_score(None) is None
    assert _parse_score("") is None
    assert _parse_score("-") is None
    assert _parse_score("?") is None
    assert _parse_score("2-1") is None  # a combined string is not a single score
    assert _parse_score("1.5") is None


async def test_convert_match_captures_scraped_score() -> None:
    # A post-finish scrape carries home_score/away_score as digit strings; they
    # land on the event's directory context (the seam threaded to the upsert).
    directory = EventDirectory()
    finished = {**MATCH, "home_score": "2", "away_score": "1"}
    loader = make_loader(directory, [finished])
    await loader.fetch_odds("soccer")

    teams = directory.lookup(str(MATCH["match_link"]))
    assert teams is not None
    assert teams.home_score == 2
    assert teams.away_score == 1


async def test_convert_match_ignores_unfinished_score() -> None:
    # Pre-kickoff / in-play scrape: no usable score -> None (the common case;
    # the settle prompt then has nothing to pre-fill and the user types it).
    directory = EventDirectory()
    unfinished = {**MATCH, "home_score": "", "away_score": "-"}
    loader = make_loader(directory, [unfinished])
    await loader.fetch_odds("soccer")

    teams = directory.lookup(str(MATCH["match_link"]))
    assert teams is not None
    assert teams.home_score is None
    assert teams.away_score is None

    # The bare MATCH carries no score keys at all -> also None.
    directory2 = EventDirectory()
    await make_loader(directory2, [MATCH]).fetch_odds("soccer")
    teams2 = directory2.lookup(str(MATCH["match_link"]))
    assert teams2 is not None
    assert teams2.home_score is None
    assert teams2.away_score is None
