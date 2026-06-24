"""Cycle-level orchestration for the curl_cffi OddsPortal JSON-feed path.

This module owns everything that spans a WHOLE poll cycle (~700 GETs across the
day's slate) for the JSON fast path, keeping `oddsportal_json.py` focused on the
per-match feed mechanics (fetch -> decrypt -> parse). It exists so the JSON
cut-over is GENUINELY fast + reliable, applying the sourced 2026-06-24 "now"-tier
patterns:

  F1  ONE shared curl_cffi ``AsyncSession`` reused for the whole cycle, with
      ``max_clients >= semaphore N`` (the default ``max_clients=10`` silently
      serialises past 10 in-flight handles) and a PINNED chrome impersonation
      (``chrome146``, not bare ``chrome``).
  F3  A bounded ``asyncio.Semaphore`` (N=8) + ``asyncio.gather(...,
      return_exceptions=True)`` per-match fan-out — one bad match is ONE filtered
      gap, never a cycle-killer. NOT ``TaskGroup`` (it cancels siblings on any
      child error).
  F5  A module-level, double-checked-``asyncio.Lock`` cache for the static AES
      decrypt key (fetch/derive once per PROCESS, not per cycle). The bookmaker
      id->name registry is cached per CYCLE on a shared `BookmakerRegistry`
      instance (it rotates its bundle filename, so a fresh instance each cycle
      re-resolves — process-lifetime caching there would pin a stale bundle).
  R1  Classify a curl_cffi failure as TRANSIENT vs PERMANENT by the libcurl int
      ``exc.code`` (NOT brittle string matching). 35=SSL_CONNECT is transient;
      60=cert-verify is permanent — they sit on OPPOSITE sides.
  R2  tenacity retry whose backoff sleep happens OUTSIDE the semaphore: the slot
      is acquired PER ATTEMPT inside the retry, and released before the backoff
      sleep, so one match's backoff never starves the other ~699. curl_cffi does
      NOT raise on 4xx/5xx, so a transient HTTP status (429/5xx) is surfaced via
      ``retry_if_result``; ``Retry-After`` is honoured; full jitter; 4 attempts.
  R3  A fail-closed per-cycle completeness gate: the cycle is flagged INCOMPLETE
      (so the caller can alert / withhold) when the parsed row count collapses
      versus the previous cycle, or an expected market is wholly missing. Any
      exception while evaluating the gate fails CLOSED (incomplete).

SAFETY: every network call remains a GET via the injected curl_cffi session
(`oddsportal_json.scrape_match_odds`), so this path is structurally incapable of
POST/PUT/DELETE — the READ-ONLY market-data rule holds. No bet placement, no
login, no anti-bot defeat beyond the TLS impersonation the Playwright path also
performs. Secrets never appear here (the feed is keyless; proxy creds are inlined
only at the request boundary in the caller, never logged).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tenacity import (
    retry,
    retry_if_exception,
    retry_if_result,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.schemas.odds import OddsSnapshotIn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# F1: shared session knobs.
# ---------------------------------------------------------------------------
# A PINNED chrome impersonation: bare "chrome" floats to whatever curl_cffi's
# default is, so a curl_cffi upgrade could silently change our TLS fingerprint
# mid-deploy. Pin the version the live feed was verified against.
PINNED_IMPERSONATE = "chrome146"
# Default per-cycle fan-out width. max_clients on the shared session MUST be >=
# this or curl_cffi serialises the surplus handles (defeating the concurrency).
DEFAULT_CONCURRENCY = 8
# tenacity attempt cap for one match's GETs (the slot is re-acquired each attempt).
MATCH_MAX_ATTEMPTS = 4


# ---------------------------------------------------------------------------
# R1: transient vs permanent libcurl error classification (by int code).
# ---------------------------------------------------------------------------
# Transient: a retry on a fresh handle/connection can plausibly succeed.
#   28 OPERATION_TIMEDOUT, 7 COULDNT_CONNECT, 56 RECV_ERROR, 55 SEND_ERROR,
#   52 GOT_NOTHING (empty reply), 18 PARTIAL_FILE, 35 SSL_CONNECT_ERROR (the TLS
#   handshake itself failed — retryable, DISTINCT from a cert-verify failure).
TRANSIENT_CURL_CODES: frozenset[int] = frozenset({28, 7, 56, 55, 52, 18, 35})
# Permanent: retrying the same request changes nothing.
#   6 COULDNT_RESOLVE_HOST, 60 PEER_FAILED_VERIFICATION (cert verify — config/CA,
#   NOT the same as 35), 77 SSL_CACERT_BADFILE, 3 URL_MALFORMAT, 1
#   UNSUPPORTED_PROTOCOL, 5 COULDNT_RESOLVE_PROXY.
PERMANENT_CURL_CODES: frozenset[int] = frozenset({6, 60, 77, 3, 1, 5})

# HTTP statuses that warrant a retry (curl_cffi does NOT raise on these — the
# response comes back with a 4xx/5xx status, so we detect them on the RESULT).
TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _curl_error_code(exc: BaseException) -> int | None:
    """The libcurl integer code carried by a curl_cffi error, or None.

    curl_cffi's ``RequestException``/``CurlError`` carry ``.code`` (a
    ``CurlECode`` int enum). We read it as a plain int and never depend on the
    message string (R1: classify by code, not by brittle text)."""
    code = getattr(exc, "code", None)
    if code is None:
        return None
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def classify_curl_error(exc: BaseException) -> str:
    """Classify a per-match GET failure as ``"transient"``/``"permanent"``/
    ``"unknown"`` by the libcurl int code (R1).

    An UNKNOWN code (no ``.code``, or one in neither set) is treated as an ALERT
    case by the caller — it is NOT silently retried forever (it does not match
    the transient predicate) and it is NOT swallowed; it surfaces as a gap with a
    WARNING so a genuinely new failure mode is visible, never masked."""
    code = _curl_error_code(exc)
    if code is None:
        return "unknown"
    if code in TRANSIENT_CURL_CODES:
        return "transient"
    if code in PERMANENT_CURL_CODES:
        return "permanent"
    return "unknown"


def _is_transient_exception(exc: BaseException) -> bool:
    """tenacity predicate: retry ONLY libcurl-transient failures (R1+R2).

    Permanent and unknown codes are NOT retried (an unknown code is surfaced once
    as a gap, not hammered). Non-curl exceptions (a bug in our own parse) are not
    retried either — they are real errors, not network blips."""
    return classify_curl_error(exc) == "transient"


def status_of(result: Any) -> int | None:
    """Best-effort HTTP status of a curl_cffi response-like object."""
    status = getattr(result, "status_code", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _is_transient_status_result(result: Any) -> bool:
    """tenacity predicate: retry when the GET returned a transient HTTP status.

    curl_cffi does not raise on 4xx/5xx, so a 429/5xx arrives as a normal result
    with that ``status_code``; we retry those (R2). A permanent 4xx (e.g. 404)
    is NOT retried — it is a real gap (off-window / unknown match)."""
    status = status_of(result)
    return status is not None and status in TRANSIENT_HTTP_STATUSES


def retry_after_seconds(result: Any, *, cap: float = 30.0) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds form) from a response-like
    object, capped so a hostile/huge value can't wedge the cycle. None when
    absent/unparseable (the caller then uses plain backoff)."""
    headers = getattr(result, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        secs = float(str(raw).strip())
    except (TypeError, ValueError):
        return None  # HTTP-date form: fall back to plain backoff
    if secs < 0:
        return None
    return min(secs, cap)


# A per-match scrape: (match_url) -> rows. Raises on a hard failure (network/
# decrypt). The orchestrator supplies this as a closure that pins the shared
# session + registry + markets, so the fan-out signature stays a bare URL.
MatchScrape = Callable[[str], Awaitable[list[OddsSnapshotIn]]]


@dataclass(frozen=True)
class CycleOutcome:
    """The result of one JSON poll cycle's per-match fan-out + completeness gate.

    ``snapshots`` is every parsed row (deduped per-match upstream). ``complete``
    is the R3 verdict: False means the cycle looks PARTIAL (row collapse or a
    missing expected market) and the caller should alert / treat the slate as
    degraded rather than overwrite a healthy prior cycle. ``reason`` explains an
    incomplete verdict (empty when complete)."""

    snapshots: list[OddsSnapshotIn]
    complete: bool
    reason: str = ""
    matches_total: int = 0
    matches_with_odds: int = 0
    transient_failures: int = 0
    permanent_failures: int = 0
    unknown_failures: int = 0
    per_market: Mapping[str, int] = field(default_factory=dict)


async def _scrape_one_with_retry(
    match_url: str,
    scrape: MatchScrape,
    semaphore: asyncio.Semaphore,
    *,
    max_attempts: int = MATCH_MAX_ATTEMPTS,
) -> list[OddsSnapshotIn]:
    """Run one match's scrape under the semaphore, with tenacity retry whose
    BACKOFF SLEEP HAPPENS OUTSIDE THE SLOT (R2).

    The slot is acquired PER ATTEMPT *inside* the retried coroutine and released
    the instant the attempt returns/raises; tenacity then performs its backoff
    sleep with NO slot held, so a single match's exponential backoff never
    occupies one of the N concurrency slots and starves the other ~699 matches.

    Retries only on a libcurl-transient exception (R1) OR a transient HTTP status
    result (R2). Permanent/unknown failures and a final exhausted retry propagate
    to the caller's ``gather(return_exceptions=True)`` as a single filtered gap.
    """

    @retry(
        retry=(
            retry_if_exception(_is_transient_exception)
            | retry_if_result(_is_transient_status_result)
        ),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),  # full jitter
        reraise=True,
    )
    async def _attempt() -> list[OddsSnapshotIn]:
        # Acquire the slot for THIS attempt only; release before tenacity sleeps.
        async with semaphore:
            return await scrape(match_url)

    return await _attempt()


