"""Frozen replay corpus for the evidence-flow autoresearch run.

READ-ONLY / LOCKED asset for run tag `autoresearch/2026-07-07-evidence-flow`.
Part of the locked scorer `research/score.py`; must NOT be edited during a run.
Only the bookmaker-normalization asset group in `app/ingestion/oddschecker.py`
is editable.

Each fixture replays a raw OddsChecker payload through the PURE parse layer with
a fixed ``now`` (deterministic, no network). Objective ground truth is derived
from payload semantics.

Headroom is isolated to BOOKMAKER NORMALIZATION (asset group C). The all-odds
and legacy parse paths call ``_bookmaker_name(code, {})`` with EMPTY entities, so
any book whose 2-letter code is not in the 16-entry `_BOOKMAKER_FALLBACKS` map is
emitted as a RAW CODE (e.g. "SM") — a split identity vs bestOdds' canonical feed
name ("Smarkets") that corrupts dedup/CLV and fakes source-independence (SUB-6).
The additive-safe fix: thread the payload's ``bookmakers.entities`` map into the
all-odds path so off-map books resolve to the feed's canonical name.

Fresh-coverage recall uses a BOOKMAKER-AGNOSTIC 4-tuple
``(event_id, market, selection, market_detail)`` so the bookmaker fix cannot
inflate it; canonical/duplicate terms read the emitted bookmaker string
(raw code := len==2 and isupper()). Guard fixtures use in-map/canonical books.
"""

from __future__ import annotations

import json
from typing import Any

NOW = "2026-07-05T10:00:00Z"
_BASE = "https://www.oddschecker.com"
_TS = "2026-07-05T09:58:00Z"  # provider betFeedTimestamp (!= NOW -> not "unknown")


def _json_script(payload: dict[str, Any]) -> str:
    return f'<script type="application/json"><!--{json.dumps(payload)}--></script>'


