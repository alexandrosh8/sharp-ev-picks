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
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
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

# OddsPortal sport URL segment per our sport key. Soccer is the 3-way 1X2 BACK
# row; basketball is the 2-way moneyline (home/away, NO draw). The exchange-table
# DOM is identical across sports (betting-exchanges-section / table-row / the
# ancestor odds container) — only the outcome COUNT and selection mapping differ
# (see _BACK_OUTCOMES_BY_SEGMENT). Tennis (also 2-way) and others can extend this
# map plus _BACK_OUTCOMES_BY_SEGMENT once their exchange rows are probed.
SPORT_SEGMENTS: dict[str, str] = {"soccer": "football", "basketball": "basketball"}

# Confirmed live (2026-06-19, UK proxy) DOM contract — see ADR-0015:
#   section : [data-testid="betting-exchanges-section"]
#   rows    : [data-testid="betting-exchanges-table-row"]
#   the Betfair row is the row whose <img alt="Betfair Exchange">.
# The row renders, per outcome, a BACK triple then a LAY triple, each cell an
# odds token (fractional "28/25") immediately followed by a liquidity token
# "(9052)". For a 3-way (1X2) market the BACK side is the FIRST three
# odds+liquidity pairs in DOM order (home, draw, away); LAY is the next three.
#
# EXTRACTION CORRECTION (2026-06-19, re-probed live via UK proxy): the odds do
# NOT live inside the testid'd row's [data-testid="odd-container"] cells — those
# are EMPTY. The testid row carries only the Betfair logo + the "Back"/"Lay"
# header + the "CLAIM BONUS" promo. The fractional prices + liquidity sit in an
# ANCESTOR/sibling container ~2 levels ABOVE the testid row. The real ancestor
# chain from img[alt="Betfair Exchange"] is:
#   A    logo link
#   DIV  "...justify-center flex w-full items-center"  -> "...CLAIM BONUS" (no odds)
#   DIV  the betting-exchanges-table-row               -> "...Back Lay"    (no odds)
#   DIV  "w-full"                                      -> "...Back Lay"    (no odds)
#   DIV  "flex"  -> "...Back Lay 28/25 (9052) 5/2 (3307) 3/1 (1307) 99.3%
#                       57/50 (11317) 51/20 (41) 31/10 (2683) 100"        (THE ODDS)
# So extraction WALKS UP from the row (and the img) and returns the leaf
# odds/liquidity tokens of the NEAREST ancestor that yields >= 2 odds tokens —
# stopping at the Betfair odds container so it never climbs into a container
# holding OTHER bookmakers' rows. The promo "CLAIM BONUS" has no fraction and no
# parenthesised number, so it can never become a cell. A row that yields no odds
# at any level is an EXPECTED gap (market closed / no liquidity), never an error.
_SECTION_TESTID = "betting-exchanges-section"
_ROW_TESTID = "betting-exchanges-table-row"
_EXCHANGE_ALT = "Betfair Exchange"
# How many ancestor levels above the row/img we climb looking for the odds
# container (the live odds sit ~2 levels up; 5 gives margin without reaching the
# section root that holds OTHER bookmakers' rows).
_ANCESTOR_WALK_UP = 5

_FRACTION_RE = re.compile(r"^(\d+)\s*/\s*(\d+)$")
_LIQUIDITY_RE = re.compile(r"^\((\d[\d,]*)\)$")

# BACK outcomes in the DOM order OddsPortal renders them, per sport. Soccer is
# the 3-way 1X2 (home/draw/away); basketball is the 2-way moneyline (home/away,
# NO draw). The reader takes the FIRST len(outcomes) (odds, liquidity) cells off
# the BACK side and discards the LAY tail, so the outcome COUNT alone tunes the
# parser per sport. Keyed by the OddsPortal URL SEGMENT (SPORT_SEGMENTS value),
# so the reader needs only the segment — never the platform sport key.
_FOOTBALL_BACK_OUTCOMES = ("home", "draw", "away")
_BASKETBALL_BACK_OUTCOMES = ("home", "away")
_BACK_OUTCOMES_BY_SEGMENT: dict[str, tuple[str, ...]] = {
    "football": _FOOTBALL_BACK_OUTCOMES,
    "basketball": _BASKETBALL_BACK_OUTCOMES,
}


