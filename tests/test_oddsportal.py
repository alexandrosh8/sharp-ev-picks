"""OddsPortal adapter: OddsHarvester match dicts -> snapshots + directory.

Uses an injected fake scrape_fn — no oddsharvester import, no network.
"""

import asyncio
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


def test_coerce_finished_status() -> None:
    """isFinished / eventStageId==3 / 'Finished' => True; an in-play 2nd-half
    (stage 13) carrying a populated PARTIAL score => False (must never settle);
    no status fields => None (caller falls back to the conservative time-floor)."""
    from app.ingestion.oddsportal import _coerce_finished

    # explicit finished signals
    assert _coerce_finished(True, None, None) is True
    assert _coerce_finished(None, 3, "Finished") is True
    assert _coerce_finished(None, None, "Finished") is True
    # present-but-not-finished: scheduled, and (critically) IN-PLAY -> reject
    assert _coerce_finished(False, 1, "Scheduled") is False
    assert _coerce_finished(False, 13, "2nd Half") is False  # live partial score
    # no status at all (obscure league / dehydrated page) -> None -> floor fallback
    assert _coerce_finished(None, None, None) is None
    assert _coerce_finished(None, None, "") is None


def test_event_finished_fields_parses_status() -> None:
    """Pull isFinished / eventStageId / eventStageName from the react-event-header
    JSON; return {} when the header/JSON is absent (-> _coerce_finished None ->
    time-floor fallback)."""
    from app.ingestion.oddsportal import _event_finished_fields

    finished = (
        '<div id="react-event-header" data=\''
        '{"eventBody": {"eventStageId": 3, "eventStageName": "Finished"},'
        ' "eventData": {"isFinished": true, "countryName": "Ethiopia"}}'
        "'></div>"
    )
    assert _event_finished_fields(finished) == {
        "is_finished": True,
        "event_stage_id": 3,
        "event_stage_name": "Finished",
        "country_name": "Ethiopia",  # feature A: dated-page country capture
    }

    inplay = (
        '<div id="react-event-header" data=\''
        '{"eventBody": {"eventStageId": 13, "eventStageName": "2nd Half"},'
        ' "eventData": {"isFinished": false}}'
        "'></div>"
    )
    assert _event_finished_fields(inplay) == {
        "is_finished": False,
        "event_stage_id": 13,
        "event_stage_name": "2nd Half",
        "country_name": None,  # absent in eventData -> None
    }

    assert _event_finished_fields("<div>no header here</div>") == {}
    assert _event_finished_fields('<div id="react-event-header" data="not json"></div>') == {}


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


def test_push_bearing_over_under_lines_rejected() -> None:
    # Integer/quarter totals carry a PUSH outcome (exact total) — direct devig is
    # invalid; only half-lines (1.5, 2.5, …) are accepted (audit #6, 2026-06-21).
    for bad in ("over_under_2", "over_under_2_25", "over_under_3_75"):
        with pytest.raises(ValueError, match="half line"):
            OddsPortalLoader(
                directory=EventDirectory(),
                leagues_by_sport_key={},
                markets=("1x2", bad),
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


# ---------------------------------------------------------------------------
# Per-cycle scrape watchdog (cactusbets.cloud prod fix): a hung Over/Under
# extraction (PageScroller 20s timeout per missing sub-line, 52x across the
# slate) made poll_odds run forever, so every later interval slot skipped with
# "maximum number of running instances reached" and settle_results never ran.
# A bounded asyncio.wait_for at OUR scrape boundary ends the cycle cleanly.
# ---------------------------------------------------------------------------


async def test_fetch_odds_watchdog_cancels_a_hung_scrape() -> None:
    # A scrape that never returns (the Over/Under wedge) must be cancelled by
    # the per-cycle timeout so fetch_odds returns cleanly instead of running
    # forever. The hung coroutine is cancelled, not left dangling.
    import asyncio

    cancelled = asyncio.Event()

    async def hung_scrape(**kwargs: Any) -> Any:
        try:
            await asyncio.Event().wait()  # never returns
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return SimpleNamespace(success=[MATCH], failed=[], partial=[])  # pragma: no cover

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=hung_scrape,
        cycle_timeout_seconds=0.05,
    )
    snapshots = await loader.fetch_odds("soccer")
    assert snapshots == []  # cycle ended cleanly, no wedge
    assert cancelled.is_set()  # the hung scrape was actually cancelled
    assert loader.last_fetch_matches["soccer"] == 0


