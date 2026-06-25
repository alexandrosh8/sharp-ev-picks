"""Metadata-driven basketball/tennis market binding for the JSON feed.

The headline money line (basketball ``home_away``, tennis ``match_winner``) and
basketball points totals (``over_under_games_<line>``) are NOT statically
keyed like the four soccer markets: their feed betType/scope come from the
event's OWN bootstrap (``defaultBetId`` / ``defaultScopeId``) — live-verified
2026-06-25 across pre-match + finished basketball/tennis events:

    basketball: defaultBetId=3, defaultScopeId=1  (E-3-1 = Home/Away incl. OT)
    tennis:     defaultBetId=3, defaultScopeId=2  (E-3-2 = match winner)
    over/under: betType 2 at defaultScopeId, line in the 5th feed segment

This module pins that binding. Pure parse contract — synthetic payloads, no
network (project rule: no network in tests).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal_bookmakers import static_bookmaker_map
from app.ingestion.oddsportal_json import (
    build_feed_url,
    extract_bootstrap_tokens,
    parse_feed_payload,
)
from app.schemas.base import Market

REGISTRY = static_bookmaker_map()
BOOKIE = "707"  # -> BetMGM in the static map
BOOK = REGISTRY[BOOKIE]


def _payload(feed_key: str, outcomes: object) -> dict:
    """Minimal decrypted-feed shape: d.oddsdata.back[feed_key].odds[bookie]."""
    return {"d": {"oddsdata": {"back": {feed_key: {"odds": {BOOKIE: outcomes}}}}}}


def _parse(payload, *, market, home, away, default_bet_id, default_scope_id):
    return parse_feed_payload(
        payload,
        event_url="https://www.oddsportal.com/x/y/h-a-EVENTID1/",
        home=home,
        away=away,
        league="L",
        starts_at=None,
        markets=(market,),
        directory=EventDirectory(),
        now=datetime.now(tz=UTC),
        bookmakers=REGISTRY,
        default_bet_id=default_bet_id,
        default_scope_id=default_scope_id,
    )


def test_basketball_home_away_binds_to_default_bet_and_scope() -> None:
    # E-3-1 (defaultBetId 3, defaultScopeId 1): list [home, away].
    snaps = _parse(
        _payload("E-3-1-0-0-0", [1.35, 2.9]),
        market="home_away",
        home="Lakers",
        away="Celtics",
        default_bet_id=3,
        default_scope_id=1,
    )
    got = {(s.selection, s.decimal_odds, s.market, s.market_detail) for s in snaps}
    assert got == {
        ("Lakers", 1.35, Market.H2H, "home_away"),
        ("Celtics", 2.9, Market.H2H, "home_away"),
    }
    assert all(s.bookmaker == BOOK for s in snaps)


def test_tennis_match_winner_binds_to_default_bet_and_scope() -> None:
    # E-3-2 (defaultBetId 3, defaultScopeId 2): list [player1=home, player2=away].
    snaps = _parse(
        _payload("E-3-2-0-0-0", [1.5, 2.6]),
        market="match_winner",
        home="Alcaraz",
        away="Sinner",
        default_bet_id=3,
        default_scope_id=2,
    )
    got = {(s.selection, s.decimal_odds) for s in snaps}
    assert got == {("Alcaraz", 1.5), ("Sinner", 2.6)}
    assert all(s.market is Market.H2H and s.market_detail == "match_winner" for s in snaps)


def test_basketball_over_under_games_binds_to_betType2_at_default_scope() -> None:
    snaps = _parse(
        _payload("E-2-1-0-220.5-0", [1.9, 1.95]),
        market="over_under_games_220_5",
        home="Lakers",
        away="Celtics",
        default_bet_id=3,
        default_scope_id=1,
    )
    got = {(s.selection, s.decimal_odds, s.market, s.market_detail) for s in snaps}
    assert got == {
        ("Over 220.5", 1.9, Market.TOTALS, "over_under_games_220_5"),
        ("Under 220.5", 1.95, Market.TOTALS, "over_under_games_220_5"),
    }


def test_dynamic_markets_skipped_without_defaults() -> None:
    # No bootstrap defaults => the dynamic headline market cannot be located and
    # is a scrape gap (never guessed), never a crash.
    snaps = _parse(
        _payload("E-3-1-0-0-0", [1.35, 2.9]),
        market="home_away",
        home="Lakers",
        away="Celtics",
        default_bet_id=0,
        default_scope_id=0,
    )
    assert snaps == []


def test_soccer_static_markets_unchanged_by_defaults() -> None:
    # Regression: the proven soccer 1x2 static spec still resolves (and ignores
    # the dynamic defaults), keeping the live soccer path byte-identical.
    snaps = _parse(
        _payload("E-1-2-0-0-0", {"0": 2.5, "1": 4.0, "2": 3.3}),
        market="1x2",
        home="Home",
        away="Away",
        default_bet_id=0,
        default_scope_id=0,
    )
    got = {(s.selection, s.decimal_odds) for s in snaps}
    # feed idx 0=home, 1=draw, 2=away (1/X/2): Home=2.5, Draw=4.0, Away=3.3.
    assert got == {("Home", 2.5), ("Draw", 4.0), ("Away", 3.3)}


def test_build_feed_url_uses_bootstrap_defaults_for_dynamic_markets() -> None:
    # basketball home_away -> betType=defaultBetId(3), scope=defaultScopeId(1)
    u = build_feed_url(3, "EVENTID1", "home_away", default_bet_id=3, default_scope_id=1)
    assert u is not None and "1-3-EVENTID1-3-1-" in u
    # tennis match_winner -> 3 / 2
    u = build_feed_url(2, "EVENTID1", "match_winner", default_bet_id=3, default_scope_id=2)
    assert u is not None and "1-2-EVENTID1-3-2-" in u
    # basketball over/under -> betType 2 at defaultScopeId
    u = build_feed_url(
        3, "EVENTID1", "over_under_games_220_5", default_bet_id=3, default_scope_id=1
    )
    assert u is not None and "1-3-EVENTID1-2-1-" in u
    # soccer 1x2 still works with no defaults (static path)
    u = build_feed_url(1, "EVENTID1", "1x2")
    assert u is not None and "1-1-EVENTID1-1-2-" in u


def _bootstrap_html(*, sport_id: int, default_bet_id: int, default_scope_id: int) -> str:
    data = json.dumps(
        {
            "eventData": {
                "id": "EVENTID1",
                "sportId": sport_id,
                "defaultBetId": default_bet_id,
                "defaultScopeId": default_scope_id,
                "home": "Lakers",
                "away": "Celtics",
                "startDate": 1782000000,
            },
            "eventBody": {},
        }
    )
    return f"<div id='react-event-header' data='{data}'></div>"


def test_extract_bootstrap_reads_default_bet_and_scope() -> None:
    tok = extract_bootstrap_tokens(
        _bootstrap_html(sport_id=3, default_bet_id=3, default_scope_id=1)
    )
    assert tok.sport_id == 3
    assert tok.default_bet_id == 3
    assert tok.default_scope_id == 1


def test_extract_bootstrap_builds_dynamic_feed_urls() -> None:
    tok = extract_bootstrap_tokens(
        _bootstrap_html(sport_id=3, default_bet_id=3, default_scope_id=1),
        markets=("home_away", "over_under_games_220_5"),
    )
    assert "1-3-EVENTID1-3-1-" in tok.feed_urls["home_away"]
    assert "1-3-EVENTID1-2-1-" in tok.feed_urls["over_under_games_220_5"]


# --- wildcard markets: fetch EVERY half-line of a betType in one feed --------
# basketball needs the FULL totals + handicap ladder (the value strategy shops
# every line), not a fixed few — and one betType-2/5 feed body carries them all.


def _multi(back_blocks: dict) -> dict:
    """Decrypted-feed payload with several back keys (one bookie each)."""
    return {"d": {"oddsdata": {"back": {k: {"odds": {BOOKIE: v}} for k, v in back_blocks.items()}}}}


def test_over_under_games_wildcard_emits_all_half_lines_excluding_integers() -> None:
    snaps = _parse(
        _multi(
            {
                "E-2-1-0-171-0": [1.65, 2.1],  # integer line -> PUSH risk -> excluded
                "E-2-1-0-171.5-0": [1.70, 2.00],  # half -> kept
                "E-2-1-0-172.5-0": [1.83, 1.85],  # half -> kept
            }
        ),
        market="over_under_games",
        home="Lakers",
        away="Celtics",
        default_bet_id=3,
        default_scope_id=1,
    )
    assert {s.market_detail for s in snaps} == {"over_under_games_171_5", "over_under_games_172_5"}
    got = {(s.selection, s.decimal_odds, s.market) for s in snaps}
    assert ("Over 171.5", 1.70, Market.TOTALS) in got
    assert ("Under 172.5", 1.85, Market.TOTALS) in got


def test_asian_handicap_games_wildcard_emits_signed_half_lines() -> None:
    snaps = _parse(
        _multi(
            {
                "E-5-1-0--3.5-0": {"0": 1.73, "1": 1.90},  # home -3.5 / away +3.5
                "E-5-1-0--1.5-0": {"0": 1.69, "1": 1.96},
                "E-5-1-0--3-0": {"0": 1.50, "1": 2.50},  # integer handicap -> PUSH -> excluded
            }
        ),
        market="asian_handicap_games",
        home="Lakers",
        away="Celtics",
        default_bet_id=3,
        default_scope_id=1,
    )
    assert {s.market_detail for s in snaps} == {
        "asian_handicap_games_-3_5",
        "asian_handicap_games_-1_5",
    }
    got = {(s.selection, s.decimal_odds, s.market, s.market_detail) for s in snaps}
    assert ("Lakers -3.5", 1.73, Market.SPREADS, "asian_handicap_games_-3_5") in got  # idx0=home
    assert ("Celtics +3.5", 1.90, Market.SPREADS, "asian_handicap_games_-3_5") in got  # idx1=away


def test_wildcard_build_feed_url_points_to_bettype_2_and_5() -> None:
    ou = build_feed_url(3, "EVT", "over_under_games", default_bet_id=3, default_scope_id=1)
    ah = build_feed_url(3, "EVT", "asian_handicap_games", default_bet_id=3, default_scope_id=1)
    assert ou is not None and "1-3-EVT-2-1-" in ou
    assert ah is not None and "1-3-EVT-5-1-" in ah


def test_validate_markets_accepts_wildcard_families() -> None:
    from app.ingestion.oddsportal import _validate_markets

    _validate_markets(["home_away", "over_under_games", "asian_handicap_games"])  # must not raise
