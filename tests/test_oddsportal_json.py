"""curl_cffi OddsPortal JSON-feed ingester — parse contract.

This is the ADDITIVE fast path: it fetches OddsPortal's encrypted odds JSON
feed directly (no Playwright DOM render), decrypts it in pure Python, and must
yield byte-identical `OddsSnapshotIn` rows + `EventDirectory.register` calls to
the Playwright adapter (app/ingestion/oddsportal._convert_match).

The feed fixtures are REAL encrypted envelopes: a decrypted payload modelled on
the live England-vs-Ghana event (eventId KhgvzGjJ, verify findings 2026-06-22)
re-encrypted with the STATIC public-bundle key, so the test exercises the full
decrypt -> parse path, not a stubbed JSON blob.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal_bookmakers import static_bookmaker_map
from app.ingestion.oddsportal_json import (
    _FEED_MARKETS,
    FeedDecryptError,
    FeedOffWindow,
    FeedToken,
    build_feed_url,
    build_feed_urls,
    decrypt_feed_body,
    extract_bootstrap_tokens,
    fetch_match_feed,
    parse_feed_payload,
    scrape_match_odds,
)
from app.schemas.base import Market

_FIX = Path(__file__).parent / "fixtures"
_FEED = _FIX / "oddsportal_feed_KhgvzGjJ.dat"
_FEED_GZIP = _FIX / "oddsportal_feed_KhgvzGjJ_gzip.dat"
_DECRYPTED = _FIX / "oddsportal_feed_KhgvzGjJ.decrypted.json"
_MATCH_PAGE = _FIX / "oddsportal_match_page_KhgvzGjJ.html"

# The navigable OddsPortal match URL IS the event identity across the platform.
EVENT_URL = (
    "https://www.oddsportal.com/football/england/international-friendly/england-ghana-KhgvzGjJ/"
)
NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
MARKETS = ("1x2", "over_under_2_5", "btts", "double_chance")

# The feed keys odds by numeric provider IDs; the parser MUST translate them to
# canonical bookmaker NAMES (the exact spellings the Playwright path emits) — so
# every assertion below keys on the NAME, never the id. Sourced from the shipped
# STATIC map (app/ingestion/oddsportal_bookmakers.py) so the parser tests use the
# SAME id->name bindings the live feed path uses (the `bookies-*.js` bundle the
# old `parse_bookmaker_registry` parsed was removed in OddsPortal's React
# migration — root-cause 2026-06-24). The feed fixture's ids (16/21/263/44/707/
# 841) are all present in the static map.
REGISTRY = static_bookmaker_map()
# Sanity: the pipeline-critical canonical spellings are present and exact.
assert REGISTRY["707"] == "BetMGM"
assert REGISTRY["44"] == "Betfair Exchange"
assert REGISTRY["16"] == "bet365"

# The feed's d.time-base (provider observation time) is the snapshot captured_at
# (FIX 3), NOT `now` — epoch 1750000000 == 2025-06-15T15:06:40Z.
FEED_CAPTURED_AT = datetime(2025, 6, 15, 15, 6, 40, tzinfo=UTC)


def _decrypted_ref() -> dict:
    return json.loads(_DECRYPTED.read_text())


def test_decrypt_feed_body_roundtrips_to_reference_json() -> None:
    """The static-key AES-256-CBC decrypt of the captured envelope yields the
    exact reference JSON payload (top keys s/d/refresh, odds at
    d.oddsdata.back)."""
    payload = decrypt_feed_body(_FEED.read_text())
    assert payload == _decrypted_ref()
    assert payload["refresh"] == 20
    assert "E-1-2-0-0-0" in payload["d"]["oddsdata"]["back"]


def test_decrypt_handles_gzip_inner_envelope() -> None:
    """Some feed bodies gzip the plaintext before AES; the decrypt must
    transparently gunzip and return the same JSON."""
    assert decrypt_feed_body(_FEED_GZIP.read_text()) == _decrypted_ref()


def test_parse_feed_yields_playwright_identical_snapshots() -> None:
    """parse_feed_payload turns the decrypted feed into OddsSnapshotIn rows that
    match the Playwright adapter's output boundary exactly."""
    directory = EventDirectory()
    payload = decrypt_feed_body(_FEED.read_text())
    snaps = parse_feed_payload(
        payload,
        event_url=EVENT_URL,
        home="England",
        away="Ghana",
        league="International Friendly",
        starts_at=NOW,
        markets=MARKETS,
        directory=directory,
        now=NOW,
        bookmakers=REGISTRY,
    )

    # Every row carries the navigable match URL as event_id, the raw market key
    # as market_detail, decimal odds > 1.0, a NON-numeric bookmaker NAME, and the
    # feed's provider observation time as captured_at (FIX 3).
    assert snaps, "expected snapshots from the feed"
    for s in snaps:
        assert s.event_id == EVENT_URL
        assert s.decimal_odds > 1.0
        assert s.market_detail in MARKETS
        assert s.ingested_at == NOW
        assert s.captured_at == FEED_CAPTURED_AT  # feed time-base, not now()
        assert not s.bookmaker.isdigit()  # NAME, never a numeric feed id

    # --- 1x2 (H2H): home/Draw/away with the verified bookie prices, keyed by
    # the canonical NAME (BetMGM = id 707, Betfair Exchange = id 44). ---
    h2h = {(s.bookmaker, s.selection): s.decimal_odds for s in snaps if s.market_detail == "1x2"}
    assert h2h[("BetMGM", "England")] == 1.15
    assert h2h[("BetMGM", "Draw")] == 7.5
    assert h2h[("BetMGM", "Ghana")] == 15.0
    assert h2h[("Betfair Exchange", "England")] == 1.20
    assert all(s.market is Market.H2H for s in snaps if s.market_detail == "1x2")
    # six bookies x three outcomes
    assert len(h2h) == 18

    # --- over_under_2_5 (TOTALS): Over/Under labels, market_detail carries line. ---
    ou = {
        (s.bookmaker, s.selection): s.decimal_odds
        for s in snaps
        if s.market_detail == "over_under_2_5"
    }
    assert ou[("BetMGM", "Over 2.5")] == 1.90
    assert ou[("BetMGM", "Under 2.5")] == 1.90
    assert ou[("BetUK", "Under 2.5")] == 1.85  # id 263
    assert all(s.market is Market.TOTALS for s in snaps if s.market_detail == "over_under_2_5")

    # --- btts (BTTS): Yes/No. ---
    btts = {(s.bookmaker, s.selection): s.decimal_odds for s in snaps if s.market_detail == "btts"}
    assert btts[("BetMGM", "BTTS Yes")] == 2.10
    assert btts[("BetMGM", "BTTS No")] == 1.70
    assert all(s.market is Market.BTTS for s in snaps if s.market_detail == "btts")

    # --- double_chance (DOUBLE_CHANCE): 1X / 12 / X2 readable names. ---
    dc = {
        (s.bookmaker, s.selection): s.decimal_odds
        for s in snaps
        if s.market_detail == "double_chance"
    }
    assert dc[("BetMGM", "England or Draw")] == 1.04
    assert dc[("BetMGM", "England or Ghana")] == 1.02
    assert dc[("BetMGM", "Draw or Ghana")] == 3.40
    assert all(
        s.market is Market.DOUBLE_CHANCE for s in snaps if s.market_detail == "double_chance"
    )

    # Directory registered with team context, keyed by the match URL.
    teams = directory.lookup(EVENT_URL)
    assert teams is not None
    assert teams.home == "England"
    assert teams.away == "Ghana"
    assert teams.league == "International Friendly"
    assert teams.starts_at == NOW


