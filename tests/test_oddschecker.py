"""OddsChecker curl_cffi/Hypernova ingestion contract."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from app.ingestion.base import EventDirectory, ScraperProxy
from app.ingestion.oddschecker import (
    OddsCheckerChallenge,
    OddsCheckerError,
    OddsCheckerFetchResult,
    OddsCheckerLoader,
    _line_bearing_selection,
    discover_football_daily_match_urls,
    fetch_html,
    football_listing_context,
    football_match_urls_from_api,
    is_challenge_response,
    parse_competition_match_urls,
    parse_legacy_match_page,
    parse_market_api_payloads,
    parse_match_page,
    parse_static_sport_match_urls,
    supported_market_ids_from_match_page,
)
from app.ingestion.oddsportal import _fmt_line
from app.schemas.base import Market


@pytest.mark.parametrize(
    ("selection", "line", "market"),
    [
        ("Over", "2.5", Market.TOTALS),
        ("Under", "3.5", Market.TEAM_TOTALS),
    ],
)
def test_line_bearing_totals_match_oddsportal_form(
    selection: str, line: str, market: Market
) -> None:
    # OddsPortal totals selections are f"Over {line:g}" (unsigned); OddsChecker
    # emits a bare betName + separate line — they must end up identical.
    assert _line_bearing_selection(selection, line, market) == f"{selection} {float(line):g}"


@pytest.mark.parametrize(("team", "line"), [("Carolina Panthers", "-1.5"), ("Alpha FC", "+1.5")])
def test_line_bearing_spreads_match_oddsportal_signed_form(team: str, line: str) -> None:
    # OddsPortal spreads selections are f"{team} {_fmt_line(line)}" (signed).
    assert _line_bearing_selection(team, line, Market.SPREADS) == f"{team} {_fmt_line(float(line))}"


def test_line_bearing_is_idempotent_and_skips_non_line_markets() -> None:
    # Already-line-bearing legacy grid rows are not double-appended.
    assert _line_bearing_selection("Over 2.5", "2.5", Market.TOTALS) == "Over 2.5"
    assert _line_bearing_selection("Guinea -3.5", "-3.5", Market.SPREADS) == "Guinea -3.5"
    # h2h / non-line markets and missing lines pass through unchanged.
    assert _line_bearing_selection("Arsenal", None, Market.H2H) == "Arsenal"
    assert _line_bearing_selection("Over", None, Market.TOTALS) == "Over"


def _json_script(payload: dict[str, object]) -> str:
    return f'<script type="application/json"><!--{json.dumps(payload)}--></script>'


def _match_html() -> str:
    header = {
        "repub": "OC",
        "lastUpdated": 1783246057889,
        "eventName": "English Premier League Matches",
        "subeventName": "Arsenal vs Coventry",
        "subeventStartTime": "2026-08-21T19:00:00Z",
        "breadcrumbs": [
            {"name": "Home", "url": "/", "type": "menu"},
            {"name": "Premier League", "url": "football/english/premier-league", "type": "card"},
            {
                "id": 101610031,
                "name": "Arsenal vs Coventry",
                "url": "football/english/premier-league/arsenal-v-coventry/winner",
                "type": "subevent",
            },
        ],
    }
    odds = {
        "repub": "OC",
        "lastUpdated": 1783246073819,
        "bestOdds": {
            "bets": {
                "entities": {
                    "1": {
                        "ocBetId": 1,
                        "betName": "Arsenal",
                        "marketId": 10,
                        "line": None,
                    },
                    "2": {
                        "ocBetId": 2,
                        "betName": "Draw",
                        "marketId": 10,
                        "line": None,
                    },
                    "3": {
                        "ocBetId": 3,
                        "betName": "Arsenal or Draw",
                        "marketId": 20,
                        "line": None,
                    },
                    "4": {
                        "ocBetId": 4,
                        "betName": "Over",
                        "marketId": 30,
                        "line": "2.5",
                    },
                },
                "ids": [1, 2, 3, 4],
            },
            "odds": {
                "1": {
                    "WH": {
                        "bookmakerCode": "WH",
                        "oddsDecimal": 1.17,
                        "status": "ACTIVE",
                        "expired": False,
                        "notExpired": True,
                        "betFeedTimestamp": "2026-07-04T04:28:47.279655245Z",
                    },
                    "BAD": {
                        "bookmakerCode": "BAD",
                        "oddsDecimal": 1.16,
                        "status": "SUSPENDED",
                        "expired": False,
                        "notExpired": True,
                    },
                },
                "2": {
                    "OE": {
                        "bookmakerCode": "OE",
                        "oddsDecimal": 7.5,
                        "status": "ACTIVE",
                        "expired": False,
                        "notExpired": True,
                        "betFeedTimestamp": "2026-06-29T08:05:21.379683645Z",
                    }
                },
                "3": {
                    "WH": {
                        "bookmakerCode": "WH",
                        "oddsDecimal": 1.05,
                        "status": "ACTIVE",
                        "expired": True,
                        "notExpired": False,
                    }
                },
                "4": {
                    "WH": {
                        "bookmakerCode": "WH",
                        "oddsDecimal": 2.1,
                        "status": "ACTIVE",
                        "expired": False,
                        "notExpired": True,
                    }
                },
            },
            "markets": {
                "entities": {
                    "10": {"ocMarketId": 10, "marketTypeName": "Win Market"},
                    "20": {"ocMarketId": 20, "marketTypeName": "Double Chance"},
                    "30": {"ocMarketId": 30, "marketTypeName": "Asian Total"},
                },
                "ids": [10, 20, 30],
            },
            "bookmakers": {
                "entities": {
                    "WH": {"bookmakerCode": "WH", "bookmakerName": "William Hill"},
                    "OE": {"bookmakerCode": "OE", "bookmakerName": "Betfair Exchange"},
                },
                "ids": ["WH", "OE"],
            },
            "subeventConfig": {
                "name": "Arsenal vs Coventry",
                "subeventId": "101610031",
                "eventId": 2457,
                "homeTeamName": "Arsenal",
                "awayTeamName": "Coventry",
            },
        },
    }
    return f"<html><body>{_json_script(header)}{_json_script(odds)}</body></html>"


def test_challenge_detection_ignores_normal_js_detection_snippet() -> None:
    assert not is_challenge_response(
        status_code=200,
        headers={"server": "cloudflare"},
        body="<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>",
    )
    assert is_challenge_response(
        status_code=403,
        headers={"server": "cloudflare"},
        body="<title>Just a moment...</title><div cf-chl-widget></div>",
    )
    assert is_challenge_response(
        status_code=200,
        headers={"cf-mitigated": "challenge"},
        body="<html></html>",
    )


def test_parse_match_page_emits_snapshots_and_registers_event() -> None:
    directory = EventDirectory()
    now = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)

    snapshots = parse_match_page(
        _match_html(),
        url="https://www.oddschecker.com/football/english/premier-league/arsenal-v-coventry/winner",
        directory=directory,
        now=now,
    )

    assert len(snapshots) == 3
    assert {snapshot.bookmaker for snapshot in snapshots} == {
        "William Hill",
        "Betfair Exchange",
    }
    assert {(snapshot.market, snapshot.selection) for snapshot in snapshots} == {
        (Market.H2H, "Arsenal"),
        (Market.H2H, "Draw"),
        (Market.TOTALS, "Over 2.5"),
    }
    total = next(snapshot for snapshot in snapshots if snapshot.market is Market.TOTALS)
    assert total.market_detail == "totals_2_5"
    assert total.captured_at == datetime.fromtimestamp(1783246073819 / 1000, tz=UTC)

    teams = directory.lookup("oddschecker:101610031")
    assert teams is not None
    assert teams.home == "Arsenal"
    assert teams.away == "Coventry"
    assert teams.league == "Premier League"
    assert teams.starts_at == datetime(2026, 8, 21, 19, 0, tzinfo=UTC)


def _correct_score_html() -> str:
    """Minimal bestOdds page carrying one Correct Score market with BOTH
    scoreline-bearing bet names and the ambiguous scoreline-less forms the
    feed also emits (audit 2026-07-10 L-oddschecker-969)."""
    header: dict[str, object] = {
        "repub": "OC",
        "eventName": "English Premier League Matches",
        "subeventName": "Arsenal vs Coventry",
        "subeventStartTime": "2026-08-21T19:00:00Z",
        "breadcrumbs": [],
    }
    active = {
        "oddsDecimal": 9.0,
        "status": "ACTIVE",
        "expired": False,
        "notExpired": True,
    }
    odds = {
        "repub": "OC",
        "lastUpdated": 1783246073819,
        "bestOdds": {
            "bets": {
                "entities": {
                    "1": {"ocBetId": 1, "betName": "2-1", "marketId": 40, "line": None},
                    "2": {"ocBetId": 2, "betName": "1:1", "marketId": 40, "line": None},
                    # scoreline-less names: DISTINCT scorelines collapse onto
                    # these keys — ambiguous, must be dropped (fail-closed).
                    "3": {"ocBetId": 3, "betName": "Draw", "marketId": 40, "line": None},
                    "4": {"ocBetId": 4, "betName": "Arsenal", "marketId": 40, "line": None},
                },
                "ids": [1, 2, 3, 4],
            },
            "odds": {
                "1": {"WH": {"bookmakerCode": "WH", **active}},
                "2": {"WH": {"bookmakerCode": "WH", **active}},
                "3": {"WH": {"bookmakerCode": "WH", **active}},
                "4": {"WH": {"bookmakerCode": "WH", **active}},
            },
            "markets": {
                "entities": {"40": {"ocMarketId": 40, "marketTypeName": "Correct Score"}},
                "ids": [40],
            },
            "bookmakers": {
                "entities": {"WH": {"bookmakerCode": "WH", "bookmakerName": "William Hill"}},
                "ids": ["WH"],
            },
            "subeventConfig": {
                "name": "Arsenal vs Coventry",
                "subeventId": "101610032",
                "eventId": 2458,
                "homeTeamName": "Arsenal",
                "awayTeamName": "Coventry",
            },
        },
    }
    return f"<html><body>{_json_script(header)}{_json_script(odds)}</body></html>"


def test_correct_score_keeps_scorelines_and_drops_ambiguous_selections() -> None:
    # L-oddschecker-969: scoreline-less correct-score bet names ('Draw', a bare
    # team) collapse DISTINCT scorelines onto one snapshot key. Only selections
    # carrying an explicit scoreline survive; ambiguous ones are dropped.
    directory = EventDirectory()
    snapshots = parse_match_page(
        _correct_score_html(),
        url="https://www.oddschecker.com/football/english/premier-league/arsenal-v-coventry/correct-score",
        directory=directory,
    )
    assert {s.selection for s in snapshots} == {"2-1", "1:1"}
    assert all(s.market is Market.CORRECT_SCORE for s in snapshots)


def test_parse_match_page_can_filter_markets() -> None:
    directory = EventDirectory()

    snapshots = parse_match_page(
        _match_html(),
        url="https://www.oddschecker.com/football/english/premier-league/arsenal-v-coventry/winner",
        directory=directory,
        markets=(Market.H2H,),
    )

    assert len(snapshots) == 2
    assert {snapshot.market for snapshot in snapshots} == {Market.H2H}


def test_supported_market_ids_selects_core_moneyline_totals_and_spreads() -> None:
    html = _json_script(
        {
            "repub": "OC",
            "lastUpdated": 1783246073819,
            "bestOdds": {
                "bets": {"entities": {"1": {"ocBetId": 1}}, "ids": [1]},
                "odds": {"1": {"WH": {"oddsDecimal": 2.0}}},
                "markets": {
                    "entities": {
                        "10": {"ocMarketId": 10, "marketTypeName": "Win Market"},
                        "20": {"ocMarketId": 20, "marketTypeName": "Point Spread"},
                        "30": {"ocMarketId": 30, "marketTypeName": "Total Points"},
                        "40": {"ocMarketId": 40, "marketTypeName": "Total Goals Exact"},
                        "50": {"ocMarketId": 50, "marketTypeName": "Total Corners"},
                    },
                    "ids": [10, 20, 30, 40, 50],
                },
            },
        }
    )

    assert supported_market_ids_from_match_page(html) == ["10", "20", "30"]
    assert supported_market_ids_from_match_page(html, markets=(Market.SPREADS,)) == ["20"]


def test_parse_market_api_payloads_emits_totals_and_spreads() -> None:
    directory = EventDirectory()
    now = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    payloads = [
        {
            "marketId": 100,
            "subeventId": 9001,
            "subeventName": "Carolina Panthers at Arizona Cardinals",
            "subeventStartTime": "2026-09-14T20:00:00Z",
            "eventName": "NFL Matches",
            "marketTypeName": "Point Spread",
            "bets": [
                {"betId": 1, "betName": "Carolina Panthers", "line": "-1.5"},
                {"betId": 2, "betName": "Arizona Cardinals", "line": "+1.5"},
            ],
            "odds": [
                {
                    "betId": 1,
                    "bookmakerCode": "WH",
                    "oddsDecimal": 1.91,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:59:00Z",
                },
                {
                    "betId": 2,
                    "bookmakerCode": "B3",
                    "oddsDecimal": 1.95,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:59:00Z",
                },
            ],
        },
        {
            "marketId": 101,
            "subeventId": 9001,
            "subeventName": "Carolina Panthers at Arizona Cardinals",
            "subeventStartTime": "2026-09-14T20:00:00Z",
            "eventName": "NFL Matches",
            "marketTypeName": "Total Points",
            "bets": [{"betId": 3, "betName": "Over", "line": "41.5"}],
            "odds": [
                {
                    "betId": 3,
                    "bookmakerCode": "WH",
                    "oddsDecimal": 1.83,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:59:30Z",
                }
            ],
        },
    ]

    snapshots = parse_market_api_payloads(
        payloads,
        url="https://www.oddschecker.com/american-football/nfl/carolina-panthers-at-arizona-cardinals/winner",
        directory=directory,
        now=now,
    )

    actual = {
        (snapshot.market, snapshot.selection, snapshot.market_detail) for snapshot in snapshots
    }
    assert actual == {
        (Market.SPREADS, "Carolina Panthers -1.5", "spreads_minus_1_5"),
        (Market.SPREADS, "Arizona Cardinals +1.5", "spreads_plus_1_5"),
        (Market.TOTALS, "Over 41.5", "totals_41_5"),
    }
    teams = directory.lookup("oddschecker:9001")
    assert teams is not None
    assert teams.home == "Arizona Cardinals"
    assert teams.away == "Carolina Panthers"
    assert teams.league == "NFL"


async def test_fetch_match_odds_markets_override_reaches_api_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller's `markets` override (the shared off-window CLV re-price
    signature) must drive the market-API payload filtering, not just the
    market-id collection — a loader scoped to H2H asked for TOTALS must
    return TOTALS rows, never silently []."""
    import app.ingestion.oddschecker as oc

    async def fake_payloads(
        market_ids: object, *, referer: object, session: object = None, proxy: object = None
    ) -> list[dict[str, object]]:
        return [
            {
                "marketId": 30,
                "subeventId": 101610031,
                "subeventName": "Arsenal vs Coventry",
                "subeventStartTime": "2026-08-21T19:00:00Z",
                "eventName": "English Premier League Matches",
                "marketTypeName": "Asian Total",
                "bets": [{"betId": 4, "betName": "Over", "line": "2.5"}],
                "odds": [
                    {
                        "betId": 4,
                        "bookmakerCode": "WH",
                        "oddsDecimal": 2.1,
                        "status": "ACTIVE",
                        "betFeedTimestamp": "2026-07-05T09:59:00Z",
                    }
                ],
            }
        ]

    monkeypatch.setattr(oc, "fetch_market_api_payloads", fake_payloads)
    loader = OddsCheckerLoader(EventDirectory(), markets=(Market.H2H,))
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/english/premier-league/arsenal-v-coventry/winner",
        html=_match_html(),
        status_code=200,
    )
    snapshots = await loader._parse_modern_or_legacy_match_page(
        page, now=datetime(2026, 7, 5, 10, 0, tzinfo=UTC), session=None, markets=(Market.TOTALS,)
    )
    assert snapshots, "explicitly requested TOTALS override returned no rows"
    assert {s.market for s in snapshots} == {Market.TOTALS}


