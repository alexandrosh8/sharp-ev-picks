"""ADDITIVE curl_cffi OddsPortal JSON-feed ingester (fast path, OFF by default).

This module fetches OddsPortal's encrypted odds JSON feed *directly* over HTTPS
with browser-TLS impersonation (curl_cffi), decrypts it in pure Python, and
adapts the result into the SAME `OddsSnapshotIn` rows + `EventDirectory`
registrations the Playwright adapter yields (`app/ingestion/oddsportal.py`).
It is the slow-DOM-scrape replacement proven viable in the 2026-06-22 spike.

SAFETY / SCOPE (project HARD RULES):
  * READ-ONLY. Every network call is a GET of the SAME public feed a browser
    reads. No login, no credentials, no bet placement, no anti-bot defeat
    beyond the TLS impersonation the Playwright path already performs via UA /
    locale spoofing.
  * ADDITIVE. Nothing here is wired into `app/scheduler.py`. The proven
    Playwright path in `app/ingestion/oddsportal.py` is untouched. A real
    cut-over is a later, separate change (and must `uv add curl_cffi
    cryptography`; the spike installed them via `uv pip` only).

CANONICAL VOCABULARY REUSE: market->Market mapping, the odds-label->selection
names, line parsing, decimal-odds parsing, the in-play URL-fork collapse and
market validation are imported verbatim from `app/ingestion/oddsportal.py`, so
this path can never drift from the Playwright contract.

FEED MECHANICS (verified live 2026-06-22; market layout + URL re-verified by the
live curl_cffi-vs-DB speed-pass 2026-06-23):
  * Feed URL: /match-event/1-{sportId}-{eventId}-{betTypeId}-{scopeId}-{md5}.dat
    ?geo={geo}&lang={lang}
  * Response body is ENCRYPTED: base64 of "{ct_b64}:{iv_hex}". Decrypt =
    AES-256-CBC under a STATIC PBKDF2 key (compiled into the public bundle),
    optional gzip, UTF-8 -> JSON.
  * Odds live at d.oddsdata.back["E-{betType}-{scope}-..."].odds[bookieId].
    3-way markets (1x2, double_chance) key by outcome index ({"0":..,"1":..,
    "2":..}); 2-way markets (over_under, btts) send a 2-element LIST. Both are
    handled by `_outcome_at`.
  * The 32-hex feed-URL md5 (dataTagMD5) is COSMETIC: the server routes purely
    on the 1-{sportId}-{eventId}-{betType}-{scope} prefix and ignores the hash
    (live-verified, 72/72 requests 200). So `build_feed_url` computes it in PURE
    PYTHON (hashlib.md5) — no browser, no token provider. The whole flow
    (`scrape_match_odds`) is GET-only curl_cffi: HTML -> bootstrap -> build URLs
    -> fetch+decrypt+parse. The decrypt constants are guarded by
    `_verify_key_fingerprint` (fail-closed on a bundle rotation).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Protocol
from urllib.parse import unquote

from app.ingestion.base import EventDirectory, EventTeams
from app.ingestion.oddsportal import (
    _coerce_finished,
    _market_for_key,
    _parse_odds,
    _parse_score,
    _parse_ts,
    _selections,
    normalize_match_link,
)
from app.schemas.odds import OddsSnapshotIn

logger = logging.getLogger(__name__)


class FeedOffWindow(ValueError):
    """The feed body is the short alternate envelope a finished / empty match
    serves (no ``ct:iv`` separator) — there are simply no odds to read. EXPECTED;
    the caller treats it as a no-odds scrape gap, exactly like a Playwright gap.
    """


class FeedDecryptError(ValueError):
    """A WELL-FORMED ``ct:iv`` envelope failed to decrypt / parse. This is the
    bundle-rotation signal: the server returned a 200 odds body but the static
    AES key/format no longer fits it -> the public bundle likely rotated its
    constants. The caller logs this LOUDLY (re-scrape the app.js constants),
    distinct from the benign off-window case. Fail-closed: no rows, never a
    silently-wrong price."""


# Browser-TLS fingerprint for curl_cffi. Same INTENT as the Playwright path's
# UA/locale spoofing — present a coherent human fingerprint, never defeat
# anti-bot beyond TLS impersonation. GET-only; no cookies, no login.
# PINNED to a fixed chrome version (F1): bare "chrome" floats to curl_cffi's
# rolling default, so an upgrade could silently change our TLS fingerprint
# mid-deploy. Kept in lockstep with oddsportal_json_session.PINNED_IMPERSONATE.
_IMPERSONATE = "chrome146"
_FEED_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json,text/plain,*/*",
}

# --- Static decrypt constants (compiled into OddsPortal's public JS bundle) ---
# These rotate only on a site bundle redeploy; a real migration re-scrapes them
# behind a version guard, exactly like oddsportal._patch_upstream_quirks pins
# the OddsHarvester version. PBKDF2-HMAC-SHA256, 1000 iters, 32-byte key.
_KDF_PASSPHRASE = "J*8sQ!p$7aD_fR2yW@gHn*3bVp#sAdLd_k"
_KDF_SALT = b"5b9a8f2c3e6d1a4b7c8e9d0f1a2b3c4d"
_KDF_ITERATIONS = 1000
_KDF_DKLEN = 32

# The bundle these constants were lifted from (content-hashed filename). Bump
# this string in lockstep with the constants whenever a redeploy rotates them —
# it is the human-readable companion to the fingerprint guard below, mirroring
# oddsportal._PATCHED_UPSTREAM_VERSION.
_DECRYPT_BUNDLE_VERSION = "app-BIq1dU23.js"


@lru_cache(maxsize=4)
def _pbkdf2(passphrase: str, salt: bytes, iterations: int, dklen: int) -> bytes:
    """Memoized PBKDF2 keyed ON ITS INPUTS (F5).

    Caching on the constants — not unconditionally — means a CHANGE to any KDF
    constant (a real bundle rotation, or a test/monkeypatch tampering with
    `_KDF_PASSPHRASE`) produces a DIFFERENT cache key and re-derives, so the
    fingerprint tamper-guard still fires. Unchanged constants (the steady state)
    hit the cache, so the ~1000-iter derive runs ONCE per process instead of once
    per feed body (~700/cycle)."""
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, iterations, dklen)


def _derive_key() -> bytes:
    """The static AES-256 key (PBKDF2 of the public-bundle constants above).
    Verified to decrypt the live feed to valid JSON (verify 2026-06-22).

    Reads the current module constants and delegates to the input-keyed
    `_pbkdf2` cache, so it stays sensitive to a constant edit (the tamper guard
    keeps working) while paying the PBKDF2 cost only once per distinct constant
    set. The async `oddsportal_json_session.cached_decrypt_key` is the documented
    entry that pairs this with the fingerprint check."""
    return _pbkdf2(_KDF_PASSPHRASE, _KDF_SALT, _KDF_ITERATIONS, _KDF_DKLEN)


# Tamper fingerprint: a one-way SHA-256 of the derived key, truncated to a short
# prefix. This is NOT key material (it's a hash of a hash, irreversible) — it
# exists only so `_verify_key_fingerprint` can detect a constant edit. The full
# derived key is a public-bundle constant (re-derivable by anyone from the KDF
# inputs above); we keep neither it nor its full digest in source.
_EXPECTED_KEY_FP = "76d5d237"


def _key_fingerprint() -> str:
    """Short one-way fingerprint of the derived key (SHA-256, truncated)."""
    return hashlib.sha256(_derive_key()).hexdigest()[:8]


def _verify_key_fingerprint() -> None:
    """Fail CLOSED if the static decrypt constants drifted from the verified set.

    A constant edit (typo, a half-applied bundle-rotation patch) would silently
    change the AES key and yield garbage plaintext. This guard raises a LOUD
    RuntimeError naming the bundle version instead — the static-constant analog
    of oddsportal._patch_upstream_quirks' version check.
    """
    fingerprint = _key_fingerprint()
    if fingerprint != _EXPECTED_KEY_FP:
        raise RuntimeError(
            f"oddsportal decrypt key drifted from {_DECRYPT_BUNDLE_VERSION} "
            f"(fingerprint {fingerprint} != {_EXPECTED_KEY_FP}): the static KDF "
            "constants were edited — re-verify them against the live bundle and "
            "update _EXPECTED_KEY_FP / _DECRYPT_BUNDLE_VERSION"
        )


def decrypt_feed_body(body: str) -> dict[str, Any]:
    """Decrypt one OddsPortal `.dat` feed body to its JSON payload (pure Python).

    Mirrors the deobfuscated bundle fn ``Oco(e)``:
      o = atob(body); [ct_b64, iv_hex] = o.split(":"); IV = bytes.fromhex(iv_hex);
      ct = atob(ct_b64); AES-256-CBC decrypt; strip PKCS#7; optional gunzip;
      UTF-8 -> JSON.parse.

    Raises `FeedOffWindow` on the short "off-window" alternate body (no ``:``
    separator) so the caller treats it as a no-odds match, like a Playwright
    scrape gap. Raises `FeedDecryptError` when a WELL-FORMED ``ct:iv`` envelope
    fails to decrypt/parse — the bundle-rotation signal the caller logs loudly.
    Both subclass ValueError, so existing ``except ValueError`` callers still
    treat every failure as a gap.
    """
    # Imported lazily so the base (curl_cffi/cryptography-free) install and CI
    # profile import this module without the optional deps present.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    # Fail CLOSED if the static KDF constants drifted from the verified set
    # (a constant typo or half-applied bundle-rotation patch would otherwise
    # yield garbage plaintext). Cheap; the derive is pure CPU.
    _verify_key_fingerprint()

    try:
        outer = base64.b64decode(body).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        # Not even base64 text -> the short off-window envelope. Benign gap.
        raise FeedOffWindow(f"feed body is not base64 text: {type(exc).__name__}") from exc
    if ":" not in outer:
        # Short single-layer envelope seen when a match left its post-finish
        # grace window — no odds to read. Treat as empty, like a scrape gap.
        raise FeedOffWindow("feed body carries no ct:iv envelope (off-window / empty match)")

    # From here the envelope is WELL-FORMED (has the ct:iv shape). Any failure
    # now means the static key/format no longer fits a real odds body -> rotation.
    ct_b64, _, iv_hex = outer.rpartition(":")
    try:
        iv = bytes.fromhex(iv_hex)
        ciphertext = base64.b64decode(ct_b64)
        decryptor = Cipher(algorithms.AES(_derive_key()), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        # The plaintext is gzip-compressed in some envelopes (magic 0x1f 0x8b).
        if plaintext[:2] == b"\x1f\x8b":
            plaintext = gzip.decompress(plaintext)
        payload = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, OSError) as exc:
        raise FeedDecryptError(
            f"well-formed feed envelope failed to decrypt against "
            f"{_DECRYPT_BUNDLE_VERSION} ({type(exc).__name__}) — the public "
            "bundle likely rotated its AES key/format; re-scrape the constants"
        ) from exc
    if not isinstance(payload, dict):
        raise FeedDecryptError("decrypted feed payload is not a JSON object")
    return payload


# --- Bookmaker ID -> NAME registry ------------------------------------------
# The decrypted feed keys odds PURELY by numeric provider IDs ("707", "263",
# ...). Every downstream consumer (sharp-anchor classification in
# app/edge/value.py, the consensus median, CLV close-line capture, devig
# grouping, the persistence dedup key) keys on bookmaker NAMES exactly as the
# Playwright path emits them ("Betfair Exchange", "bet365", "BetUK"). So the
# numeric IDs MUST be translated to those canonical names before a snapshot is
# emitted; an UNKNOWN id is SKIPPED (a scrape gap), NEVER persisted as a numeric
# bookmaker (that silently disables the whole value engine — review 2026-06-23).
#
# WHY STATIC (root-cause investigation 2026-06-24, live curl_cffi through the
# prod proxy pool): OddsPortal historically served the registry as a versioned
# `/res/x/bookies-<ts>.js` bundle assigning `var bookmakersData = {...}`. That
# mechanism IS GONE — the site moved to a Vite/React SSR build (`/build/assets/
# app-*.js`). LIVE FACTS:
#   * the raw match-page / listing HTML carries NO `bookies-*.js` reference
#     (0 hits) — so the old bundle-URL extraction ALWAYS missed and the registry
#     resolved EMPTY, skipping every soft book (the reported live failure);
#   * the app bundle has NO `bookmakersData` literal (the id->name object is not
#     statically present in any GET-reachable resource); names load at runtime
#     into lazy React chunks; the only id-bearing endpoints are
#     `/serve/bookmaker/<id>/` (a name-less PNG logo) and the geo-gated
#     `/bookmaker/` directory page.
# So there is NO stable curl_cffi-fetchable JSON map. We therefore translate via
# a STATIC, live-verified, DB-cross-checked map shipped in the repo
# (`app/ingestion/oddsportal_bookmakers.py`). It removes a per-cycle GET too.


class BookmakerRegistry:
    """Cached resolver of the bookmaker ID->NAME map (STATIC since 2026-06-24).

    A single instance is shared across the loader's per-match scrapes. `resolve`
    / `resolve_from_html` return the shipped STATIC id->name map
    (`oddsportal_bookmakers.static_bookmaker_map`) — NO network GET, because the
    map is no longer in any curl_cffi-fetchable resource (the `bookies-*.js`
    bundle mechanism was removed in OddsPortal's React migration; see the note
    above). The session/html arguments are retained for call-site compatibility
    and to keep this a drop-in for the old fetch-based resolver, but they are not
    used. An id absent from the static map is unknown -> skipped by
    `parse_feed_payload` (a logged scrape gap), never a guessed/numeric bookmaker.
    """

    def __init__(self) -> None:
        self._cache: Mapping[str, str] | None = None

    @property
    def cached(self) -> Mapping[str, str] | None:
        """The cached map (None until the first resolve)."""
        return self._cache

    async def resolve(self, session: _AsyncHTTPSession, *, page_url: str) -> Mapping[str, str]:
        """Return the static id->NAME map (no network).

        `session`/`page_url` are accepted for call-site compatibility with the
        former fetch-based resolver but are unused — the map is static."""
        return self._resolve_static()

    async def resolve_from_html(
        self, session: _AsyncHTTPSession, html: str, *, base_url: str
    ) -> Mapping[str, str]:
        """The variant `scrape_match_odds` uses. Returns the static id->NAME map
        with no GET — the match-page HTML no longer carries a usable bundle URL.
        `session`/`html`/`base_url` are unused (kept for signature compatibility)."""
        return self._resolve_static()

    def _resolve_static(self) -> Mapping[str, str]:
        """Cache + return the shipped static map. Logged once per instance (per
        cycle) at INFO so the resolved book count stays visible in ops logs."""
        if self._cache is not None:
            return self._cache
        from app.ingestion.oddsportal_bookmakers import static_bookmaker_map

        registry = static_bookmaker_map()
        logger.info(
            "oddsportal bookmaker registry resolved from static map: %d books",
            len(registry),
        )
        self._cache = registry
        return self._cache


# --- Feed-market -> OddsHarvester-market-key mapping -------------------------
# Each entry maps an OddsHarvester market key (the vocabulary the rest of the
# app speaks) to (feed_market_key, {outcome_index: odds_label}). The odds_label
# values are EXACTLY the labels `_selections()` keys on, so the readable
# selection names + Market enum come straight from the canonical adapter — no
# parallel vocabulary lives here.
#
# Feed keys + outcome layout are the LIVE-VERIFIED values (curl_cffi decrypted
# feed cross-checked against the Playwright DB rows for the same events, matched
# to 0.01-0.02 — speed-pass 2026-06-23). The earlier hypothesised keys (E-3 for
# BTTS, E-12 for DC, the line in the last segment for OU) returned EMPTY feeds
# in production and were the silent-1x2-only blocker; do NOT revert them without
# a fresh live cross-check.
#
# betType/scope grid: E-{betType}-{scope}-{?}-{lineOrZero}-{?}.
#   betType: 1=1X2, 2=Over/Under, 4=Double Chance, 13=Both Teams To Score.
#   scope 2 = Full Time. The OU line rides the 5th segment ("E-2-2-0-2.5-0").
#
# Outcome SHAPE varies by market arity (handled by parse_feed_payload):
#   3-way (1x2, double_chance): odds[bookie] = {"0":.., "1":.., "2":..} (dict).
#   2-way (over_under, btts):   odds[bookie] = [outcome0, outcome1]      (list).
# The index_to_label keys are the POSITIONAL outcome index as a string ("0",
# "1", "2"); they index a dict by key and a list positionally — same map, both
# shapes.


@dataclass(frozen=True)
class _FeedMarketSpec:
    feed_key: str
    # outcome-index (as the positional string "0"/"1"/"2") -> OddsHarvester
    # odds-label. Works for both dict-form (keyed) and list-form (positional).
    index_to_label: Mapping[str, str]


_FEED_MARKETS: dict[str, _FeedMarketSpec] = {
    # 1x2 (betType 1, 3-way dict): feed idx 0=home,1=away,2=draw -> OH 1 / 2 / X.
    "1x2": _FeedMarketSpec("E-1-2-0-0-0", {"0": "1", "1": "2", "2": "X"}),
    # Over/Under 2.5 goals, FT (betType 2, line in 5th segment, 2-way list):
    # idx 0=over, 1=under.
    "over_under_2_5": _FeedMarketSpec("E-2-2-0-2.5-0", {"0": "odds_over", "1": "odds_under"}),
    # Both teams to score (betType 13, 2-way list): idx 0=yes, 1=no.
    "btts": _FeedMarketSpec("E-13-2-0-0-0", {"0": "btts_yes", "1": "btts_no"}),
    # Double chance (betType 4, 3-way dict): idx 0=1X, 1=12, 2=X2.
    "double_chance": _FeedMarketSpec("E-4-2-0-0-0", {"0": "1X", "1": "12", "2": "X2"}),
}

# The four football markets this fast path supports today (the configured
# `oddsportal_football_markets` default). Other market families (handicaps,
# tennis/basketball axes) keep flowing through the Playwright adapter until
# their feed market keys + index layouts are pinned the same way.
SUPPORTED_FEED_MARKETS: tuple[str, ...] = tuple(_FEED_MARKETS)


def _outcome_at(outcomes: Any, index: str) -> Any:
    """Read one outcome odds from a bookie block, tolerant of BOTH feed shapes.

    The 3-way markets (1x2, double_chance) send a DICT keyed by the positional
    index string (``{"0":.., "1":.., "2":..}``); the 2-way markets (over_under,
    btts) send a 2-element LIST (``[outcome0, outcome1]``). `index` is always the
    positional string ("0"/"1"/"2"). Returns None for an out-of-range / wrong-
    type block so the caller drops that selection (scrape gap), never crashes.
    """
    if isinstance(outcomes, Mapping):
        return outcomes.get(index)
    if isinstance(outcomes, (list, tuple)):
        try:
            pos = int(index)
        except ValueError:
            return None
        if 0 <= pos < len(outcomes):
            return outcomes[pos]
        return None
    return None


def _feed_captured_at(payload: Mapping[str, Any], now: datetime) -> datetime:
    """The feed's provider observation time (``d.time-base``, epoch seconds, UTC)
    as the snapshot `captured_at` — the JSON analog of the Playwright path's
    ``scraped_date`` (oddsportal._convert_match). Falls back to ``now`` when the
    feed omits/garbles it, exactly like the Playwright ``_parse_ts(...) or now``.
    Always tz-aware UTC (a naive datetime is a bug)."""
    raw = (payload.get("d") or {}).get("time-base")
    if raw is None:
        return now
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)
    except (ValueError, OverflowError, OSError, TypeError):
        return now


def parse_feed_payload(
    payload: Mapping[str, Any],
    *,
    event_url: str,
    home: str,
    away: str,
    league: str,
    starts_at: datetime | None,
    markets: Sequence[str],
    directory: EventDirectory,
    now: datetime,
    bookmakers: Mapping[str, str],
    home_score: int | None = None,
    away_score: int | None = None,
    finished: bool | None = None,
) -> list[OddsSnapshotIn]:
    """Adapt a decrypted feed payload into `OddsSnapshotIn` rows.

    Reproduces `oddsportal._convert_match` at the OUTPUT boundary: registers the
    event teams in `directory` (keyed by the normalized match URL — the
    platform-wide event identity) and emits one snapshot per (market, bookmaker,
    selection) with parseable decimal odds > 1.0. Duplicate bookmaker rows
    within a market are dropped (devig protection). `market_detail` carries the
    raw OddsHarvester market key so distinct lines never collapse in devig.

    `bookmakers` maps the feed's numeric provider IDs to canonical bookmaker
    NAMES (the exact spellings the Playwright path emits, e.g. "Pinnacle",
    "Betfair Exchange", "bet365"). An id NOT in the map is SKIPPED — never
    persisted as a numeric bookmaker (that silently breaks the value engine's
    sharp/soft classification, CLV join, and devig grouping). An empty map => no
    rows (a visible scrape gap), never numeric bookmakers.

    `captured_at` is the feed's provider observation time (``d.time-base``),
    matching the Playwright path's ``scraped_date`` semantics (FIX 3).
    """
    home = home.strip()
    away = away.strip()
    if not home or not away:
        return []
    event_id = normalize_match_link(event_url)
    directory.register(
        event_id,
        EventTeams(
            home=home,
            away=away,
            league=league,
            starts_at=starts_at,
            home_score=home_score,
            away_score=away_score,
            finished=finished,
        ),
    )

    back = (((payload.get("d") or {}).get("oddsdata") or {}).get("back")) or {}
    if not isinstance(back, Mapping):
        return []

    captured_at = _feed_captured_at(payload, now)
    snapshots: list[OddsSnapshotIn] = []
    for market_key in markets:
        spec = _FEED_MARKETS.get(market_key)
        market = _market_for_key(market_key)
        if spec is None or market is None:
            # Market not (yet) wired into the feed path — silently skipped so a
            # mixed market list still yields the supported families.
            continue
        block = back.get(spec.feed_key)
        if not isinstance(block, Mapping):
            continue
        odds_by_bookie = block.get("odds")
        if not isinstance(odds_by_bookie, Mapping):
            continue
        # OddsHarvester odds-label -> readable selection name (canonical).
        label_to_selection = dict(_selections(market_key, home, away))

        seen_books: set[str] = set()
        for bookie_id, outcomes in odds_by_bookie.items():
            # Translate the numeric feed id to the canonical book NAME. An
            # UNKNOWN id is a scrape gap — SKIP it, NEVER emit a numeric
            # bookmaker (it would silently disable sharp/soft classification,
            # fork CLV history, and corrupt devig grouping — review 2026-06-23).
            bookmaker = bookmakers.get(str(bookie_id))
            if not bookmaker:
                continue
            # Dedup on the resolved NAME (two ids could map to one book; the
            # Playwright path dedups on name too).
            if bookmaker in seen_books:
                continue
            seen_books.add(bookmaker)
            for index, label in spec.index_to_label.items():
                selection = label_to_selection.get(label)
                if selection is None:
                    continue
                odds = _parse_odds(_outcome_at(outcomes, index))
                if odds is None:
                    continue
                snapshots.append(
                    OddsSnapshotIn(
                        event_id=event_id,
                        bookmaker=bookmaker,
                        market=market,
                        selection=selection,
                        decimal_odds=odds,
                        captured_at=captured_at,
                        ingested_at=now,
                        market_detail=market_key,
                    )
                )
    return snapshots


# ---------------------------------------------------------------------------
# Bootstrap tokens + the feed URL + the feed fetch.
#
# TOKEN APPROACH (verify 2026-06-22 + live speed-pass 2026-06-23):
#   * BOOTSTRAP tokens (eventId, sportId, finished-status, teams) live in the
#     match-page SSR HTML, div#react-event-header data='...JSON...'. The match
#     page returns HTTP 200 to curl_cffi — NO browser required. They are parsed
#     by `extract_bootstrap_tokens`.
#   * The 32-hex feed-URL segment is computed client-side by the bundle's
#     dataTagMD5(). The verify pass HYPOTHESISED its input formula from the JS;
#     the live speed-pass then EMPIRICALLY established it is MOOT: OddsPortal
#     does NOT validate that path segment — it routes purely on the
#     1-{sportId}-{eventId}-{betType}-{scope} prefix, and ANY 32-hex value
#     returns HTTP 200 with the correct body (0 failures across 72 live
#     requests). So the feed URL is built in PURE PYTHON here (`build_feed_url`)
#     with a deterministic hashlib.md5 over a stable per-(event, market) string
#     — for URL stability/caching, NOT for auth. No browser token-mint, no
#     Playwright touch, no FeedTokenProvider indirection.
#   * The DECRYPT key is fully static and never rotates per request (it changes
#     only on a site bundle redeploy -> guarded by `_verify_key_fingerprint`).
# ---------------------------------------------------------------------------

# Host + path template for the encrypted odds feed (verified live).
_FEED_HOST = "https://www.oddsportal.com"
_FEED_PATH_TEMPLATE = "/match-event/1-{sport_id}-{event_id}-{bet_type}-{scope}-{md5}.dat"
# The OddsHarvester market key -> (betTypeId, scopeId) needed to build its feed
# URL. Mirrors the betType/scope grid encoded in _FEED_MARKETS' feed_key. scope
# 2 = Full Time for all four football markets.
_FEED_URL_PARAMS: dict[str, tuple[int, int]] = {
    "1x2": (1, 2),
    "over_under_2_5": (2, 2),
    "btts": (13, 2),
    "double_chance": (4, 2),
}


def build_feed_url(sport_id: int, event_id: str, market_key: str) -> str | None:
    """Build the encrypted-feed URL for one (event, market) in PURE PYTHON.

    OddsPortal routes the feed on the ``1-{sportId}-{eventId}-{betType}-{scope}``
    prefix and IGNORES the trailing 32-hex segment (live-verified: any md5 ->
    HTTP 200 with the correct body). We therefore compute a DETERMINISTIC md5
    over a stable per-(event, market) string so the URL is well-formed and
    cache-stable — this is dataTagMD5's role for us, not authentication.

    Returns None for a market with no known (betType, scope) — the caller skips
    it (scrape gap), never crashes.
    """
    params = _FEED_URL_PARAMS.get(market_key)
    if params is None or not event_id:
        return None
    bet_type, scope = params
    # Stable input -> stable hash for the same (event, market). The exact bytes
    # do not matter to the server; determinism matters to us (caching, logs).
    digest = hashlib.md5(
        f"{sport_id}-{event_id}-{bet_type}-{scope}".encode(), usedforsecurity=False
    ).hexdigest()
    path = _FEED_PATH_TEMPLATE.format(
        sport_id=sport_id, event_id=event_id, bet_type=bet_type, scope=scope, md5=digest
    )
    return f"{_FEED_HOST}{path}"


def build_feed_urls(sport_id: int, event_id: str, markets: Sequence[str]) -> dict[str, str]:
    """Build the pure-Python feed URL for every supported market in `markets`.

    Unsupported / unmappable markets are simply omitted (they keep flowing
    through the Playwright adapter). The result is the `feed_urls` mapping a
    `FeedToken` carries."""
    out: dict[str, str] = {}
    for market_key in markets:
        url = build_feed_url(sport_id, event_id, market_key)
        if url is not None:
            out[market_key] = url
    return out


@dataclass(frozen=True)
class FeedToken:
    """Everything needed to GET + identify one match's odds feed.

    `feed_urls` maps an OddsHarvester market key to the fully-built encrypted
    feed URL (deterministic, pure-Python via `build_feed_urls`). The 32-hex
    segment is cosmetic (server ignores it), so no browser mint is needed."""

    event_id: str
    sport_id: int
    feed_urls: Mapping[str, str]
    xhash: str = ""
    xhashf: str = ""
    home: str = ""
    away: str = ""
    league: str = ""
    starts_at: datetime | None = None
    home_score: int | None = None
    away_score: int | None = None
    finished: bool | None = None
    referer: str = ""  # match page URL, sent as the feed Referer


def _decode_xhash(raw: Any) -> str:
    """OddsPortal stores xhash/xhashf percent-escaped ('%79%6a%64%35%31');
    URL-decode to the usable token ('yjd51'). Non-string -> ''."""
    if not isinstance(raw, str):
        return ""
    return unquote(raw)


def _coerce_kickoff(*candidates: Any) -> datetime | None:
    """Pick the kickoff from the first candidate that is a REAL positive epoch.

    OddsPortal serves the kickoff in ``eventData.startDate`` for some events and
    in ``eventBody.startDate`` for others (live verify 2026-06-24: US lower-league
    fixtures such as "Los Angeles FC 2" carried ``eventData.startDate=None`` but
    ``eventBody.startDate=1782352800``). Both are unix epochs. A falsy/sentinel
    value (``None``, ``False``, ``0``, ``""``) or a non-positive epoch is NOT a
    kickoff — those are skipped so a genuinely-TBD fixture stays ``None`` (never
    an invented time), while ``endDate: False``-style sentinels can't masquerade
    as a start. The first candidate that parses to a tz-aware UTC datetime wins,
    so ``eventData`` stays authoritative when present.
    """
    for raw in candidates:
        # bool is an int subclass — reject True/False explicitly before the
        # numeric guard so `startDate: False` never reads as epoch 0.
        if isinstance(raw, bool) or not raw:
            continue
        if isinstance(raw, (int, float)) and raw <= 0:
            continue
        parsed = _parse_ts(raw)
        if parsed is not None:
            return parsed
    return None


def extract_bootstrap_tokens(html: str, *, markets: Sequence[str] = ()) -> FeedToken:
    """Parse the match-page SSR react-event-header JSON into a `FeedToken`.

    Returns the eventId / sportId / xhash / xhashf / teams / kickoff /
    finished-status PLUS the per-market `feed_urls`, all from the single
    curl_cffi HTML fetch — no browser. When `markets` is given, the feed URLs
    are built in pure Python (`build_feed_urls`) for every supported market; the
    32-hex segment is cosmetic (server ignores it). Raises ValueError if the
    header is absent or unparseable (the page failed to render its bootstrap)."""
    from bs4 import BeautifulSoup

    div = BeautifulSoup(html, "html.parser").find("div", id="react-event-header")
    data = div.get("data") if div is not None else None
    if not isinstance(data, str) or not data:
        raise ValueError("react-event-header bootstrap JSON missing")
    payload = json.loads(data)
    event = payload.get("eventData") or {}
    body = payload.get("eventBody") or {}
    event_id = str(event.get("id") or "")
    if not event_id:
        raise ValueError("bootstrap JSON has no eventData.id")
    sport_id = int(event.get("sportId") or 0)
    # Kickoff lives in eventData.startDate for some events and eventBody.startDate
    # for others (US lower-league fixtures, live verify 2026-06-24); read whichever
    # carries a real positive epoch, eventData first (authoritative when present).
    starts_at = _coerce_kickoff(event.get("startDate"), body.get("startDate"))
    if starts_at is None:
        # Genuinely TBD on OddsPortal — log the gap, never invent a time.
        logger.info(
            "oddsportal bootstrap has no kickoff for event %s (%s vs %s) — TBD",
            event_id,
            event.get("home") or "?",
            event.get("away") or "?",
        )
    return FeedToken(
        event_id=event_id,
        sport_id=sport_id,
        feed_urls=build_feed_urls(sport_id, event_id, markets),
        xhash=_decode_xhash(event.get("xhash")),
        xhashf=_decode_xhash(event.get("xhashf")),
        home=str(event.get("home") or ""),
        away=str(event.get("away") or ""),
        league=str(event.get("tournamentName") or ""),
        starts_at=starts_at,
        home_score=_parse_score(event.get("homeResult")),
        away_score=_parse_score(event.get("awayResult")),
        finished=_coerce_finished(
            event.get("isFinished"),
            body.get("eventStageId"),
            body.get("eventStageName"),
        ),
    )


class _AsyncHTTPSession(Protocol):
    """The slice of curl_cffi.AsyncSession this module uses — GET only. Declared
    as a Protocol so tests inject a no-network fake (project rule: no network in
    tests) and the production session never needs subclassing."""

    async def get(self, url: str, **kwargs: Any) -> Any: ...


async def fetch_match_feed(
    match_url: str,
    *,
    token: FeedToken,
    markets: Sequence[str],
    directory: EventDirectory,
    now: datetime,
    session: _AsyncHTTPSession,
    bookmakers: Mapping[str, str],
    geo: str = "GB",
    lang: str = "en",
) -> list[OddsSnapshotIn]:
    """GET-only: fetch + decrypt + parse one match's odds feed via curl_cffi.

    For each requested market with a known feed URL in `token.feed_urls`, GETs
    the encrypted `.dat` body, decrypts it, and merges its parsed snapshots. The
    event teams are registered ONCE (even if every feed is empty), so the
    /games view + Betfair reader see the fixture exactly like the Playwright
    path. A non-200, an off-window envelope, or a decrypt failure is treated as
    a scrape gap (no rows for that market), NEVER a hard error — matching the
    Playwright path's tolerance of missing sub-markets.

    `bookmakers` is the numeric-id -> canonical-NAME map applied in
    `parse_feed_payload`; an unknown id is skipped, never emitted numeric.

    This function only ever calls ``session.get`` — it is structurally incapable
    of POST/PUT/DELETE, honouring the READ-ONLY market-data safety rule.
    """
    home = token.home.strip()
    away = token.away.strip()
    # Register the event up front so a fully-empty feed still surfaces the
    # fixture (parse_feed_payload also registers; idempotent — same teams).
    if home and away:
        directory.register(
            normalize_match_link(match_url),
            EventTeams(
                home=home,
                away=away,
                league=token.league,
                starts_at=token.starts_at,
                home_score=token.home_score,
                away_score=token.away_score,
                finished=token.finished,
            ),
        )

    headers = dict(_FEED_HEADERS)
    headers["Referer"] = token.referer or match_url

    snapshots: list[OddsSnapshotIn] = []
    for market_key in markets:
        feed_url = token.feed_urls.get(market_key)
        if not feed_url:
            continue  # no URL minted for this market this cycle — scrape gap
        try:
            resp = await session.get(
                feed_url,
                headers=headers,
                impersonate=_IMPERSONATE,
                params={"geo": geo, "lang": lang},
            )
        except Exception as exc:  # network / TLS / timeout -> scrape gap
            logger.warning(
                "oddsportal feed GET failed for market %s (%s) — treating as gap",
                market_key,
                type(exc).__name__,
            )
            continue
        if getattr(resp, "status_code", 0) != 200:
            logger.info(
                "oddsportal feed for market %s returned status %s — gap",
                market_key,
                getattr(resp, "status_code", "?"),
            )
            continue
        try:
            payload = decrypt_feed_body(resp.text)
        except FeedDecryptError as exc:
            # A 200 odds body that WON'T decrypt = the bundle likely rotated its
            # static AES constants. Loud so ops re-scrapes them (version guard).
            logger.warning(
                "oddsportal feed ROTATION suspected for market %s: %s",
                market_key,
                exc,
            )
            continue
        except RuntimeError as exc:
            # The version-guard fail-closed signal (_verify_key_fingerprint): the
            # static KDF constants DRIFTED — a half-applied bundle rotation or an
            # edited constant. With NO Playwright fallback this would otherwise
            # silently empty the JSON-wide slate, so surface it LOUDLY at WARNING
            # naming the bundle (it is NOT a ValueError, so it would otherwise
            # escape to the loader's quiet INFO catch). Fail-closed: no rows.
            logger.warning(
                "oddsportal feed KEY/BUNDLE ROTATION (version guard) for market %s — "
                "JSON feed fail-closed, scrape gap until constants re-verified: %s",
                market_key,
                exc,
            )
            continue
        except ValueError as exc:  # off-window / empty match -> benign gap
            logger.info(
                "oddsportal feed decrypt skipped for market %s (%s)",
                market_key,
                type(exc).__name__,
            )
            continue
        snapshots.extend(
            parse_feed_payload(
                payload,
                event_url=match_url,
                home=token.home,
                away=token.away,
                league=token.league,
                starts_at=token.starts_at,
                markets=(market_key,),
                directory=directory,
                now=now,
                bookmakers=bookmakers,
                home_score=token.home_score,
                away_score=token.away_score,
                finished=token.finished,
            )
        )
    return snapshots


async def scrape_match_odds(
    match_url: str,
    *,
    markets: Sequence[str],
    directory: EventDirectory,
    now: datetime,
    session: _AsyncHTTPSession,
    registry: BookmakerRegistry | None = None,
    geo: str = "GB",
    lang: str = "en",
) -> list[OddsSnapshotIn]:
    """Full PURE-PYTHON end-to-end scrape of one match — no browser, GET-only.

    Flow (each step GET-only via the injected curl_cffi session):
      1. GET the match page HTML (HTTP 200; carries the SSR bootstrap tokens).
      2. `extract_bootstrap_tokens` -> eventId / sportId / teams / kickoff /
         finished-status + the per-market feed URLs built in pure Python
         (`build_feed_urls`; the 32-hex segment is cosmetic / server-ignored).
      3. Resolve the bookmaker id->NAME map (cached on `registry`) from the SAME
         match-page HTML — one extra GET of the static bundle, once per cycle.
      4. `fetch_match_feed` GETs + decrypts + parses each market's encrypted
         `.dat` into `OddsSnapshotIn` rows matching the Playwright contract,
         translating numeric ids to canonical NAMES (unknown id -> skip).

    A missing/unparseable bootstrap header or a non-200 HTML page is a scrape
    gap (returns no rows, never crashes), exactly like a Playwright nav gap.
    This is the slow-DOM-render path collapsed to ~5-6 small GETs per match.

    `registry` is a shared `BookmakerRegistry` (the caller reuses one per cycle so
    the bundle is fetched once); when None a throwaway instance resolves it from
    this match's HTML. A failure to resolve names yields an EMPTY map, so the feed
    parse emits a scrape gap rather than numeric bookmakers.

    Only ever calls ``session.get`` — structurally GET-only (READ-ONLY safety).
    """
    try:
        resp = await session.get(match_url, impersonate=_IMPERSONATE)
    except Exception as exc:  # network / TLS / timeout -> scrape gap
        logger.warning(
            "oddsportal match-page GET failed (%s) — treating as gap: %s",
            type(exc).__name__,
            normalize_match_link(match_url),
        )
        return []
    if getattr(resp, "status_code", 0) != 200:
        logger.info(
            "oddsportal match page returned status %s — gap",
            getattr(resp, "status_code", "?"),
        )
        return []
    html = resp.text
    try:
        token = extract_bootstrap_tokens(html, markets=markets)
    except ValueError as exc:  # bootstrap header absent / unparseable -> gap
        logger.info(
            "oddsportal bootstrap parse skipped (%s) for %s",
            type(exc).__name__,
            normalize_match_link(match_url),
        )
        return []
    # Resolve numeric-id -> canonical book NAME (cached on the shared registry).
    # Read the bundle URL from THIS page's HTML to avoid a second page GET; the
    # bundle itself is the only extra GET, and it's cached across the cycle.
    registry = registry or BookmakerRegistry()
    bookmakers = await registry.resolve_from_html(session, html, base_url=match_url)
    # The match page IS the feed Referer.
    token = replace(token, referer=match_url)
    return await fetch_match_feed(
        match_url,
        token=token,
        markets=markets,
        directory=directory,
        now=now,
        session=session,
        bookmakers=bookmakers,
        geo=geo,
        lang=lang,
    )
