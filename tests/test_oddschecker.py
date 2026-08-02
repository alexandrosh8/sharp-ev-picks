"""OddsChecker curl_cffi/Hypernova ingestion contract."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from app.ingestion.oddschecker import _ProxySessionPool

from app.ingestion.base import EventDirectory, ScraperProxy
from app.ingestion.oddschecker import (
    OddsCheckerChallenge,
    OddsCheckerEmptyMarket,
    OddsCheckerError,
    OddsCheckerFetchResult,
    OddsCheckerHTTPError,
    OddsCheckerLoader,
    OddsCheckerParseError,
    OddsCheckerSecurityError,
    _find_match_payload,
    _line_bearing_selection,
    _other_market_detail,
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
from app.schemas.odds import OddsSnapshotIn


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


# --- tennis SET-vs-GAME handicap key collision (capture-side namespacing) -----
# Both "Set Handicap" and "Game(s) Handicap" used to slug to the SAME
# spreads_<line> detail at an identical numeric line, risking a mixed devig
# group of two different units (sets vs games). Fail-closed fix: the
# GAME-handicap vocabulary stays EXACTLY as-is (existing stamped picks keep
# matching); SET-handicap markets are routed to a distinct namespaced detail
# at capture, so a set-line group can never merge with a game-line one.


def test_set_handicap_routes_to_namespaced_sets_detail() -> None:
    from app.ingestion.oddschecker import _market_for_type

    assert _market_for_type("Set Handicap", "-1.5") == (Market.SPREADS, "spreads_sets_minus_1_5")
    assert _market_for_type("Sets Handicap", "+1.5") == (Market.SPREADS, "spreads_sets_plus_1_5")
    assert _market_for_type("Asian Set Handicap", "-2.5") == (
        Market.SPREADS,
        "spreads_sets_minus_2_5",
    )


def test_game_handicap_vocabulary_is_untouched() -> None:
    # The game-handicap keys must keep producing the EXACT historical detail —
    # renaming them would orphan every already-stamped spreads_* pick.
    from app.ingestion.oddschecker import _market_for_type

    assert _market_for_type("Handicap", "-1.5") == (Market.SPREADS, "spreads_minus_1_5")
    assert _market_for_type("Game Handicap", "-1.5") == (Market.SPREADS, "spreads_minus_1_5")
    assert _market_for_type("Games Handicap", "-1.5") == (Market.SPREADS, "spreads_minus_1_5")
    assert _market_for_type("Asian Handicap", "-1.5") == (Market.SPREADS, "spreads_minus_1_5")


def test_set_and_game_handicap_same_line_never_share_a_devig_key() -> None:
    from app.ingestion.oddschecker import _market_for_type

    set_result = _market_for_type("Set Handicap", "-1.5")
    game_result = _market_for_type("Games Handicap", "-1.5")
    assert set_result is not None and game_result is not None
    assert set_result[1] != game_result[1]


# --- corners/cards leak into the goals TEAM_TOTALS namespace (unit collision) --
# "Total Away Corners" / "Total Home Team Cards" substring-match the
# "total away"/"total home" team-total vocabulary, so a corners 4.5 line and a
# goals 4.5 line both slugged to team_totals_4_5 — one devig group mixing two
# units. Fail-closed fix: the team-total classifier rejects the same
# _EXCLUDED_PLAYER_PROP_TERMS the spread/total classifiers already reject; the
# markets flow to the sharp-anchored OTHER capture path instead.


@pytest.mark.parametrize(
    "market_type",
    [
        "Total Home Corners",
        "Total Away Corners",
        "Total Home Team Cards",
        "Total Away Team Cards",
        "Total Home Bookings",
    ],
)
def test_corners_and_cards_never_map_to_goals_team_totals(market_type: str) -> None:
    from app.ingestion.oddschecker import _market_for_type

    assert _market_for_type(market_type, "4.5") is None


def test_goal_team_totals_vocabulary_is_untouched() -> None:
    # Genuine goal-unit team totals must keep the EXACT historical detail —
    # renaming them would orphan every already-stamped team_totals_* pick.
    from app.ingestion.oddschecker import _market_for_type

    assert _market_for_type("Total Home Goals", "4.5") == (Market.TEAM_TOTALS, "team_totals_4_5")
    assert _market_for_type("Total Away Goals", "1.5") == (Market.TEAM_TOTALS, "team_totals_1_5")
    assert _market_for_type("Home Team Total", "2.5") == (Market.TEAM_TOTALS, "team_totals_2_5")


def test_home_corners_flow_to_sharp_anchored_other_capture() -> None:
    # Rejected from TEAM_TOTALS, a team-corners market takes the existing
    # OTHER route: captured with an oc_ detail when Betfair anchors it,
    # dropped entirely otherwise.
    directory = EventDirectory()
    payload = {
        "subeventId": 7001,
        "subeventName": "Arsenal vs Chelsea",
        "eventName": "Premier League Matches",
        "marketTypeName": "Total Home Corners",
        "bets": [{"betId": 1, "betName": "Over", "line": "4.5"}],
        "odds": [
            {"betId": 1, "bookmakerCode": "BF", "oddsDecimal": 1.9, "status": "ACTIVE"},
            {"betId": 1, "bookmakerCode": "WH", "oddsDecimal": 1.85, "status": "ACTIVE"},
        ],
    }

    dropped = parse_market_api_payloads(
        [payload], url="https://www.oddschecker.com/football/x/y/winner", directory=directory
    )
    assert dropped == []  # no TEAM_TOTALS snapshots, and OTHER is opt-in

    captured = parse_market_api_payloads(
        [payload],
        url="https://www.oddschecker.com/football/x/y/winner",
        directory=directory,
        capture_other=True,
    )
    assert {(s.market, s.selection, s.market_detail, s.bookmaker) for s in captured} == {
        (Market.OTHER, "Over", "oc_total_home_corners_4_5", "Betfair Exchange"),
        (Market.OTHER, "Over", "oc_total_home_corners_4_5", "William Hill"),
    }


def test_other_market_detail_preserves_identity_beyond_legacy_64_chars() -> None:
    prefix = "long capture market " + "x" * 80
    first = _other_market_detail(f"{prefix} alpha")
    second = _other_market_detail(f"{prefix} beta")

    assert len(first) > 64
    assert first != second
    assert first.endswith("_alpha")
    assert second.endswith("_beta")


def test_other_market_detail_rejects_instead_of_truncating_oversized_key() -> None:
    with pytest.raises(ValueError, match="512 UTF-8 bytes"):
        _other_market_detail("x" * 513)


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
    header: dict[str, object] = {
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


def _empty_odds_html() -> str:
    """A validly-fetched match page OddsChecker LISTS but has not PRICED yet: the
    bestOdds structure is present but its odds map is empty. OddsChecker serves
    these for future/unpriced fixtures (obs: 'FAILED rows have no usable price
    data'). It is a 200-OK page, not a block or malformed response."""
    header: dict[str, object] = {
        "repub": "OC",
        "eventName": "ATP Tour",
        "subeventName": "Player A vs Player B",
        "breadcrumbs": [
            {
                "id": 55501,
                "name": "Player A vs Player B",
                "url": "tennis/atp/a-v-b/winner",
                "type": "subevent",
            },
        ],
    }
    odds: dict[str, object] = {
        "repub": "OC",
        "bestOdds": {
            "bets": {"entities": {}, "ids": []},
            "odds": {},
            "markets": {"entities": {}, "ids": []},
            "bookmakers": {"entities": {}, "ids": []},
            "subeventConfig": {"subeventId": "55501", "name": "Player A vs Player B"},
        },
    }
    return f"<html><body>{_json_script(header)}{_json_script(odds)}</body></html>"


def test_find_match_payload_empty_odds_is_empty_market() -> None:
    """bestOdds present but odds map empty = a listed-but-unpriced match. This is
    a legitimate empty result the fetch layer returns [] for, NOT a failed
    match-page fetch — so it must raise OddsCheckerEmptyMarket, distinct from a
    genuine parse failure."""
    with pytest.raises(OddsCheckerEmptyMarket):
        _find_match_payload(_empty_odds_html())


def test_find_match_payload_no_bestodds_is_parse_error_not_empty_market() -> None:
    """A page with NO bestOdds structure at all is a genuine parse failure
    (malformed / soft-block), NOT the empty-but-valid case."""
    html = f"<html><body>{_json_script({'repub': 'OC', 'x': 1})}</body></html>"
    with pytest.raises(OddsCheckerParseError) as excinfo:
        _find_match_payload(html)
    assert not isinstance(excinfo.value, OddsCheckerEmptyMarket)


def test_find_match_payload_no_subevent_match_is_empty_market() -> None:
    """Populated bestOdds blobs exist but NONE prices the page's canonical subevent
    (OddsChecker served a flaky/partial page carrying only accumulator/related-match
    odds). Our target match is not priced on this fetch — that is EmptyMarket
    (fetch layer returns []), NOT a hard failure. The wrong-game guard is preserved:
    a mismatched blob is still never returned."""
    with pytest.raises(OddsCheckerEmptyMarket):
        # _match_html prices subeventId "101610031"; ask for a different canonical id.
        _find_match_payload(_match_html(), prefer_subevent_id="999999999")


def test_find_match_payload_subevent_match_still_returns_payload() -> None:
    """When a populated blob DOES match the canonical subevent id, it is returned
    (regression guard: the empty-market change must not break the happy path)."""
    payload = _find_match_payload(_match_html(), prefer_subevent_id="101610031")
    assert payload["bestOdds"]["subeventConfig"]["subeventId"] == "101610031"


async def test_empty_market_pages_are_counted_for_observability() -> None:
    """The empty-market path returns [] as a clean success (no failure count,
    no incomplete ratio), so the per-cycle counter is the ONLY signal that
    distinguishes genuinely unpriced fixtures from a provider payload drift
    silently emptying the slate — it must increment."""
    loader = OddsCheckerLoader(EventDirectory())
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/tennis/a-v-b/winner",
        html=_empty_odds_html(),
        status_code=200,
    )
    assert loader._cycle_empty_markets == 0
    result = await loader._parse_modern_or_legacy_match_page(page, now=None, session=None)
    assert result == []
    assert loader._cycle_empty_markets == 1


