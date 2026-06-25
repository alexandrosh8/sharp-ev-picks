"""Free OddsPortal odds via OddsHarvester, used directly (ADR-0012).

OddsHarvester (MIT, jordantete/OddsHarvester) scrapes oddsportal.com — an
odds AGGREGATOR, not a bookmaker: read-only data collection, no betting
surface. Its `run_scraper` coroutine is consumed as-is; this module only
adapts its match dicts into our `OddsSnapshotIn` stream and registers team
context in the EventDirectory for the model layer.

The oddsharvester import is lazy so the default (extras-free) install and CI
profile keep working; install with `uv sync --extra backfill`.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.ingestion.base import EventDirectory, EventTeams, ScraperProxy

# Pinned browser-TLS impersonation for the curl_cffi JSON path (F1): a fixed
# chrome version, not bare "chrome" (which would drift with a curl_cffi upgrade).
# Sourced from the cycle orchestrator so both modules agree on one value. The
# session module imports only from app.schemas, so this is not circular.
from app.ingestion.oddsportal_json_session import PINNED_IMPERSONATE as _JSON_IMPERSONATE
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING only — app.ingestion.oddsportal_json imports
    # canonical vocabulary FROM this module, so a top-level import here would be
    # circular. Constructed via a lazy import inside the methods that need it.
    from app.ingestion.oddsportal_json import BookmakerRegistry

logger = logging.getLogger(__name__)


def _is_orphaned_playwright_future(context: dict[str, Any]) -> bool:
    """True for an asyncio 'Future exception was never retrieved' whose exception
    comes from Playwright — a scrape op orphaned when OddsHarvester closed a tab
    or browser mid-operation (TimeoutError on a DOM miss, TargetClosedError on a
    teardown/crash race, ...). Matched by MODULE, not a hard-coded class, so every
    Playwright variant is covered and playwright need not be imported here."""
    exc = context.get("exception")
    msg = context.get("message") or ""
    return (
        exc is not None
        and type(exc).__module__.startswith("playwright")
        and "never retrieved" in msg
    )


def scrape_loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """asyncio exception handler that HANDLES — never hides — Playwright futures
    orphaned when a scrape tab/browser closes mid-operation (TimeoutError on a DOM
    miss, TargetClosedError on a teardown race, ...). These are benign scrape
    misses, but asyncio otherwise dumps them as an ERROR traceback ("Future
    exception was never retrieved"). We retrieve them and log honestly (WARNING +
    type); EVERYTHING else is delegated to the default handler, so genuine
    unhandled-future bugs still surface loudly."""
    if _is_orphaned_playwright_future(context):
        logger.warning(
            "playwright scrape future orphaned on tab/browser close (benign scrape miss): %s",
            type(context["exception"]).__name__,
        )
        return
    loop.default_exception_handler(context)


def install_scrape_future_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Install scrape_loop_exception_handler on `loop` (call once at startup)."""
    loop.set_exception_handler(scrape_loop_exception_handler)


ScrapeFn = Callable[..., Awaitable[Any]]
# Per-match JSON-feed scrape: (match_url, *, markets, directory, now, proxy, ...)
# -> list[OddsSnapshotIn]. The default (`_default_json_scrape`) drives the
# curl_cffi path in app/ingestion/oddsportal_json.py; tests inject a fake so no
# network / curl_cffi import is needed. SELECTABLE + OFF by default — the proven
# Playwright path stays the per-match FALLBACK (never removed).
JsonScrapeFn = Callable[..., Awaitable[list[OddsSnapshotIn]]]

# OddsPortal forks a fixture's URL once it goes live: the SAME match page is
# also listed under an '/inplay-odds' path segment (identical trailing
# #fragment). Event identity throughout the platform IS the match link, so
# the fork must collapse to the pre-match URL — otherwise one game becomes
# TWO events: double premium exposure, a forked odds-snapshot history, and a
# tier dedupe/upgrade path that cannot see across the fork (observed live
# 2026-06-12: two premium picks on one basketball fixture, one per URL).
_INPLAY_SEGMENT_RE = re.compile(r"/inplay-odds(?=[/#?]|$)")


def normalize_match_link(link: str) -> str:
    """Collapse OddsPortal's in-play URL fork to the pre-match match link."""
    return _INPLAY_SEGMENT_RE.sub("", link)


# OddsHarvester market keys we can devig SOUNDLY (mutually-exclusive,
# full-coverage outcomes). Each line of a totals/handicap family groups
# separately via OddsSnapshotIn.market_detail. HALF-LINE Asian handicaps
# (±0.5, ±1.5, …) and European handicaps (3-way incl. handicap-draw) are
# full markets — direct devig is valid. INTEGER/QUARTER AH lines carry push
# outcomes (probabilities do not sum to 1) and are rejected at construction.
# The pricing bridge for them now EXISTS (app/models/ah_bridge.py, 2026-06-10:
# devigged 1X2+OU2.5 -> goal grid -> split-stake EV); they stay rejected here
# until the value pipeline EV path is wired and backtest-validated.
_EXACT_MARKETS: dict[str, Market] = {
    "1x2": Market.H2H,  # football/basketball 3-way
    "home_away": Market.H2H,  # basketball moneyline (OddsHarvester has no "moneyline" key)
    "match_winner": Market.H2H,  # tennis 2-way ML (upstream labels player_1/player_2)
    "btts": Market.BTTS,
    "dnb": Market.DNB,
    "double_chance": Market.DOUBLE_CHANCE,
}

# Tennis carries two totals/handicap AXES — sets and games — which collide in
# the bare over_under_/asian_handicap_ namespaces (over_under_sets_2_5 vs the
# football goals over_under_2_5). OddsHarvester encodes the axis as an infix
# (over_under_sets_/over_under_games_) or a trailing suffix
# (asian_handicap_-1_5_sets). We strip the axis to read the numeric line, but
# the FULL key is still carried into OddsSnapshotIn.market_detail, so distinct
# axes never share a devig group — group_market_prices keys on market_detail.
_TENNIS_AXIS_SUFFIXES = ("_sets", "_games")


def _market_for_key(key: str) -> Market | None:
    if key in _EXACT_MARKETS:
        return _EXACT_MARKETS[key]
    if key.startswith("over_under_"):
        return Market.TOTALS
    if key.startswith(("asian_handicap_", "european_handicap_")):
        return Market.SPREADS
    return None


def _line_from_key(market_key: str) -> float | None:
    """'over_under_2_5' -> 2.5; 'asian_handicap_-1_5' -> -1.5;
    'asian_handicap_games_-7_5_games' -> -7.5; 'european_handicap_+1' -> 1.0.
    Tennis: 'over_under_sets_2_5' -> 2.5; 'over_under_games_22_5' -> 22.5;
    'asian_handicap_-1_5_sets' -> -1.5 (the _sets/_games axis is stripped here
    but preserved in market_detail so the sets vs games lines never collide)."""
    raw = market_key
    for prefix in (
        # tennis axis-prefixed totals first (over_under_sets_/over_under_games_),
        # then the basketball-AH and football/basketball bare prefixes.
        "over_under_sets_",
        "over_under_games_",
        "over_under_",
        "asian_handicap_games_",
        "asian_handicap_",
        "european_handicap_",
    ):
        if raw.startswith(prefix):
            raw = raw.removeprefix(prefix)
            # Strip a trailing tennis axis suffix (asian_handicap_-1_5_sets,
            # ..._games) and the basketball AH _games suffix.
            for suffix in (*_TENNIS_AXIS_SUFFIXES, "_games"):
                raw = raw.removesuffix(suffix)
            try:
                return float(raw.replace("_", "."))
            except ValueError:
                return None
    return None


def _fmt_line(line: float) -> str:
    return f"{line:+g}"


# Bare wildcard family keys (JSON feed): each means "emit EVERY half-line of this
# betType". They carry no line, so the per-line half-line checks below don't apply
# — the JSON enumerator (oddsportal_json) drops integer/quarter lines as it reads
# the feed. `_market_for_key` already classifies them (over_under_ -> TOTALS,
# asian_handicap_ -> SPREADS), so they pass the unknown-market check too.
_WILDCARD_MARKET_KEYS = frozenset({"over_under_games", "asian_handicap_games"})


def _validate_markets(markets: Sequence[str]) -> None:
    unknown = [m for m in markets if _market_for_key(m) is None]
    if unknown:
        raise ValueError(f"unsupported oddsportal markets: {unknown}")
    for m in markets:
        if m in _WILDCARD_MARKET_KEYS:
            continue
        if m.startswith("asian_handicap"):
            line = _line_from_key(m)
            if line is None or abs(line % 1.0) != 0.5:
                # integer/quarter lines have PUSH outcomes -> direct devig
                # invalid; only half-lines are sound without a score grid.
                raise ValueError(
                    f"asian handicap line must be a half line (±0.5, ±1.5, …), got: {m}"
                )
        if m.startswith("over_under_"):
            line = _line_from_key(m)
            if line is None:
                raise ValueError(f"cannot parse totals line from: {m}")
            if abs(line % 1.0) != 0.5:
                # integer/quarter totals have a PUSH outcome -> direct devig is
                # invalid; only half-lines (1.5, 2.5, …) settle cleanly.
                raise ValueError(f"over/under line must be a half line (1.5, 2.5, …), got: {m}")
        if m.startswith("european_handicap_") and _line_from_key(m) is None:
            raise ValueError(f"cannot parse handicap line from: {m}")


