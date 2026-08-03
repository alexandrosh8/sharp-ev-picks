"""STRICTLY READ-ONLY Betfair Exchange API client for market data (odds).

SAFETY (ADR-0002 + Rule 1 read-only exception, operator commit 0e27433):
this module reads PRICES ONLY from the official Betfair Exchange API. It uses
EXACTLY these endpoints and NOTHING else:

  * session:  login / keepAlive / logout  (identitysso)
  * data:     listEventTypes / listCompetitions / listEvents /
              listMarketCatalogue / listMarketBook  (SportsAPING JSON-RPC)

It contains NO bet order-placement method and NO betting-account / order-ledger
method (the exact identifier names are deliberately absent from this source so
the ``scripts/safety_audit.sh`` grep and
``tests/test_betfair_api.py::test_no_order_or_account_methods_in_module`` both
stay empty). The JSON-RPC data calls are POST by protocol, but they are
read-only: they return prices and never mutate anything on Betfair.

Secret hygiene (CLAUDE.md security rules): the App Key, username, password are
held in memory only and are NEVER logged, persisted, or placed in any error
string. The session token (ssoid) lives in memory only and is NEVER written to
disk. Errors carry the operation name + Betfair errorCode / HTTP status only —
never the URL (no query secrets here, but the rule is uniform), never a body.

SHADOW-FIRST (req #2): ``BetfairApiShadowCapture`` fetches the Match-Odds
catalogue, routes each Betfair market through the EXISTING hardened cross-source
matcher (``app.resolution.matching.match_event_hardened`` — reused verbatim,
never re-implemented), and LOGS the match rate + the would-be BACK anchor. It
writes NOTHING and produces rows tagged with a SHADOW bookmaker name that is
deliberately NOT in ``app.edge.value.SHARP_BOOKS`` — so the existing
OddsPortal-sourced "Betfair Exchange" anchor is never replaced. Default-OFF and
fully inert unless explicitly enabled with all credentials present.
"""

from __future__ import annotations

import inspect
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.edge.betfair_ticks import (
    _TICK_LADDER,
    betfair_tick_size,
    tick_distance,
    within_one_tick,
)
from app.ingestion.base import EventTeams
from app.ingestion.http_safety import UpstreamBodyTooLarge, request_httpx_bounded
from app.pipeline import canonical_market_detail
from app.resolution.matching import (
    AliasTable,
    EventCandidate,
    default_aliases,
    match_event_hardened_scored,
)
from app.schemas.base import Market
from app.schemas.odds import MAX_LIQUIDITY, OddsSnapshotIn

logger = logging.getLogger(__name__)

# --- Betfair endpoints (the ONLY URLs this module ever touches) -------------- #
IDENTITY_LOGIN_URL = "https://identitysso.betfair.com/api/login"
IDENTITY_KEEPALIVE_URL = "https://identitysso.betfair.com/api/keepAlive"
IDENTITY_LOGOUT_URL = "https://identitysso.betfair.com/api/logout"
JSON_RPC_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"

# JSON-RPC operation names — the READ-ONLY allowlist, and nothing else. (Each is
# a price/metadata read; none place a bet or touch a betting account.)
_RPC_PREFIX = "SportsAPING/v1.0/"
_OP_LIST_MARKET_CATALOGUE = "listMarketCatalogue"
_OP_LIST_MARKET_BOOK = "listMarketBook"
# RUNTIME allowlist: the ONLY operations _rpc may ever dispatch. The JSON-RPC
# endpoint also serves order-placement ops for a full-capability session, so the
# read-only guarantee must be STRUCTURAL, not just "no caller passes anything
# else" — _rpc refuses any op outside this frozenset BEFORE any login or HTTP
# (scripts/safety_audit.sh asserts this allowlist is present).
_ALLOWED_OPS = frozenset({_OP_LIST_MARKET_CATALOGUE, _OP_LIST_MARKET_BOOK})
# listMarketBook caps at 200 weight-points/request; EX_BEST_OFFERS ~5/market, so
# batch <=25 markets/call (~125 weight) to stay under the cap (else TOO_MUCH_DATA).
_MARKET_BOOK_BATCH = 25

# Betfair eventType ids this read-only capture may fetch. Soccer is the
# promoted anchor path; TENNIS is SHADOW-ONLY (coverage research 2026-08-03:
# 193 Betfair tennis events sat unfetched in the 72h window against a 58-event
# canonical tennis slate, while the soccer slate held only 10 events).
EVENT_TYPE_SOCCER = "1"
EVENT_TYPE_TENNIS = "2"
MARKET_TYPE_MATCH_ODDS = "MATCH_ODDS"
# EXTENDED price-read market types (default-OFF, VALUE_BETFAIR_API_EXTENDED_
# MARKETS): the soccer Asian-Handicap ladder, the "Goal Lines" asian-total
# ladder, and the fixed half-goal Over/Under markets. Betfair's official
# marketTypeCodes vocabulary — price reads only, exactly like MATCH_ODDS.
MARKET_TYPE_ASIAN_HANDICAP = "ASIAN_HANDICAP"
MARKET_TYPE_ALT_TOTAL_GOALS = "ALT_TOTAL_GOALS"
OVER_UNDER_MARKET_TYPES: tuple[str, ...] = tuple(f"OVER_UNDER_{n}5" for n in range(9))
EXTENDED_MARKET_TYPES: tuple[str, ...] = (
    MARKET_TYPE_ASIAN_HANDICAP,
    MARKET_TYPE_ALT_TOTAL_GOALS,
    *OVER_UNDER_MARKET_TYPES,
)
_OVER_UNDER_TYPE_RE = re.compile(r"^OVER_UNDER_(\d{2})$")
# Extended-catalogue eventIds cap: 11 extended market types per event x 18
# events = 198 markets <= the 200-result catalogue ceiling, so one filtered
# call can never lose markets to FIRST_TO_START truncation (live defect
# 2026-08-03 — the unfiltered call covered only the ~18 soonest events of the
# whole window and starved the capture to 0 lines on a full slate).
_EXTENDED_MAX_EVENTS = 18
# Betfair's constant selectionId for "The Draw" on a soccer Match-Odds market.
DRAW_SELECTION_ID = 58805

# Betfair errorCodes that mean "session expired / missing" -> re-login once.
_SESSION_EXPIRY_CODES = frozenset({"INVALID_SESSION_INFORMATION", "NO_SESSION"})

# Shadow bookmaker tag. DELIBERATELY not a member of app.edge.value.SHARP_BOOKS
# ("betfair exchange"), so a shadow row can NEVER be promoted to the sharp anchor
# or replace the OddsPortal-sourced exchange price.
SHADOW_BOOKMAKER = "betfair exchange (api-shadow)"

# PROMOTION tag (req #2). When VALUE_BETFAIR_API_PROMOTE is enabled the API rows
# carry the LIVE sharp-anchor name ("betfair exchange", a member of
# app.edge.value.SHARP_BOOKS) so they feed the sharp anchor INSTEAD of the scrape.
# DEFAULT OFF: the capture only ever emits this name when promote=True is passed
# explicitly — until then every row stays SHADOW_BOOKMAKER (non-sharp). Promotion
# must be evidence-gated on the comparison below, never flipped blind.
PROMOTED_BOOKMAKER = "betfair exchange"

_TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MARKETS_PER_FETCH = 1_000


# --- price-comparison math (PURE — lives in app/edge/betfair_ticks) ---------- #
# The tick ladder + comparisons were RELOCATED verbatim to the pure-math
# boundary (app/edge/betfair_ticks.py, staleness-guard package 2026-07-02) so
# the mint-path guard can use them without importing an ingestion module. They
# are imported at the top of this module and re-exported here so existing
# consumers/tests keep their import path; the conservative coarser-tick +
# None-propagation semantics are unchanged.
__all__ = [
    "_TICK_LADDER",
    "betfair_tick_size",
    "tick_distance",
    "within_one_tick",
]

# Staleness-guard verdict decisions persisted per (event, market, selection)
# by the API capture's verdict sink (betfair_anchor_verdicts). Only DEMOTE can
# ever alter anchoring (and only when fresh + guard enforcing); every other
# decision is a no-op at mint (fail-open on missing evidence, fail-closed only
# on positive fresh disagreement). STALE_API is a READ-time classification (a
# verdict older than the TTL) — the sink never writes it.
VERDICT_PASS = "pass"
VERDICT_DEMOTE = "demote"
VERDICT_NO_API_MATCH = "no_api_match"
VERDICT_NO_API_PRICE = "no_api_price"
VERDICT_STALE_API = "stale_api"


def verdict_decision(
    inline_price: float | None,
    api_price: float | None,
    *,
    max_ticks: float = 1.0,
) -> str:
    """Write-time staleness-guard decision for one selection (PURE).

    * API price absent -> ``no_api_price`` (the API market had no backable
      price for this runner — nothing to compare, never a demotion).
    * Inline (scrape) price absent -> ``no_api_match`` (the API matched the
      event but this selection has no inline reference row to compare — no
      comparison possible, never a demotion).
    * Both present -> ``demote`` when the tick distance (tick at the COARSER
      price, app/edge/betfair_ticks.tick_distance) exceeds ``max_ticks``,
      else ``pass``. The 1e-9 tolerance mirrors ``within_one_tick`` so a
      boundary price (e.g. 2.00 vs 2.02 at tick 0.02) is a pass, never a
      float-dust demotion.
    """
    if api_price is None:
        return VERDICT_NO_API_PRICE
    if inline_price is None:
        return VERDICT_NO_API_MATCH
    distance = tick_distance(inline_price, api_price)
    if distance is None:  # unreachable given the guards above; fail open
        return VERDICT_NO_API_PRICE
    return VERDICT_DEMOTE if distance > max_ticks + 1e-9 else VERDICT_PASS