def test_parse_feed_dedups_bookmaker_rows_per_market() -> None:
    """A duplicate bookie block within one market must not double the rows
    (devig protection — mirrors _convert_match's seen_books)."""
    payload = decrypt_feed_body(_FEED.read_text())
    # Inject a duplicate price block for bookie 707 in the 1x2 market.
    back = payload["d"]["oddsdata"]["back"]
    back["E-1-2-0-0-0"]["odds"]["707"] = {"0": 1.15, "1": 15.0, "2": 7.5}
    snaps = parse_feed_payload(
        payload,
        event_url=EVENT_URL,
        home="England",
        away="Ghana",
        league="International Friendly",
        starts_at=NOW,
        markets=("1x2",),
        directory=EventDirectory(),
        now=NOW,
        bookmakers=REGISTRY,
    )
    keys = [(s.bookmaker, s.selection) for s in snaps]
    assert len(keys) == len(set(keys)), "duplicate bookmaker rows leaked"


def test_parse_feed_skips_unparseable_and_sub_1_odds() -> None:
    """Odds <= 1.0 or non-numeric are dropped (decimal-odds invariant). Uses a
    KNOWN bookie id (16 -> bet365) with all-invalid odds, so the drop is the
    odds filter — not the unknown-id skip — and bet365 contributes no rows."""
    payload = decrypt_feed_body(_FEED.read_text())
    payload["d"]["oddsdata"]["back"]["E-1-2-0-0-0"]["odds"]["16"] = {
        "0": 1.0,  # exactly 1.0 -> rejected
        "1": "n/a",  # non-numeric -> rejected
        "2": 0.5,  # < 1.0 -> rejected
    }
    snaps = parse_feed_payload(
        payload,
        event_url=EVENT_URL,
        home="England",
        away="Ghana",
        league="International Friendly",
        starts_at=NOW,
        markets=("1x2",),
        directory=EventDirectory(),
        now=NOW,
        bookmakers=REGISTRY,
    )
    # bet365 (id 16) had only invalid odds -> no rows; no numeric id ever leaks.
    assert all(s.bookmaker != "bet365" for s in snaps)
    assert not any(s.bookmaker.isdigit() for s in snaps)