def test_parse_match_page_empty_odds_propagates_empty_market() -> None:
    """parse_match_page surfaces the empty-market signal so the fetch layer can
    return [] (no odds priced) instead of dropping to the legacy parser and
    counting the match as a failed fetch."""
    with pytest.raises(OddsCheckerEmptyMarket):
        parse_match_page(
            _empty_odds_html(),
            url="https://www.oddschecker.com/tennis/a-v-b/winner",
            directory=EventDirectory(),
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )


def test_match_parser_enforces_snapshot_ceiling_before_append() -> None:
    with pytest.raises(OddsCheckerSecurityError, match="snapshot ceiling"):
        parse_match_page(
            _match_html(),
            url="https://www.oddschecker.com/football/a-v-b/winner",
            directory=EventDirectory(),
            now=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
            max_snapshots=1,
        )


def _all_odds_payload() -> list[dict[str, object]]:
    common: dict[str, object] = {
        "subeventId": 101610031,
        "subeventName": "Arsenal vs Coventry",
        "subeventStartTime": "2026-08-21T19:00:00Z",
        "eventName": "English Premier League Matches",
    }
    rows: list[tuple[int, str, dict[str, object], float]] = [
        (10, "Win Market", {"betId": 1, "betName": "Arsenal"}, 1.9),
        (20, "Double Chance", {"betId": 2, "betName": "Arsenal or Draw"}, 1.5),
        (30, "Asian Total", {"betId": 3, "betName": "Over", "line": "2.5"}, 2.1),
    ]
    return [
        {
            **common,
            "marketId": market_id,
            "marketTypeName": market_type,
            "bets": [bet],
            "odds": [
                {
                    "betId": bet["betId"],
                    "bookmakerCode": "WH",
                    "oddsDecimal": price,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:59:00Z",
                }
            ],
        }
        for market_id, market_type, bet, price in rows
    ]


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
    now = datetime(2026, 7, 5, 10, 10, tzinfo=UTC)

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
        page,
        now=datetime(2026, 7, 5, 10, 10, tzinfo=UTC),
        session=None,
        markets=(Market.TOTALS,),
    )
    assert snapshots, "explicitly requested TOTALS override returned no rows"
    assert {s.market for s in snapshots} == {Market.TOTALS}


