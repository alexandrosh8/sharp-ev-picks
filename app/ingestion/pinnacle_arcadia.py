"""Clean-room, read-only Pinnacle sharp-line capture (arcadia guest JSON API).

GET-only PUBLIC market data — NO account, NO login, NO stored credentials, NO
order placement. The optional guest ``x-api-key`` is Pinnacle's own PUBLIC
web-client constant (authenticates no user); the two endpoints used here
require none, so the key defaults to empty and is never committed.

Purpose: build a FREE live-Pinnacle CLOSING-line archive — the project's
biggest documented data gap (``docs/research/free-odds-sources.md``,
ADR-0013). The capture job runs INDEPENDENTLY, alongside whatever
``ODDS_SOURCE`` is active; it never replaces it and mints NO picks/alerts.
Captured rows land under the isolated ``pinnacle_<sport>`` warehouse namespace
(``bookmaker="Pinnacle"``) so they never pollute the live dashboard/pick path,
and ``closing_odds_from_snapshots`` can later reconstruct the Pinnacle close.

Clean-room provenance: API facts (endpoint paths, the market ``key`` scheme,
sport ids, the American->decimal formula) were extracted from the LIVE public
API and cross-checked against public references; NO code was copied from any
(unlicensed) scraper repo. See ADR-0013.

Read-only safety: this module performs GET requests only and contains no
bet-placement, bookmaker-login, or credential-storage code (ADR-0002).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://guest.api.arcadia.pinnacle.com/0.1"
BOOKMAKER = "Pinnacle"  # contains "pinnacle" -> top-priority sharp anchor (value.py)

# arcadia numeric sport ids (verified live against /0.1/sports). "american_
# football" is id 15 ("Football" upstream); soccer is the distinct id 29.
SPORT_IDS: dict[str, int] = {
    "soccer": 29,
    "tennis": 33,
    "basketball": 4,
    "american_football": 15,
}

# The match/game-winner market: straight (s), period 0 (full event), moneyline
# (m). period>=1 is halves/quarters/sets — never the match winner.
_MONEYLINE_KEY = "s;0;m"


class PinnacleArcadiaError(Exception):
    """Non-retryable fetch failure. Message never contains the URL or key."""


def american_to_decimal(price: int) -> float:
    """Convert American moneyline odds to European decimal odds (> 1.0)."""
    if price == 0:
        raise ValueError("american price cannot be 0")
    if price > 0:
        return 1.0 + price / 100.0
    return 1.0 + 100.0 / abs(price)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _parse_ts(raw: str) -> datetime | None:
    """Parse arcadia ISO-8601 timestamps (Z suffix or +00:00 offset) to aware
    UTC. Returns None on anything unparseable or naive."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class _Matchup:
    """Event/team context for one upcoming arcadia matchup."""

    event_id: str
    home: str
    away: str
    league: str
    starts_at: datetime


@dataclass(frozen=True)
class MoneylineQuote:
    """One period-0 moneyline observation for an event: the per-selection
    snapshots plus Pinnacle's monotonic market ``version`` (the change-gate)."""

    event_id: str
    version: int
    snapshots: tuple[OddsSnapshotIn, ...]


def parse_matchups(
    raw: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    horizon_end: datetime,
) -> dict[str, _Matchup]:
    """Top-level upcoming matchups within (now, horizon_end], keyed by id.

    Drops props/outrights (``type != "matchup"``), derived sub-matchups
    (``parent`` set), two-sided events missing a home/away participant, and
    anything already started or beyond the horizon.
    """
    out: dict[str, _Matchup] = {}
    for matchup in raw:
        if matchup.get("type") != "matchup":
            continue
        if matchup.get("parent") is not None or matchup.get("parentId") is not None:
            continue
        participants = matchup.get("participants") or []
        home = next((p.get("name") for p in participants if p.get("alignment") == "home"), None)
        away = next((p.get("name") for p in participants if p.get("alignment") == "away"), None)
        if not home or not away:
            continue
        starts_at = _parse_ts(str(matchup.get("startTime", "")))
        if starts_at is None or starts_at <= now or starts_at > horizon_end:
            continue
        league = str((matchup.get("league") or {}).get("name") or "")
        event_id = str(matchup.get("id"))
        out[event_id] = _Matchup(
            event_id=event_id,
            home=str(home),
            away=str(away),
            league=league,
            starts_at=starts_at,
        )
    return out


def _selection_for(designation: str, matchup: _Matchup) -> str | None:
    if designation == "home":
        return matchup.home
    if designation == "away":
        return matchup.away
    if designation == "draw":
        return "Draw"
    return None  # participantId-keyed / unexpected designation: skip


