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

import ast
import importlib.util
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip(
    "oddsharvester",
    reason="oddsharvester not installed — run 'uv sync --extra backfill' to cover these patches",
)

from app.ingestion.base import EventDirectory  # noqa: E402
from app.ingestion.oddsportal import (  # noqa: E402
    OddsPortalLoader,
    _apply_nav_timeout_override,
    _ExchangeIncompleteOddsFilter,
    _is_real_more_button,
    _patch_upstream_quirks,
    _patched_click_more_if_market_hidden,
    _patched_extract_bookmaker_name,
    _patched_tab_selectors,
    _patched_wait_and_click,
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


# --- 'More' dropdown navigation (consent-click fix) -------------------------------


class _ClickableElement:
    def __init__(self, text: str | None, visible: bool = True) -> None:
        self._text = text
        self._visible = visible
        self.clicks = 0

    async def is_visible(self) -> bool:
        return self._visible

    async def text_content(self) -> str | None:
        return self._text

    async def click(self) -> None:
        self.clicks += 1


class _MoreDropdownPage:
    """Duck-typed Page: MORE_BUTTON_SELECTORS queries return the tab-bar
    candidates; every other selector returns the dropdown entries."""

    def __init__(self, more: list[_ClickableElement], dropdown: list[_ClickableElement]) -> None:
        self._more = more
        self._dropdown = dropdown

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def query_selector_all(self, selector: str) -> list[_ClickableElement]:
        from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors

        if selector in OddsPortalSelectors.MORE_BUTTON_SELECTORS:
            return self._more
        return self._dropdown


@pytest.mark.asyncio
async def test_click_more_skips_consent_and_hidden_nodes() -> None:
    """The headline consent-click fix: the visible OneTrust blob (its text
    contains 'more') and hidden ot-* nodes must be SKIPPED; the real 'More'
    tab is clicked, then only a VISIBLE dropdown entry."""
    hidden_ot = _ClickableElement("More", visible=False)  # hidden consent leftover
    consent = _ClickableElement(_CONSENT_BLOB, visible=True)
    real_more = _ClickableElement("More", visible=True)
    hidden_entry = _ClickableElement("Home/Away", visible=False)
    real_entry = _ClickableElement("Home/Away", visible=True)
    page = _MoreDropdownPage(
        more=[hidden_ot, consent, real_more], dropdown=[hidden_entry, real_entry]
    )
    assert await _patched_click_more_if_market_hidden(_nav_self(), page, "Home/Away")
    # a regression that clicks the hidden ot-* node or the consent dialog
    # fails on the next two lines
    assert hidden_ot.clicks == 0
    assert consent.clicks == 0
    assert real_more.clicks == 1
    assert hidden_entry.clicks == 0
    assert real_entry.clicks == 1


@pytest.mark.asyncio
async def test_click_more_reports_absence_without_clicking() -> None:
    # Only consent/hidden candidates on the page -> no 'More' to click; the
    # dropdown must never be probed and nothing may be clicked.
    consent = _ClickableElement(_CONSENT_BLOB)
    hidden = _ClickableElement("More", visible=False)
    entry = _ClickableElement("Home/Away")
    page = _MoreDropdownPage(more=[consent, hidden], dropdown=[entry])
    assert not await _patched_click_more_if_market_hidden(_nav_self(), page, "Home/Away")
    assert consent.clicks == hidden.clicks == entry.clicks == 0


# --- _wait_and_click (fallback-chain quietness) -------------------------------------


class _WaitClickPage:
    def __init__(self, element: _ClickableElement | None, timeout_expires: bool = False) -> None:
        self._element = element
        self._timeout_expires = timeout_expires

    async def wait_for_selector(self, selector: str, timeout: float) -> None:
        if self._timeout_expires:
            raise TimeoutError(f"waiting for {selector}")  # playwright-timeout stand-in

    async def query_selector(self, selector: str) -> _ClickableElement | None:
        return self._element


@pytest.mark.asyncio
async def test_wait_and_click_clicks_present_element() -> None:
    element = _ClickableElement("Over/Under")
    assert await _patched_wait_and_click(_nav_self(), _WaitClickPage(element), "li.tab", timeout=5)
    assert element.clicks == 1


@pytest.mark.asyncio
async def test_wait_and_click_delegates_text_search_to_click_by_text() -> None:
    seen: list[tuple[str, str]] = []

    async def click_by_text(page: Any, selector: str, text: str) -> bool:
        seen.append((selector, text))
        return True

    nav = SimpleNamespace(
        logger=logging.getLogger("test.NavigationManager"), _click_by_text=click_by_text
    )
    page = _WaitClickPage(_ClickableElement("unused"))
    assert await _patched_wait_and_click(nav, page, "li.tab", text="Over/Under", timeout=5)
    assert seen == [("li.tab", "Over/Under")]


@pytest.mark.asyncio
async def test_wait_and_click_timeout_is_quiet_and_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A selector missing its window mid fallback-chain is the EXPECTED path:
    return False and log at DEBUG only (upstream logged ERROR per selector —
    the warning storm). navigate_to_tab reports the real failure once."""
    page = _WaitClickPage(None, timeout_expires=True)
    with caplog.at_level(logging.DEBUG, logger="test.NavigationManager"):
        ok = await _patched_wait_and_click(_nav_self(), page, "li.gone", timeout=5)
    assert not ok
    ours = [r for r in caplog.records if "li.gone" in r.getMessage()]
    assert ours  # the miss is still visible at debug
    assert all(r.levelno == logging.DEBUG for r in ours)


@pytest.mark.asyncio
async def test_wait_and_click_handles_vanished_element() -> None:
    # selector appeared but the node vanished before query_selector — the
    # upstream version crashes on None.click(); ours reports failure.
    page = _WaitClickPage(None)
    assert not await _patched_wait_and_click(_nav_self(), page, "li.tab", timeout=5)


# --- upstream signature pinning -------------------------------------------------


def _upstream_method_params(module_name: str, class_name: str, method_name: str) -> list[str]:
    """Parameter names of an upstream method, read from the INSTALLED source
    via AST — runtime patching replaces the live class attributes, so
    inspecting the class would just reflect our own functions back."""
    spec = importlib.util.find_spec(module_name)
    assert spec is not None and spec.origin is not None
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef)
                    and item.name == method_name
                ):
                    args = item.args
                    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    raise AssertionError(f"{class_name}.{method_name} not found in {module_name}")


def test_patched_methods_match_upstream_parameter_names() -> None:
    """A silent oddsharvester version bump that reshapes a patched method
    must turn RED here, not corrupt scraping at runtime (in-repo memory
    pitfall: re-verify runtime patches on any version bump)."""
    cases: list[tuple[Any, str, str, str]] = [
        (
            _patched_wait_for_market_switch,
            "oddsharvester.core.market_extraction.navigation_manager",
            "NavigationManager",
            "wait_for_market_switch",
        ),
        (
            _patched_wait_and_click,
            "oddsharvester.core.browser.market_navigation",
            "MarketTabNavigator",
            "_wait_and_click",
        ),
        (
            _patched_click_more_if_market_hidden,
            "oddsharvester.core.browser.market_navigation",
            "MarketTabNavigator",
            "_click_more_if_market_hidden",
        ),
        (
            _patched_extract_bookmaker_name,
            "oddsharvester.core.market_extraction.odds_parser",
            "OddsParser",
            "_extract_bookmaker_name",
        ),
    ]
    for patched, module_name, class_name, method_name in cases:
        ours = list(inspect.signature(patched).parameters)
        upstream = _upstream_method_params(module_name, class_name, method_name)
        assert ours == upstream, (
            f"{class_name}.{method_name}: patched params {ours} != upstream {upstream}"
        )


@pytest.mark.asyncio
async def test_forwarded_run_scraper_kwargs_subset_of_signature() -> None:
    """Every kwarg our loader forwards must exist on the INSTALLED
    run_scraper — a version bump that renames or drops one turns red here
    instead of failing at the first live scrape."""
    from oddsharvester.core.scraper_app import run_scraper

    recorded: list[dict[str, Any]] = []

    async def recording_scrape(**kwargs: Any) -> Any:
        recorded.append(kwargs)
        return SimpleNamespace(success=[], failed=[], partial=[])

    loader = OddsPortalLoader(
        directory=EventDirectory(),
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=recording_scrape,
        days_ahead=0,
    )
    await loader.fetch_odds("soccer")
    await loader.fetch_match_odds("soccer", ["https://www.oddsportal.com/football/a/b/"])
    assert len(recorded) == 2  # both scrape paths exercised

    params = set(inspect.signature(run_scraper).parameters)
    forwarded = {name for call in recorded for name in call}
    forwarded.add("command")  # added by _default_scrape itself
    missing = forwarded - params
    assert not missing, f"kwargs unknown to installed run_scraper: {sorted(missing)}"


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

    tab_missing = logging.LogRecord(
        "MarketTabNavigator",
        logging.ERROR,
        __file__,
        0,
        "Failed to find or click the Home/Away tab (searched visible tabs and 'More' dropdown).",
        None,
        None,
    )
    assert f.filter(tab_missing)
    assert tab_missing.levelno == logging.INFO

    bookies_nav = logging.LogRecord(
        "SelectionManager",
        logging.WARNING,
        __file__,
        0,
        "bookies-filter navigation not found on page. Skipping selection.",
        None,
        None,
    )
    assert f.filter(bookies_nav)
    assert bookies_nav.levelno == logging.INFO

    # A period tab that a match page doesn't offer (e.g. football double_chance
    # pages where the "Full Time" period div isn't present/ready) is the SAME
    # expected-gap class — the match's market is skipped gracefully. Upstream
    # logs it at ERROR; downgrade to INFO so it doesn't masquerade as a real
    # failure (it was inflating the "errors" count in live monitoring).
    period_gap = logging.LogRecord(
        "SelectionManager",
        logging.ERROR,
        __file__,
        0,
        "period target element not found for: Full Time",
        None,
        None,
    )
    assert f.filter(period_gap)
    assert period_gap.levelno == logging.INFO

    # SCOPED: a bookies-filter target miss is MORE meaningful (it could mean we
    # read a filtered book set), so it must STAY at ERROR — not downgraded.
    bookies_target = logging.LogRecord(
        "SelectionManager",
        logging.ERROR,
        __file__,
        0,
        "bookies-filter target element not found for: All Bookies",
        None,
        None,
    )
    assert f.filter(bookies_target)
    assert bookies_target.levelno == logging.ERROR  # NOT downgraded

    other = logging.LogRecord(
        "PageScroller", logging.WARNING, __file__, 0, "something else broke", None, None
    )
    assert f.filter(other)
    assert other.levelno == logging.WARNING  # untouched


def test_patch_guard_rejects_unverified_upstream_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime patches replace 0.3.0 PRIVATE internals; any other
    installed version must hard-fail loudly instead of silently corrupting
    scraping (in-repo memory pitfall: re-verify patches on version bumps)."""
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.4.0")
    with pytest.raises(RuntimeError, match=r"oddsharvester 0\.4\.0 != 0\.3\.0"):
        _patch_upstream_quirks()


def test_apply_nav_timeout_override_raises_the_15s_match_page_timeout() -> None:
    """The headline robustness lever for issue 1: OddsHarvester hardcodes a 15s
    match-page Page.goto timeout (NAVIGATION_TIMEOUT_MS), not env-configurable.
    The override rebinds the binding base_scraper.scrape_match actually reads
    (the module-level name imported there), AND the source constant."""
    import oddsharvester.core.base_scraper as base_scraper
    import oddsharvester.utils.constants as constants

    original_base = base_scraper.NAVIGATION_TIMEOUT_MS
    original_const = constants.NAVIGATION_TIMEOUT_MS
    assert original_base == 15000  # upstream's too-tight default (pinned 0.3.0)
    try:
        _apply_nav_timeout_override(30000)
        # base_scraper.scrape_match() reads its OWN module global at goto time —
        # that exact binding must change, or the override is a no-op.
        assert base_scraper.NAVIGATION_TIMEOUT_MS == 30000
        assert constants.NAVIGATION_TIMEOUT_MS == 30000
    finally:
        base_scraper.NAVIGATION_TIMEOUT_MS = original_base
        constants.NAVIGATION_TIMEOUT_MS = original_const


def test_apply_nav_timeout_override_none_is_a_noop() -> None:
    """None = keep upstream's default untouched (extras-free path imports
    nothing extra and the byte-for-byte legacy behaviour is preserved)."""
    import oddsharvester.core.base_scraper as base_scraper

    original = base_scraper.NAVIGATION_TIMEOUT_MS
    try:
        _apply_nav_timeout_override(None)
        assert original == base_scraper.NAVIGATION_TIMEOUT_MS
    finally:
        base_scraper.NAVIGATION_TIMEOUT_MS = original


def test_apply_nav_timeout_override_is_guarded_against_lib_change(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If a future oddsharvester drops the constant, the override must degrade
    gracefully (log, no crash) — it can never break the read-only scrape."""
    import oddsharvester.core.base_scraper as base_scraper
    import oddsharvester.utils.constants as constants

    monkeypatch.delattr(base_scraper, "NAVIGATION_TIMEOUT_MS", raising=False)
    monkeypatch.delattr(constants, "NAVIGATION_TIMEOUT_MS", raising=False)
    with caplog.at_level(logging.WARNING, logger="app.ingestion.oddsportal"):
        _apply_nav_timeout_override(30000)  # must NOT raise
    assert not hasattr(base_scraper, "NAVIGATION_TIMEOUT_MS")  # nothing created


def test_loader_applies_nav_timeout_override_on_construction() -> None:
    """The loader is the boundary: constructing it with nav_timeout_ms applies
    the override so every match-page goto inherits the wider budget."""
    import oddsharvester.core.base_scraper as base_scraper

    original = base_scraper.NAVIGATION_TIMEOUT_MS
    try:
        OddsPortalLoader(
            directory=EventDirectory(),
            leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
            days_ahead=0,
            nav_timeout_ms=45000,
        )
        assert base_scraper.NAVIGATION_TIMEOUT_MS == 45000
    finally:
        base_scraper.NAVIGATION_TIMEOUT_MS = original


def test_loader_without_nav_timeout_leaves_upstream_default() -> None:
    import oddsharvester.core.base_scraper as base_scraper

    original = base_scraper.NAVIGATION_TIMEOUT_MS
    try:
        OddsPortalLoader(
            directory=EventDirectory(),
            leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
            days_ahead=0,
        )
        assert original == base_scraper.NAVIGATION_TIMEOUT_MS
    finally:
        base_scraper.NAVIGATION_TIMEOUT_MS = original


# --- bounded submarket selection (Over/Under wedge fix) ---------------------


@pytest.mark.asyncio
async def test_select_specific_market_uses_a_short_bounded_timeout() -> None:
    """A missing Over/Under sub-line must fail FAST: upstream
    scroll_until_visible_and_click_parent defaults to a 20s timeout per missing
    sub-line (the '52x Failed to find and click parent ... within timeout' log),
    which made one match page burn minutes. The patch passes a short bounded
    timeout so a stubborn line is skipped quickly, never wedging the cycle."""
    from app.ingestion.oddsportal import (
        _SUBMARKET_SELECT_TIMEOUT_S,
        _patched_select_specific_market,
    )

    assert _SUBMARKET_SELECT_TIMEOUT_S < 20  # shorter than upstream's default

    seen: dict[str, object] = {}

    class _Scroller:
        async def scroll_until_visible_and_click_parent(self, **kwargs: Any) -> bool:
            seen.update(kwargs)
            return False  # the sub-line is absent (the gap case)

    nav = SimpleNamespace(logger=logging.getLogger("test.NavigationManager"), scroller=_Scroller())
    ok = await _patched_select_specific_market(
        nav, page=object(), specific_market="Over/Under +2.5"
    )
    assert ok is False  # absence is reported (caller skips the sub-line)
    assert seen["text"] == "Over/Under +2.5"
    assert seen["timeout"] == _SUBMARKET_SELECT_TIMEOUT_S  # bounded, not the 20s default


@pytest.mark.asyncio
async def test_close_specific_market_is_also_bounded() -> None:
    from app.ingestion.oddsportal import (
        _SUBMARKET_SELECT_TIMEOUT_S,
        _patched_close_specific_market,
    )

    seen: dict[str, object] = {}

    class _Scroller:
        async def scroll_until_visible_and_click_parent(self, **kwargs: Any) -> bool:
            seen.update(kwargs)
            return True

    nav = SimpleNamespace(logger=logging.getLogger("test.NavigationManager"), scroller=_Scroller())
    await _patched_close_specific_market(nav, page=object(), specific_market="Over/Under +2.5")
    assert seen["timeout"] == _SUBMARKET_SELECT_TIMEOUT_S


def test_patched_select_market_matches_upstream_parameter_names() -> None:
    """Pin the patched submarket-selection methods to the installed signatures —
    a silent oddsharvester bump that reshapes them turns RED here."""
    from app.ingestion.oddsportal import (
        _patched_close_specific_market,
        _patched_select_specific_market,
    )

    for patched, method in (
        (_patched_select_specific_market, "select_specific_market"),
        (_patched_close_specific_market, "close_specific_market"),
    ):
        ours = list(inspect.signature(patched).parameters)
        upstream = _upstream_method_params(
            "oddsharvester.core.market_extraction.navigation_manager",
            "NavigationManager",
            method,
        )
        assert ours == upstream, f"{method}: patched params {ours} != upstream {upstream}"


def test_patch_upstream_quirks_applies_and_is_idempotent() -> None:
    from oddsharvester.core.browser.market_navigation import MarketTabNavigator
    from oddsharvester.core.market_extraction.navigation_manager import NavigationManager
    from oddsharvester.core.market_extraction.odds_parser import OddsParser
    from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors

    _patch_upstream_quirks()
    _patch_upstream_quirks()  # second call must be a no-op
    assert NavigationManager.select_specific_market.__module__ == "app.ingestion.oddsportal"
    assert NavigationManager.close_specific_market.__module__ == "app.ingestion.oddsportal"

    assert NavigationManager.wait_for_market_switch.__module__ == "app.ingestion.oddsportal"
    assert MarketTabNavigator._click_more_if_market_hidden.__module__ == (
        "app.ingestion.oddsportal"
    )
    assert MarketTabNavigator._wait_and_click.__module__ == "app.ingestion.oddsportal"
    assert OddsParser._extract_bookmaker_name.__module__ == "app.ingestion.oddsportal"
    assert "li[class*='tab']" not in OddsPortalSelectors.MARKET_TAB_SELECTORS
    parser_filters = [
        f
        for f in logging.getLogger("OddsParser").filters
        if isinstance(f, _ExchangeIncompleteOddsFilter)
    ]
    assert len(parser_filters) == 1