@dataclass(frozen=True)
class ReferenceOdds:
    """The EXISTING (OddsPortal-sourced) "betfair exchange" anchor for one event,
    resolved by ROLE (home/draw/away) + the anchor's capture time. Built at the
    composition root from the snapshot store (no DB coupling in this module)."""

    home_back: float | None
    draw_back: float | None
    away_back: float | None
    captured_at: datetime | None


@dataclass(frozen=True)
class SelectionComparison:
    """API-vs-reference price for one selection (role)."""

    selection: str
    api_price: float | None
    ref_price: float | None

    @property
    def delta(self) -> float | None:
        """API price minus reference price, or None when either is absent."""
        if self.api_price is None or self.ref_price is None:
            return None
        return self.api_price - self.ref_price

    @property
    def within_tick(self) -> bool | None:
        return within_one_tick(self.api_price, self.ref_price)


@dataclass(frozen=True)
class EventComparison:
    """One matched event's API-vs-OddsPortal-Betfair comparison."""

    event_ref: str
    home: SelectionComparison
    draw: SelectionComparison
    away: SelectionComparison
    freshness_gap_seconds: float | None

    @property
    def selections(self) -> tuple[SelectionComparison, ...]:
        return (self.home, self.draw, self.away)

    @property
    def api_fresher(self) -> bool | None:
        """True when the API read is newer than the scrape anchor (gap > 0)."""
        if self.freshness_gap_seconds is None:
            return None
        return self.freshness_gap_seconds > 0.0

    def abs_deltas(self) -> list[float]:
        return [abs(s.delta) for s in self.selections if s.delta is not None]

    def tick_flags(self) -> list[bool]:
        return [s.within_tick for s in self.selections if s.within_tick is not None]


def compare_event(
    odds: BetfairMatchOdds,
    reference: ReferenceOdds,
    *,
    api_captured_at: datetime,
    event_ref: str,
) -> EventComparison:
    """Pure per-event comparison of the Betfair-API BACK prices against the
    existing OddsPortal-sourced "betfair exchange" anchor (by role), plus the
    capture-time freshness gap (api_captured_at - reference.captured_at)."""
    gap: float | None = None
    if reference.captured_at is not None:
        gap = (api_captured_at - reference.captured_at).total_seconds()
    return EventComparison(
        event_ref=event_ref,
        home=SelectionComparison("home", odds.home_back, reference.home_back),
        draw=SelectionComparison("draw", odds.draw_back, reference.draw_back),
        away=SelectionComparison("away", odds.away_back, reference.away_back),
        freshness_gap_seconds=gap,
    )


@dataclass(frozen=True)
class ComparisonAggregate:
    """Per-cycle roll-up of the per-event comparisons (measurement only)."""

    compared: int
    mean_abs_delta: float | None
    pct_within_one_tick: float | None
    pct_api_fresher: float | None

    @classmethod
    def from_events(cls, events: Sequence[EventComparison]) -> ComparisonAggregate:
        if not events:
            return cls(
                compared=0, mean_abs_delta=None, pct_within_one_tick=None, pct_api_fresher=None
            )
        abs_deltas = [d for e in events for d in e.abs_deltas()]
        tick_flags = [f for e in events for f in e.tick_flags()]
        fresh_flags = [e.api_fresher for e in events if e.api_fresher is not None]
        mean_abs = sum(abs_deltas) / len(abs_deltas) if abs_deltas else None
        pct_tick = 100.0 * sum(tick_flags) / len(tick_flags) if tick_flags else None
        pct_fresh = 100.0 * sum(fresh_flags) / len(fresh_flags) if fresh_flags else None
        return cls(
            compared=len(events),
            mean_abs_delta=mean_abs,
            pct_within_one_tick=pct_tick,
            pct_api_fresher=pct_fresh,
        )


class BetfairApiError(Exception):
    """Read failure. The message never contains credentials, the session token,
    or the request URL — only the operation name + Betfair/HTTP status."""


class BetfairAuthError(BetfairApiError):
    """Login / session establishment failed (after a re-login retry)."""


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _parse_market_start(raw: str) -> datetime | None:
    """Betfair ``marketStartTime`` (ISO-8601, ``...Z``) -> UTC-aware datetime, or
    None when absent/garbled. UTC everywhere (naive datetime = bug)."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _best_back_level(available_to_back: Any) -> tuple[float, float] | None:
    """(best BACK price, available SIZE (£) at that price) from a runner's
    ``ex.availableToBack`` ladder, or None when empty/garbled. The size is the
    LIQUIDITY proxy for the API path (the dedicated OddsPortal capture uses matched
    volume; this uses backable depth at the best price). An absent/garbled size
    reads as 0.0 (treated as no liquidity by any downstream floor)."""
    if not isinstance(available_to_back, Sequence):
        return None
    best: tuple[float, float] | None = None
    for level in available_to_back:
        if not isinstance(level, Mapping):
            continue
        price = level.get("price")
        if (
            not isinstance(price, int | float)
            or isinstance(price, bool)
            or not math.isfinite(float(price))
            or not 1.0 < float(price) <= 1_000.0
        ):
            continue
        size = level.get("size")
        size_f = (
            float(size)
            if isinstance(size, int | float)
            and not isinstance(size, bool)
            and math.isfinite(float(size))
            and 0 <= size <= MAX_LIQUIDITY
            else 0.0
        )
        if best is None or float(price) > best[0]:
            best = (float(price), size_f)
    return best


def _best_back(available_to_back: Any) -> float | None:
    """Best (highest) BACK price from a runner's ``ex.availableToBack`` ladder, or
    None when the ladder is empty/garbled or only holds non-prices (<=1.0)."""
    level = _best_back_level(available_to_back)
    return level[0] if level is not None else None


@dataclass(frozen=True)
class BetfairRunner:
    """One catalogue runner (no price). ``handicap`` is populated only on
    ladder markets (Asian Handicap / Goal Lines), where the SAME selectionId
    repeats once per line — None on non-ladder markets, never invented."""

    selection_id: int
    name: str
    sort_priority: int
    handicap: float | None = None


@dataclass(frozen=True)
class BetfairMarketCatalogue:
    """One ``listMarketCatalogue`` market with EVENT / COMPETITION /
    MARKET_START_TIME / RUNNER_DESCRIPTION projections. ``market_type`` is the
    Betfair marketType code and is populated only when the call also asked for
    the MARKET_DESCRIPTION projection (the extended-markets path)."""

    market_id: str
    event_id: str
    event_name: str
    competition: str
    market_start_time: datetime | None
    runners: tuple[BetfairRunner, ...]
    market_type: str = ""


@dataclass(frozen=True)
class BetfairMatchOdds:
    """The joined catalogue + book view for one soccer Match-Odds market: the
    home/away/draw best BACK prices with the event identity for matching."""

    market_id: str
    event_id: str
    competition: str
    kickoff: datetime | None
    home: str
    away: str
    home_back: float | None
    away_back: float | None
    draw_back: float | None
    # Available BACK size (£) at the best price per outcome — the API liquidity
    # proxy persisted as OddsSnapshotIn.liquidity when promoted. None = no price.
    home_back_size: float | None = None
    away_back_size: float | None = None
    draw_back_size: float | None = None


def parse_market_catalogue(payload: Sequence[Mapping[str, Any]]) -> list[BetfairMarketCatalogue]:
    """Pure parser for a ``listMarketCatalogue`` result array. A market with no
    marketId is skipped; missing projections degrade gracefully (empty/None)."""
    out: list[BetfairMarketCatalogue] = []
    for market in payload:
        if not isinstance(market, Mapping):
            continue
        market_id = str(market.get("marketId", "")).strip()
        if not market_id:
            continue
        event_raw = market.get("event")
        event: Mapping[str, Any] = event_raw if isinstance(event_raw, Mapping) else {}
        competition_raw = market.get("competition")
        competition: Mapping[str, Any] = (
            competition_raw if isinstance(competition_raw, Mapping) else {}
        )
        description_raw = market.get("description")
        description: Mapping[str, Any] = (
            description_raw if isinstance(description_raw, Mapping) else {}
        )
        runners: list[BetfairRunner] = []
        for runner in market.get("runners") or []:
            if not isinstance(runner, Mapping):
                continue
            sel = runner.get("selectionId")
            if not isinstance(sel, int):
                continue
            runners.append(
                BetfairRunner(
                    selection_id=sel,
                    name=str(runner.get("runnerName", "")).strip(),
                    sort_priority=int(runner.get("sortPriority", 0) or 0),
                    handicap=_finite_handicap(runner.get("handicap")),
                )
            )
        out.append(
            BetfairMarketCatalogue(
                market_id=market_id,
                event_id=str(event.get("id", "")).strip(),
                event_name=str(event.get("name", "")).strip(),
                competition=str(competition.get("name", "")).strip(),
                market_start_time=_parse_market_start(str(market.get("marketStartTime", ""))),
                runners=tuple(runners),
                market_type=str(description.get("marketType", "")).strip(),
            )
        )
    return out


def _finite_handicap(raw: Any) -> float | None:
    """A runner's ``handicap`` as a finite float, or None when absent/garbled
    (never invented — a ladder runner without a parseable line is unusable)."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def parse_market_book_backs_by_handicap(
    payload: Sequence[Mapping[str, Any]],
) -> dict[str, dict[tuple[int, float], tuple[float, float]]]:
    """Pure parser for a ``listMarketBook`` result array (EX_BEST_OFFERS) on
    LADDER markets (Asian Handicap / Goal Lines) ->
    ``{market_id: {(selection_id, handicap): (best_back_price, size@best)}}``.

    Keyed by (selectionId, handicap) because ladder markets repeat the SAME
    selectionId once per line — the selectionId-only parser above would silently
    collapse the ladder to one arbitrary line. An absent handicap keys as 0.0
    (non-ladder markets such as the fixed OVER_UNDER_x5 books). The same
    OPEN/non-in-play fail-closed guards as ``parse_market_book_backs``."""
    books: dict[str, dict[tuple[int, float], tuple[float, float]]] = {}
    for market in payload:
        if not isinstance(market, Mapping):
            continue
        market_id = str(market.get("marketId", "")).strip()
        if not market_id:
            continue
        if str(market.get("status", "")).upper() != "OPEN":
            continue
        if market.get("inplay") is not False:
            continue
        per_runner: dict[tuple[int, float], tuple[float, float]] = {}
        for runner in market.get("runners") or []:
            if not isinstance(runner, Mapping):
                continue
            sel = runner.get("selectionId")
            if not isinstance(sel, int):
                continue
            handicap = _finite_handicap(runner.get("handicap"))
            ex_raw = runner.get("ex")
            ex: Mapping[str, Any] = ex_raw if isinstance(ex_raw, Mapping) else {}
            level = _best_back_level(ex.get("availableToBack"))
            if level is not None:
                per_runner[(sel, _line_key(handicap or 0.0))] = level
        books[market_id] = per_runner
    return books


