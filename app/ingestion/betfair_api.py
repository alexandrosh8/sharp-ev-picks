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

from app.ingestion.base import EventTeams
from app.resolution.matching import (
    AliasTable,
    EventCandidate,
    default_aliases,
    match_event_hardened,
)
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

logger = logging.getLogger(__name__)

# --- Betfair endpoints (the ONLY URLs this module ever touches) -------------- #
IDENTITY_LOGIN_URL = "https://identitysso.betfair.com/api/login"
IDENTITY_KEEPALIVE_URL = "https://identitysso.betfair.com/api/keepAlive"
IDENTITY_LOGOUT_URL = "https://identitysso.betfair.com/api/logout"
JSON_RPC_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"

# JSON-RPC operation names — the READ-ONLY allowlist, and nothing else. (Each is
# a price/metadata read; none place a bet or touch a betting account.)
_RPC_PREFIX = "SportsAPING/v1.0/"
_OP_LIST_EVENT_TYPES = "listEventTypes"
_OP_LIST_COMPETITIONS = "listCompetitions"
_OP_LIST_EVENTS = "listEvents"
_OP_LIST_MARKET_CATALOGUE = "listMarketCatalogue"
_OP_LIST_MARKET_BOOK = "listMarketBook"
# listMarketBook caps at 200 weight-points/request; EX_BEST_OFFERS ~5/market, so
# batch <=25 markets/call (~125 weight) to stay under the cap (else TOO_MUCH_DATA).
_MARKET_BOOK_BATCH = 25

# Soccer event type; basketball ("7522") can be added later (req #1).
EVENT_TYPE_SOCCER = "1"
EVENT_TYPE_BASKETBALL = "7522"
MARKET_TYPE_MATCH_ODDS = "MATCH_ODDS"
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


# --- price-comparison math (PURE: numpy/stdlib-free, no IO) ------------------ #
# The Betfair price-increment ("tick") ladder. The minimum quotable gap widens as
# the price climbs; "within one tick" uses the COARSER (higher) of the two prices
# so a near-agreement is never overstated. Source: Betfair price increments table.
_TICK_LADDER: tuple[tuple[float, float], ...] = (
    (2.0, 0.01),
    (3.0, 0.02),
    (4.0, 0.05),
    (6.0, 0.10),
    (10.0, 0.20),
    (20.0, 0.50),
    (30.0, 1.00),
    (50.0, 2.00),
    (100.0, 5.00),
    (1000.0, 10.0),
)


def betfair_tick_size(price: float) -> float:
    """The Betfair minimum price increment at ``price`` (the exchange tick)."""
    for upper, tick in _TICK_LADDER:
        if price < upper:
            return tick
    return 10.0


def within_one_tick(a: float | None, b: float | None) -> bool | None:
    """True when two BACK prices are within one exchange tick of each other.

    None when EITHER price is missing — an absent price is undefined, never a
    silent "agree". The tick is taken at the coarser (higher) price so the test
    is conservative (a wider band never inflates the agreement rate)."""
    if a is None or b is None:
        return None
    tick = betfair_tick_size(max(a, b))
    return abs(a - b) <= tick + 1e-9


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


def _best_back(available_to_back: Any) -> float | None:
    """Best (highest) BACK price from a runner's ``ex.availableToBack`` ladder, or
    None when the ladder is empty/garbled or only holds non-prices (<=1.0)."""
    if not isinstance(available_to_back, Sequence):
        return None
    best: float | None = None
    for level in available_to_back:
        if not isinstance(level, Mapping):
            continue
        price = level.get("price")
        if not isinstance(price, int | float) or price <= 1.0:
            continue
        value = float(price)
        if best is None or value > best:
            best = value
    return best


@dataclass(frozen=True)
class BetfairRunner:
    """One Match-Odds runner from the catalogue (no price)."""

    selection_id: int
    name: str
    sort_priority: int