def test_extract_bootstrap_tokens_from_match_page_html() -> None:
    """The SSR react-event-header div carries eventData.{id,xhash,xhashf,sportId}
    — fetchable by curl_cffi (HTTP 200), no browser needed. The %-escaped
    xhash/xhashf are URL-decoded ('%79%6a%64%35%31' -> 'yjd51')."""
    html = _MATCH_PAGE.read_text()
    tok = extract_bootstrap_tokens(html)
    assert tok.event_id == "KhgvzGjJ"
    assert tok.sport_id == 1
    assert tok.xhash == "yjd51"
    assert tok.xhashf == "yj0da"
    assert tok.home == "England"
    assert tok.away == "Ghana"
    # No status -> finished is None/False (Scheduled), never True pre-kickoff.
    assert tok.finished is not True


# --- kickoff capture: eventData.startDate OR eventBody.startDate ------------
# Live verify 2026-06-24 (US lower-league fixtures, 42 NULL-starts_at events):
# OddsPortal serves the kickoff epoch in `eventData.startDate` for some events
# and in `eventBody.startDate` for others (e.g. "Los Angeles FC 2" carried
# eventData.startDate=None, eventBody.startDate=1782352800). The parser must
# read whichever location holds a real positive epoch, else starts_at is NULL
# and the dashboard shows "TBD" despite OddsPortal knowing the time.


def _header_html(event_data: dict, event_body: dict | None = None) -> str:
    """Minimal SSR match page carrying a react-event-header bootstrap blob."""
    payload = json.dumps({"eventData": event_data, "eventBody": event_body or {}})
    # single-quote the data attr (matches the live page + shipped fixture form)
    return f"<div id='react-event-header' data='{payload}'></div>"


def test_extract_bootstrap_captures_kickoff_from_event_data_start_date() -> None:
    """The shipped fixture (English friendly) keeps the kickoff in
    eventData.startDate — a 200-epoch -> UTC datetime. Regression guard so the
    eventBody fallback never regresses the primary path."""
    tok = extract_bootstrap_tokens(_MATCH_PAGE.read_text())
    assert tok.starts_at == datetime(2025, 6, 16, 12, 0, tzinfo=UTC)  # epoch 1750075200


def test_extract_bootstrap_falls_back_to_event_body_start_date() -> None:
    """US lower-league bug: eventData.startDate is absent/None but
    eventBody.startDate carries the real kickoff epoch. The parser must capture
    it (the LA FC 2 case: epoch 1782352800 == 2026-06-25T02:00:00Z)."""
    html = _header_html(
        {"id": "M5775Uo9", "sportId": 1, "home": "Los Angeles FC 2", "away": "Minnesota 2"},
        {"startDate": 1782352800, "endDate": False},
    )
    tok = extract_bootstrap_tokens(html)
    assert tok.starts_at == datetime(2026, 6, 25, 2, 0, tzinfo=UTC)


