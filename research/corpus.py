"""Frozen replay corpus for the capture-freshness autoresearch run.

READ-ONLY / LOCKED asset. This file is part of the locked scorer
(`research/score.py`) for run tag `autoresearch/2026-07-07-capture-freshness`.
It must NOT be edited during a run — only `app/ingestion/oddschecker.py` is
editable. Changing the corpus changes the definition of "better", which the run
doctrine forbids mid-run.

Each fixture replays a raw OddsChecker payload through the *pure* parse layer of
`app/ingestion/oddschecker.py` with a fixed ``now`` (no network, deterministic)
and carries OBJECTIVE ground truth derived from the payload semantics — NOT from
what the current parser happens to emit. Two fixture classes:

* regression guards (``kind="guard"``): GT == the current correct output. A
  parser change that drops/relabels these loses score. Their GT snapshot tuples
  are pinned to the real parser output.
* headroom fixtures (``kind="headroom"``): GT == the objectively-correct output
  that the CURRENT parser fails to produce. Each targets one code-grounded
  suboptimality (SUB-1/3/5/7 from the 2026-07-07 parser audit):
    - SUB-1  api path keeps expired/notExpired odds (stale leak, primary path)
    - SUB-3  sharp-anchor gate accepts a DEAD (suspended) Betfair OE quote
    - SUB-5  api team-split ignores structured home/away -> empty/unmatchable
    - SUB-7  _find_match_payload picks byte-largest bestOdds blob, not the URL's
             subevent -> WRONG-GAME registration

Ground-truth key for a snapshot is the 5-tuple
``(event_id, market_value, selection, market_detail, bookmaker)`` compared as a
SET (order-independent; the parser makes no list-order contract).
"""

from __future__ import annotations

import json
from typing import Any

NOW = "2026-07-05T10:00:00Z"
_BASE = "https://www.oddschecker.com"


def _json_script(payload: dict[str, Any]) -> str:
    return f'<script type="application/json"><!--{json.dumps(payload)}--></script>'


# --------------------------------------------------------------------------- #
# bestOdds (parse_match_page) inputs                                          #
# --------------------------------------------------------------------------- #
def _bestodds_match_html(
    *, subevent_id: str, home: str, away: str, league_slug: str, league_name: str
) -> str:
    """A minimal single-match Hypernova bestOdds page: H2H (home/draw) + a
    totals line, one active price per selection. Mirrors the shape exercised by
    tests/test_oddschecker.py::test_parse_match_page_emits_snapshots."""
    header = {
        "repub": "OC",
        "lastUpdated": 1783246057889,
        "eventName": f"{league_name} Matches",
        "subeventName": f"{home} vs {away}",
        "subeventStartTime": "2026-08-21T19:00:00Z",
        "breadcrumbs": [
            {"name": "Home", "url": "/", "type": "menu"},
            {"name": league_name, "url": f"football/{league_slug}", "type": "card"},
            {
                "id": int(subevent_id),
                "name": f"{home} vs {away}",
                "url": f"football/{league_slug}/{home.lower()}-v-{away.lower()}/winner",
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
                    "1": {"ocBetId": 1, "betName": home, "marketId": 10, "line": None},
                    "2": {"ocBetId": 2, "betName": "Draw", "marketId": 10, "line": None},
                    "3": {"ocBetId": 3, "betName": "Over", "marketId": 30, "line": "2.5"},
                },
                "ids": [1, 2, 3],
            },
            "odds": {
                "1": {
                    "WH": {
                        "bookmakerCode": "WH",
                        "oddsDecimal": 1.9,
                        "status": "ACTIVE",
                        "expired": False,
                        "notExpired": True,
                    }
                },
                "2": {
                    "OE": {
                        "bookmakerCode": "OE",
                        "oddsDecimal": 3.6,
                        "status": "ACTIVE",
                        "expired": False,
                        "notExpired": True,
                    }
                },
                "3": {
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
                    "30": {"ocMarketId": 30, "marketTypeName": "Asian Total"},
                },
                "ids": [10, 30],
            },
            "bookmakers": {
                "entities": {
                    "WH": {"bookmakerCode": "WH", "bookmakerName": "William Hill"},
                    "OE": {"bookmakerCode": "OE", "bookmakerName": "Betfair Exchange"},
                },
                "ids": ["WH", "OE"],
            },
            "subeventConfig": {
                "name": f"{home} vs {away}",
                "subeventId": subevent_id,
                "eventId": 2457,
                "homeTeamName": home,
                "awayTeamName": away,
            },
        },
    }
    return f"<html><body>{_json_script(header)}{_json_script(odds)}</body></html>"


