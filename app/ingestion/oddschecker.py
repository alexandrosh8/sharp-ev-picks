"""OddsChecker read-only HTML/Hypernova ingester.

The current OddsChecker pages server-render their usable odds state into
``<script type="application/json">`` Hypernova payloads.  That lets us use the
same lightweight shape as the OddsPortal JSON path: one browser-compatible
``curl_cffi`` GET for the page, parse the embedded JSON, and emit normal
``OddsSnapshotIn`` rows.  No login, no cookies persisted, no challenge solving,
no betslip surface.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from app.identity import MARKET_DETAIL_MAX_BYTES, require_bounded_identity
from app.ingestion.base import EventDirectory, EventTeams, ScraperProxy
from app.ingestion.http_safety import (
    UnsafeUpstreamURL,
    UpstreamBodyTooLarge,
    get_bounded,
    validate_https_url,
)
from app.schemas.base import Market
from app.schemas.odds import MAX_DECIMAL_ODDS, OddsSnapshotIn

logger = logging.getLogger(__name__)

ODDSCHECKER_BASE_URL = "https://www.oddschecker.com/"
FOOTBALL_HOME_URL = "https://www.oddschecker.com/football"
ODDSCHECKER_SPORT_HOME_URLS: Mapping[str, str] = MappingProxyType(
    {
        "football": FOOTBALL_HOME_URL,
        "basketball": "https://www.oddschecker.com/basketball",
        "tennis": "https://www.oddschecker.com/tennis",
        "american_football": "https://www.oddschecker.com/american-football",
    }
)

# Keep this pinned like the OddsPortal JSON path.  Bare "chrome" drifts when
# curl_cffi updates its profiles.
PINNED_IMPERSONATE: Literal["chrome146"] = "chrome146"
DEFAULT_TIMEOUT: tuple[float, float] = (8.0, 25.0)
DEFAULT_MAX_CLIENTS = 8
MARKET_API_CHUNK_SIZE = 35
MAX_RETRY_AFTER_SECONDS = 60.0
MAX_HTML_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_MATCH_URLS_PER_CYCLE = 2_000
MAX_LEGACY_MARKET_URLS_PER_MATCH = 64
MAX_MARKET_IDS_PER_MATCH = 256
MAX_SNAPSHOTS_PER_MATCH = 5_000
MAX_SNAPSHOTS_PER_CYCLE = 250_000
ODDSCHECKER_ALLOWED_HOSTS: frozenset[str] = frozenset({"www.oddschecker.com"})
_MAPPED_MARKET_SCOPE: tuple[Market, ...] = tuple(
    market for market in Market if market is not Market.OTHER
)

_HTML_HEADERS: Mapping[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
_JSON_HEADERS: Mapping[str, str] = {
    "Accept": "application/json,text/plain,*/*",
}
_LONDON = ZoneInfo("Europe/London")

_SUPPORTED_MARKET_TYPES: Mapping[str, tuple[Market, str]] = {
    "win market": (Market.H2H, "h2h"),
    "match result": (Market.H2H, "h2h"),
    "moneyline": (Market.H2H, "h2h"),
    "winner": (Market.H2H, "h2h"),
    "double chance": (Market.DOUBLE_CHANCE, "double_chance"),
    "draw no bet": (Market.DNB, "dnb"),
    "both teams to score": (Market.BTTS, "btts"),
    "correct score": (Market.CORRECT_SCORE, "correct_score"),
}

_EXCLUDED_TOTAL_MARKET_TERMS = (
    "3-way",
    "3 way",
    "banded",
    "exact",
    "odd/even",
    "odd even",
)
_EXCLUDED_PLAYER_PROP_TERMS = (
    "booking",
    "card",
    "corner",
    "foul",
    "goalkeeper",
    "offsides",
    "pass",
    "player",
    "shot",
    "tackle",
)

# The sharp anchor this project trusts (app/edge/value.py SHARP_BOOKS). On
# OddsChecker, Betfair Exchange is bookmaker code "OE". A capture-only ("OTHER")
# market is emitted ONLY when it carries a quote from one of these — so props /
# period markets are captured as odds history solely when a sharp exchange
# actually prices them, never soft-book-only noise.
_SHARP_ANCHOR_BOOK_CODES: frozenset[str] = frozenset({"OE"})

# Markets that are DELIBERATELY mispriced (boosted / hand-built) — never capture
# them, even under OTHER: feeding boosted odds anywhere near devig is poison.
_EXCLUDED_OTHER_MARKET_TERMS: tuple[str, ...] = (
    "price boost",
    "boost",
    "enhanced",
    "bet builder",
    "builder",
    "popular bet",
    "request a bet",
    "acca",
    "same game",
)

_BOOKMAKER_FALLBACKS: Mapping[str, str] = {
    "B3": "bet365",
    "BF": "Betfair Sportsbook",
    "BY": "BOYLE Sports",
    "CE": "Coral",
    "FR": "Betfred",
    "KN": "BetMGM UK",
    "LD": "Ladbrokes",
    "MA": "Matchbook",
    "OE": "Betfair Exchange",
    "PP": "Paddy Power",
    "SK": "Skybet",
    "UN": "Unibet",
    "VC": "BetVictor",
    "VE": "Virgin Bet",
    "WA": "Betway",
    "WH": "William Hill",
    # Harvested 2026-07-07 from the live bestOdds bookmakers.entities feed (the
    # all-odds/legacy paths carry no entities map, so off-map codes were emitting
    # raw 2-letter codes — a split identity vs bestOdds' canonical name).
    "AKB": "AK Bets",
    "BAH": "BetAhoy",
    "BRS": "BresBet",
    "BTT": "BetTom",
    "G5": "BetGoodwin",
    "PUP": "PricedUp",
    "QN": "QuinnBet",
    "S6": "Star Sports",
    "SI": "Sporting Index",
    "SX": "Spreadex",
}


class OddsCheckerError(RuntimeError):
    """Base OddsChecker ingestion error."""


class OddsCheckerChallenge(OddsCheckerError):
    """The response was a provider/CDN challenge page, not usable odds HTML."""


class OddsCheckerHTTPError(OddsCheckerError):
    """A non-success HTTP response with status/retry metadata preserved."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class OddsCheckerParseError(OddsCheckerError):
    """The HTML did not contain the expected odds payload shape."""


class OddsCheckerSecurityError(OddsCheckerError):
    """A response violated an origin, redirect, or resource ceiling policy."""


class AsyncGetSession(Protocol):
    """The tiny subset of curl_cffi AsyncSession we use; tests inject fakes."""

    async def get(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class OddsCheckerFetchResult:
    """Raw page response with the URL after redirects."""

    url: str
    html: str
    status_code: int


@dataclass(frozen=True)
class OddsCheckerFootballContext:
    """Football-home API context lifted from the server-rendered page."""

    card_ids: tuple[str, ...]
    event_urls: Mapping[str, str]


@dataclass(frozen=True)
class _ProxySessionLease:
    """Identity-bearing lease used to evict exactly the failed proxy session."""

    index: int
    session: AsyncGetSession


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _proxy_with_creds(proxy: ScraperProxy) -> str:
    if not proxy.username:
        return proxy.url
    scheme, _, rest = proxy.url.partition("://")
    if not rest:
        return proxy.url
    from urllib.parse import quote

    user = quote(proxy.username, safe="")
    pwd = quote(proxy.password, safe="")
    return f"{scheme}://{user}:{pwd}@{rest}"


def _normalize_url(url: str, *, base_url: str = ODDSCHECKER_BASE_URL) -> str:
    absolute = urljoin(base_url, url)
    try:
        return validate_https_url(absolute, allowed_hosts=ODDSCHECKER_ALLOWED_HOSTS)
    except UnsafeUpstreamURL as exc:
        raise OddsCheckerSecurityError("invalid OddsChecker HTTPS origin") from exc


def _site_root(base_url: str) -> str:
    parsed = urlparse(_normalize_url(base_url))
    return f"{parsed.scheme}://{parsed.netloc}/"


def _normalize_site_href(href: str, *, base_url: str = ODDSCHECKER_BASE_URL) -> str:
    parsed = urlparse(href)
    if parsed.scheme and parsed.netloc:
        return _normalize_url(href)
    # OddsChecker frequently renders root-relative paths without a leading slash
    # (``football/english/...``).  Joining those against the current page would
    # duplicate path segments, so resolve all local hrefs against site root.
    return _normalize_url(href.lstrip("/"), base_url=_site_root(base_url))


def _event_id_from_url(url: str) -> str:
    parsed = urlparse(_normalize_url(url))
    path = parsed.path.strip("/")
    return f"oddschecker:{path or 'home'}"


def is_challenge_response(
    *,
    status_code: int,
    headers: Mapping[str, str] | Any,
    body: str,
) -> bool:
    """True only for an interstitial/challenge response, not normal JS telemetry.

    OddsChecker's normal 200 pages can include Cloudflare JS-detections snippets.
    Those snippets alone are not a blocked/challenge page.  We only classify a
    response as challenged when Cloudflare says so in headers, or when an error
    status carries challenge/interstitial text.
    """
    try:
        mitigated = str(headers.get("cf-mitigated", "")).lower()
    except AttributeError:
        mitigated = ""
    if mitigated == "challenge":
        return True
    if status_code not in {403, 429, 503}:
        return False
    lowered = body[:20_000].lower()
    return any(
        marker in lowered
        for marker in (
            "just a moment",
            "checking your browser",
            "cf-chl-",
            "turnstile",
            "challenge-platform",
        )
    )


def _retry_after_seconds(headers: Mapping[str, Any] | Any) -> float | None:
    """Parse Retry-After as seconds or an HTTP date, bounded at zero."""
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        if not math.isfinite(seconds):
            return None
        return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    seconds = (retry_at.astimezone(UTC) - _utcnow()).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))


_TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
_TRANSIENT_CURL_CODES: frozenset[int] = frozenset({7, 18, 28, 35, 52, 55, 56})


