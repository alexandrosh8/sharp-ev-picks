"""curl_cffi OddsPortal JSON-feed: bookmaker ID->NAME registry (BLOCKER fix).

The decrypted odds feed keys odds PURELY by numeric provider IDs ("707", "263",
"44", ...). Downstream (sharp-anchor classification in app/edge/value.py,
consensus median, CLV close-line capture, devig grouping) keys on bookmaker
NAMES exactly as the Playwright path emits them ("Pinnacle", "Betfair Exchange",
"bet365"). So the parser MUST translate numeric IDs to canonical names before
emitting, and an UNKNOWN id must be SKIPPED (a scrape gap), never persisted as a
numeric bookmaker.

OddsPortal serves the registry as a static-ish JS bundle assigning
``var bookmakersData = {...}`` keyed by id with the display name in ``WebName``
(quant-sports research 2026-06-23; cross-checked against borewicz/oddsportal +
the live bookies-*.js bundle). These tests pin the PURE parser of that blob; the
GET-only fetch/cache is exercised separately with an injected session.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal_json import (
    BookmakerRegistry,
    parse_bookmaker_registry,
    parse_feed_payload,
)

EVENT_URL = "https://www.oddsportal.com/football/x/alpha-beta-AbCdEf12/"
NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)

# A bundle blob shaped exactly like the live one: bookmakersData keyed by id, the
# display name in WebName (other fields present and ignored). Two of these ids
# (707, 263) appear in the feed fixture below; 9999 is deliberately absent so we
# can assert the unknown-id skip.
_BUNDLE_JS = (
    "var something=1;var bookmakersData={"
    '"707":{"Name":"pinnacle","WebName":"Pinnacle","WebUrl":"/x"},'
    '"263":{"Name":"bet365","WebName":"bet365"},'
    '"44":{"Name":"betfair-ex","WebName":"Betfair Exchange"}'
    "};var other={};"
)


def test_parse_bookmaker_registry_extracts_id_to_webname() -> None:
    """The pure parser pulls bookmakersData out of the JS bundle and returns
    {id: WebName} with OddsPortal's exact display spelling/casing."""
    reg = parse_bookmaker_registry(_BUNDLE_JS)
    assert reg["707"] == "Pinnacle"
    assert reg["263"] == "bet365"  # lowercase b — must match the pipeline exactly
    assert reg["44"] == "Betfair Exchange"
    # keys are the same string ids the feed uses
    assert set(reg) == {"707", "263", "44"}


def test_parse_bookmaker_registry_empty_on_missing_blob() -> None:
    """A bundle without the bookmakersData assignment yields {} (the caller then
    has no map and the feed parse skips every row — a loud scrape gap, never a
    numeric bookmaker)."""
    assert parse_bookmaker_registry("var nope={};") == {}


def _feed_payload() -> dict:
    """A minimal decrypted feed with 1x2 odds for bookies 707, 263 and an
    UNKNOWN bookie 9999 (absent from the registry)."""
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
    """parse_feed_payload emits bookmaker NAMES (from the registry), never the
    numeric feed ids — the sharp-anchor/devig/CLV contract."""
    registry = {"707": "Pinnacle", "263": "bet365", "44": "Betfair Exchange"}
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
        bookmakers=registry,
    )
    books = {s.bookmaker for s in snaps}
    assert books == {"Pinnacle", "bet365"}  # names, and 9999 dropped
    # No numeric bookmaker ever leaks through.
    assert not any(s.bookmaker.isdigit() for s in snaps)
    h2h = {(s.bookmaker, s.selection): s.decimal_odds for s in snaps}
    assert h2h[("Pinnacle", "Alpha")] == 1.15
    assert h2h[("bet365", "Alpha")] == 1.16


def test_parse_feed_skips_unknown_bookie_id() -> None:
    """An id absent from the registry is SKIPPED (scrape gap) — NEVER persisted
    as a numeric bookmaker (the BLOCKER: numeric ids silently break the value
    engine's sharp/soft classification)."""
    registry = {"707": "Pinnacle", "263": "bet365"}  # 9999 intentionally absent
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
        bookmakers=registry,
    )
    assert all(s.bookmaker in {"Pinnacle", "bet365"} for s in snaps)
    assert not any(s.bookmaker == "9999" for s in snaps)


def test_parse_feed_empty_registry_yields_no_rows() -> None:
    """With NO registry every id is unknown -> zero rows (a loud scrape gap the
    loader treats like an empty feed), never numeric bookmakers."""
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


# --- GET-only fetch + cache of the registry ---------------------------------


class _FakeResponse:
    def __init__(self, *, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _FakeSession:
    """Records GETs, returns queued responses by URL substring (no network)."""

    def __init__(self, routes: dict[str, _FakeResponse]) -> None:
        self._routes = routes
        self.requests: list[str] = []

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requests.append(url)
        for needle, resp in self._routes.items():
            if needle in url:
                return resp
        return _FakeResponse(status_code=404)


# A listing/match page that references the versioned bundle URL (the filename is
# timestamp-gated, so it must be read from the page, never hardcoded).
_PAGE_HTML = (
    "<html><head>"
    '<script src="/res/x/bookies-201014103652-1602877009.js"></script>'
    "</head><body>ok</body></html>"
)


async def test_registry_fetches_bundle_get_only_and_builds_map() -> None:
    """BookmakerRegistry.resolve: GET a page -> read the bundle URL from HTML ->
    GET the bundle -> build {id: WebName}. ALL GET (read-only safety)."""
    session = _FakeSession(
        {
            "bookies-": _FakeResponse(text=_BUNDLE_JS),
            "oddsportal.com": _FakeResponse(text=_PAGE_HTML),
        }
    )
    reg = BookmakerRegistry()
    mapping = await reg.resolve(session, page_url="https://www.oddsportal.com/football/")
    assert mapping["707"] == "Pinnacle"
    assert mapping["263"] == "bet365"
    # exactly two GETs: the page, then the bundle
    assert len(session.requests) == 2
    assert any("bookies-" in u for u in session.requests)


async def test_registry_caches_after_first_resolve() -> None:
    """The registry is static-ish: a second resolve uses the cache, no new GET."""
    session = _FakeSession(
        {
            "bookies-": _FakeResponse(text=_BUNDLE_JS),
            "oddsportal.com": _FakeResponse(text=_PAGE_HTML),
        }
    )
    reg = BookmakerRegistry()
    await reg.resolve(session, page_url="https://www.oddsportal.com/football/")
    first = len(session.requests)
    await reg.resolve(session, page_url="https://www.oddsportal.com/football/")
    assert len(session.requests) == first  # cached — no extra network


async def test_registry_resolve_returns_empty_when_bundle_url_absent() -> None:
    """If the page carries no bookies-*.js reference, resolve returns {} (a loud
    scrape gap the caller surfaces) — never guesses an id->name."""
    session = _FakeSession(
        {"oddsportal.com": _FakeResponse(text="<html><body>no bundle</body></html>")}
    )
    reg = BookmakerRegistry()
    mapping = await reg.resolve(session, page_url="https://www.oddsportal.com/football/")
    assert mapping == {}