async def test_fetch_odds_watchdog_does_not_leak_secrets_on_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Secret hygiene: a timeout must log type-only, never the URL/proxy creds.
    import asyncio
    import logging

    async def hung_scrape(**kwargs: Any) -> Any:
        await asyncio.Event().wait()

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=hung_scrape,
        cycle_timeout_seconds=0.05,
    )
    with caplog.at_level(logging.WARNING, logger="app.ingestion.oddsportal"):
        await loader.fetch_odds("soccer")
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "oddsportal.com" not in msgs  # no URL leak
    assert "testland-league" not in msgs  # no league slug / URL fragment
    assert any("timed out" in r.getMessage().lower() for r in caplog.records)


async def test_fetch_odds_persists_partial_progress_per_date() -> None:
    # Incremental progress: a multi-date cycle whose SECOND date hangs must
    # still return the FIRST date's snapshots (the per-date scrape is bounded
    # individually, so a late wedge never discards earlier progress).
    import asyncio

    seen_dates: list[str] = []

    async def flaky_scrape(**kwargs: Any) -> Any:
        seen_dates.append(kwargs["date"])
        if len(seen_dates) == 1:
            return SimpleNamespace(success=[MATCH], failed=[], partial=[])
        await asyncio.Event().wait()  # second date hangs forever

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=flaky_scrape,
        days_ahead=1,  # two dated scrapes: today + tomorrow
        cycle_timeout_seconds=0.05,
    )
    snapshots = await loader.fetch_odds("soccer")
    assert len(seen_dates) == 2  # both dates attempted
    assert snapshots  # the first date's odds survived the second date's hang
    assert {s.event_id for s in snapshots} == {str(MATCH["match_link"])}


async def test_fetch_match_odds_watchdog_cancels_a_hung_scrape() -> None:
    # The finished-score / off-window match-page path is bounded too: one hung
    # match-page extraction must not wedge the cycle.
    import asyncio

    async def hung_scrape(**kwargs: Any) -> Any:
        await asyncio.Event().wait()

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=hung_scrape,
        cycle_timeout_seconds=0.05,
    )
    snapshots = await loader.fetch_match_odds(
        "soccer", ["https://www.oddsportal.com/football/a/b-vs-c/XY1/"]
    )
    assert snapshots == []  # bounded, returned cleanly


async def test_cycle_timeout_none_keeps_unbounded_behaviour() -> None:
    # Default None = no watchdog (in-process callers/tests that pass nothing):
    # a normal scrape is byte-for-byte unchanged.
    directory = EventDirectory()
    loader = make_loader(directory, [MATCH])  # no cycle_timeout_seconds
    assert loader._cycle_timeout_seconds is None
    snapshots = await loader.fetch_odds("soccer")
    assert len(snapshots) == 8  # unchanged happy path


# ---------------------------------------------------------------------------
# Score-only match-page scrape (finished-score capture fix, cactusbets.cloud):
# the finished-score pass only needs the match HEADER score (home/away result),
# never any market. Scraping it WITH markets re-runs the slow (and sometimes
# hung) Over/Under extraction, which made the per-link timeout fire BEFORE the
# already-available score was read — only 1 of ~24 scores landed. score_only
# requests NO markets so OddsHarvester skips market scraping entirely.
# ---------------------------------------------------------------------------