# Leagues OddsPortal carries but OddsHarvester 0.3.0's registry omits —
# standard URL pattern, each verified live (HTTP 200) on 2026-06-11.
# (turkey-super-lig and greece-super-league ARE upstream keys already.)
_EXTRA_LEAGUES: dict[str, dict[str, str]] = {
    "football": {
        "netherlands-eredivisie": "https://www.oddsportal.com/football/netherlands/eredivisie",
        "belgium-jupiler-pro-league": (
            "https://www.oddsportal.com/football/belgium/jupiler-pro-league"
        ),
    },
    # OddsHarvester 0.3.0 ships only nfl/ncaa for american football; CFL
    # (active Jun-Nov) and UFL (spring) are the other live AF leagues
    # OddsPortal lists. Visibility-only — they mint no picks, like nfl/ncaa.
    "american-football": {
        "cfl": "https://www.oddsportal.com/american-football/canada/cfl/",
        "ufl": "https://www.oddsportal.com/american-football/usa/ufl/",
    },
}


def register_extra_leagues() -> None:
    """Extend OddsHarvester's league registry in-place (idempotent).

    setdefault means upstream wins the moment a release adds these keys.
    """
    from oddsharvester.utils.sport_league_constants import SPORTS_LEAGUES_URLS_MAPPING

    for sport_name, extras in _EXTRA_LEAGUES.items():
        for sport, leagues in SPORTS_LEAGUES_URLS_MAPPING.items():
            if str(getattr(sport, "value", sport)) == sport_name:
                for slug, url in extras.items():
                    leagues.setdefault(slug, url)


# ---------------------------------------------------------------------------
# oddsharvester 0.3.0 quirk patches — applied in place at runtime (same
# pattern as register_extra_leagues; a fork would have to track upstream for
# two ~20-line methods). Root causes from live poll logs, 2026-06-11:
#
# 1. OddsPortal keeps the OneTrust consent dialog in the DOM (hidden) after
#    dismissal. The generic tab selectors (li[class*='tab'], nav li) match
#    its ot-* nodes, so wait_for_selector burns its full timeout waiting for
#    hidden elements; worse, the 'More'-button substring search ("more" in
#    text) clicked the consent dialog — its blurb contains "more relevant".
# 2. NavigationManager.wait_for_market_switch checks only the FIRST element
#    matching stale `.active` selectors, so verification never passes on the
#    current DOM: one warning per market per match page plus 3 x 3s of dead
#    waiting — minutes of wasted wall-clock per cycle.
# 3. Exchange rows (back/lay layout) are structurally short of the parser's
#    fixed column count; skipping them is by design, not a WARNING-worthy
#    defect — the multi-book consensus proceeds without the exchange price.

_MORE_BUTTON_TEXT_MAX_LEN = 20


def _is_real_more_button(text: str | None) -> bool:
    """True only for a short, literal 'More'-style tab label — never the
    OneTrust consent blurb, whose text also contains the substring 'more'."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _MORE_BUTTON_TEXT_MAX_LEN:
        return False
    return "more" in stripped.lower() or "..." in stripped


def _patched_tab_selectors(selectors: list[str]) -> list[str]:
    """Exclude OneTrust (ot-*) nodes from the generic tab selectors."""
    exclusion = ":not([class*='ot-'])"
    return [sel + exclusion if sel in ("li[class*='tab']", "nav li") else sel for sel in selectors]


async def _patched_wait_for_market_switch(
    self: Any, page: Any, market_name: str, max_attempts: int = 3
) -> bool:
    """Drop-in for NavigationManager.wait_for_market_switch: scan ALL
    active-tab candidates, then fall back to a page-content check — the same
    confirmation MarketTabNavigator._verify_tab_is_active already accepts."""
    from oddsharvester.utils.constants import MARKET_SWITCH_WAIT_TIME_MS

    self.logger.info("Waiting for market switch to complete for: %s", market_name)
    needle = market_name.lower()
    for attempt in range(max_attempts):
        try:
            await page.wait_for_timeout(MARKET_SWITCH_WAIT_TIME_MS)
            for element in await page.query_selector_all("li.active, li[class*='active'], .active"):
                text = await element.text_content()
                if text and needle in text.lower():
                    self.logger.info("Market switch confirmed: %s is active", market_name)
                    return True
            if needle in (await page.content()).lower():
                self.logger.info("Market switch confirmed via page content: %s", market_name)
                return True
        except Exception as exc:
            self.logger.debug(
                "market switch verification attempt %d failed: %s",
                attempt + 1,
                type(exc).__name__,
            )
    self.logger.warning("Market switch verification failed after %d attempts", max_attempts)
    return False


async def _patched_wait_and_click(
    self: Any,
    page: Any,
    selector: str,
    text: str | None = None,
    timeout: float | None = None,
) -> bool:
    """Drop-in for MarketTabNavigator._wait_and_click: a selector missing its
    window is the EXPECTED path mid fallback-chain — log at debug and let
    navigate_to_tab report the real failure once the chain is exhausted."""
    if timeout is None:
        from oddsharvester.utils.constants import DEFAULT_MARKET_TIMEOUT_MS

        timeout = DEFAULT_MARKET_TIMEOUT_MS
    try:
        await page.wait_for_selector(selector=selector, timeout=timeout)
        if text:
            return bool(await self._click_by_text(page=page, selector=selector, text=text))
        element = await page.query_selector(selector)
        if element is None:
            return False
        await element.click()
        return True
    except Exception as exc:
        self.logger.debug("selector %r not clickable in time: %s", selector, type(exc).__name__)
        return False


async def _patched_click_more_if_market_hidden(
    self: Any, page: Any, market_tab_name: str, timeout: int | None = None
) -> bool:
    """Drop-in for MarketTabNavigator._click_more_if_market_hidden: only
    VISIBLE elements whose text is a short literal 'More' qualify, and only
    visible dropdown entries are clicked."""
    del timeout  # signature compatibility; candidates are queried directly
    from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors
    from oddsharvester.utils.constants import DROPDOWN_WAIT_MS

    try:
        more_clicked = False
        for selector in OddsPortalSelectors.MORE_BUTTON_SELECTORS:
            for element in await page.query_selector_all(selector):
                try:
                    if not await element.is_visible():
                        continue
                    if not _is_real_more_button(await element.text_content()):
                        continue
                    await element.click()
                    more_clicked = True
                    break
                except Exception as exc:  # candidate vanished mid-iteration
                    self.logger.debug(
                        "'More' candidate from %r failed: %s", selector, type(exc).__name__
                    )
            if more_clicked:
                break
        if not more_clicked:
            self.logger.info("No visible 'More' tab button found")
            return False

        await page.wait_for_timeout(DROPDOWN_WAIT_MS)
        needle = market_tab_name.lower()
        for selector in OddsPortalSelectors.get_dropdown_selectors_for_market(market_tab_name):
            for element in await page.query_selector_all(selector):
                try:
                    if not await element.is_visible():
                        continue
                    text = await element.text_content()
                    if text and needle in text.lower():
                        self.logger.info("Found '%s' in dropdown. Clicking...", market_tab_name)
                        await element.click()
                        return True
                except Exception as exc:
                    self.logger.debug(
                        "dropdown candidate from %r failed: %s", selector, type(exc).__name__
                    )
        return False
    except Exception as exc:
        self.logger.debug("'More' dropdown navigation failed: %s", type(exc).__name__)
        return False


def _patched_extract_bookmaker_name(self: Any, block: Any) -> Any:
    """Drop-in for OddsParser._extract_bookmaker_name: the loose fallbacks
    (<a title>, <img alt>) only fire when the row carries odds cells.

    When the bookmaker-table scoping misses its header testid and falls back
    to the whole page, H2H/Previous-Matches team rows leak in and their crest
    alt/title resolved as 'bookmakers' (Racing, Al-Mabarrah — 2026-06-11
    live logs), tripping the incomplete-odds warning and risking a team name
    being ingested as a book. Real bookmaker rows always carry odds cells.
    """
    from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors

    img_tag = block.find("img", class_=OddsPortalSelectors.BOOKMAKER_LOGO_CLASS)
    if img_tag and img_tag.get("title"):
        return img_tag["title"]

    has_odds_cells = (
        block.find("div", class_=re.compile(OddsPortalSelectors.ODDS_BLOCK_CLASS_PATTERN))
        is not None
    )
    if not has_odds_cells:
        return None  # team/peripheral row, not a bookmaker row

    a_tag = block.find("a", attrs={"title": True})
    if a_tag and a_tag["title"]:
        name = a_tag["title"]
        # Normalise CTA-style titles like "Go to Betfair Exchange website!"
        if name.lower().startswith("go to ") and name.endswith("!"):
            name = name[len("go to ") : -1].strip()
            if name.lower().endswith(" website"):
                name = name[: -len(" website")].strip()
        return name

    for img in block.find_all("img"):
        alt = img.get("alt", "")
        if alt and alt.lower() not in ("", "logo"):
            return alt
    return None


# OddsHarvester's NavigationManager.select_specific_market scrolls for a sub-line
# with PageScroller.scroll_until_visible_and_click_parent, which DEFAULTS to a 20s
# timeout (SCROLL_UNTIL_CLICK_TIMEOUT_S). When a match page simply does not offer
# an Over/Under sub-line (the common gap on thin/obscure leagues) that is a full
# 20s burned PER missing line — the live "52x Failed to find and click parent ...
# Over/Under +NNN.5 within timeout" wedge that made one match page take minutes
# and a whole cycle effectively unbounded. A missing sub-line is an EXPECTED gap,
# not a slow-network case, so it should fail FAST: the patched methods below pass
# a short bounded timeout. Still strictly read-only — only LOWERS a wait, never
# bypasses anti-bot. The per-cycle watchdog (OddsPortalLoader._scrape_bounded) is
# the hard backstop; this keeps a single page cheap so the backstop rarely fires.
_SUBMARKET_SELECT_TIMEOUT_S = 4


async def _patched_select_specific_market(self: Any, page: Any, specific_market: str) -> bool:
    """Drop-in for NavigationManager.select_specific_market: a missing sub-line
    fails fast (bounded scroll-and-click) instead of burning the upstream 20s."""
    return bool(
        await self.scroller.scroll_until_visible_and_click_parent(
            page=page,
            selector="div.flex.w-full.items-center.justify-start.pl-3.font-bold p",
            text=specific_market,
            timeout=_SUBMARKET_SELECT_TIMEOUT_S,
        )
    )


async def _patched_close_specific_market(self: Any, page: Any, specific_market: str) -> bool:
    """Drop-in for NavigationManager.close_specific_market — bounded the same way."""
    self.logger.info("Closing sub-market: %s", specific_market)
    return bool(
        await self.scroller.scroll_until_visible_and_click_parent(
            page=page,
            selector="div.flex.w-full.items-center.justify-start.pl-3.font-bold p",
            text=specific_market,
            timeout=_SUBMARKET_SELECT_TIMEOUT_S,
        )
    )


# How long to wait for the active period/bookies element to (re)attach after a
# market-tab switch before reading it. OddsPortal re-renders the
# kickoff-events-nav on each switch, so the unpatched immediate read misses the
# "already on the default Full Time period" short-circuit, clicks needlessly,
# and logs a benign "ERROR Failed to set period to: Full Time" when the verify
# races the re-render (the correct Full-Time odds still extract). Cheap when the
# element is already attached; falls through to the same None upstream returns if
# it never settles.
_ACTIVE_SETTLE_MS = 1500


async def _patched_get_current_value(self: Any, page: Any, strategy: Any) -> str | None:
    """Drop-in for SelectionManager._get_current_value: wait for the active
    element to (re)attach after a market-tab switch, THEN read it — so
    ensure_selected's already-selected short-circuit fires instead of a needless
    click and a benign ERROR. The read below mirrors upstream 0.3.0 exactly;
    only the wait is new. Graceful: on timeout/missing it returns None just as
    upstream does (secret-safe logs: exception TYPE only, at debug)."""
    active_selector = f"{strategy.container_selector} .{strategy.active_class}"
    try:
        await page.wait_for_selector(active_selector, state="attached", timeout=_ACTIVE_SETTLE_MS)
    except Exception as exc:
        self.logger.debug(
            "active %s not settled in %dms: %s",
            strategy.name,
            _ACTIVE_SETTLE_MS,
            type(exc).__name__,
        )
    try:
        active_element = await page.query_selector(active_selector)
        if active_element:
            return await strategy.extract_active_value(active_element)
        self.logger.debug("No active %s found", strategy.name)
        return None
    except Exception as exc:
        self.logger.debug("error reading current %s: %s", strategy.name, type(exc).__name__)
        return None


class _ScrapeGapDowngradeFilter(logging.Filter):
    """Downgrade expected scrape-gap messages to INFO (never drop them): a
    match simply not offering a submarket is normal. The durable DOM-break
    signal is the per-market snapshot count logged at the end of each cycle —
    a real selector break craters those counts immediately."""

    _NEEDLES = (
        "Failed to find and click parent of element",
        "Failed to find or select",
        # A match page without the market tab at all (thin/obscure leagues)
        # or without the bookies-filter nav is the same expected-gap class.
        "Failed to find or click",
        "bookies-filter navigation not found",
        # A period tab a match page doesn't offer (e.g. football double_chance
        # pages whose "Full Time" period div isn't present/ready within the
        # timeout) — same expected-gap class; the market is skipped gracefully.
        # SCOPED to "period ..." so a bookies-filter target miss (which could
        # mean we read a filtered book set) stays visible at ERROR.
        "period target element not found",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.INFO and any(
            needle in record.getMessage() for needle in self._NEEDLES
        ):
            record.levelno = logging.INFO
            record.levelname = "INFO"
        return True


class _ExchangeIncompleteOddsFilter(logging.Filter):
    """Drop the parser's incomplete-odds warning for exchange books only;
    other bookmakers' incomplete rows stay visible."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("Incomplete odds data for bookmaker" in msg and "exchange" in msg.lower())


