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
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard

import httpx
from pydantic import SecretStr
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
# Public web-client config blob (no auth, no login) — used only when
# arcadia_discover_config is opted in. Carries the guest x-api-key + guest
# base URL + apiVersion. A plain public GET; never a credential/login path.
CONFIG_APP_JSON_URL = "https://www.pinnacle.com/config/app.json"
BOOKMAKER = "Pinnacle"  # contains "pinnacle" -> top-priority sharp anchor (value.py)
# Sent on every arcadia GET (the public web client and the operator-provided R
# reference both set it). A blocked/datacenter egress is 403'd less often with a
# site Referer present; it is NOT an anti-bot bypass (GET-only, no challenge
# solving) and carries no credential, so it is a plain public constant.
_PINNACLE_REFERER = "https://www.pinnacle.com/"

# arcadia numeric sport ids (verified live against /0.1/sports). "american_
# football" is id 15 ("Football" upstream); soccer is the distinct id 29.
# The latter four widen coverage to the sports picks actually span (the 34-
# fixture gap is mostly sport-enumeration on our side, not a missing endpoint —
# docs/research/pinnacle-odds-sources-2026-06-19.md). capture_once still
# INTERSECTS this set with the live /sports feed, so off-season ids cost nothing.
SPORT_IDS: dict[str, int] = {
    "soccer": 29,
    "tennis": 33,
    "basketball": 4,
    "american_football": 15,
    "baseball": 3,
    "hockey": 19,
    "rugby": 27,
    "handball": 18,
}

# Straight (s), period 0 (full event) market keys. period>=1 is
# halves/quarters/sets — never the full-event line, so always filtered out.
#   moneyline:  "s;0;m"            (m)   -> Market.H2H
#   total:      "s;0;ou;<line>"    (ou)  -> Market.TOTALS  (over/under)
#   spread/AH:  "s;0;s;<line>"     (s)   -> Market.SPREADS (home/away handicap)
_MONEYLINE_KEY = "s;0;m"
_TOTAL_PREFIX = "s;0;ou;"
_SPREAD_PREFIX = "s;0;s;"

# Sports whose totals/spreads must use the OddsPortal SOFT source's "_games"
# market_detail namespace so a Pinnacle snapshot GROUPS WITH — and anchors —
# the soft pick. Basketball's soft loader (app/ingestion/oddsportal.py +
# oddsportal_json.py) emits over_under_games_<line> / asian_handicap_games_<line>;
# emitting the BARE over_under_/asian_handicap_ namespace here grouped the sharp
# snapshot APART from the basketball pick (same market+selection, different
# market_detail), so it never anchored (~16 picks). Soccer's soft source emits
# the BARE namespace, so soccer must stay bare — adding it here would un-anchor it.
_GAMES_DETAIL_SPORTS: frozenset[str] = frozenset({"basketball"})


class PinnacleArcadiaError(Exception):
    """Non-retryable fetch failure. Message never contains the URL or key."""


# Transient upstream HTTP statuses worth one or two retries before giving up: a
# rate-limit (429) or a momentary server-side hiccup (5xx). A permanent 4xx
# (400/401/404/422 …) is a real error — retrying it only burns budget, so it
# is deliberately excluded and surfaces immediately as PinnacleArcadiaError.
_TRANSIENT_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# arcadia 403s requests from a blocked/datacenter egress IP but serves the SAME
# request fine through a healthy proxy. The arcadia client rotates proxies
# round-robin per request (build_arcadia_proxy_http_client), so a 403 is treated
# as "this egress is blocked — rotate to the next proxy" and retried like a
# transient status (this is NOT an anti-bot bypass: GET-only through the
# already-configured proxy pool). After all attempts it still surfaces as
# PinnacleArcadiaError(403), so a genuinely-blocked-everywhere fetch is honest.
_PROXY_ROTATE_STATUSES: frozenset[int] = frozenset({403})