async def test_fetch_match_odds_markets_override_reaches_fallback_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the market API down, the parse_match_page fallback must honor the
    caller's `markets` override too (same H2H-scoped loader, TOTALS request)."""
    import app.ingestion.oddschecker as oc

    async def boom(
        market_ids: object, *, referer: object, session: object = None, proxy: object = None
    ) -> list[dict[str, object]]:
        raise OddsCheckerError("api down")

    monkeypatch.setattr(oc, "fetch_market_api_payloads", boom)
    loader = OddsCheckerLoader(EventDirectory(), markets=(Market.H2H,))
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/english/premier-league/arsenal-v-coventry/winner",
        html=_match_html(),
        status_code=200,
    )
    snapshots = await loader._parse_modern_or_legacy_match_page(
        page, now=datetime(2026, 7, 5, 10, 0, tzinfo=UTC), session=None, markets=(Market.TOTALS,)
    )
    assert snapshots, "explicitly requested TOTALS override returned no rows"
    assert {s.market for s in snapshots} == {Market.TOTALS}


def test_parse_legacy_match_page_reads_old_table_grid() -> None:
    directory = EventDirectory()
    now = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    html = """
    <table class="eventTable" data-mid="3585276690" data-mname="Point Spread"
      data-sname="Guinea at Tunisia" data-time="2026-07-05 13:00:00"
      data-ename="FIBA World Cup Qualification">
      <tbody>
        <tr class="diff-row evTabRow bc" data-bname="Guinea -3.5">
          <td class="sel nm">Guinea -3.5</td>
          <td data-bk="WH" data-odig="1.91" data-o="10/11" data-hcap="-3.5"></td>
          <td data-bk="B3" data-odig="0" data-o="" data-hcap="-3.5"></td>
        </tr>
        <tr class="diff-row evTabRow bc" data-bname="Tunisia +3.5">
          <td class="sel nm">Tunisia +3.5</td>
          <td data-bk="WH" data-odig="1.95" data-o="20/21" data-hcap="+3.5"></td>
        </tr>
      </tbody>
    </table>
    """

    snapshots = parse_legacy_match_page(
        html,
        url="https://www.oddschecker.com/basketball/fiba-world-cup-qualification/guinea-at-tunisia/point-spread",
        directory=directory,
        now=now,
    )

    actual = {
        (snapshot.selection, snapshot.market_detail, snapshot.decimal_odds)
        for snapshot in snapshots
    }
    assert actual == {
        ("Guinea -3.5", "spreads_minus_3_5", 1.91),
        ("Tunisia +3.5", "spreads_plus_3_5", 1.95),
    }
    teams = directory.lookup(
        "oddschecker:basketball/fiba-world-cup-qualification/guinea-at-tunisia"
    )
    assert teams is not None
    assert teams.home == "Tunisia"
    assert teams.away == "Guinea"
    assert teams.starts_at == datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