_EXCHANGE_NOISE_FILTER = _ExchangeIncompleteOddsFilter()
_SCRAPE_GAP_FILTER = _ScrapeGapDowngradeFilter()
_upstream_patched = False

# The ONLY oddsharvester version the runtime patches below were verified
# against (pyproject pins it exactly). A version bump must re-verify every
# patch target — see .claude/memory/pitfalls.md.
_PATCHED_UPSTREAM_VERSION = "0.3.0"


def _patch_upstream_quirks() -> None:
    """Apply the quirk fixes in place (idempotent; lazy oddsharvester import)."""
    global _upstream_patched, _ORIG_EXTRACT_HEADER
    # LOUD version guard, checked BEFORE the idempotency early-return: the
    # patches replace private upstream internals, so running them against any
    # other release risks silently corrupted scraping.
    installed = importlib.metadata.version("oddsharvester")
    if installed != _PATCHED_UPSTREAM_VERSION:
        raise RuntimeError(
            f"oddsharvester {installed} != {_PATCHED_UPSTREAM_VERSION}: runtime patches "
            f"target {_PATCHED_UPSTREAM_VERSION} internals — re-verify each patch against "
            "the new version (see .claude/memory/pitfalls.md), then update this guard"
        )
    if _upstream_patched:
        return
    from oddsharvester.core.base_scraper import BaseScraper
    from oddsharvester.core.browser.market_navigation import MarketTabNavigator
    from oddsharvester.core.browser.selection import SelectionManager
    from oddsharvester.core.market_extraction.navigation_manager import NavigationManager
    from oddsharvester.core.market_extraction.odds_parser import OddsParser
    from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors

    OddsPortalSelectors.MARKET_TAB_SELECTORS = _patched_tab_selectors(
        OddsPortalSelectors.MARKET_TAB_SELECTORS
    )
    NavigationManager.wait_for_market_switch = _patched_wait_for_market_switch
    # Bound the submarket scroll-and-click so a missing Over/Under sub-line fails
    # FAST (4s) instead of upstream's 20s — the headline Over/Under wedge fix.
    NavigationManager.select_specific_market = _patched_select_specific_market
    NavigationManager.close_specific_market = _patched_close_specific_market
    MarketTabNavigator._wait_and_click = _patched_wait_and_click
    MarketTabNavigator._click_more_if_market_hidden = _patched_click_more_if_market_hidden
    OddsParser._extract_bookmaker_name = _patched_extract_bookmaker_name
    # Wait for the active period element to re-attach after a market switch so
    # the already-selected short-circuit fires — kills the benign per-market
    # "Failed to set period to: Full Time" ERROR at its source (no false click).
    SelectionManager._get_current_value = _patched_get_current_value
    # Augment the match-detail header with OddsPortal's explicit finished-status
    # (isFinished / eventStageId) so finished-score capture settles on the page's
    # "Finished" flag within minutes, and NEVER on an in-play partial.
    _ORIG_EXTRACT_HEADER = BaseScraper._extract_match_details_event_header
    BaseScraper._extract_match_details_event_header = _patched_extract_match_details_event_header
    logging.getLogger("OddsParser").addFilter(_EXCHANGE_NOISE_FILTER)
    for gap_logger in (
        "PageScroller",
        "OddsPortalMarketExtractor",
        "MarketTabNavigator",
        "SelectionManager",
    ):
        logging.getLogger(gap_logger).addFilter(_SCRAPE_GAP_FILTER)
    _upstream_patched = True


# OddsHarvester 0.3.0 hardcodes the match-page Page.goto timeout as the module
# constant NAVIGATION_TIMEOUT_MS (15000ms) in oddsharvester/utils/constants.py;
# base_scraper.py imports it BY NAME, so scrape_match()'s goto reads
# base_scraper.NAVIGATION_TIMEOUT_MS. 15s is too tight for OddsPortal's heavy
# match pages, so one slow page raises "Timeout 15000ms exceeded" and that match
# is skipped (recovered next cycle). The constant is NOT env-configurable, so we
# raise it here at the loader boundary. This is a DOCUMENTED, guarded override —
# only ever INCREASING a timeout (never an anti-bot bypass), strictly read-only.
def _apply_nav_timeout_override(timeout_ms: int | None) -> None:
    """Raise OddsHarvester's hardcoded match-page navigation timeout.

    ``None`` keeps the upstream default untouched (the extras-free path imports
    nothing). Otherwise rebinds the module global the goto call actually reads
    (``base_scraper.NAVIGATION_TIMEOUT_MS``) plus the source constant. Guarded:
    if a future oddsharvester drops/renames the constant, it degrades to a
    type-only WARNING and the scrape proceeds on whatever the library uses — the
    override can never break the read-only scrape.
    """
    if timeout_ms is None:
        return
    try:
        from oddsharvester.core import base_scraper
        from oddsharvester.utils import constants

        applied = False
        for module in (base_scraper, constants):
            # Only rebind a constant that already exists — never invent one.
            if hasattr(module, "NAVIGATION_TIMEOUT_MS"):
                module.NAVIGATION_TIMEOUT_MS = timeout_ms
                applied = True
        if applied:
            logger.info("oddsportal match-page navigation timeout set to %dms", timeout_ms)
        else:
            logger.warning(
                "oddsportal nav-timeout override skipped: NAVIGATION_TIMEOUT_MS not found "
                "in oddsharvester (version change?) — using the library default"
            )
    except Exception as exc:  # import/attr failure must never break the scrape
        logger.warning(
            "oddsportal nav-timeout override failed: %s — using the library default",
            type(exc).__name__,
        )


