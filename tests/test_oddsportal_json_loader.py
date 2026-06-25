"""OddsPortalLoader JSON-feed source selection — NO Playwright fallback.

These tests pin the WIRING (not the feed mechanics — those live in
test_oddsportal_json.py): with `use_json_feed=True` the per-match ODDS come ONLY
from the curl_cffi JSON path. There is NO per-match Playwright odds fallback
(operator instruction 2026-06-23): a per-match JSON failure LOGS (exception type
only) and SKIPS that match — a scrape gap, exactly like a benign Playwright DOM
miss — never silently emptying the slate with stale/Playwright data.

CRITICAL savings invariant: when JSON is on, the dated LISTING runs with NO
markets (match URLs + team context only — cheap), so the expensive per-match
Playwright odds extraction is NEVER paid. Pinned by
`test_json_listing_requests_no_markets_so_no_per_match_playwright`.

The listing-derived contract (`last_fetch_event_ids`, `last_fetch_matches`,
`EventDirectory` registrations) stays identical to the Playwright path.

No network, no oddsharvester import: the listing scrape is an injected fake and
the curl_cffi JSON scrape is an injected no-network fake.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal import OddsPortalLoader
from app.schemas.base import Market

# A listed match dict as the OddsHarvester listing pass yields it. With JSON on
# the listing runs markets=[] so it carries NO odds market dict — only the URL +
# team context. (A leftover odds dict is included to PROVE the loader ignores it
# under the no-fallback policy.)
LISTED_MATCH = {
    "home_team": "Alpha FC",
    "away_team": "Beta United",
    "match_date": "2026-06-11",
    "league_name": "Testland League",
    "match_link": "https://www.oddsportal.com/football/testland/alpha-beta-AbCdEf12/",
    "scraped_date": "2026-06-10T12:00:00Z",
    "1x2_market": [
        {"1": "2.10", "X": "3.40", "2": "3.60", "bookmaker_name": "PWBook"},
    ],
}

# The decrypted feed payload curl_cffi would return for that match. Keyed by a
# numeric provider id (16 -> bet365 in the registry below); proves the JSON odds
# (not any Playwright dict) were used AND that ids are mapped to NAMES.
FEED_PAYLOAD = {
    "s": 1,
    "d": {
        "oddsdata": {
            "back": {
                "E-1-2-0-0-0": {
                    "odds": {"16": {"0": 1.50, "1": 6.00, "2": 4.20}},
                }
            }
        }
    },
    "refresh": 20,
}
# The id->NAME registry the JSON scrape resolves (id 16 -> bet365).
REGISTRY = {"16": "bet365"}


def _listing_scrape(matches: list[dict[str, Any]]) -> Any:
    async def fake_scrape(**kwargs: Any) -> Any:
        fake_scrape.calls.append(kwargs)  # type: ignore[attr-defined]
        return SimpleNamespace(success=matches, failed=[], partial=[])

    fake_scrape.calls = []  # type: ignore[attr-defined]
    return fake_scrape


def _json_loader(
    directory: EventDirectory,
    matches: list[dict[str, Any]],
    *,
    scrape_match: Any,
    listing: Any = None,
) -> OddsPortalLoader:
    """A loader with the JSON feed ON, an injected listing scrape, and an
    injected per-match JSON scrape (so no network and no curl_cffi import)."""
    return OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        markets=("1x2",),
        scrape_fn=listing or _listing_scrape(matches),
        use_json_feed=True,
        json_scrape_fn=scrape_match,
    )


def _json_scrape_using_real_parser(seen: list[str] | None = None) -> Any:
    async def scrape_match(match_url: str, **kwargs: Any) -> list[Any]:
        if seen is not None:
            seen.append(match_url)
        from app.ingestion.oddsportal_json import parse_feed_payload

        return parse_feed_payload(
            FEED_PAYLOAD,
            event_url=match_url,
            home="Alpha FC",
            away="Beta United",
            league="Testland League",
            starts_at=None,
            markets=kwargs["markets"],
            directory=kwargs["directory"],
            now=kwargs["now"],
            bookmakers=REGISTRY,
        )

    return scrape_match


async def test_json_feed_source_selected_when_flag_on() -> None:
    """With use_json_feed=True the per-match odds come from the JSON scrape and
    bookies are canonical NAMES (id 16 -> bet365), never numeric, never PWBook."""
    directory = EventDirectory()
    seen: list[str] = []
    loader = _json_loader(
        directory, [LISTED_MATCH], scrape_match=_json_scrape_using_real_parser(seen)
    )
    snaps = await loader.fetch_odds("soccer")

    assert seen == [str(LISTED_MATCH["match_link"])]
    by_sel = {(s.bookmaker, s.selection): s.decimal_odds for s in snaps}
    assert by_sel[("bet365", "Alpha FC")] == 1.50  # idx0 = home
    assert by_sel[("bet365", "Draw")] == 6.00  # idx1 = Draw (1/X/2 feed order)
    assert by_sel[("bet365", "Beta United")] == 4.20  # idx2 = away
    assert all(s.bookmaker == "bet365" for s in snaps)  # NAME, no numeric, no PWBook
    assert all(s.market is Market.H2H for s in snaps)


async def test_json_failure_skips_match_no_playwright_fallback() -> None:
    """A per-match JSON failure (raises) SKIPS that match — it does NOT fall back
    to Playwright odds (operator: no fallback). The slate simply omits the match,
    exactly like a benign DOM miss; PWBook never appears."""
    directory = EventDirectory()

    async def scrape_match(match_url: str, **kwargs: Any) -> list[Any]:
        raise RuntimeError("feed exploded")

    loader = _json_loader(directory, [LISTED_MATCH], scrape_match=scrape_match)
    snaps = await loader.fetch_odds("soccer")

    # No fallback: the match is a scrape gap, NOT the Playwright PWBook odds.
    assert snaps == []
    assert not any(s.bookmaker == "PWBook" for s in snaps)


async def test_json_empty_skips_match_no_playwright_fallback() -> None:
    """A JSON scrape yielding ZERO snapshots (off-window / empty / unresolved
    registry) also SKIPS the match — no Playwright fallback."""
    directory = EventDirectory()

    async def scrape_match(match_url: str, **kwargs: Any) -> list[Any]:
        return []

    loader = _json_loader(directory, [LISTED_MATCH], scrape_match=scrape_match)
    snaps = await loader.fetch_odds("soccer")
    assert snaps == []
    assert not any(s.bookmaker == "PWBook" for s in snaps)


async def test_json_listing_requests_no_markets_so_no_per_match_playwright() -> None:
    """SAVINGS INVARIANT: with JSON on, the dated LISTING scrape is invoked with
    an EMPTY markets list, so OddsHarvester does NOT run the expensive per-match
    Playwright odds extraction (it only collects URLs + header team context). The
    per-match odds come solely from curl_cffi. If this regresses, the migration
    pays the full Playwright cost AND the JSON cost — zero savings."""
    directory = EventDirectory()
    listing = _listing_scrape([LISTED_MATCH])
    loader = _json_loader(
        directory, [LISTED_MATCH], scrape_match=_json_scrape_using_real_parser(), listing=listing
    )
    await loader.fetch_odds("soccer")

    assert listing.calls, "listing scrape must still run (URLs + team context)"
    for call in listing.calls:
        assert call["markets"] == [], (
            "JSON-on listing must request NO markets — otherwise OddsHarvester "
            "still does per-match Playwright odds extraction (zero CPU savings)"
        )


async def test_default_listing_requests_full_markets() -> None:
    """With the flag OFF (default) the listing keeps requesting the full markets
    list (the Playwright path IS the odds source) — savings change only applies
    to the JSON path."""
    directory = EventDirectory()
    listing = _listing_scrape([LISTED_MATCH])
    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        markets=("1x2",),
        scrape_fn=listing,
    )
    await loader.fetch_odds("soccer")
    assert listing.calls
    for call in listing.calls:
        assert call["markets"] == ["1x2"], "default Playwright path requests real markets"


async def test_json_path_preserves_last_fetch_event_ids_contract() -> None:
    """last_fetch_event_ids / last_fetch_matches come from the LISTING and stay
    identical to the Playwright path — the Betfair/pipeline contract must not
    change when the JSON feed is on. Team context is registered by the JSON
    scrape (it reads the match-page HTML)."""
    directory = EventDirectory()
    loader = _json_loader(directory, [LISTED_MATCH], scrape_match=_json_scrape_using_real_parser())
    await loader.fetch_odds("soccer")

    assert loader.last_fetch_matches["soccer"] == 1
    assert loader.last_fetch_event_ids["soccer"] == (str(LISTED_MATCH["match_link"]),)
    teams = directory.lookup(str(LISTED_MATCH["match_link"]))
    assert teams is not None
    assert teams.home == "Alpha FC"
    assert teams.away == "Beta United"


async def test_default_loader_keeps_playwright_path() -> None:
    """With the flag OFF (the default) the per-match odds come straight from the
    Playwright market dict — the JSON path is never touched."""
    directory = EventDirectory()
    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        markets=("1x2",),
        scrape_fn=_listing_scrape([LISTED_MATCH]),
    )
    snaps = await loader.fetch_odds("soccer")
    by_sel = {(s.bookmaker, s.selection): s.decimal_odds for s in snaps}
    assert by_sel[("PWBook", "Alpha FC")] == 2.10


# --- fetch_match_odds (off-window / finished-score path) under the flag ------


async def test_fetch_match_odds_uses_json_for_odds_scrapes() -> None:
    """The off-window odds revalidation path also uses the JSON feed per link
    when the flag is on (canonical bet365 prices prove it)."""
    directory = EventDirectory()
    url = str(LISTED_MATCH["match_link"])
    loader = _json_loader(directory, [LISTED_MATCH], scrape_match=_json_scrape_using_real_parser())
    snaps = await loader.fetch_match_odds("soccer", [url], prefiltered=True)

    by_sel = {(s.bookmaker, s.selection): s.decimal_odds for s in snaps}
    assert by_sel[("bet365", "Alpha FC")] == 1.50
    assert all(s.bookmaker == "bet365" for s in snaps)


async def test_fetch_match_odds_json_failure_skips_link_no_playwright() -> None:
    """A JSON failure on the off-window path SKIPS the failed link — it does NOT
    fall back to a Playwright scrape (no fallback). The Playwright listing scrape
    must NEVER be invoked for odds when JSON is on."""
    directory = EventDirectory()
    url = str(LISTED_MATCH["match_link"])

    async def scrape_match(match_url: str, **kwargs: Any) -> list[Any]:
        raise RuntimeError("feed exploded")

    playwright_called = False

    async def listing_scrape(**kwargs: Any) -> Any:
        nonlocal playwright_called
        playwright_called = True
        return SimpleNamespace(success=[dict(LISTED_MATCH)], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        markets=("1x2",),
        scrape_fn=listing_scrape,
        use_json_feed=True,
        json_scrape_fn=scrape_match,
    )
    snaps = await loader.fetch_match_odds("soccer", [url], prefiltered=True)
    assert snaps == []  # failed link skipped, not Playwright-recovered
    assert not playwright_called, "no Playwright odds fallback when JSON is on"


async def test_fetch_match_odds_score_only_stays_playwright() -> None:
    """score_only=True (finished-score capture) must NOT use the JSON odds feed —
    it stays the cheap Playwright header-only read (zero markets). The JSON
    scrape is never called; the Playwright pass runs with an empty market list."""
    directory = EventDirectory()
    url = str(LISTED_MATCH["match_link"])
    json_called = False

    async def scrape_match(match_url: str, **kwargs: Any) -> list[Any]:
        nonlocal json_called
        json_called = True
        return []

    seen_markets: list[list[str]] = []

    async def fallback_scrape(**kwargs: Any) -> Any:
        seen_markets.append(list(kwargs["markets"]))
        return SimpleNamespace(
            success=[
                {
                    "home_team": "Alpha FC",
                    "away_team": "Beta United",
                    "match_link": url,
                    "match_date": "2026-06-11",
                    "home_score": "2",
                    "away_score": "1",
                    "is_finished": True,
                }
            ],
            failed=[],
            partial=[],
        )

    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        markets=("1x2",),
        scrape_fn=fallback_scrape,
        use_json_feed=True,
        json_scrape_fn=scrape_match,
    )
    snaps = await loader.fetch_match_odds("soccer", [url], prefiltered=True, score_only=True)

    assert not json_called, "score_only must not touch the JSON odds feed"
    assert seen_markets == [[]], "score_only forces an empty Playwright market list"
    assert snaps == []  # header-only: score reaches the caller via the directory
    teams = directory.lookup(url)
    assert teams is not None
    assert teams.home_score == 2
    assert teams.away_score == 1
    assert teams.finished is True


# --- THE PROOF: zero per-match Playwright; teams from curl_cffi HTML ----------
#
# The handoff's root cause was that JSON-on still Playwright-rendered EVERY match
# page (to read the header for team context), so there was no speed win. These
# tests prove the fix end to end: with the flag on, the loader (1) enumerates
# match URLs from a SINGLE listing call that carries NO team fields, and (2) gets
# odds + TEAMS for each match from the REAL `oddsportal_json.scrape_match_odds`
# running over a fake curl_cffi session — which makes ONLY ``.get`` calls and no
# Playwright Page render whatsoever. Team context is derived from the curl-fetched
# match-page HTML (the England/Ghana fixture), NOT from any listing/Playwright dict.

from pathlib import Path  # noqa: E402

_FIX = Path(__file__).parent / "fixtures"
_MATCH_PAGE = (_FIX / "oddsportal_match_page_KhgvzGjJ.html").read_text()
_FEED = (_FIX / "oddsportal_feed_KhgvzGjJ.dat").read_text()
# The navigable match URL IS the platform-wide event identity.
_EVENT_URL = (
    "https://www.oddsportal.com/football/england/international-friendly/england-ghana-KhgvzGjJ/"
)


class _RecordingCurlSession:
    """A curl_cffi AsyncSession stand-in that serves the England-Ghana fixtures
    and RECORDS every call. It exposes ONLY ``.get`` (and ``.close``) — so the
    per-match path is structurally GET-only, and any Playwright render would have
    to go through a DIFFERENT object the JSON path never touches."""

    def __init__(self) -> None:
        self.gets: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> Any:
        self.gets.append(url)
        # match-event feed vs the match page HTML (the page carries team context).
        text = _FEED if "match-event/" in url else _MATCH_PAGE
        return SimpleNamespace(text=text, status_code=200, headers={})

    async def close(self) -> None:  # pragma: no cover - not used by the JSON path
        pass


def _real_json_scrape_over_curl(session: _RecordingCurlSession) -> Any:
    """A `json_scrape_fn` that runs the REAL `scrape_match_odds` over the fake
    curl session — proving the production per-match mechanics (HTML -> bootstrap
    teams -> feed decrypt) without a browser and without network."""

    async def scrape_match(match_url: str, **kwargs: Any) -> list[Any]:
        from app.ingestion.oddsportal_json import scrape_match_odds

        return await scrape_match_odds(
            match_url,
            markets=kwargs["markets"],
            directory=kwargs["directory"],
            now=kwargs["now"],
            session=session,
            registry=kwargs.get("registry"),
        )

    return scrape_match


# The listing yields ONLY the match URL — NO home_team/away_team/league. So if
# team context appears in the directory, it can ONLY have come from the curl_cffi
# match-page HTML (the whole point of the fix).
_URL_ONLY_LISTED_MATCH = {"match_link": _EVENT_URL}


async def test_json_per_match_uses_only_curl_get_no_playwright_render() -> None:
    """PROOF (savings): with JSON on, the per-match path makes ONLY curl_cffi
    ``.get`` calls — NO Playwright page render / page.content(). The fake curl
    session is the ONLY transport the per-match scrape touches; it records 5 GETs
    (1 HTML + 4 feed) and ZERO renders. The bookmaker id->name map is STATIC
    (no bundle GET since 2026-06-24). The listing fake is the single
    Playwright-shaped call and it runs ONCE (URL enumeration), not per match. If
    the old bug regressed (a per-match Playwright render), this path would need a
    Page object the JSON scrape never imports — there isn't one."""
    directory = EventDirectory()
    curl = _RecordingCurlSession()

    listing_calls = {"n": 0}

    async def listing_scrape(**kwargs: Any) -> Any:
        listing_calls["n"] += 1
        # markets=[] AND, critically, the dicts carry ONLY the URL — no team
        # fields, so no per-match Playwright header read produced them.
        assert kwargs["markets"] == []
        return SimpleNamespace(success=[dict(_URL_ONLY_LISTED_MATCH)], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["international-friendly"])},
        markets=("1x2", "over_under_2_5", "btts", "double_chance"),
        scrape_fn=listing_scrape,
        use_json_feed=True,
        json_scrape_fn=_real_json_scrape_over_curl(curl),
    )

    snaps = await loader.fetch_odds("soccer")

    # 1. The per-match transport was curl_cffi GET-only: 5 GETs, no render method.
    #    The id->name map is STATIC (no bundle GET), so it's 1 HTML + 4 feeds.
    assert curl.gets, "the JSON per-match path must have fetched via curl .get"
    assert len(curl.gets) == 5, f"expected 1 HTML + 4 feed GETs, got {curl.gets}"
    assert sum("match-event/" in u for u in curl.gets) == 4  # the 4 market feeds
    assert not any("bookies-" in u for u in curl.gets)  # static map: no bundle GET
    # The match PAGE HTML was GET-fetched by curl (this is where teams come from).
    assert any(u == _EVENT_URL for u in curl.gets)

    # 2. The listing (the only Playwright-shaped call) ran ONCE — not per match.
    assert listing_calls["n"] == 1, "listing must be a single URL-enumeration call"

    # 3. Team context was derived from the curl-fetched HTML, NOT a Playwright/
    #    listing dict (the listing carried only the URL).
    teams = directory.lookup(_EVENT_URL)
    assert teams is not None
    assert teams.home == "England"
    assert teams.away == "Ghana"

    # 4. Real soft-book odds flowed with canonical NAMES (not numeric ids).
    assert snaps, "the JSON feed must yield soft-book odds"
    assert any(s.bookmaker == "BetMGM" for s in snaps)  # id 707 -> static map
    assert any(s.bookmaker == "bet365" for s in snaps)  # id 16 -> static map
    assert all(not s.bookmaker.isdigit() for s in snaps)


