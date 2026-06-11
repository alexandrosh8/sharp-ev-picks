"""Upstream-quirk patches for oddsharvester 0.3.0 (app/ingestion/oddsportal.py).

Covers the 2026-06-11 live-log findings:
- OneTrust consent DOM (hidden, `ot-*` classes) matched generic tab selectors
  and the 'More'-button text search, clicking the consent dialog instead of
  the market tab.
- NavigationManager.wait_for_market_switch checked only the FIRST `.active`
  match, so verification never passed: warning spam + 9s wasted per market.
- Exchange rows (back/lay layout) are structurally incomplete -> parser
  warning is by-design noise for exchanges only.
"""

import logging
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("oddsharvester")

from app.ingestion.oddsportal import (  # noqa: E402
    _ExchangeIncompleteOddsFilter,
    _is_real_more_button,
    _patch_upstream_quirks,
    _patched_tab_selectors,
    _patched_wait_for_market_switch,
)

_CONSENT_BLOB = (
    "Create profiles for personalised advertising 615 partners can use this "
    "purpose   Switch Label Information about your activity on this service "
    "can be used to present advertising that appears more relevant based on "
    "your possible interests by this and other entities.View Illustrations"
)


# --- 'More' button guard ----------------------------------------------------


def test_more_button_guard_accepts_literal_more() -> None:
    assert _is_real_more_button("More")
    assert _is_real_more_button("  more  ")
    assert _is_real_more_button("More ...")
    assert _is_real_more_button("...")


def test_more_button_guard_rejects_consent_blob_and_junk() -> None:
    assert not _is_real_more_button(_CONSENT_BLOB)
    assert not _is_real_more_button(None)
    assert not _is_real_more_button("")
    assert not _is_real_more_button("Show me more relevant advertising")


# --- tab selector hygiene ----------------------------------------------------


def test_tab_selectors_exclude_onetrust_and_are_idempotent() -> None:
    original = ["ul.odds-tabs > li", "li[class*='tab']", "nav li"]
    patched = _patched_tab_selectors(original)
    assert "li[class*='tab']:not([class*='ot-'])" in patched
    assert "nav li:not([class*='ot-'])" in patched
    assert "li[class*='tab']" not in patched
    assert "ul.odds-tabs > li" in patched  # scoped selectors untouched
    assert _patched_tab_selectors(patched) == patched  # second pass = no-op


# --- exchange parser-noise filter ---------------------------------------------