def test_parse_competition_match_urls_dedupes_winner_links() -> None:
    html = """
    <div data-hypernova-key="competitionsaccumulatormatches">
      <a href="football/english/premier-league/arsenal-v-coventry/winner">Compare odds</a>
      <a href="/football/english/premier-league/arsenal-v-coventry/winner">Duplicate</a>
      <a href="/football/english/premier-league/hull-v-man-utd/winner">Compare odds</a>
      <a href="/football/english/premier-league">Competition</a>
    </div>
    """

    assert parse_competition_match_urls(html) == [
        "https://www.oddschecker.com/football/english/premier-league/arsenal-v-coventry/winner",
        "https://www.oddschecker.com/football/english/premier-league/hull-v-man-utd/winner",
    ]


def test_parse_static_sport_match_urls_filters_today_tomorrow_dates() -> None:
    html = """
    <table class="at-12 standard-list">
      <tbody>
        <tr><td><p class="beta-caption1 event-date">Sunday 5th July 2026</p></td></tr>
        <tr class="match-on"><td>
          <a class="whole-row-link" href="/tennis/a/winner">All Odds</a>
        </td></tr>
        <tr><td><p class="beta-caption1 event-date">Monday 6th July 2026</p></td></tr>
        <tr class="match-on"><td>
          <a class="whole-row-link" href="/tennis/b/winner">All Odds</a>
        </td></tr>
        <tr><td><p class="beta-caption1 event-date">Tuesday 7th July 2026</p></td></tr>
        <tr class="match-on"><td>
          <a class="whole-row-link" href="/tennis/c/winner">All Odds</a>
        </td></tr>
      </tbody>
    </table>
    """

    assert parse_static_sport_match_urls(html, start_date=date(2026, 7, 5), days=2) == [
        "https://www.oddschecker.com/tennis/a/winner",
        "https://www.oddschecker.com/tennis/b/winner",
    ]