async def test_fetch_match_odds_score_only_requests_no_markets() -> None:
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
    link = "https://www.oddsportal.com/football/world/a-vs-b/XY9/"
    snaps = await loader.fetch_match_odds("soccer", [link], score_only=True)
    # NO markets scraped — the slow Over/Under path is never entered, so a hung
    # sub-line can't burn the per-link timeout before the score is read.
    assert calls[-1]["markets"] == []
    assert snaps == []  # score-only path yields no odds snapshots (only the score)


async def test_fetch_match_odds_score_only_overrides_market_fallback() -> None:
    # The normal trim falls back to the FULL list on an empty intersection;
    # score_only must NOT — it forces an empty market list regardless.
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
    link = "https://www.oddsportal.com/football/world/a-vs-b/XY9/"
    # Even passing markets=[] (empty intersection) must stay empty under score_only.
    await loader.fetch_match_odds("soccer", [link], markets=[], score_only=True)
    assert calls[-1]["markets"] == []
    # And the score still registers in the directory (the header carries it).
    await loader.fetch_match_odds("soccer", [link], score_only=True)
    teams = loader._directory.lookup(str(MATCH["match_link"]))
    assert teams is not None
    assert teams.home == "Alpha FC"


# Bare wildcard family keys carry NO line — the JSON feed enumerates every
# half-line — so they map to a market family but have ZERO direct selections here.
_WILDCARD_KEYS = {"over_under_games", "asian_handicap_games"}


def _expected_market_and_outcomes(key: str) -> tuple[Market, int]:
    """Doctrine layout per market family: 2-way vs 3-way full outcome sets;
    a wildcard family key maps to a market but has no direct (line-less) outcomes."""
    exact = {
        "1x2": (Market.H2H, 3),
        "home_away": (Market.H2H, 2),
        "btts": (Market.BTTS, 2),
        "dnb": (Market.DNB, 2),
        "double_chance": (Market.DOUBLE_CHANCE, 3),
    }
    if key in exact:
        return exact[key]
    if key in _WILDCARD_KEYS:
        return (Market.TOTALS if key.startswith("over_under_") else Market.SPREADS), 0
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
    if key not in _WILDCARD_KEYS and key.startswith(
        ("over_under_", "asian_handicap_", "european_handicap_")
    ):
        line = _line_from_key(key)
        assert line is not None
        # Every line-bearing selection embeds its line, so distinct lines of
        # one family can never collide in (event, market, selection) keys
        # (picks dedupe/supersede/revalidation all key on selection).
        assert all(f"{abs(line):g}" in sel for _, sel in selections)
    if key not in _WILDCARD_KEYS and key.startswith("asian_handicap"):
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
    # Proxy #0 RAISES (network / anti-bot / timeout — the real failover trigger)
    # -> failover to #1, which returns the slate. (A clean EMPTY listing is NOT a
    # failover trigger any more — see test_proxy_clean_empty_listing_is_final.)
    # Credentials must travel via the separate proxy_user/proxy_pass kwargs,
    # NEVER embedded in proxy_url.
    from app.ingestion.base import ScraperProxy

    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            raise TimeoutError("proxy #0 hung")  # real transport failure
        return SimpleNamespace(success=[MATCH], failed=[], partial=[])

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


async def test_proxy_clean_empty_listing_is_final_no_relaunch() -> None:
    # CYCLE-COST FIX (2026-06-28): a clean, exception-free listing of ZERO matches
    # is a genuine empty slate (off-season sport / empty slug), NOT a throttle —
    # so it is FINAL and the browser is NOT relaunched across the pool. Relaunching
    # Chromium up to _MAX_PROXY_FAILOVER× per empty sport/date was wasted
    # wall-clock that helped overrun the odds-freshness window.
    from app.ingestion.base import ScraperProxy

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
    assert len(calls) == 1  # ONE browser launch — no relaunch on a clean empty slate


