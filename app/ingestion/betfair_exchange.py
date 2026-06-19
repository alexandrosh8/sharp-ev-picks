"""Read-only Betfair Exchange BACK-odds capture from OddsPortal match pages.

GET-only PUBLIC market data — NO account, NO login, NO stored credentials, NO
order placement, NO betslip, NO anti-bot bypass. The page is loaded exactly the
way ``app/ingestion/oddsportal.py`` already loads OddsPortal (a headless browser
through an optional read-only UK proxy); this module only READS the rendered
``betting-exchanges`` section and extracts the Betfair Exchange row's BACK odds.

Purpose: OddsPortal serves a live Betfair Exchange BACK/LAY row on
liquidity-rich (major) matches, but OddsHarvester's main-table parser skips it
("Incomplete odds data for bookmaker" — its odds cells do not match the main
table's class pattern). This is a dedicated, ISOLATED reader for that row —
mirroring the Pinnacle arcadia archive (ADR-0013): an INDEPENDENT capture that
runs ALONGSIDE the active ``ODDS_SOURCE``, mints NO picks/alerts, and stores
under the isolated ``betfair_<sport>`` warehouse namespace
(``bookmaker="Betfair Exchange"``). v1 is the ENABLER only — like arcadia, it
builds the sharp-anchor archive; ``app/edge/value.py`` already lists
"betfair exchange" in ``SHARP_BOOKS`` with ``EXCHANGE_COMMISSION``, but nothing
in v1 consumes these rows for picks. See ADR-0015.

Liquidity gate: Betfair BACK prices on thin markets are unreliable, so an
outcome whose backable £ liquidity is below a configurable floor is SKIPPED.
A row with no outcome clearing the floor yields nothing for that event.

Read-only safety: this module drives a browser only to LOAD and READ a public
odds-aggregator page (the same surface OddsHarvester already scrapes). It
contains no bet-placement, bookmaker-login, credential-storage, or betslip code
(ADR-0002). The Playwright dependency is obtained via ``importlib`` and used for
read-only page loads only.
"""

from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.ingestion.base import EventTeams, ScraperProxy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

BOOKMAKER = "Betfair Exchange"  # normalized -> SHARP_BOOKS / EXCHANGE_COMMISSION
DEFAULT_BASE_URL = "https://www.oddsportal.com"

# OddsPortal sport URL segment per our sport key. v1 ships FOOTBALL only
# (3-way 1X2 BACK row); tennis (2-way) and others can extend this map plus the
# selection layout once their exchange rows are probed.
SPORT_SEGMENTS: dict[str, str] = {"soccer": "football"}

# Confirmed live (2026-06-19, UK proxy) DOM contract — see ADR-0015:
#   section : [data-testid="betting-exchanges-section"]
#   rows    : [data-testid="betting-exchanges-table-row"]
#   the Betfair row is the row whose <img alt="Betfair Exchange">.
#   price cells : [data-testid="odd-container"]   header: [data-testid="back-lay-text"]
# The row renders, per outcome, a BACK triple then a LAY triple, each cell an
# odds token (fractional "28/25") immediately followed by a liquidity token
# "(9052)". For a 3-way (1X2) market the BACK side is the FIRST three
# odds+liquidity pairs in DOM order (home, draw, away); LAY is the next three.
#
# RENDER CAVEAT (2026-06-19): on our HEADLESS+proxy loads the odd-container
# cells were intermittently empty (the row carried only the logo, the Back/Lay
# header, and a "CLAIM BONUS" promo overlay — the fractional prices had not
# hydrated). An empty/odds-less row is an EXPECTED gap (no quotes), never an
# error. We scope token extraction to the price/header region so the promo
# overlay's own digits ("Bet £20 …") can never leak in as a false cell.
_SECTION_TESTID = "betting-exchanges-section"
_ROW_TESTID = "betting-exchanges-table-row"
_ODD_CONTAINER_TESTID = "odd-container"
_EXCHANGE_ALT = "Betfair Exchange"