def parse_market_book_backs(
    payload: Sequence[Mapping[str, Any]],
) -> dict[str, dict[int, tuple[float, float]]]:
    """Pure parser for a ``listMarketBook`` result array (EX_BEST_OFFERS) ->
    ``{market_id: {selection_id: (best_back_price, size@best)}}``. Only an
    explicitly OPEN, non-in-play book is eligible; missing status fields fail
    closed because Betfair can return a book while a market transitions at
    kickoff. Runners with no backable price are omitted (never invented)."""
    books: dict[str, dict[int, tuple[float, float]]] = {}
    for market in payload:
        if not isinstance(market, Mapping):
            continue
        market_id = str(market.get("marketId", "")).strip()
        if not market_id:
            continue
        if str(market.get("status", "")).upper() != "OPEN":
            continue
        if market.get("inplay") is not False:
            continue
        per_runner: dict[int, tuple[float, float]] = {}
        for runner in market.get("runners") or []:
            if not isinstance(runner, Mapping):
                continue
            sel = runner.get("selectionId")
            if not isinstance(sel, int):
                continue
            ex_raw = runner.get("ex")
            ex: Mapping[str, Any] = ex_raw if isinstance(ex_raw, Mapping) else {}
            level = _best_back_level(ex.get("availableToBack"))
            if level is not None:
                per_runner[sel] = level  # (best_back_price, size)
        books[market_id] = per_runner
    return books


def _roles(
    runners: Sequence[BetfairRunner],
) -> tuple[BetfairRunner | None, BetfairRunner | None, BetfairRunner | None]:
    """(home, away, draw) runners from a Match-Odds runner set. Home/away come
    from sortPriority (1/2, Betfair's stable convention); the draw is the runner
    with selectionId 58805 (or sortPriority 3 / name 'The Draw') as a fallback."""
    home = away = draw = None
    for runner in runners:
        if runner.selection_id == DRAW_SELECTION_ID or runner.name.strip().lower() == "the draw":
            draw = runner
        elif runner.sort_priority == 1:
            home = runner
        elif runner.sort_priority == 2:
            away = runner
    if draw is None:
        for runner in runners:
            if runner.sort_priority == 3:
                draw = runner
                break
    return home, away, draw


def join_match_odds(
    catalogue: Sequence[BetfairMarketCatalogue],
    backs: Mapping[str, Mapping[int, tuple[float, float]]],
) -> list[BetfairMatchOdds]:
    """Join catalogue runner identities with their best BACK prices into
    ``BetfairMatchOdds``. A market with no resolvable home/away runner is skipped
    (it is not a usable Match-Odds market)."""
    out: list[BetfairMatchOdds] = []
    for market in catalogue:
        home, away, draw = _roles(market.runners)
        if home is None or away is None:
            continue
        # Absence means the book was non-open/in-play/malformed and was rejected
        # by parse_market_book_backs.  Do not manufacture a price-less match.
        if market.market_id not in backs:
            continue
        per_runner = backs[market.market_id]
        home_ps = per_runner.get(home.selection_id)
        away_ps = per_runner.get(away.selection_id)
        draw_ps = per_runner.get(draw.selection_id) if draw is not None else None
        out.append(
            BetfairMatchOdds(
                market_id=market.market_id,
                event_id=market.event_id,
                competition=market.competition,
                kickoff=market.market_start_time,
                home=home.name,
                away=away.name,
                home_back=home_ps[0] if home_ps is not None else None,
                away_back=away_ps[0] if away_ps is not None else None,
                draw_back=draw_ps[0] if draw_ps is not None else None,
                home_back_size=home_ps[1] if home_ps is not None else None,
                away_back_size=away_ps[1] if away_ps is not None else None,
                draw_back_size=draw_ps[1] if draw_ps is not None else None,
            )
        )
    return out


# --- EXTENDED markets: Asian Handicap + Over/Under goal lines ---------------- #
# Vocabulary NOTE: the emitted market_detail keys are the SCRAPED OddsChecker
# vocabulary (spreads_minus_0_25 / totals_2_5 — byte-equal to
# app.ingestion.oddschecker._market_for_type output for the same line) run
# through the pipeline's canonical fold, so a captured API line lands in the
# SAME (event, market, detail) devig group as the scraped rows. Never a new
# vocabulary (pipeline mint-grouping unification, audit 2026-08-02).


def _line_key(value: float) -> float:
    """Stable float key for a Betfair handicap (quarter-goal grid) so catalogue
    and book runner entries join exactly (2 decimals is exact on the grid)."""
    return round(value, 2)


def _is_fractional_line(value: float) -> bool:
    """True for a half/quarter line (non-integer). Integer lines are
    fail-closed: an integer AH detail would collide with the scraped 3-way
    European-handicap spreads_* key space (vocabulary audit 2026-07-10), and an
    integer totals line has a push third outcome (the candidate boundary
    rejects such groups anyway)."""
    return abs(value - round(value)) > 1e-9


def _signed_line_token(line: float) -> str:
    """'-0.25' -> 'minus_0_25', '+0.25' -> 'plus_0_25' — byte-equal to the
    scrape's slug of the signed line string (oddschecker._slug_line)."""
    text = format(line, "+g").replace("+", "plus_").replace("-", "minus_")
    return re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()


def _unsigned_line_token(line: float) -> str:
    """'2.5' -> '2_5', '2.25' -> '2_25' (totals lines are unsigned)."""
    return re.sub(r"[^0-9A-Za-z]+", "_", format(abs(line), "g")).strip("_").lower()


@dataclass(frozen=True)
class BetfairLineQuote:
    """One side of one captured AH / Over-Under line: the canonical devig-group
    key (market + market_detail, scraped vocabulary) plus the best BACK price
    and size. ``line`` is the signed HOME line for SPREADS and the goals line
    for TOTALS; the canonical selection STRING is built later from the matched
    candidate's team names (never the Betfair runner names)."""

    market_id: str
    event_id: str
    market: Market
    market_detail: str
    side: str  # 'home' | 'away' (SPREADS) / 'over' | 'under' (TOTALS)
    line: float
    back: float
    back_size: float


def _spread_detail(home_line: float) -> str | None:
    """Canonical scraped-vocabulary detail for a fractional AH home line, via
    the pipeline fold (a fixed point for spreads_*)."""
    return canonical_market_detail(f"spreads_{_signed_line_token(home_line)}")


def _totals_detail(line: float) -> str | None:
    """Canonical scraped-vocabulary detail for a fractional totals line."""
    return canonical_market_detail(f"totals_{_unsigned_line_token(line)}")


