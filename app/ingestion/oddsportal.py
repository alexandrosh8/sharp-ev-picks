"""Free OddsPortal odds via OddsHarvester, used directly (ADR-0012).

OddsHarvester (MIT, jordantete/OddsHarvester) scrapes oddsportal.com — an
odds AGGREGATOR, not a bookmaker: read-only data collection, no betting
surface. Its `run_scraper` coroutine is consumed as-is; this module only
adapts its match dicts into our `OddsSnapshotIn` stream and registers team
context in the EventDirectory for the model layer.

The oddsharvester import is lazy so the default (extras-free) install and CI
profile keep working; install with `uv sync --extra backfill`.
"""

import importlib.metadata
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from app.ingestion.base import EventDirectory, EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

logger = logging.getLogger(__name__)

ScrapeFn = Callable[..., Awaitable[Any]]

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


def _validate_markets(markets: Sequence[str]) -> None:
    unknown = [m for m in markets if _market_for_key(m) is None]
    if unknown:
        raise ValueError(f"unsupported oddsportal markets: {unknown}")
    for m in markets:
        if m.startswith("asian_handicap"):
            line = _line_from_key(m)
            if line is None or abs(line % 1.0) != 0.5:
                # integer/quarter lines have PUSH outcomes -> direct devig
                # invalid; only half-lines are sound without a score grid.
                raise ValueError(
                    f"asian handicap line must be a half line (±0.5, ±1.5, …), got: {m}"
                )
        if m.startswith("over_under_") and _line_from_key(m) is None:
            raise ValueError(f"cannot parse totals line from: {m}")
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

# The ONLY oddsharvester version the six runtime patches below were verified
# against (pyproject pins it exactly). A version bump must re-verify every
# patch target — see .claude/memory/pitfalls.md.
_PATCHED_UPSTREAM_VERSION = "0.3.0"


def _patch_upstream_quirks() -> None:
    """Apply the quirk fixes in place (idempotent; lazy oddsharvester import)."""
    global _upstream_patched
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
    from oddsharvester.core.browser.market_navigation import MarketTabNavigator
    from oddsharvester.core.market_extraction.navigation_manager import NavigationManager
    from oddsharvester.core.market_extraction.odds_parser import OddsParser
    from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors

    OddsPortalSelectors.MARKET_TAB_SELECTORS = _patched_tab_selectors(
        OddsPortalSelectors.MARKET_TAB_SELECTORS
    )
    NavigationManager.wait_for_market_switch = _patched_wait_for_market_switch
    MarketTabNavigator._wait_and_click = _patched_wait_and_click
    MarketTabNavigator._click_more_if_market_hidden = _patched_click_more_if_market_hidden
    OddsParser._extract_bookmaker_name = _patched_extract_bookmaker_name
    logging.getLogger("OddsParser").addFilter(_EXCHANGE_NOISE_FILTER)
    for gap_logger in (
        "PageScroller",
        "OddsPortalMarketExtractor",
        "MarketTabNavigator",
        "SelectionManager",
    ):
        logging.getLogger(gap_logger).addFilter(_SCRAPE_GAP_FILTER)
    _upstream_patched = True


async def _default_scrape(**kwargs: Any) -> Any:
    """Call OddsHarvester's run_scraper as-is (lazy import)."""
    register_extra_leagues()
    _patch_upstream_quirks()
    from oddsharvester.core.scraper_app import run_scraper
    from oddsharvester.utils.command_enum import CommandEnum

    return await run_scraper(command=CommandEnum.UPCOMING_MATCHES, **kwargs)


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
        # Liveness contract read by app/pipeline._record_poll: listing count
        # of the last fetch_odds per sport key. "Matches listed but zero odds
        # parsed" is the selector-break/anti-bot signature — the pipeline
        # surfaces it as a degraded poll on /health.
        self.last_fetch_matches: dict[str, int] = {}
        self.last_fetch_event_ids: dict[str, tuple[str, ...]] = {}

    def _markets_for(self, sport_key: str) -> tuple[str, ...]:
        return self._markets_by_sport.get(sport_key, self._markets)

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

        matches: list[dict[str, Any]] = []
        seen_links: set[str] = set()
        for scrape_date in dates:
            result = await self._scrape(
                sport=sport,
                date=scrape_date,
                leagues=scrape_leagues,
                markets=list(self._markets_for(sport_key)),
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

        snapshots: list[OddsSnapshotIn] = []
        event_ids: list[str] = []
        for match in matches:
            home = str(match.get("home_team") or "").strip()
            away = str(match.get("away_team") or "").strip()
            if home and away:
                event_ids.append(
                    normalize_match_link(
                        str(
                            match.get("match_link")
                            or f"{home}|{away}|{match.get('match_date', '')}"
                        )
                    )
                )
            snapshots.extend(self._convert_match(match, now, self._markets_for(sport_key)))
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
    ) -> list[OddsSnapshotIn]:
        """Scrape SPECIFIC match pages (open picks outside the dated window
        still need fresh prices). Links from other sports are filtered out
        — oddsportal URLs embed the sport segment.

        `markets` optionally NARROWS the scrape to the submarkets the caller
        actually needs (every market key costs one browser tab per match
        page; the full configured list is 18-21 tabs). Narrowing only ever
        selects from the validated configured list — unknown keys are
        dropped, and an empty intersection falls back to the full list so a
        trimmed request can never have WORSE coverage than no request."""
        if sport_key not in self._config:
            return []
        sport, _leagues = self._config[sport_key]
        links = [link for link in match_links if f"/{sport}/" in link]
        if not links:
            return []
        requested = self._markets_for(sport_key)
        if markets is not None:
            wanted = set(markets)
            trimmed = tuple(key for key in requested if key in wanted)
            if trimmed:
                requested = trimmed
        now = datetime.now(tz=UTC)
        result = await self._scrape(
            sport=sport,
            match_links=links,
            markets=list(requested),
            headless=self._headless,
            browser_timezone_id="UTC",  # see fetch_odds — host tz leaks otherwise
            browser_locale_timezone=self._locale,  # playwright locale (coherent fp)
            concurrency_tasks=self._concurrency_tasks,
            request_delay=self._request_delay,
        )
        snapshots: list[OddsSnapshotIn] = []
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