async def test_market_api_failure_does_not_fall_back_to_embedded_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page that advertises API markets must not become a healthy-looking
    embedded subset when that API fails; API-only markets would disappear."""
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
    with pytest.raises(OddsCheckerError, match="api down"):
        await loader._parse_modern_or_legacy_match_page(
            page,
            now=datetime(2026, 7, 5, 10, 10, tzinfo=UTC),
            session=None,
            markets=(Market.TOTALS,),
        )


async def test_transient_market_api_failure_escapes_for_proxy_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.ingestion.oddschecker as oc

    async def unavailable(*args: object, **kwargs: object) -> list[dict[str, object]]:
        del args, kwargs
        raise OddsCheckerHTTPError("rate limited", status_code=429)

    monkeypatch.setattr(oc, "fetch_market_api_payloads", unavailable)
    loader = OddsCheckerLoader(EventDirectory())
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/a-v-b/winner",
        html=_match_html(),
        status_code=200,
    )

    with pytest.raises(OddsCheckerHTTPError) as caught:
        await loader._parse_modern_or_legacy_match_page(
            page,
            now=datetime(2026, 7, 5, 10, 10, tzinfo=UTC),
            session=None,
        )
    assert caught.value.status_code == 429


async def test_market_api_nonempty_malformed_rows_raise_schema_failure() -> None:
    from app.ingestion import oddschecker as oc

    class Response:
        status_code = 200
        text = '["renamed-market-row"]'
        headers: dict[str, str] = {}
        url = "https://www.oddschecker.com/api/markets/v2/all-odds"

        def json(self) -> object:
            return ["renamed-market-row"]

    class Session:
        async def get(self, url: str, **kwargs: object) -> Response:
            del url, kwargs
            return Response()

    with pytest.raises(oc.OddsCheckerParseError, match="malformed rows"):
        await oc.fetch_market_api_payloads(
            ["1"],
            referer="https://www.oddschecker.com/football/a-v-b/winner",
            session=Session(),
        )


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


async def test_fetch_html_enforces_body_byte_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ingestion import oddschecker as oc

    monkeypatch.setattr(oc, "MAX_HTML_RESPONSE_BYTES", 8)
    session = _QueuedSession(
        [
            _QueuedResponse(
                text="x" * 9,
                url="https://www.oddschecker.com/football",
                headers={"content-type": "text/html"},
            )
        ]
    )
    with pytest.raises(OddsCheckerSecurityError, match="UpstreamBodyTooLarge"):
        await fetch_html("https://www.oddschecker.com/football", session=session)


async def test_fetch_html_rejects_final_redirect_host() -> None:
    session = _QueuedSession(
        [
            _QueuedResponse(
                text="ok",
                url="https://evil.invalid/redirected",
                headers={"content-type": "text/html"},
            )
        ]
    )
    with pytest.raises(OddsCheckerSecurityError, match="UnsafeUpstreamURL"):
        await fetch_html("https://www.oddschecker.com/football", session=session)


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
    """capture_other persists unmapped markets ONLY when Betfair (code BF since
    the 2026-08-02 recycle) prices them, never boosted markets, under
    Market.OTHER with an oc_<slug> detail."""
    directory = EventDirectory()
    payloads = [
        {  # unmapped + Betfair Exchange (BF) present -> captured as OTHER
            "subeventId": 7001,
            "subeventName": "Arsenal vs Chelsea",
            "eventName": "Premier League Matches",
            "marketTypeName": "Total Corners",
            "bets": [{"betId": 1, "betName": "Over", "line": "9.5"}],
            "odds": [
                {"betId": 1, "bookmakerCode": "BF", "oddsDecimal": 1.9, "status": "ACTIVE"},
                {"betId": 1, "bookmakerCode": "WH", "oddsDecimal": 1.85, "status": "ACTIVE"},
            ],
        },
        {  # unmapped + soft books only (no BF) -> dropped
            "subeventId": 7001,
            "subeventName": "Arsenal vs Chelsea",
            "marketTypeName": "Anytime Goalscorer",
            "bets": [{"betId": 2, "betName": "Some Player"}],
            "odds": [{"betId": 2, "bookmakerCode": "WH", "oddsDecimal": 3.5, "status": "ACTIVE"}],
        },
        {  # boosted market, even with BF -> dropped
            "subeventId": 7001,
            "subeventName": "Arsenal vs Chelsea",
            "marketTypeName": "Enhanced Win Market",
            "bets": [{"betId": 3, "betName": "Arsenal"}],
            "odds": [{"betId": 3, "bookmakerCode": "BF", "oddsDecimal": 5.0, "status": "ACTIVE"}],
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


def test_market_api_overflow_is_mapped_fail_closed_but_optional_prefix_bounded() -> None:
    with pytest.raises(OddsCheckerSecurityError, match="snapshot ceiling"):
        parse_market_api_payloads(
            _all_odds_payload(),
            url="https://www.oddschecker.com/football/x/y/winner",
            directory=EventDirectory(),
            max_snapshots=1,
        )

    optional_payload = {
        "subeventId": 7001,
        "subeventName": "Arsenal vs Chelsea",
        "marketTypeName": "Total Corners",
        "bets": [{"betId": 1, "betName": "Over", "line": "9.5"}],
        "odds": [
            {"betId": 1, "bookmakerCode": "BF", "oddsDecimal": 1.9, "status": "ACTIVE"},
            {"betId": 1, "bookmakerCode": "WH", "oddsDecimal": 1.85, "status": "ACTIVE"},
        ],
    }
    snapshots = parse_market_api_payloads(
        [optional_payload],
        url="https://www.oddschecker.com/football/x/y/winner",
        directory=EventDirectory(),
        capture_other=True,
        capture_only_other=True,
        truncate_on_limit=True,
        max_snapshots=1,
    )

    assert len(snapshots) == 1
    assert snapshots[0].market is Market.OTHER
    assert snapshots[0].bookmaker == "Betfair Exchange"


def _modern_html_with_optional_market() -> str:
    payload: dict[str, object] = {
        "repub": "OC",
        "bestOdds": {
            "bets": {
                "entities": {"1": {"ocBetId": 1, "betName": "Alpha", "marketId": 10}},
                "ids": [1],
            },
            "odds": {"1": {"WH": {"oddsDecimal": 2.0}}},
            "markets": {
                "entities": {
                    "10": {"ocMarketId": 10, "marketTypeName": "Win Market"},
                    "50": {"ocMarketId": 50, "marketTypeName": "Total Corners"},
                },
                "ids": [10, 50],
            },
        },
    }
    return _json_script(payload)


@pytest.mark.asyncio
async def test_optional_market_api_failure_preserves_mapped_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    calls: list[tuple[str, ...]] = []

    async def fetch(market_ids: list[str], **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        ids = tuple(str(market_id) for market_id in market_ids)
        calls.append(ids)
        if ids == ("10",):
            return [_all_odds_payload()[0]]
        raise OddsCheckerSecurityError("optional response exceeded body ceiling")

    monkeypatch.setattr(oc, "fetch_market_api_payloads", fetch)
    loader = OddsCheckerLoader(EventDirectory(), capture_other=True)
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/x/y/winner",
        html=_modern_html_with_optional_market(),
        status_code=200,
    )

    snapshots = await loader._parse_modern_or_legacy_match_page(
        page, now=datetime(2026, 7, 5, 10, 0, tzinfo=UTC), session=None
    )

    assert calls == [("10",), ("50",)]
    assert len(snapshots) == 1
    assert snapshots[0].market is Market.H2H


@pytest.mark.parametrize(
    ("optional_selection", "optional_event_id", "expect_optional"),
    [
        pytest.param("Over", 101610031, True, id="success"),
        pytest.param("x" * 10_000, 101610031, False, id="parse-error"),
        pytest.param("Over", 999999999, False, id="mismatched-event"),
        pytest.param("Over", None, False, id="missing-event"),
    ],
)
@pytest.mark.asyncio
async def test_optional_market_metadata_cannot_mutate_mapped_event(
    optional_selection: str,
    optional_event_id: int | None,
    expect_optional: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    optional_payload: dict[str, object] = {
        "marketId": 50,
        "marketTypeName": "Total Corners",
        "bets": [{"betId": 2, "betName": optional_selection, "line": "9.5"}],
        "odds": [
            {
                "betId": 2,
                "bookmakerCode": "BF",
                "oddsDecimal": 1.9,
                "status": "ACTIVE",
            }
        ],
        # Event metadata is deliberately absent. Optional archive payloads
        # must not replace the mapped response's authoritative team context.
    }
    if optional_event_id is not None:
        optional_payload["subeventId"] = optional_event_id

    async def fetch(market_ids: list[str], **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        ids = tuple(str(market_id) for market_id in market_ids)
        return [_all_odds_payload()[0]] if ids == ("10",) else [optional_payload]

    monkeypatch.setattr(oc, "fetch_market_api_payloads", fetch)
    directory = EventDirectory()
    loader = OddsCheckerLoader(directory, capture_other=True)
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/x/y/winner",
        html=_modern_html_with_optional_market(),
        status_code=200,
    )

    snapshots = await loader._parse_modern_or_legacy_match_page(
        page, now=datetime(2026, 7, 5, 10, 0, tzinfo=UTC), session=None
    )

    event = directory.lookup("oddschecker:101610031")
    assert event is not None
    assert (event.home, event.away, event.league) == (
        "Arsenal",
        "Coventry",
        "English Premier League",
    )
    assert event.starts_at == datetime(2026, 8, 21, 19, 0, tzinfo=UTC)
    expected_markets = {Market.H2H, Market.OTHER} if expect_optional else {Market.H2H}
    assert {snapshot.market for snapshot in snapshots} == expected_markets
    assert {snapshot.event_id for snapshot in snapshots} == {"oddschecker:101610031"}


@pytest.mark.asyncio
async def test_optional_markets_are_not_fetched_without_a_mapped_event_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    calls: list[tuple[str, ...]] = []
    mapped_payload = _all_odds_payload()[0]
    raw_odds = mapped_payload["odds"]
    assert isinstance(raw_odds, list)
    mapped_payload["odds"] = [
        {**raw_odd, "status": "SUSPENDED"} for raw_odd in raw_odds if isinstance(raw_odd, dict)
    ]

    async def fetch(market_ids: list[str], **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        ids = tuple(str(market_id) for market_id in market_ids)
        calls.append(ids)
        if ids != ("10",):
            raise AssertionError("optional markets must not load without mapped identity")
        return [mapped_payload]

    monkeypatch.setattr(oc, "fetch_market_api_payloads", fetch)
    loader = OddsCheckerLoader(EventDirectory(), capture_other=True)
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/x/y/winner",
        html=_modern_html_with_optional_market(),
        status_code=200,
    )

    snapshots = await loader._parse_modern_or_legacy_match_page(
        page, now=datetime(2026, 7, 5, 10, 0, tzinfo=UTC), session=None
    )

    assert snapshots == []
    assert calls == [("10",)]


@pytest.mark.asyncio
async def test_mapped_market_api_failure_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    async def fetch(market_ids: object, **kwargs: object) -> list[dict[str, object]]:
        del market_ids, kwargs
        raise OddsCheckerSecurityError("mapped response exceeded body ceiling")

    monkeypatch.setattr(oc, "fetch_market_api_payloads", fetch)
    loader = OddsCheckerLoader(EventDirectory(), capture_other=True)
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/x/y/winner",
        html=_modern_html_with_optional_market(),
        status_code=200,
    )

    with pytest.raises(OddsCheckerSecurityError, match="mapped response"):
        await loader._parse_modern_or_legacy_match_page(page, now=None, session=None)


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
            (
                "api/markets/v2/all-odds",
                _QueuedResponse(
                    text=json.dumps(_all_odds_payload()),
                    url="https://www.oddschecker.com/api/markets",
                ),
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
async def test_pool_evict_defers_close_until_shared_leases_release() -> None:
    from app.ingestion.oddschecker import _ProxySessionPool

    proxy = ScraperProxy(url="http://p0", username="", password="")
    session = _PoolFakeSession([])
    pool = _ProxySessionPool((proxy,), session_factory=lambda unused: session)
    first = pool.acquire_lease()
    sibling = pool.acquire_lease()

    await pool.evict(first)
    assert session.closed is False
    await pool.release(first)
    assert session.closed is False
    await pool.release(sibling)
    assert session.closed is True


@pytest.mark.asyncio
async def test_partial_match_failure_is_retained_but_explicitly_incomplete() -> None:
    captured_at = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del now, session, markets
            if url.endswith("/bad"):
                raise OddsCheckerError("market API schema drift")
            return [
                OddsSnapshotIn(
                    event_id="good-event",
                    bookmaker="bet365",
                    market=Market.H2H,
                    selection="Alpha",
                    decimal_odds=2.0,
                    captured_at=captured_at,
                    ingested_at=captured_at,
                )
            ]

    loader = Loader(EventDirectory())
    snapshots = await loader._gather_snapshots(
        ["https://www.oddschecker.com/good", "https://www.oddschecker.com/bad"],
        None,
        pipeline_key="soccer",
    )

    assert len(snapshots) == 1
    assert loader.last_fetch_complete["soccer"] is False
    assert loader.last_fetch_completeness_reason["soccer"] == "1/2 listed match fetch(es) failed"


async def test_optional_rows_never_make_cycle_snapshot_ceiling_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    captured_at = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)

    market_scopes: list[tuple[Market, ...] | None] = []

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del now, session
            market_scopes.append(None if markets is None else tuple(markets))
            market = Market.OTHER if url.endswith("/optional") else Market.H2H
            return [
                OddsSnapshotIn(
                    event_id=url,
                    bookmaker="Betfair Exchange",
                    market=market,
                    selection="Alpha",
                    decimal_odds=2.0,
                    captured_at=captured_at,
                    ingested_at=captured_at,
                    market_detail="oc_archive" if market is Market.OTHER else "h2h",
                )
            ]

    monkeypatch.setattr(oc, "MAX_SNAPSHOTS_PER_CYCLE", 1)
    loader = Loader(EventDirectory(), capture_other=True, max_clients=1)
    snapshots = await loader._gather_snapshots(
        [
            "https://www.oddschecker.com/optional",
            "https://www.oddschecker.com/mapped",
        ],
        None,
        pipeline_key="soccer",
    )

    assert [snapshot.market for snapshot in snapshots] == [Market.H2H]
    assert market_scopes[0] is None
    assert market_scopes[1] == tuple(market for market in Market if market is not Market.OTHER)
    assert loader.last_fetch_complete["soccer"] is True
    assert loader.last_fetch_completeness_reason["soccer"] == ""


async def test_gather_match_url_ceiling_is_explicitly_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    monkeypatch.setattr(oc, "MAX_MATCH_URLS_PER_CYCLE", 1)
    loader = OddsCheckerLoader(EventDirectory())
    snapshots = await loader._gather_snapshots(
        [
            "https://www.oddschecker.com/football/a/winner",
            "https://www.oddschecker.com/football/b/winner",
        ],
        None,
        pipeline_key="soccer",
    )
    assert snapshots == []
    assert loader.last_fetch_complete["soccer"] is False
    assert "URL ceiling" in loader.last_fetch_completeness_reason["soccer"]


async def test_gather_cycle_snapshot_ceiling_is_explicitly_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    captured_at = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del now, session, markets
            return [
                OddsSnapshotIn(
                    event_id=url,
                    bookmaker="bet365",
                    market=Market.H2H,
                    selection="Alpha",
                    decimal_odds=2.0,
                    captured_at=captured_at,
                    ingested_at=captured_at,
                )
            ]

    monkeypatch.setattr(oc, "MAX_SNAPSHOTS_PER_CYCLE", 1)
    loader = Loader(EventDirectory())
    snapshots = await loader._gather_snapshots(
        [
            "https://www.oddschecker.com/football/a/winner",
            "https://www.oddschecker.com/football/b/winner",
        ],
        None,
        pipeline_key="soccer",
    )
    assert len(snapshots) == 1
    assert loader.last_fetch_complete["soccer"] is False
    assert "snapshot ceiling" in loader.last_fetch_completeness_reason["soccer"]


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
                    _QueuedResponse(
                        text=json.dumps(_all_odds_payload()),
                        url="https://www.oddschecker.com/api/markets",
                    ),
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
    # 3 matches x 3 active all-odds snapshots each.
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
    """Audit 2026-07-10 (M863), REVISED 2026-08-02: the feed labels the
    exchange with the bare display name 'Betfair' — and effective_odds maps
    bare 'betfair' to 5% exchange commission. OddsChecker recycles CODES, so
    the bare brand now disambiguates via the entity's live ``bookmakerType``;
    only a typeless bare brand falls back to the (corrected) static map.
    Unambiguous entity names pass through; unknown typeless codes are
    rejected."""
    from app.ingestion.oddschecker import _bookmaker_name

    # typeless bare brand: the corrected fallback map decides (BF = exchange).
    assert _bookmaker_name("BF", {"BF": {"bookmakerName": "Betfair"}}) == "Betfair Exchange"
    assert _bookmaker_name("OE", {"OE": {"bookmakerName": "Betfair"}}) == "10bet"
    # unambiguous display names pass through
    assert (
        _bookmaker_name("BF", {"BF": {"bookmakerName": "Betfair Sportsbook"}})
        == "Betfair Sportsbook"
    )
    # unknown TYPELESS code with the ambiguous brand cannot safely select
    # sportsbook versus exchange commission semantics.
    assert _bookmaker_name("ZZ", {"ZZ": {"bookmakerName": "Betfair"}}) is None
    # fallback path (no entity) uses the corrected map
    assert _bookmaker_name("BF", {}) == "Betfair Exchange"
    # A raw provider code is not a canonical bookmaker identity.
    assert _bookmaker_name("ZZ", {}) is None


def test_canonical_header_id_never_falls_back_to_another_embedded_match() -> None:
    """Related/accumulator blobs must never be attributed to the page URL."""
    header: dict[str, object] = {
        "subeventName": "Target FC vs Target United",
        "subeventStartTime": "2026-07-06T10:00:00Z",
        "breadcrumbs": [{"type": "subevent", "id": "target-123"}],
    }
    wrong_match: dict[str, object] = {
        "bestOdds": {
            "bets": {"entities": {"1": {"betName": "Wrong FC", "marketId": "10"}}},
            "odds": {"1": {"WH": {"oddsDecimal": 2.0, "status": "ACTIVE"}}},
            "markets": {"entities": {"10": {"marketTypeName": "Win Market"}}},
            "subeventConfig": {
                "subeventId": "wrong-999",
                "homeTeamName": "Wrong FC",
                "awayTeamName": "Wrong United",
            },
        }
    }
    html = f"{_json_script(header)}{_json_script(wrong_match)}"
    directory = EventDirectory()

    with pytest.raises(OddsCheckerError, match="canonical subevent id"):
        parse_match_page(
            html,
            url="https://www.oddschecker.com/football/target-fc-v-target-united/winner",
            directory=directory,
        )
    assert directory.lookup("oddschecker:wrong-999") is None
    assert supported_market_ids_from_match_page(html) == []


def test_market_api_isolates_nonfinite_prices_and_bad_provider_timestamps() -> None:
    now = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    bets = [{"betId": index, "betName": f"Team {index}"} for index in range(1, 7)]
    odds = [
        {"betId": 1, "bookmakerCode": "WH", "oddsDecimal": float("nan"), "status": "ACTIVE"},
        {"betId": 2, "bookmakerCode": "WH", "oddsDecimal": float("inf"), "status": "ACTIVE"},
        {"betId": 3, "bookmakerCode": "WH", "oddsDecimal": 1001.0, "status": "ACTIVE"},
        {
            "betId": 4,
            "bookmakerCode": "WH",
            "oddsDecimal": 2.04,
            "status": "ACTIVE",
            "betFeedTimestamp": "2026-07-05T11:00:00Z",
        },
        {
            "betId": 5,
            "bookmakerCode": "WH",
            "oddsDecimal": 2.05,
            "status": "ACTIVE",
            "betFeedTimestamp": "not-a-timestamp",
        },
        {"betId": 6, "bookmakerCode": "WH", "oddsDecimal": 2.06, "status": "ACTIVE"},
    ]
    snapshots = parse_market_api_payloads(
        [
            {
                "subeventId": "finite-1",
                "subeventName": "Home FC vs Away FC",
                "marketTypeName": "Win Market",
                "bets": bets,
                "odds": odds,
            }
        ],
        url="https://www.oddschecker.com/football/home-v-away/winner",
        directory=EventDirectory(),
        now=now,
    )

    assert [(snapshot.selection, snapshot.decimal_odds) for snapshot in snapshots] == [
        ("Team 6", 2.06)
    ]
    assert snapshots[0].captured_at == now


def test_epoch_millisecond_overflow_is_not_fatal() -> None:
    from app.ingestion.oddschecker import _parse_epoch_ms

    assert _parse_epoch_ms(10**100) is None
    assert _parse_epoch_ms(float("inf")) is None


@pytest.mark.asyncio
async def test_fetch_html_preserves_http_status_and_bounded_retry_after() -> None:
    class Response:
        status_code = 429
        text = "rate limited"
        headers = {"Retry-After": "999999"}
        url = "https://www.oddschecker.com/football"

    class Session:
        async def get(self, url: str, **kwargs: object) -> Response:
            del url, kwargs
            return Response()

    with pytest.raises(OddsCheckerHTTPError) as caught:
        await fetch_html("https://www.oddschecker.com/football", session=Session())
    assert caught.value.status_code == 429
    assert caught.value.retry_after == 60.0

    from app.ingestion.oddschecker import _retry_after_seconds

    assert _retry_after_seconds({"Retry-After": "inf"}) is None
    assert _retry_after_seconds({"Retry-After": "NaN"}) is None


@pytest.mark.asyncio
async def test_proxy_retry_evicts_challenged_session_and_rotates_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc
    from app.ingestion.oddschecker import _ProxySessionPool

    monkeypatch.setattr(oc, "_DISCOVERY_CHALLENGE_BACKOFF_RANGE", (0.0, 0.0))
    proxies = (
        ScraperProxy(url="http://p0", username="", password=""),
        ScraperProxy(url="http://p1", username="", password=""),
    )
    created: list[tuple[ScraperProxy | None, _PoolFakeSession]] = []

    def factory(proxy: ScraperProxy | None) -> _PoolFakeSession:
        result = _PoolFakeSession([])
        created.append((proxy, result))
        return result

    pool = _ProxySessionPool(proxies, session_factory=factory)

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, markets
            if session is created[0][1]:
                raise OddsCheckerChallenge("challenge")
            return ["snapshot"]

    loader = Loader(EventDirectory())
    loader._session_pool = pool
    snapshots = await loader._gather_snapshots(["https://www.oddschecker.com/a/winner"], None)

    assert snapshots == ["snapshot"]
    assert [proxy for proxy, _session in created] == [proxies[0], proxies[1]]
    assert created[0][1].closed is True
    await pool.aclose()


@pytest.mark.asyncio
async def test_proxy_retry_evicts_the_second_transient_failure_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc
    from app.ingestion.oddschecker import _ProxySessionPool

    monkeypatch.setattr(oc, "_DISCOVERY_CHALLENGE_BACKOFF_RANGE", (0.0, 0.0))
    proxies = (
        ScraperProxy(url="http://p0", username="", password=""),
        ScraperProxy(url="http://p1", username="", password=""),
    )
    created: list[_PoolFakeSession] = []

    def factory(proxy: ScraperProxy | None) -> _PoolFakeSession:
        del proxy
        result = _PoolFakeSession([])
        created.append(result)
        return result

    pool = _ProxySessionPool(proxies, session_factory=factory)

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, session, markets
            raise OddsCheckerChallenge("challenge")

    loader = Loader(EventDirectory())
    loader._session_pool = pool

    assert await loader._gather_snapshots(["https://www.oddschecker.com/a/winner"], None) == []
    assert len(created) == 2
    assert all(session.closed for session in created)
    assert pool._sessions == {}


def _challenge_pool(created: list[_PoolFakeSession]) -> _ProxySessionPool:
    from app.ingestion.oddschecker import _ProxySessionPool

    proxies = (
        ScraperProxy(url="http://p0", username="", password=""),
        ScraperProxy(url="http://p1", username="", password=""),
    )

    def factory(proxy: ScraperProxy | None) -> _PoolFakeSession:
        del proxy
        session = _PoolFakeSession([])
        created.append(session)
        return session

    return _ProxySessionPool(proxies, session_factory=factory)


@pytest.mark.asyncio
async def test_match_page_challenge_retry_backs_off_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Challenge-then-success: the page gets ONE rotated-session retry with the
    jittered challenge backoff and is NOT counted as a failed fetch."""
    from app.ingestion import oddschecker as oc

    backoff_calls = 0

    async def backoff() -> None:
        nonlocal backoff_calls
        backoff_calls += 1

    monkeypatch.setattr(oc, "_discovery_challenge_backoff", backoff)
    created: list[_PoolFakeSession] = []
    pool = _challenge_pool(created)
    snapshot = SimpleNamespace(event_id="oc:e1", market=Market.H2H)

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, markets
            if session is created[0]:
                raise OddsCheckerChallenge("challenge")
            return [snapshot]

    loader = Loader(EventDirectory())
    loader._session_pool = pool
    snapshots = await loader._gather_snapshots(
        ["https://www.oddschecker.com/a/winner"], None, pipeline_key="soccer"
    )

    assert snapshots == [snapshot]
    assert backoff_calls == 1
    assert created[0].closed is True
    assert loader.last_fetch_complete["soccer"] is True
    assert loader.last_fetch_incomplete_ratio["soccer"] == 0.0
    assert loader.last_fetch_challenge_retries["soccer"] == 1
    assert loader.last_fetch_challenge_failures["soccer"] == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_match_page_double_challenge_still_counted_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Challenge-challenge: exactly two attempts (bounded), then the page stays a
    failed fetch in the incomplete ratio — fail-closed accounting unchanged."""
    from app.ingestion import oddschecker as oc

    backoff_calls = 0

    async def backoff() -> None:
        nonlocal backoff_calls
        backoff_calls += 1

    monkeypatch.setattr(oc, "_discovery_challenge_backoff", backoff)
    created: list[_PoolFakeSession] = []
    pool = _challenge_pool(created)
    attempts = 0

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, session, markets
            nonlocal attempts
            attempts += 1
            raise OddsCheckerChallenge("challenge")

    loader = Loader(EventDirectory())
    loader._session_pool = pool
    snapshots = await loader._gather_snapshots(
        ["https://www.oddschecker.com/a/winner"], None, pipeline_key="soccer"
    )

    assert snapshots == []
    assert attempts == 2
    assert backoff_calls == 1
    assert loader.last_fetch_complete["soccer"] is False
    assert loader.last_fetch_incomplete_ratio["soccer"] == 1.0
    assert loader.last_fetch_challenge_retries["soccer"] == 1
    assert loader.last_fetch_challenge_failures["soccer"] == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_match_page_transient_non_challenge_retry_skips_challenge_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeouts keep the existing single rotated retry, never the challenge
    backoff, and never increment the challenge counters."""
    from app.ingestion import oddschecker as oc

    async def backoff() -> None:
        raise AssertionError("challenge backoff must not run for timeouts")

    monkeypatch.setattr(oc, "_discovery_challenge_backoff", backoff)
    created: list[_PoolFakeSession] = []
    pool = _challenge_pool(created)
    attempts = 0

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, session, markets
            nonlocal attempts
            attempts += 1
            raise TimeoutError("timed out")

    loader = Loader(EventDirectory())
    loader._session_pool = pool
    snapshots = await loader._gather_snapshots(
        ["https://www.oddschecker.com/a/winner"], None, pipeline_key="soccer"
    )

    assert snapshots == []
    assert attempts == 2
    assert loader.last_fetch_incomplete_ratio["soccer"] == 1.0
    assert loader.last_fetch_challenge_retries["soccer"] == 0
    assert loader.last_fetch_challenge_failures["soccer"] == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_match_page_non_transient_error_never_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    async def backoff() -> None:
        raise AssertionError("challenge backoff must not run for parse errors")

    monkeypatch.setattr(oc, "_discovery_challenge_backoff", backoff)
    created: list[_PoolFakeSession] = []
    pool = _challenge_pool(created)
    attempts = 0

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, session, markets
            nonlocal attempts
            attempts += 1
            raise OddsCheckerParseError("bad payload")

    loader = Loader(EventDirectory())
    loader._session_pool = pool
    snapshots = await loader._gather_snapshots(
        ["https://www.oddschecker.com/a/winner"], None, pipeline_key="soccer"
    )

    assert snapshots == []
    assert attempts == 1
    assert loader.last_fetch_incomplete_ratio["soccer"] == 1.0
    assert loader.last_fetch_challenge_retries["soccer"] == 0
    assert loader.last_fetch_challenge_failures["soccer"] == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_match_page_challenge_retries_are_concurrent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two challenged pages must sit in the retry backoff AT THE SAME TIME: a
    barrier inside the backoff deadlocks (and fails the fetch via timeout) if
    the retry path serializes the concurrent page tasks."""
    from app.ingestion import oddschecker as oc

    entered = 0
    both_entered = asyncio.Event()

    async def backoff() -> None:
        nonlocal entered
        entered += 1
        if entered >= 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2.0)

    monkeypatch.setattr(oc, "_discovery_challenge_backoff", backoff)
    created: list[_PoolFakeSession] = []
    pool = _challenge_pool(created)
    calls: dict[str, int] = {}

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del now, session, markets
            calls[url] = calls.get(url, 0) + 1
            if calls[url] == 1:
                raise OddsCheckerChallenge("challenge")
            return [SimpleNamespace(event_id=url, market=Market.H2H)]

    loader = Loader(EventDirectory())
    loader._session_pool = pool
    urls = [
        "https://www.oddschecker.com/a/winner",
        "https://www.oddschecker.com/b/winner",
    ]
    snapshots = await loader._gather_snapshots(urls, None, pipeline_key="soccer")

    assert entered == 2
    assert {snapshot.event_id for snapshot in snapshots} == set(urls)
    assert loader.last_fetch_incomplete_ratio["soccer"] == 0.0
    assert loader.last_fetch_challenge_retries["soccer"] == 2
    assert loader.last_fetch_challenge_failures["soccer"] == 0
    await pool.aclose()


def test_malformed_and_nonfinite_market_lines_fail_closed() -> None:
    from app.ingestion.oddschecker import _market_for_type

    for line in ("not-a-line", float("nan"), float("inf"), object()):
        assert _market_for_type("Asian Handicap", line) is None


@pytest.mark.asyncio
async def test_proxy_pool_closes_every_session_when_one_close_fails() -> None:
    from app.ingestion.oddschecker import _ProxySessionPool

    class CloseSession(_PoolFakeSession):
        def __init__(self, *, fail: bool) -> None:
            super().__init__([])
            self.fail = fail

        async def close(self) -> None:
            self.closed = True
            if self.fail:
                raise RuntimeError("close failed")

    created: list[CloseSession] = []

    def factory(proxy: ScraperProxy | None) -> CloseSession:
        del proxy
        result = CloseSession(fail=not created)
        created.append(result)
        return result

    pool = _ProxySessionPool(
        (
            ScraperProxy(url="http://p0", username="", password=""),
            ScraperProxy(url="http://p1", username="", password=""),
        ),
        session_factory=factory,
    )
    pool.acquire()
    pool.acquire()

    await pool.aclose()

    assert all(session.closed for session in created)
    assert pool._sessions == {}


@pytest.mark.asyncio
async def test_scheduler_discovery_retries_typed_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    calls = 0

    async def discover(*args: object, **kwargs: object) -> list[str]:
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 1:
            raise OddsCheckerHTTPError("unavailable", status_code=503)
        return []

    monkeypatch.setattr(oc, "discover_sport_daily_match_urls", discover)
    loader = OddsCheckerLoader.for_scheduler(EventDirectory())

    assert await loader._fetch_sport("soccer", None) == []
    assert calls == 2


@pytest.mark.asyncio
async def test_scheduler_discovery_evicts_failed_pool_lease_and_rotates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    proxies = (
        ScraperProxy(url="http://p0", username="", password=""),
        ScraperProxy(url="http://p1", username="", password=""),
    )
    created: list[tuple[ScraperProxy | None, _PoolFakeSession]] = []

    def factory(proxy: ScraperProxy | None) -> _PoolFakeSession:
        result = _PoolFakeSession([])
        created.append((proxy, result))
        return result

    pool = oc._ProxySessionPool(proxies, session_factory=factory)
    seen_sessions: list[object] = []

    async def discover(*args: object, **kwargs: object) -> list[str]:
        del args
        seen_sessions.append(kwargs["session"])
        if len(seen_sessions) == 1:
            raise OddsCheckerChallenge("challenge")
        return []

    monkeypatch.setattr(oc, "discover_sport_daily_match_urls", discover)
    loader = OddsCheckerLoader.for_scheduler(EventDirectory())
    loader._session_pool = pool

    assert await loader._fetch_sport("soccer", None) == []
    assert seen_sessions == [created[0][1], created[1][1]]
    assert [proxy for proxy, _session in created] == [proxies[0], proxies[1]]
    assert created[0][1].closed is True
    await pool.aclose()


@pytest.mark.asyncio
async def test_scheduler_discovery_survives_two_challenges_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    monkeypatch.setattr(oc, "_DISCOVERY_CHALLENGE_BACKOFF_RANGE", (0.0, 0.0))
    calls = 0

    async def discover(*args: object, **kwargs: object) -> list[str]:
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls < 3:
            raise OddsCheckerChallenge("challenge")
        return []

    monkeypatch.setattr(oc, "discover_sport_daily_match_urls", discover)
    loader = OddsCheckerLoader.for_scheduler(EventDirectory())

    assert await loader._fetch_sport("soccer", None) == []
    assert calls == 3
    assert loader.last_fetch_complete["soccer"] is True


@pytest.mark.asyncio
async def test_scheduler_discovery_challenge_exhaustion_degrades_to_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 challenges must NOT hard-fail the poll: the cycle degrades to
    source-incomplete (fail-closed withholds picks) instead of raising."""
    from app.ingestion import oddschecker as oc

    monkeypatch.setattr(oc, "_DISCOVERY_CHALLENGE_BACKOFF_RANGE", (0.0, 0.0))
    calls = 0

    async def discover(*args: object, **kwargs: object) -> list[str]:
        nonlocal calls
        del args, kwargs
        calls += 1
        raise OddsCheckerChallenge("challenge")

    monkeypatch.setattr(oc, "discover_sport_daily_match_urls", discover)
    loader = OddsCheckerLoader.for_scheduler(EventDirectory())

    assert await loader._fetch_sport("soccer", None) == []
    assert calls == 3
    assert loader.last_fetch_complete["soccer"] is False
    assert "challenge" in loader.last_fetch_completeness_reason["soccer"]
    assert loader.last_fetch_incomplete_ratio["soccer"] == 1.0
    assert loader.last_fetch_matches["soccer"] == 0
    assert loader.last_fetch_event_ids["soccer"] == ()


