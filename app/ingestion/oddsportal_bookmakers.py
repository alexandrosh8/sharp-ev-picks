"""Static OddsPortal bookmaker ID -> canonical NAME map (BLOCKER fix 2026-06-24).

WHY STATIC (root-cause investigation 2026-06-24, live curl_cffi through the prod
proxy pool):
  The JSON migration's `BookmakerRegistry` expected a versioned
  ``/res/x/bookies-<ts>.js`` bundle assigning ``var bookmakersData = {...}`` and
  read the id->``WebName`` map from it. That mechanism NO LONGER EXISTS. OddsPortal
  migrated to a Vite/React SSR build (`/build/assets/app-*.js`):
    * the raw match-page / listing HTML contains NO ``bookies-*.js`` reference
      (0 hits live) — so the bundle-URL extraction always misses and the registry
      resolves EMPTY, skipping every soft book (the reported live failure);
    * the app bundle has NO ``bookmakersData`` literal (the id->name object is not
      statically present anywhere a GET can reach it);
    * the names are pulled at runtime into lazy React chunks; the only id-bearing
      endpoints are ``/serve/bookmaker/<id>/`` (a PNG logo, name-less) and the
      geo-gated ``/bookmaker/`` directory page;
    * the decrypted odds feed itself keys odds purely by numeric id
      (``providersPriority`` / ``oddsdata.back[...].odds[<id>]``) and carries NO
      names.
  There is therefore NO stable curl_cffi-fetchable JSON resource exposing the full
  id->name map. The robust fix the task selected is a STATIC cached map shipped in
  the repo (it also removes a per-cycle bundle GET — strictly faster).

PROVENANCE OF THESE PAIRS (authoritative, not guessed):
  * The id->name pairs below were read LIVE from the SSR ``/bookmaker/`` directory
    page, which renders ``<img src="/serve/bookmaker/<id>/" ... alt="<NAME>">`` for
    every book in the proxy's geo (the prod proxy pool exits GB -> the UK book set).
  * EACH NAME WAS CROSS-VERIFIED against the spellings the proven Playwright odds
    path already stores in ``odds_snapshots.bookmaker`` (e.g. ``bet365`` lowercase,
    ``Betano.uk``, ``Unibetuk``, ``BetUK``, ``Betfair Exchange``) — the EXACT
    strings the value engine's sharp/soft classification, consensus median, CLV
    join and devig grouping match on. We read them verbatim; we never normalise or
    invent a spelling.
  * Across many live feeds fetched through the prod proxy pool, the union of feed
    bookie ids was EXACTLY these 19 — i.e. this map fully covers what the live feed
    returns in production. Any id outside it (another geo's book) is unknown ->
    SKIPPED (a logged scrape gap), exactly the pre-existing contract; it is NEVER
    emitted as a numeric bookmaker (that silently disables the value engine).

ROTATION / MAINTENANCE:
  OddsPortal's id->name assignments are append-only and very stable (ids do not get
  reassigned). If a NEW book appears in feeds (logged as an unknown-id skip by the
  loader), add its ``<id>: "<WebName>"`` pair here after reading it from the live
  ``/bookmaker/`` directory page through a proxy of the relevant geo AND confirming
  the spelling against what the Playwright path stores. NEVER add an id whose name
  cannot be sourced that way — an unknown id must skip, not be guessed (project rule
  "unmatched names are logged and quarantined, not guessed").
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# id -> canonical bookmaker name (the exact spelling the Playwright path emits and
# downstream matches on). Live-verified + DB-cross-checked 2026-06-24. Wrapped in a
# read-only MappingProxyType so a caller can never mutate the shared registry.
_STATIC_BOOKMAKERS: dict[str, str] = {
    "2": "bwin",
    "14": "10bet",
    "15": "William Hill",
    "16": "bet365",
    "21": "Betfred",
    "26": "Betway",
    "27": "888sport",
    "44": "Betfair Exchange",
    "60": "Paddy Power",
    "74": "Skybet",
    "76": "BetVictor",
    "263": "BetUK",
    "625": "Unibetuk",
    "707": "BetMGM",
    "841": "Midnite",
    "869": "AllBritishCasino",
    "895": "7BetUK",
    "979": "Betano.uk",
    "1009": "SpreadEX",
}

#: The shipped, read-only id->name map. Keys are the feed's numeric provider ids as
#: strings (matching how the decrypted feed keys odds); values are the canonical
#: names. Read-only so the cycle-shared registry can hand it out without a defensive
#: copy and no caller can corrupt it.
STATIC_BOOKMAKERS: Mapping[str, str] = MappingProxyType(_STATIC_BOOKMAKERS)


def static_bookmaker_map() -> Mapping[str, str]:
    """Return the shipped, read-only id->canonical-NAME map.

    A single source of truth for the curl_cffi JSON feed path: the numeric provider
    ids the decrypted feed uses are translated to the exact bookmaker names the rest
    of the pipeline matches on. Unknown ids are NOT here -> the caller skips them
    (logged scrape gap), never emitting a numeric bookmaker."""
    return STATIC_BOOKMAKERS