@dataclass(frozen=True)
class BetfairMarketCatalogue:
    """One ``listMarketCatalogue`` market with EVENT / COMPETITION /
    MARKET_START_TIME / RUNNER_DESCRIPTION projections."""

    market_id: str
    event_id: str
    event_name: str
    competition: str
    market_start_time: datetime | None
    runners: tuple[BetfairRunner, ...]


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
            )
        )
    return out


def parse_market_book_backs(payload: Sequence[Mapping[str, Any]]) -> dict[str, dict[int, float]]:
    """Pure parser for a ``listMarketBook`` result array (EX_BEST_OFFERS) ->
    ``{market_id: {selection_id: best_back_price}}``. Runners with no backable
    price are omitted (never invented)."""
    books: dict[str, dict[int, float]] = {}
    for market in payload:
        if not isinstance(market, Mapping):
            continue
        market_id = str(market.get("marketId", "")).strip()
        if not market_id:
            continue
        per_runner: dict[int, float] = {}
        for runner in market.get("runners") or []:
            if not isinstance(runner, Mapping):
                continue
            sel = runner.get("selectionId")
            if not isinstance(sel, int):
                continue
            ex_raw = runner.get("ex")
            ex: Mapping[str, Any] = ex_raw if isinstance(ex_raw, Mapping) else {}
            best = _best_back(ex.get("availableToBack"))
            if best is not None:
                per_runner[sel] = best
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
    backs: Mapping[str, Mapping[int, float]],
) -> list[BetfairMatchOdds]:
    """Join catalogue runner identities with their best BACK prices into
    ``BetfairMatchOdds``. A market with no resolvable home/away runner is skipped
    (it is not a usable Match-Odds market)."""
    out: list[BetfairMatchOdds] = []
    for market in catalogue:
        home, away, draw = _roles(market.runners)
        if home is None or away is None:
            continue
        per_runner = backs.get(market.market_id, {})
        out.append(
            BetfairMatchOdds(
                market_id=market.market_id,
                event_id=market.event_id,
                competition=market.competition,
                kickoff=market.market_start_time,
                home=home.name,
                away=away.name,
                home_back=per_runner.get(home.selection_id),
                away_back=per_runner.get(away.selection_id),
                draw_back=per_runner.get(draw.selection_id) if draw is not None else None,
            )
        )
    return out


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
        return await self._client.post(url, json=json, data=data, headers=headers, timeout=_TIMEOUT)

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def _get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        return await self._client.get(url, headers=headers, timeout=_TIMEOUT)

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
        needed and re-logs-in EXACTLY ONCE on a session-expiry errorCode."""
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
    async def list_event_types(self, *, event_type_ids: Sequence[str] | None = None) -> Any:
        market_filter = {"eventTypeIds": list(event_type_ids)} if event_type_ids else {}
        return await self._rpc(_OP_LIST_EVENT_TYPES, {"filter": market_filter})

    async def list_competitions(self, *, event_type_ids: Sequence[str]) -> Any:
        return await self._rpc(
            _OP_LIST_COMPETITIONS, {"filter": {"eventTypeIds": list(event_type_ids)}}
        )

    async def list_events(
        self,
        *,
        event_type_ids: Sequence[str],
        market_start_from: datetime | None = None,
        market_start_to: datetime | None = None,
    ) -> Any:
        return await self._rpc(
            _OP_LIST_EVENTS,
            {"filter": _build_filter(event_type_ids, [], market_start_from, market_start_to)},
        )

    async def list_market_catalogue(
        self,
        *,
        event_type_ids: Sequence[str],
        market_start_from: datetime,
        market_start_to: datetime,
        market_type_codes: Sequence[str] = (MARKET_TYPE_MATCH_ODDS,),
        max_results: int = 200,
    ) -> list[BetfairMarketCatalogue]:
        result = await self._rpc(
            _OP_LIST_MARKET_CATALOGUE,
            {
                "filter": _build_filter(
                    event_type_ids, market_type_codes, market_start_from, market_start_to
                ),
                "marketProjection": [
                    "EVENT",
                    "COMPETITION",
                    "MARKET_START_TIME",
                    "RUNNER_DESCRIPTION",
                ],
                "maxResults": max_results,
                "sort": "FIRST_TO_START",
            },
        )
        return parse_market_catalogue(result if isinstance(result, list) else [])

    async def list_market_book_backs(
        self, market_ids: Sequence[str]
    ) -> dict[str, dict[int, float]]:
        # Betfair caps listMarketBook at 200 weight-points/request; EX_BEST_OFFERS is
        # ~5/market, so request in batches of <=25 markets (~125 weight) to stay safely
        # under the cap (a single all-markets call returns TOO_MUCH_DATA). Read-only.
        if not market_ids:
            return {}
        ids = list(market_ids)
        out: dict[str, dict[int, float]] = {}
        for start in range(0, len(ids), _MARKET_BOOK_BATCH):
            batch = ids[start : start + _MARKET_BOOK_BATCH]
            result = await self._rpc(
                _OP_LIST_MARKET_BOOK,
                {
                    "marketIds": batch,
                    "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
                },
            )
            out.update(parse_market_book_backs(result if isinstance(result, list) else []))
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


def _build_filter(
    event_type_ids: Sequence[str],
    market_type_codes: Sequence[str],
    market_start_from: datetime | None,
    market_start_to: datetime | None,
) -> dict[str, Any]:
    market_filter: dict[str, Any] = {"eventTypeIds": list(event_type_ids)}
    if market_type_codes:
        market_filter["marketTypeCodes"] = list(market_type_codes)
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

    @property
    def match_rate(self) -> float:
        return self.matched / self.markets_fetched if self.markets_fetched else 0.0


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
    ) -> None:
        self._client = client
        self._candidates_fn = candidates_fn
        self._window = window
        self._aliases = aliases or default_aliases()
        self._event_type_ids = tuple(event_type_ids)
        self._now_fn = now_fn or _utc_now
        self._reference_odds_fn = reference_odds_fn
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
        self, odds: BetfairMatchOdds, event_ref: str, now: datetime
    ) -> list[OddsSnapshotIn]:
        rows: list[OddsSnapshotIn] = []
        for selection, price in (
            (odds.home, odds.home_back),
            (odds.away, odds.away_back),
            ("Draw", odds.draw_back),
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
                    captured_at=now,  # listMarketBook is a live read; provider time ~ now
                    ingested_at=now,
                )
            )
        return rows

    async def capture_once(self) -> BetfairApiShadowReport:
        """Run one shadow cycle. RPC/auth errors propagate to the caller (the
        scheduler logs type-only and skips) — never swallowed as a silent
        success. An empty Betfair window yields an honest zero report.

        When a reference loader is wired, each MATCHED event is compared against
        the existing OddsPortal-sourced "betfair exchange" anchor (per-selection
        delta + freshness gap) and the per-cycle roll-up is logged. When the
        default-OFF promotion flag is enabled, the sharp-tagged rows are routed to
        the anchor sink; otherwise nothing is persisted (measurement only)."""
        now = self._now_fn()
        odds = await self._client.fetch_match_odds(
            market_start_from=now,
            market_start_to=now + self._window,
            event_type_ids=self._event_type_ids,
        )
        candidates = list(await self._candidates())
        matched = 0
        unmatched = 0
        snapshots: list[OddsSnapshotIn] = []
        teams_by_event: dict[str, EventTeams] = {}
        matched_pairs: list[tuple[BetfairMatchOdds, str]] = []
        for market in odds:
            if not market.home or not market.away or market.kickoff is None:
                unmatched += 1
                continue
            # REUSE the hardened matcher verbatim. league is left None: Betfair
            # competition names do not normalize-equal OddsPortal league names, so
            # passing them would FALSE-BLOCK every market; name + tight kickoff
            # window + ambiguity guard carry precision. Unmatched -> skipped, never
            # guessed (a wrong attach would be fake CLV).
            hit = match_event_hardened(
                market.home,
                market.away,
                market.kickoff,
                candidates,
                aliases=self._aliases,
                ordered=True,
                league=None,
            )
            if hit is None:
                unmatched += 1
                continue
            matched += 1
            snapshots.extend(self._snapshots_for(market, hit.ref, now))
            # Teams for the (only-when-promoting) attach-only persist; sourced from
            # the matched canonical candidate, never the Betfair competition name.
            teams_by_event[hit.ref] = EventTeams(
                home=hit.home, away=hit.away, starts_at=hit.kickoff
            )
            matched_pairs.append((market, hit.ref))

        comparison = await self._compare(matched_pairs, now)
        report = BetfairApiShadowReport(
            markets_fetched=len(odds),
            matched=matched,
            unmatched=unmatched,
            snapshots=tuple(snapshots),
            comparison=comparison,
            promoted=self._promote,
        )
        await self._maybe_promote(snapshots, teams_by_event)
        self._log(report)
        return report

    async def _compare(
        self, matched_pairs: Sequence[tuple[BetfairMatchOdds, str]], now: datetime
    ) -> ComparisonAggregate | None:
        if self._reference_odds_fn is None or not matched_pairs:
            return None
        events: list[EventComparison] = []
        for market, ref in matched_pairs:
            reference = await self._reference(ref)
            if reference is None:
                continue  # no existing anchor yet — nothing to compare against
            cmp = compare_event(market, reference, api_captured_at=now, event_ref=ref)
            events.append(cmp)
            logger.info(
                "betfair api COMPARE %s: dHome=%s dDraw=%s dAway=%s fresh_gap=%ss",
                ref,
                _fmt_delta(cmp.home.delta),
                _fmt_delta(cmp.draw.delta),
                _fmt_delta(cmp.away.delta),
                _fmt_gap(cmp.freshness_gap_seconds),
            )
        return ComparisonAggregate.from_events(events)

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
        cmp = report.comparison
        if cmp is not None and cmp.compared:
            logger.info(
                "betfair api SHADOW: fetched=%d matched=%d unmatched=%d match_rate=%.1f%% "
                "would-be-anchor-rows=%d | COMPARE compared=%d mean|delta|=%s within1tick=%s%% "
                "api_fresher=%s%% (%s — measure before trusting)",
                report.markets_fetched,
                report.matched,
                report.unmatched,
                report.match_rate * 100.0,
                len(report.snapshots),
                cmp.compared,
                _fmt_num(cmp.mean_abs_delta),
                _fmt_num(cmp.pct_within_one_tick, "%.0f"),
                _fmt_num(cmp.pct_api_fresher, "%.0f"),
                persisted,
            )
            return
        logger.info(
            "betfair api SHADOW: fetched=%d matched=%d unmatched=%d match_rate=%.1f%% "
            "would-be-anchor-rows=%d (%s — measure before trusting)",
            report.markets_fetched,
            report.matched,
            report.unmatched,
            report.match_rate * 100.0,
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
) -> BetfairApiShadowCapture | None:
    """Build the shadow capture, or None when the integration is INERT — i.e.
    disabled OR any credential blank. None means the scheduler adds NO job and no
    login/network ever happens (req #3).

    ``reference_odds_fn`` (optional) wires the price comparison against the
    existing OddsPortal-sourced "betfair exchange" anchor. ``promote`` is the
    DEFAULT-OFF promotion flag; when false the capture is a measurement-only
    shadow (non-sharp rows, nothing persisted) and ``promote_sink`` is ignored."""
    if not enabled or credentials is None:
        return None
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
    )