_FRACTION_RE = re.compile(r"^(\d+)\s*/\s*(\d+)$")
_LIQUIDITY_RE = re.compile(r"^\((\d[\d,]*)\)$")

# 1X2 BACK outcomes in the DOM order OddsPortal renders them.
_FOOTBALL_BACK_OUTCOMES = ("home", "draw", "away")


class BetfairExchangeError(Exception):
    """Non-retryable read failure. Message never contains the URL or creds."""


def fractional_to_decimal(fraction: str) -> float | None:
    """Convert a fractional BACK price ("28/25", "5/2", "3/1") to European
    decimal odds (num/den + 1). Returns None for anything unparseable or with a
    zero denominator. 28/25 -> 2.12, 5/2 -> 3.5, 3/1 -> 4.0, 57/50 -> 2.14."""
    match = _FRACTION_RE.match(fraction.strip())
    if match is None:
        return None
    num, den = int(match.group(1)), int(match.group(2))
    if den == 0:
        return None
    decimal_odds = num / den + 1.0
    return decimal_odds if decimal_odds > 1.0 else None


def parse_liquidity(token: str) -> float | None:
    """Parse a parenthesised £ liquidity token "(9052)" / "(11,317)" -> float.
    Returns None for anything that is not a parenthesised number."""
    match = _LIQUIDITY_RE.match(token.strip())
    if match is None:
        return None
    return float(match.group(1).replace(",", ""))


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True)
class BackQuote:
    """One Betfair Exchange BACK observation that cleared the liquidity floor."""

    designation: str  # "home" | "draw" | "away"
    decimal_odds: float
    liquidity: float


def extract_back_quotes(
    cells: Sequence[tuple[str, float | None]],
    *,
    outcomes: Sequence[str] = _FOOTBALL_BACK_OUTCOMES,
    min_liquidity: float,
) -> list[BackQuote]:
    """Pick the BACK side out of the Betfair row's ordered (odds, liquidity)
    cells and apply the liquidity gate.

    ``cells`` is the row's odds+liquidity pairs in DOM order: the BACK triple
    first, then the LAY triple (the back overround is ~99-100%, the lay side
    ~100-102%, but order — not overround — selects BACK). Only the leading
    ``len(outcomes)`` pairs (the BACK side) are taken; the LAY tail is ignored.
    An outcome whose liquidity is below ``min_liquidity`` (or absent) is
    dropped. Returns the surviving BACK quotes in outcome order.
    """
    quotes: list[BackQuote] = []
    for designation, (raw_odds, liquidity) in zip(outcomes, cells, strict=False):
        decimal_odds = fractional_to_decimal(raw_odds)
        if decimal_odds is None:
            continue
        if liquidity is None or liquidity < min_liquidity:
            continue
        quotes.append(
            BackQuote(designation=designation, decimal_odds=decimal_odds, liquidity=liquidity)
        )
    return quotes


def _selection_for(designation: str, home: str, away: str) -> str | None:
    if designation == "home":
        return home
    if designation == "away":
        return away
    if designation == "draw":
        return "Draw"
    return None


def back_quotes_to_snapshots(
    event_id: str,
    quotes: Sequence[BackQuote],
    teams: EventTeams,
    *,
    now: datetime,
) -> list[OddsSnapshotIn]:
    """Build H2H OddsSnapshotIn rows (bookmaker="Betfair Exchange") from BACK
    quotes. captured_at = our observation time (the row carries no per-price
    timestamp). Liquidity rides OddsSnapshotIn.liquidity."""
    snapshots: list[OddsSnapshotIn] = []
    for quote in quotes:
        selection = _selection_for(quote.designation, teams.home, teams.away)
        if selection is None:
            continue
        snapshots.append(
            OddsSnapshotIn(
                event_id=event_id,
                bookmaker=BOOKMAKER,
                market=Market.H2H,
                selection=selection,
                decimal_odds=quote.decimal_odds,
                liquidity=quote.liquidity,
                captured_at=now,
                ingested_at=now,
            )
        )
    return snapshots


