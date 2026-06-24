"""curl_cffi OddsPortal JSON-feed: bookmaker ID->NAME registry (BLOCKER fix).

The decrypted odds feed keys odds PURELY by numeric provider IDs ("707", "263",
"44", ...). Downstream (sharp-anchor classification in app/edge/value.py,
consensus median, CLV close-line capture, devig grouping) keys on bookmaker
NAMES exactly as the Playwright path emits them ("bet365", "Betfair Exchange",
"BetUK"). So the parser MUST translate numeric IDs to canonical names before
emitting, and an UNKNOWN id must be SKIPPED (a scrape gap), never persisted as a
numeric bookmaker.

ROOT CAUSE OF THE LIVE FAILURE (investigation 2026-06-24): OddsPortal migrated to
a Vite/React SSR build. The raw HTML no longer references any ``bookies-*.js``
bundle and the app bundle has no ``bookmakersData`` literal — so the old
fetch-the-bundle registry resolved EMPTY and skipped every soft book. The id->name
map is no longer in any curl_cffi-fetchable JSON resource, so we now ship a STATIC,
live-verified, DB-cross-checked map (app/ingestion/oddsportal_bookmakers.py). These
tests pin the STATIC behaviour: the registry returns that map with ZERO network,
and the feed parser still maps ids->names and skips unknown ids.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal_bookmakers import static_bookmaker_map
from app.ingestion.oddsportal_json import (
    BookmakerRegistry,
    parse_feed_payload,
)

EVENT_URL = "https://www.oddsportal.com/football/x/alpha-beta-AbCdEf12/"
NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)


# --- The shipped static map -------------------------------------------------


def test_static_map_has_live_verified_ids() -> None:
    """The shipped map carries the exact live ids->canonical names verified
    2026-06-24 (the task-named ones, plus the full UK soft-book set). Spellings
    are the EXACT strings the Playwright path stores and downstream matches on."""
    m = static_bookmaker_map()
    assert m["44"] == "Betfair Exchange"
    assert m["21"] == "Betfred"
    assert m["707"] == "BetMGM"
    assert m["263"] == "BetUK"
    assert m["841"] == "Midnite"
    assert m["16"] == "bet365"  # lowercase b — must match the pipeline exactly
    # the named soft books all resolve
    for soft in ("bet365", "bwin", "888sport", "William Hill", "BetVictor", "10bet"):
        assert soft in set(m.values())


def test_static_map_is_read_only() -> None:
    """The shared map must be immutable — a caller mutating it would corrupt the
    registry for the whole process."""
    m = static_bookmaker_map()
    with pytest.raises(TypeError):
        m["999"] = "Hacked"  # type: ignore[index]


def test_static_map_never_maps_to_a_numeric_string() -> None:
    """No value is a bare number — every entry is a real display name (a numeric
    'name' would defeat the whole point of the registry)."""
    for name in static_bookmaker_map().values():
        assert not name.isdigit()


# --- The registry now returns the static map with NO network ----------------


class _ExplodingSession:
    """A session that FAILS the test if any network call is attempted — the
    registry must be purely static now (no page GET, no bundle GET)."""

    async def get(self, url: str, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError(f"registry made a network GET it must not: {url}")


async def test_registry_resolve_returns_static_map_no_network() -> None:
    """BookmakerRegistry.resolve returns the shipped static map without any GET
    (READ-ONLY *and* zero-fetch — the bundle mechanism is gone)."""
    reg = BookmakerRegistry()
    mapping = await reg.resolve(
        _ExplodingSession(), page_url="https://www.oddsportal.com/football/"
    )
    assert mapping["707"] == "BetMGM"
    assert mapping["16"] == "bet365"
    assert mapping == dict(static_bookmaker_map())


async def test_registry_resolve_from_html_returns_static_map_no_network() -> None:
    """resolve_from_html (the path scrape_match_odds uses) also returns the static
    map with no GET — the HTML no longer carries a usable bundle URL."""
    reg = BookmakerRegistry()
    mapping = await reg.resolve_from_html(
        _ExplodingSession(), "<html>no bundle here</html>", base_url=EVENT_URL
    )
    assert mapping["44"] == "Betfair Exchange"
    assert mapping == dict(static_bookmaker_map())


async def test_registry_cached_property_exposes_map_after_resolve() -> None:
    """`cached` returns the map once resolved (the loader reuses it per cycle)."""
    reg = BookmakerRegistry()
    assert reg.cached is None
    await reg.resolve_from_html(_ExplodingSession(), "<html/>", base_url=EVENT_URL)
    assert reg.cached is not None
    assert reg.cached["263"] == "BetUK"


# --- Feed parse still maps ids -> names and skips unknown ids ----------------


def _feed_payload() -> dict:
    """A minimal decrypted feed with 1x2 odds for live ids 707, 263 and an
    UNKNOWN bookie 9999 (absent from the static map)."""
    return {
        "s": 1,
        "d": {
            "time-base": 1750000000,
            "oddsdata": {
                "back": {
                    "E-1-2-0-0-0": {
                        "odds": {
                            "707": {"0": 1.15, "1": 15.0, "2": 7.5},
                            "263": {"0": 1.16, "1": 18.0, "2": 8.0},
                            "9999": {"0": 1.20, "1": 21.0, "2": 8.2},
                        }
                    }
                }
            },
        },
        "refresh": 20,
    }


def test_parse_feed_maps_numeric_ids_to_canonical_names() -> None:
    """parse_feed_payload emits bookmaker NAMES (from the static map), never the
    numeric feed ids — the sharp-anchor/devig/CLV contract."""
    snaps = parse_feed_payload(
        _feed_payload(),
        event_url=EVENT_URL,
        home="Alpha",
        away="Beta",
        league="L",
        starts_at=NOW,
        markets=("1x2",),
        directory=EventDirectory(),
        now=NOW,
        bookmakers=static_bookmaker_map(),
    )
    books = {s.bookmaker for s in snaps}
    assert books == {"BetMGM", "BetUK"}  # names, and 9999 dropped
    assert not any(s.bookmaker.isdigit() for s in snaps)
    h2h = {(s.bookmaker, s.selection): s.decimal_odds for s in snaps}
    assert h2h[("BetMGM", "Alpha")] == 1.15
    assert h2h[("BetUK", "Alpha")] == 1.16


def test_parse_feed_skips_unknown_bookie_id() -> None:
    """An id absent from the map is SKIPPED (scrape gap) — NEVER persisted as a
    numeric bookmaker (the BLOCKER: numeric ids silently break the value engine's
    sharp/soft classification)."""
    snaps = parse_feed_payload(
        _feed_payload(),
        event_url=EVENT_URL,
        home="Alpha",
        away="Beta",
        league="L",
        starts_at=NOW,
        markets=("1x2",),
        directory=EventDirectory(),
        now=NOW,
        bookmakers={"707": "BetMGM", "263": "BetUK"},  # 9999 intentionally absent
    )
    assert all(s.bookmaker in {"BetMGM", "BetUK"} for s in snaps)
    assert not any(s.bookmaker == "9999" for s in snaps)


def test_parse_feed_empty_registry_yields_no_rows() -> None:
    """With NO map every id is unknown -> zero rows (a loud scrape gap the loader
    treats like an empty feed), never numeric bookmakers."""
    snaps = parse_feed_payload(
        _feed_payload(),
        event_url=EVENT_URL,
        home="Alpha",
        away="Beta",
        league="L",
        starts_at=NOW,
        markets=("1x2",),
        directory=EventDirectory(),
        now=NOW,
        bookmakers={},
    )
    assert snaps == []