async def test_match_fetch_empty_fails_over_across_proxies() -> None:
    # CORRECTION to the #135 empty-is-final change: it is right ONLY for the dated
    # LISTING path (empty = no fixtures). The per-match / finished-score path
    # (fetch_match_odds, e.g. score_only settlement capture) routes a SPECIFIC
    # stored match URL through the SAME helper, where a 0-result means the match
    # page didn't parse / was blocked — NOT an empty listing. So it MUST still fail
    # over (pre-#135 behavior), capped at _MAX_PROXY_FAILOVER, or one bad proxy
    # leaves finished scores uncaptured.
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
    snaps = await loader.fetch_match_odds(
        "soccer", ["https://www.oddsportal.com/football/x/y/a-b-Ab12/"], score_only=True
    )
    assert snaps == []
    assert len(calls) == _MAX_PROXY_FAILOVER  # failover RESTORED for the match path


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


async def test_proxy_failover_capped_on_dead_proxies() -> None:
    # When EVERY proxy RAISES (all dead / all blocked), the failover sweep must NOT
    # burn the whole pool — it is capped at _MAX_PROXY_FAILOVER so a bad run can't
    # starve the rest of the scrape cycle.
    from app.ingestion.base import ScraperProxy
    from app.ingestion.oddsportal import _MAX_PROXY_FAILOVER

    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise TimeoutError("proxy dead")  # every proxy fails -> real failover trigger

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
# Parallel dated-LISTING fan-out across the proxy pool (throughput fix
# 2026-06-28). DEFAULT 1 = today's serial single-proxy listing, bit-identical;
# knob>1 spreads the (date, league) units across distinct proxies concurrently.
# ---------------------------------------------------------------------------


def _league_match(league: str) -> dict[str, Any]:
    """A unique listed match per league slug (distinct match_link so the
    cross-unit dedup keeps one per league)."""
    return {
        "home_team": f"{league} Home",
        "away_team": f"{league} Away",
        "match_date": "2026-06-11",
        "match_link": f"https://www.oddsportal.com/football/{league}/{league}-game/",
        "1x2_market": [
            {"1": "2.10", "X": "3.40", "2": "3.60", "bookmaker_name": "B", "period": "FullTime"}
        ],
    }


async def test_listing_concurrency_default_one_is_serial_single_proxy() -> None:
    # DEFAULT (knob unset) MUST reproduce the legacy serial behaviour EXACTLY:
    # ONE Chromium context for the whole date with ALL leagues passed together,
    # behind ONE proxy (the cursor's first). No per-league split, no extra
    # contexts — strictly opt-in concurrency.
    from app.ingestion.base import ScraperProxy

    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(success=[MATCH], failed=[], partial=[])

    pool = tuple(
        ScraperProxy(url=f"http://h{i}:1", username=f"u{i}", password=f"p{i}") for i in range(4)
    )
    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["alpha", "beta", "gamma"])},
        scrape_fn=fake_scrape,
        proxy_pool=pool,
        # listing_concurrency defaults to 1
    )
    await loader.fetch_odds("soccer")
    assert len(calls) == 1  # single context, no per-league split
    assert calls[0]["leagues"] == ["alpha", "beta", "gamma"]  # all leagues in ONE call
    assert calls[0]["proxy_url"] == "http://h0:1"  # one proxy
    assert "proxy_start_index" not in calls[0]  # legacy path untouched