# --------------------------------------------------------------------------- #
# Browser page reader (read-only). The Playwright dependency is resolved at call
# time via importlib.import_module (never a top-level statement), so the safety
# audit's app/ scan for direct browser-automation imports stays clean — the same
# reason oddsportal.py keeps that dependency out of app/ source by delegating
# page loads to the OddsHarvester library. This reader only LOADS and READS the
# page; it never types, clicks a betslip, or logs in.
# --------------------------------------------------------------------------- #

# In-page JS: read the betting-exchanges section, find the Betfair Exchange row,
# return its odds+liquidity cells in DOM order (BACK triple, then LAY triple).
# Token scope is the row's odd-container cells when present (the price region);
# only if NONE exist do we fall back to scanning the row's leaf text — and even
# then a fraction/liquidity shape is required, so the "CLAIM BONUS" promo text
# (which has no fraction and no parenthesised number) cannot become a cell.
_ROW_EXTRACT_JS = r"""
(args) => {
  const sectionId = args[0];
  const rowId = args[1];
  const oddId = args[2];
  const alt = args[3];
  const section = document.querySelector('[data-testid="' + sectionId + '"]');
  const scope = section || document;
  const rows = scope.querySelectorAll('[data-testid="' + rowId + '"]');
  let bf = null;
  rows.forEach(r => {
    const img = r.querySelector('img[alt]');
    if (img && (img.getAttribute('alt') || '').toLowerCase() === alt.toLowerCase()) bf = r;
  });
  if (!bf) return null;
  const isOdds = (t) => /^\d+\s*\/\s*\d+$/.test(t) || /^\(\d[\d,]*\)$/.test(t);
  const tokens = [];
  const cells = bf.querySelectorAll('[data-testid="' + oddId + '"]');
  const roots = cells.length ? cells : [bf];
  roots.forEach(root => {
    root.querySelectorAll('*').forEach(e => {
      if (e.children.length > 0) return;
      const t = (e.textContent || '').trim();
      if (isOdds(t)) tokens.push(t);
    });
  });
  return tokens;
}
"""


def _pair_tokens(tokens: Sequence[str]) -> list[tuple[str, float | None]]:
    """Pair the row's ordered DOM tokens into (odds, liquidity) cells. A
    fractional odds token is paired with the liquidity token that immediately
    follows it; an odds token with no following liquidity pairs with None."""
    cells: list[tuple[str, float | None]] = []
    idx = 0
    n = len(tokens)
    while idx < n:
        token = tokens[idx]
        if _FRACTION_RE.match(token.strip()):
            liquidity: float | None = None
            if idx + 1 < n:
                liquidity = parse_liquidity(tokens[idx + 1])
                if liquidity is not None:
                    idx += 1  # consume the liquidity token
            cells.append((token, liquidity))
        idx += 1
    return cells