def _football_home_html() -> str:
    return _json_script(
        {
            "repub": "OC",
            "lastUpdated": 1783246904387,
            "config": {
                "cards": [{"id": 11}, {"id": 22}],
                "events": [
                    {"eventId": 100, "url": "football/world-cup"},
                    {"eventId": 200, "url": "football/brazil/serie-b"},
                ],
            },
        }
    )


def _football_daily_api_payload() -> dict[str, object]:
    return {
        "subevents": [
            {
                "id": 1,
                "eventId": 100,
                "name": "Brazil vs Norway",
                "startTime": "2026-07-05T20:00:00Z",
                "urlMap": "brazil-v-norway",
            },
            {
                "id": 2,
                "eventId": 100,
                "name": "Brazil vs Norway",
                "startTime": "2026-07-05T20:00:00Z",
                "urlMap": "brazil-v-norway",
            },
            {
                "id": 3,
                "eventId": 200,
                "name": "Nautico vs Juventude",
                # Europe/London date is 2026-07-06.
                "startTime": "2026-07-05T23:30:00Z",
                "urlMap": "nautico-v-juventude",
            },
            {
                "id": 4,
                "eventId": 100,
                "name": "Out of range",
                "startTime": "2026-07-07T20:00:00Z",
                "urlMap": "out-of-range",
            },
            {
                "id": 5,
                "eventId": 999,
                "name": "Unknown event",
                "startTime": "2026-07-05T20:00:00Z",
                "urlMap": "unknown-event",
            },
        ]
    }