@pytest.mark.asyncio
async def test_scheduler_discovery_total_blackout_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-challenge transient blackout (nothing fetchable) keeps raising."""
    from app.ingestion import oddschecker as oc

    calls = 0

    async def discover(*args: object, **kwargs: object) -> list[str]:
        nonlocal calls
        del args, kwargs
        calls += 1
        raise ConnectionError("connection refused")

    monkeypatch.setattr(oc, "discover_sport_daily_match_urls", discover)
    loader = OddsCheckerLoader.for_scheduler(EventDirectory())

    with pytest.raises(ConnectionError):
        await loader._fetch_sport("soccer", None)
    assert calls == 2


@pytest.mark.asyncio
async def test_linked_legacy_pages_honor_per_call_market_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import oddschecker as oc

    parsed_scopes: list[tuple[Market, ...] | None] = []

    def parse(
        *args: object,
        markets: tuple[Market, ...] | None = None,
        **kwargs: object,
    ) -> list[OddsSnapshotIn]:
        del args, kwargs
        parsed_scopes.append(markets)
        return []

    def discover(*args: object, **kwargs: object) -> list[str]:
        del args, kwargs
        return ["https://www.oddschecker.com/basketball/a/total-points"]

    async def fetch(*args: object, **kwargs: object) -> OddsCheckerFetchResult:
        del args, kwargs
        return OddsCheckerFetchResult(
            url="https://www.oddschecker.com/basketball/a/total-points",
            html="linked",
            status_code=200,
        )

    monkeypatch.setattr(oc, "parse_legacy_match_page", parse)
    monkeypatch.setattr(oc, "discover_legacy_market_urls", discover)
    monkeypatch.setattr(oc, "fetch_html", fetch)
    loader = OddsCheckerLoader(EventDirectory(), markets=(Market.H2H,))
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/basketball/a/winner",
        html="main",
        status_code=200,
    )

    await loader._parse_legacy_match_with_linked_markets(
        page,
        now=datetime(2026, 7, 5, 10, 0, tzinfo=UTC),
        session=None,
        markets=(Market.TOTALS,),
    )

    assert parsed_scopes == [(Market.TOTALS,), (Market.TOTALS,)]


# --------------------------------------------------------------------------- #
# TASK PERF (2026-07-26): the full-page Hypernova parse (BeautifulSoup +
# json.loads over every application/json script) must run ONCE per fetched
# match page. Previously supported_market_ids_from_match_page and the
# parse_match_page fallback each re-ran it (header + bestOdds lookups), i.e.
# 4 identical full-page parses per page — ~1s of duplicated CPU.
# --------------------------------------------------------------------------- #
async def test_match_page_hypernova_parse_runs_once_per_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.ingestion.oddschecker as oc

    calls = {"n": 0}
    real = oc.hypernova_payloads

    def counting(html: str) -> list[dict[str, object]]:
        calls["n"] += 1
        return real(html)

    async def no_api_rows(
        market_ids: object, *, referer: object, session: object = None, proxy: object = None
    ) -> list[dict[str, object]]:
        return []  # force the parse_match_page embedded fallback too

    monkeypatch.setattr(oc, "hypernova_payloads", counting)
    monkeypatch.setattr(oc, "fetch_market_api_payloads", no_api_rows)
    loader = OddsCheckerLoader(EventDirectory(), markets=(Market.H2H,))
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/english/premier-league/arsenal-v-coventry/winner",
        html=_match_html(),
        status_code=200,
    )
    snapshots = await loader._parse_modern_or_legacy_match_page(
        page,
        now=datetime(2026, 7, 5, 10, 10, tzinfo=UTC),
        session=None,
    )
    assert snapshots, "embedded fallback must still produce rows"
    assert calls["n"] == 1, f"hypernova_payloads ran {calls['n']}x for one page fetch"


def test_parse_helpers_reuse_supplied_payloads_without_reparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a pre-parsed payload list supplied, neither helper may re-parse."""
    import app.ingestion.oddschecker as oc

    html = _match_html()
    payloads = oc.hypernova_payloads(html)

    def boom(_html: str) -> list[dict[str, object]]:
        raise AssertionError("hypernova_payloads must not be re-invoked")

    monkeypatch.setattr(oc, "hypernova_payloads", boom)
    ids = supported_market_ids_from_match_page(html, markets=(Market.H2H,), payloads=payloads)
    assert ids == ["10"]
    snapshots = parse_match_page(
        html,
        url="https://www.oddschecker.com/football/english/premier-league/arsenal-v-coventry/winner",
        directory=EventDirectory(),
        now=datetime(2026, 7, 5, 10, 10, tzinfo=UTC),
        payloads=payloads,
    )
    assert snapshots