class _TransientStatusError(Exception):
    """Internal-only: a retryable transient HTTP status (429/5xx). Carries the
    status code ONLY — never the URL or key — and is converted to the public
    PinnacleArcadiaError once retries are exhausted, so callers see the same
    error type/semantics as before (per-sport isolation/dedupe unchanged)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"transient upstream status {status_code}")
        self.status_code = status_code


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
class ArcadiaConfig:
    """Public guest credentials discovered from ``/config/app.json``.

    ``guest_key`` is a SecretStr-style value: it is Pinnacle's PUBLIC web-client
    constant (authenticates no user), but it must NEVER be logged, committed, or
    appear in any exception/URL — so it is wrapped so ``repr`` redacts it and the
    raw value is reachable only via the explicit ``.get_secret_value()`` accessor.
    """

    guest_key: SecretStr
    base_url: str


async def discover_arcadia_config(
    client: httpx.AsyncClient,
    *,
    url: str = CONFIG_APP_JSON_URL,
    timeout: float = 10.0,
) -> ArcadiaConfig | None:
    """Best-effort public GET of the Pinnacle web-client config blob.

    Reads ``api.haywire.apiKey`` (guest x-api-key), ``routes.curacao.guestRoot``
    (base URL) and ``apiVersion``. Returns an ``ArcadiaConfig`` on success, or
    ``None`` on ANY failure (transport error, non-200, non-JSON, missing keys) so
    the caller falls back to the current empty key + DEFAULT_BASE_URL. A plain
    read-only GET to a PUBLIC file — no login, no session, no anti-bot bypass.

    Secret hygiene: the guest key is never logged or placed in an exception; only
    a success/failure line is emitted, and it carries the failure TYPE only,
    never the URL (which identifies the source) or the key.
    """
    try:
        response = await client.get(
            url,
            headers={"accept": "application/json", "referer": _PINNACLE_REFERER},
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.info(
                "arcadia config discovery: non-200 (%d); using fallback key/base",
                response.status_code,
            )
            return None
        data = response.json()
        api_key = (((data.get("api") or {}).get("haywire") or {}).get("apiKey")) or ""
        guest_root = (((data.get("routes") or {}).get("curacao") or {}).get("guestRoot")) or ""
        if not isinstance(api_key, str) or not api_key:
            logger.info("arcadia config discovery: no apiKey present; using fallback")
            return None
        base_url = guest_root if isinstance(guest_root, str) and guest_root else DEFAULT_BASE_URL
        logger.info("arcadia config discovery: succeeded; refreshed guest key/base")
        return ArcadiaConfig(guest_key=SecretStr(api_key), base_url=base_url)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        # type-only — never the URL (identifies the source) or the key.
        logger.info("arcadia config discovery failed: %s; using fallback", type(exc).__name__)
        return None


@dataclass(frozen=True)
class _Matchup:
    """Event/team context for one upcoming arcadia matchup."""

    event_id: str
    home: str
    away: str
    league: str
    starts_at: datetime


@dataclass(frozen=True)
class MarketQuote:
    """One period-0 market observation for an event (moneyline / total /
    spread): the per-selection snapshots, the arcadia ``market_key`` (which
    identifies the market+line so each line change-gates independently), and
    Pinnacle's monotonic market ``version``."""

    event_id: str
    market_key: str
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

    Kickoff prefers the period-0 ``cutoffAt`` (the TRUE betting cutoff) over
    ``startTime`` when present, so the latest pre-kickoff row IS the close;
    falls back to ``startTime`` when ``cutoffAt`` is absent or unparseable.
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
        starts_at = _parse_ts(str(matchup.get("cutoffAt") or "")) or _parse_ts(
            str(matchup.get("startTime", ""))
        )
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


def _line_token(line: float) -> str:
    """Unsigned decimal line -> market-key token matching the OddsPortal loader:
    2.5->'2_5', 3.0->'3_0', 220.5->'220_5', 0.25->'0_25'. Distinct lines MUST
    yield distinct tokens so they group separately for devig."""
    mag = abs(line)
    return f"{mag:g}".replace(".", "_") if mag != int(mag) else f"{int(mag)}_0"


def _signed_token(line: float) -> str:
    """Signed AH line token for market_detail: -1.5->'-1_5', 0.5->'+0_5'."""
    return f"{'-' if line < 0 else '+'}{_line_token(line)}"