async def _default_scrape(**kwargs: Any) -> Any:
    """Call OddsHarvester's run_scraper as-is (lazy import)."""
    register_extra_leagues()
    _patch_upstream_quirks()
    from oddsharvester.core.scraper_app import run_scraper
    from oddsharvester.utils.command_enum import CommandEnum

    return await run_scraper(command=CommandEnum.UPCOMING_MATCHES, **kwargs)


@dataclass(frozen=True)
class _ListingResult:
    """A `run_scraper`-shaped result whose `.success` carries ONLY match URLs.

    The JSON path needs the dated listing to enumerate match URLs and NOTHING
    else — team context comes from each match's own curl_cffi HTML fetch, not a
    per-match Playwright render. So `.success` is a list of ``{"match_link": url}``
    dicts (the only key `fetch_odds`'s JSON cycle reads), mirroring
    `ScrapeResult.success` shape minus every odds/team field."""

    success: list[dict[str, Any]]
    failed: tuple[Any, ...] = ()
    partial: tuple[Any, ...] = ()


async def _default_listing_scrape(
    *,
    sport: str,
    date: str | None,
    leagues: Sequence[str] | None,
    headless: bool = True,
    browser_timezone_id: str | None = None,
    browser_locale_timezone: str | None = None,
    proxy_url: str | None = None,
    proxy_user: str | None = None,
    proxy_pass: str | None = None,
    **_ignored: Any,
) -> _ListingResult:
    """LISTING-ONLY OddsHarvester drive — enumerate match URLs, NO per-match render.

    This is the SAVINGS pivot for the JSON feed (root-cause fix 2026-06-24): the
    proven `run_scraper(UPCOMING_MATCHES)` path runs the full pipeline
    (listing -> `extract_match_odds` -> per-match `page.goto`), and OddsHarvester
    opens EVERY match page in Playwright even with ``markets=[]`` (it reads the
    header for team context). That per-match render is the whole CPU cost the
    migration must remove. So when the JSON feed is on, the listing must yield
    ONLY URLs and the per-match odds + TEAMS both come from curl_cffi.

    This function reuses OddsHarvester's OWN, proven listing logic (navigate the
    dated upcoming page, dismiss banners, lazy-load scroll, date-filter the rows,
    `extract_match_links`) but STOPS before `extract_match_odds` — so exactly ONE
    Playwright page is opened per (sport, date, league) listing, and ZERO match
    pages are rendered. The returned URLs feed `oddsportal_json.scrape_match_odds`,
    which GET-fetches each match page over curl_cffi and reads the team context
    out of THAT HTML (`extract_bootstrap_tokens`).

    READ-ONLY: it only navigates + reads the DOM (GET semantics); no odds POST,
    no betting surface. Proxy creds reach Playwright as separate fields (never in
    a logged URL), exactly like the full path.
    """
    register_extra_leagues()
    _patch_upstream_quirks()

    from datetime import datetime as _dt

    from oddsharvester.core.browser.cookies import CookieDismisser
    from oddsharvester.core.browser.market_navigation import MarketTabNavigator
    from oddsharvester.core.browser.scrolling import PageScroller
    from oddsharvester.core.browser.selection import SelectionManager
    from oddsharvester.core.odds_portal_market_extractor import OddsPortalMarketExtractor
    from oddsharvester.core.odds_portal_scraper import OddsPortalScraper
    from oddsharvester.core.playwright_manager import PlaywrightManager
    from oddsharvester.core.sport_market_registry import SportMarketRegistrar
    from oddsharvester.core.url_builder import URLBuilder
    from oddsharvester.utils.constants import GOTO_TIMEOUT_MS
    from oddsharvester.utils.proxy_manager import ProxyManager

    SportMarketRegistrar.register_all_markets()
    proxy_manager = ProxyManager(proxy_url=proxy_url, proxy_user=proxy_user, proxy_pass=proxy_pass)
    playwright_manager = PlaywrightManager()
    selection_manager = SelectionManager()
    scroller = PageScroller()
    market_extractor = OddsPortalMarketExtractor(
        scroller=scroller,
        tab_navigator=MarketTabNavigator(),
        selection_manager=selection_manager,
    )
    scraper = OddsPortalScraper(
        playwright_manager=playwright_manager,
        market_extractor=market_extractor,
        scroller=scroller,
        cookie_dismisser=CookieDismisser(),
        selection_manager=selection_manager,
        preview_submarkets_only=False,
    )

    # league=None => the league-less dated daily page (every league that day).
    league_list: list[str | None] = list(leagues) if leagues else [None]
    links: list[str] = []
    seen: set[str] = set()
    try:
        await scraper.start_playwright(
            headless=headless,
            browser_locale_timezone=browser_locale_timezone,
            browser_timezone_id=browser_timezone_id,
            proxy=proxy_manager.get_current_proxy(),
        )
        page = playwright_manager.page
        if page is None:  # pragma: no cover - start_playwright raises on failure
            return _ListingResult(success=[])
        for league in league_list:
            url = URLBuilder.get_upcoming_matches_url(sport=sport, date=date or "", league=league)
            await page.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
            await scraper._prepare_page_for_scraping(page=page)
            await scroller.scroll_until_loaded(
                page=page,
                timeout=30,
                scroll_pause_time=2,
                max_scroll_attempts=3,
                content_check_selector="div[class*='eventRow']",
            )
            date_filter = None
            if league and date:
                try:
                    date_filter = _dt.strptime(date, "%Y%m%d").date()
                except ValueError:  # pragma: no cover - dates are computed YYYYMMDD
                    date_filter = None
            # extract_match_links returns ONLY URLs — no match page is rendered.
            for link in await scraper.extract_match_links(
                page=page, date_filter=date_filter, skip_started=True
            ):
                if link not in seen:
                    seen.add(link)
                    links.append(link)
    finally:
        await scraper.stop_playwright()

    return _ListingResult(success=[{"match_link": link} for link in links])


async def _default_json_scrape(
    match_url: str,
    *,
    markets: Sequence[str],
    directory: EventDirectory,
    now: datetime,
    proxy: ScraperProxy | None = None,
    registry: BookmakerRegistry | None = None,
    session: Any | None = None,
    geo: str = "GB",
    lang: str = "en",
) -> list[OddsSnapshotIn]:
    """Drive the curl_cffi JSON-feed path for ONE match (lazy curl_cffi import).

    GET-only: hands a browser-TLS-impersonating ``AsyncSession`` to
    `oddsportal_json.scrape_match_odds`, which fetches + decrypts + parses the
    feed into `OddsSnapshotIn` rows matching the Playwright contract, translating
    numeric provider ids to canonical book NAMES via the shared `registry`
    (resolved once per cycle, GET-only; unknown id -> skip). The curl_cffi session
    only exposes ``.get`` to that function — structurally incapable of
    POST/PUT/DELETE (READ-ONLY market-data safety rule). Any failure propagates
    to the caller, which SKIPS this match (a scrape gap — no Playwright
    fallback, operator instruction 2026-06-23).

    F1: when the cycle supplies a SHARED ``session`` it is reused (one session
    for the whole ~700-GET cycle, pinned chrome impersonation, ``max_clients``
    sized to the fan-out). Only when no shared session is given (e.g. the
    off-window single-link path) does this open a short-lived one — optionally
    through one rotating proxy (creds inlined only here, never logged)."""
    from app.ingestion.oddsportal_json import scrape_match_odds

    if session is not None:
        # Reuse the cycle's shared session (F1) — proxy is already bound on it.
        return await scrape_match_odds(
            match_url,
            markets=markets,
            directory=directory,
            now=now,
            session=session,
            registry=registry,
            geo=geo,
            lang=lang,
        )

    from curl_cffi.requests import AsyncSession

    session_kwargs: dict[str, Any] = {"impersonate": _JSON_IMPERSONATE}
    if proxy is not None and proxy.url:
        # curl_cffi takes credentials inline in the proxy URL; build it here at
        # the call boundary (never logged) from the separated ScraperProxy
        # fields, mirroring the Playwright path's separate-creds handling.
        inline = _proxy_with_creds(proxy)
        session_kwargs["proxies"] = {"https": inline, "http": inline}

    async with AsyncSession(**session_kwargs) as own_session:
        return await scrape_match_odds(
            match_url,
            markets=markets,
            directory=directory,
            now=now,
            session=own_session,
            registry=registry,
            geo=geo,
            lang=lang,
        )


def _proxy_with_creds(proxy: ScraperProxy) -> str:
    """Inline ``scheme://user:pass@host:port`` for curl_cffi (it has no separate
    creds field like Playwright). Built only at the request boundary and NEVER
    logged — the loader's INFO logs are index-only, so creds can't leak."""
    if not proxy.username:
        return proxy.url
    scheme, _, rest = proxy.url.partition("://")
    if not rest:
        return proxy.url
    from urllib.parse import quote

    user = quote(proxy.username, safe="")
    pwd = quote(proxy.password, safe="")
    return f"{scheme}://{user}:{pwd}@{rest}"


# Cap the proxy failover sweep: an empty/blocked slate retries at most this many
# proxies (not the whole pool), so a genuinely-empty sport/date can't burn all 15
# proxies and starve the rest of the scrape cycle.
_MAX_PROXY_FAILOVER = 3