def _multiblob_wronggame_html(
    *,
    url_id: str = "555",
    url_home: str = "Real Madrid",
    url_away: str = "Barcelona",
    url_slug: str = "real-madrid-v-barcelona",
    other_id: str = "999",
    other_home: str = "Getafe",
    other_away: str = "Alaves",
) -> str:
    """SUB-7: one header for the URL's game + TWO bestOdds blobs — the URL's game
    (small) and an unrelated accumulator game (LARGER json). The current
    _find_match_payload picks max(len(json.dumps(bestOdds))) -> the larger blob,
    registering and pricing the WRONG game. A correct parser selects the blob
    whose subevent id matches the header/URL."""
    header = {
        "repub": "OC",
        "lastUpdated": 1783246057889,
        "eventName": "La Liga Matches",
        "subeventName": f"{url_home} vs {url_away}",
        "subeventStartTime": "2026-08-21T19:00:00Z",
        "breadcrumbs": [
            {"name": "Home", "url": "/", "type": "menu"},
            {"name": "La Liga", "url": "football/spain/la-liga", "type": "card"},
            {
                "id": int(url_id),
                "name": f"{url_home} vs {url_away}",
                "url": f"football/spain/la-liga/{url_slug}/winner",
                "type": "subevent",
            },
        ],
    }

    def _blob(subevent_id: str, home: str, away: str, extra_bets: int) -> dict[str, Any]:
        bets_entities: dict[str, Any] = {
            "1": {"ocBetId": 1, "betName": home, "marketId": 10, "line": None},
            "2": {"ocBetId": 2, "betName": "Draw", "marketId": 10, "line": None},
        }
        odds: dict[str, Any] = {
            "1": {
                "WH": {
                    "bookmakerCode": "WH",
                    "oddsDecimal": 2.0,
                    "status": "ACTIVE",
                    "expired": False,
                    "notExpired": True,
                }
            },
            "2": {
                "WH": {
                    "bookmakerCode": "WH",
                    "oddsDecimal": 3.3,
                    "status": "ACTIVE",
                    "expired": False,
                    "notExpired": True,
                }
            },
        }
        # Padding bets to make blob 999 serialize LARGER than blob 555 (drives the
        # current byte-size selection to the wrong game).
        for i in range(extra_bets):
            bid = 100 + i
            bets_entities[str(bid)] = {
                "ocBetId": bid,
                "betName": f"Padding Selection {i}",
                "marketId": 10,
                "line": None,
            }
            odds[str(bid)] = {
                "WH": {
                    "bookmakerCode": "WH",
                    "oddsDecimal": 5.0 + i,
                    "status": "ACTIVE",
                    "expired": False,
                    "notExpired": True,
                }
            }
        return {
            "repub": "OC",
            "lastUpdated": 1783246073819,
            "bestOdds": {
                "bets": {"entities": bets_entities, "ids": list(map(int, bets_entities))},
                "odds": odds,
                "markets": {
                    "entities": {"10": {"ocMarketId": 10, "marketTypeName": "Win Market"}},
                    "ids": [10],
                },
                "bookmakers": {
                    "entities": {"WH": {"bookmakerCode": "WH", "bookmakerName": "William Hill"}},
                    "ids": ["WH"],
                },
                "subeventConfig": {
                    "name": f"{home} vs {away}",
                    "subeventId": subevent_id,
                    "eventId": 1,
                    "homeTeamName": home,
                    "awayTeamName": away,
                    "url": f"football/spain/la-liga/{home.lower().replace(' ', '-')}"
                    f"-v-{away.lower().replace(' ', '-')}/winner",
                },
            },
        }

    url_game = _blob(url_id, url_home, url_away, extra_bets=0)  # small
    other_game = _blob(other_id, other_home, other_away, extra_bets=12)  # LARGER json
    return (
        "<html><body>"
        + _json_script(header)
        + _json_script(url_game)
        + _json_script(other_game)
        + "</body></html>"
    )