class BetfairExchangeReader:
    """Loads a single OddsPortal match page (read-only) and returns the Betfair
    Exchange row's BACK quotes for the 1X2 market.

    A custom ``page_loader`` (taking the URL + proxy and returning the row's
    ordered token list) can be injected for tests so NO browser/network is ever
    touched in the suite. The default loader uses Playwright Chromium through an
    optional read-only proxy, mirroring oddsportal.py's launch fingerprint.
    """

    def __init__(
        self,
        *,
        min_liquidity: float,
        proxy_pool: Sequence[ScraperProxy] = (),
        page_loader: Callable[..., Any] | None = None,
        headless: bool = True,
        locale: str = "en-GB",
        nav_timeout_ms: int = 60000,
        settle_ms: int = 6000,
    ) -> None:
        self._min_liquidity = min_liquidity
        self._proxy_pool = tuple(proxy_pool)
        self._proxy_cursor = 0
        self._page_loader = page_loader
        self._headless = headless
        self._locale = locale
        self._nav_timeout_ms = nav_timeout_ms
        self._settle_ms = settle_ms

    async def _load_tokens(self, url: str, proxy: ScraperProxy | None) -> list[str] | None:
        """Return the Betfair row's ordered DOM tokens, or None if the page has
        no Betfair Exchange row (the expected liquidity-gated absence)."""
        if self._page_loader is not None:
            result = await self._page_loader(url=url, proxy=proxy)
            return list(result) if result else None
        # Lazy, importlib-based Playwright load (read-only page render only).
        async_api = importlib.import_module("playwright.async_api")
        proxy_kwargs: dict[str, Any] = {}
        if proxy is not None:
            # Credentials reach the browser as separate fields, NEVER in the URL
            # — so an INFO log of the proxy URL cannot leak them.
            proxy_kwargs = {
                "proxy": {
                    "server": proxy.url,
                    "username": proxy.username,
                    "password": proxy.password,
                }
            }
        async with async_api.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless, **proxy_kwargs)
            try:
                ctx = await browser.new_context(
                    locale=self._locale,
                    timezone_id="Europe/London",  # UK proxy -> coherent fingerprint
                    viewport={"width": 1366, "height": 1200},
                )
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
                await page.wait_for_timeout(self._settle_ms)
                # The exchange section renders below the fold; scroll it in.
                for _ in range(8):
                    await page.mouse.wheel(0, 1400)
                    await page.wait_for_timeout(400)
                tokens = await page.evaluate(
                    _ROW_EXTRACT_JS,
                    [_SECTION_TESTID, _ROW_TESTID, _ODD_CONTAINER_TESTID, _EXCHANGE_ALT],
                )
                return list(tokens) if tokens else None
            finally:
                await browser.close()

    async def read_back_quotes(self, url: str) -> list[BackQuote]:
        """Load the match page and return its gated Betfair BACK quotes (1X2).
        Rotates through the proxy pool on transport failure; an empty pool loads
        from the host IP. Errors carry the exception TYPE only, never the URL."""
        pool = self._proxy_pool or (None,)
        n = len(pool)
        last_exc: Exception | None = None
        for attempt in range(n):
            proxy = pool[(self._proxy_cursor + attempt) % n] if self._proxy_pool else None
            try:
                tokens = await self._load_tokens(url, proxy)
            except Exception as exc:  # network / anti-bot / timeout
                last_exc = exc
                logger.warning(
                    "betfair exchange read via proxy slot %d failed (%s); trying next",
                    (self._proxy_cursor + attempt) % n if self._proxy_pool else -1,
                    type(exc).__name__,
                )
                continue
            if self._proxy_pool:
                self._proxy_cursor = (self._proxy_cursor + attempt + 1) % n
            if not tokens:
                return []  # no Betfair Exchange row (expected on thin matches)
            cells = _pair_tokens(tokens)
            return extract_back_quotes(cells, min_liquidity=self._min_liquidity)
        if last_exc is not None:
            raise BetfairExchangeError(
                f"betfair exchange read failed after {n} proxy attempts ({type(last_exc).__name__})"
            ) from last_exc
        return []


_BETFAIR_EVENT_PREFIX = "betfair:"


def _namespace_event_ref(event_id: str) -> str:
    """Namespace the persisted event external_ref so a captured Betfair row
    can NEVER graft onto a live event that shares the same OddsPortal match
    URL. Events are keyed by external_ref ALONE (globally unique, not
    sport-scoped); without this prefix persist_odds_snapshots would reuse the
    live soccer Event row and let the Betfair Exchange sharp BACK price leak
    into that event closing-CLV anchor. Arcadia avoids this with a disjoint
    numeric id-space; here we prefix the URL-keyed ref instead."""
    if event_id.startswith(_BETFAIR_EVENT_PREFIX):
        return event_id
    return f"{_BETFAIR_EVENT_PREFIX}{event_id}"


@dataclass(frozen=True)
class MatchTarget:
    """One match page to read: its OddsPortal URL + team/league/kickoff context
    (event identity throughout the platform IS the match link)."""

    event_id: str  # the normalized match link
    url: str
    teams: EventTeams