def test_football_daily_api_builds_today_tomorrow_match_urls() -> None:
    context = football_listing_context(_football_home_html())

    urls = football_match_urls_from_api(
        _football_daily_api_payload(),
        context,
        start_date=date(2026, 7, 5),
        days=2,
    )

    assert urls == [
        "https://www.oddschecker.com/football/world-cup/brazil-v-norway/winner",
        "https://www.oddschecker.com/football/brazil/serie-b/nautico-v-juventude/winner",
    ]


class _QueuedResponse:
    def __init__(
        self,
        *,
        text: str,
        url: str,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}

    def json(self) -> dict[str, object]:
        return json.loads(self.text)


class _QueuedSession:
    def __init__(self, responses: list[_QueuedResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs: object) -> _QueuedResponse:
        del kwargs
        self.urls.append(url)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_discover_football_daily_match_urls_uses_api_window() -> None:
    session = _QueuedSession(
        [
            _QueuedResponse(
                text=_football_home_html(),
                url="https://www.oddschecker.com/football",
                headers={"content-type": "text/html"},
            ),
            _QueuedResponse(
                text=json.dumps(_football_daily_api_payload()),
                url="https://www.oddschecker.com/api/acca/v1/acca/coupon/cards/11,22/"
                "marketTemplate/1/loadData/3/forDate/2026-07-05/andDays/2",
            ),
        ]
    )

    urls = await discover_football_daily_match_urls(
        start_date=date(2026, 7, 5),
        days=2,
        session=session,
    )

    assert urls == [
        "https://www.oddschecker.com/football/world-cup/brazil-v-norway/winner",
        "https://www.oddschecker.com/football/brazil/serie-b/nautico-v-juventude/winner",
    ]
    assert (
        "cards/11,22/marketTemplate/1/loadData/3/forDate/2026-07-05/andDays/2" in (session.urls[1])
    )


class _RouteSession:
    """Fake session that routes GETs by URL substring (order-independent).

    Concurrent match fetches + market-API round-trips make a FIFO queue
    non-deterministic; substring routing keeps the scheduler-mode test stable.
    Routes are tried in order — put the most specific substrings first.
    """

    def __init__(self, routes: list[tuple[str, _QueuedResponse]]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs: object) -> _QueuedResponse:
        del kwargs
        self.urls.append(url)
        for substring, response in self.routes:
            if substring in url:
                return response
        raise AssertionError(f"no route for {url}")


def test_parse_market_api_capture_other_is_sharp_anchor_gated() -> None:
    """capture_other persists unmapped markets ONLY when Betfair (OE) prices
    them, never boosted markets, under Market.OTHER with an oc_<slug> detail."""
    directory = EventDirectory()
    payloads = [
        {  # unmapped + Betfair Exchange (OE) present -> captured as OTHER
            "subeventId": 7001,
            "subeventName": "Arsenal vs Chelsea",
            "eventName": "Premier League Matches",
            "marketTypeName": "Total Corners",
            "bets": [{"betId": 1, "betName": "Over", "line": "9.5"}],
            "odds": [
                {"betId": 1, "bookmakerCode": "OE", "oddsDecimal": 1.9, "status": "ACTIVE"},
                {"betId": 1, "bookmakerCode": "WH", "oddsDecimal": 1.85, "status": "ACTIVE"},
            ],
        },
        {  # unmapped + soft books only (no OE) -> dropped
            "subeventId": 7001,
            "subeventName": "Arsenal vs Chelsea",
            "marketTypeName": "Anytime Goalscorer",
            "bets": [{"betId": 2, "betName": "Some Player"}],
            "odds": [{"betId": 2, "bookmakerCode": "WH", "oddsDecimal": 3.5, "status": "ACTIVE"}],
        },
        {  # boosted market, even with OE -> dropped
            "subeventId": 7001,
            "subeventName": "Arsenal vs Chelsea",
            "marketTypeName": "Enhanced Win Market",
            "bets": [{"betId": 3, "betName": "Arsenal"}],
            "odds": [{"betId": 3, "bookmakerCode": "OE", "oddsDecimal": 5.0, "status": "ACTIVE"}],
        },
    ]

    without = parse_market_api_payloads(
        payloads, url="https://www.oddschecker.com/football/x/y/winner", directory=directory
    )
    assert without == []  # capture_other defaults off -> nothing (all unmapped)

    got = parse_market_api_payloads(
        payloads,
        url="https://www.oddschecker.com/football/x/y/winner",
        directory=directory,
        capture_other=True,
    )
    assert {(s.market, s.selection, s.market_detail, s.bookmaker) for s in got} == {
        (Market.OTHER, "Over", "oc_total_corners_9_5", "Betfair Exchange"),
        (Market.OTHER, "Over", "oc_total_corners_9_5", "William Hill"),
    }


@pytest.mark.asyncio
async def test_for_scheduler_fetch_odds_is_per_sport() -> None:
    """Scheduler mode discovers + parses ONLY the requested pipeline sport."""
    from app.ingestion.oddschecker import OddsCheckerLoader

    directory = EventDirectory()
    session = _RouteSession(
        [
            (
                "api/acca",
                _QueuedResponse(
                    text=json.dumps(_football_daily_api_payload()),
                    url="https://www.oddschecker.com/api/acca",
                ),
            ),
            # Empty all-odds list -> parse_market_api_payloads yields nothing ->
            # the loader falls back to the embedded bestOdds match payload.
            (
                "api/markets/v2/all-odds",
                _QueuedResponse(text="[]", url="https://www.oddschecker.com/api/markets"),
            ),
            (
                "/winner",
                _QueuedResponse(
                    text=_match_html(),
                    url="https://www.oddschecker.com/football/x/y/winner",
                    headers={"content-type": "text/html"},
                ),
            ),
            (
                "/football",
                _QueuedResponse(
                    text=_football_home_html(),
                    url="https://www.oddschecker.com/football",
                    headers={"content-type": "text/html"},
                ),
            ),
        ]
    )
    loader = OddsCheckerLoader.for_scheduler(
        directory,
        sport_keys=("soccer", "basketball"),
        start_date=date(2026, 7, 5),
    )

    snapshots = await loader.fetch_odds("soccer", session=session)

    # Two football matches discovered -> both parsed; each match yields the
    # 3 active snapshots from _match_html.
    assert len(snapshots) == 6
    assert loader.last_fetch_matches["soccer"] == 2
    assert loader.last_fetch_event_ids["soccer"] == ("oddschecker:101610031",)


@pytest.mark.asyncio
async def test_for_scheduler_unknown_sport_key_is_empty() -> None:
    from app.ingestion.oddschecker import OddsCheckerLoader

    loader = OddsCheckerLoader.for_scheduler(EventDirectory())
    assert await loader.fetch_odds("cricket") == []


class _FakeResponse:
    status_code = 403
    text = "<title>Just a moment...</title>"
    headers = {"cf-mitigated": "challenge"}
    url = "https://www.oddschecker.com/football"


class _FakeSession:
    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        del url, kwargs
        return _FakeResponse()


@pytest.mark.asyncio
async def test_fetch_html_raises_on_challenge_response() -> None:
    with pytest.raises(OddsCheckerChallenge):
        await fetch_html("https://www.oddschecker.com/football", session=_FakeSession())


class _PoolFakeSession:
    """Fake per-proxy pool session: routes GETs by URL substring and records
    every GET plus whether the pool closed it (aclose lifecycle)."""

    def __init__(self, routes: list[tuple[str, _QueuedResponse]]) -> None:
        self.routes = routes
        self.urls: list[str] = []
        self.closed = False

    async def get(self, url: str, **kwargs: object) -> _QueuedResponse:
        del kwargs
        self.urls.append(url)
        for substring, response in self.routes:
            if substring in url:
                return response
        raise AssertionError(f"no route for {url}")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_proxy_session_pool_reuses_one_session_per_proxy() -> None:
    """The pool builds ONE session per proxy, reuses it, and closes all on aclose."""
    from app.ingestion.oddschecker import _ProxySessionPool

    proxies = (
        ScraperProxy(url="http://p0", username="", password=""),
        ScraperProxy(url="http://p1", username="", password=""),
    )
    created: list[tuple[ScraperProxy | None, _PoolFakeSession]] = []

    def factory(proxy: ScraperProxy | None) -> _PoolFakeSession:
        session = _PoolFakeSession([])
        created.append((proxy, session))
        return session

    pool = _ProxySessionPool(proxies, session_factory=factory)

    first_p0 = pool.acquire()  # proxy 0
    first_p1 = pool.acquire()  # proxy 1
    second_p0 = pool.acquire()  # proxy 0 again -> reused, no rebuild
    second_p1 = pool.acquire()  # proxy 1 again -> reused

    assert first_p0 is second_p0
    assert first_p1 is second_p1
    assert first_p0 is not first_p1
    # Exactly one session built per distinct proxy, bound to the right proxy.
    assert [proxy for proxy, _ in created] == [proxies[0], proxies[1]]

    await pool.aclose()
    assert all(session.closed for _, session in created)


@pytest.mark.asyncio
async def test_fetch_odds_reuses_persistent_session_per_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll cycle with a proxy pool reuses ONE persistent session per proxy
    across all match-page fetches, then closes it (no leak)."""
    from app.ingestion import oddschecker as oc

    created: list[_PoolFakeSession] = []

    def factory(proxy: ScraperProxy | None) -> _PoolFakeSession:
        del proxy
        session = _PoolFakeSession(
            [
                (
                    "api/markets/v2/all-odds",
                    _QueuedResponse(text="[]", url="https://www.oddschecker.com/api/markets"),
                ),
                (
                    "/winner",
                    _QueuedResponse(
                        text=_match_html(),
                        url="https://www.oddschecker.com/football/x/y/winner",
                        headers={"content-type": "text/html"},
                    ),
                ),
            ]
        )
        created.append(session)
        return session

    monkeypatch.setattr(oc, "_new_impersonated_session", factory)

    match_urls = [
        "https://www.oddschecker.com/football/a/1/winner",
        "https://www.oddschecker.com/football/b/2/winner",
        "https://www.oddschecker.com/football/c/3/winner",
    ]
    loader = oc.OddsCheckerLoader(
        match_urls=match_urls,
        directory=EventDirectory(),
        proxy_pool=(ScraperProxy(url="http://p0", username="", password=""),),
        max_clients=2,
    )

    snapshots = await loader.fetch_odds("soccer")  # no injected session -> pool path

    # Single proxy -> exactly one persistent session built and reused for all
    # three match pages (+ their all-odds round-trips).
    assert len(created) == 1
    session = created[0]
    assert sum("/winner" in url for url in session.urls) == 3
    # 3 matches x 3 active snapshots each (all-odds returns [] -> bestOdds fallback).
    assert len(snapshots) == 9
    # The cycle closed the session and dropped the pool (no leak).
    assert session.closed is True
    assert loader._session_pool is None


async def test_gather_snapshots_retries_once_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A match-page fetch that times out (stale pooled keep-alive connection) is
    retried ONCE on a fresh session and recovers; non-timeout errors do not
    retry. Regression for the ~7%->~1.4% net-timeout fix (2026-07-06)."""
    from app.ingestion import oddschecker as oc

    # The retry builds a fresh session via the module factory — stub it so no
    # network is touched.
    monkeypatch.setattr(oc, "_new_impersonated_session", lambda proxy: _PoolFakeSession([]))

    class _Timeout(Exception):
        pass

    calls = {"n": 0}

    class _RetryLoader(oc.OddsCheckerLoader):
        async def fetch_match_odds(self, url, *, session=None, **kw):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Timeout("connect timeout")
            return ["snap"]  # sentinel; _gather only len()s it

    loader = _RetryLoader(
        match_urls=["https://www.oddschecker.com/football/a/1/winner"],
        directory=EventDirectory(),
        proxy_pool=(ScraperProxy(url="http://p0", username="", password=""),),
        max_clients=2,
    )
    snaps = await loader._gather_snapshots(
        ["https://www.oddschecker.com/football/a/1/winner"], session=None
    )
    assert calls["n"] == 2  # timed out once, retried once
    assert len(snaps) == 1  # the retry recovered the page


def test_bookmaker_name_disambiguates_bare_betfair_by_code() -> None:
    """Audit 2026-07-10 (M863): the feed's entity display name for code BF is
    sometimes the bare 'Betfair', persisting the SAME sportsbook under two
    names (2,241 live rows) — and effective_odds maps bare 'betfair' to 5%
    exchange commission, mispricing those rows. The ambiguous display name
    must resolve through the code's canonical fallback; unambiguous names and
    unknown codes pass through unchanged."""
    from app.ingestion.oddschecker import _bookmaker_name

    assert _bookmaker_name("BF", {"BF": {"bookmakerName": "Betfair"}}) == "Betfair Sportsbook"
    assert _bookmaker_name("OE", {"OE": {"bookmakerName": "Betfair"}}) == "Betfair Exchange"
    # unambiguous display names pass through
    assert (
        _bookmaker_name("BF", {"BF": {"bookmakerName": "Betfair Sportsbook"}})
        == "Betfair Sportsbook"
    )
    # unknown code with the ambiguous name: no canonical mapping exists -> unchanged
    assert _bookmaker_name("ZZ", {"ZZ": {"bookmakerName": "Betfair"}}) == "Betfair"
    # fallback path (no entity) unchanged
    assert _bookmaker_name("BF", {}) == "Betfair Sportsbook"