# --------------------------------------------------------------------------- #
# all-odds API (parse_market_api_payloads) inputs                            #
# --------------------------------------------------------------------------- #
def _api_expired_leak_payloads() -> list[dict[str, Any]]:
    """SUB-1: a Total market whose second book (bet365) is status=ACTIVE but
    expired=true/notExpired=false. The bestOdds path drops such an odd; the
    all-odds path (primary) keeps it. GT: the expired price is FORBIDDEN."""
    return [
        {
            "marketId": 200,
            "subeventId": 8001,
            "subeventName": "Ajax vs PSV",
            "subeventStartTime": "2026-08-01T18:00:00Z",
            "eventName": "Eredivisie Matches",
            "marketTypeName": "Point Spread",
            "bets": [
                {"betId": 1, "betName": "Ajax", "line": "-0.5"},
                {"betId": 2, "betName": "PSV", "line": "+0.5"},
            ],
            "odds": [
                {
                    "betId": 1,
                    "bookmakerCode": "WH",
                    "oddsDecimal": 1.90,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:59:00Z",
                },
                {
                    "betId": 2,
                    "bookmakerCode": "WH",
                    "oddsDecimal": 1.95,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:59:00Z",
                },
            ],
        },
        {
            "marketId": 201,
            "subeventId": 8001,
            "subeventName": "Ajax vs PSV",
            "subeventStartTime": "2026-08-01T18:00:00Z",
            "eventName": "Eredivisie Matches",
            "marketTypeName": "Total Points",
            "bets": [{"betId": 3, "betName": "Over", "line": "2.5"}],
            "odds": [
                {
                    "betId": 3,
                    "bookmakerCode": "WH",
                    "oddsDecimal": 1.83,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:59:00Z",
                },
                {
                    "betId": 3,
                    "bookmakerCode": "B3",
                    "oddsDecimal": 1.80,
                    "status": "ACTIVE",
                    "expired": True,
                    "notExpired": False,
                    "betFeedTimestamp": "2026-07-05T09:40:00Z",
                },
            ],
        },
    ]


def _api_anchor_payloads(subevent_id: int, home: str, away: str, oe_status: str) -> list[dict]:
    """SUB-3: an unmapped market (Total Corners) priced by Betfair OE + a soft
    book. When OE is SUSPENDED the market has NO live sharp anchor -> it must NOT
    be captured under Market.OTHER. When OE is ACTIVE it is a true anchor and
    SHOULD be captured. capture_other mirrors production (settings default True)."""
    return [
        {
            "marketId": 300 + subevent_id,
            "subeventId": subevent_id,
            "subeventName": f"{home} vs {away}",
            "subeventStartTime": "2026-08-02T18:00:00Z",
            "eventName": "Serie A Matches",
            "marketTypeName": "Total Corners",
            "bets": [{"betId": 1, "betName": "Over", "line": "9.5"}],
            "odds": [
                {
                    "betId": 1,
                    "bookmakerCode": "OE",
                    "oddsDecimal": 1.90,
                    "status": oe_status,
                    "betFeedTimestamp": "2026-07-05T09:58:00Z",
                },
                {
                    "betId": 1,
                    "bookmakerCode": "WH",
                    "oddsDecimal": 1.85,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:58:00Z",
                },
            ],
        }
    ]


def _api_structured_teams_payloads() -> list[dict[str, Any]]:
    """SUB-5: subeventName uses '@' (which _split_match_name does not recognise)
    so the current API path registers EMPTY team names -> unmatchable. The
    payload also carries structured homeTeamName/awayTeamName; reading them when
    the split fails is an additive-safe fix (absent in prod -> unchanged). GT
    event: home=Boston Celtics, away=Toronto Raptors (NBA 'away @ home')."""
    return [
        {
            "marketId": 400,
            "subeventId": 9500,
            "subeventName": "Toronto Raptors @ Boston Celtics",
            "homeTeamName": "Boston Celtics",
            "awayTeamName": "Toronto Raptors",
            "subeventStartTime": "2026-08-03T23:00:00Z",
            "eventName": "NBA Matches",
            "marketTypeName": "Point Spread",
            "bets": [
                {"betId": 1, "betName": "Toronto Raptors", "line": "-3.5"},
                {"betId": 2, "betName": "Boston Celtics", "line": "+3.5"},
            ],
            "odds": [
                {
                    "betId": 1,
                    "bookmakerCode": "WH",
                    "oddsDecimal": 1.91,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:57:00Z",
                },
                {
                    "betId": 2,
                    "bookmakerCode": "WH",
                    "oddsDecimal": 1.91,
                    "status": "ACTIVE",
                    "betFeedTimestamp": "2026-07-05T09:57:00Z",
                },
            ],
        }
    ]