# --- 2026-08-02 recycled bookmaker codes (live-feed verified) -----------------
# Trimmed from the live ``bestOdds.bookmakers.entities`` capture (2026-08-02,
# cross-checked against the read-only Betfair Exchange API): OddsChecker
# RECYCLED the codes — "OE" is now the TRADITIONAL book 10bet while "BF" is the
# Betfair EXCHANGE (its selection ids embed real Betfair market ids).
_LIVE_BOOKMAKER_ENTITIES: dict[str, object] = {
    "B3": {"bookmakerType": "traditional", "bookmakerCode": "B3", "bookmakerName": "bet365"},
    "WH": {
        "bookmakerType": "traditional",
        "bookmakerCode": "WH",
        "bookmakerName": "William Hill",
    },
    "OE": {"bookmakerType": "traditional", "bookmakerCode": "OE", "bookmakerName": "10bet"},
    "BF": {"bookmakerType": "exchange", "bookmakerCode": "BF", "bookmakerName": "Betfair"},
    "MA": {"bookmakerType": "exchange", "bookmakerCode": "MA", "bookmakerName": "Matchbook"},
}


def test_live_entities_resolve_recycled_codes_and_sharp_anchor_set() -> None:
    """Entities present: names come from the live feed (OE=10bet, BF=Betfair
    Exchange via bookmakerType), and the sharp-anchor code set derives {BF}
    (betfair-named exchange only — Matchbook is exchange but not the anchor)."""
    from app.ingestion.oddschecker import _bookmaker_name, _sharp_anchor_codes

    assert _bookmaker_name("OE", _LIVE_BOOKMAKER_ENTITIES) == "10bet"
    assert _bookmaker_name("BF", _LIVE_BOOKMAKER_ENTITIES) == "Betfair Exchange"
    assert _bookmaker_name("MA", _LIVE_BOOKMAKER_ENTITIES) == "Matchbook"
    assert _sharp_anchor_codes(_LIVE_BOOKMAKER_ENTITIES) == frozenset({"BF"})