def extract_moneyline_quotes(
    matchups: Mapping[str, _Matchup],
    markets: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[MoneylineQuote]:
    """Period-0 moneyline (match/game winner) quotes joined to their matchups.

    Skips non-``s;0;m`` keys, non-open markets, markets for events outside the
    matchup window, and participantId-keyed multiway prices (no designation).
    Prices are American integers -> European decimal; soccer carries a draw.
    """
    quotes: list[MoneylineQuote] = []
    for market in markets:
        if market.get("key") != _MONEYLINE_KEY:
            continue
        if market.get("type") != "moneyline" or market.get("period") != 0:
            continue
        if market.get("status") != "open":
            continue
        event_id = str(market.get("matchupId"))
        matchup = matchups.get(event_id)
        if matchup is None:
            continue
        snapshots: list[OddsSnapshotIn] = []
        for price in market.get("prices") or []:
            designation = price.get("designation")
            raw_price = price.get("price")
            if designation is None:
                continue  # participantId-keyed multiway leg
            if not isinstance(raw_price, int) or isinstance(raw_price, bool):
                continue
            selection = _selection_for(str(designation), matchup)
            if selection is None:
                continue
            decimal_odds = american_to_decimal(raw_price)
            if decimal_odds <= 1.0:
                continue
            snapshots.append(
                OddsSnapshotIn(
                    event_id=event_id,
                    bookmaker=BOOKMAKER,
                    market=Market.H2H,
                    selection=selection,
                    decimal_odds=decimal_odds,
                    captured_at=now,
                    ingested_at=now,
                )
            )
        if snapshots:
            version = market.get("version")
            # A versionless market cannot be change-gated; capturing it once
            # would freeze it (a synthesized 0 gates every later move). Skip and
            # retry next cycle — arcadia's version is effectively always present.
            if not isinstance(version, int) or isinstance(version, bool):
                continue
            quotes.append(
                MoneylineQuote(
                    event_id=event_id,
                    version=version,
                    snapshots=tuple(snapshots),
                )
            )
    return quotes


class _MarketSource(Protocol):
    async def fetch_matchups(self, sport_id: int) -> list[dict[str, Any]]: ...
    async def fetch_straight_markets(self, sport_id: int) -> list[dict[str, Any]]: ...


class _RoundRobinTransport(httpx.AsyncBaseTransport):
    """Route each request through the next configured proxy transport."""

    def __init__(self, transports: Sequence[httpx.AsyncBaseTransport]) -> None:
        if not transports:
            raise ValueError("at least one transport is required")
        self._transports = tuple(transports)
        self._next_index = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._transports[self._next_index % len(self._transports)]
        self._next_index += 1
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        for transport in self._transports:
            await transport.aclose()


def build_arcadia_proxy_http_client(proxy_urls: Sequence[str]) -> httpx.AsyncClient:
    """Build an Arcadia-only client with rotating outbound proxies.

    The proxy URLs may contain credentials. They must never be logged or stored
    in tracked files; callers pass them from Settings/.env only.
    """
    transports = tuple(
        httpx.AsyncHTTPTransport(proxy=proxy_url, trust_env=False) for proxy_url in proxy_urls
    )
    return httpx.AsyncClient(transport=_RoundRobinTransport(transports), trust_env=False)


class PinnacleArcadiaClient:
    """Read-only client for the public Pinnacle guest JSON API. GET-only.

    Secret hygiene mirrors OddsApiClient: the optional guest key travels only
    in an outbound header; it is NEVER placed in exceptions or logs, and the
    request URL (which would identify the source) is never stringified into an
    error. Transport errors retry with backoff; non-200 fails without retry.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = DEFAULT_BASE_URL,
        guest_key: str = "",
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._guest_key = guest_key

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self._guest_key:
            headers["x-api-key"] = self._guest_key
        return headers

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        return await self._client.get(
            f"{self._base_url}{path}", params=params, headers=self._headers(), timeout=20.0
        )

    async def _fetch_list(
        self, path: str, params: dict[str, str] | None, what: str, sport_id: int
    ) -> list[dict[str, Any]]:
        response = await self._get(path, params)
        if response.status_code != 200:
            # Never include response.url (identifies the source / carries the
            # key when set) in the error — status + sport id only.
            raise PinnacleArcadiaError(
                f"pinnacle {what} returned status {response.status_code} for sport={sport_id}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            # A 200 with a non-JSON body (anti-bot/HTML interstitial — an
            # expected scrape gap) must raise OUR error type so capture_once's
            # per-sport isolation holds and one sport's gap never aborts the
            # rest. Message carries status/type only, never the URL or key.
            raise PinnacleArcadiaError(
                f"pinnacle {what} returned a non-JSON body for sport={sport_id}"
            ) from exc
        return data if isinstance(data, list) else []

    async def fetch_matchups(self, sport_id: int) -> list[dict[str, Any]]:
        return await self._fetch_list(f"/sports/{sport_id}/matchups", None, "matchups", sport_id)

    async def fetch_straight_markets(self, sport_id: int) -> list[dict[str, Any]]:
        return await self._fetch_list(
            f"/sports/{sport_id}/markets/straight",
            {"primaryOnly": "false"},
            "markets",
            sport_id,
        )


class PinnacleArcadiaCapture:
    """Forward closing-line capture: persists Pinnacle period-0 moneyline
    snapshots into the ``pinnacle_<sport>`` warehouse namespace.

    Change-gated on Pinnacle's monotonic per-market ``version`` (mirrors the
    pipeline's change-only cache): a snapshot is written only when a market
    reprices, so the latest pre-kickoff row IS that event's sharp close.
    captured_at is our observation time (the public feed exposes no per-price
    timestamp); the version-gate makes it approximate the reprice instant.
    The in-memory gate resets on restart -> the next cycle re-emits each still-
    open market's current line once with a fresh captured_at, writing ONE
    duplicate observation row per market per restart (the unique key includes
    captured_at, so it does not dedupe these). Identical odds; benign for CLV
    (closing_odds_from_snapshots takes the latest pre-kickoff row) — just mild,
    bounded bloat.
    """

    def __init__(
        self,
        client: _MarketSource | None,
        session_factory: async_sessionmaker | None,
        *,
        sports: Sequence[str],
        horizon: timedelta,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._sports = tuple(sports)
        self._horizon = horizon
        self._now_fn = now_fn or _utc_now
        self._seen_version: dict[tuple[str, str], int] = {}

    def _select_fresh(
        self,
        quotes: Sequence[MoneylineQuote],
        matchups: Mapping[str, _Matchup],
        sport: str,
    ) -> tuple[list[OddsSnapshotIn], dict[str, EventTeams]]:
        """Filter quotes to those whose market version advanced since last seen;
        return their snapshots plus the team context needed to persist them."""
        fresh: list[OddsSnapshotIn] = []
        teams: dict[str, EventTeams] = {}
        for quote in quotes:
            seen_key = (sport, quote.event_id)
            if self._seen_version.get(seen_key, -1) >= quote.version:
                continue
            self._seen_version[seen_key] = quote.version
            fresh.extend(quote.snapshots)
            matchup = matchups.get(quote.event_id)
            if matchup is not None:
                teams[quote.event_id] = EventTeams(
                    home=matchup.home,
                    away=matchup.away,
                    league=matchup.league,
                    starts_at=matchup.starts_at,
                )
        return fresh, teams

    async def capture_once(self) -> dict[str, int]:
        """One capture cycle across all configured sports. Returns new rows
        written per sport. A failed sport is logged (type + status only, never
        the URL/key) and skipped — never aborts the other sports."""
        if self._client is None:
            return {sport: 0 for sport in self._sports}

        from app.storage.repositories import persist_odds_snapshots

        now = self._now_fn()
        horizon_end = now + self._horizon
        written: dict[str, int] = {}
        for sport in self._sports:
            sport_id = SPORT_IDS.get(sport)
            if sport_id is None:
                logger.warning("pinnacle arcadia: unknown sport %r; skipping", sport)
                continue
            try:
                raw_matchups = await self._client.fetch_matchups(sport_id)
                raw_markets = await self._client.fetch_straight_markets(sport_id)
            except (httpx.HTTPError, PinnacleArcadiaError) as exc:
                logger.warning(
                    "pinnacle arcadia fetch failed for %s: %s", sport, type(exc).__name__
                )
                continue
            matchups = parse_matchups(raw_matchups, now=now, horizon_end=horizon_end)
            quotes = extract_moneyline_quotes(matchups, raw_markets, now=now)
            fresh, teams = self._select_fresh(quotes, matchups, sport)
            if not fresh or self._session_factory is None:
                written[sport] = 0
                continue
            namespace = f"pinnacle_{sport}"
            rows = await persist_odds_snapshots(
                self._session_factory,
                fresh,
                teams,
                sport=namespace,
                default_league=namespace,
            )
            written[sport] = rows
            if rows:
                logger.info(
                    "pinnacle arcadia: %s captured %d new sharp rows (%d events)",
                    sport,
                    rows,
                    len(teams),
                )
        return written