class OddsPortalLoader:
    """OddsLoader over OddsHarvester's upcoming-matches scraper."""

    def __init__(
        self,
        directory: EventDirectory,
        leagues_by_sport_key: dict[str, tuple[str, list[str]]],
        markets: Sequence[str] = ("1x2", "over_under_2_5"),
        scrape_fn: ScrapeFn | None = None,
        headless: bool = True,
        max_pages: int = 1,
        date: str | None = None,
        markets_by_sport_key: dict[str, Sequence[str]] | None = None,
        days_ahead: int | None = None,
        concurrency_tasks: int = 3,
        request_delay: float = 1.0,
        locale: str = "en-GB",
        proxy_pool: Sequence[ScraperProxy] = (),
        nav_timeout_ms: int | None = None,
        cycle_timeout_seconds: float | None = None,
        use_json_feed: bool = False,
        json_scrape_fn: JsonScrapeFn | None = None,
        listing_scrape_fn: ScrapeFn | None = None,
        json_concurrency: int = 8,
    ) -> None:
        """`leagues_by_sport_key` maps our sport key (e.g. "soccer") to
        (oddsharvester sport, [oddsportal league slugs]). `markets_by_sport_key`
        overrides `markets` per sport key (football and basketball use
        different OddsHarvester market keys). `date` is an optional YYYYMMDD
        filter; None (default) scrapes the general upcoming page.
        `days_ahead` switches to dated scrapes computed at FETCH time:
        one pass per UTC date from today through today+days_ahead — cycles
        then cover exactly the actionable games instead of a league's whole
        future fixture list (far-future matches are skipped by design)."""
        self._markets_by_sport = {k: tuple(v) for k, v in (markets_by_sport_key or {}).items()}
        for market_list in (tuple(markets), *self._markets_by_sport.values()):
            _validate_markets(market_list)
        # leagues=["all"] -> league-less scrape of oddsportal's dated daily
        # page (every league that day). That URL needs a date, so "all"
        # requires dated scraping. Cycle time scales with the day's full
        # fixture list x markets — busy weekends take a while.
        if days_ahead is None and date is None:
            for _sport, league_list in leagues_by_sport_key.values():
                if league_list == ["all"]:
                    raise ValueError(
                        "leagues=['all'] needs dated scraping — set days_ahead (or date)"
                    )
        self._directory = directory
        self._config = dict(leagues_by_sport_key)
        self._markets = tuple(markets)
        self._scrape = scrape_fn or _default_scrape
        self._headless = headless
        # max_pages is HISTORIC-only upstream: run_scraper forwards it to the
        # historic scraper alone and silently ignores it for UPCOMING_MATCHES
        # (the only command this loader issues) — a no-op kept for the kwarg
        # contract with scripts/ callers, not a working pagination knob.
        self._max_pages = max_pages
        self._date = date
        self._days_ahead = days_ahead
        # Upstream-sanctioned pacing knobs (upstream README Disclaimer: "Use
        # responsibly and ensure compliance with their terms of service").
        # These tune OddsHarvester's OWN scheduler — never anti-bot bypass.
        self._concurrency_tasks = concurrency_tasks
        self._request_delay = request_delay
        # Browser locale: paired with the forced UTC timezone for a COHERENT
        # human fingerprint (UTC = London -> en-GB). A real browser always
        # sends a locale; leaving it None is itself an automation tell.
        # OddsHarvester already rotates realistic UAs, randomizes the viewport,
        # jitters delays, and runs a webdriver-hiding init script — we add the
        # missing locale, never anything that DEFEATS bot detection.
        self._locale = locale
        # Rotating outbound proxy pool for the scrape (empty = host IP). Creds
        # travel via separate proxy_user/proxy_pass kwargs, never in the URL.
        self._proxy_pool = tuple(proxy_pool)
        self._proxy_cursor = 0
        # Liveness contract read by app/pipeline._record_poll: listing count
        # of the last fetch_odds per sport key. "Matches listed but zero odds
        # parsed" is the selector-break/anti-bot signature — the pipeline
        # surfaces it as a degraded poll on /health.
        self.last_fetch_matches: dict[str, int] = {}
        self.last_fetch_event_ids: dict[str, tuple[str, ...]] = {}
        # Apply the wider match-page navigation timeout once, at the loader
        # boundary (None = keep OddsHarvester's too-tight 15s default). Guarded
        # and read-only; see _apply_nav_timeout_override.
        self._nav_timeout_ms = nav_timeout_ms
        _apply_nav_timeout_override(nav_timeout_ms)
        # HARD per-scrape watchdog (cactusbets.cloud prod fix): a single hung
        # OddsPortal Over/Under extraction (PageScroller burns its 20s
        # scroll-and-click timeout per missing sub-line, x52 across a slate)
        # otherwise made a poll cycle run FOREVER — every later interval slot
        # then skipped ("max running instances reached") and settle_results
        # never ran. Each _scrape_with_failover call is bounded by this many
        # seconds; on timeout the hung scrape is CANCELLED and that pass is
        # treated as empty (recovered next cycle). None = unbounded (the
        # default for in-process callers/tests; the scheduler injects a value
        # from Settings, so prod is always bounded).
        self._cycle_timeout_seconds = cycle_timeout_seconds
        # SELECTABLE odds source (OFF by default — the proven Playwright path
        # stays the default until prod-verified). When on, the per-match ODDS
        # fetch uses the curl_cffi JSON feed (app/ingestion/oddsportal_json.py)
        # ONLY; the dated LISTING then runs with NO markets (match URLs + team
        # context only), so the per-match Playwright odds extraction is never paid
        # (real CPU savings). There is NO Playwright odds fallback (operator
        # 2026-06-23): a per-match JSON failure is logged (type only) and the
        # match is SKIPPED — a scrape gap, like a benign DOM miss. A key/bundle
        # rotation fails CLOSED with a loud WARNING (never a wrong price).
        self._use_json_feed = use_json_feed
        self._json_scrape = json_scrape_fn or _default_json_scrape
        # LISTING-ONLY scrape used by the JSON path: enumerates match URLs via a
        # SINGLE Playwright listing page (no per-match render). The per-match odds
        # + TEAMS both come from curl_cffi (`_json_scrape`). Resolution order:
        #   1. an explicit `listing_scrape_fn` (purpose-built listing injection);
        #   2. else an injected `scrape_fn` (tests wire the listing via scrape_fn
        #      — its fake yields the match-URL dicts directly, no oddsharvester);
        #   3. else the real `_default_listing_scrape` (OddsHarvester link-only
        #      drive — one listing page, zero per-match renders).
        # Only ever used when `use_json_feed` — the default path keeps `_scrape`.
        self._listing_scrape = listing_scrape_fn or scrape_fn or _default_listing_scrape
        # JSON per-match fan-out width (F3 bounded semaphore). The shared session's
        # max_clients must be >= this or curl_cffi serialises the surplus handles.
        self._json_concurrency = json_concurrency
        # Per-sport previous-cycle row count, the R3 completeness-gate baseline.
        # Updated only after a COMPLETE cycle so a degraded one can't lower the bar.
        self._prev_cycle_rows: dict[str, int] = {}

    def _markets_for(self, sport_key: str) -> tuple[str, ...]:
        return self._markets_by_sport.get(sport_key, self._markets)

    def _new_registry(self) -> BookmakerRegistry | None:
        """A fresh per-cycle bookmaker id->NAME registry when the JSON feed is on
        (None otherwise). Shared across one cycle's per-match scrapes so the
        static bundle is GET-fetched once and cached; a new instance per cycle
        picks up a rotated bundle next cycle. Lazy import avoids the
        oddsportal_json<->oddsportal circular import at module load."""
        if not self._use_json_feed:
            return None
        from app.ingestion.oddsportal_json import BookmakerRegistry

        return BookmakerRegistry()

    def _next_proxy(self) -> ScraperProxy | None:
        """One rotating proxy for a JSON per-match fetch (advances the cursor),
        or None when the pool is empty (direct host IP). Mirrors the Playwright
        failover rotation so both paths share the pool fairly; creds stay in the
        ScraperProxy fields and are only inlined at the request boundary."""
        pool = self._proxy_pool
        if not pool:
            return None
        proxy = pool[self._proxy_cursor % len(pool)]
        self._proxy_cursor = (self._proxy_cursor + 1) % len(pool)
        return proxy

    async def _json_odds_for_url(
        self,
        match_url: str,
        now: datetime,
        markets: Sequence[str],
        registry: BookmakerRegistry | None = None,
    ) -> list[OddsSnapshotIn] | None:
        """Fetch ONE match URL's odds via the curl_cffi JSON feed.

        Returns the JSON `OddsSnapshotIn` rows on success, or ``None`` to signal
        the caller to SKIP this match (NO Playwright fallback — operator 2026-06-23).
        None is returned when the URL isn't scrapeable (synthetic id), the JSON
        scrape raises (decrypt / version-guard / HTTP / envelope), OR it yields
        zero snapshots — each a scrape gap, exactly like a benign Playwright DOM
        miss. The JSON path derives team context from the match-page HTML itself,
        so only the URL is needed; it registers the SAME `EventTeams` the
        Playwright path would, keeping the directory/`last_fetch_event_ids`
        contract identical. `registry` is the shared per-cycle bookmaker
        id->NAME resolver (fetched once per cycle, GET-only)."""
        # Synthetic ids (no scrapeable URL) can't be JSON-fetched — skip them
        # (no Playwright fallback); they carry no real match page.
        if not match_url.startswith("http"):
            return None
        try:
            snaps = await self._json_scrape(
                match_url,
                markets=markets,
                directory=self._directory,
                now=now,
                proxy=self._next_proxy(),
                registry=registry,
            )
        except Exception as exc:  # decrypt / HTTP / TLS / envelope -> scrape gap
            logger.info(
                "oddsportal JSON feed failed for a match (%s) — skipping it "
                "(scrape gap, no fallback)",
                type(exc).__name__,
            )
            return None
        if not snaps:
            # Off-window / empty feed / unresolved registry -> scrape gap (skip).
            return None
        return snaps

    @contextlib.asynccontextmanager
    async def _json_session(self) -> AsyncIterator[Any | None]:
        """The cycle's SHARED curl_cffi session (F1), or None for the test fake.

        In production (`self._json_scrape is _default_json_scrape`) this opens ONE
        ``AsyncSession`` for the whole cycle with ``max_clients`` sized to the
        fan-out (curl_cffi serialises handles past ``max_clients``, so it MUST be
        >= the concurrency N) and the pinned chrome impersonation. One proxy is
        bound for the cycle (rotating per CYCLE, not per match — a single session
        binds one proxy set; creds inlined here only, never logged). When a test
        injects a fake `json_scrape_fn`, no real session exists — it yields None
        and the fake runs with no network.

        GET-only: the session is handed to `scrape_match_odds`, which only ever
        calls ``.get`` — structurally incapable of POST/PUT/DELETE (READ-ONLY)."""
        if self._json_scrape is not _default_json_scrape:
            yield None  # injected fake scrape — no shared session needed
            return
        from curl_cffi.requests import AsyncSession

        session_kwargs: dict[str, Any] = {
            "impersonate": _JSON_IMPERSONATE,
            # max_clients MUST be >= the semaphore N or curl_cffi serialises the
            # surplus in-flight handles, silently defeating the concurrency.
            "max_clients": max(self._json_concurrency, 10),
        }
        proxy = self._next_proxy()
        if proxy is not None and proxy.url:
            inline = _proxy_with_creds(proxy)
            session_kwargs["proxies"] = {"https": inline, "http": inline}
        async with AsyncSession(**session_kwargs) as session:
            yield session

    async def _json_scrape_raw(
        self,
        match_url: str,
        now: datetime,
        markets: Sequence[str],
        registry: BookmakerRegistry | None,
        session: Any | None = None,
    ) -> list[OddsSnapshotIn]:
        """ONE match's JSON scrape for the cycle orchestrator — curl errors PROPAGATE.

        Unlike `_json_odds_for_url` (which swallows every failure into a None gap),
        this RAISES on a transport failure so the orchestrator's tenacity retry can
        classify it (R1) and retry a transient blip (R2). A non-scrapeable URL
        (synthetic id) and an off-window / empty feed both return ``[]`` (a benign
        gap, never an error). Team context is registered inside the JSON scrape
        from the match-page HTML — so this needs ONLY the URL (no Playwright).

        ``session`` is the cycle's SHARED curl_cffi session (F1) when present —
        passed straight through so every match reuses ONE session. A None proxy is
        passed because the shared session already carries the cycle's proxy; the
        per-match proxy rotation only applies on the session-less single-link
        path. The fake `json_scrape_fn` in tests ignores the session kwarg."""
        if not match_url.startswith("http"):
            return []  # synthetic id — no real match page, benign gap (not an error)
        kwargs: dict[str, Any] = {
            "markets": markets,
            "directory": self._directory,
            "now": now,
            "registry": registry,
        }
        if session is not None:
            kwargs["session"] = session
        else:
            kwargs["proxy"] = self._next_proxy()
        snaps = await self._json_scrape(match_url, **kwargs)
        return list(snaps)

    async def _json_cycle_snapshots(
        self,
        matches: list[dict[str, Any]],
        now: datetime,
        markets: Sequence[str],
        sport_key: str,
    ) -> list[OddsSnapshotIn]:
        """Fan the listed match URLs out over the curl_cffi JSON feed (F3/R1/R2)
        and gate the cycle's completeness (R3).

        Each match's odds + TEAMS come from `_json_scrape_raw` (curl_cffi reads the
        match-page HTML — no Playwright render). The shared per-cycle bookmaker
        registry is resolved once (F5-per-cycle). `run_cycle` bounds concurrency
        with a semaphore, retries transient failures with backoff OUTSIDE the slot,
        and marks the cycle incomplete (fail-closed) on a row collapse or a wholly-
        missing market. An INCOMPLETE verdict is surfaced LOUDLY but still returns
        the rows it got (append-only persistence + dedupe never overwrite a healthy
        prior snapshot; the WARNING flags the degradation for /health)."""
        from app.ingestion.oddsportal_json_session import run_cycle

        registry = self._new_registry()
        match_urls = [
            normalize_match_link(str(m.get("match_link") or ""))
            for m in matches
            if str(m.get("match_link") or "").startswith("http")
        ]

        # F1: ONE shared curl_cffi session for the whole cycle (~700 GETs), with
        # max_clients sized to the fan-out (else curl_cffi serialises the surplus)
        # and the pinned chrome impersonation. Only the PRODUCTION default scrape
        # uses a real session; an injected test fake takes no session. The session
        # is created once here and reused by every match via `_json_scrape_raw`.
        async with self._json_session() as session:

            async def scrape_one(url: str) -> list[OddsSnapshotIn]:
                return await self._json_scrape_raw(url, now, markets, registry, session)

            outcome = await run_cycle(
                match_urls,
                scrape_one,
                markets=markets,
                concurrency=self._json_concurrency,
                prev_cycle_rows=self._prev_cycle_rows.get(sport_key),
            )
        if not outcome.complete:
            logger.warning(
                "oddsportal %s JSON cycle flagged INCOMPLETE: %s "
                "(transient=%d permanent=%d unknown=%d) — slate degraded, see /health",
                sport_key,
                outcome.reason,
                outcome.transient_failures,
                outcome.permanent_failures,
                outcome.unknown_failures,
            )
        # Track this cycle's row count as the next cycle's completeness baseline,
        # but ONLY when the cycle was COMPLETE — else a degraded cycle would lower
        # the floor and mask a continued degradation next time.
        if outcome.complete and outcome.snapshots:
            self._prev_cycle_rows[sport_key] = len(outcome.snapshots)
        return outcome.snapshots

    async def _scrape_bounded(self, *, scrape_fn: ScrapeFn | None = None, **kwargs: Any) -> Any:
        """`_scrape_with_failover` under the per-cycle watchdog.

        On timeout the underlying scrape coroutine is cancelled (asyncio.wait_for
        cancels the awaitable) and a sentinel "no matches" result is returned, so
        ONE hung match-page/Over-Under extraction can never wedge the whole cycle.
        Logs type-only (never the URL / proxy creds). ``None`` timeout = the
        unbounded legacy path (byte-identical to before).

        ``scrape_fn`` selects the underlying scrape (defaults to the full
        Playwright `self._scrape`); the JSON path passes `self._listing_scrape`
        so the dated pass enumerates URLs WITHOUT per-match renders."""
        timeout = self._cycle_timeout_seconds
        if timeout is None:
            return await self._scrape_with_failover(scrape_fn=scrape_fn, **kwargs)
        try:
            return await asyncio.wait_for(
                self._scrape_with_failover(scrape_fn=scrape_fn, **kwargs), timeout=timeout
            )
        except TimeoutError:
            # asyncio.wait_for raises the builtin TimeoutError (3.11+).
            # Bound exceeded: the scrape was cancelled. Treat this pass as empty —
            # the slate recovers next cycle. NEVER log the URL/league/proxy.
            logger.warning(
                "oddsportal scrape pass timed out (>%ss) — cancelled and skipped "
                "this cycle (recovered next cycle)",
                timeout,
            )
            return None

    async def _scrape_with_failover(
        self, *, scrape_fn: ScrapeFn | None = None, **kwargs: Any
    ) -> Any:
        """Scrape via the proxy pool, rotating with failover: on an exception OR
        a zero-match result (the throttle signature), retry with the NEXT proxy.
        Empty pool -> a single direct call (host IP, default). Credentials go via
        separate proxy_user/proxy_pass kwargs (never in the URL); logging is
        index-only so nothing leaks. The sweep is CAPPED at ``_MAX_PROXY_FAILOVER``
        proxies so a genuinely-empty slate (no games that day) can't burn the whole
        pool and starve later sports in the cycle.

        ``scrape_fn`` selects the scrape coroutine (default `self._scrape`); the
        JSON path injects `self._listing_scrape` (listing-only, URLs only)."""
        scrape = scrape_fn or self._scrape
        pool = self._proxy_pool
        if not pool:
            return await scrape(**kwargs)
        n = len(pool)
        tries = min(n, _MAX_PROXY_FAILOVER)
        result: Any = None
        for attempt in range(tries):
            idx = (self._proxy_cursor + attempt) % n
            proxy = pool[idx]
            try:
                result = await scrape(
                    **kwargs,
                    proxy_url=proxy.url,
                    proxy_user=proxy.username,
                    proxy_pass=proxy.password,
                )
            except Exception as exc:  # network / anti-bot / timeout
                logger.warning(
                    "oddsportal scrape via proxy #%d failed (%s); trying next",
                    idx,
                    type(exc).__name__,
                )
                result = None
                continue
            if getattr(result, "success", None):
                self._proxy_cursor = (idx + 1) % n  # advance past the winner
                return result
            logger.info("oddsportal scrape via proxy #%d returned 0 matches; trying next", idx)
        self._proxy_cursor = (self._proxy_cursor + tries) % n  # skip the ones just tried
        return result

    async def fetch_odds(self, sport_key: str) -> list[OddsSnapshotIn]:
        if sport_key not in self._config:
            logger.warning("no oddsportal league config for sport key %s", sport_key)
            return []
        sport, leagues = self._config[sport_key]
        # ["all"] -> league-less scrape: the dated daily page already lists
        # every league's games for that day.
        scrape_leagues: list[str] | None = None if leagues == ["all"] else leagues
        now = datetime.now(tz=UTC)
        if self._days_ahead is not None:
            # Dated pages (UTC, matching browser_timezone_id below): today
            # through today+N — only the actionable slate, computed per fetch.
            dates: list[str | None] = [
                (now + timedelta(days=offset)).strftime("%Y%m%d")
                for offset in range(self._days_ahead + 1)
            ]
        else:
            dates = [self._date]

        # SAVINGS PIVOT (ROOT-CAUSE FIX 2026-06-24): when the JSON feed is on, the
        # dated listing runs LISTING-ONLY (`_default_listing_scrape`): it opens a
        # SINGLE Playwright page per (sport, date, league) to enumerate match
        # URLs, then STOPS — it never calls `extract_match_odds`, so ZERO match
        # pages are rendered. (The prior wire ran the FULL `run_scraper` path with
        # markets=[]; OddsHarvester still `page.goto`s every match page to read
        # the header for team context — no CPU win. The team context now comes
        # from each match's OWN curl_cffi HTML in `scrape_match_odds`.) With the
        # flag OFF the proven full Playwright path stays the odds source.
        listing_scrape = self._listing_scrape if self._use_json_feed else self._scrape
        listing_markets: list[str] = (
            [] if self._use_json_feed else list(self._markets_for(sport_key))
        )
        matches: list[dict[str, Any]] = []
        seen_links: set[str] = set()
        for scrape_date in dates:
            # Per-DATE bound: a late date hanging never discards earlier dates'
            # already-collected matches (incremental progress, prod fix).
            result = await self._scrape_bounded(
                scrape_fn=listing_scrape,
                sport=sport,
                date=scrape_date,
                leagues=scrape_leagues,
                markets=listing_markets,
                headless=self._headless,
                max_pages=self._max_pages,  # historic-only upstream; no-op here
                # CRITICAL: oddsportal embeds timestamps shifted to the
                # BROWSER's timezone; without this, kickoffs/capture times
                # inherit the host offset (observed +3h on a Cyprus-time Mac)
                # while labeled UTC. Also keeps dated pages aligned to the
                # UTC dates computed above (upstream gotcha doc §10).
                browser_timezone_id="UTC",
                browser_locale_timezone=self._locale,  # playwright locale
                concurrency_tasks=self._concurrency_tasks,
                request_delay=self._request_delay,
            )
            for match in getattr(result, "success", None) or []:
                home = str(match.get("home_team") or "").strip()
                away = str(match.get("away_team") or "").strip()
                link = normalize_match_link(
                    str(match.get("match_link") or f"{home}|{away}|{match.get('match_date', '')}")
                )
                if link in seen_links:
                    continue  # same fixture on adjacent date pages OR its in-play fork
                seen_links.add(link)
                matches.append(match)

        markets_for_sport = self._markets_for(sport_key)
        # event_ids (the Betfair-target / /games contract) come from the LISTING
        # URLs. On the JSON path the listing dicts carry ONLY `match_link` (no
        # team fields — teams come from curl_cffi later), so the id derives from
        # the normalized URL; on the Playwright path the same URL is present.
        event_ids: list[str] = []
        for match in matches:
            home = str(match.get("home_team") or "").strip()
            away = str(match.get("away_team") or "").strip()
            link = str(match.get("match_link") or "")
            if link or (home and away):
                event_ids.append(
                    normalize_match_link(link or f"{home}|{away}|{match.get('match_date', '')}")
                )

        if self._use_json_feed:
            # Per-match ODDS + TEAMS both come from curl_cffi (no Playwright odds
            # fallback — operator 2026-06-23). Fan the slate out under the bounded
            # semaphore + tenacity retry orchestrator (F3/R1/R2) and gate the
            # cycle's completeness (R3). Team context is registered by the JSON
            # scrape itself (it reads each match-page HTML).
            snapshots = await self._json_cycle_snapshots(matches, now, markets_for_sport, sport_key)
        else:
            snapshots = []
            for match in matches:
                snapshots.extend(self._convert_match(match, now, markets_for_sport))
        self.last_fetch_matches[sport_key] = len(matches)
        self.last_fetch_event_ids[sport_key] = tuple(dict.fromkeys(event_ids))
        # Per-market counts make scrape gaps visible: OddsPortal market-tab
        # navigation is DOM-fragile upstream, so secondary markets (btts/dnb/
        # AH/EH) intermittently come back empty while 1x2 succeeds — that is
        # why picks can skew to h2h. Gaps are expected; never bypass anti-bot.
        per_market: dict[str, int] = {}
        for snap in snapshots:
            key = snap.market_detail or str(snap.market)
            per_market[key] = per_market.get(key, 0) + 1
        missing = [m for m in self._markets_for(sport_key) if m not in per_market]
        logger.info(
            "oddsportal %s: %d matches -> %d snapshots %s%s",
            sport_key,
            len(matches),
            len(snapshots),
            per_market,
            f" | markets with NO odds: {missing}" if missing and matches else "",
        )
        if matches and not snapshots:
            # A gap in SOME markets is expected; matches listed with EVERY
            # market empty is not — that is a selector/DOM break or an
            # anti-bot wall (0 rows + 0 parse errors -> suspect anti-bot,
            # upstream gotcha doc §6). Cycles still complete, so without
            # this WARNING a broken scraper looks healthy.
            logger.warning(
                "oddsportal %s: %d matches found but 0 odds snapshots parsed — "
                "selector/DOM break or anti-bot wall; check /health polls payload",
                sport_key,
                len(matches),
            )
        return snapshots

    def sport_segment(self, sport_key: str) -> str | None:
        """URL sport segment for a sport key ("soccer" -> "football") — lets
        callers pre-filter match links before spending a scrape budget."""
        cfg = self._config.get(sport_key)
        return str(cfg[0]) if cfg else None

    async def fetch_match_odds(
        self,
        sport_key: str,
        match_links: Sequence[str],
        markets: Sequence[str] | None = None,
        *,
        prefiltered: bool = False,
        score_only: bool = False,
    ) -> list[OddsSnapshotIn]:
        """Scrape SPECIFIC match pages (open picks outside the dated window
        still need fresh prices). Links from other sports are filtered out
        — oddsportal URLs embed the sport segment.

        `prefiltered=True` means the caller already routed these links to this
        sport authoritatively (e.g. the finished-score path selects them by the
        DB sport on the open pick), so the URL sport-segment filter is SKIPPED
        and the stored match URLs are scraped AS-IS. This keeps that path working
        even if OddsPortal changes a sport's URL path segment — the segment filter
        would otherwise silently drop the renamed links. The odds-poll path keeps
        the default (filter on) since it may receive mixed-sport links.

        `markets` optionally NARROWS the scrape to the submarkets the caller
        actually needs (every market key costs one browser tab per match
        page; the full configured list is 18-21 tabs). Narrowing only ever
        selects from the validated configured list — unknown keys are
        dropped, and an empty intersection falls back to the full list so a
        trimmed request can never have WORSE coverage than no request.

        `score_only=True` requests ZERO markets: OddsHarvester then skips market
        scraping entirely and only reads the match HEADER (which carries the
        finished home/away score). This is the finished-score capture path — it
        needs the score, never odds — and it MATTERS because scraping the full
        market list on a finished page re-runs the slow (sometimes hung)
        Over/Under extraction, so the per-link timeout could fire BEFORE the
        already-available score was read (only 1 of ~24 scores landed live,
        cactusbets.cloud). score_only OVERRIDES the market-trim fallback: it
        forces an empty market list (never the full-list fallback), so the
        finished-score scrape stays cheap and reliable. No odds snapshots come
        back (the score reaches the caller via the EventDirectory register)."""
        if sport_key not in self._config:
            return []
        sport, _leagues = self._config[sport_key]
        links = (
            list(match_links)
            if prefiltered
            else [link for link in match_links if f"/{sport}/" in link]
        )
        if not links:
            return []
        if score_only:
            # Force NO markets — skip the (slow, hang-prone) market extraction;
            # only the header score is needed. Overrides the trim fallback below.
            requested: tuple[str, ...] = ()
        else:
            requested = self._markets_for(sport_key)
            if markets is not None:
                wanted = set(markets)
                trimmed = tuple(key for key in requested if key in wanted)
                if trimmed:
                    requested = trimmed
        now = datetime.now(tz=UTC)
        snapshots: list[OddsSnapshotIn] = []
        # SELECTABLE source on the off-window odds path: the per-match ODDS come
        # ONLY from the curl_cffi JSON feed when the flag is on — there is NO
        # Playwright odds fallback (operator instruction 2026-06-23). A link the
        # feed can't serve is a scrape gap: logged (type only) and SKIPPED, never
        # recovered via a Playwright odds scrape. This path therefore RETURNS
        # here without ever invoking the (expensive) Playwright market extraction.
        # score_only is the finished-SCORE capture (zero markets, header-only):
        # the JSON feed is for ODDS, so score_only STAYS the cheap, well-tuned
        # Playwright header read — never the odds feed (handled below).
        if self._use_json_feed and not score_only and requested:
            registry = self._new_registry()
            for link in links:
                # The JSON path derives team context from the match-page HTML
                # itself, so only the URL is needed; a None result (failure /
                # empty) means SKIP this link (no Playwright fallback).
                match_snaps = await self._json_odds_for_url(link, now, requested, registry)
                if match_snaps:
                    snapshots.extend(match_snaps)
            logger.info(
                "oddsportal %s match-link revalidation (JSON feed): %d links x %d markets "
                "-> %d snapshots (no-fallback: unserved links are scrape gaps)",
                sport_key,
                len(links),
                len(requested),
                len(snapshots),
            )
            return snapshots
        # Same per-cycle watchdog as fetch_odds: one hung match-page extraction
        # (Over/Under wedge) must not stall the off-window / finished-score pass.
        result = await self._scrape_bounded(
            sport=sport,
            match_links=links,
            markets=list(requested),
            headless=self._headless,
            browser_timezone_id="UTC",  # see fetch_odds — host tz leaks otherwise
            browser_locale_timezone=self._locale,  # playwright locale (coherent fp)
            concurrency_tasks=self._concurrency_tasks,
            request_delay=self._request_delay,
        )
        for match in getattr(result, "success", None) or []:
            snapshots.extend(self._convert_match(match, now, requested))
        logger.info(
            "oddsportal %s match-link revalidation: %d links x %d markets -> %d snapshots",
            sport_key,
            len(links),
            len(requested),
            len(snapshots),
        )
        return snapshots

    def _convert_match(
        self, match: dict[str, Any], now: datetime, markets: Sequence[str]
    ) -> list[OddsSnapshotIn]:
        home = str(match.get("home_team") or "").strip()
        away = str(match.get("away_team") or "").strip()
        if not home or not away:
            return []
        # In-play fork collapses to the pre-match URL — one fixture, one event.
        event_id = normalize_match_link(
            str(match.get("match_link") or f"{home}|{away}|{match.get('match_date', '')}")
        )
        league = str(match.get("league_name") or "")
        self._directory.register(
            event_id,
            EventTeams(
                home=home,
                away=away,
                league=league,
                starts_at=_parse_ts(match.get("match_date")),
                # Final score + explicit finished-status, present on a post-finish
                # scrape. The capture path settles from these, gated by `finished`
                # so an in-play partial (stage 13, populated live score) is never
                # taken as final. Scores parsed only when the whole string is digits.
                home_score=_parse_score(match.get("home_score")),
                away_score=_parse_score(match.get("away_score")),
                finished=_coerce_finished(
                    match.get("is_finished"),
                    match.get("event_stage_id"),
                    match.get("event_stage_name"),
                ),
            ),
        )
        captured_at = _parse_ts(match.get("scraped_date")) or now

        snapshots: list[OddsSnapshotIn] = []
        for market_key in markets:
            entries = match.get(f"{market_key}_market") or []
            market = _market_for_key(market_key)
            if market is None:  # pragma: no cover — blocked by _validate_markets
                continue
            seen_books: set[str] = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                bookmaker = str(entry.get("bookmaker_name") or "unknown")
                # OddsHarvester can emit duplicate bookmaker rows (e.g. odds
                # history); duplicates would corrupt devig (6-leg "markets").
                if bookmaker in seen_books:
                    continue
                seen_books.add(bookmaker)
                for label, selection in _selections(market_key, home, away):
                    odds = _parse_odds(entry.get(label))
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