def test_extract_bootstrap_event_data_start_date_wins_over_body() -> None:
    """When both are present, eventData.startDate is authoritative (the body is
    only a fallback for the events that omit it)."""
    html = _header_html(
        {"id": "X1", "sportId": 1, "home": "A", "away": "B", "startDate": 1750075200},
        {"startDate": 1782352800},
    )
    tok = extract_bootstrap_tokens(html)
    assert tok.starts_at == datetime(2025, 6, 16, 12, 0, tzinfo=UTC)


def test_extract_bootstrap_genuinely_missing_kickoff_stays_none() -> None:
    """A truly-TBD fixture (no positive epoch anywhere) yields starts_at=None —
    NEVER an invented time. `endDate: False` and a 0/negative epoch are not a
    kickoff and must not be mistaken for one."""
    html = _header_html(
        {"id": "X2", "sportId": 1, "home": "A", "away": "B", "startDate": None},
        {"startDate": False, "endDate": 0},
    )
    tok = extract_bootstrap_tokens(html)
    assert tok.starts_at is None


class _FakeResponse:
    def __init__(self, *, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _FakeSession:
    """Minimal curl_cffi AsyncSession stand-in: records GETs, returns queued
    responses by URL substring. No network — honours the project's test rule."""

    def __init__(self, routes: dict[str, _FakeResponse]) -> None:
        self._routes = routes
        self.requests: list[tuple[str, dict]] = []
        self.closed = False

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requests.append((url, dict(kwargs)))
        for needle, resp in self._routes.items():
            if needle in url:
                return resp
        return _FakeResponse(status_code=404)

    async def close(self) -> None:
        self.closed = True


async def test_fetch_match_feed_is_get_only_and_yields_snapshots() -> None:
    """Given a match URL + a feed token, fetch_match_feed GETs the encrypted
    feed via the (injected) session, decrypts + parses it, and returns the same
    OddsSnapshotIn rows the Playwright path would. ALL requests are GET."""
    feed_url = (
        "https://www.oddsportal.com/match-event/"
        "1-1-KhgvzGjJ-1-2-579bd8637cd0ec950f3bcfc57126a839.dat?geo=GB&lang=en"
    )
    token = FeedToken(
        event_id="KhgvzGjJ",
        sport_id=1,
        feed_urls={"1x2": feed_url},
        home="England",
        away="Ghana",
        league="International Friendly",
        starts_at=NOW,
    )
    session = _FakeSession({"match-event/": _FakeResponse(text=_FEED.read_text())})
    directory = EventDirectory()

    snaps = await fetch_match_feed(
        EVENT_URL,
        token=token,
        markets=("1x2",),
        directory=directory,
        now=NOW,
        session=session,
        bookmakers=REGISTRY,
    )

    # GET-only: the fake records the method implicitly (only .get exists).
    assert session.requests, "expected at least one GET"
    assert all("match-event/" in url or EVENT_URL in url for url, _ in session.requests)

    h2h = {(s.bookmaker, s.selection): s.decimal_odds for s in snaps}
    assert h2h[("BetMGM", "England")] == 1.15  # id 707 -> canonical NAME (BetMGM)
    assert h2h[("BetMGM", "Draw")] == 7.5
    assert all(s.event_id == EVENT_URL for s in snaps)
    assert directory.lookup(EVENT_URL) is not None


async def test_fetch_match_feed_treats_off_window_envelope_as_no_odds() -> None:
    """A short off-window body (no ct:iv envelope) yields no snapshots but still
    registers the event — like a Playwright scrape gap, never a hard error."""
    token = FeedToken(
        event_id="KhgvzGjJ",
        sport_id=1,
        feed_urls={"1x2": "https://www.oddsportal.com/match-event/x.dat"},
        home="England",
        away="Ghana",
        league="International Friendly",
        starts_at=NOW,
    )
    # base64("no-colon-here") -> decodes to text without ':' -> off-window path.
    import base64 as _b64

    bad = _b64.b64encode(b"no-colon-here").decode()
    session = _FakeSession({"match-event/": _FakeResponse(text=bad)})
    directory = EventDirectory()

    snaps = await fetch_match_feed(
        EVENT_URL,
        token=token,
        markets=("1x2",),
        directory=directory,
        now=NOW,
        session=session,
        bookmakers=REGISTRY,
    )
    assert snaps == []
    assert directory.lookup(EVENT_URL) is not None  # event still registered


async def test_fetch_match_feed_handles_non_200_gracefully() -> None:
    """A non-200 feed response is a scrape gap, not a crash: no snapshots."""
    token = FeedToken(
        event_id="KhgvzGjJ",
        sport_id=1,
        feed_urls={"1x2": "https://www.oddsportal.com/match-event/x.dat"},
        home="England",
        away="Ghana",
        league="International Friendly",
        starts_at=NOW,
    )
    session = _FakeSession({"match-event/": _FakeResponse(status_code=503, text="")})
    snaps = await fetch_match_feed(
        EVENT_URL,
        token=token,
        markets=("1x2",),
        directory=EventDirectory(),
        now=NOW,
        session=session,
        bookmakers=REGISTRY,
    )
    assert snaps == []


async def test_fetch_match_feed_logs_loud_rotation_alert_on_constant_drift(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With NO Playwright fallback, a JSON-wide failure must be LOUD. The
    version-guard RuntimeError (KDF-constant drift / a half-applied bundle
    rotation) is the highest-signal failure — it must surface at WARNING naming
    the bundle, NOT be swallowed quietly. fetch_match_feed catches it, keeps the
    scrape a gap (fail-closed), and logs the rotation alert."""
    import logging

    import app.ingestion.oddsportal_json as mod

    # Drift the KDF constants so decrypt_feed_body's _verify_key_fingerprint
    # raises the RuntimeError on a real (well-formed) feed body.
    monkeypatch.setattr(mod, "_KDF_PASSPHRASE", "tampered-passphrase")
    token = FeedToken(
        event_id="KhgvzGjJ",
        sport_id=1,
        feed_urls={"1x2": "https://www.oddsportal.com/match-event/x.dat"},
        home="England",
        away="Ghana",
        league="International Friendly",
        starts_at=NOW,
    )
    session = _FakeSession({"match-event/": _FakeResponse(text=_FEED.read_text())})
    with caplog.at_level(logging.WARNING, logger="app.ingestion.oddsportal_json"):
        snaps = await fetch_match_feed(
            EVENT_URL,
            token=token,
            markets=("1x2",),
            directory=EventDirectory(),
            now=NOW,
            session=session,
            bookmakers=REGISTRY,
        )
    assert snaps == []  # fail-closed: never a wrong price
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a constant-drift/rotation failure must log at WARNING (no fallback now)"
    assert any(
        "rotation" in r.getMessage().lower() or "drift" in r.getMessage().lower() for r in warnings
    ), "the rotation alert must name the bundle-rotation / key-drift cause"


def test_pytest_imported_marker() -> None:
    """`pytest` import is used (async markers); keep the linter honest."""
    assert pytest is not None


# --- dataTagMD5 feed-URL builder (pure Python) ------------------------------


def test_build_feed_url_is_pure_python_md5_and_deterministic() -> None:
    """The feed URL is built in pure Python with a 32-hex md5 segment, routed on
    the 1-{sportId}-{eventId}-{betType}-{scope} prefix. The server ignores the
    hash, but we make it deterministic per (event, market)."""
    import hashlib

    url = build_feed_url(1, "KhgvzGjJ", "1x2")
    assert url is not None
    # prefix the server routes on (betType 1 = 1X2, scope 2 = Full Time)
    assert url.startswith("https://www.oddsportal.com/match-event/1-1-KhgvzGjJ-1-2-")
    assert url.endswith(".dat")
    # 32-hex md5 segment, deterministic for the same input
    md5_seg = url.rsplit("-", 1)[1].removesuffix(".dat")
    assert len(md5_seg) == 32
    assert all(c in "0123456789abcdef" for c in md5_seg)
    assert build_feed_url(1, "KhgvzGjJ", "1x2") == url  # stable
    # the hash is a real md5 of the documented stable input string
    expected = hashlib.md5(b"1-KhgvzGjJ-1-2", usedforsecurity=False).hexdigest()
    assert md5_seg == expected
    # betType differs per market -> distinct prefix
    assert "-2-2-" in (build_feed_url(1, "KhgvzGjJ", "over_under_2_5") or "")
    assert "-13-2-" in (build_feed_url(1, "KhgvzGjJ", "btts") or "")
    assert "-4-2-" in (build_feed_url(1, "KhgvzGjJ", "double_chance") or "")


def test_build_feed_url_skips_unmapped_market() -> None:
    """An unsupported / unmappable market yields None (caller scrape-gaps it),
    never a malformed URL."""
    assert build_feed_url(1, "KhgvzGjJ", "asian_handicap_-1_5") is None
    assert build_feed_url(1, "", "1x2") is None
    urls = build_feed_urls(1, "KhgvzGjJ", ("1x2", "asian_handicap_-1_5"))
    assert set(urls) == {"1x2"}


def test_extract_bootstrap_builds_feed_urls_in_pure_python() -> None:
    """Given markets, extract_bootstrap_tokens fills feed_urls from the SSR HTML
    alone — no browser token-mint."""
    tok = extract_bootstrap_tokens(_MATCH_PAGE.read_text(), markets=MARKETS)
    assert set(tok.feed_urls) == set(MARKETS)
    for url in tok.feed_urls.values():
        assert url.startswith("https://www.oddsportal.com/match-event/1-1-KhgvzGjJ-")


# --- list-form (2-way) odds parsing -----------------------------------------


def test_parse_feed_reads_list_form_two_way_odds() -> None:
    """2-way markets (over_under, btts) send odds as a 2-element LIST per bookie,
    not a dict; the parser must read them positionally (the prod blocker that
    silently dropped every list-form bookie)."""
    payload = decrypt_feed_body(_FEED.read_text())
    # the fixture's OU/BTTS blocks are genuinely list-shaped
    assert isinstance(payload["d"]["oddsdata"]["back"]["E-2-2-0-2.5-0"]["odds"]["707"], list)
    assert isinstance(payload["d"]["oddsdata"]["back"]["E-13-2-0-0-0"]["odds"]["707"], list)
    snaps = parse_feed_payload(
        payload,
        event_url=EVENT_URL,
        home="England",
        away="Ghana",
        league="International Friendly",
        starts_at=NOW,
        markets=("over_under_2_5", "btts"),
        directory=EventDirectory(),
        now=NOW,
        bookmakers=REGISTRY,
    )
    ou = {
        (s.bookmaker, s.selection): s.decimal_odds
        for s in snaps
        if s.market_detail == "over_under_2_5"
    }
    assert ou[("BetMGM", "Over 2.5")] == 1.90  # id 707
    assert ou[("BetMGM", "Under 2.5")] == 1.90
    assert ou[("BetUK", "Under 2.5")] == 1.85  # id 263
    btts = {(s.bookmaker, s.selection): s.decimal_odds for s in snaps if s.market_detail == "btts"}
    assert btts[("BetMGM", "BTTS Yes")] == 2.10
    assert btts[("BetMGM", "BTTS No")] == 1.70


def test_feed_market_map_uses_live_verified_keys() -> None:
    """Guard against silently reverting to the empty-feed hypothesised keys
    (E-3 BTTS, E-12 DC, OU line in last segment)."""
    assert _FEED_MARKETS["over_under_2_5"].feed_key == "E-2-2-0-2.5-0"
    assert _FEED_MARKETS["btts"].feed_key == "E-13-2-0-0-0"
    assert _FEED_MARKETS["double_chance"].feed_key == "E-4-2-0-0-0"
    assert _FEED_MARKETS["1x2"].feed_key == "E-1-2-0-0-0"


# --- decrypt-constants version guard ----------------------------------------


def test_key_fingerprint_guard_fails_closed_on_constant_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the static KDF constants are edited so the derived key drifts, decrypt
    fails CLOSED with a loud RuntimeError naming the bundle — never silently
    decrypts to garbage (the bundle-rotation version guard)."""
    import app.ingestion.oddsportal_json as mod

    monkeypatch.setattr(mod, "_KDF_PASSPHRASE", "tampered-passphrase")
    with pytest.raises(RuntimeError, match="decrypt key drifted"):
        mod.decrypt_feed_body(_FEED.read_text())


def test_well_formed_envelope_decrypt_failure_is_rotation_signal() -> None:
    """A WELL-FORMED ct:iv envelope that won't decrypt raises FeedDecryptError
    (the rotation signal), distinct from the benign off-window FeedOffWindow."""
    import base64

    # Valid ct:iv shape but the ciphertext is junk -> AES/unpad fails.
    bad_ct = base64.b64encode(b"not-real-ciphertext-bytes-xx").decode()
    body = base64.b64encode(f"{bad_ct}:00112233445566778899aabbccddeeff".encode()).decode()
    with pytest.raises(FeedDecryptError):
        decrypt_feed_body(body)


def test_off_window_envelope_raises_feed_off_window() -> None:
    """The short off-window body (no ct:iv) raises FeedOffWindow (a ValueError
    subclass), so callers keep treating it as a no-odds gap."""
    import base64

    body = base64.b64encode(b"no-colon-here").decode()
    with pytest.raises(FeedOffWindow):
        decrypt_feed_body(body)
    # both subclass ValueError so legacy `except ValueError` still catches them
    assert issubclass(FeedOffWindow, ValueError)
    assert issubclass(FeedDecryptError, ValueError)


# --- full pure-Python end-to-end scrape -------------------------------------


async def test_scrape_match_odds_end_to_end_pure_python() -> None:
    """The full flow with NO browser: GET match HTML -> bootstrap + build feed
    URLs in pure Python -> translate ids via the STATIC bookmaker map (no GET) ->
    GET + decrypt + parse every market's feed. All GET; every bookmaker is a
    canonical NAME. The bookmaker registry is STATIC since 2026-06-24, so there is
    NO bundle GET — one fewer round-trip per cycle than the old bundle path."""
    html = _MATCH_PAGE.read_text()
    feed = _FEED.read_text()

    class _Sess:
        def __init__(self) -> None:
            self.requests: list[str] = []

        async def get(self, url: str, **kwargs: object) -> _FakeResponse:
            self.requests.append(url)
            if "match-event/" in url:
                return _FakeResponse(text=feed)
            return _FakeResponse(text=html)  # the match page

    session = _Sess()
    directory = EventDirectory()
    snaps = await scrape_match_odds(
        EVENT_URL,
        markets=MARKETS,
        directory=directory,
        now=NOW,
        session=session,
    )
    # 1 HTML + 4 feed GETs = 5 small page-loads (the static id->name map needs NO
    # bundle GET; vs Playwright's full per-match DOM render of every market tab).
    assert len(session.requests) == 5
    assert not any("bookies-" in u for u in session.requests)  # no bundle fetch
    by_market: dict[str, int] = {}
    for s in snaps:
        assert s.market_detail is not None
        assert not s.bookmaker.isdigit()  # canonical NAME, never a numeric id
        by_market[s.market_detail] = by_market.get(s.market_detail, 0) + 1
    assert by_market == {"1x2": 18, "over_under_2_5": 6, "btts": 4, "double_chance": 6}
    # canonical names resolved (id 707 -> BetMGM, 44 -> Betfair Exchange).
    assert any(s.bookmaker == "BetMGM" for s in snaps)
    assert any(s.bookmaker == "Betfair Exchange" for s in snaps)
    # event identity is the navigable match URL; teams registered.
    assert all(s.event_id == EVENT_URL for s in snaps)
    assert directory.lookup(EVENT_URL) is not None


async def test_scrape_match_odds_non_200_html_is_gap() -> None:
    """A non-200 match page is a scrape gap (no rows, no crash) — no feed GET."""

    class _Sess:
        def __init__(self) -> None:
            self.requests: list[str] = []

        async def get(self, url: str, **kwargs: object) -> _FakeResponse:
            self.requests.append(url)
            return _FakeResponse(status_code=403, text="")

    session = _Sess()
    snaps = await scrape_match_odds(
        EVENT_URL,
        markets=MARKETS,
        directory=EventDirectory(),
        now=NOW,
        session=session,
    )
    assert snaps == []
    assert len(session.requests) == 1  # only the HTML GET, no feed GETs