def _record(msg: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord("OddsParser", level, __file__, 0, msg, None, None)


def test_exchange_filter_drops_only_exchange_incompleteness() -> None:
    f = _ExchangeIncompleteOddsFilter()
    assert not f.filter(
        _record("Incomplete odds data for bookmaker: Betfair Exchange. Skipping...")
    )
    assert f.filter(_record("Incomplete odds data for bookmaker: Bet365. Skipping..."))
    assert f.filter(_record("No bookmaker blocks found."))


# --- market-switch verification ------------------------------------------------


class _FakeElement:
    def __init__(self, text: str | None) -> None:
        self._text = text

    async def text_content(self) -> str | None:
        return self._text


class _FakePage:
    """Duck-typed Playwright Page for the verification path."""

    def __init__(self, content: str, active_texts: tuple[str, ...] = ()) -> None:
        self._content = content
        self._active_texts = active_texts
        self.waits = 0

    async def wait_for_timeout(self, _ms: int) -> None:
        self.waits += 1

    async def query_selector_all(self, _selector: str) -> list[_FakeElement]:
        return [_FakeElement(t) for t in self._active_texts]

    async def content(self) -> str:
        return self._content


def _nav_self() -> Any:
    return SimpleNamespace(logger=logging.getLogger("test.NavigationManager"))


@pytest.mark.asyncio
async def test_market_switch_confirms_via_any_active_element() -> None:
    page = _FakePage(content="", active_texts=("Asian Handicap", "Over/Under"))
    assert await _patched_wait_for_market_switch(_nav_self(), page, "Over/Under")
    assert page.waits == 1  # single animation wait, not 3


@pytest.mark.asyncio
async def test_market_switch_falls_back_to_page_content() -> None:
    page = _FakePage(content="<html>… Over/Under …</html>", active_texts=())
    assert await _patched_wait_for_market_switch(_nav_self(), page, "Over/Under")


@pytest.mark.asyncio
async def test_market_switch_fails_honestly_when_market_absent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = _FakePage(content="<html>1X2 only</html>", active_texts=("1X2",))
    with caplog.at_level(logging.WARNING, logger="test.NavigationManager"):
        ok = await _patched_wait_for_market_switch(_nav_self(), page, "Home/Away")
    assert not ok
    assert page.waits == 3  # honoured max_attempts
    assert any("verification failed" in r.message for r in caplog.records)


# --- patch application ----------------------------------------------------------


def test_bookmaker_name_fallbacks_require_odds_cells() -> None:
    """H2H/Previous-Matches team rows (crest <img alt>, team <a title>) must
    NOT resolve as bookmakers when the scoping fallback lets them leak in;
    rows with real odds cells keep the full fallback chain."""
    from bs4 import BeautifulSoup

    from app.ingestion.oddsportal import _patched_extract_bookmaker_name

    def block(html: str) -> Any:
        return BeautifulSoup(html, "html.parser").div

    parser_self = SimpleNamespace(logger=logging.getLogger("test.OddsParser"))
    odds_cell = '<div class="flex-center flex-col font-bold"><p>2.45</p></div>'

    # primary strategy: bookmaker logo wins regardless of cells
    assert (
        _patched_extract_bookmaker_name(
            parser_self,
            block('<div><img class="bookmaker-logo" title="bet365"/></div>'),
        )
        == "bet365"
    )
    # fallback strategies allowed when the row carries odds cells
    assert (
        _patched_extract_bookmaker_name(
            parser_self,
            block(f'<div><a title="Go to Betfair Exchange website!"></a>{odds_cell}</div>'),
        )
        == "Betfair Exchange"
    )
    assert (
        _patched_extract_bookmaker_name(
            parser_self, block(f'<div><img alt="10bet"/>{odds_cell}</div>')
        )
        == "10bet"
    )
    # team rows: no odds cells -> no name, regardless of alt/title
    assert (
        _patched_extract_bookmaker_name(parser_self, block('<div><img alt="Racing"/></div>'))
        is None
    )
    assert (
        _patched_extract_bookmaker_name(
            parser_self, block('<div><a title="Al-Mabarrah"><img alt="crest"/></a></div>')
        )
        is None
    )


def test_scrape_gap_filter_downgrades_expected_misses_to_info() -> None:
    """A match not offering the submarket is an expected scrape gap — the
    durable DOM-break signal is the per-market snapshot count per cycle."""
    from app.ingestion.oddsportal import _ScrapeGapDowngradeFilter

    f = _ScrapeGapDowngradeFilter()

    scroller = logging.LogRecord(
        "PageScroller",
        logging.WARNING,
        __file__,
        0,
        "Failed to find and click parent of element matching selector 'x' "
        "with text 'Over/Under +2.5' within timeout.",
        None,
        None,
    )
    assert f.filter(scroller)
    assert scroller.levelno == logging.INFO

    extractor = logging.LogRecord(
        "OddsPortalMarketExtractor",
        logging.ERROR,
        __file__,
        0,
        "Failed to find or select Over/Under +2.5 within Over/Under",
        None,
        None,
    )
    assert f.filter(extractor)
    assert extractor.levelno == logging.INFO

    other = logging.LogRecord(
        "PageScroller", logging.WARNING, __file__, 0, "something else broke", None, None
    )
    assert f.filter(other)
    assert other.levelno == logging.WARNING  # untouched


def test_patch_upstream_quirks_applies_and_is_idempotent() -> None:
    from oddsharvester.core.browser.market_navigation import MarketTabNavigator
    from oddsharvester.core.market_extraction.navigation_manager import NavigationManager
    from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors

    _patch_upstream_quirks()
    _patch_upstream_quirks()  # second call must be a no-op

    assert NavigationManager.wait_for_market_switch.__module__ == "app.ingestion.oddsportal"
    assert MarketTabNavigator._click_more_if_market_hidden.__module__ == (
        "app.ingestion.oddsportal"
    )
    assert "li[class*='tab']" not in OddsPortalSelectors.MARKET_TAB_SELECTORS
    parser_filters = [
        f
        for f in logging.getLogger("OddsParser").filters
        if isinstance(f, _ExchangeIncompleteOddsFilter)
    ]
    assert len(parser_filters) == 1
