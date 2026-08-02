"""BTTS + tennis exchange-close capture vocabulary (capture-coverage upgrades).

Empirical key forms — verified READ-ONLY against the live warehouse
(odds_snapshots / picks, 2026-07-11):

NOTE 2026-08-02: OddsChecker RECYCLED bookmaker codes — the Betfair Exchange is
now code ``BF`` (``OE`` became 10bet); fixtures below use ``BF``. The warehouse
facts referenced were captured under the pre-recycle code vocabulary.

- BTTS rows key as ``market='btts'``; the Betfair Exchange (then code ``OE``,
  now ``BF``) inline
  rows use bare selections ``'Yes'``/``'No'`` exclusively (3,272 rows since
  2026-07-03), while a legacy soft-book form ``'BTTS Yes'``/``'BTTS No'``
  (~66k rows each, last seen 2026-07-05) coexisted — splitting the two-outcome
  devig group into four selections and breaking the exact-selection close
  match (0 sharp snapshot closes across all btts picks).
- Tennis set-totals close groups key as ``totals_2_5`` with selections
  ``'Over 2.5'``/``'Under 2.5'`` (the canonical settlement/CLV vocabulary).
  Betfair Exchange prices NO tennis set-totals Over/Under on OddsChecker
  (0 OE rows on totals_2_5/3_5/4_5); it prices the same best-of-3 event only
  as the exact-sets market (``'Total Sets Exact'``, bets ``'2 Sets'``/``'3
  Sets'`` — 422 OE rows stranded under capture-only ``oc_total_sets_exact``).
  In a best-of-3, exactly-2 IS under-2.5 and exactly-3 IS over-2.5 — a rename,
  not a derivation — and the {2 Sets, 3 Sets} bet set itself proves best-of-3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.ingestion.base import EventDirectory
from app.ingestion.oddschecker import (
    parse_market_api_payloads,
    parse_match_page,
    supported_market_ids_from_match_page,
)
from app.schemas.base import Market

NOW = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
_TS = "2026-07-11T09:58:00Z"


def _active_odd(bet_id: int, code: str, odds: float) -> dict[str, Any]:
    return {
        "betId": bet_id,
        "bookmakerCode": code,
        "oddsDecimal": odds,
        "status": "ACTIVE",
        "betFeedTimestamp": _TS,
    }


def _api_market(
    market_type: str,
    bets: list[dict[str, Any]],
    odds: list[dict[str, Any]],
    *,
    market_id: int = 500,
    subevent: str = "Alpha Player vs Beta Player",
    event_name: str = "ATP Testville Matches",
) -> dict[str, Any]:
    return {
        "marketId": market_id,
        "subeventId": 7001,
        "subeventName": subevent,
        "subeventStartTime": "2026-07-12T13:00:00Z",
        "eventName": event_name,
        "marketTypeName": market_type,
        "bets": bets,
        "odds": odds,
    }


# --- Part 1: BTTS selection vocabulary ---------------------------------------


def test_btts_prefixed_selections_normalize_to_exchange_form_api_path() -> None:
    """'BTTS Yes'/'BTTS No' bets must land as the canonical 'Yes'/'No' the
    Betfair Exchange rows (and the close true-up groups) use."""
    payloads = [
        _api_market(
            "Both Teams To Score",
            bets=[
                {"betId": 1, "betName": "BTTS Yes", "line": None},
                {"betId": 2, "betName": "BTTS No", "line": None},
            ],
            odds=[
                _active_odd(1, "WH", 1.95),
                _active_odd(2, "WH", 1.85),
                _active_odd(1, "BF", 2.00),
                _active_odd(2, "BF", 1.99),
            ],
            subevent="Alpha FC vs Beta FC",
            event_name="English Premier League Matches",
        )
    ]
    snapshots = parse_market_api_payloads(
        payloads,
        url="https://www.oddschecker.com/football/english/alpha-fc-v-beta-fc/winner",
        directory=EventDirectory(),
        now=NOW,
    )
    assert {(s.market, s.selection, s.market_detail) for s in snapshots} == {
        (Market.BTTS, "Yes", "btts"),
        (Market.BTTS, "No", "btts"),
    }
    exchange = [s for s in snapshots if s.bookmaker == "Betfair Exchange"]
    assert {s.selection for s in exchange} == {"Yes", "No"}


def test_btts_bare_selections_pass_through_unchanged_api_path() -> None:
    payloads = [
        _api_market(
            "Both Teams To Score",
            bets=[
                {"betId": 1, "betName": "Yes", "line": None},
                {"betId": 2, "betName": "No", "line": None},
            ],
            odds=[_active_odd(1, "BF", 2.02), _active_odd(2, "BF", 1.97)],
            subevent="Alpha FC vs Beta FC",
        )
    ]
    snapshots = parse_market_api_payloads(
        payloads,
        url="https://www.oddschecker.com/football/english/alpha-fc-v-beta-fc/winner",
        directory=EventDirectory(),
        now=NOW,
    )
    assert {s.selection for s in snapshots} == {"Yes", "No"}


def _bestodds_html(
    market_type: str,
    bet_names: tuple[str, ...],
    *,
    home: str = "Alpha Player",
    away: str = "Beta Player",
) -> str:
    import json

    bets = {
        str(i + 1): {"ocBetId": i + 1, "betName": name, "marketId": 10, "line": None}
        for i, name in enumerate(bet_names)
    }
    odds = {
        str(i + 1): {
            "BF": {
                "bookmakerCode": "BF",
                "oddsDecimal": 1.9 + i * 0.2,
                "status": "ACTIVE",
                "expired": False,
                "notExpired": True,
                "betFeedTimestamp": _TS,
            }
        }
        for i in range(len(bet_names))
    }
    payload = {
        "repub": "OC",
        "lastUpdated": 1783246073819,
        "bestOdds": {
            "bets": {"entities": bets, "ids": list(range(1, len(bet_names) + 1))},
            "odds": odds,
            "markets": {
                "entities": {"10": {"ocMarketId": 10, "marketTypeName": market_type}},
                "ids": [10],
            },
            "bookmakers": {
                "entities": {"BF": {"bookmakerCode": "BF", "bookmakerName": "Betfair Exchange"}},
                "ids": ["BF"],
            },
            "subeventConfig": {
                "name": f"{home} vs {away}",
                "subeventId": "7001",
                "eventId": 42,
                "homeTeamName": home,
                "awayTeamName": away,
            },
        },
    }
    header = {
        "repub": "OC",
        "eventName": "Test Matches",
        "subeventName": f"{home} vs {away}",
        "subeventStartTime": "2026-07-12T13:00:00Z",
        "breadcrumbs": [],
    }
    return (
        "<html><body>"
        f'<script type="application/json"><!--{json.dumps(header)}--></script>'
        f'<script type="application/json"><!--{json.dumps(payload)}--></script>'
        "</body></html>"
    )


def test_btts_prefixed_selections_normalize_on_bestodds_path() -> None:
    html = _bestodds_html(
        "Both Teams To Score", ("BTTS Yes", "BTTS No"), home="Alpha FC", away="Beta FC"
    )
    snapshots = parse_match_page(
        html,
        url="https://www.oddschecker.com/football/english/alpha-fc-v-beta-fc/winner",
        directory=EventDirectory(),
        now=NOW,
    )
    assert {(s.market, s.selection) for s in snapshots} == {
        (Market.BTTS, "Yes"),
        (Market.BTTS, "No"),
    }


# --- Part 2: tennis exact-sets -> canonical set-totals ------------------------


def test_exact_sets_bo3_maps_to_canonical_set_totals_api_path() -> None:
    """A {2 Sets, 3 Sets} 'Total Sets Exact' market is a proven best-of-3 and
    maps bijectively onto the canonical totals_2_5 group the settlement/CLV
    side keys tennis set-total picks under."""
    payloads = [
        _api_market(
            "Total Sets Exact",
            bets=[
                {"betId": 1, "betName": "2 Sets", "line": None},
                {"betId": 2, "betName": "3 Sets", "line": None},
            ],
            odds=[
                _active_odd(1, "BF", 1.80),
                _active_odd(2, "BF", 2.26),
                _active_odd(1, "WH", 1.73),
                _active_odd(2, "WH", 2.10),
            ],
        )
    ]
    snapshots = parse_market_api_payloads(
        payloads,
        url="https://www.oddschecker.com/tennis/atp-testville/alpha-v-beta/winner",
        directory=EventDirectory(),
        now=NOW,
    )
    assert {(s.market, s.selection, s.market_detail) for s in snapshots} == {
        (Market.TOTALS, "Under 2.5", "totals_2_5"),
        (Market.TOTALS, "Over 2.5", "totals_2_5"),
    }
    exchange = {s.selection: s.decimal_odds for s in snapshots if s.bookmaker == "Betfair Exchange"}
    assert exchange == {"Under 2.5": 1.80, "Over 2.5": 2.26}


def test_exact_sets_bo5_market_is_never_mapped_to_totals() -> None:
    """{3,4,5} Sets has no per-selection Over/Under bijection — it must stay
    out of every totals devig group (fail-closed; capture-only OTHER at most)."""
    payloads = [
        _api_market(
            "Total Sets Exact",
            bets=[
                {"betId": 1, "betName": "3 Sets", "line": None},
                {"betId": 2, "betName": "4 Sets", "line": None},
                {"betId": 3, "betName": "5 Sets", "line": None},
            ],
            odds=[
                _active_odd(1, "BF", 2.5),
                _active_odd(2, "BF", 3.0),
                _active_odd(3, "BF", 3.5),
            ],
        )
    ]
    snapshots = parse_market_api_payloads(
        payloads,
        url="https://www.oddschecker.com/tennis/atp-testville/alpha-v-beta/winner",
        directory=EventDirectory(),
        now=NOW,
    )
    assert not [s for s in snapshots if s.market is Market.TOTALS]
    # With capture_other, the bo5 exact market keeps today's OTHER behavior.
    other = parse_market_api_payloads(
        payloads,
        url="https://www.oddschecker.com/tennis/atp-testville/alpha-v-beta/winner",
        directory=EventDirectory(),
        now=NOW,
        capture_other=True,
    )
    assert {s.market for s in other} == {Market.OTHER}
    assert {s.market_detail for s in other} == {"oc_total_sets_exact"}


def test_exact_sets_bo3_respects_markets_filter() -> None:
    payloads = [
        _api_market(
            "Total Sets Exact",
            bets=[
                {"betId": 1, "betName": "2 Sets", "line": None},
                {"betId": 2, "betName": "3 Sets", "line": None},
            ],
            odds=[_active_odd(1, "BF", 1.80), _active_odd(2, "BF", 2.26)],
        )
    ]
    snapshots = parse_market_api_payloads(
        payloads,
        url="https://www.oddschecker.com/tennis/atp-testville/alpha-v-beta/winner",
        directory=EventDirectory(),
        now=NOW,
        markets=(Market.H2H,),
    )
    assert snapshots == []


def test_exact_sets_bo3_maps_on_bestodds_path() -> None:
    html = _bestodds_html("Total Sets Exact", ("2 Sets", "3 Sets"))
    snapshots = parse_match_page(
        html,
        url="https://www.oddschecker.com/tennis/atp-testville/alpha-v-beta/winner",
        directory=EventDirectory(),
        now=NOW,
    )
    assert {(s.market, s.selection, s.market_detail) for s in snapshots} == {
        (Market.TOTALS, "Under 2.5", "totals_2_5"),
        (Market.TOTALS, "Over 2.5", "totals_2_5"),
    }


def test_exact_sets_bo5_not_mapped_on_bestodds_path() -> None:
    html = _bestodds_html("Total Sets Exact", ("3 Sets", "4 Sets", "5 Sets"))
    snapshots = parse_match_page(
        html,
        url="https://www.oddschecker.com/tennis/atp-testville/alpha-v-beta/winner",
        directory=EventDirectory(),
        now=NOW,
    )
    assert snapshots == []


def test_supported_market_ids_include_bo3_exact_sets_only() -> None:
    """The market-ids collector must request the bo3 exact-sets market from the
    all-odds API even without capture_other — it is now a MAPPED close source —
    while a bo5 exact market keeps needing the OTHER opt-in."""
    bo3 = _bestodds_html("Total Sets Exact", ("2 Sets", "3 Sets"))
    bo5 = _bestodds_html("Total Sets Exact", ("3 Sets", "4 Sets", "5 Sets"))
    assert supported_market_ids_from_match_page(bo3) == ["10"]
    assert supported_market_ids_from_match_page(bo3, markets=(Market.TOTALS,)) == ["10"]
    assert supported_market_ids_from_match_page(bo3, markets=(Market.H2H,)) == []
    assert supported_market_ids_from_match_page(bo5) == []