def _asian_handicap_quotes(
    market: BetfairMarketCatalogue,
    per_runner: Mapping[tuple[int, float], tuple[float, float]],
    teams: tuple[str, str],
) -> list[BetfairLineQuote]:
    """Two-sided quotes for each FRACTIONAL AH line of one ladder market.

    Sides are resolved by comparing runner names against the Match-Odds
    home/away runner names for the SAME Betfair event (fail-closed: an
    unrecognized runner name is skipped, never guessed). A line is emitted only
    when BOTH sides carry a backable price — a one-sided API line cannot form
    the plausible two-way book this capture exists to provide."""
    home_name, away_name = (t.strip().casefold() for t in teams)
    if not home_name or not away_name or home_name == away_name:
        return []
    home_prices: dict[float, tuple[float, float]] = {}
    away_prices: dict[float, tuple[float, float]] = {}
    for runner in market.runners:
        if runner.handicap is None:
            continue
        line = _line_key(runner.handicap)
        level = per_runner.get((runner.selection_id, line))
        if level is None:
            continue
        name = runner.name.strip().casefold()
        if name == home_name:
            home_prices[line] = level
        elif name == away_name:
            away_prices[line] = level
    out: list[BetfairLineQuote] = []
    for home_line in sorted(home_prices):
        if not _is_fractional_line(home_line):
            continue  # integer AH: fail-closed (EH-collision, see note above)
        away_level = away_prices.get(_line_key(-home_line))
        if away_level is None:
            continue  # one-sided line: adds nothing beyond the thin scrape
        detail = _spread_detail(home_line)
        if detail is None:  # unreachable for spreads_*; fail closed
            continue
        home_level = home_prices[home_line]
        out.append(
            BetfairLineQuote(
                market_id=market.market_id,
                event_id=market.event_id,
                market=Market.SPREADS,
                market_detail=detail,
                side="home",
                line=home_line,
                back=home_level[0],
                back_size=home_level[1],
            )
        )
        out.append(
            BetfairLineQuote(
                market_id=market.market_id,
                event_id=market.event_id,
                market=Market.SPREADS,
                market_detail=detail,
                side="away",
                line=home_line,
                back=away_level[0],
                back_size=away_level[1],
            )
        )
    return out


def _totals_quotes_for_lines(
    market: BetfairMarketCatalogue,
    over_prices: Mapping[float, tuple[float, float]],
    under_prices: Mapping[float, tuple[float, float]],
) -> list[BetfairLineQuote]:
    """Two-sided TOTALS quotes for every fractional line priced on BOTH sides."""
    out: list[BetfairLineQuote] = []
    for line in sorted(over_prices):
        if line <= 0 or not _is_fractional_line(line):
            continue  # integer totals: push third outcome — never emitted
        under_level = under_prices.get(line)
        if under_level is None:
            continue
        detail = _totals_detail(line)
        if detail is None:  # unreachable for fractional totals_*; fail closed
            continue
        over_level = over_prices[line]
        for side, level in (("over", over_level), ("under", under_level)):
            out.append(
                BetfairLineQuote(
                    market_id=market.market_id,
                    event_id=market.event_id,
                    market=Market.TOTALS,
                    market_detail=detail,
                    side=side,
                    line=line,
                    back=level[0],
                    back_size=level[1],
                )
            )
    return out


def _over_under_sides(
    market: BetfairMarketCatalogue,
    per_runner: Mapping[tuple[int, float], tuple[float, float]],
    *,
    line_from_handicap: bool,
    fixed_line: float = 0.0,
) -> tuple[dict[float, tuple[float, float]], dict[float, tuple[float, float]]]:
    """(over, under) price maps keyed by line. ALT_TOTAL_GOALS carries the line
    in the runner handicap (``line_from_handicap=True``); the fixed
    OVER_UNDER_x5 markets carry it in the marketType (``fixed_line``)."""
    over: dict[float, tuple[float, float]] = {}
    under: dict[float, tuple[float, float]] = {}
    for runner in market.runners:
        handicap = _line_key(runner.handicap or 0.0)
        level = per_runner.get((runner.selection_id, handicap))
        if level is None:
            continue
        line = handicap if line_from_handicap else _line_key(fixed_line)
        name = runner.name.strip().casefold()
        if name.startswith("over"):
            over[line] = level
        elif name.startswith("under"):
            under[line] = level
    return over, under


def join_extended_lines(
    catalogue: Sequence[BetfairMarketCatalogue],
    books: Mapping[str, Mapping[tuple[int, float], tuple[float, float]]],
    home_away_by_event: Mapping[str, tuple[str, str]],
) -> list[BetfairLineQuote]:
    """Join the EXTENDED catalogue (AH + goal lines) with its handicap-keyed
    best-back books into canonical-vocabulary two-sided line quotes.

    ``home_away_by_event`` maps a Betfair event id to the Match-Odds home/away
    RUNNER NAMES from the same cycle's h2h fetch — the extended capture covers
    only events the h2h path already resolves (side identity comes from the
    proven Match-Odds sortPriority convention, never re-derived). Pure."""
    quotes: list[BetfairLineQuote] = []
    for market in catalogue:
        per_runner = books.get(market.market_id)
        if not per_runner:
            continue  # book rejected (in-play / non-open) or priceless
        market_type = market.market_type.strip().upper()
        if market_type == MARKET_TYPE_ASIAN_HANDICAP:
            teams = home_away_by_event.get(market.event_id)
            if teams is None:
                continue  # not an event the h2h path resolved this cycle
            quotes.extend(_asian_handicap_quotes(market, per_runner, teams))
        elif market_type == MARKET_TYPE_ALT_TOTAL_GOALS:
            over, under = _over_under_sides(market, per_runner, line_from_handicap=True)
            quotes.extend(_totals_quotes_for_lines(market, over, under))
        else:
            match = _OVER_UNDER_TYPE_RE.match(market_type)
            if match is None:
                continue  # unknown type: skipped, never guessed
            fixed = int(match.group(1)) / 10.0
            over, under = _over_under_sides(
                market, per_runner, line_from_handicap=False, fixed_line=fixed
            )
            quotes.extend(_totals_quotes_for_lines(market, over, under))
    return quotes