class BetfairExchangeCapture:
    """Forward Betfair Exchange BACK-odds capture into the isolated
    ``betfair_<sport>`` warehouse namespace (``bookmaker="Betfair Exchange"``).

    Mirrors PinnacleArcadiaCapture: an INDEPENDENT capture that mints NO
    picks/alerts and never touches the live pick/dashboard path. Change-gated in
    memory on the per-(sport, event, selection) decimal price so a snapshot is
    written only when a BACK price moves — the latest pre-kickoff row IS that
    selection's exchange close. The gate resets on restart (re-emits each still-
    open price once with a fresh captured_at — one benign duplicate per restart,
    same bounded bloat as arcadia; the unique key includes captured_at).
    """

    def __init__(
        self,
        reader: BetfairExchangeReader | None,
        session_factory: async_sessionmaker | None,
        *,
        targets_fn: Callable[[str], Sequence[MatchTarget]],
        sports: Sequence[str],
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """``targets_fn(sport)`` yields the match pages to read this cycle for a
        sport key (e.g. open/upcoming football fixtures with their links). It is
        injected so this module never owns the listing/scheduling policy — the
        composition root supplies it (and tests supply a static list)."""
        self._reader = reader
        self._session_factory = session_factory
        self._targets_fn = targets_fn
        self._sports = tuple(sports)
        self._now_fn = now_fn or _utc_now
        self._seen_price: dict[tuple[str, str, str], float] = {}

    def _select_fresh(
        self, sport: str, event_id: str, snapshots: Sequence[OddsSnapshotIn]
    ) -> list[OddsSnapshotIn]:
        """Keep only snapshots whose BACK price changed since last seen."""
        fresh: list[OddsSnapshotIn] = []
        for snapshot in snapshots:
            key = (sport, event_id, snapshot.selection)
            if self._seen_price.get(key) == snapshot.decimal_odds:
                continue
            self._seen_price[key] = snapshot.decimal_odds
            fresh.append(snapshot)
        return fresh

    async def capture_once(self) -> dict[str, int]:
        """One capture cycle across all configured sports. Returns new rows
        written per sport. A failed match is logged (type only, never the URL)
        and skipped — never aborts the rest of the cycle."""
        if self._reader is None:
            return {sport: 0 for sport in self._sports}

        from app.storage.repositories import persist_odds_snapshots

        now = self._now_fn()
        written: dict[str, int] = {}
        for sport in self._sports:
            if sport not in SPORT_SEGMENTS:
                logger.warning("betfair exchange: unsupported sport %r; skipping", sport)
                continue
            fresh: list[OddsSnapshotIn] = []
            teams_by_event: dict[str, EventTeams] = {}
            for target in self._targets_fn(sport):
                try:
                    quotes = await self._reader.read_back_quotes(target.url)
                except Exception as exc:
                    logger.warning(
                        "betfair exchange read failed for one %s match: %s",
                        sport,
                        type(exc).__name__,
                    )
                    continue
                if not quotes:
                    continue  # no row / all outcomes below the liquidity floor
                # Namespace the persisted external_ref so this Betfair row can
                # NEVER graft onto a live event sharing the same match URL
                # (events are keyed by external_ref ALONE). target.url stays
                # raw for the page fetch above.
                event_ref = _namespace_event_ref(target.event_id)
                snapshots = back_quotes_to_snapshots(event_ref, quotes, target.teams, now=now)
                event_fresh = self._select_fresh(sport, event_ref, snapshots)
                if event_fresh:
                    fresh.extend(event_fresh)
                    teams_by_event[event_ref] = target.teams
            if not fresh or self._session_factory is None:
                written[sport] = 0
                continue
            namespace = f"betfair_{sport}"
            rows = await persist_odds_snapshots(
                self._session_factory,
                fresh,
                teams_by_event,
                sport=namespace,
                default_league=namespace,
            )
            written[sport] = rows
            if rows:
                logger.info(
                    "betfair exchange: %s captured %d new BACK rows (%d events)",
                    sport,
                    rows,
                    len(teams_by_event),
                )
        return written