async def test_listing_concurrency_pins_distinct_proxies_and_runs_concurrently() -> None:
    # knob=N: each (date, league) unit runs in its OWN context, PINNED to a
    # distinct proxy (round-robin), and the N units overlap in flight (true
    # concurrency, not serial).
    from app.ingestion.base import ScraperProxy

    calls: list[dict[str, Any]] = []
    in_flight = 0
    peak = 0

    async def fake_scrape(**kwargs: Any) -> Any:
        nonlocal in_flight, peak
        calls.append(kwargs)
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            # yield twice so siblings can enter before any returns
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            league = (kwargs.get("leagues") or [None])[0]
            return SimpleNamespace(success=[_league_match(str(league))], failed=[], partial=[])
        finally:
            in_flight -= 1

    leagues = ["alpha", "beta", "gamma", "delta"]
    pool = tuple(
        ScraperProxy(url=f"http://h{i}:1", username=f"u{i}", password=f"p{i}") for i in range(4)
    )
    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", leagues)},
        scrape_fn=fake_scrape,
        proxy_pool=pool,
        listing_concurrency=4,
    )
    snaps = await loader.fetch_odds("soccer")
    # one unit per league, each a single-league listing call
    assert len(calls) == 4
    assert sorted((c["leagues"] or [None])[0] for c in calls) == sorted(leagues)
    # each unit pinned to a DISTINCT proxy (all four IPs used exactly once)
    assert sorted(c["proxy_url"] for c in calls) == [f"http://h{i}:1" for i in range(4)]
    # creds travel via separate kwargs, never embedded in the URL
    assert all(c["proxy_user"] not in c["proxy_url"] for c in calls)
    # the four units overlapped in flight — proves concurrency, not serial
    assert peak == 4
    # every league's match survived the merge/dedupe
    assert {s.event_id for s in snaps}  # non-empty
    assert loader.last_fetch_matches["soccer"] == 4


async def test_listing_concurrency_clamped_to_pool_size() -> None:
    # effective concurrency = min(pool_size, knob): a knob bigger than the pool
    # never exceeds the pool (you cannot pin more concurrent units than IPs).
    from app.ingestion.base import ScraperProxy

    in_flight = 0
    peak = 0

    async def fake_scrape(**kwargs: Any) -> Any:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            league = (kwargs.get("leagues") or [None])[0]
            return SimpleNamespace(success=[_league_match(str(league))], failed=[], partial=[])
        finally:
            in_flight -= 1

    leagues = ["alpha", "beta", "gamma", "delta"]
    pool = tuple(
        ScraperProxy(url=f"http://h{i}:1", username=f"u{i}", password=f"p{i}") for i in range(2)
    )
    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", leagues)},
        scrape_fn=fake_scrape,
        proxy_pool=pool,
        listing_concurrency=8,  # > pool size
    )
    snaps = await loader.fetch_odds("soccer")
    assert loader.last_fetch_matches["soccer"] == 4  # all leagues covered
    assert peak <= 2  # never more concurrent units than proxies
    assert snaps


async def test_listing_concurrency_failing_unit_fails_over_without_sinking_batch() -> None:
    # A unit whose PINNED proxy raises must fail over to the next proxy and still
    # return its slate — and one failing unit must NOT sink the whole batch.
    from app.ingestion.base import ScraperProxy

    calls: list[dict[str, Any]] = []

    async def fake_scrape(**kwargs: Any) -> Any:
        calls.append(kwargs)
        await asyncio.sleep(0)
        # proxy #1 is "dead": every GET through it raises (real transport failure)
        if kwargs["proxy_url"] == "http://h1:1":
            raise TimeoutError("proxy #1 hung")
        league = (kwargs.get("leagues") or [None])[0]
        return SimpleNamespace(success=[_league_match(str(league))], failed=[], partial=[])

    leagues = ["alpha", "beta"]  # unit0 pins h0 (ok), unit1 pins h1 (dead -> failover)
    pool = tuple(
        ScraperProxy(url=f"http://h{i}:1", username=f"u{i}", password=f"p{i}") for i in range(4)
    )
    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", leagues)},
        scrape_fn=fake_scrape,
        proxy_pool=pool,
        listing_concurrency=2,
    )
    snaps = await loader.fetch_odds("soccer")
    # BOTH leagues survived: the dead-proxy unit failed over to the next IP
    assert loader.last_fetch_matches["soccer"] == 2
    assert snaps
    # the dead proxy was actually attempted (and then failed over)
    assert any(c["proxy_url"] == "http://h1:1" for c in calls)


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