class BetfairApiClient:
    """READ-ONLY Betfair Exchange market-data client.

    The ``httpx.AsyncClient`` is injected (so tests drive it with MockTransport
    and the composition root binds the single dedicated proxy). The session token
    is established lazily on first call, held in memory, refreshed on expiry, and
    discarded on ``logout``/``aclose`` — never written to disk.
    """

    def __init__(
        self,
        *,
        app_key: str,
        username: str,
        password: str,
        client: httpx.AsyncClient,
    ) -> None:
        if not app_key or not username or not password:
            raise ValueError("betfair api requires app_key, username and password")
        self._app_key = app_key
        self._username = username
        self._password = password
        self._client = client
        self._session_token: str | None = None

    @property
    def has_session(self) -> bool:
        return self._session_token is not None

    # --- transport (retry transport errors ONLY; never 4xx/5xx) -------------- #
    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def _post(
        self,
        url: str,
        *,
        json: Any | None = None,
        data: Any | None = None,
        headers: dict[str, str],
    ) -> httpx.Response:
        try:
            return await request_httpx_bounded(
                self._client,
                "POST",
                url,
                max_bytes=MAX_RESPONSE_BYTES,
                json=json,
                data=data,
                headers=headers,
                timeout=_TIMEOUT,
            )
        except UpstreamBodyTooLarge:
            raise BetfairApiError("betfair response exceeded byte ceiling") from None

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def _get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        try:
            return await request_httpx_bounded(
                self._client,
                "GET",
                url,
                max_bytes=MAX_RESPONSE_BYTES,
                headers=headers,
                timeout=_TIMEOUT,
            )
        except UpstreamBodyTooLarge:
            raise BetfairApiError("betfair response exceeded byte ceiling") from None

    # --- session ------------------------------------------------------------- #
    async def login(self) -> None:
        """Interactive login -> in-memory session token. Raises BetfairAuthError
        on any non-SUCCESS status (message carries the Betfair loginStatus
        category only — never the username/password)."""
        headers = {
            "X-Application": self._app_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        response = await self._post(
            IDENTITY_LOGIN_URL,
            data={"username": self._username, "password": self._password},
            headers=headers,
        )
        if response.status_code != 200:
            raise BetfairAuthError(f"betfair login HTTP status {response.status_code}")
        body = response.json()
        status = str(body.get("status", "")) if isinstance(body, Mapping) else ""
        if status != "SUCCESS":
            # loginStatus (e.g. INVALID_USERNAME_OR_PASSWORD) is a category, not a
            # secret; the credentials themselves are never echoed.
            login_status = str(body.get("error", "")) if isinstance(body, Mapping) else ""
            raise BetfairAuthError(
                f"betfair login failed (status={status} loginStatus={login_status})"
            )
        token = str(body.get("token", "")) if isinstance(body, Mapping) else ""
        if not token:
            raise BetfairAuthError("betfair login returned no session token")
        self._session_token = token
        logger.info("betfair api: session established (read-only market data)")

    async def keep_alive(self) -> None:
        """Refresh the session TTL. A non-SUCCESS response clears the token so the
        next call re-logs in. Never raises on a benign keep-alive miss."""
        if self._session_token is None:
            return
        response = await self._get(IDENTITY_KEEPALIVE_URL, headers=self._auth_headers())
        ok = False
        if response.status_code == 200:
            body = response.json()
            ok = isinstance(body, Mapping) and str(body.get("status", "")) == "SUCCESS"
        if not ok:
            logger.warning("betfair api: keepAlive did not succeed; will re-login on next call")
            self._session_token = None

    async def logout(self) -> None:
        """Invalidate + drop the in-memory session token (call on shutdown)."""
        if self._session_token is None:
            return
        try:
            await self._get(IDENTITY_LOGOUT_URL, headers=self._auth_headers())
        except (httpx.HTTPError, BetfairApiError) as exc:  # logout is best-effort
            logger.warning("betfair api: logout failed (%s)", type(exc).__name__)
        finally:
            self._session_token = None

    async def aclose(self) -> None:
        await self.logout()

    def _auth_headers(self) -> dict[str, str]:
        return {
            "X-Application": self._app_key,
            "X-Authentication": self._session_token or "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # --- JSON-RPC core (read-only ops only) ---------------------------------- #
    async def _rpc(self, op: str, params: Mapping[str, Any]) -> Any:
        """Call a read-only SportsAPING operation. Establishes a session if
        needed and re-logs-in EXACTLY ONCE on a session-expiry errorCode.

        Any op outside the read-only ``_ALLOWED_OPS`` allowlist is refused HERE,
        before any login or HTTP — a structural guard, since the JSON-RPC
        endpoint would also accept order-placement ops."""
        if op not in _ALLOWED_OPS:
            raise BetfairApiError(f"betfair op {op!r} is not in the read-only allowlist")
        if self._session_token is None:
            await self.login()
        body = await self._rpc_once(op, params)
        if self._is_session_expired(body):
            logger.info("betfair api: session expired mid-call; re-logging in")
            self._session_token = None
            await self.login()
            body = await self._rpc_once(op, params)
            if self._is_session_expired(body):
                raise BetfairAuthError(f"betfair {op} failed: session invalid after re-login")
        error = body.get("error") if isinstance(body, Mapping) else None
        if error is not None:
            raise BetfairApiError(f"betfair {op} error: {_error_code(body)}")
        result = body.get("result") if isinstance(body, Mapping) else None
        if result is None:
            raise BetfairApiError(f"betfair {op} returned no result")
        return result

    async def _rpc_once(self, op: str, params: Mapping[str, Any]) -> Any:
        request = {
            "jsonrpc": "2.0",
            "method": f"{_RPC_PREFIX}{op}",
            "params": dict(params),
            "id": 1,
        }
        response = await self._post(JSON_RPC_URL, json=request, headers=self._auth_headers())
        if response.status_code != 200:
            raise BetfairApiError(f"betfair {op} HTTP status {response.status_code}")
        return response.json()

    @staticmethod
    def _is_session_expired(body: Any) -> bool:
        return _error_code(body) in _SESSION_EXPIRY_CODES

    # --- read-only operations ------------------------------------------------ #
    async def list_market_catalogue(
        self,
        *,
        event_type_ids: Sequence[str],
        market_start_from: datetime,
        market_start_to: datetime,
        market_type_codes: Sequence[str] = (MARKET_TYPE_MATCH_ODDS,),
        max_results: int = 200,
        include_market_description: bool = False,
        event_ids: Sequence[str] | None = None,
    ) -> list[BetfairMarketCatalogue]:
        if isinstance(max_results, bool) or not 1 <= max_results <= MAX_MARKETS_PER_FETCH:
            raise BetfairApiError(f"betfair max_results must be within 1..{MAX_MARKETS_PER_FETCH}")
        # MARKET_DESCRIPTION (extended path only) adds description.marketType so
        # the ladder joiner can dispatch AH vs goal-line handling — still a pure
        # metadata read on the same read-only op.
        projection = ["EVENT", "COMPETITION", "MARKET_START_TIME", "RUNNER_DESCRIPTION"]
        if include_market_description:
            projection.append("MARKET_DESCRIPTION")
        result = await self._rpc(
            _OP_LIST_MARKET_CATALOGUE,
            {
                "filter": _build_filter(
                    event_type_ids,
                    market_type_codes,
                    market_start_from,
                    market_start_to,
                    event_ids=event_ids,
                ),
                "marketProjection": projection,
                "maxResults": max_results,
                "sort": "FIRST_TO_START",
            },
        )
        if not isinstance(result, list):
            raise BetfairApiError("betfair listMarketCatalogue returned invalid result container")
        if len(result) > max_results or len(result) > MAX_MARKETS_PER_FETCH:
            raise BetfairApiError("betfair listMarketCatalogue exceeded result ceiling")
        return parse_market_catalogue(result)

    async def list_market_book_backs(
        self, market_ids: Sequence[str]
    ) -> dict[str, dict[int, tuple[float, float]]]:
        # Betfair caps listMarketBook at 200 weight-points/request; EX_BEST_OFFERS is
        # ~5/market, so request in batches of <=25 markets (~125 weight) to stay safely
        # under the cap (a single all-markets call returns TOO_MUCH_DATA). Read-only.
        if not market_ids:
            return {}
        ids = list(market_ids)
        if len(ids) > MAX_MARKETS_PER_FETCH:
            raise BetfairApiError("betfair listMarketBook exceeded market-id ceiling")
        out: dict[str, dict[int, tuple[float, float]]] = {}
        for start in range(0, len(ids), _MARKET_BOOK_BATCH):
            batch = ids[start : start + _MARKET_BOOK_BATCH]
            result = await self._rpc(
                _OP_LIST_MARKET_BOOK,
                {
                    "marketIds": batch,
                    "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
                },
            )
            if not isinstance(result, list):
                raise BetfairApiError("betfair listMarketBook returned invalid result container")
            if len(result) > len(batch):
                raise BetfairApiError("betfair listMarketBook exceeded batch result ceiling")
            out.update(parse_market_book_backs(result))
        return out

    async def fetch_match_odds(
        self,
        *,
        market_start_from: datetime,
        market_start_to: datetime,
        event_type_ids: Sequence[str] = (EVENT_TYPE_SOCCER,),
        max_results: int = 200,
    ) -> list[BetfairMatchOdds]:
        """High-level read: the Match-Odds catalogue joined with EX_BEST_OFFERS
        best-back prices, for markets starting in the window. Empty list when the
        window holds no markets (a benign quiet slate, never a silent success on
        an error — RPC errors raise)."""
        catalogue = await self.list_market_catalogue(
            event_type_ids=event_type_ids,
            market_start_from=market_start_from,
            market_start_to=market_start_to,
            max_results=max_results,
        )
        if not catalogue:
            return []
        market_ids = [m.market_id for m in catalogue]
        backs = await self.list_market_book_backs(market_ids)
        return join_match_odds(catalogue, backs)

    async def list_market_book_backs_by_handicap(
        self, market_ids: Sequence[str]
    ) -> dict[str, dict[tuple[int, float], tuple[float, float]]]:
        """Ladder-aware sibling of ``list_market_book_backs``: the SAME read-only
        listMarketBook op and <=25-market batching, parsed keyed by
        (selectionId, handicap) so AH / goal-line ladders never collapse."""
        if not market_ids:
            return {}
        ids = list(market_ids)
        if len(ids) > MAX_MARKETS_PER_FETCH:
            raise BetfairApiError("betfair listMarketBook exceeded market-id ceiling")
        out: dict[str, dict[tuple[int, float], tuple[float, float]]] = {}
        for start in range(0, len(ids), _MARKET_BOOK_BATCH):
            batch = ids[start : start + _MARKET_BOOK_BATCH]
            result = await self._rpc(
                _OP_LIST_MARKET_BOOK,
                {
                    "marketIds": batch,
                    "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
                },
            )
            if not isinstance(result, list):
                raise BetfairApiError("betfair listMarketBook returned invalid result container")
            if len(result) > len(batch):
                raise BetfairApiError("betfair listMarketBook exceeded batch result ceiling")
            out.update(parse_market_book_backs_by_handicap(result))
        return out

    async def fetch_extended_line_books(
        self,
        *,
        market_start_from: datetime,
        market_start_to: datetime,
        event_ids: Sequence[str],
        event_type_ids: Sequence[str] = (EVENT_TYPE_SOCCER,),
        max_results: int = 200,
    ) -> tuple[
        list[BetfairMarketCatalogue],
        dict[str, dict[tuple[int, float], tuple[float, float]]],
    ]:
        """High-level EXTENDED read: ONE catalogue call over the AH + goal-line
        market types plus ONE batched book pass — the exact request shape of the
        h2h path, so arming the flag adds exactly one catalogue+book batch per
        cycle. Empty catalogue -> ([], {}) (a benign quiet slate; RPC errors
        raise, never a silent success).

        ``event_ids`` (REQUIRED — the matched Betfair events, live defect
        2026-08-03): an unfiltered call caps at ``max_results`` markets ~= the
        ~18 soonest events of the window (11 types/event, FIRST_TO_START) and
        starves the capture to 0 lines on a full slate. Empty -> ([], {}) with
        NO network call. Ids beyond ``_EXTENDED_MAX_EVENTS`` are truncated
        (caller order preserved — soonest-first from the h2h scan) so the
        catalogue result ceiling can never silently drop markets mid-slate."""
        ids = list(event_ids)[:_EXTENDED_MAX_EVENTS]
        if not ids:
            return [], {}
        catalogue = await self.list_market_catalogue(
            event_type_ids=event_type_ids,
            market_start_from=market_start_from,
            market_start_to=market_start_to,
            market_type_codes=EXTENDED_MARKET_TYPES,
            max_results=max_results,
            include_market_description=True,
            event_ids=ids,
        )
        if not catalogue:
            return [], {}
        books = await self.list_market_book_backs_by_handicap([m.market_id for m in catalogue])
        return catalogue, books


def _build_filter(
    event_type_ids: Sequence[str],
    market_type_codes: Sequence[str],
    market_start_from: datetime | None,
    market_start_to: datetime | None,
    event_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    market_filter: dict[str, Any] = {"eventTypeIds": list(event_type_ids)}
    if market_type_codes:
        market_filter["marketTypeCodes"] = list(market_type_codes)
    if event_ids:
        # Scope the catalogue to specific Betfair events (the extended-markets
        # fix, live defect 2026-08-03): without this, FIRST_TO_START + the
        # 200-result cap silently truncate a full slate to the ~18
        # soonest-starting events — which almost never intersect the matched
        # canonical slate, starving the capture to lines=0.
        market_filter["eventIds"] = list(event_ids)
    time_range: dict[str, str] = {}
    if market_start_from is not None:
        time_range["from"] = _iso_z(market_start_from)
    if market_start_to is not None:
        time_range["to"] = _iso_z(market_start_to)
    if time_range:
        market_filter["marketStartTime"] = time_range
    return market_filter


def _fmt_num(value: float | None, fmt: str = "%.3f") -> str:
    """Safe log formatter: ``n/a`` when the metric is undefined (None)."""
    return "n/a" if value is None else fmt % value


def _fmt_delta(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}"


def _fmt_gap(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}"


def _iso_z(value: datetime) -> str:
    """UTC ISO-8601 with a trailing Z (Betfair's expected time format)."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_code(body: Any) -> str:
    """The Betfair APING errorCode from a JSON-RPC error envelope, or "" — used
    for session-expiry detection and for safe (non-secret) error messages."""
    if not isinstance(body, Mapping):
        return ""
    error = body.get("error")
    if not isinstance(error, Mapping):
        return ""
    data = error.get("data")
    if isinstance(data, Mapping):
        aping = data.get("APINGException")
        if isinstance(aping, Mapping):
            code = aping.get("errorCode")
            if isinstance(code, str):
                return code
    message = error.get("message")
    return message if isinstance(message, str) else ""


# --- shadow capture ---------------------------------------------------------- #
CandidatesFn = Callable[[], Sequence[EventCandidate] | Awaitable[Sequence[EventCandidate]]]
# Resolves a matched canonical event_ref -> the EXISTING OddsPortal-sourced
# "betfair exchange" anchor (by role) for the price comparison, or None when the
# event has no such anchor yet. Built at the composition root (the snapshot store
# lives there); this module never opens a session. Sync or async.
ReferenceOddsFn = Callable[[str], ReferenceOdds | None | Awaitable[ReferenceOdds | None]]
# PROMOTION sink (req #2): persists the API-sourced sharp rows so they feed the
# live "betfair exchange" anchor. Wired ONLY when promotion is enabled; default
# OFF means it is never constructed and never called (provably inert).
PromoteSink = Callable[[Sequence[OddsSnapshotIn], Mapping[str, EventTeams]], Awaitable[int]]


@dataclass(frozen=True)
class SourceLinkObservation:
    """One ACCEPTED ingestion-time match: the Betfair event/market ids linked to
    a canonical event ref, with the hardened matcher's confidence provenance.
    Pure data — the composition root persists these into event_source_links
    (observability only; a sink failure never breaks the capture)."""

    source: str
    source_event_id: str
    source_market_id: str | None
    canonical_external_ref: str
    confidence: float
    method: str
    matched_at: datetime
    raw_league: str | None = None
    raw_home: str | None = None
    raw_away: str | None = None
    raw_start_time_utc: datetime | None = None


# OBSERVABILITY sink for accepted-match link observations (event_source_links).
# Optional and best-effort: never wired -> nothing recorded; failure -> logged
# type-only and the capture continues (a tap, never a gate).
LinkSink = Callable[[Sequence[SourceLinkObservation]], Awaitable[None]]


@dataclass(frozen=True)
class AnchorVerdictObservation:
    """One selection's staleness-guard verdict from a compare cycle: the inline
    (OddsPortal-scrape) Betfair price vs the fresh API best-back price, with the
    tick distance and the write-time decision (``verdict_decision``). Pure data
    — the composition root persists these into betfair_anchor_verdicts
    (keep-latest upsert per (event, market, selection)). The row IS the
    diagnostic record; the mint-time guard only ever READS the table."""

    event_ref: str  # canonical OddsPortal match URL
    market: str  # 'h2h' (the only market the API capture covers, v1)
    selection_role: str  # home | draw | away
    inline_price: float | None
    api_price: float | None
    api_best_back_size: float | None  # £ available at best back — (i) cross-check
    tick_diff: float | None  # |delta| in ticks at the coarser price
    inline_captured_at: datetime | None
    api_captured_at: datetime
    decision: str  # pass | demote | no_api_match | no_api_price


# OBSERVABILITY sink for per-selection staleness verdicts (betfair_anchor_
# verdicts). Same contract as LinkSink: optional, best-effort, a tap never a
# gate — never wired -> nothing recorded; failure -> type-only log, capture
# continues. The MINT path never calls the Betfair API: it reads the latest
# persisted verdicts from the DB (fresh-only, TTL at read time).
VerdictSink = Callable[[Sequence[AnchorVerdictObservation]], Awaitable[None]]


@dataclass(frozen=True)
class BetfairApiShadowReport:
    """One shadow cycle's outcome: how many Betfair markets were fetched, how many
    matched a canonical event, how many did not, and the would-be anchor rows.

    ``comparison`` is the per-cycle API-vs-OddsPortal-Betfair price roll-up (None
    when no reference loader is wired). ``promoted`` is True only when the
    default-OFF promotion flag is enabled — then the rows carry the SHARP
    ``PROMOTED_BOOKMAKER`` name; otherwise they stay the non-sharp
    ``SHADOW_BOOKMAKER`` and nothing is persisted."""

    markets_fetched: int
    matched: int
    unmatched: int
    snapshots: tuple[OddsSnapshotIn, ...]
    comparison: ComparisonAggregate | None = None
    promoted: bool = False
    # Canonical candidates the matcher was given this cycle. ``match_rate``
    # divides by BETFAIR's slate (how much of the exchange we can attach);
    # ``slate_coverage`` divides by OUR slate (how much of the pick universe
    # got a Betfair anchor) — the number that actually measures coverage
    # (2026-08-03: a healthy 9-of-10-slate cycle read as "5.8%" because only
    # match_rate was logged).
    candidates_considered: int = 0
    # EXTENDED-markets telemetry (counts only): distinct (event, market, detail)
    # line groups emitted / distinct events they cover, and whether the extended
    # fetch FAILED this cycle (flagged loudly, never a silent pretend-success).
    extended_lines: int = 0
    extended_events: int = 0
    extended_failed: bool = False

    @property
    def match_rate(self) -> float:
        return self.matched / self.markets_fetched if self.markets_fetched else 0.0

    @property
    def slate_coverage(self) -> float:
        """Matched share of OUR candidate slate (0.0 when the slate is empty)."""
        return self.matched / self.candidates_considered if self.candidates_considered else 0.0


class BetfairApiShadowCapture:
    """SHADOW-only capture: fetch Betfair Match-Odds, match each market to a
    canonical event with the EXISTING hardened matcher, and LOG the match rate +
    would-be BACK anchor. Persists NOTHING and never touches the live anchor."""

    def __init__(
        self,
        client: BetfairApiClient,
        *,
        candidates_fn: CandidatesFn,
        window: timedelta,
        aliases: AliasTable | None = None,
        event_type_ids: Sequence[str] = (EVENT_TYPE_SOCCER,),
        now_fn: Callable[[], datetime] | None = None,
        reference_odds_fn: ReferenceOddsFn | None = None,
        promote: bool = False,
        promote_sink: PromoteSink | None = None,
        link_sink: LinkSink | None = None,
        verdict_sink: VerdictSink | None = None,
        verdict_ticks: float = 1.0,
        extended_markets: bool = False,
        ordered: bool = True,
        name_fold: Callable[[str], str] | None = None,
        label: str = "soccer",
    ) -> None:
        self._client = client
        self._candidates_fn = candidates_fn
        self._window = window
        # MULTI-SPORT surface (2026-08-03, shadow-only tennis): ``ordered``
        # feeds the hardened matcher (False for two-player tennis — no fixed
        # home/away orientation); ``name_fold`` is applied to the BETFAIR
        # runner names at MATCH time only (e.g. canonical_tennis_name) — the
        # candidates_fn feeds already-folded candidate names, and the raw
        # Betfair names still ride the link observations untouched. Defaults
        # keep the soccer path byte-identical. ``label`` tags the telemetry
        # lines so per-sport cycles are distinguishable.
        self._ordered = ordered
        self._name_fold = name_fold
        self._label = label
        # EXTENDED markets (VALUE_BETFAIR_API_EXTENDED_MARKETS, default OFF):
        # when False, zero extra RPC calls — the rate budget is byte-identical
        # to the h2h-only capture.
        self._extended_markets = extended_markets
        self._aliases = aliases or default_aliases()
        self._event_type_ids = tuple(event_type_ids)
        self._now_fn = now_fn or _utc_now
        self._reference_odds_fn = reference_odds_fn
        # OBSERVABILITY tap (event_source_links): default None -> inert.
        self._link_sink = link_sink
        # OBSERVABILITY tap (betfair_anchor_verdicts): default None -> inert.
        # Demotion threshold in ticks (VALUE_BETFAIR_STALENESS_TICKS) — used
        # only to compute the WRITE-time decision; the mint path re-reads the
        # persisted rows and applies its own freshness TTL.
        self._verdict_sink = verdict_sink
        self._verdict_ticks = verdict_ticks
        # PROMOTION is default-OFF. When OFF the rows are tagged the NON-SHARP
        # SHADOW_BOOKMAKER and the sink is never invoked — byte-equivalent to the
        # measurement-only shadow. The sharp PROMOTED_BOOKMAKER is emitted ONLY
        # when promote=True is passed explicitly (evidence-gated by the operator).
        self._promote = promote
        self._promote_sink = promote_sink
        self._bookmaker = PROMOTED_BOOKMAKER if promote else SHADOW_BOOKMAKER

    @property
    def promote(self) -> bool:
        return self._promote

    async def _candidates(self) -> Sequence[EventCandidate]:
        candidates = self._candidates_fn()
        if inspect.isawaitable(candidates):
            candidates = await candidates
        return candidates

    async def _reference(self, event_ref: str) -> ReferenceOdds | None:
        if self._reference_odds_fn is None:
            return None
        result = self._reference_odds_fn(event_ref)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _snapshots_for(
        self,
        odds: BetfairMatchOdds,
        home: str,
        away: str,
        event_ref: str,
        now: datetime,
    ) -> list[OddsSnapshotIn]:
        # Selection names come from the MATCHED CANONICAL candidate (home/away),
        # NOT odds.home/odds.away (the Betfair runner names) — the promoted rows
        # attach to the canonical event and must speak the pick's OddsPortal
        # selection vocabulary, or the anchor's per-selection lookup silently
        # misses (complete=False) on any name-form gap. Mirrors the Pinnacle path
        # (repositories.resolve_pinnacle_close_snaps re-keys via selection_map).
        rows: list[OddsSnapshotIn] = []
        for selection, price, size in (
            (home, odds.home_back, odds.home_back_size),
            (away, odds.away_back, odds.away_back_size),
            ("Draw", odds.draw_back, odds.draw_back_size),
        ):
            if price is None or not selection:
                continue
            rows.append(
                OddsSnapshotIn(
                    event_id=event_ref,
                    bookmaker=self._bookmaker,
                    market=Market.H2H,
                    selection=selection,
                    decimal_odds=price,
                    liquidity=size,  # best-back available £ — gated Betfair when promoted
                    captured_at=now,  # listMarketBook is a live read; provider time ~ now
                    ingested_at=now,
                )
            )
        return rows

    def _extended_snapshots_for(
        self,
        quotes: Sequence[BetfairLineQuote],
        home: str,
        away: str,
        event_ref: str,
        now: datetime,
    ) -> list[OddsSnapshotIn]:
        """Snapshot rows for one matched event's extended line quotes.

        Selection strings speak the CANONICAL matched candidate's vocabulary
        (exactly like the h2h path — the Betfair runner names never leak):
        SPREADS emit the scraped "{team} {signed-line}" form ("Alpha -0.25" /
        "Beta +0.25"), TOTALS the scraped "Over/Under {line:g}" form."""
        rows: list[OddsSnapshotIn] = []
        for quote in quotes:
            if quote.market is Market.SPREADS:
                if not home or not away:
                    continue
                if quote.side == "home":
                    selection = f"{home} {quote.line:+g}"
                else:
                    selection = f"{away} {-quote.line:+g}"
            else:
                word = "Over" if quote.side == "over" else "Under"
                selection = f"{word} {quote.line:g}"
            rows.append(
                OddsSnapshotIn(
                    event_id=event_ref,
                    bookmaker=self._bookmaker,
                    market=quote.market,
                    market_detail=quote.market_detail,
                    selection=selection,
                    decimal_odds=quote.back,
                    liquidity=quote.back_size,  # best-back available £, as h2h
                    captured_at=now,
                    ingested_at=now,
                )
            )
        return rows

    async def _fetch_extended_quotes(
        self,
        query_now: datetime,
        odds: Sequence[BetfairMatchOdds],
        matched_event_ids: Sequence[str],
    ) -> tuple[dict[str, list[BetfairLineQuote]], bool]:
        """({betfair_event_id: line quotes}, failed) for the extended markets
        of the MATCHED Betfair events only (eventIds-filtered catalogue — see
        the live-defect note on ``fetch_extended_line_books``). No matched
        events -> no network call at all.

        A fetch/RPC failure is FLAGGED (WARNING, type-only — never a URL or
        token) and returns failed=True, but never kills the h2h anchor capture:
        the extended feed is an add-on, the h2h anchor is live-promoted."""
        if not self._extended_markets or not matched_event_ids:
            return {}, False
        try:
            catalogue, books = await self._client.fetch_extended_line_books(
                market_start_from=query_now,
                market_start_to=query_now + self._window,
                event_ids=matched_event_ids,
                event_type_ids=self._event_type_ids,
            )
        except (httpx.HTTPError, BetfairApiError) as exc:
            logger.warning("betfair api EXTENDED capture FAILED: %s", type(exc).__name__)
            return {}, True
        home_away = {m.event_id: (m.home, m.away) for m in odds if m.home and m.away}
        quotes_by_event: dict[str, list[BetfairLineQuote]] = {}
        for quote in join_extended_lines(catalogue, books, home_away):
            quotes_by_event.setdefault(quote.event_id, []).append(quote)
        return quotes_by_event, False

    async def capture_once(self) -> BetfairApiShadowReport:
        """Run one shadow cycle. RPC/auth errors propagate to the caller (the
        scheduler logs type-only and skips) — never swallowed as a silent
        success. An empty Betfair window yields an honest zero report.

        When a reference loader is wired, each MATCHED event is compared against
        the existing OddsPortal-sourced "betfair exchange" anchor (per-selection
        delta + freshness gap) and the per-cycle roll-up is logged. When the
        default-OFF promotion flag is enabled, the sharp-tagged rows are routed to
        the anchor sink; otherwise nothing is persisted (measurement only)."""
        query_now = self._now_fn()
        odds = await self._client.fetch_match_odds(
            market_start_from=query_now,
            market_start_to=query_now + self._window,
            event_type_ids=self._event_type_ids,
        )
        # Capture only after the complete catalogue/book response sequence.
        # This is conservative: a slow request that crosses kickoff can never be
        # stamped with its pre-request time and promoted as a pre-match anchor.
        observed_at = self._now_fn()
        candidates = list(await self._candidates())
        matched = 0
        unmatched = 0
        snapshots: list[OddsSnapshotIn] = []
        teams_by_event: dict[str, EventTeams] = {}
        matched_pairs: list[tuple[BetfairMatchOdds, str]] = []
        matched_hits: list[tuple[BetfairMatchOdds, EventCandidate]] = []
        link_observations: list[SourceLinkObservation] = []
        for market in odds:
            if not market.home or not market.away or market.kickoff is None:
                unmatched += 1
                continue
            if market.kickoff <= observed_at:
                unmatched += 1
                continue
            # REUSE the hardened matcher verbatim (the SCORED variant — identical
            # accept/reject, plus confidence provenance for event_source_links).
            # league is left None: Betfair competition names do not normalize-equal
            # OddsPortal league names, so passing them would FALSE-BLOCK every
            # market; name + tight kickoff window + ambiguity guard carry
            # precision. Unmatched -> skipped, never guessed (a wrong attach would
            # be fake CLV). ``name_fold`` (tennis: canonical_tennis_name) folds
            # the Betfair runner names into the candidates' name space at match
            # time only — raw names still ride the link observations.
            match_home = self._name_fold(market.home) if self._name_fold else market.home
            match_away = self._name_fold(market.away) if self._name_fold else market.away
            outcome = match_event_hardened_scored(
                match_home,
                match_away,
                market.kickoff,
                candidates,
                aliases=self._aliases,
                ordered=self._ordered,
                league=None,
            )
            if outcome is None:
                unmatched += 1
                continue
            hit = outcome.candidate
            matched += 1
            snapshots.extend(self._snapshots_for(market, hit.home, hit.away, hit.ref, observed_at))
            matched_hits.append((market, hit))
            # Teams for the (only-when-promoting) attach-only persist; sourced from
            # the matched canonical candidate, never the Betfair competition name.
            teams_by_event[hit.ref] = EventTeams(
                home=hit.home, away=hit.away, starts_at=hit.kickoff
            )
            matched_pairs.append((market, hit.ref))
            # OBSERVABILITY (event_source_links): persist the Betfair stable ids
            # (event_id/market_id — previously thrown away) + the match score.
            if self._link_sink is not None:
                link_observations.append(
                    SourceLinkObservation(
                        source="betfair_api",
                        source_event_id=market.event_id,
                        source_market_id=market.market_id,
                        canonical_external_ref=hit.ref,
                        confidence=outcome.confidence,
                        method=outcome.method,
                        matched_at=observed_at,
                        raw_league=market.competition,
                        raw_home=market.home,
                        raw_away=market.away,
                        raw_start_time_utc=market.kickoff,
                    )
                )

        # EXTENDED markets (default OFF): fetched AFTER matching, filtered to
        # the MATCHED Betfair event ids (live defect 2026-08-03 — an unfiltered
        # window-wide catalogue truncates at 200 results to the ~18 soonest
        # events and never intersects the matched slate). Still exactly ONE
        # catalogue call + one batched book pass; zero calls when nothing
        # matched. Rows are stamped POST-extended-fetch and re-guarded against
        # kickoff, the same conservative pre-match rule as the h2h rows.
        extended_by_event, extended_failed = await self._fetch_extended_quotes(
            query_now, odds, [market.event_id for market, _ in matched_hits if market.event_id]
        )
        # Post-extended-fetch stamp; taken only when the fetch produced quotes
        # (flag off / nothing matched / empty slate never consumes now_fn).
        ext_observed_at = self._now_fn() if extended_by_event else observed_at
        extended_groups: set[tuple[str, Market, str]] = set()
        extended_event_refs: set[str] = set()
        for market, hit in matched_hits:
            if market.kickoff is None or market.kickoff <= ext_observed_at:
                continue  # crossed kickoff during the extended fetch: never a pre-match row
            ext_rows = self._extended_snapshots_for(
                extended_by_event.get(market.event_id, ()),
                hit.home,
                hit.away,
                hit.ref,
                ext_observed_at,
            )
            if not ext_rows:
                continue
            snapshots.extend(ext_rows)
            extended_event_refs.add(hit.ref)
            for row in ext_rows:
                if row.market_detail is not None:
                    extended_groups.add((hit.ref, row.market, row.market_detail))

        await self._record_links(link_observations)
        comparison = await self._compare(matched_pairs, observed_at)
        report = BetfairApiShadowReport(
            markets_fetched=len(odds),
            matched=matched,
            unmatched=unmatched,
            snapshots=tuple(snapshots),
            comparison=comparison,
            promoted=self._promote,
            candidates_considered=len(candidates),
            extended_lines=len(extended_groups),
            extended_events=len(extended_event_refs),
            extended_failed=extended_failed,
        )
        await self._maybe_promote(snapshots, teams_by_event)
        self._log(report)
        return report

    async def _record_links(self, observations: Sequence[SourceLinkObservation]) -> None:
        """Best-effort observability: route accepted-match link observations to
        the (optional) sink. NEVER breaks the capture — failure logs the
        exception type only (no ids/URLs in the error path)."""
        if self._link_sink is None or not observations:
            return
        try:
            await self._link_sink(observations)
        except Exception as exc:
            logger.warning("betfair api link sink failed: %s", type(exc).__name__)

    async def _compare(
        self, matched_pairs: Sequence[tuple[BetfairMatchOdds, str]], now: datetime
    ) -> ComparisonAggregate | None:
        if self._reference_odds_fn is None or not matched_pairs:
            return None
        events: list[EventComparison] = []
        verdicts: list[AnchorVerdictObservation] = []
        for market, ref in matched_pairs:
            reference = await self._reference(ref)
            if reference is None:
                continue  # no existing anchor yet — nothing to compare against
            cmp = compare_event(market, reference, api_captured_at=now, event_ref=ref)
            events.append(cmp)
            verdicts.extend(
                self._verdicts_for(market, reference, cmp, api_captured_at=now, event_ref=ref)
            )
            logger.info(
                "betfair api COMPARE %s: dHome=%s dDraw=%s dAway=%s fresh_gap=%ss",
                ref,
                _fmt_delta(cmp.home.delta),
                _fmt_delta(cmp.draw.delta),
                _fmt_delta(cmp.away.delta),
                _fmt_gap(cmp.freshness_gap_seconds),
            )
        await self._record_verdicts(verdicts)
        return ComparisonAggregate.from_events(events)

    def _verdicts_for(
        self,
        odds: BetfairMatchOdds,
        reference: ReferenceOdds,
        cmp: EventComparison,
        *,
        api_captured_at: datetime,
        event_ref: str,
    ) -> list[AnchorVerdictObservation]:
        """Per-selection staleness verdicts for one compared event (PURE build).

        Decision semantics live in ``verdict_decision``; the tick distance is
        the pure ``app.edge.betfair_ticks.tick_distance`` at the coarser price.
        The API best-back SIZE rides along so the verdict table doubles as the
        empirical liquidity cross-check the volume-semantics decision asks for."""
        sizes = {
            "home": odds.home_back_size,
            "draw": odds.draw_back_size,
            "away": odds.away_back_size,
        }
        out: list[AnchorVerdictObservation] = []
        for sel in cmp.selections:
            out.append(
                AnchorVerdictObservation(
                    event_ref=event_ref,
                    market="h2h",
                    selection_role=sel.selection,
                    inline_price=sel.ref_price,
                    api_price=sel.api_price,
                    api_best_back_size=sizes.get(sel.selection),
                    tick_diff=tick_distance(sel.ref_price, sel.api_price),
                    inline_captured_at=reference.captured_at,
                    api_captured_at=api_captured_at,
                    decision=verdict_decision(
                        sel.ref_price, sel.api_price, max_ticks=self._verdict_ticks
                    ),
                )
            )
        return out

    async def _record_verdicts(self, observations: Sequence[AnchorVerdictObservation]) -> None:
        """Best-effort observability: route staleness verdicts to the (optional)
        sink. NEVER breaks the capture — failure logs the exception type only
        (no ids/URLs in the error path). Exactly the LinkSink pattern."""
        if self._verdict_sink is None or not observations:
            return
        try:
            await self._verdict_sink(observations)
        except Exception as exc:
            logger.warning("betfair api verdict sink failed: %s", type(exc).__name__)

    async def _maybe_promote(
        self, snapshots: Sequence[OddsSnapshotIn], teams_by_event: Mapping[str, EventTeams]
    ) -> int:
        # INERT unless promotion is explicitly enabled AND a sink is wired.
        if not self._promote or self._promote_sink is None or not snapshots:
            return 0
        written = await self._promote_sink(snapshots, teams_by_event)
        logger.info(
            "betfair api PROMOTE: routed %d API rows to the live '%s' sharp anchor (%d new)",
            len(snapshots),
            PROMOTED_BOOKMAKER,
            written,
        )
        return written

    def _log(self, report: BetfairApiShadowReport) -> None:
        persisted = "promoted to the live sharp anchor" if report.promoted else "persisted nothing"
        if self._extended_markets:
            # Counts only — never team names, refs, or prices in this line.
            logger.info(
                "betfair api EXTENDED: lines=%d events=%d%s",
                report.extended_lines,
                report.extended_events,
                " (fetch FAILED this cycle)" if report.extended_failed else "",
            )
        cmp = report.comparison
        if cmp is not None and cmp.compared:
            logger.info(
                "betfair api SHADOW [%s]: fetched=%d matched=%d unmatched=%d match_rate=%.1f%% "
                "slate=%d slate_coverage=%.1f%% "
                "would-be-anchor-rows=%d | COMPARE compared=%d mean|delta|=%s within1tick=%s%% "
                "api_fresher=%s%% (%s — measure before trusting)",
                self._label,
                report.markets_fetched,
                report.matched,
                report.unmatched,
                report.match_rate * 100.0,
                report.candidates_considered,
                report.slate_coverage * 100.0,
                len(report.snapshots),
                cmp.compared,
                _fmt_num(cmp.mean_abs_delta),
                _fmt_num(cmp.pct_within_one_tick, "%.0f"),
                _fmt_num(cmp.pct_api_fresher, "%.0f"),
                persisted,
            )
            return
        logger.info(
            "betfair api SHADOW [%s]: fetched=%d matched=%d unmatched=%d match_rate=%.1f%% "
            "slate=%d slate_coverage=%.1f%% "
            "would-be-anchor-rows=%d (%s — measure before trusting)",
            self._label,
            report.markets_fetched,
            report.matched,
            report.unmatched,
            report.match_rate * 100.0,
            report.candidates_considered,
            report.slate_coverage * 100.0,
            len(report.snapshots),
            persisted,
        )


def build_shadow_capture(
    *,
    enabled: bool,
    credentials: tuple[str, str, str] | None,
    window_hours: int,
    http_client: httpx.AsyncClient,
    candidates_fn: CandidatesFn,
    aliases: AliasTable | None = None,
    event_type_ids: Sequence[str] = (EVENT_TYPE_SOCCER,),
    now_fn: Callable[[], datetime] | None = None,
    reference_odds_fn: ReferenceOddsFn | None = None,
    promote: bool = False,
    promote_sink: PromoteSink | None = None,
    link_sink: LinkSink | None = None,
    verdict_sink: VerdictSink | None = None,
    verdict_ticks: float = 1.0,
    extended_markets: bool = False,
    ordered: bool = True,
    name_fold: Callable[[str], str] | None = None,
    label: str = "soccer",
    api_client: BetfairApiClient | None = None,
) -> BetfairApiShadowCapture | None:
    """Build the shadow capture, or None when the integration is INERT — i.e.
    disabled OR any credential blank. None means the scheduler adds NO job and no
    login/network ever happens (req #3).

    ``reference_odds_fn`` (optional) wires the price comparison against the
    existing OddsPortal-sourced "betfair exchange" anchor. ``promote`` is the
    DEFAULT-OFF promotion flag; when false the capture is a measurement-only
    shadow (non-sharp rows, nothing persisted) and ``promote_sink`` is ignored.
    ``verdict_sink`` (optional, best-effort) persists per-selection staleness
    verdicts (betfair_anchor_verdicts) computed at ``verdict_ticks``."""
    if not enabled or credentials is None:
        return None
    if api_client is not None:
        # SHARED read-only session across per-sport captures (2026-08-03): a
        # second interactive login can invalidate the first session token, so
        # the composition root builds ONE BetfairApiClient and injects it.
        client = api_client
    else:
        app_key, username, password = credentials
        client = BetfairApiClient(
            app_key=app_key, username=username, password=password, client=http_client
        )
    return BetfairApiShadowCapture(
        client,
        candidates_fn=candidates_fn,
        window=timedelta(hours=window_hours),
        aliases=aliases,
        event_type_ids=event_type_ids,
        now_fn=now_fn,
        reference_odds_fn=reference_odds_fn,
        promote=promote,
        promote_sink=promote_sink,
        link_sink=link_sink,
        verdict_sink=verdict_sink,
        verdict_ticks=verdict_ticks,
        extended_markets=extended_markets,
        ordered=ordered,
        name_fold=name_fold,
        label=label,
    )