def _bestodds_html(*, subevent_id: str, home: str, away: str, league: str) -> str:
    """Modern bestOdds page: H2H home priced by William Hill + a totals line by
    Betfair Exchange (OE, the sharp anchor). Canonical books via feed entities."""
    header = {
        "repub": "OC",
        "lastUpdated": 1783246057889,
        "eventName": f"{league} Matches",
        "subeventName": f"{home} vs {away}",
        "subeventStartTime": "2026-08-21T19:00:00Z",
        "breadcrumbs": [
            {"name": "Home", "url": "/", "type": "menu"},
            {"name": league, "url": f"football/{league.lower()}", "type": "card"},
            {
                "id": int(subevent_id),
                "name": f"{home} vs {away}",
                "url": f"football/{league.lower()}/x/winner",
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
                    "2": {"ocBetId": 2, "betName": "Over", "marketId": 30, "line": "2.5"},
                },
                "ids": [1, 2],
            },
            "odds": {
                "1": {
                    "WH": {
                        "bookmakerCode": "WH",
                        "oddsDecimal": 1.9,
                        "status": "ACTIVE",
                        "expired": False,
                        "notExpired": True,
                        "betFeedTimestamp": _TS,
                    }
                },
                "2": {
                    "OE": {
                        "bookmakerCode": "OE",
                        "oddsDecimal": 2.1,
                        "status": "ACTIVE",
                        "expired": False,
                        "notExpired": True,
                        "betFeedTimestamp": _TS,
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
                "eventId": 1,
                "homeTeamName": home,
                "awayTeamName": away,
            },
        },
    }
    return f"<html><body>{_json_script(header)}{_json_script(odds)}</body></html>"


def _api_payload(
    *,
    subevent_id: int,
    home: str,
    away: str,
    league: str,
    book_code: str,
    book_entity_name: str | None,
) -> list[dict[str, Any]]:
    """One all-odds Point-Spread market priced by William Hill (in-map) + a second
    book ``book_code``. When ``book_entity_name`` is given the payload carries a
    bookmakers.entities map naming it (the feed's canonical name) — the current
    path ignores it and emits the raw code; a correct path threads it."""
    entities: dict[str, Any] = {"WH": {"bookmakerCode": "WH", "bookmakerName": "William Hill"}}
    if book_entity_name is not None:
        entities[book_code] = {"bookmakerCode": book_code, "bookmakerName": book_entity_name}
    return [
        {
            "marketId": 100 + subevent_id,
            "subeventId": subevent_id,
            "subeventName": f"{home} vs {away}",
            "subeventStartTime": "2026-09-01T18:00:00Z",
            "eventName": f"{league} Matches",
            "marketTypeName": "Point Spread",
            "bookmakers": {"entities": entities, "ids": list(entities)},
            "bets": [
                {"betId": 1, "betName": home, "line": "-0.5"},
                {"betId": 2, "betName": away, "line": "+0.5"},
            ],
            "odds": [
                {
                    "betId": 1,
                    "bookmakerCode": "WH",
                    "oddsDecimal": 1.91,
                    "status": "ACTIVE",
                    "betFeedTimestamp": _TS,
                },
                {
                    "betId": 2,
                    "bookmakerCode": book_code,
                    "oddsDecimal": 1.95,
                    "status": "ACTIVE",
                    "betFeedTimestamp": _TS,
                },
            ],
        }
    ]


_LEGACY_HTML = """
<table class="eventTable" data-mid="551" data-mname="Point Spread"
  data-sname="Guinea at Tunisia" data-time="2026-07-05 13:00:00"
  data-ename="FIBA World Cup Qualification">
  <tbody>
    <tr class="diff-row evTabRow bc" data-bname="Guinea -3.5">
      <td class="sel nm">Guinea -3.5</td>
      <td data-bk="WH" data-odig="1.91" data-o="10/11" data-hcap="-3.5"></td>
    </tr>
    <tr class="diff-row evTabRow bc" data-bname="Tunisia +3.5">
      <td class="sel nm">Tunisia +3.5</td>
      <td data-bk="WH" data-odig="1.95" data-o="20/21" data-hcap="+3.5"></td>
    </tr>
  </tbody>
</table>
"""


def _c(event_id: str, market: str, selection: str, detail: str | None) -> dict[str, Any]:
    return {"event_id": event_id, "market": market, "selection": selection, "market_detail": detail}


def build_corpus() -> list[dict[str, Any]]:
    return [
        # ---- guards (baseline already correct -> components at max) ----------
        {
            "id": "guard_bestodds_canonical",
            "kind": "guard",
            "entrypoint": "bestodds",
            "url": f"{_BASE}/football/laliga/real-madrid-v-barcelona/winner",
            "input": _bestodds_html(
                subevent_id="700001", home="Real Madrid", away="Barcelona", league="LaLiga"
            ),
            "expect": {
                "events": [
                    {"event_id": "oddschecker:700001", "home": "Real Madrid", "away": "Barcelona"}
                ],
                "candidates": [
                    _c("oddschecker:700001", "h2h", "Real Madrid", "h2h"),
                    _c("oddschecker:700001", "totals", "Over 2.5", "totals_2_5"),
                ],
                "forbidden": [],
                "anchor_events": [{"event_id": "oddschecker:700001", "should_be_anchored": True}],
            },
        },
        {
            "id": "guard_api_inmap",
            "kind": "guard",
            "entrypoint": "api",
            "url": f"{_BASE}/football/eredivisie/ajax-v-psv/winner",
            "input": _api_payload(
                subevent_id=8001,
                home="Ajax",
                away="PSV",
                league="Eredivisie",
                book_code="B3",
                book_entity_name="bet365",
            ),
            "expect": {
                "events": [{"event_id": "oddschecker:8001", "home": "Ajax", "away": "PSV"}],
                "candidates": [
                    _c("oddschecker:8001", "spreads", "Ajax -0.5", "spreads_minus_0_5"),
                    _c("oddschecker:8001", "spreads", "PSV +0.5", "spreads_plus_0_5"),
                ],
                "forbidden": [],
                "anchor_events": [],
            },
        },
        {
            "id": "guard_legacy_inmap",
            "kind": "guard",
            "entrypoint": "legacy",
            "url": f"{_BASE}/basketball/fiba/guinea-at-tunisia/point-spread",
            "input": _LEGACY_HTML,
            "expect": {
                "events": [
                    {
                        "event_id": "oddschecker:basketball/fiba/guinea-at-tunisia",
                        "home": "Tunisia",
                        "away": "Guinea",
                    }
                ],
                "candidates": [
                    _c(
                        "oddschecker:basketball/fiba/guinea-at-tunisia",
                        "spreads",
                        "Guinea -3.5",
                        "spreads_minus_3_5",
                    ),
                    _c(
                        "oddschecker:basketball/fiba/guinea-at-tunisia",
                        "spreads",
                        "Tunisia +3.5",
                        "spreads_plus_3_5",
                    ),
                ],
                "forbidden": [],
                "anchor_events": [],
            },
        },
        # ---- headroom (asset C): off-map book -> raw code at baseline --------
        {
            "id": "headroom_api_offmap_smarkets",
            "kind": "headroom",
            "entrypoint": "api",
            "targets": ["SUB-6"],
            "url": f"{_BASE}/football/serie-a/inter-v-milan/winner",
            "input": _api_payload(
                subevent_id=7101,
                home="Inter",
                away="Milan",
                league="Serie A",
                book_code="SM",
                book_entity_name="Smarkets",
            ),
            "expect": {
                # Baseline emits bookmaker "SM" (raw code) for the away price because
                # the all-odds path passes {} entities; GT canonical = "Smarkets".
                "events": [{"event_id": "oddschecker:7101", "home": "Inter", "away": "Milan"}],
                "candidates": [
                    _c("oddschecker:7101", "spreads", "Inter -0.5", "spreads_minus_0_5"),
                    _c("oddschecker:7101", "spreads", "Milan +0.5", "spreads_plus_0_5"),
                ],
                "forbidden": [],
                "anchor_events": [],
                "canonical_book": {"selection": "Milan +0.5", "name": "Smarkets"},
            },
        },
        {
            "id": "headroom_api_offmap_spreadex",
            "kind": "headroom",
            "entrypoint": "api",
            "targets": ["SUB-6"],
            "url": f"{_BASE}/football/bundesliga/bayern-v-dortmund/winner",
            "input": _api_payload(
                subevent_id=7102,
                home="Bayern",
                away="Dortmund",
                league="Bundesliga",
                book_code="SX",
                book_entity_name="Spreadex",
            ),
            "expect": {
                "events": [{"event_id": "oddschecker:7102", "home": "Bayern", "away": "Dortmund"}],
                "candidates": [
                    _c("oddschecker:7102", "spreads", "Bayern -0.5", "spreads_minus_0_5"),
                    _c("oddschecker:7102", "spreads", "Dortmund +0.5", "spreads_plus_0_5"),
                ],
                "forbidden": [],
                "anchor_events": [],
                "canonical_book": {"selection": "Dortmund +0.5", "name": "Spreadex"},
            },
        },
    ]