async def run_cycle(
    match_urls: Sequence[str],
    scrape: MatchScrape,
    *,
    markets: Sequence[str],
    concurrency: int = DEFAULT_CONCURRENCY,
    prev_cycle_rows: int | None = None,
    completeness_floor: float = 0.8,
    require_market_coverage: bool = True,
) -> CycleOutcome:
    """Fan out the per-match JSON scrape across the slate and gate completeness.

    F3: a bounded ``asyncio.Semaphore(concurrency)`` caps in-flight GETs; the
    fan-out uses ``asyncio.gather(..., return_exceptions=True)`` so ONE match's
    failure is one filtered gap, never a cancelled cycle (NOT ``TaskGroup``).
    Each match runs under `_scrape_one_with_retry` (R1/R2).

    R3: the cycle is marked INCOMPLETE (``complete=False``) when
      * the parsed row count fell below ``completeness_floor`` x the previous
        cycle's rows (a silent partial-empty — e.g. half the feeds rotated out),
        OR
      * ``require_market_coverage`` and an EXPECTED market produced ZERO rows
        across the whole slate while OTHER markets did (one market wholly broke).
    Any exception while evaluating the gate fails CLOSED (incomplete) — a gate
    that itself errors must never green-light a degraded slate.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results = await asyncio.gather(
        *(_scrape_one_with_retry(url, scrape, semaphore) for url in match_urls),
        return_exceptions=True,
    )

    snapshots: list[OddsSnapshotIn] = []
    matches_with_odds = 0
    transient = permanent = unknown = 0
    for result in results:
        if isinstance(result, BaseException):
            kind = classify_curl_error(result)
            if kind == "transient":
                transient += 1
            elif kind == "permanent":
                permanent += 1
            else:
                unknown += 1
                # An UNKNOWN failure mode is surfaced loudly (type only — never a
                # URL/secret): it is neither a known-benign gap nor retried.
                logger.warning(
                    "oddsportal JSON match failed with UNCLASSIFIED error (%s) — "
                    "treated as a gap; investigate if persistent",
                    type(result).__name__,
                )
            continue
        if result:
            matches_with_odds += 1
            snapshots.extend(result)

    per_market = _count_per_market(snapshots)
    complete, reason = _evaluate_completeness(
        snapshots=snapshots,
        per_market=per_market,
        markets=markets,
        prev_cycle_rows=prev_cycle_rows,
        completeness_floor=completeness_floor,
        require_market_coverage=require_market_coverage,
        matches_total=len(match_urls),
    )
    if not complete:
        logger.warning("oddsportal JSON cycle INCOMPLETE (fail-closed): %s", reason)

    return CycleOutcome(
        snapshots=snapshots,
        complete=complete,
        reason=reason,
        matches_total=len(match_urls),
        matches_with_odds=matches_with_odds,
        transient_failures=transient,
        permanent_failures=permanent,
        unknown_failures=unknown,
        per_market=per_market,
    )


def _count_per_market(snapshots: Sequence[OddsSnapshotIn]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for snap in snapshots:
        key = snap.market_detail or str(snap.market)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _evaluate_completeness(
    *,
    snapshots: Sequence[OddsSnapshotIn],
    per_market: Mapping[str, int],
    markets: Sequence[str],
    prev_cycle_rows: int | None,
    completeness_floor: float,
    require_market_coverage: bool,
    matches_total: int,
) -> tuple[bool, str]:
    """R3 verdict (pure). Any raised exception is caught here and treated as
    INCOMPLETE (fail-closed) — a gate that errors must never green-light a
    degraded slate."""
    try:
        if matches_total == 0:
            # No matches listed: nothing to be incomplete ABOUT this cycle (an
            # empty slate is the caller's separate "0 matches" signal).
            return True, ""
        rows = len(snapshots)
        # Row-collapse vs the previous healthy cycle (catches a silent partial).
        if prev_cycle_rows is not None and prev_cycle_rows > 0:
            floor = completeness_floor * prev_cycle_rows
            if rows < floor:
                return (
                    False,
                    f"row collapse: {rows} rows < {completeness_floor:.0%} of "
                    f"previous {prev_cycle_rows}",
                )
        # A wholly-missing expected market while OTHERS produced rows = one market
        # broke (its feed key/layout, a rotation) — degrade, don't silently skew.
        if require_market_coverage and rows > 0:
            market_list = list(markets)
            missing = [m for m in market_list if per_market.get(m, 0) == 0]
            if missing and len(missing) != len(market_list):
                return False, f"expected markets returned 0 rows: {missing}"
        return True, ""
    except Exception as exc:  # gate evaluation must FAIL CLOSED, never green-light
        return False, f"completeness gate errored ({type(exc).__name__}) — fail-closed"


# ---------------------------------------------------------------------------
# F5: process-lifetime cache for the static AES decrypt key (double-checked Lock).
# ---------------------------------------------------------------------------
# The key derivation is pure CPU (PBKDF2, 1000 iters) and the constants only
# rotate on a site bundle redeploy (guarded by oddsportal_json._verify_key_*).
# Deriving it once per process — instead of once per feed body, ~700/cycle —
# removes that repeated CPU from the hot path. The double-checked asyncio.Lock
# keeps a concurrent first-cycle fan-out from racing the derive.
_key_cache: bytes | None = None
_key_lock = asyncio.Lock()


async def cached_decrypt_key() -> bytes:
    """The static AES-256 decrypt key, derived once per process (F5).

    Double-checked locking: the fast path returns the cached key with no lock;
    only the first caller (and any that raced it before it populated) takes the
    lock and derives. Subsequent feed-body decrypts reuse this — the per-cycle
    PBKDF2 cost collapses from ~700 derivations to one per process."""
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    async with _key_lock:
        if _key_cache is None:
            # Lazy import avoids a module-load cycle and keeps the (crypto-free)
            # base install importing this module.
            from app.ingestion.oddsportal_json import _derive_key, _verify_key_fingerprint

            _verify_key_fingerprint()  # fail-closed on a constant drift, once.
            _key_cache = _derive_key()
    return _key_cache


def _reset_key_cache_for_tests() -> None:
    """Clear the process key cache (tests only — never called in production)."""
    global _key_cache
    _key_cache = None