def test_bare_betfair_resolves_by_bookmaker_type() -> None:
    """A bare 'Betfair' display name disambiguates via the entity's live
    bookmakerType (replaces the 2026-07-10 M863 code-fallback branch, which
    renamed the true exchange to Sportsbook via the inverted static map)."""
    from app.ingestion.oddschecker import _bookmaker_name

    exchange = {"BF": {"bookmakerName": "Betfair", "bookmakerType": "exchange"}}
    assert _bookmaker_name("BF", exchange) == "Betfair Exchange"
    traditional = {"BF": {"bookmakerName": "Betfair", "bookmakerType": "traditional"}}
    assert _bookmaker_name("BF", traditional) == "Betfair Sportsbook"
    # Even an off-map code disambiguates when the live type is present.
    assert (
        _bookmaker_name("ZZ", {"ZZ": {"bookmakerName": "Betfair", "bookmakerType": "exchange"}})
        == "Betfair Exchange"
    )
    # Bare brand without a type: only the audited fallback map may resolve it.
    assert _bookmaker_name("BF", {"BF": {"bookmakerName": "Betfair"}}) == "Betfair Exchange"
    assert _bookmaker_name("ZZ", {"ZZ": {"bookmakerName": "Betfair"}}) is None


def test_corrected_fallbacks_apply_only_without_entities() -> None:
    from app.ingestion.oddschecker import (
        _BOOKMAKER_FALLBACKS,
        _SHARP_ANCHOR_BOOK_CODES,
        _bookmaker_name,
        _sharp_anchor_codes,
    )

    assert _BOOKMAKER_FALLBACKS["BF"] == "Betfair Exchange"
    assert _BOOKMAKER_FALLBACKS["OE"] == "10bet"
    assert _bookmaker_name("BF", {}) == "Betfair Exchange"
    assert _bookmaker_name("OE", {}) == "10bet"
    assert frozenset({"BF"}) == _SHARP_ANCHOR_BOOK_CODES
    assert _sharp_anchor_codes({}) == frozenset({"BF"})