def _selections(market_key: str, home: str, away: str) -> list[tuple[str, str]]:
    """OddsHarvester odds-label -> our selection name (must match what the
    model layer emits so the pipeline join works). Label names verified
    against OddsHarvester's SportMarketRegistrar (2026-06-10 research)."""
    if market_key == "1x2":
        return [("1", home), ("X", "Draw"), ("2", away)]
    if market_key == "home_away":  # basketball moneyline
        return [("1", home), ("2", away)]
    if market_key == "match_winner":  # tennis 2-way ML — upstream player_1/player_2
        return [("player_1", home), ("player_2", away)]
    if market_key == "btts":
        return [("btts_yes", "BTTS Yes"), ("btts_no", "BTTS No")]
    if market_key == "dnb":
        return [("dnb_team1", home), ("dnb_team2", away)]
    if market_key == "double_chance":
        return [
            ("1X", f"{home} or Draw"),
            ("12", f"{home} or {away}"),
            ("X2", f"Draw or {away}"),
        ]
    line = _line_from_key(market_key)
    if line is None:
        return []
    if market_key.startswith("over_under_"):
        # Football goals, basketball points (over_under_games_*) and tennis
        # sets/games totals all share the upstream odds_over/odds_under labels.
        # The readable text stays axis-free (basketball already labels its
        # points totals "Over 220.5" via the over_under_games_ key); the FULL
        # market_key rides in market_detail, so the sets vs games vs goals
        # axes never collapse into one devig group regardless of the text.
        return [("odds_over", f"Over {line:g}"), ("odds_under", f"Under {line:g}")]
    # Tennis AH carries a _sets / _games SUFFIX (asian_handicap_-1_5_sets,
    # asian_handicap_+2_5_games) and DIFFERENT upstream labels than football
    # (sets_handicap_player_1 / games_handicap_player_1). Match the suffix
    # BEFORE the generic asian_handicap_ branch — otherwise the football
    # team1_handicap labels silently drop every tennis-AH snapshot. The
    # basketball asian_handicap_games_ PREFIX is matched separately below.
    if market_key.startswith("asian_handicap_") and market_key.endswith("_sets"):
        return [
            ("sets_handicap_player_1", f"{home} {_fmt_line(line)}"),
            ("sets_handicap_player_2", f"{away} {_fmt_line(-line)}"),
        ]
    if (
        market_key.startswith("asian_handicap_")
        and not market_key.startswith("asian_handicap_games_")
        and market_key.endswith("_games")
    ):
        return [
            ("games_handicap_player_1", f"{home} {_fmt_line(line)}"),
            ("games_handicap_player_2", f"{away} {_fmt_line(-line)}"),
        ]
    if market_key.startswith("asian_handicap_games_"):  # basketball AH labels differ
        return [
            ("handicap_team_1", f"{home} {_fmt_line(line)}"),
            ("handicap_team_2", f"{away} {_fmt_line(-line)}"),
        ]
    if market_key.startswith("asian_handicap_"):
        return [
            ("team1_handicap", f"{home} {_fmt_line(line)}"),
            ("team2_handicap", f"{away} {_fmt_line(-line)}"),
        ]
    if market_key.startswith("european_handicap_"):
        return [
            ("team1_handicap", f"{home} {_fmt_line(line)}"),
            ("draw_handicap", f"Draw ({_fmt_line(line)})"),
            ("team2_handicap", f"{away} {_fmt_line(-line)}"),
        ]
    return []