def _games_line_token(line: float) -> str:
    """OddsPortal SOFT "_games" line token — mirrors the JSON feed's market_detail
    EXACTLY: ``f"{line:g}"`` with '.'->'_', a leading '-' for negatives and NO '+'
    for positives (220.5->'220_5', -7.5->'-7_5', 7.5->'7_5'). Distinct from
    ``_signed_token`` (which adds a '+'): the soft feed key is ``(-?\\d+...)`` so
    a positive line carries no sign. Used so Arcadia basketball totals/spreads
    share the soft pick's (market, market_detail) devig group; the signed line is
    preserved verbatim (never abs / never flipped)."""
    return f"{line:g}".replace(".", "_")


def _fmt_signed(line: float) -> str:
    """Human selection suffix for a handicap: -1.5->'-1.5', 1.5->'+1.5', 0->'0'."""
    return "0" if line == 0 else f"{line:+g}"


def _is_int_price(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finalize_quote(
    market: Mapping[str, Any],
    event_id: str,
    market_key: str,
    snapshots: list[OddsSnapshotIn],
) -> MarketQuote | None:
    """Wrap snapshots into a versioned MarketQuote, or None if unusable. A
    versionless market cannot be change-gated (a synthesized 0 would freeze it),
    so it is skipped and retried next cycle — arcadia's version is ~always set."""
    if not snapshots:
        return None
    version = market.get("version")
    if not _is_int_price(version):
        return None
    return MarketQuote(
        event_id=event_id, market_key=market_key, version=version, snapshots=tuple(snapshots)
    )


def extract_moneyline_quotes(
    matchups: Mapping[str, _Matchup],
    markets: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[MarketQuote]:
    """Period-0 moneyline (match/game winner) quotes joined to their matchups.

    Skips non-``s;0;m`` keys, non-open markets, markets for events outside the
    matchup window, and participantId-keyed multiway prices (no designation).
    Prices are American integers -> European decimal; soccer carries a draw.
    """
    quotes: list[MarketQuote] = []
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
            if not _is_int_price(raw_price):
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
        quote = _finalize_quote(market, event_id, _MONEYLINE_KEY, snapshots)
        if quote is not None:
            quotes.append(quote)
    return quotes


def extract_total_quotes(
    matchups: Mapping[str, _Matchup],
    markets: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    sport: str = "",
) -> list[MarketQuote]:
    """Period-0 over/under (``s;0;ou;<line>``) MAIN-line quotes -> Market.TOTALS.

    Alternates (``isAlternate``) are excluded — the main line is the sharp
    anchor; alternates carry wider margin and would pollute the devig. The line
    rides BOTH the selection text ("Over 2.5") and market_detail so distinct
    lines never collapse into one devig group.

    ``sport`` selects the market_detail namespace so the snapshot groups with the
    matching soft pick: basketball uses the soft "_games" namespace
    ("over_under_games_220_5"); soccer/other keep the bare "over_under_2_5".
    """
    games_ns = sport in _GAMES_DETAIL_SPORTS
    quotes: list[MarketQuote] = []
    for market in markets:
        if not str(market.get("key") or "").startswith(_TOTAL_PREFIX):
            continue
        if market.get("type") != "total" or market.get("period") != 0:
            continue
        if market.get("status") != "open" or market.get("isAlternate"):
            continue
        event_id = str(market.get("matchupId"))
        if matchups.get(event_id) is None:
            continue
        snapshots: list[OddsSnapshotIn] = []
        for price in market.get("prices") or []:
            designation = price.get("designation")
            raw_price = price.get("price")
            points = price.get("points")
            if designation not in ("over", "under") or not _is_int_price(raw_price):
                continue
            if not _is_number(points):
                continue
            decimal_odds = american_to_decimal(raw_price)
            if decimal_odds <= 1.0:
                continue
            line = float(points)
            label = "Over" if designation == "over" else "Under"
            market_detail = (
                f"over_under_games_{_games_line_token(line)}"
                if games_ns
                else f"over_under_{_line_token(line)}"
            )
            snapshots.append(
                OddsSnapshotIn(
                    event_id=event_id,
                    bookmaker=BOOKMAKER,
                    market=Market.TOTALS,
                    selection=f"{label} {line:g}",
                    decimal_odds=decimal_odds,
                    captured_at=now,
                    ingested_at=now,
                    market_detail=market_detail,
                )
            )
        quote = _finalize_quote(market, event_id, str(market.get("key")), snapshots)
        if quote is not None:
            quotes.append(quote)
    return quotes


def extract_spread_quotes(
    matchups: Mapping[str, _Matchup],
    markets: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    sport: str = "",
) -> list[MarketQuote]:
    """Period-0 spread / Asian-handicap (``s;0;s;<line>``) MAIN-line quotes ->
    Market.SPREADS. Alternates excluded. market_detail is keyed on the HOME
    handicap (the OddsPortal AH convention) so home -1.5 / away +1.5 group as
    one line; the per-side signed handicap rides the selection text.

    ``sport`` selects the market_detail namespace so the snapshot groups with the
    matching soft pick: basketball uses the soft "_games" namespace
    ("asian_handicap_games_-7_5", positive lines carry NO '+'); soccer/other keep
    the bare "asian_handicap_-1_5" (positive lines carry a '+')."""
    games_ns = sport in _GAMES_DETAIL_SPORTS
    quotes: list[MarketQuote] = []
    for market in markets:
        if not str(market.get("key") or "").startswith(_SPREAD_PREFIX):
            continue
        if market.get("type") != "spread" or market.get("period") != 0:
            continue
        if market.get("status") != "open" or market.get("isAlternate"):
            continue
        event_id = str(market.get("matchupId"))
        matchup = matchups.get(event_id)
        if matchup is None:
            continue
        prices = market.get("prices") or []
        home_line = next(
            (
                float(p["points"])
                for p in prices
                if p.get("designation") == "home" and _is_number(p.get("points"))
            ),
            None,
        )
        if home_line is None:
            continue  # cannot key the line without the home handicap
        market_detail = (
            f"asian_handicap_games_{_games_line_token(home_line)}"
            if games_ns
            else f"asian_handicap_{_signed_token(home_line)}"
        )
        snapshots: list[OddsSnapshotIn] = []
        for price in prices:
            designation = price.get("designation")
            raw_price = price.get("price")
            points = price.get("points")
            if designation not in ("home", "away") or not _is_int_price(raw_price):
                continue
            if not _is_number(points):
                continue
            decimal_odds = american_to_decimal(raw_price)
            if decimal_odds <= 1.0:
                continue
            team = matchup.home if designation == "home" else matchup.away
            snapshots.append(
                OddsSnapshotIn(
                    event_id=event_id,
                    bookmaker=BOOKMAKER,
                    market=Market.SPREADS,
                    selection=f"{team} {_fmt_signed(float(points))}",
                    decimal_odds=decimal_odds,
                    captured_at=now,
                    ingested_at=now,
                    market_detail=market_detail,
                )
            )
        quote = _finalize_quote(market, event_id, str(market.get("key")), snapshots)
        if quote is not None:
            quotes.append(quote)
    return quotes


def extract_market_quotes(
    matchups: Mapping[str, _Matchup],
    markets: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    sport: str = "",
) -> list[MarketQuote]:
    """All capturable period-0 sharp markets: moneyline + MAIN-line total +
    MAIN-line spread (Asian handicap). The sharp closing anchor across H2H /
    TOTALS / SPREADS for the archive.

    ``sport`` threads to the totals/spread extractors so their market_detail
    namespace matches the matching soft pick (basketball -> soft "_games"
    namespace; soccer/other -> the bare namespace, unchanged). Moneyline (H2H)
    carries no market_detail, so it is namespace-agnostic."""
    return [
        *extract_moneyline_quotes(matchups, markets, now=now),
        *extract_total_quotes(matchups, markets, now=now, sport=sport),
        *extract_spread_quotes(matchups, markets, now=now, sport=sport),
    ]


class _MarketSource(Protocol):
    async def fetch_sports(self) -> dict[str, int]: ...
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

    def apply_config(self, config: ArcadiaConfig) -> None:
        """Adopt a base URL + guest key discovered from ``/config/app.json``.

        The key is read out of the SecretStr only here, only into the private
        header field; it is never logged or returned. Used by the composition
        root when ``arcadia_discover_config`` is opted in."""
        self._base_url = config.base_url.rstrip("/")
        self._guest_key = config.guest_key.get_secret_value()

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "referer": _PINNACLE_REFERER}
        if self._guest_key:
            headers["x-api-key"] = self._guest_key
        return headers

    @retry(
        # Retry transport errors (timeouts, resets) AND transient HTTP statuses
        # (429/5xx, surfaced as _TransientStatusError) with backoff+jitter, so a
        # momentary upstream hiccup is recovered instead of becoming an immediate
        # "no data this cycle". Permanent 4xx is NEVER raised as transient here,
        # so it is never retried (it returns the Response and the caller's non-200
        # check raises PinnacleArcadiaError at once).
        # 6 attempts (was 3): a 403 rotates to the NEXT proxy each request (round-
        # robin), so with ~8 proxies in the pool 3 attempts only tried ~3 before
        # declaring "no data" — daytime 403 spikes (more arcadia fetches) leaked
        # transient blocks into the floor. 6 attempts exhausts more of the pool
        # so a recoverable 403 actually recovers instead of becoming "no data".
        retry=retry_if_exception_type((httpx.TransportError, _TransientStatusError)),
        stop=stop_after_attempt(6),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        response = await self._client.get(
            f"{self._base_url}{path}", params=params, headers=self._headers(), timeout=20.0
        )
        if (
            response.status_code in _TRANSIENT_STATUSES
            or response.status_code in _PROXY_ROTATE_STATUSES
        ):
            # Raise to trigger the retry: a transient 429/5xx, or a 403 that
            # rotates to the next proxy. Status code ONLY — never the URL/key.
            raise _TransientStatusError(response.status_code)
        return response

    async def _fetch_list(
        self, path: str, params: dict[str, str] | None, what: str, sport_id: int
    ) -> list[dict[str, Any]]:
        try:
            response = await self._get(path, params)
        except _TransientStatusError as exc:
            # Retries exhausted on a transient 429/5xx: surface it as the normal
            # non-retryable error so capture_once's per-sport isolation/dedupe is
            # unchanged. Status + sport id only — never the URL or key.
            raise PinnacleArcadiaError(
                f"pinnacle {what} returned status {exc.status_code} for sport={sport_id}"
            ) from exc
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

    async def fetch_sports(self) -> dict[str, int]:
        """LIVE sports from ``GET /sports``: name (lowercased) -> id, keeping
        only entries with ``isHidden`` falsey AND ``matchupCount > 0``.

        Lets capture_once skip hidden/empty sports so no matchups fetch is wasted
        on a sport pricing nothing. GET-only; a transient 429/5xx retries with
        backoff before giving up, then raises PinnacleArcadiaError on a
        non-200/non-JSON body (status only, never the URL/key) so the caller's
        fallback path is the same as for any other fetch failure.
        """
        try:
            response = await self._get("/sports")
        except _TransientStatusError as exc:
            # Retries exhausted on a transient 429/5xx — status only, never URL/key.
            raise PinnacleArcadiaError(
                f"pinnacle sports returned status {exc.status_code}"
            ) from exc
        if response.status_code != 200:
            raise PinnacleArcadiaError(f"pinnacle sports returned status {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise PinnacleArcadiaError("pinnacle sports returned a non-JSON body") from exc
        live: dict[str, int] = {}
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict):
                continue
            if entry.get("isHidden"):
                continue
            count = entry.get("matchupCount")
            # isinstance (not _is_number) so mypy narrows away None; bool is not
            # a valid count.
            if not isinstance(count, (int, float)) or isinstance(count, bool) or count <= 0:
                continue
            sport_id = entry.get("id")
            name = entry.get("name")
            if not _is_int_price(sport_id) or not isinstance(name, str) or not name:
                continue
            live[name.lower()] = sport_id
        return live


class PinnacleArcadiaCapture:
    """Forward closing-line capture: persists Pinnacle period-0 sharp snapshots
    — moneyline (H2H) plus the MAIN-line total (O/U) and spread (Asian
    handicap) — into the ``pinnacle_<sport>`` warehouse namespace. (Alternate
    lines are excluded: the main line is the sharp anchor.)

    Change-gated on Pinnacle's monotonic per-market ``version`` keyed by
    (sport, event, market_key) so each line gates INDEPENDENTLY: a snapshot is
    written only when that market reprices, so the latest pre-kickoff row IS
    that market's sharp close.
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
        self._seen_version: dict[tuple[str, str, str], int] = {}
        # sports whose per-cycle fetch failure has already been logged once, so a
        # persistently-empty sport (e.g. NFL out of season) does not warn every
        # cycle. Cleared on a successful capture so a later real outage re-warns.
        self._fetch_warned: set[str] = set()

    def _select_fresh(
        self,
        quotes: Sequence[MarketQuote],
        matchups: Mapping[str, _Matchup],
        sport: str,
    ) -> tuple[list[OddsSnapshotIn], dict[str, EventTeams]]:
        """Filter quotes to those whose market version advanced since last seen;
        return their snapshots plus the team context needed to persist them."""
        fresh: list[OddsSnapshotIn] = []
        teams: dict[str, EventTeams] = {}
        for quote in quotes:
            seen_key = (sport, quote.event_id, quote.market_key)
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

    async def _live_sport_ids(self) -> set[int] | None:
        """Live sport ids from ``/sports`` (isHidden==false & matchupCount>0),
        or ``None`` if discovery fails for ANY reason. ``None`` signals the
        caller to FALL BACK to capturing every configured sport (today's
        behaviour) — discovery is an optimisation, never a gate that can abort
        or shrink the cycle on a transient error."""
        assert self._client is not None
        try:
            live = await self._client.fetch_sports()
        except (httpx.HTTPError, PinnacleArcadiaError) as exc:
            logger.info(
                "pinnacle arcadia /sports discovery failed: %s; capturing all configured sports",
                type(exc).__name__,
            )
            return None
        return set(live.values())

    async def capture_once(self) -> dict[str, int]:
        """One capture cycle across the CONFIGURED sports, intersected with the
        live ``/sports`` feed so hidden/empty sports are skipped. Returns new
        rows written per sport. A failed sport is logged (type + status only,
        never the URL/key) and skipped — never aborts the other sports; a failed
        ``/sports`` discovery falls back to all configured sports."""
        if self._client is None:
            return {sport: 0 for sport in self._sports}

        from app.storage.repositories import persist_odds_snapshots

        # None = discovery unavailable -> capture every configured sport (today).
        live_ids = await self._live_sport_ids()

        now = self._now_fn()
        horizon_end = now + self._horizon
        written: dict[str, int] = {}
        for sport in self._sports:
            sport_id = SPORT_IDS.get(sport)
            if sport_id is None:
                logger.warning("pinnacle arcadia: unknown sport %r; skipping", sport)
                continue
            if live_ids is not None and sport_id not in live_ids:
                # Hidden/empty upstream this cycle: skip the wasted matchups
                # fetch. Recorded as 0 (honest), never silently dropped.
                written[sport] = 0
                continue
            try:
                raw_matchups = await self._client.fetch_matchups(sport_id)
                raw_markets = await self._client.fetch_straight_markets(sport_id)
            except (httpx.HTTPError, PinnacleArcadiaError) as exc:
                # Warn ONCE per sport per process: a persistently-empty sport
                # (e.g. NFL out of season, which arcadia discovery may still
                # list) would otherwise WARN every 60s cycle. First failure ->
                # WARNING (visible); repeats -> DEBUG. A later successful capture
                # clears the flag so a genuine new outage re-warns.
                first = sport not in self._fetch_warned
                self._fetch_warned.add(sport)
                logger.log(
                    logging.WARNING if first else logging.DEBUG,
                    "pinnacle arcadia: no data for %s this cycle (%s)",
                    sport,
                    type(exc).__name__,
                )
                continue
            self._fetch_warned.discard(sport)  # fetch recovered -> allow re-warn later
            matchups = parse_matchups(raw_matchups, now=now, horizon_end=horizon_end)
            quotes = extract_market_quotes(matchups, raw_markets, now=now, sport=sport)
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