def test_all_odds_path_resolves_names_from_threaded_page_entities() -> None:
    """The all-odds API payloads carry NO ``bookmakers`` key (live-verified
    2026-08-02) — with the match page's entities threaded in, names resolve
    from the live feed, not the static fallback map."""
    payloads = [
        {
            "subeventId": 7001,
            "subeventName": "Hibernian vs Motherwell",
            "eventName": "Scottish Premiership Matches",
            "marketTypeName": "Win Market",
            "bets": [{"betId": 1, "betName": "Hibernian"}],
            "odds": [
                {"betId": 1, "bookmakerCode": "OE", "oddsDecimal": 2.10, "status": "ACTIVE"},
                {"betId": 1, "bookmakerCode": "BF", "oddsDecimal": 2.14, "status": "ACTIVE"},
            ],
        }
    ]
    snapshots = parse_market_api_payloads(
        payloads,
        url="https://www.oddschecker.com/football/scottish/hibernian-v-motherwell/winner",
        directory=EventDirectory(),
        now=datetime(2026, 8, 2, 10, 0, tzinfo=UTC),
        page_bookmaker_entities=_LIVE_BOOKMAKER_ENTITIES,
    )
    assert {(s.bookmaker, float(s.decimal_odds)) for s in snapshots} == {
        ("10bet", 2.10),
        ("Betfair Exchange", 2.14),
    }


def test_other_capture_anchor_gate_derives_from_page_entities() -> None:
    """With live entities threaded, the OTHER sharp-anchor gate keys on the
    derived exchange code (BF) — an OE (now 10bet) quote is no anchor."""

    def corners(code: str) -> list[dict[str, object]]:
        return [
            {
                "subeventId": 7001,
                "subeventName": "Hibernian vs Motherwell",
                "marketTypeName": "Total Corners",
                "bets": [{"betId": 1, "betName": "Over", "line": "9.5"}],
                "odds": [
                    {"betId": 1, "bookmakerCode": code, "oddsDecimal": 1.9, "status": "ACTIVE"}
                ],
            }
        ]

    url = "https://www.oddschecker.com/football/scottish/hibernian-v-motherwell/winner"
    anchored = parse_market_api_payloads(
        corners("BF"),
        url=url,
        directory=EventDirectory(),
        capture_other=True,
        page_bookmaker_entities=_LIVE_BOOKMAKER_ENTITIES,
    )
    assert {(s.market, s.bookmaker) for s in anchored} == {(Market.OTHER, "Betfair Exchange")}
    soft_only = parse_market_api_payloads(
        corners("OE"),
        url=url,
        directory=EventDirectory(),
        capture_other=True,
        page_bookmaker_entities=_LIVE_BOOKMAKER_ENTITIES,
    )
    assert soft_only == []


def test_bookmaker_entity_fallback_drift_detects_contradictions() -> None:
    from app.ingestion.oddschecker import bookmaker_entity_fallback_drift

    # The corrected fallback map agrees with the live entities: no drift.
    assert bookmaker_entity_fallback_drift(_LIVE_BOOKMAKER_ENTITIES) == ()
    assert bookmaker_entity_fallback_drift({}) == ()
    # A future re-recycle (OE claiming the exchange again) must be flagged.
    stale = {"OE": {"bookmakerName": "Betfair", "bookmakerType": "exchange"}}
    assert bookmaker_entity_fallback_drift(stale) == (("OE", "Betfair Exchange", "10bet"),)