def _parse_odds(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return value if value > 1.0 else None


def _parse_score(raw: Any) -> int | None:
    """OddsHarvester's scraped final score, as a non-negative int — None unless
    the whole (stripped) string is digits. Guards the not-yet-finished cases
    where upstream emits "" or "-" (and any other non-numeric text)."""
    if raw is None:
        return None
    text = str(raw).strip()
    return int(text) if text.isdigit() else None


def _coerce_finished(is_finished: Any, stage_id: Any, stage_name: Any) -> bool | None:
    """Map OddsPortal's event status to a settlement-safe finished flag.

    True  — explicitly Finished (eventData.isFinished, eventStageId == 3, or
            eventStageName == "Finished"): the score is FINAL, safe to capture.
    False — a status WAS reported but is not Finished — Scheduled, or (the
            dangerous case) an in-play 2nd Half (stage 13) whose homeResult/
            awayResult carry a LIVE partial: must NEVER be recorded as final.
    None  — the source carried no status at all (obscure league / dehydrated
            page); the caller falls back to the conservative time-floor.

    isLive is deliberately NOT used — it was observed False even mid-match.
    """
    if is_finished is True:
        return True
    if stage_id == 3:
        return True
    if isinstance(stage_name, str) and stage_name.strip().lower() == "finished":
        return True
    if is_finished is None and stage_id is None and not stage_name:
        return None
    return False


def _event_finished_fields(html: str) -> dict[str, Any]:
    """Pull OddsPortal's explicit finished-status from the react-event-header
    JSON (eventData.isFinished, eventBody.eventStageId/eventStageName) so
    finished-score capture can trust the page's "Finished" flag, not just a
    timer. Returns {} when the header/JSON is absent or unparseable — the
    caller's _coerce_finished then yields None -> conservative time-floor."""
    try:
        import json

        from bs4 import BeautifulSoup

        div = BeautifulSoup(html, "html.parser").find("div", id="react-event-header")
        data = div.get("data") if div is not None else None
        if not isinstance(data, str) or not data:
            return {}
        payload = json.loads(data)
        body = payload.get("eventBody") or {}
        event = payload.get("eventData") or {}
        return {
            "is_finished": event.get("isFinished"),
            "event_stage_id": body.get("eventStageId"),
            "event_stage_name": body.get("eventStageName"),
        }
    except (ValueError, TypeError, AttributeError):
        return {}


# Bound to the upstream BaseScraper._extract_match_details_event_header at
# _patch_upstream_quirks() time so the wrapper below can delegate to it.
_ORIG_EXTRACT_HEADER: Any = None


async def _patched_extract_match_details_event_header(self: Any, page: Any, match_link: str) -> Any:
    """Drop-in wrapper for BaseScraper._extract_match_details_event_header:
    returns the upstream dict AUGMENTED with explicit finished-status fields
    (is_finished / event_stage_id / event_stage_name) from the same
    react-event-header JSON, so capture settles on the page's "Finished" flag
    rather than only a timer. Augment-only — upstream team/score fields are
    untouched; on ANY error the upstream dict passes through unchanged (status
    -> None -> time-floor fallback). An in-play 2nd-half (stage 13) yields
    finished=False downstream and is never recorded as final."""
    details = await _ORIG_EXTRACT_HEADER(self, page, match_link)
    if not isinstance(details, dict):
        return details
    try:
        html = await page.content()
        return {**details, **_event_finished_fields(html)}
    except Exception as exc:  # never let status-augment break score extraction
        self.logger.debug("finished-status augment skipped: %s", type(exc).__name__)
        return details


def _parse_ts(raw: Any) -> datetime | None:
    """Parse OddsHarvester timestamps. The installed version emits
    'YYYY-MM-DD HH:MM:SS UTC' (not ISO) — handle both, plus epoch floats."""
    if not raw:
        return None
    text = str(raw).strip()
    # epoch seconds (odds-history rows)
    try:
        return datetime.fromtimestamp(float(text), tz=UTC)
    except (ValueError, OverflowError, OSError):
        pass
    cleaned = text.replace("Z", "+00:00")
    if cleaned.endswith(" UTC"):
        cleaned = cleaned[:-4]
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