def _api_clean_payloads() -> list[dict[str, Any]]:
    """Regression guard: a clean all-odds payload, all ACTIVE, ' at ' name.
    Mirrors tests/test_oddschecker.py::test_parse_market_api_payloads."""
    return [
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
                },
            ],
        },
    ]


# --------------------------------------------------------------------------- #
# legacy grid (parse_legacy_match_page) input                                #
# --------------------------------------------------------------------------- #
_LEGACY_GRID_HTML = """
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


def build_corpus() -> list[dict[str, Any]]:
    """Return the frozen fixture list. GT snapshot tuples for ``kind='guard'``
    fixtures are pinned to real parser output; ``kind='headroom'`` GT is the
    objectively-correct target the current parser misses."""
    return [
        # ---- regression guards (GT == current correct output) --------------
        {
            "id": "guard_bestodds_h2h_totals",
            "kind": "guard",
            "entrypoint": "bestodds",
            "targets": [],
            "url": f"{_BASE}/football/spain/la-liga/real-madrid-v-barcelona/winner",
            "input": _bestodds_match_html(
                subevent_id="700001",
                home="Real Madrid",
                away="Barcelona",
                league_slug="spain/la-liga",
                league_name="La Liga",
            ),
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:700001",
                        "home": "Real Madrid",
                        "away": "Barcelona",
                        "league": "La Liga",
                    }
                ],
                "candidates": [
                    {
                        "event_id": "oddschecker:700001",
                        "market": "h2h",
                        "selection": "Real Madrid",
                        "market_detail": "h2h",
                        "bookmaker": "William Hill",
                    },
                    {
                        "event_id": "oddschecker:700001",
                        "market": "h2h",
                        "selection": "Draw",
                        "market_detail": "h2h",
                        "bookmaker": "Betfair Exchange",
                    },
                    {
                        "event_id": "oddschecker:700001",
                        "market": "totals",
                        "selection": "Over 2.5",
                        "market_detail": "totals_2_5",
                        "bookmaker": "William Hill",
                    },
                ],
                "forbidden": [],
                "anchor_events": [],
            },
        },
        {
            "id": "guard_api_clean_nfl",
            "kind": "guard",
            "entrypoint": "api",
            "targets": [],
            "url": f"{_BASE}/american-football/nfl/carolina-panthers-at-arizona-cardinals/winner",
            "input": _api_clean_payloads(),
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:9001",
                        "home": "Arizona Cardinals",
                        "away": "Carolina Panthers",
                        "league": "NFL",
                    }
                ],
                "candidates": [
                    {
                        "event_id": "oddschecker:9001",
                        "market": "spreads",
                        "selection": "Carolina Panthers -1.5",
                        "market_detail": "spreads_minus_1_5",
                        "bookmaker": "William Hill",
                    },
                    {
                        "event_id": "oddschecker:9001",
                        "market": "spreads",
                        "selection": "Arizona Cardinals +1.5",
                        "market_detail": "spreads_plus_1_5",
                        "bookmaker": "bet365",
                    },
                    {
                        "event_id": "oddschecker:9001",
                        "market": "totals",
                        "selection": "Over 41.5",
                        "market_detail": "totals_41_5",
                        "bookmaker": "William Hill",
                    },
                ],
                "forbidden": [],
                "anchor_events": [],
            },
        },
        {
            "id": "guard_legacy_grid",
            "kind": "guard",
            "entrypoint": "legacy",
            "targets": [],
            "url": f"{_BASE}/basketball/fiba-world-cup-qualification/guinea-at-tunisia/point-spread",  # noqa: E501
            "input": _LEGACY_GRID_HTML,
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:basketball/fiba-world-cup-qualification/"
                        "guinea-at-tunisia",
                        "home": "Tunisia",
                        "away": "Guinea",
                        "league": "FIBA World Cup Qualification",
                    }
                ],
                "candidates": [
                    {
                        "event_id": "oddschecker:basketball/fiba-world-cup-qualification/"
                        "guinea-at-tunisia",
                        "market": "spreads",
                        "selection": "Guinea -3.5",
                        "market_detail": "spreads_minus_3_5",
                        "bookmaker": "William Hill",
                    },
                    {
                        "event_id": "oddschecker:basketball/fiba-world-cup-qualification/"
                        "guinea-at-tunisia",
                        "market": "spreads",
                        "selection": "Tunisia +3.5",
                        "market_detail": "spreads_plus_3_5",
                        "bookmaker": "William Hill",
                    },
                ],
                "forbidden": [],
                "anchor_events": [],
            },
        },
        {
            "id": "guard_api_live_anchor",
            "kind": "guard",
            "entrypoint": "api",
            "targets": [],
            "url": f"{_BASE}/football/italy/serie-a/inter-v-milan/winner",
            "input": _api_anchor_payloads(7101, "Inter", "Milan", oe_status="ACTIVE"),
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:7101",
                        "home": "Inter",
                        "away": "Milan",
                        "league": "Serie A",
                    }
                ],
                # A LIVE OE anchor -> the unmapped corners market IS captured as OTHER
                # for both the anchor and the soft book. GT pinned to real output.
                "candidates": [
                    {
                        "event_id": "oddschecker:7101",
                        "market": "other",
                        "selection": "Over",
                        "market_detail": "oc_total_corners_9_5",
                        "bookmaker": "Betfair Exchange",
                    },
                    {
                        "event_id": "oddschecker:7101",
                        "market": "other",
                        "selection": "Over",
                        "market_detail": "oc_total_corners_9_5",
                        "bookmaker": "William Hill",
                    },
                ],
                "forbidden": [],
                "anchor_events": [{"event_id": "oddschecker:7101", "should_be_anchored": True}],
            },
        },
        # ---- headroom fixtures (GT == objectively-correct target) -----------
        {
            "id": "headroom_sub1_api_expired_leak",
            "kind": "headroom",
            "entrypoint": "api",
            "targets": ["SUB-1"],
            "url": f"{_BASE}/football/netherlands/eredivisie/ajax-v-psv/winner",
            "input": _api_expired_leak_payloads(),
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:8001",
                        "home": "Ajax",
                        "away": "PSV",
                        "league": "Eredivisie",
                    }
                ],
                "candidates": [
                    {
                        "event_id": "oddschecker:8001",
                        "market": "spreads",
                        "selection": "Ajax -0.5",
                        "market_detail": "spreads_minus_0_5",
                        "bookmaker": "William Hill",
                    },
                    {
                        "event_id": "oddschecker:8001",
                        "market": "spreads",
                        "selection": "PSV +0.5",
                        "market_detail": "spreads_plus_0_5",
                        "bookmaker": "William Hill",
                    },
                    {
                        "event_id": "oddschecker:8001",
                        "market": "totals",
                        "selection": "Over 2.5",
                        "market_detail": "totals_2_5",
                        "bookmaker": "William Hill",
                    },
                ],
                # bet365's expired price on Over 2.5 MUST NOT be emitted (stale leak).
                "forbidden": [
                    {
                        "event_id": "oddschecker:8001",
                        "market": "totals",
                        "selection": "Over 2.5",
                        "market_detail": "totals_2_5",
                        "bookmaker": "bet365",
                    },
                ],
                "anchor_events": [],
            },
        },
        {
            "id": "headroom_sub3_api_dead_anchor",
            "kind": "headroom",
            "entrypoint": "api",
            "targets": ["SUB-3"],
            "url": f"{_BASE}/football/italy/serie-a/roma-v-lazio/winner",
            "input": _api_anchor_payloads(7102, "Roma", "Lazio", oe_status="SUSPENDED"),
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:7102",
                        "home": "Roma",
                        "away": "Lazio",
                        "league": "Serie A",
                    }
                ],
                # OE is SUSPENDED -> no live sharp anchor -> the corners market must
                # NOT be captured. No candidates; the anchor decision must be False.
                "candidates": [],
                "forbidden": [],
                "anchor_events": [{"event_id": "oddschecker:7102", "should_be_anchored": False}],
            },
        },
        {
            "id": "headroom_sub5_api_structured_teams",
            "kind": "headroom",
            "entrypoint": "api",
            "targets": ["SUB-5"],
            "url": f"{_BASE}/basketball/nba/toronto-raptors-at-boston-celtics/winner",
            "input": _api_structured_teams_payloads(),
            "expect": {
                # Baseline registers EMPTY teams (unmatchable). GT: correct home/away
                # from the structured fields.
                "events": [
                    {
                        "event_id": "oddschecker:9500",
                        "home": "Boston Celtics",
                        "away": "Toronto Raptors",
                        "league": "NBA",
                    }
                ],
                "candidates": [
                    {
                        "event_id": "oddschecker:9500",
                        "market": "spreads",
                        "selection": "Toronto Raptors -3.5",
                        "market_detail": "spreads_minus_3_5",
                        "bookmaker": "William Hill",
                    },
                    {
                        "event_id": "oddschecker:9500",
                        "market": "spreads",
                        "selection": "Boston Celtics +3.5",
                        "market_detail": "spreads_plus_3_5",
                        "bookmaker": "William Hill",
                    },
                ],
                "forbidden": [],
                "anchor_events": [],
            },
        },
        {
            "id": "headroom_sub7_multiblob_wronggame",
            "kind": "headroom",
            "entrypoint": "bestodds",
            "targets": ["SUB-7"],
            "url": f"{_BASE}/football/spain/la-liga/real-madrid-v-barcelona/winner",
            "input": _multiblob_wronggame_html(),
            "expect": {
                # The URL is Real Madrid vs Barcelona (subevent 555). Baseline picks
                # the byte-largest blob (Getafe/Alaves, 999) -> WRONG game. GT: 555.
                "events": [
                    {
                        "event_id": "oddschecker:555",
                        "home": "Real Madrid",
                        "away": "Barcelona",
                        "league": "La Liga",
                    }
                ],
                "candidates": [
                    {
                        "event_id": "oddschecker:555",
                        "market": "h2h",
                        "selection": "Real Madrid",
                        "market_detail": "h2h",
                        "bookmaker": "William Hill",
                    },
                    {
                        "event_id": "oddschecker:555",
                        "market": "h2h",
                        "selection": "Draw",
                        "market_detail": "h2h",
                        "bookmaker": "William Hill",
                    },
                ],
                "forbidden": [],
                "anchor_events": [],
                # Only the URL's game may legitimately be registered/priced. Any
                # snapshot for another event id (e.g. 999) is a wrong-game leak.
                "allowed_event_ids": ["oddschecker:555"],
            },
        },
        # ---- headroom variants (different literals -> defeat single-literal
        #      hardcoding: a value-keyed hack fixes one instance, never reaches max) --
        {
            "id": "headroom_sub1_variant_expired_leak",
            "kind": "headroom",
            "entrypoint": "api",
            "targets": ["SUB-1"],
            "url": f"{_BASE}/football/portugal/primeira-liga/porto-v-benfica/winner",
            "input": [
                {
                    "marketId": 210,
                    "subeventId": 8002,
                    "subeventName": "Porto vs Benfica",
                    "subeventStartTime": "2026-08-04T18:00:00Z",
                    "eventName": "Primeira Liga Matches",
                    "marketTypeName": "Total Points",
                    "bets": [{"betId": 1, "betName": "Over", "line": "1.5"}],
                    "odds": [
                        {
                            "betId": 1,
                            "bookmakerCode": "WH",
                            "oddsDecimal": 1.44,
                            "status": "ACTIVE",
                            "betFeedTimestamp": "2026-07-05T09:59:00Z",
                        },
                        {
                            "betId": 1,
                            "bookmakerCode": "B3",
                            "oddsDecimal": 1.42,
                            "status": "ACTIVE",
                            "expired": True,
                            "notExpired": False,
                            "betFeedTimestamp": "2026-07-05T09:30:00Z",
                        },
                    ],
                }
            ],
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:8002",
                        "home": "Porto",
                        "away": "Benfica",
                        "league": "Primeira Liga",
                    }
                ],
                "candidates": [
                    {
                        "event_id": "oddschecker:8002",
                        "market": "totals",
                        "selection": "Over 1.5",
                        "market_detail": "totals_1_5",
                        "bookmaker": "William Hill",
                    },
                ],
                "forbidden": [
                    {
                        "event_id": "oddschecker:8002",
                        "market": "totals",
                        "selection": "Over 1.5",
                        "market_detail": "totals_1_5",
                        "bookmaker": "bet365",
                    },
                ],
                "anchor_events": [],
            },
        },
        {
            "id": "headroom_sub3_variant_dead_anchor",
            "kind": "headroom",
            "entrypoint": "api",
            "targets": ["SUB-3"],
            "url": f"{_BASE}/football/germany/bundesliga/bayern-v-dortmund/winner",
            "input": _api_anchor_payloads(7103, "Bayern", "Dortmund", oe_status="SUSPENDED"),
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:7103",
                        "home": "Bayern",
                        "away": "Dortmund",
                        "league": "Serie A",
                    }
                ],
                "candidates": [],
                "forbidden": [],
                "anchor_events": [{"event_id": "oddschecker:7103", "should_be_anchored": False}],
            },
        },
        {
            "id": "headroom_sub5_variant_structured_teams",
            "kind": "headroom",
            "entrypoint": "api",
            "targets": ["SUB-5"],
            "url": f"{_BASE}/basketball/nba/los-angeles-lakers-at-golden-state-warriors/winner",
            "input": [
                {
                    "marketId": 410,
                    "subeventId": 9501,
                    "subeventName": "Los Angeles Lakers @ Golden State Warriors",
                    "homeTeamName": "Golden State Warriors",
                    "awayTeamName": "Los Angeles Lakers",
                    "subeventStartTime": "2026-08-05T23:00:00Z",
                    "eventName": "NBA Matches",
                    "marketTypeName": "Point Spread",
                    "bets": [
                        {"betId": 1, "betName": "Los Angeles Lakers", "line": "-4.5"},
                        {"betId": 2, "betName": "Golden State Warriors", "line": "+4.5"},
                    ],
                    "odds": [
                        {
                            "betId": 1,
                            "bookmakerCode": "WH",
                            "oddsDecimal": 1.91,
                            "status": "ACTIVE",
                            "betFeedTimestamp": "2026-07-05T09:57:00Z",
                        },
                        {
                            "betId": 2,
                            "bookmakerCode": "WH",
                            "oddsDecimal": 1.91,
                            "status": "ACTIVE",
                            "betFeedTimestamp": "2026-07-05T09:57:00Z",
                        },
                    ],
                }
            ],
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:9501",
                        "home": "Golden State Warriors",
                        "away": "Los Angeles Lakers",
                        "league": "NBA",
                    }
                ],
                "candidates": [
                    {
                        "event_id": "oddschecker:9501",
                        "market": "spreads",
                        "selection": "Los Angeles Lakers -4.5",
                        "market_detail": "spreads_minus_4_5",
                        "bookmaker": "William Hill",
                    },
                    {
                        "event_id": "oddschecker:9501",
                        "market": "spreads",
                        "selection": "Golden State Warriors +4.5",
                        "market_detail": "spreads_plus_4_5",
                        "bookmaker": "William Hill",
                    },
                ],
                "forbidden": [],
                "anchor_events": [],
            },
        },
        {
            "id": "headroom_sub7_variant_multiblob",
            "kind": "headroom",
            "entrypoint": "bestodds",
            "targets": ["SUB-7"],
            "url": f"{_BASE}/football/italy/serie-a/juventus-v-napoli/winner",
            "input": _multiblob_wronggame_html(
                url_id="556",
                url_home="Juventus",
                url_away="Napoli",
                url_slug="juventus-v-napoli",
                other_id="998",
                other_home="Torino",
                other_away="Genoa",
            ),
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:556",
                        "home": "Juventus",
                        "away": "Napoli",
                        "league": "La Liga",
                    }
                ],
                "candidates": [
                    {
                        "event_id": "oddschecker:556",
                        "market": "h2h",
                        "selection": "Juventus",
                        "market_detail": "h2h",
                        "bookmaker": "William Hill",
                    },
                    {
                        "event_id": "oddschecker:556",
                        "market": "h2h",
                        "selection": "Draw",
                        "market_detail": "h2h",
                        "bookmaker": "William Hill",
                    },
                ],
                "forbidden": [],
                "anchor_events": [],
                "allowed_event_ids": ["oddschecker:556"],
            },
        },
        # ---- robustness guard ----------------------------------------------
        {
            "id": "guard_garbage_no_payload",
            "kind": "guard",
            "entrypoint": "bestodds",
            "targets": [],
            "url": f"{_BASE}/football/x/y/winner",
            "input": "<html><body><p>no hypernova payload here</p></body></html>",
            "expect": {
                "events": [],
                "candidates": [],
                "forbidden": [],
                "anchor_events": [],
                # Raising OddsCheckerParseError on a payload-less page is the
                # parser's defined contract (the loader catches it). An exception
                # here is acceptable; an UNEXPECTED crash on other fixtures is not.
                "may_raise": True,
            },
        },
    ]