def test_drift_alarm_warns_once_per_poll_cycle(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    loader = OddsCheckerLoader(EventDirectory())
    stale = {"OE": {"bookmakerName": "Betfair", "bookmakerType": "exchange"}}
    with caplog.at_level(logging.WARNING, logger="app.ingestion.oddschecker"):
        loader._warn_on_bookmaker_code_drift(stale)
        loader._warn_on_bookmaker_code_drift(stale)  # same cycle: no second warning
        loader._warn_on_bookmaker_code_drift({})  # clean entities never warn
    drift_logs = [r for r in caplog.records if "drift" in r.getMessage()]
    assert len(drift_logs) == 1
    message = drift_logs[0].getMessage()
    assert "OE" in message
    assert "http" not in message.lower()  # never URLs/secrets — name+code only
    # A new poll cycle re-arms the alarm (the gather loop resets the flag).
    loader._cycle_bookmaker_drift_warned = False
    with caplog.at_level(logging.WARNING, logger="app.ingestion.oddschecker"):
        loader._warn_on_bookmaker_code_drift(stale)
    assert len([r for r in caplog.records if "drift" in r.getMessage()]) == 2


@pytest.mark.asyncio
async def test_loader_threads_page_entities_into_all_odds_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through _parse_modern_or_legacy_match_page: the match page's
    bookmakers entities drive both name resolution and the OTHER anchor gate
    for all-odds payloads that carry no bookmakers key."""
    from app.ingestion import oddschecker as oc

    page_payload: dict[str, object] = {
        "repub": "OC",
        "bestOdds": {
            "bets": {
                "entities": {"1": {"ocBetId": 1, "betName": "Hibernian", "marketId": 10}},
                "ids": [1],
            },
            "odds": {"1": {"WH": {"oddsDecimal": 2.0}}},
            "markets": {
                "entities": {
                    "10": {"ocMarketId": 10, "marketTypeName": "Win Market"},
                    "50": {"ocMarketId": 50, "marketTypeName": "Total Corners"},
                },
                "ids": [10, 50],
            },
            "bookmakers": {
                "entities": _LIVE_BOOKMAKER_ENTITIES,
                "ids": list(_LIVE_BOOKMAKER_ENTITIES),
            },
        },
    }
    mapped_payload: dict[str, object] = {
        "marketId": 10,
        "subeventId": 7001,
        "subeventName": "Hibernian vs Motherwell",
        "eventName": "Scottish Premiership Matches",
        "marketTypeName": "Win Market",
        "bets": [{"betId": 1, "betName": "Hibernian"}],
        "odds": [
            {"betId": 1, "bookmakerCode": "OE", "oddsDecimal": 2.10, "status": "ACTIVE"},
            {"betId": 1, "bookmakerCode": "BF", "oddsDecimal": 2.14, "status": "ACTIVE"},
        ],
    }
    optional_payload: dict[str, object] = {
        "marketId": 50,
        "subeventId": 7001,
        "subeventName": "Hibernian vs Motherwell",
        "marketTypeName": "Total Corners",
        "bets": [{"betId": 2, "betName": "Over", "line": "9.5"}],
        "odds": [{"betId": 2, "bookmakerCode": "BF", "oddsDecimal": 1.9, "status": "ACTIVE"}],
    }

    async def fetch(market_ids: list[str], **kwargs: object) -> list[dict[str, object]]:
        del kwargs
        ids = tuple(str(market_id) for market_id in market_ids)
        return [mapped_payload] if ids == ("10",) else [optional_payload]

    monkeypatch.setattr(oc, "fetch_market_api_payloads", fetch)
    loader = OddsCheckerLoader(EventDirectory(), capture_other=True)
    page = OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/scottish/hibernian-v-motherwell/winner",
        html=_json_script(page_payload),
        status_code=200,
    )
    snapshots = await loader._parse_modern_or_legacy_match_page(
        page, now=datetime(2026, 8, 2, 10, 0, tzinfo=UTC), session=None
    )
    assert {(s.market, s.bookmaker) for s in snapshots} == {
        (Market.H2H, "10bet"),
        (Market.H2H, "Betfair Exchange"),
        (Market.OTHER, "Betfair Exchange"),
    }


# --------------------------------------------------------------------------- #
# Proxy-health-aware pool rotation (2026-08-02 challenge-storm incident)
# --------------------------------------------------------------------------- #


def _three_slot_pool_with_health(
    created: list[tuple[ScraperProxy | None, _PoolFakeSession]],
    health: object,
) -> object:
    from app.ingestion.oddschecker import _ProxySessionPool

    proxies = (
        ScraperProxy(url="http://p0", username="", password=""),
        ScraperProxy(url="http://p1", username="", password=""),
        ScraperProxy(url="http://p2", username="", password=""),
    )

    def factory(proxy: ScraperProxy | None) -> _PoolFakeSession:
        session = _PoolFakeSession([])
        created.append((proxy, session))
        return session

    return _ProxySessionPool(proxies, session_factory=factory, health=health)  # type: ignore[arg-type]


def test_pool_acquire_lease_skips_quarantined_index() -> None:
    from app.ingestion.proxy_health import ProxyHealthRegistry

    health = ProxyHealthRegistry(threshold=1)
    health.record_failure(0, "OddsCheckerChallenge")
    created: list[tuple[ScraperProxy | None, _PoolFakeSession]] = []
    pool = _three_slot_pool_with_health(created, health)

    lease = pool.acquire_lease()  # type: ignore[attr-defined]
    assert lease.index == 1


def test_pool_acquire_lease_fails_open_when_all_quarantined() -> None:
    from app.ingestion.proxy_health import ProxyHealthRegistry

    health = ProxyHealthRegistry(threshold=1)
    for index in range(3):
        health.record_failure(index, "OddsCheckerChallenge")
    created: list[tuple[ScraperProxy | None, _PoolFakeSession]] = []
    pool = _three_slot_pool_with_health(created, health)

    lease = pool.acquire_lease()  # type: ignore[attr-defined]
    assert lease.index == 0  # fail-open: full rotation, never zero availability


@pytest.mark.asyncio
async def test_challenge_retry_rotates_to_healthy_slot_and_records_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rotated-session retry must SKIP a quarantined slot (the 2026-08-02
    storm: retry landed on the deterministic next slot, which was also bad) and
    record the failed slot's failure + the succeeding slot's success."""
    from app.ingestion import oddschecker as oc
    from app.ingestion.proxy_health import ProxyHealthRegistry

    monkeypatch.setattr(oc, "_DISCOVERY_CHALLENGE_BACKOFF_RANGE", (0.0, 0.0))
    health = ProxyHealthRegistry(threshold=3)
    health.record_failure(1, "OddsCheckerChallenge")
    health.record_failure(1, "OddsCheckerChallenge")
    health.record_failure(1, "OddsCheckerChallenge")  # index 1 quarantined
    created: list[tuple[ScraperProxy | None, _PoolFakeSession]] = []
    pool = _three_slot_pool_with_health(created, health)

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, markets
            if session is created[0][1]:
                raise OddsCheckerChallenge("challenge")
            return ["snapshot"]

    loader = Loader(EventDirectory(), proxy_health=health)
    loader._session_pool = pool  # type: ignore[assignment]
    snapshots = await loader._gather_snapshots(["https://www.oddschecker.com/a/winner"], None)

    assert snapshots == ["snapshot"]
    # First lease slot 0 (challenged), retry must pick slot 2 — NOT quarantined slot 1.
    assert [proxy.url for proxy, _session in created if proxy is not None] == [
        "http://p0",
        "http://p2",
    ]
    assert health._slots[0].consecutive_failures == 1
    assert health._slots[2].consecutive_failures == 0
    assert health._slots[2].successes >= 1
    await pool.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_match_page_success_resets_proxy_failure_streak() -> None:
    from app.ingestion.proxy_health import ProxyHealthRegistry

    health = ProxyHealthRegistry(threshold=3)
    health.record_failure(0, "Timeout")
    created: list[tuple[ScraperProxy | None, _PoolFakeSession]] = []
    pool = _three_slot_pool_with_health(created, health)

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, session, markets
            return ["snapshot"]

    loader = Loader(EventDirectory(), proxy_health=health)
    loader._session_pool = pool  # type: ignore[assignment]
    await loader._gather_snapshots(["https://www.oddschecker.com/a/winner"], None)

    assert health._slots[0].consecutive_failures == 0
    assert health._slots[0].successes == 1
    await pool.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_all_slots_quarantined_never_crashes_and_degrades_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property: consecutive challenges on every slot never crash the cycle —
    it always yields a recorded incomplete verdict (fail-closed)."""
    from app.ingestion import oddschecker as oc
    from app.ingestion.proxy_health import ProxyHealthRegistry

    monkeypatch.setattr(oc, "_DISCOVERY_CHALLENGE_BACKOFF_RANGE", (0.0, 0.0))
    health = ProxyHealthRegistry(threshold=1)
    for index in range(3):
        health.record_failure(index, "OddsCheckerChallenge")
    created: list[tuple[ScraperProxy | None, _PoolFakeSession]] = []
    pool = _three_slot_pool_with_health(created, health)

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, session, markets
            raise OddsCheckerChallenge("challenge")

    loader = Loader(EventDirectory(), proxy_health=health)
    loader._session_pool = pool  # type: ignore[assignment]
    snapshots = await loader._gather_snapshots(
        ["https://www.oddschecker.com/a/winner"], None, pipeline_key="soccer"
    )

    assert snapshots == []
    assert loader.last_fetch_complete["soccer"] is False
    assert loader.last_fetch_incomplete_ratio["soccer"] == 1.0
    assert loader.last_fetch_challenge_failures["soccer"] == 1
    await pool.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_match_page_skip_warning_logs_path_slug_without_query(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failed pages must be identifiable in logs: path slug only, never query
    strings (page identity was unloggable during the 2026-08-02 storm)."""
    from app.ingestion import oddschecker as oc

    monkeypatch.setattr(oc, "_DISCOVERY_CHALLENGE_BACKOFF_RANGE", (0.0, 0.0))
    created: list[_PoolFakeSession] = []
    pool = _challenge_pool(created)

    class Loader(OddsCheckerLoader):
        async def fetch_match_odds(  # type: ignore[no-untyped-def]
            self, url, *, now=None, session=None, markets=None
        ):
            del url, now, session, markets
            raise OddsCheckerChallenge("challenge")

    loader = Loader(EventDirectory())
    loader._session_pool = pool
    with caplog.at_level("WARNING", logger="app.ingestion.oddschecker"):
        await loader._gather_snapshots(
            ["https://www.oddschecker.com/football/a-v-b/winner?tracker=secret"],
            None,
            pipeline_key="soccer",
        )

    skip_messages = [
        r.getMessage() for r in caplog.records if "match page skipped" in r.getMessage()
    ]
    assert skip_messages, "expected a skip warning"
    assert any("/football/a-v-b/winner" in message for message in skip_messages)
    assert all("?" not in message and "secret" not in message for message in skip_messages)


@pytest.mark.asyncio
async def test_run_with_session_builds_health_aware_pool() -> None:
    """The per-cycle production pool must carry the loader's health registry —
    otherwise rotation is quarantine-blind exactly like the 2026-08-02 outage."""
    from app.ingestion.proxy_health import ProxyHealthRegistry

    health = ProxyHealthRegistry(threshold=3)
    proxies = (ScraperProxy(url="http://p0", username="", password=""),)
    loader = OddsCheckerLoader(EventDirectory(), proxy_pool=proxies, proxy_health=health)
    seen: list[object] = []

    async def runner(session: object) -> list[object]:
        del session
        assert loader._session_pool is not None
        seen.append(loader._session_pool._health)
        return []

    await loader._run_with_session(runner)  # type: ignore[arg-type]
    assert seen == [health]