async def test_json_on_never_imports_or_calls_playwright_per_match(
    monkeypatch: Any,
) -> None:
    """PROOF (hard guard): POISON ``playwright`` so importing it raises, then run a
    full JSON-on cycle. The per-match path resolves teams + odds from curl_cffi
    HTML, so it never imports/launches Playwright — the cycle completes cleanly.
    If a per-match render sneaked back in (the old bug), it would import
    ``playwright`` and crash here on the poisoned import, failing the test."""
    import builtins

    real_import = builtins.__import__

    def _no_playwright(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "playwright" or name.startswith("playwright."):
            raise AssertionError(
                "the JSON per-match path imported playwright — the per-match "
                "render the migration must eliminate has regressed"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_playwright)

    directory = EventDirectory()
    curl = _RecordingCurlSession()

    async def listing_scrape(**kwargs: Any) -> Any:
        return SimpleNamespace(success=[dict(_URL_ONLY_LISTED_MATCH)], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["international-friendly"])},
        markets=("1x2",),
        scrape_fn=listing_scrape,
        use_json_feed=True,
        json_scrape_fn=_real_json_scrape_over_curl(curl),
    )

    snaps = await loader.fetch_odds("soccer")  # must not trip the poisoned import
    assert snaps  # odds flowed entirely via curl_cffi
    assert directory.lookup(_EVENT_URL) is not None  # teams from curl HTML