def _is_transient_fetch_error(exc: BaseException) -> bool:
    """Classify retryable network/provider failures without class-name-only logic."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OddsCheckerChallenge):
            return True
        if isinstance(current, OddsCheckerHTTPError):
            return current.status_code in _TRANSIENT_HTTP_STATUSES
        if isinstance(current, (TimeoutError, ConnectionError)):
            return True

        raw_code = getattr(current, "code", None)
        try:
            code = int(raw_code) if raw_code is not None else None
        except (TypeError, ValueError):
            code = None
        if code in _TRANSIENT_CURL_CODES:
            return True

        text = f"{type(current).__name__} {current}".lower()
        if any(
            marker in text
            for marker in (
                "timeout",
                "timed out",
                "connection reset",
                "connectionreset",
                "recv failure",
                "curl: (56)",
                "curl (56)",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _wait_before_retry(exc: BaseException) -> None:
    retry_after = exc.retry_after if isinstance(exc, OddsCheckerHTTPError) else None
    if retry_after is not None and retry_after > 0:
        await asyncio.sleep(min(retry_after, 60.0))


async def fetch_html(
    url: str,
    *,
    session: AsyncGetSession | None = None,
    proxy: ScraperProxy | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> OddsCheckerFetchResult:
    """GET one OddsChecker HTML page with curl_cffi browser impersonation."""
    target = _normalize_url(url)
    try:
        if session is not None:
            bounded = await get_bounded(
                session,
                target,
                allowed_hosts=ODDSCHECKER_ALLOWED_HOSTS,
                max_bytes=MAX_HTML_RESPONSE_BYTES,
                headers=dict(_HTML_HEADERS),
                timeout=timeout,
            )
        else:
            from curl_cffi.requests import AsyncSession

            kwargs: dict[str, Any] = {
                "impersonate": PINNED_IMPERSONATE,
                "default_headers": True,
                "timeout": timeout,
                "allow_redirects": False,
            }
            if proxy is not None and proxy.url:
                inline = _proxy_with_creds(proxy)
                kwargs["proxies"] = {"http": inline, "https": inline}
            async with AsyncSession(**kwargs) as own_session:
                bounded = await get_bounded(
                    own_session,
                    target,
                    allowed_hosts=ODDSCHECKER_ALLOWED_HOSTS,
                    max_bytes=MAX_HTML_RESPONSE_BYTES,
                    headers=dict(_HTML_HEADERS),
                )
    except (UnsafeUpstreamURL, UpstreamBodyTooLarge) as exc:
        raise OddsCheckerSecurityError(type(exc).__name__) from exc

    response = bounded.response
    status = int(getattr(response, "status_code", 0) or 0)
    text = bounded.body.decode("utf-8", errors="replace")
    headers = getattr(response, "headers", {})
    if is_challenge_response(status_code=status, headers=headers, body=text):
        raise OddsCheckerChallenge("oddschecker returned a challenge/interstitial response")
    if status >= 400:
        raise OddsCheckerHTTPError(
            f"oddschecker GET returned HTTP {status}",
            status_code=status,
            retry_after=_retry_after_seconds(headers),
        )
    return OddsCheckerFetchResult(url=bounded.final_url, html=text, status_code=status)


async def fetch_json_value(
    url: str,
    *,
    session: AsyncGetSession | None = None,
    proxy: ScraperProxy | None = None,
    referer: str = FOOTBALL_HOME_URL,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> Any:
    """GET one OddsChecker JSON endpoint with the same curl_cffi profile."""
    target = _normalize_url(url)
    headers = dict(_JSON_HEADERS)
    headers["Referer"] = _normalize_url(referer)
    try:
        if session is not None:
            bounded = await get_bounded(
                session,
                target,
                allowed_hosts=ODDSCHECKER_ALLOWED_HOSTS,
                max_bytes=MAX_JSON_RESPONSE_BYTES,
                headers=headers,
                timeout=timeout,
            )
        else:
            from curl_cffi.requests import AsyncSession

            kwargs: dict[str, Any] = {
                "impersonate": PINNED_IMPERSONATE,
                "default_headers": True,
                "timeout": timeout,
                "allow_redirects": False,
            }
            if proxy is not None and proxy.url:
                inline = _proxy_with_creds(proxy)
                kwargs["proxies"] = {"http": inline, "https": inline}
            async with AsyncSession(**kwargs) as own_session:
                bounded = await get_bounded(
                    own_session,
                    target,
                    allowed_hosts=ODDSCHECKER_ALLOWED_HOSTS,
                    max_bytes=MAX_JSON_RESPONSE_BYTES,
                    headers=headers,
                )
    except (UnsafeUpstreamURL, UpstreamBodyTooLarge) as exc:
        raise OddsCheckerSecurityError(type(exc).__name__) from exc

    response = bounded.response
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        text = bounded.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OddsCheckerParseError("oddschecker JSON response was not UTF-8") from exc
    response_headers = getattr(response, "headers", {})
    if is_challenge_response(status_code=status, headers=response_headers, body=text):
        raise OddsCheckerChallenge("oddschecker returned a challenge/interstitial response")
    if status >= 400:
        raise OddsCheckerHTTPError(
            f"oddschecker JSON GET returned HTTP {status}",
            status_code=status,
            retry_after=_retry_after_seconds(response_headers),
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OddsCheckerParseError("oddschecker JSON response did not parse") from exc


async def fetch_json(
    url: str,
    *,
    session: AsyncGetSession | None = None,
    proxy: ScraperProxy | None = None,
    referer: str = FOOTBALL_HOME_URL,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """GET one OddsChecker JSON object endpoint with the same curl_cffi profile."""
    payload = await fetch_json_value(
        url,
        session=session,
        proxy=proxy,
        referer=referer,
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise OddsCheckerParseError("oddschecker JSON response is not an object")
    return payload


def _strip_json_comment(raw: str) -> str:
    text = raw.strip()
    if text.startswith("<!--"):
        text = text[4:]
    if text.endswith("-->"):
        text = text[:-3]
    return text.strip()


def hypernova_payloads(html: str) -> list[dict[str, Any]]:
    """Extract all JSON payloads from Hypernova application/json scripts."""
    soup = BeautifulSoup(html, "html.parser")
    payloads: list[dict[str, Any]] = []
    for script in soup.find_all("script", {"type": "application/json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            payload = json.loads(_strip_json_comment(raw))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def football_listing_context(html: str) -> OddsCheckerFootballContext:
    """Extract card ids + event URL map needed by the football daily API."""
    best_cards: tuple[str, ...] = ()
    best_event_urls: dict[str, str] = {}
    for payload in hypernova_payloads(html):
        config = payload.get("config")
        if not isinstance(config, Mapping):
            continue
        raw_cards = config.get("cards")
        raw_events = config.get("events")
        if not isinstance(raw_cards, Sequence) or isinstance(raw_cards, (str, bytes)):
            continue
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            continue
        card_ids = tuple(
            str(card.get("id"))
            for card in raw_cards
            if isinstance(card, Mapping) and card.get("id") is not None
        )
        event_urls = {
            str(event.get("eventId")): str(event.get("url")).strip("/")
            for event in raw_events
            if isinstance(event, Mapping) and event.get("eventId") is not None and event.get("url")
        }
        # The football home payload has the full card/event catalogue; smaller
        # placeholder payloads have empty or partial config.
        if len(card_ids) > len(best_cards):
            best_cards = card_ids
            best_event_urls = event_urls
    if not best_cards or not best_event_urls:
        raise OddsCheckerParseError("football daily page did not expose card/event context")
    return OddsCheckerFootballContext(
        card_ids=best_cards,
        event_urls=MappingProxyType(best_event_urls),
    )


def build_football_daily_api_url(
    context: OddsCheckerFootballContext,
    *,
    start_date: date,
    days: int = 2,
    base_url: str = ODDSCHECKER_BASE_URL,
    market_template_id: int = 1,
    load_data_for: int = 3,
) -> str:
    """The endpoint the football date picker uses for coupon match discovery."""
    if days < 1:
        raise OddsCheckerParseError("days must be >= 1")
    cards = ",".join(context.card_ids)
    path = (
        "api/acca/v1/acca/coupon/cards/"
        f"{cards}/marketTemplate/{market_template_id}/loadData/{load_data_for}/"
        f"forDate/{start_date.isoformat()}/andDays/{days}"
    )
    return _normalize_url(path, base_url=_site_root(base_url))


def football_match_urls_from_api(
    payload: Mapping[str, Any],
    context: OddsCheckerFootballContext,
    *,
    start_date: date,
    days: int = 2,
    base_url: str = ODDSCHECKER_BASE_URL,
) -> list[str]:
    """Build unique match-page URLs from the football daily API payload."""
    raw_subevents = payload.get("subevents")
    if not isinstance(raw_subevents, Sequence) or isinstance(raw_subevents, (str, bytes)):
        raise OddsCheckerParseError("football daily API payload has no subevents list")
    end_date = start_date + timedelta(days=days)
    rows: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for raw in raw_subevents:
        if not isinstance(raw, Mapping):
            continue
        kickoff = _parse_datetime(raw.get("startTime"))
        if kickoff is None:
            continue
        local_day = kickoff.astimezone(_LONDON).date()
        if local_day < start_date or local_day >= end_date:
            continue
        event_url = context.event_urls.get(str(raw.get("eventId") or ""))
        url_map = str(raw.get("urlMap") or "").strip("/")
        if not event_url or not url_map:
            continue
        url = _normalize_site_href(f"{event_url}/{url_map}/winner", base_url=base_url)
        if url in seen:
            continue
        seen.add(url)
        rows.append((kickoff, url))
        if len(rows) > MAX_MATCH_URLS_PER_CYCLE:
            raise OddsCheckerSecurityError("football discovery exceeded match URL ceiling")
    rows.sort(key=lambda item: (item[0], item[1]))
    return [url for _, url in rows]


async def discover_football_daily_match_urls(
    *,
    home_url: str = FOOTBALL_HOME_URL,
    start_date: date | None = None,
    days: int = 2,
    session: AsyncGetSession | None = None,
    proxy: ScraperProxy | None = None,
) -> list[str]:
    """Fast discovery for all football match pages in a London-date window."""
    page = await fetch_html(home_url, session=session, proxy=proxy)
    context = football_listing_context(page.html)
    day = start_date or datetime.now(tz=_LONDON).date()
    api_url = build_football_daily_api_url(context, start_date=day, days=days, base_url=page.url)
    payload = await fetch_json(api_url, session=session, proxy=proxy, referer=page.url)
    return football_match_urls_from_api(
        payload,
        context,
        start_date=day,
        days=days,
        base_url=page.url,
    )


def _parse_ordinal_listing_date(value: str) -> date | None:
    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", value.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    for fmt in ("%A %d %B %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_static_sport_match_urls(
    html: str,
    *,
    start_date: date,
    days: int = 2,
    base_url: str = ODDSCHECKER_BASE_URL,
) -> list[str]:
    """Extract today/tomorrow match URLs from OddsChecker legacy sport lists.

    Basketball, tennis, and American football still render their sport home pages
    as static date-grouped tables.  We walk each table in document order, track
    the latest ``event-date`` header, and collect the row's ``/winner`` match
    page when the header date falls in the requested London-date window.
    """
    if days < 1:
        raise OddsCheckerParseError("days must be >= 1")
    end_date = start_date + timedelta(days=days)
    soup = BeautifulSoup(html, "html.parser")
    rows: list[tuple[date, int, str]] = []
    seen: set[str] = set()
    order = 0
    for table in soup.select("table.standard-list, table.at-12"):
        current_date: date | None = None
        for element in table.descendants:
            element_any: Any = element
            name = getattr(element_any, "name", None)
            if not name:
                continue
            classes = set(getattr(element_any, "get", lambda _key, _default=None: [])("class", []))
            if "event-date" in classes:
                current_date = _parse_ordinal_listing_date(element_any.get_text(" ", strip=True))
                continue
            if name != "tr" or "match-on" not in classes or current_date is None:
                continue
            if current_date < start_date or current_date >= end_date:
                continue
            anchor = element_any.select_one('a[href*="/winner"]')
            if anchor is None:
                continue
            href = str(anchor.get("href") or "")
            if not href:
                continue
            absolute = _normalize_site_href(href, base_url=base_url)
            if absolute in seen:
                continue
            seen.add(absolute)
            rows.append((current_date, order, absolute))
            order += 1
            if len(rows) > MAX_MATCH_URLS_PER_CYCLE:
                raise OddsCheckerSecurityError("sport discovery exceeded match URL ceiling")
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    return [url for _, _, url in rows]


async def discover_static_sport_daily_match_urls(
    sport_key: str,
    *,
    home_url: str | None = None,
    start_date: date | None = None,
    days: int = 2,
    session: AsyncGetSession | None = None,
    proxy: ScraperProxy | None = None,
) -> list[str]:
    """Fast discovery for legacy static sport pages in a London-date window."""
    resolved_home_url = home_url or ODDSCHECKER_SPORT_HOME_URLS.get(sport_key)
    if resolved_home_url is None:
        raise OddsCheckerParseError(f"unsupported OddsChecker sport key: {sport_key}")
    page = await fetch_html(resolved_home_url, session=session, proxy=proxy)
    day = start_date or datetime.now(tz=_LONDON).date()
    return parse_static_sport_match_urls(
        page.html,
        start_date=day,
        days=days,
        base_url=page.url,
    )


async def discover_sport_daily_match_urls(
    sport_key: str,
    *,
    start_date: date | None = None,
    days: int = 2,
    session: AsyncGetSession | None = None,
    proxy: ScraperProxy | None = None,
) -> list[str]:
    """Discover today/tomorrow match URLs for any supported OddsChecker sport."""
    if sport_key == "football":
        return await discover_football_daily_match_urls(
            start_date=start_date,
            days=days,
            session=session,
            proxy=proxy,
        )
    return await discover_static_sport_daily_match_urls(
        sport_key,
        start_date=start_date,
        days=days,
        session=session,
        proxy=proxy,
    )


def _entity_map(container: Any) -> dict[str, Any]:
    if not isinstance(container, Mapping):
        return {}
    entities = container.get("entities")
    if not isinstance(entities, Mapping):
        return {}
    return {str(key): value for key, value in entities.items()}


def _ids(container: Any) -> list[str]:
    if not isinstance(container, Mapping):
        return []
    ids = container.get("ids")
    if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)):
        return []
    return [str(value) for value in ids]


def _find_match_payload(html: str, *, prefer_subevent_id: str | None = None) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for payload in hypernova_payloads(html):
        best = payload.get("bestOdds")
        if not isinstance(best, Mapping):
            continue
        odds = best.get("odds")
        if isinstance(odds, Mapping) and odds:
            candidates.append(payload)
    if not candidates:
        raise OddsCheckerParseError("no populated OddsChecker bestOdds payload found")
    # When the page's canonical subevent id is known (from the header breadcrumb),
    # prefer the blob that actually prices THAT match. A page can embed several
    # bestOdds blobs (accumulator / related matches); picking the byte-largest one
    # can register and price a DIFFERENT game than the URL (wrong-game hazard).
    if prefer_subevent_id:
        for payload in candidates:
            config = payload["bestOdds"].get("subeventConfig")
            if (
                isinstance(config, Mapping)
                and str(config.get("subeventId") or "").strip() == prefer_subevent_id
            ):
                return payload
        raise OddsCheckerParseError(
            "no populated bestOdds payload matches the page's canonical subevent id"
        )

    def _payload_score(payload: Mapping[str, Any]) -> tuple[int, int, int]:
        """Prefer the structurally richest blob without serializing it again."""
        best = payload.get("bestOdds")
        if not isinstance(best, Mapping):
            return (0, 0, 0)
        odds = best.get("odds")
        quote_count = 0
        bet_count = 0
        if isinstance(odds, Mapping):
            bet_count = len(odds)
            quote_count = sum(len(value) for value in odds.values() if isinstance(value, Mapping))
        market_count = len(_entity_map(best.get("markets")))
        return quote_count, bet_count, market_count

    return max(candidates, key=_payload_score)


def _find_header_payload(html: str) -> dict[str, Any]:
    for payload in hypernova_payloads(html):
        if "subeventStartTime" in payload and "subeventName" in payload:
            return payload
    return {}


def _header_subevent_id(header: Mapping[str, Any]) -> str | None:
    """The canonical subevent id of the page's match, from its breadcrumb —
    used to disambiguate multiple embedded bestOdds blobs to the URL's game."""
    for crumb in header.get("breadcrumbs", []):
        if isinstance(crumb, Mapping) and crumb.get("type") == "subevent":
            crumb_id = str(crumb.get("id") or "").strip()
            if crumb_id:
                return crumb_id
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python supports microseconds; provider timestamps can be nanoseconds.
    text = re.sub(r"(\.\d{6})\d+(?=[+-]\d\d:\d\d$)", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_epoch_ms(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _is_boost_market(market_type: str) -> bool:
    """True for boosted/hand-built markets that must never be captured."""
    key = market_type.strip().lower()
    return any(term in key for term in _EXCLUDED_OTHER_MARKET_TERMS)


def _other_market_detail(market_type: str, line: Any = None) -> str:
    """`market_detail` for a capture-only OTHER market: ``oc_<slug>[_<line>]``.

    Carries the real submarket identity for odds history when a market has no
    Market-enum home. Oversized values are rejected, never truncated: truncation
    can alias two distinct capture-only instruments under the snapshot key."""
    base = re.sub(r"[^0-9a-z]+", "_", market_type.strip().lower()).strip("_")
    line_slug = "" if line is None or line == "" else _slug_line(line)
    detail = f"oc_{base}" if not line_slug else f"oc_{base}_{line_slug}"
    return require_bounded_identity(
        detail,
        maximum_bytes=MARKET_DETAIL_MAX_BYTES,
        field="OddsChecker market_detail",
    )


def _odds_have_sharp_anchor(raw_odds: Sequence[Any]) -> bool:
    """True when any LIVE odd carries a sharp-anchor book quote (active OE).

    A SUSPENDED or expired exchange quote is not a live anchor: the OTHER-capture
    gate must require the sharp book to actually be pricing the market now, not
    merely to appear in the grid."""
    for odd in raw_odds:
        if not isinstance(odd, Mapping):
            continue
        if str(odd.get("bookmakerCode") or "") not in _SHARP_ANCHOR_BOOK_CODES:
            continue
        if str(odd.get("status") or "").upper() != "ACTIVE":
            continue
        if odd.get("expired") is True or odd.get("notExpired") is False:
            continue
        if _decimal(odd.get("oddsDecimal")) is None:
            continue
        return True
    return False


def _market_for_type(
    market_type: str,
    line: Any,
    selection: str | None = None,
) -> tuple[Market, str] | None:
    key = market_type.strip().lower()
    direct = _SUPPORTED_MARKET_TYPES.get(key)
    if direct is not None:
        return direct
    if _is_nonfinite_numeric(line):
        return None
    if "draw no bet" in key:
        return Market.DNB, _market_detail("dnb", key, line)
    if key == "both teams to score":
        return Market.BTTS, "btts"
    if key == "double chance":
        return Market.DOUBLE_CHANCE, "double_chance"
    if _is_spread_market_type(key):
        # SET-vs-GAME handicap collision guard (hardening 2026-07-11): a tennis
        # SET handicap and a GAME handicap at the same numeric line used to slug
        # to the SAME spreads_<line> detail, risking a mixed devig group of two
        # different units. Fail-closed: game handicaps keep the historical
        # vocabulary EXACTLY (stamped picks stay matched); set handicaps get a
        # distinct namespaced detail for NEW rows only — an unmatched namespaced
        # group simply never merges with game-line picks.
        prefix = "spreads_sets" if _is_set_handicap_market_type(key) else "spreads"
        detail = _market_detail(prefix, key, line)
        return Market.SPREADS, detail
    if _is_team_total_market_type(key):
        detail = _market_detail("team_totals", key, line)
        return Market.TEAM_TOTALS, detail
    if _is_total_market_type(key, selection):
        detail = _market_detail("totals", key, line)
        return Market.TOTALS, detail
    return None


def _is_spread_market_type(key: str) -> bool:
    if not any(term in key for term in ("handicap", "spread")):
        return False
    return not any(term in key for term in _EXCLUDED_PLAYER_PROP_TERMS)


def _is_set_handicap_market_type(key: str) -> bool:
    """True for a SET-denominated handicap key ("set handicap", "sets handicap",
    "asian set handicap") — a different UNIT from the game handicap, so it must
    never share the game line's spreads_<line> devig key. Token-matched (never
    substring) so unrelated words can't misfire."""
    if not _is_spread_market_type(key):
        return False
    tokens = set(re.split(r"[^0-9a-z]+", key))
    return bool(tokens & {"set", "sets"})


def _is_team_total_market_type(key: str) -> bool:
    return any(
        term in key
        for term in (
            "away team total",
            "home team total",
            "player a total",
            "player b total",
            "total away",
            "total home",
        )
    )


def _is_total_market_type(key: str, selection: str | None = None) -> bool:
    if any(term in key for term in _EXCLUDED_TOTAL_MARKET_TERMS):
        return False
    if " and " in key:
        return False
    if any(term in key for term in _EXCLUDED_PLAYER_PROP_TERMS):
        return False
    if any(
        term in key
        for term in (
            "asian total",
            "over/under",
            "total games",
            "total goals",
            "total points",
            "total sets",
        )
    ):
        return True
    selection_key = (selection or "").strip().lower()
    return "total" in key and (
        selection_key in {"over", "under"}
        or selection_key.startswith("over ")
        or selection_key.startswith("under ")
    )


def _market_detail(prefix: str, market_type: str, line: Any) -> str:
    period = _market_period_slug(market_type)
    line_slug = "" if line is None or line == "" else _slug_line(line)
    parts = [prefix]
    if period:
        parts.append(period)
    if line_slug:
        parts.append(line_slug)
    return "_".join(parts)


def _market_period_slug(market_type: str) -> str:
    key = market_type.strip().lower()
    period_terms = (
        ("1st quarter", "1st_quarter"),
        ("first quarter", "1st_quarter"),
        ("2nd quarter", "2nd_quarter"),
        ("second quarter", "2nd_quarter"),
        ("3rd quarter", "3rd_quarter"),
        ("third quarter", "3rd_quarter"),
        ("4th quarter", "4th_quarter"),
        ("fourth quarter", "4th_quarter"),
        ("1st half", "1st_half"),
        ("first half", "1st_half"),
        ("2nd half", "2nd_half"),
        ("second half", "2nd_half"),
        ("set 1", "set_1"),
        ("set 2", "set_2"),
        ("set 3", "set_3"),
        ("set 4", "set_4"),
        ("set 5", "set_5"),
    )
    for needle, slug in period_terms:
        if needle in key:
            return slug
    return ""


def _slug_line(value: Any) -> str:
    text = str(value).strip().replace("+", "plus_").replace("-", "minus_")
    return re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()


def _bookmaker_name(code: str, bookmaker_entities: Mapping[str, Any]) -> str | None:
    raw = bookmaker_entities.get(code)
    if isinstance(raw, Mapping):
        name = raw.get("bookmakerName") or raw.get("name")
        if isinstance(name, str) and name.strip():
            cleaned = name.strip()
            # M863 (audit 2026-07-10): the feed sometimes labels code BF with the
            # bare display name "Betfair", persisting the SAME sportsbook under
            # two bookmaker names — and edge/EV maps bare "betfair" to 5%
            # EXCHANGE commission, mispricing those rows. The ambiguous bare
            # name resolves through the code's canonical fallback (BF ->
            # "Betfair Sportsbook", OE -> "Betfair Exchange"); every other
            # display name passes through unchanged.
            if cleaned.lower() == "betfair":
                # The bare brand is ambiguous: BF is the sportsbook while OE
                # is the exchange and only the latter carries commission. An
                # unknown code cannot be priced safely as either venue.
                return _BOOKMAKER_FALLBACKS.get(code)
            return cleaned
    # Raw provider codes are not stable bookmaker identities. If neither the
    # payload nor our audited mapping can name the code, drop that quote rather
    # than persist a second identity that can carry the wrong commission model.
    return _BOOKMAKER_FALLBACKS.get(code)


def _team_names(best: Mapping[str, Any], header: Mapping[str, Any]) -> tuple[str, str]:
    config = best.get("subeventConfig")
    if isinstance(config, Mapping):
        home = str(config.get("homeTeamName") or "").strip()
        away = str(config.get("awayTeamName") or "").strip()
        if home and away:
            return home, away
        name = str(config.get("name") or "").strip()
        split = _split_match_name(name)
        if split is not None:
            return split
    split = _split_match_name(str(header.get("subeventName") or ""))
    if split is not None:
        return split
    return "", ""


def _split_match_name(name: str) -> tuple[str, str] | None:
    for separator in (" vs ", " v "):
        if separator in name:
            home, away = name.split(separator, 1)
            if home.strip() and away.strip():
                return home.strip(), away.strip()
    if " at " in name:
        away, home = name.split(" at ", 1)
        if home.strip() and away.strip():
            return home.strip(), away.strip()
    return None


def _event_id(best: Mapping[str, Any], header: Mapping[str, Any], url: str) -> str:
    config = best.get("subeventConfig")
    if isinstance(config, Mapping):
        subevent_id = str(config.get("subeventId") or "").strip()
        if subevent_id:
            return f"oddschecker:{subevent_id}"
    for crumb in header.get("breadcrumbs", []):
        if isinstance(crumb, Mapping) and crumb.get("type") == "subevent":
            crumb_id = str(crumb.get("id") or "").strip()
            if crumb_id:
                return f"oddschecker:{crumb_id}"
    return _event_id_from_url(url)


def parse_match_page(
    html: str,
    *,
    url: str,
    directory: EventDirectory,
    now: datetime | None = None,
    markets: Sequence[Market] | None = None,
    max_snapshots: int = MAX_SNAPSHOTS_PER_MATCH,
) -> list[OddsSnapshotIn]:
    """Parse one OddsChecker match page into normalized odds snapshots."""
    ingested_at = now or _utcnow()
    header = _find_header_payload(html)
    payload = _find_match_payload(html, prefer_subevent_id=_header_subevent_id(header))
    best = payload["bestOdds"]
    if not isinstance(best, Mapping):
        raise OddsCheckerParseError("bestOdds payload is not an object")

    wanted = set(markets) if markets is not None else None
    bets = _entity_map(best.get("bets"))
    odds = best.get("odds")
    if not isinstance(odds, Mapping):
        raise OddsCheckerParseError("bestOdds.odds payload is not an object")
    market_entities = _entity_map(best.get("markets"))
    bookmaker_entities = _entity_map(best.get("bookmakers"))
    event_id = _event_id(best, header, url)
    home, away = _team_names(best, header)
    starts_at = _parse_datetime(header.get("subeventStartTime"))
    directory.register(
        event_id,
        EventTeams(home=home, away=away, league=_league_name(header), starts_at=starts_at),
    )

    fallback_captured = _parse_epoch_ms(payload.get("lastUpdated")) or ingested_at
    bet_names_by_market = _bet_names_by_market(bets)
    snapshots: list[OddsSnapshotIn] = []
    for bet_id, per_book in odds.items():
        bet = bets.get(str(bet_id))
        if not isinstance(bet, Mapping) or not isinstance(per_book, Mapping):
            continue
        market = market_entities.get(str(bet.get("marketId") or ""))
        if not isinstance(market, Mapping):
            continue
        selection = str(bet.get("betName") or "").strip()
        if not selection:
            continue
        market_type = str(market.get("marketTypeName") or "")
        exact_sets_map = _exact_sets_totals_map(
            market_type, bet_names_by_market.get(str(bet.get("marketId") or ""), ())
        )
        if exact_sets_map is not None:
            # Proven-bo3 exact-sets bet -> the canonical set-totals group.
            exact_sets_selection = exact_sets_map.get(selection.lower())
            if exact_sets_selection is None:
                continue
            market_key = Market.TOTALS
            market_detail = _EXACT_SETS_BO3_DETAIL
            if wanted is not None and market_key not in wanted:
                continue
            selection = exact_sets_selection
        else:
            mapped = _market_for_type(market_type, bet.get("line"), selection)
            if mapped is None:
                continue
            market_key, market_detail = mapped
            if wanted is not None and market_key not in wanted:
                continue
            canonical = _canonical_selection(selection, bet.get("line"), market_key)
            if canonical is None:
                continue  # ambiguous selection (e.g. scoreline-less correct score)
            selection = canonical
        for code, raw_odd in per_book.items():
            if not isinstance(raw_odd, Mapping):
                continue
            if str(raw_odd.get("status") or "").upper() != "ACTIVE":
                continue
            if raw_odd.get("expired") is True or raw_odd.get("notExpired") is False:
                continue
            decimal = _decimal(raw_odd.get("oddsDecimal"))
            if decimal is None:
                continue
            bookmaker = _bookmaker_name(str(code), bookmaker_entities)
            if bookmaker is None:
                continue
            captured_at = _provider_capture_time(
                raw_odd.get("betFeedTimestamp"),
                fallback=fallback_captured,
                ingested_at=ingested_at,
            )
            if captured_at is None:
                continue
            if len(snapshots) >= max_snapshots:
                raise OddsCheckerSecurityError(f"match exceeded snapshot ceiling ({max_snapshots})")
            snapshots.append(
                OddsSnapshotIn(
                    event_id=event_id,
                    bookmaker=bookmaker,
                    market=market_key,
                    selection=selection,
                    decimal_odds=decimal,
                    captured_at=captured_at,
                    ingested_at=ingested_at,
                    market_detail=market_detail,
                )
            )
    return snapshots


def _league_name(header: Mapping[str, Any]) -> str:
    breadcrumbs = header.get("breadcrumbs")
    if isinstance(breadcrumbs, Sequence) and not isinstance(breadcrumbs, (str, bytes)):
        names = [
            str(item.get("name") or "").strip()
            for item in breadcrumbs
            if isinstance(item, Mapping) and item.get("type") == "card"
        ]
        if names:
            return names[-1]
    return str(header.get("eventName") or "").strip()


def _decimal(value: Any) -> float | None:
    try:
        decimal = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(decimal) or decimal <= 1.0 or decimal > MAX_DECIMAL_ODDS:
        return None
    return decimal


def _is_nonfinite_numeric(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return True
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(numeric)


def _provider_capture_time(
    value: Any,
    *,
    fallback: datetime,
    ingested_at: datetime,
) -> datetime | None:
    """Validate an optional provider timestamp without masking malformed data."""
    supplied = value is not None and str(value).strip() != ""
    captured_at = _parse_datetime(value) if supplied else fallback
    if captured_at is None:
        return None
    if captured_at > ingested_at + timedelta(minutes=5):
        return None
    return captured_at


def supported_market_ids_from_match_page(
    html: str,
    *,
    markets: Sequence[Market] | None = None,
    include_other: bool = False,
) -> list[str]:
    """Return market ids exposed in a modern match page payload.

    Mapped (devig-sound) markets always. With ``include_other`` (and no
    ``markets`` filter) ALSO returns unmapped, non-boost market ids so the
    all-odds API can be queried for capture-only OTHER markets — the OTHER
    emission is still sharp-anchor-gated in ``parse_market_api_payloads``."""
    wanted = set(markets) if markets is not None else None
    try:
        header = _find_header_payload(html)
        payload = _find_match_payload(html, prefer_subevent_id=_header_subevent_id(header))
    except OddsCheckerParseError:
        return []
    best = payload.get("bestOdds")
    if not isinstance(best, Mapping):
        return []
    market_entities = _entity_map(best.get("markets"))
    bet_names_by_market = _bet_names_by_market(_entity_map(best.get("bets")))
    ids: list[str] = []
    for market_id, market in market_entities.items():
        if not isinstance(market, Mapping):
            continue
        market_type = str(market.get("marketTypeName") or "")
        mapped = _market_for_type(market_type, None)
        if mapped is None:
            if (
                _exact_sets_totals_map(market_type, bet_names_by_market.get(market_id, ()))
                is not None
            ):
                # Proven-bo3 exact-sets market: a MAPPED set-totals close
                # source now — requested without the OTHER opt-in.
                if wanted is not None and Market.TOTALS not in wanted:
                    continue
            # Capture-only path: unmapped, non-boost markets, but ONLY when no
            # explicit Market filter is set (capture is all-or-nothing).
            elif not include_other or wanted is not None or _is_boost_market(market_type):
                continue
        else:
            market_key, _detail = mapped
            if wanted is not None and market_key not in wanted:
                continue
        ids.append(str(market.get("ocMarketId") or market_id))
        if len(ids) > MAX_MARKET_IDS_PER_MATCH:
            raise OddsCheckerSecurityError("match page exceeded market ID ceiling")
    return ids


async def fetch_market_api_payloads(
    market_ids: Sequence[str],
    *,
    referer: str,
    session: AsyncGetSession | None = None,
    proxy: ScraperProxy | None = None,
    chunk_size: int = MARKET_API_CHUNK_SIZE,
) -> list[Mapping[str, Any]]:
    """Load full all-odds market JSON for modern OddsChecker match pages."""
    deduped = [market_id for market_id in dict.fromkeys(market_ids) if market_id]
    payloads: list[Mapping[str, Any]] = []
    for start in range(0, len(deduped), max(1, chunk_size)):
        chunk = deduped[start : start + chunk_size]
        url = _normalize_url(
            f"api/markets/v2/all-odds?market-ids={','.join(chunk)}&repub=OC",
            base_url=_site_root(referer),
        )
        payload = await fetch_json_value(url, session=session, proxy=proxy, referer=referer)
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            raise OddsCheckerParseError("oddschecker market API response is not a list")
        chunk_payloads: list[Mapping[str, Any]] = []
        for item in payload:
            if not isinstance(item, Mapping):
                raise OddsCheckerParseError("oddschecker market API returned malformed rows")
            raw_bets = item.get("bets")
            raw_odds = item.get("odds")
            if (
                not isinstance(raw_bets, Sequence)
                or isinstance(raw_bets, (str, bytes))
                or not isinstance(raw_odds, Sequence)
                or isinstance(raw_odds, (str, bytes))
            ):
                raise OddsCheckerParseError("oddschecker market API returned malformed rows")
            chunk_payloads.append(item)
        returned_market_ids = {
            str(item.get("marketId") or item.get("ocMarketId") or "") for item in chunk_payloads
        }
        missing_market_ids = set(chunk) - returned_market_ids
        if missing_market_ids:
            raise OddsCheckerParseError("oddschecker market API returned an incomplete chunk")
        payloads.extend(chunk_payloads)
    return payloads


def parse_market_api_payloads(
    payloads: Sequence[Mapping[str, Any]],
    *,
    url: str,
    directory: EventDirectory,
    now: datetime | None = None,
    markets: Sequence[Market] | None = None,
    capture_other: bool = False,
    capture_only_other: bool = False,
    truncate_on_limit: bool = False,
    max_snapshots: int = MAX_SNAPSHOTS_PER_MATCH,
) -> list[OddsSnapshotIn]:
    """Parse ``/api/markets/v2/all-odds`` payloads into normalized snapshots.

    With ``capture_other`` (and no ``markets`` filter), unmapped non-boost
    markets that carry a sharp-anchor (Betfair Exchange) quote are captured
    under ``Market.OTHER`` with an ``oc_<slug>`` market_detail — odds history
    only (never priced/settled; see the OTHER enum note). ``capture_only_other``
    isolates that optional archive from mapped, pick-critical markets.
    ``truncate_on_limit`` is valid only for that optional-only mode: it retains
    a bounded prefix rather than letting archive overflow fail a mapped match.
    """
    if truncate_on_limit and not capture_only_other:
        raise ValueError("truncate_on_limit requires capture_only_other")
    ingested_at = now or _utcnow()
    wanted = set(markets) if markets is not None else None
    snapshots: list[OddsSnapshotIn] = []
    for market_payload in payloads:
        market_type = str(market_payload.get("marketTypeName") or "")
        raw_bets = market_payload.get("bets")
        raw_odds = market_payload.get("odds")
        if not isinstance(raw_bets, Sequence) or isinstance(raw_bets, (str, bytes)):
            continue
        if not isinstance(raw_odds, Sequence) or isinstance(raw_odds, (str, bytes)):
            continue
        # Sharp-anchor gate for capture-only OTHER markets (computed once/market).
        other_ok = (
            capture_other
            and wanted is None
            and not _is_boost_market(market_type)
            and _odds_have_sharp_anchor(raw_odds)
        )
        # Best-of-3 exact-sets bijection (computed once/market from the FULL
        # bet-name set — the market-level proof of format).
        exact_sets_map = _exact_sets_totals_map(
            market_type,
            (str(b.get("betName") or "") for b in raw_bets if isinstance(b, Mapping)),
        )
        event_id = _api_event_id(market_payload, url)
        home, away = _split_match_name(str(market_payload.get("subeventName") or "")) or ("", "")
        if not home and not away:
            # The subeventName separator was unrecognised; fall back to the
            # structured team fields when the all-odds payload carries them
            # (additive: absent -> unchanged, so no orientation guessing).
            home = str(market_payload.get("homeTeamName") or "").strip()
            away = str(market_payload.get("awayTeamName") or "").strip()
        directory.register(
            event_id,
            EventTeams(
                home=home,
                away=away,
                league=_clean_league_name(str(market_payload.get("eventName") or "")),
                starts_at=_parse_datetime(market_payload.get("subeventStartTime")),
            ),
        )
        odds_by_bet: dict[str, list[Mapping[str, Any]]] = {}
        for raw_odd in raw_odds:
            if not isinstance(raw_odd, Mapping):
                continue
            bet_id = str(raw_odd.get("betId") or "")
            if not bet_id:
                continue
            odds_by_bet.setdefault(bet_id, []).append(raw_odd)
        # Thread the payload's bookmaker entities so off-map book codes resolve to
        # the feed's canonical name (matches the bestOdds path); {} when absent.
        bm_entities = _entity_map(market_payload.get("bookmakers"))
        for raw_bet in raw_bets:
            if not isinstance(raw_bet, Mapping):
                continue
            selection = str(raw_bet.get("betName") or "").strip()
            if not selection:
                continue
            line = raw_bet.get("line")
            if _is_nonfinite_numeric(line):
                continue
            exact_sets_selection = (
                None if exact_sets_map is None else exact_sets_map.get(selection.lower())
            )
            if exact_sets_selection is not None:
                # Proven-bo3 exact-sets bet -> the canonical set-totals group.
                if capture_only_other:
                    continue
                market_key = Market.TOTALS
                market_detail = _EXACT_SETS_BO3_DETAIL
                if wanted is not None and market_key not in wanted:
                    continue
                selection = exact_sets_selection
            elif (mapped := _market_for_type(market_type, line, selection)) is not None:
                if capture_only_other:
                    continue
                market_key, market_detail = mapped
                if wanted is not None and market_key not in wanted:
                    continue
                canonical = _canonical_selection(selection, line, market_key)
                if canonical is None:
                    continue  # ambiguous selection (e.g. scoreline-less correct score)
                selection = canonical
            elif other_ok:
                market_key = Market.OTHER
                market_detail = _other_market_detail(market_type, line)
            else:
                continue
            for raw_odd in odds_by_bet.get(str(raw_bet.get("betId") or ""), []):
                if str(raw_odd.get("status") or "").upper() != "ACTIVE":
                    continue
                if raw_odd.get("expired") is True or raw_odd.get("notExpired") is False:
                    continue
                decimal = _decimal(raw_odd.get("oddsDecimal"))
                if decimal is None:
                    continue
                bookmaker = _bookmaker_name(str(raw_odd.get("bookmakerCode") or ""), bm_entities)
                if bookmaker is None:
                    continue
                captured_at = _provider_capture_time(
                    raw_odd.get("betFeedTimestamp"),
                    fallback=ingested_at,
                    ingested_at=ingested_at,
                )
                if captured_at is None:
                    continue
                if len(snapshots) >= max_snapshots:
                    if truncate_on_limit:
                        return snapshots
                    raise OddsCheckerSecurityError(
                        f"match exceeded snapshot ceiling ({max_snapshots})"
                    )
                snapshots.append(
                    OddsSnapshotIn(
                        event_id=event_id,
                        bookmaker=bookmaker,
                        market=market_key,
                        selection=selection,
                        decimal_odds=decimal,
                        captured_at=captured_at,
                        ingested_at=ingested_at,
                        market_detail=market_detail,
                    )
                )
    return snapshots


def _api_event_id(payload: Mapping[str, Any], url: str) -> str:
    subevent_id = str(payload.get("subeventId") or "").strip()
    if subevent_id:
        return f"oddschecker:{subevent_id}"
    return _legacy_event_id_from_url(url)


def _clean_league_name(name: str) -> str:
    return re.sub(r"\s+Matches$", "", name.strip())


def parse_legacy_match_page(
    html: str,
    *,
    url: str,
    directory: EventDirectory,
    now: datetime | None = None,
    markets: Sequence[Market] | None = None,
    max_snapshots: int = MAX_SNAPSHOTS_PER_MATCH,
) -> list[OddsSnapshotIn]:
    """Parse the older OddsChecker table grid used by basketball pages."""
    ingested_at = now or _utcnow()
    wanted = set(markets) if markets is not None else None
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.eventTable[data-mid]")
    if table is None:
        raise OddsCheckerParseError("no legacy OddsChecker event table found")
    event_id = _legacy_event_id_from_url(url)
    home, away = _split_match_name(str(table.get("data-sname") or "")) or ("", "")
    directory.register(
        event_id,
        EventTeams(
            home=home,
            away=away,
            league=str(table.get("data-ename") or "").strip(),
            starts_at=_parse_legacy_table_datetime(table.get("data-time")),
        ),
    )
    market_type = str(table.get("data-mname") or "").strip()
    snapshots: list[OddsSnapshotIn] = []
    for row in table.select("tbody tr.diff-row.evTabRow"):
        raw_selection = _legacy_row_selection(row)
        if not raw_selection:
            continue
        line = _legacy_row_line(row, raw_selection)
        mapped = _market_for_type(market_type, line, raw_selection)
        if mapped is None:
            continue
        market_key, market_detail = mapped
        if wanted is not None and market_key not in wanted:
            continue
        selection = _canonical_selection(raw_selection, line, market_key)
        if selection is None:
            continue  # ambiguous selection (e.g. scoreline-less correct score)
        for cell in row.select("td[data-bk][data-odig]"):
            decimal = _decimal(cell.get("data-odig"))
            if decimal is None:
                continue
            bookmaker_code = str(cell.get("data-bk") or "").strip()
            if not bookmaker_code:
                continue
            bookmaker = _bookmaker_name(bookmaker_code, {})
            if bookmaker is None:
                continue
            if len(snapshots) >= max_snapshots:
                raise OddsCheckerSecurityError(f"match exceeded snapshot ceiling ({max_snapshots})")
            snapshots.append(
                OddsSnapshotIn(
                    event_id=event_id,
                    bookmaker=bookmaker,
                    market=market_key,
                    selection=selection,
                    decimal_odds=decimal,
                    captured_at=ingested_at,
                    ingested_at=ingested_at,
                    market_detail=market_detail,
                )
            )
    return snapshots


def discover_legacy_market_urls(
    html: str,
    *,
    base_url: str,
    markets: Sequence[Market] | None = None,
) -> list[str]:
    """Extract linked old-grid market pages for supported markets."""
    wanted = set(markets) if markets is not None else None
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select(".market-dd a[href]"):
        market_name = anchor.get_text(" ", strip=True)
        mapped = _market_for_type(market_name, None, market_name)
        if mapped is None:
            continue
        market_key, _detail = mapped
        if wanted is not None and market_key not in wanted:
            continue
        href = str(anchor.get("href") or "")
        if not href or href.startswith("javascript:"):
            continue
        absolute = _normalize_site_href(href, base_url=base_url)
        if absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
    return urls


def _legacy_event_id_from_url(url: str) -> str:
    parsed = urlparse(_normalize_url(url))
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if parts:
        parts = parts[:-1]
    path = "/".join(parts) or parsed.path.strip("/") or "home"
    return f"oddschecker:{path}"


def _parse_legacy_table_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=_LONDON).astimezone(UTC)
    return _parse_datetime(text)


def _legacy_row_selection(row: Any) -> str:
    value = str(row.get("data-bname") or "").strip()
    if value:
        return value
    first_cell = row.select_one("td.sel")
    if first_cell is None:
        return ""
    return first_cell.get_text(" ", strip=True)


def _legacy_row_line(row: Any, selection: str) -> str | None:
    first_price = row.select_one("td[data-hcap]")
    if first_price is not None:
        hcap = str(first_price.get("data-hcap") or "").strip()
        if hcap:
            return hcap
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*$", selection.strip())
    if match is None:
        return None
    return match.group(1)


_LINE_BEARING_MARKETS: frozenset[Market] = frozenset(
    {Market.SPREADS, Market.TOTALS, Market.TEAM_TOTALS}
)


def _line_value(line: Any) -> float | None:
    if line is None or line == "":
        return None
    try:
        value = float(line)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _line_bearing_selection(selection: str, line: Any, market: Market) -> str:
    """Bake the line into a line-market selection, mirroring the OddsPortal
    contract (app/ingestion/oddsportal.py: ``Over {line:g}`` for totals,
    ``{team} {line:+g}`` for spreads/AH).

    OddsChecker emits a BARE betName ("Over"/"Under" or a bare team name) and
    carries the line separately. The whole downstream — the CLV re-price keys,
    the settlement parsers, and the picks uniqueness constraint — keys on
    ``selection`` and expects the line to live INSIDE it (as it does under
    OddsPortal). This is the exact reverse of the old ``_strip_selection_line``
    used by the legacy grid path; the line is appended once, never duplicated."""
    if market not in _LINE_BEARING_MARKETS:
        return selection
    value = _line_value(line)
    if value is None:
        return selection
    suffix = f"{value:+g}" if market is Market.SPREADS else f"{value:g}"
    if selection.endswith(suffix):
        return selection  # already line-bearing (legacy grid rows carry it in the name)
    return f"{selection} {suffix}".strip()


# BTTS selection vocabulary (warehouse evidence 2026-07-11): the feed has
# emitted BOTH bare 'Yes'/'No' (every Betfair Exchange inline row, and all
# soft rows since 2026-07-05) and a prefixed 'BTTS Yes'/'BTTS No' soft form
# (~66k rows each through 2026-07-05). Two forms in one two-outcome market
# split the devig group into four selections and broke the close true-up's
# exact-selection match (0 sharp snapshot closes across all btts picks).
_BTTS_SELECTION_PREFIX_RE = re.compile(r"^\s*btts\s+", re.IGNORECASE)

# CORRECT SCORE selections must carry an explicit scoreline ("2-1", "1:1",
# "Arsenal 2-1") to identify ONE outcome. The feed also emits scoreline-less
# bet names ("Draw", a bare team name) on this market; persisting those
# collapses DISTINCT scorelines onto one snapshot key (audit 2026-07-10
# L-oddschecker-969: every draw scoreline landed on selection='Draw') —
# poisoning the archive and any future devig group. No scoreline => the
# outcome is ambiguous => the bet is DROPPED (fail-closed), never guessed.
_SCORELINE_RE = re.compile(r"\d+\s*[-:–]\s*\d+")


def _canonical_selection(selection: str, line: Any, market: Market) -> str | None:
    """The platform-canonical selection for one mapped bet, or None to DROP it.

    BTTS normalizes to the bare 'Yes'/'No' form the Betfair Exchange rows use
    (see ``_BTTS_SELECTION_PREFIX_RE``); CORRECT_SCORE requires an explicit
    scoreline in the bet name (see ``_SCORELINE_RE`` — ambiguous names are
    dropped, fail-closed); every other market keeps the OddsPortal-parity
    line-bearing contract of ``_line_bearing_selection``."""
    if market is Market.BTTS:
        stripped = _BTTS_SELECTION_PREFIX_RE.sub("", selection).strip()
        return stripped or selection
    if market is Market.CORRECT_SCORE:
        return selection if _SCORELINE_RE.search(selection) else None
    return _line_bearing_selection(selection, line, market)


# Betfair Exchange prices NO tennis set-totals Over/Under on OddsChecker
# (warehouse 2026-07-11: 0 Exchange rows on totals_2_5/3_5/4_5 — the lines
# tennis set-total picks live on); it prices the SAME best-of-3 event only as
# the exact-sets market ("Total Sets Exact", bets "2 Sets"/"3 Sets" — 422
# Exchange rows stranded under capture-only ``oc_total_sets_exact``). In a
# best-of-3, exactly-2-sets IS under-2.5-sets and exactly-3-sets IS
# over-2.5-sets: a bijective RENAME, never a derived/summed price. Only the
# two-outcome {2 Sets, 3 Sets} form maps — that bet set itself proves
# best-of-3. A best-of-5 exact market ({3,4,5} Sets) has no per-selection
# Over/Under bijection and stays unmapped (fail-closed; OTHER at most).
_EXACT_SETS_MARKET_TYPE = "total sets exact"
_EXACT_SETS_BO3_SELECTIONS: Mapping[str, str] = MappingProxyType(
    {"2 sets": "Under 2.5", "3 sets": "Over 2.5"}
)
_EXACT_SETS_BO3_DETAIL = "totals_2_5"


def _exact_sets_totals_map(market_type: str, bet_names: Iterable[str]) -> Mapping[str, str] | None:
    """Lowered exact-sets bet name -> canonical set-totals selection, or None
    when the market is not the provably-best-of-3 exact-sets form."""
    if market_type.strip().lower() != _EXACT_SETS_MARKET_TYPE:
        return None
    names = {str(name).strip().lower() for name in bet_names}
    names.discard("")
    if names != set(_EXACT_SETS_BO3_SELECTIONS):
        return None
    return _EXACT_SETS_BO3_SELECTIONS


def _bet_names_by_market(bets: Mapping[str, Any]) -> dict[str, list[str]]:
    """Per-marketId bet-name lists from a bestOdds ``bets`` entity map — the
    market-level evidence ``_exact_sets_totals_map`` needs on the page path."""
    names: dict[str, list[str]] = {}
    for bet in bets.values():
        if isinstance(bet, Mapping):
            names.setdefault(str(bet.get("marketId") or ""), []).append(
                str(bet.get("betName") or "")
            )
    return names


def parse_competition_match_urls(
    html: str,
    *,
    base_url: str = ODDSCHECKER_BASE_URL,
) -> list[str]:
    """Extract match compare-odds URLs from a competition/listing page."""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select('[data-hypernova-key="competitionsaccumulatormatches"] a[href]'):
        href = str(anchor.get("href") or "")
        if "/winner" not in href:
            continue
        absolute = _normalize_site_href(href, base_url=base_url)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
            if len(urls) > MAX_MATCH_URLS_PER_CYCLE:
                raise OddsCheckerSecurityError("competition exceeded match URL ceiling")
    return urls


# Pipeline sport key (see app/pipeline.py / app/scheduler.py) -> OddsChecker
# sport key. OddsPortal/pipeline call football "soccer"; OddsChecker calls it
# "football". The rest are 1:1. Only keys present here can be scheduler-driven.
SCHEDULER_SPORT_KEY_MAP: Mapping[str, str] = MappingProxyType(
    {
        "soccer": "football",
        "basketball": "basketball",
        "tennis": "tennis",
        "american_football": "american_football",
    }
)


def _new_impersonated_session(proxy: ScraperProxy | None) -> AsyncGetSession:
    """Build one persistent curl_cffi AsyncSession, optionally proxy-bound.

    Reused across many match-page fetches in a poll cycle (keep-alive +
    connection pool + retained cf cookies) — the validated
    ``session-reuse-per-proxy`` strategy. Same impersonation profile and read
    timeout as the per-call fetch path; the pool that owns it closes it."""
    from curl_cffi.requests import AsyncSession

    kwargs: dict[str, Any] = {
        "impersonate": PINNED_IMPERSONATE,
        "default_headers": True,
        "timeout": DEFAULT_TIMEOUT,
        "allow_redirects": False,
    }
    if proxy is not None and proxy.url:
        inline = _proxy_with_creds(proxy)
        kwargs["proxies"] = {"http": inline, "https": inline}
    # curl_cffi types get() with a narrower **RequestParams than the AsyncGetSession
    # Protocol's **Any; the shapes we use are compatible (the per-call fetch path
    # uses AsyncSession the same way via `async with`).
    return cast(AsyncGetSession, AsyncSession(**kwargs))


class _ProxySessionPool:
    """One persistent AsyncSession per proxy, reused across a poll cycle.

    ``acquire`` round-robins the proxy pool and returns that proxy's session,
    created lazily on first use and cached — so match-page fetches keep the TLS
    handshake + connection pool + cf cookies warm instead of paying a cold
    handshake per request. ``aclose`` closes every created session at cycle end
    (never leaked). Single-threaded asyncio: ``acquire`` has no await points, so
    no lock is needed."""

    def __init__(
        self,
        proxies: Sequence[ScraperProxy],
        *,
        session_factory: Callable[[ScraperProxy | None], AsyncGetSession] | None = None,
    ) -> None:
        self._proxies = tuple(proxies)
        # Resolved at call time (not bound as a default) so tests can monkeypatch
        # the module-level factory.
        self._factory = session_factory or _new_impersonated_session
        self._sessions: dict[int, AsyncGetSession] = {}
        self._lease_counts: dict[tuple[int, int], int] = {}
        self._retired_sessions: dict[tuple[int, int], AsyncGetSession] = {}
        self._cursor = 0

    @staticmethod
    def _lease_key(lease: _ProxySessionLease) -> tuple[int, int]:
        return lease.index, id(lease.session)

    def acquire_lease(
        self,
        *,
        exclude_indices: frozenset[int] = frozenset(),
    ) -> _ProxySessionLease:
        if not self._proxies:
            raise OddsCheckerError("proxy session pool is empty")
        index: int | None = None
        for _ in range(len(self._proxies)):
            candidate = self._cursor % len(self._proxies)
            self._cursor += 1
            if candidate not in exclude_indices:
                index = candidate
                break
        if index is None:
            raise OddsCheckerError("no proxy session remains after exclusions")
        session = self._sessions.get(index)
        if session is None:
            session = self._factory(self._proxies[index])
            self._sessions[index] = session
        lease = _ProxySessionLease(index=index, session=session)
        key = self._lease_key(lease)
        self._lease_counts[key] = self._lease_counts.get(key, 0) + 1
        return lease

    def acquire(self) -> AsyncGetSession:
        """Compatibility wrapper for callers that do not need lease identity."""
        return self.acquire_lease().session

    @staticmethod
    async def _close_session(session: AsyncGetSession) -> None:
        close = getattr(session, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def evict(self, lease: _ProxySessionLease) -> None:
        """Retire the failed session without closing it under active siblings.

        New acquisitions stop seeing it immediately. The actual close is
        deferred until every outstanding lease on that exact session releases,
        preventing one challenged request from cancelling healthy in-flight
        siblings that share the per-proxy connection pool.
        """
        current = self._sessions.get(lease.index)
        if current is not lease.session:
            return
        self._sessions.pop(lease.index, None)
        key = self._lease_key(lease)
        if self._lease_counts.get(key, 0) > 0:
            self._retired_sessions[key] = lease.session
            return
        try:
            await self._close_session(lease.session)
        except Exception as exc:
            logger.warning(
                "oddschecker proxy session eviction close failed (%s)",
                type(exc).__name__,
            )

    async def release(self, lease: _ProxySessionLease) -> None:
        """Release one borrower and close a retired session at refcount zero."""
        key = self._lease_key(lease)
        count = self._lease_counts.get(key, 0)
        if count <= 1:
            self._lease_counts.pop(key, None)
            retired = self._retired_sessions.pop(key, None)
            if retired is None:
                return
            try:
                await self._close_session(retired)
            except Exception as exc:
                logger.warning(
                    "oddschecker retired proxy session close failed (%s)",
                    type(exc).__name__,
                )
            return
        self._lease_counts[key] = count - 1

    async def aclose(self) -> None:
        sessions_by_id = {
            id(session): session
            for session in (*self._sessions.values(), *self._retired_sessions.values())
        }
        self._sessions.clear()
        self._retired_sessions.clear()
        self._lease_counts.clear()
        results = await asyncio.gather(
            *(self._close_session(session) for session in sessions_by_id.values()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("oddschecker proxy session close failed (%s)", type(result).__name__)


class OddsCheckerLoader:
    """Fetch OddsChecker match URLs or competition pages over curl_cffi."""

    #: OddsChecker's fetch_match_odds is url-based (a single match page), NOT the
    #: OddsPortal (sport_key, match_links, ...) scrape API. Opt out of the
    #: clv_trueup off-window re-price / finished-score capture passes so they
    #: SKIP this loader instead of raising TypeError (dual-provider fix).
    supports_match_scrape: bool = False

    def __init__(
        self,
        directory: EventDirectory,
        *,
        match_urls: Sequence[str] = (),
        competition_urls: Sequence[str] = (),
        discover_football_daily: bool = False,
        discover_sport_daily_keys: Sequence[str] = (),
        football_daily_home_url: str = FOOTBALL_HOME_URL,
        football_daily_start_date: date | None = None,
        football_daily_days: int = 2,
        proxy_pool: Sequence[ScraperProxy] = (),
        max_clients: int = DEFAULT_MAX_CLIENTS,
        markets: Sequence[Market] | None = None,
        scheduler_sport_keys: Sequence[str] = (),
        capture_other: bool = False,
    ) -> None:
        self._directory = directory
        self._match_urls = tuple(match_urls)
        self._competition_urls = tuple(competition_urls)
        self._discover_football_daily = discover_football_daily
        self._discover_sport_daily_keys = tuple(discover_sport_daily_keys)
        self._football_daily_home_url = football_daily_home_url
        self._football_daily_start_date = football_daily_start_date
        self._football_daily_days = football_daily_days
        self._proxy_pool = tuple(proxy_pool)
        self._proxy_cursor = 0
        # Per-proxy persistent-session pool, live only for the duration of one
        # fetch_odds cycle (created/closed in _run_with_session). None outside a
        # cycle and whenever a session is injected (tests) or no proxy pool.
        self._session_pool: _ProxySessionPool | None = None
        # Eight bounded responses is the memory/concurrency budget used by the
        # scheduler configuration. Clamp direct construction too, so callers
        # cannot bypass the 3 GiB container safety envelope.
        self._max_clients = max(1, min(DEFAULT_MAX_CLIENTS, max_clients))
        self._markets = tuple(markets) if markets is not None else None
        # Per-sport scheduler mode: when set, ``fetch_odds(sport_key)`` discovers
        # and parses ONLY that pipeline sport key's slate (mapped to an
        # OddsChecker sport key). Empty = legacy constructor-driven behavior.
        self._scheduler_sport_keys = tuple(scheduler_sport_keys)
        # Capture-only: also persist sharp-anchored unmapped markets (props /
        # period / combo) as Market.OTHER odds history. They never mint picks.
        self._capture_other = capture_other
        # Duck-typed listing telemetry read by the pipeline's AVAILABLE GAMES
        # view (getattr with None default — see app/pipeline.py). Populated per
        # pipeline sport key on each fetch in scheduler mode.
        self.last_fetch_matches: dict[str, int] = {}
        self.last_fetch_event_ids: dict[str, tuple[str, ...]] = {}
        # Partial siblings remain persistable/visible, but pipeline picks are
        # withheld unless every listed match completed successfully.
        self.last_fetch_complete: dict[str, bool] = {}
        self.last_fetch_completeness_reason: dict[str, str] = {}
        # Fraction of this cycle's listed match-page fetches that FAILED. Read by
        # the pipeline's completeness tolerance so a few timed-out pages (partial
        # coverage) do not fail the whole cycle closed. 0.0 = fully complete.
        self.last_fetch_incomplete_ratio: dict[str, float] = {}

    @classmethod
    def football_today_tomorrow(
        cls,
        directory: EventDirectory,
        *,
        proxy_pool: Sequence[ScraperProxy] = (),
        max_clients: int = DEFAULT_MAX_CLIENTS,
        markets: Sequence[Market] | None = None,
        start_date: date | None = None,
    ) -> OddsCheckerLoader:
        """Convenience loader for the fast OddsChecker football daily slate."""
        return cls(
            directory,
            discover_football_daily=True,
            football_daily_start_date=start_date,
            football_daily_days=2,
            proxy_pool=proxy_pool,
            max_clients=max_clients,
            markets=markets,
        )

    @classmethod
    def sports_today_tomorrow(
        cls,
        directory: EventDirectory,
        *,
        sport_keys: Sequence[str] = ("football", "basketball", "tennis", "american_football"),
        proxy_pool: Sequence[ScraperProxy] = (),
        max_clients: int = DEFAULT_MAX_CLIENTS,
        markets: Sequence[Market] | None = None,
        start_date: date | None = None,
    ) -> OddsCheckerLoader:
        """Convenience loader for all supported OddsChecker daily sport slates."""
        return cls(
            directory,
            discover_sport_daily_keys=sport_keys,
            football_daily_start_date=start_date,
            football_daily_days=2,
            proxy_pool=proxy_pool,
            max_clients=max_clients,
            markets=markets,
        )

    @classmethod
    def for_scheduler(
        cls,
        directory: EventDirectory,
        *,
        sport_keys: Sequence[str] = ("soccer", "basketball", "tennis", "american_football"),
        days: int = 2,
        proxy_pool: Sequence[ScraperProxy] = (),
        max_clients: int = DEFAULT_MAX_CLIENTS,
        markets: Sequence[Market] | None = None,
        start_date: date | None = None,
        capture_other: bool = False,
    ) -> OddsCheckerLoader:
        """Scheduler loader: ``fetch_odds(pipeline_key)`` fetches ONLY that sport.

        ``sport_keys`` are PIPELINE keys ("soccer", "basketball", "tennis",
        "american_football"); unknown keys are dropped. ``markets=None`` captures
        every supported market. ``capture_other`` additionally persists
        sharp-anchored unmapped markets (props/period) as Market.OTHER odds
        history. Datacenter-direct egress is Cloudflare-blocked, so a
        ``proxy_pool`` is effectively required in production.
        """
        wanted = tuple(key for key in sport_keys if key in SCHEDULER_SPORT_KEY_MAP)
        return cls(
            directory,
            football_daily_start_date=start_date,
            football_daily_days=days,
            proxy_pool=proxy_pool,
            max_clients=max_clients,
            markets=markets,
            scheduler_sport_keys=wanted,
            capture_other=capture_other,
        )

    def _next_proxy(self) -> ScraperProxy | None:
        if not self._proxy_pool:
            return None
        proxy = self._proxy_pool[self._proxy_cursor % len(self._proxy_pool)]
        self._proxy_cursor += 1
        return proxy

    async def fetch_match_odds(
        self,
        url: str,
        *,
        now: datetime | None = None,
        session: AsyncGetSession | None = None,
        markets: Sequence[Market] | None = None,
    ) -> list[OddsSnapshotIn]:
        # ``markets`` mirrors OddsPortalLoader.fetch_match_odds so the shared
        # off-window CLV re-price path (app/clv_trueup.py) can call either loader
        # with the same signature; None falls back to the loader's own scope.
        #
        # No injected session, but a poll cycle is active with a proxy pool:
        # borrow this match's persistent per-proxy session (round-robin). The
        # page fetch AND its market-API round-trip then share one warm session
        # (same proxy, kept-alive) instead of a cold handshake per request.
        if session is None and self._session_pool is not None:
            session = self._session_pool.acquire()
        page = await fetch_html(
            url,
            session=session,
            proxy=None if session is not None else self._next_proxy(),
        )
        snapshots = await self._parse_modern_or_legacy_match_page(
            page, now=now, session=session, markets=markets
        )
        if len(snapshots) > MAX_SNAPSHOTS_PER_MATCH:
            raise OddsCheckerSecurityError("match exceeded snapshot ceiling")
        return snapshots

    async def _parse_modern_or_legacy_match_page(
        self,
        page: OddsCheckerFetchResult,
        *,
        now: datetime | None,
        session: AsyncGetSession | None,
        markets: Sequence[Market] | None = None,
    ) -> list[OddsSnapshotIn]:
        eff_markets = markets if markets is not None else self._markets
        # Fetch mapped, pick-critical markets independently. Optional OTHER
        # capture must never turn a healthy mapped match into an incomplete one.
        mapped_market_ids = supported_market_ids_from_match_page(
            page.html, markets=eff_markets, include_other=False
        )
        optional_market_ids: list[str] = []
        if self._capture_other and eff_markets is None:
            try:
                all_market_ids = supported_market_ids_from_match_page(
                    page.html, markets=None, include_other=True
                )
            except OddsCheckerSecurityError as exc:
                logger.warning(
                    "oddschecker optional market capture skipped (%s)",
                    type(exc).__name__,
                )
            else:
                mapped_ids = set(mapped_market_ids)
                optional_market_ids = [
                    market_id for market_id in all_market_ids if market_id not in mapped_ids
                ]

        mapped_snapshots: list[OddsSnapshotIn] = []
        if mapped_market_ids:
            try:
                payloads = await fetch_market_api_payloads(
                    mapped_market_ids,
                    referer=page.url,
                    session=session,
                    proxy=None if session is not None else self._next_proxy(),
                )
                mapped_snapshots = parse_market_api_payloads(
                    payloads,
                    url=page.url,
                    directory=self._directory,
                    now=now,
                    markets=eff_markets,
                    capture_other=False,
                )
            except OddsCheckerError:
                # Any API failure after the page advertised supported market
                # ids is systemic for this match. Falling back to embedded
                # bestOdds would silently discard API-only markets and make a
                # partial slate look complete. Let the gather retain healthy
                # sibling matches while marking this cycle incomplete.
                raise

        if not mapped_snapshots:
            try:
                mapped_snapshots = parse_match_page(
                    page.html,
                    url=page.url,
                    directory=self._directory,
                    now=now,
                    markets=eff_markets,
                )
            except OddsCheckerParseError:
                mapped_snapshots = await self._parse_legacy_match_with_linked_markets(
                    page,
                    now=now,
                    session=session,
                    markets=eff_markets,
                )

        mapped_event_ids = frozenset(snapshot.event_id for snapshot in mapped_snapshots)
        remaining = MAX_SNAPSHOTS_PER_MATCH - len(mapped_snapshots)
        if not optional_market_ids or remaining <= 0 or not mapped_event_ids:
            if optional_market_ids and remaining <= 0:
                logger.warning("oddschecker optional market capture skipped (snapshot ceiling)")
            elif optional_market_ids and not mapped_event_ids:
                logger.warning(
                    "oddschecker optional capture skipped (mapped event identity unavailable)"
                )
            return mapped_snapshots

        try:
            optional_payloads = await fetch_market_api_payloads(
                optional_market_ids,
                referer=page.url,
                session=session,
                proxy=None if session is not None else self._next_proxy(),
            )
            # Optional archive payloads are not authoritative event metadata.
            # Parse them against a disposable registry so missing/corrupt fields
            # cannot overwrite the mapped match context, even if parsing raises
            # after registration and the archive exception is isolated below.
            optional_directory = EventDirectory()
            parsed_optional_snapshots = parse_market_api_payloads(
                optional_payloads,
                url=page.url,
                directory=optional_directory,
                now=now,
                capture_other=True,
                capture_only_other=True,
                truncate_on_limit=True,
                max_snapshots=remaining,
            )
            optional_snapshots = [
                snapshot
                for snapshot in parsed_optional_snapshots
                if snapshot.event_id in mapped_event_ids
            ]
            if len(optional_snapshots) != len(parsed_optional_snapshots):
                logger.warning(
                    "oddschecker optional market capture discarded (event identity mismatch)"
                )
        except Exception as exc:
            # OTHER is an archive-only surface. Keep mapped snapshots and emit
            # only the safe exception class (never a URL/body/proxy credential).
            logger.warning(
                "oddschecker optional market capture skipped (%s)",
                type(exc).__name__,
            )
            return mapped_snapshots
        return [*mapped_snapshots, *optional_snapshots]

    async def _parse_legacy_match_with_linked_markets(
        self,
        page: OddsCheckerFetchResult,
        *,
        now: datetime | None,
        session: AsyncGetSession | None,
        markets: Sequence[Market] | None = None,
    ) -> list[OddsSnapshotIn]:
        # ``markets`` is the caller's effective scope (override or self._markets),
        # threaded from _parse_modern_or_legacy_match_page so the legacy fallback
        # honors a fetch_match_odds override too; None falls back to loader scope.
        eff_markets = markets if markets is not None else self._markets
        snapshots = parse_legacy_match_page(
            page.html,
            url=page.url,
            directory=self._directory,
            now=now,
            markets=eff_markets,
        )
        linked_urls = [
            url
            for url in discover_legacy_market_urls(
                page.html,
                base_url=page.url,
                markets=eff_markets,
            )
            if url != page.url
        ]
        if len(linked_urls) > MAX_LEGACY_MARKET_URLS_PER_MATCH:
            raise OddsCheckerSecurityError("legacy match exceeded linked-market URL ceiling")
        if not linked_urls:
            return snapshots
        # Legacy linked markets are rare and bounded. Parse them serially with
        # the exact remaining row budget so no concurrent sibling can allocate
        # beyond the per-match ceiling before the aggregate check runs.
        for url in linked_urls:
            remaining = MAX_SNAPSHOTS_PER_MATCH - len(snapshots)
            if remaining <= 0:
                raise OddsCheckerSecurityError("legacy match exceeded snapshot ceiling")
            try:
                linked_page = await fetch_html(
                    url,
                    session=session,
                    proxy=None if session is not None else self._next_proxy(),
                )
                result = parse_legacy_match_page(
                    linked_page.html,
                    url=linked_page.url,
                    directory=self._directory,
                    now=now,
                    markets=eff_markets,
                    max_snapshots=remaining,
                )
            except OddsCheckerSecurityError:
                raise
            except Exception as exc:
                logger.debug(
                    "oddschecker linked legacy market skipped (%s)",
                    type(exc).__name__,
                )
                continue
            snapshots.extend(result)
        return snapshots

    async def fetch_competition_match_urls(
        self, url: str, *, session: AsyncGetSession | None = None
    ) -> list[str]:
        page = await fetch_html(
            url,
            session=session,
            proxy=None if session is not None else self._next_proxy(),
        )
        return parse_competition_match_urls(page.html, base_url=page.url)

    async def fetch_odds(
        self, sport_key: str, *, session: AsyncGetSession | None = None
    ) -> Sequence[OddsSnapshotIn]:
        """Fetch OddsChecker odds for the given sport.

        In scheduler mode (``scheduler_sport_keys`` set) this discovers and
        parses ONLY the given pipeline ``sport_key``'s daily slate. Otherwise
        ``sport_key`` is ignored and URL selection is constructor-driven (the
        legacy standalone behavior). ``session`` is an optional injected
        curl_cffi-compatible session (tests inject fakes); production leaves it
        None and the loader opens/rotates its own."""
        if self._scheduler_sport_keys:
            return await self._run_with_session(
                lambda s: self._fetch_sport(sport_key, s), session=session
            )
        return await self._run_with_session(self._fetch_odds_with_session, session=session)

    async def _run_with_session(
        self,
        runner: Callable[[AsyncGetSession | None], Awaitable[Sequence[OddsSnapshotIn]]],
        *,
        session: AsyncGetSession | None = None,
    ) -> Sequence[OddsSnapshotIn]:
        # An injected session (tests) is used verbatim. With a proxy pool we open
        # ONE persistent session per proxy and reuse it across the cycle's
        # match-page fetches (session-reuse-per-proxy); otherwise one
        # impersonating session serves the whole fetch.
        if session is not None:
            return await runner(session)
        if self._proxy_pool:
            pool = _ProxySessionPool(self._proxy_pool)
            self._session_pool = pool
            try:
                return await runner(None)
            finally:
                await pool.aclose()
                self._session_pool = None

        from curl_cffi.requests import AsyncSession

        async with AsyncSession(
            impersonate=PINNED_IMPERSONATE,
            default_headers=True,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=False,
            max_clients=self._max_clients,
        ) as session:
            return await runner(session)

    async def _fetch_sport(
        self, pipeline_key: str, session: AsyncGetSession | None
    ) -> Sequence[OddsSnapshotIn]:
        oc_key = SCHEDULER_SPORT_KEY_MAP.get(pipeline_key)
        if oc_key is None:
            return []

        self.last_fetch_complete[pipeline_key] = False
        self.last_fetch_completeness_reason[pipeline_key] = "cycle did not complete"

        discovery_lease: _ProxySessionLease | None = None
        discovery_session = session
        if discovery_session is None and self._session_pool is not None:
            discovery_lease = self._session_pool.acquire_lease()
            discovery_session = discovery_lease.session

        async def _discover(active_session: AsyncGetSession | None) -> list[str]:
            return await discover_sport_daily_match_urls(
                oc_key,
                start_date=self._football_daily_start_date,
                days=self._football_daily_days,
                session=active_session,
                proxy=None if active_session is not None else self._next_proxy(),
            )

        try:
            try:
                match_urls = await _discover(discovery_session)
            except Exception as exc:
                if not _is_transient_fetch_error(exc):
                    raise
                await _wait_before_retry(exc)
                logger.warning(
                    "oddschecker %s daily discovery retrying (%s)",
                    oc_key,
                    type(exc).__name__,
                )
                if discovery_lease is not None and self._session_pool is not None:
                    failed_lease = discovery_lease
                    await self._session_pool.evict(failed_lease)
                    await self._session_pool.release(failed_lease)
                    discovery_lease = None
                    try:
                        discovery_lease = self._session_pool.acquire_lease(
                            exclude_indices=frozenset({failed_lease.index})
                        )
                    except OddsCheckerError:
                        discovery_lease = self._session_pool.acquire_lease()
                    discovery_session = discovery_lease.session
                match_urls = await _discover(discovery_session)
        finally:
            if discovery_lease is not None and self._session_pool is not None:
                await self._session_pool.release(discovery_lease)
        try:
            deduped = self._dedupe_urls(match_urls)
        except OddsCheckerSecurityError:
            self.last_fetch_matches[pipeline_key] = len(match_urls)
            self.last_fetch_complete[pipeline_key] = False
            self.last_fetch_completeness_reason[pipeline_key] = (
                f"listed match URL ceiling exceeded ({MAX_MATCH_URLS_PER_CYCLE})"
            )
            return []
        self.last_fetch_matches[pipeline_key] = len(deduped)
        return await self._gather_snapshots(deduped, session, pipeline_key=pipeline_key)

    @staticmethod
    def _dedupe_urls(match_urls: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for url in match_urls:
            if url in seen:
                continue
            seen.add(url)
            deduped.append(url)
            if len(deduped) > MAX_MATCH_URLS_PER_CYCLE:
                raise OddsCheckerSecurityError("listed match URL ceiling exceeded")
        return deduped

    async def _gather_snapshots(
        self,
        deduped: Sequence[str],
        session: AsyncGetSession | None,
        *,
        pipeline_key: str | None = None,
    ) -> Sequence[OddsSnapshotIn]:
        semaphore = asyncio.Semaphore(self._max_clients)

        async def _one(url: str, *, capture_optional: bool) -> list[OddsSnapshotIn]:
            async with semaphore:
                lease: _ProxySessionLease | None = None
                active_session = session
                market_scope = None if capture_optional else _MAPPED_MARKET_SCOPE
                if active_session is None and self._session_pool is not None:
                    lease = self._session_pool.acquire_lease()
                    active_session = lease.session
                try:
                    try:
                        return await self.fetch_match_odds(
                            url,
                            session=active_session,
                            markets=market_scope,
                        )
                    except Exception as exc:
                        if not _is_transient_fetch_error(exc):
                            raise
                        await _wait_before_retry(exc)

                        if lease is not None and self._session_pool is not None:
                            failed_lease = lease
                            await self._session_pool.evict(failed_lease)
                            await self._session_pool.release(failed_lease)
                            lease = None
                            try:
                                retry_lease = self._session_pool.acquire_lease(
                                    exclude_indices=frozenset({failed_lease.index})
                                )
                            except OddsCheckerError:
                                # A one-proxy pool cannot rotate, but it must
                                # rebuild the retired session rather than reuse
                                # the challenged/stale connection.
                                retry_lease = self._session_pool.acquire_lease()
                            try:
                                return await self.fetch_match_odds(
                                    url,
                                    session=retry_lease.session,
                                    markets=market_scope,
                                )
                            except Exception as retry_exc:
                                if _is_transient_fetch_error(retry_exc):
                                    await self._session_pool.evict(retry_lease)
                                raise
                            finally:
                                await self._session_pool.release(retry_lease)

                        retry_session = _new_impersonated_session(self._next_proxy())
                        try:
                            return await self.fetch_match_odds(
                                url,
                                session=retry_session,
                                markets=market_scope,
                            )
                        finally:
                            try:
                                await _ProxySessionPool._close_session(retry_session)
                            except Exception as close_exc:
                                logger.warning(
                                    "oddschecker retry session close failed (%s)",
                                    type(close_exc).__name__,
                                )
                finally:
                    if lease is not None and self._session_pool is not None:
                        await self._session_pool.release(lease)

        mapped_snapshots: list[OddsSnapshotIn] = []
        optional_snapshots: list[OddsSnapshotIn] = []
        failures = 0
        snapshot_overflow = False
        optional_truncated = False
        if len(deduped) > MAX_MATCH_URLS_PER_CYCLE:
            reason = f"listed match URL ceiling exceeded ({MAX_MATCH_URLS_PER_CYCLE})"
            if pipeline_key is None:
                raise OddsCheckerSecurityError(reason)
            self.last_fetch_complete[pipeline_key] = False
            self.last_fetch_completeness_reason[pipeline_key] = reason
            self.last_fetch_event_ids[pipeline_key] = ()
            return []

        # Batch fan-out at the actual concurrency width. Unlike one giant
        # gather, this never allocates one coroutine/result slot per listed URL
        # and lets us stop scheduling new work as soon as the cycle row budget
        # is exhausted.
        batch_size = max(1, self._max_clients)
        for start in range(0, len(deduped), batch_size):
            # Once optional rows fill the retained cycle budget, later batches
            # still fetch every mapped market but skip archive-only API work.
            # This bounds parsing near the cycle ceiling without letting early
            # OTHER-heavy matches hide mapped rows listed later in the slate.
            capture_optional = not (
                self._capture_other
                and self._markets is None
                and len(mapped_snapshots) + len(optional_snapshots) >= MAX_SNAPSHOTS_PER_CYCLE
            )
            results = await asyncio.gather(
                *(
                    _one(url, capture_optional=capture_optional)
                    for url in deduped[start : start + batch_size]
                ),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    failures += 1
                    logger.warning("oddschecker match page skipped (%s)", type(result).__name__)
                    continue
                mapped_result = [
                    snapshot
                    for snapshot in result
                    if getattr(snapshot, "market", None) is not Market.OTHER
                ]
                optional_result = [
                    snapshot
                    for snapshot in result
                    if getattr(snapshot, "market", None) is Market.OTHER
                ]
                if len(mapped_result) > MAX_SNAPSHOTS_PER_MATCH:
                    failures += 1
                    logger.warning("oddschecker match page skipped (snapshot ceiling)")
                    continue
                per_match_optional_capacity = MAX_SNAPSHOTS_PER_MATCH - len(mapped_result)
                if len(optional_result) > per_match_optional_capacity:
                    optional_result = optional_result[:per_match_optional_capacity]
                    optional_truncated = True
                if len(mapped_snapshots) + len(mapped_result) > MAX_SNAPSHOTS_PER_CYCLE:
                    snapshot_overflow = True
                    break
                mapped_snapshots.extend(mapped_result)

                # Optional rows never consume space required by later mapped
                # rows. Shrink the archive buffer as the critical slate grows.
                optional_capacity = MAX_SNAPSHOTS_PER_CYCLE - len(mapped_snapshots)
                if len(optional_snapshots) > optional_capacity:
                    del optional_snapshots[optional_capacity:]
                    optional_truncated = True
                remaining_optional = optional_capacity - len(optional_snapshots)
                if len(optional_result) > remaining_optional:
                    optional_result = optional_result[:remaining_optional]
                    optional_truncated = True
                optional_snapshots.extend(optional_result)
            if snapshot_overflow:
                break
        snapshots = [*mapped_snapshots, *optional_snapshots]
        if optional_truncated:
            logger.warning("oddschecker optional market capture truncated (snapshot ceiling)")
        if pipeline_key is not None:
            complete = failures == 0 and not snapshot_overflow
            self.last_fetch_complete[pipeline_key] = complete
            if snapshot_overflow:
                reason = f"cycle snapshot ceiling exceeded ({MAX_SNAPSHOTS_PER_CYCLE})"
            elif failures:
                reason = f"{failures}/{len(deduped)} listed match fetch(es) failed"
            else:
                reason = ""
            self.last_fetch_completeness_reason[pipeline_key] = reason
            # A snapshot-ceiling breach fails closed (ratio 1.0); otherwise report
            # the failed-fetch fraction so the pipeline can tolerate a few timeouts.
            if complete:
                incomplete_ratio = 0.0
            elif snapshot_overflow or not deduped:
                incomplete_ratio = 1.0
            else:
                incomplete_ratio = failures / len(deduped)
            self.last_fetch_incomplete_ratio[pipeline_key] = incomplete_ratio
            self.last_fetch_event_ids[pipeline_key] = tuple(
                dict.fromkeys(snapshot.event_id for snapshot in snapshots)
            )
        elif snapshot_overflow:
            raise OddsCheckerSecurityError(
                f"cycle snapshot ceiling exceeded ({MAX_SNAPSHOTS_PER_CYCLE})"
            )
        return snapshots

    async def _fetch_odds_with_session(
        self, session: AsyncGetSession | None
    ) -> Sequence[OddsSnapshotIn]:
        match_urls = list(self._match_urls)
        if self._discover_football_daily:
            try:
                match_urls.extend(
                    await discover_football_daily_match_urls(
                        home_url=self._football_daily_home_url,
                        start_date=self._football_daily_start_date,
                        days=self._football_daily_days,
                        session=session,
                        proxy=None if session is not None else self._next_proxy(),
                    )
                )
            except OddsCheckerError as exc:
                logger.warning(
                    "oddschecker football daily discovery skipped (%s)",
                    type(exc).__name__,
                )
        for sport_key in self._discover_sport_daily_keys:
            if self._discover_football_daily and sport_key == "football":
                continue
            try:
                match_urls.extend(
                    await discover_sport_daily_match_urls(
                        sport_key,
                        start_date=self._football_daily_start_date,
                        days=self._football_daily_days,
                        session=session,
                        proxy=None if session is not None else self._next_proxy(),
                    )
                )
            except OddsCheckerError as exc:
                logger.warning(
                    "oddschecker %s daily discovery skipped (%s)",
                    sport_key,
                    type(exc).__name__,
                )
        for competition_url in self._competition_urls:
            try:
                match_urls.extend(
                    await self.fetch_competition_match_urls(competition_url, session=session)
                )
            except OddsCheckerError as exc:
                logger.warning(
                    "oddschecker competition page skipped (%s)",
                    type(exc).__name__,
                )
        deduped = self._dedupe_urls(match_urls)
        return await self._gather_snapshots(deduped, session)