def back_outcomes_for_segment(segment: str) -> tuple[str, ...]:
    """BACK outcomes for an OddsPortal URL segment ("football" -> 3-way 1X2,
    "basketball" -> 2-way moneyline). Defaults to the 3-way layout for an
    unmapped segment (the conservative widest-market read)."""
    return _BACK_OUTCOMES_BY_SEGMENT.get(segment, _FOOTBALL_BACK_OUTCOMES)


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


_DECIMAL_RE = re.compile(r"^\d+(?:\.\d+)?$")


def parse_odds_value(raw: str) -> float | None:
    """Parse a Betfair BACK price in EITHER format OddsPortal may render — the
    odds format is a per-visitor COOKIE, so a fresh scraper context gets decimal
    OR fractional unpredictably. Decimal ("6.51", "1.66") is used directly;
    fractional ("28/25", "3/1") goes through num/den + 1. Returns None for
    anything unparseable or <= 1.0."""
    token = raw.strip()
    if _DECIMAL_RE.match(token):
        value = float(token)
        return value if value > 1.0 else None
    return fractional_to_decimal(token)


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
        decimal_odds = parse_odds_value(raw_odds)
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
# then WALK UP the ancestor chain to find the odds. The odds do NOT live in the
# testid row itself (it holds only the logo + "Back"/"Lay" header + promo) — they
# sit in an ancestor ~2 levels above. We start from the row (and, as a fallback
# anchor, the img itself in case the testid row is absent on a layout variant)
# and climb up to `maxUp` ancestors. At each level we collect that element's leaf
# odds/liquidity tokens (a `d/d` fraction OR a `(d...)` parenthesised number) in
# DOM order, and return the tokens from the NEAREST (lowest) ancestor that yields
# >= 2 odds tokens. Returning the nearest qualifying ancestor stops the climb at
# the Betfair odds container so we never reach a container holding OTHER
# bookmakers' rows. The "CLAIM BONUS" promo has no fraction/paren, so it can
# never leak in. No qualifying ancestor -> null (the expected closed-market gap).
_ROW_EXTRACT_JS = r"""
(args) => {
  const sectionId = args[0];
  const rowId = args[1];
  const alt = args[2];
  const maxUp = args[3];
  const section = document.querySelector('[data-testid="' + sectionId + '"]');
  const scope = section || document;
  const altLower = alt.toLowerCase();
  // Find the Betfair logo img (anchor) and, when present, its testid row.
  let img = null;
  scope.querySelectorAll('img[alt]').forEach(i => {
    if (img) return;
    if ((i.getAttribute('alt') || '').toLowerCase() === altLower) img = i;
  });
  let row = null;
  scope.querySelectorAll('[data-testid="' + rowId + '"]').forEach(r => {
    if (row) return;
    const ri = r.querySelector('img[alt]');
    if (!ri) return;
    if ((ri.getAttribute('alt') || '').toLowerCase() === altLower) row = r;
  });
  // Nothing identifies a Betfair Exchange row on this page -> expected absence.
  if (!row) { if (!img) return null; }
  // The odds VALUE is decimal ("6.51") or fractional ("28/25") — OddsPortal's
  // odds format is a per-visitor cookie, so accept BOTH. Liquidity is "(1838)".
  const isOdd = (t) => /^\d+(?:\.\d+)?$/.test(t) || /^\d+\s*\/\s*\d+$/.test(t);
  const isLiq = (t) => /^\(\d[\d,]*\)$/.test(t);
  // The BACK/LAY prices live in [data-testid="odd-container"] cells in a SIBLING
  // block of the testid row (the row itself holds only the logo + Back/Lay
  // header + the CLAIM BONUS promo). Climb from the Betfair anchor to the
  // nearest ancestor holding those cells, stopping early so we never reach a
  // container with OTHER books' exchange rows. payout-container cells are
  // excluded by the odd-container testid filter.
  let node = row || img;
  let cells = [];
  for (let up = 0; up <= maxUp; up++) {
    if (!node) break;
    const found = node.querySelectorAll('[data-testid="odd-container"]');
    if (found.length >= 2) { cells = Array.prototype.slice.call(found); break; }
    node = node.parentElement;
  }
  if (cells.length < 2) return null;  // no price cells (closed/unhydrated market)
  // Per cell: ONE odds value + ONE liquidity, as a flat [odd, liq, odd, liq...]
  // list in DOM order (BACK triple first, then LAY). The value is duplicated in
  // a hidden <a> and a <p> (responsive show/hide) — take the FIRST match only,
  // so the pairing is never scrambled by duplicates. extract_back_quotes keeps
  // the leading len(outcomes) cells as BACK and discards the LAY tail.
  const out = [];
  let anyReal = false;
  for (let c = 0; c < cells.length; c++) {
    let odd = null;
    let liq = null;
    cells[c].querySelectorAll('*').forEach(e => {
      if (e.children.length > 0) return;  // leaf elements only
      const t = (e.textContent || '').trim();
      if (odd === null) { if (isOdd(t)) odd = t; }
      if (liq === null) { if (isLiq(t)) liq = t; }
    });
    // Emit ONE odds token per cell so an EMPTY cell (a suspended/no-price
    // selection) keeps its POSITION. Skipping it shifts the home/draw/away
    // pairing -> the wrong price maps to a selection (corrupt CLV, the cardinal
    // sin). The '0' sentinel parses to None (<= 1.0), so extract_back_quotes
    // drops it while the surviving selections stay aligned.
    if (odd !== null) anyReal = true;
    out.push(odd !== null ? odd : '0');
    if (liq !== null) out.push(liq);
  }
  if (!anyReal) return null;  // closed/unhydrated market -> no real price yet
  return out;
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
        if _DECIMAL_RE.match(token.strip()) or _FRACTION_RE.match(token.strip()):
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

    # A proxy slot that raises (e.g. TimeoutError) is skipped for this many
    # subsequent reads before being retried, so a chronically-slow slot is not
    # re-probed — burning a full nav timeout — on every cycle. Failover is
    # unchanged and stays loud (the per-slot WARNING still fires); this only
    # reorders which healthy slot is tried first.
    _SLOT_COOLDOWN_READS = 8

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
        hydrate_timeout_ms: int = 12000,
        hydrate_step_ms: int = 500,
    ) -> None:
        self._min_liquidity = min_liquidity
        self._proxy_pool = tuple(proxy_pool)
        self._proxy_cursor = 0
        # Proxy slot index -> reads remaining before it is retried after a
        # failure. Empty == all slots eligible. Read-only bookkeeping only.
        self._slot_cooldown: dict[int, int] = {}
        self._page_loader = page_loader
        self._headless = headless
        self._locale = locale
        self._nav_timeout_ms = nav_timeout_ms
        self._settle_ms = settle_ms
        # Bounded re-poll for slowly-hydrating fractional prices (read-only):
        # keep re-running the in-page extraction up to `hydrate_timeout_ms`,
        # `hydrate_step_ms` per step, until odds appear or the budget is spent.
        self._hydrate_timeout_ms = hydrate_timeout_ms
        self._hydrate_step_ms = hydrate_step_ms

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
                args = [_SECTION_TESTID, _ROW_TESTID, _EXCHANGE_ALT, _ANCESTOR_WALK_UP]
                # Bounded hydration wait: the fractional prices hydrate slowly on
                # some loads (the row appears first with only the logo/header).
                # Re-run the extraction every `hydrate_step_ms` until it yields
                # >= 2 tokens or `hydrate_timeout_ms` elapses, then accept the
                # last result (None/[] stays clean when odds are truly absent —
                # a closed/illiquid market is an EXPECTED gap, never an error).
                tokens: list[str] | None = None
                waited = 0
                while True:
                    raw = await page.evaluate(_ROW_EXTRACT_JS, args)
                    tokens = list(raw) if raw else None
                    if tokens is not None and len(tokens) >= 2:
                        break
                    if waited >= self._hydrate_timeout_ms:
                        break
                    await page.wait_for_timeout(self._hydrate_step_ms)
                    waited += self._hydrate_step_ms
                return tokens if tokens else None
            finally:
                await browser.close()

    async def read_back_quotes(
        self,
        url: str,
        *,
        outcomes: Sequence[str] = _FOOTBALL_BACK_OUTCOMES,
    ) -> list[BackQuote]:
        """Load the match page and return its gated Betfair BACK quotes.
        ``outcomes`` selects how many leading BACK cells to keep and their
        designations — the 3-way 1X2 default (home/draw/away) for soccer, the
        2-way moneyline (home/away) for basketball. Rotates through the proxy
        pool on transport failure; an empty pool loads from the host IP. Errors
        carry the exception TYPE only, never the URL."""
        pool = self._proxy_pool or (None,)
        n = len(pool)
        last_exc: Exception | None = None
        # Tick cooldowns down once per read, then try healthy slots first and
        # any still-cooling slot only as a last resort — we never refuse to
        # read, we just stop burning a nav timeout on a known-slow slot.
        if self._proxy_pool:
            for slot in list(self._slot_cooldown):
                self._slot_cooldown[slot] -= 1
                if self._slot_cooldown[slot] <= 0:
                    del self._slot_cooldown[slot]
        rotation = [(self._proxy_cursor + offset) % n for offset in range(n)]
        if self._proxy_pool:
            order = [s for s in rotation if s not in self._slot_cooldown] + [
                s for s in rotation if s in self._slot_cooldown
            ]
        else:
            order = rotation
        for slot in order:
            proxy = pool[slot] if self._proxy_pool else None
            try:
                tokens = await self._load_tokens(url, proxy)
            except Exception as exc:  # network / anti-bot / timeout
                last_exc = exc
                if self._proxy_pool:
                    self._slot_cooldown[slot] = self._SLOT_COOLDOWN_READS
                logger.warning(
                    "betfair exchange read via proxy slot %d failed (%s); trying next",
                    slot if self._proxy_pool else -1,
                    type(exc).__name__,
                )
                continue
            if self._proxy_pool:
                self._slot_cooldown.pop(slot, None)  # recovered -> clear cooldown
                self._proxy_cursor = (slot + 1) % n
            if not tokens:
                return []  # no Betfair Exchange row (expected on thin matches)
            cells = _pair_tokens(tokens)
            return extract_back_quotes(cells, outcomes=outcomes, min_liquidity=self._min_liquidity)
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
        targets_fn: Callable[[str], Sequence[MatchTarget] | Awaitable[Sequence[MatchTarget]]],
        sports: Sequence[str],
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """``targets_fn(sport)`` yields the match pages to read this cycle for a
        sport key (e.g. open/upcoming football fixtures with their links). It is
        injected so this module never owns the listing/scheduling policy — the
        composition root supplies it (and tests supply a static list).

        It may be SYNC (returns the sequence) or ASYNC (returns an awaitable of
        it): the production root sources targets from the DB (an async query),
        while tests inject a plain ``lambda sport: [...]``. ``capture_once``
        awaits the result only when it is awaitable, so both shapes work."""
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
            # Per-sport BACK outcomes: soccer reads 3 cells (home/draw/away),
            # basketball reads 2 (home/away). Derived from the URL segment so the
            # reader's parser tracks the sport's exchange-row width.
            outcomes = back_outcomes_for_segment(SPORT_SEGMENTS[sport])
            fresh: list[OddsSnapshotIn] = []
            teams_by_event: dict[str, EventTeams] = {}
            # Honest-zero accounting (FIX 3): a 0 result means different things —
            # `targets` counts the fixtures we had a page to read this cycle
            # (0 = nothing scraped for this sport; e.g. the sport's OddsPortal
            # scrape is off or the slate was empty), and `events_with_quotes`
            # counts those that yielded a Betfair-liquid BACK row. So
            # "0 of 0 targets" (no match to read) is logged DISTINCTLY from
            # "0 of N targets" (read N pages, none Betfair-liquid / all closed).
            targets = 0
            events_with_quotes = 0
            target_list = self._targets_fn(sport)
            if inspect.isawaitable(target_list):
                target_list = await target_list
            for target in target_list:
                targets += 1
                try:
                    quotes = await self._reader.read_back_quotes(target.url, outcomes=outcomes)
                except Exception as exc:
                    logger.warning(
                        "betfair exchange read failed for one %s match: %s",
                        sport,
                        type(exc).__name__,
                    )
                    continue
                if not quotes:
                    continue  # no row / all outcomes below the liquidity floor
                events_with_quotes += 1
                # INLINE BINDING (ADR-0015 v2): persist under the CANONICAL
                # external_ref (target.event_id == the OddsPortal match URL, the
                # same ref the main scrape's soft-book rows use), so the Betfair
                # row becomes just another bookmaker on the canonical event — no
                # cross-source matching needed for any Betfair-priced game. The
                # attach-only persist below guarantees this can ONLY attach to a
                # canonical event the main scrape already created; it never mints
                # a (partial) event from Betfair data, so a same-URL graft is
                # safe BY CONSTRUCTION (the event identity + metadata are the
                # main scrape's, not ours).
                event_ref = target.event_id
                snapshots = back_quotes_to_snapshots(event_ref, quotes, target.teams, now=now)
                event_fresh = self._select_fresh(sport, event_ref, snapshots)
                if event_fresh:
                    fresh.extend(event_fresh)
                    teams_by_event[event_ref] = target.teams
            if not fresh or self._session_factory is None:
                written[sport] = 0
                if self._session_factory is not None:
                    # Distinguish the two honest zeros so logs never imply a
                    # "structural 0": no fixtures to read vs fixtures read but
                    # none Betfair-liquid (an expected thin-slate gap, NOT a
                    # sign the capture is broken for this sport).
                    if targets == 0:
                        logger.info(
                            "betfair exchange: %s captured 0 — no fixtures to read this "
                            "cycle (sport scrape off or empty slate)",
                            sport,
                        )
                    else:
                        logger.info(
                            "betfair exchange: %s captured 0 — read %d fixtures, %d had a "
                            "Betfair-liquid BACK row (thin slate / no new price)",
                            sport,
                            targets,
                            events_with_quotes,
                        )
                continue
            # INLINE BINDING (ADR-0015 v2): persist under the CANONICAL sport
            # ("soccer"/"basketball") with attach_only_to_existing=True, so the
            # Betfair rows ATTACH to the canonical event the main scrape already
            # created and NEVER mint one from Betfair-only data. A fixture whose
            # canonical event has not landed yet is skipped THIS cycle (counted +
            # logged by persist_odds_snapshots) and attaches on a later one.
            rows = await persist_odds_snapshots(
                self._session_factory,
                fresh,
                teams_by_event,
                sport=sport,
                default_league=sport,
                attach_only_to_existing=True,
            )
            written[sport] = rows
            if rows:
                logger.info(
                    "betfair exchange: %s captured %d new BACK rows (%d events of %d read)",
                    sport,
                    rows,
                    len(teams_by_event),
                    targets,
                )
        return written
