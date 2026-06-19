"""Read-only Betfair Exchange BACK-odds capture (OddsPortal DOM row).

Pure parser + odds-math tests need no network; the page reader is exercised
through an INJECTED ``page_loader`` (static token list, no browser/network); the
price change-gate runs without a DB; the capture_once integration test uses the
compose Postgres (skip when absent, same pattern as the arcadia/persistence
tests). NO live network, ever.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams, ScraperProxy
from app.ingestion.betfair_exchange import (
    _ANCESTOR_WALK_UP,
    _BASKETBALL_BACK_OUTCOMES,
    _EXCHANGE_ALT,
    _ROW_EXTRACT_JS,
    _ROW_TESTID,
    _SECTION_TESTID,
    BOOKMAKER,
    SPORT_SEGMENTS,
    BackQuote,
    BetfairExchangeCapture,
    BetfairExchangeError,
    BetfairExchangeReader,
    MatchTarget,
    _namespace_event_ref,
    _pair_tokens,
    back_outcomes_for_segment,
    back_quotes_to_snapshots,
    extract_back_quotes,
    fractional_to_decimal,
    parse_liquidity,
)
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.storage.models import Event, OddsSnapshot
from app.storage.repositories import persist_odds_snapshots

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai"
NOW = datetime(2026, 6, 19, 18, 0, tzinfo=UTC)

# The Betfair Exchange row text the user verified live (2026-06-19, UK proxy):
#   "Betfair Exchange ... Back Lay 28/25 (9052) 5/2 (3307) 3/1 (1307) 99.3%
#    57/50 (11317) 51/20 (41) 31/10 (2683) 100"
# -> BACK home/draw/away = 28/25, 5/2, 3/1 ; LAY = 57/50, 51/20, 31/10.
# Our DOM extractor keeps only fractional-odds + parenthesised-liquidity tokens
# (overround %s are dropped), so the ordered token list it returns is:
_LIVE_ROW_TOKENS = (
    "28/25",
    "(9052)",
    "5/2",
    "(3307)",
    "3/1",
    "(1307)",
    "57/50",
    "(11317)",
    "51/20",
    "(41)",
    "31/10",
    "(2683)",
)

# A 2-way BASKETBALL exchange row (moneyline, NO draw): BACK home/away then LAY
# home/away, each a fraction + parenthesised £ liquidity, overround %s dropped.
# BACK home=4/5 -> 1.8, away=11/10 -> 2.1 ; LAY home=5/6, away=6/5 (discarded).
_BASKETBALL_ROW_TOKENS = (
    "4/5",
    "(8000)",
    "11/10",
    "(7500)",
    "5/6",
    "(9000)",
    "6/5",
    "(6000)",
)


# --------------------------------------------------------------------------- #
# Fractional -> decimal odds math (TDD core: failing test first, then code)
# --------------------------------------------------------------------------- #
def test_fractional_to_decimal_user_examples() -> None:
    # The exact conversions the user quoted from the live row.
    assert fractional_to_decimal("28/25") == pytest.approx(2.12)
    assert fractional_to_decimal("5/2") == pytest.approx(3.5)
    assert fractional_to_decimal("3/1") == pytest.approx(4.0)
    assert fractional_to_decimal("57/50") == pytest.approx(2.14)


def test_fractional_to_decimal_evens_and_odds_on() -> None:
    assert fractional_to_decimal("1/1") == pytest.approx(2.0)  # evens
    assert fractional_to_decimal("1/2") == pytest.approx(1.5)  # odds-on
    assert fractional_to_decimal(" 10/3 ") == pytest.approx(4.3333, abs=1e-3)


def test_fractional_to_decimal_rejects_garbage() -> None:
    for bad in ("", "2.12", "5-2", "abc", "5/", "/2", "(9052)"):
        assert fractional_to_decimal(bad) is None


def test_fractional_to_decimal_rejects_zero_denominator() -> None:
    assert fractional_to_decimal("5/0") is None


def test_fractional_to_decimal_always_above_one() -> None:
    # Even the shortest odds-on price stays a valid decimal (> 1.0).
    assert (fractional_to_decimal("1/100") or 0.0) > 1.0


# --------------------------------------------------------------------------- #
# Liquidity token parsing
# --------------------------------------------------------------------------- #
def test_parse_liquidity_plain_and_thousands() -> None:
    assert parse_liquidity("(9052)") == 9052.0
    assert parse_liquidity("(11,317)") == 11317.0
    assert parse_liquidity(" (41) ") == 41.0


def test_parse_liquidity_rejects_non_liquidity() -> None:
    for bad in ("9052", "28/25", "99.3%", "()", "(abc)", ""):
        assert parse_liquidity(bad) is None


# --------------------------------------------------------------------------- #
# Token pairing: ordered DOM tokens -> (odds, liquidity) cells
# --------------------------------------------------------------------------- #
def test_pair_tokens_back_then_lay() -> None:
    cells = _pair_tokens(_LIVE_ROW_TOKENS)
    # 6 cells: 3 BACK then 3 LAY, each (fraction, liquidity).
    assert cells == [
        ("28/25", 9052.0),
        ("5/2", 3307.0),
        ("3/1", 1307.0),
        ("57/50", 11317.0),
        ("51/20", 41.0),
        ("31/10", 2683.0),
    ]


def test_pair_tokens_odds_without_liquidity_pairs_none() -> None:
    cells = _pair_tokens(("28/25", "5/2", "(3307)"))
    assert cells == [("28/25", None), ("5/2", 3307.0)]


# --------------------------------------------------------------------------- #
# BACK-vs-LAY selection + liquidity gate
# --------------------------------------------------------------------------- #
def test_extract_back_quotes_takes_back_side_only() -> None:
    cells = _pair_tokens(_LIVE_ROW_TOKENS)
    quotes = extract_back_quotes(cells, min_liquidity=0.0)
    # Exactly the 3 BACK outcomes (home/draw/away) — never the LAY triple.
    assert [q.designation for q in quotes] == ["home", "draw", "away"]
    assert [round(q.decimal_odds, 2) for q in quotes] == [2.12, 3.5, 4.0]
    assert [q.liquidity for q in quotes] == [9052.0, 3307.0, 1307.0]
    # The LAY prices (57/50 -> 2.14, etc.) must NOT appear.
    assert all(q.decimal_odds != pytest.approx(2.14) for q in quotes)


def test_liquidity_gate_drops_thin_outcomes() -> None:
    cells = _pair_tokens(_LIVE_ROW_TOKENS)
    # Floor between the draw (3307) and away (1307) liquidities.
    quotes = extract_back_quotes(cells, min_liquidity=2000.0)
    assert [q.designation for q in quotes] == ["home", "draw"]  # away (1307) gated out


def test_liquidity_gate_can_empty_a_thin_market() -> None:
    cells = _pair_tokens(_LIVE_ROW_TOKENS)
    assert extract_back_quotes(cells, min_liquidity=1_000_000.0) == []


def test_extract_back_quotes_skips_missing_liquidity() -> None:
    # An odds cell whose liquidity is absent is dropped even with a 0 floor.
    cells: list[tuple[str, float | None]] = [("28/25", None), ("5/2", 3307.0), ("3/1", 1307.0)]
    quotes = extract_back_quotes(cells, min_liquidity=0.0)
    assert [q.designation for q in quotes] == ["draw", "away"]


def test_extract_back_quotes_two_way_outcomes() -> None:
    # Forward-compat: a 2-way market (tennis) keys home/away only.
    cells: list[tuple[str, float | None]] = [("5/4", 5000.0), ("13/8", 4000.0)]
    quotes = extract_back_quotes(cells, outcomes=("home", "away"), min_liquidity=0.0)
    assert [q.designation for q in quotes] == ["home", "away"]


# --------------------------------------------------------------------------- #
# Snapshot construction (selection naming + liquidity carried through)
# --------------------------------------------------------------------------- #
def _teams() -> EventTeams:
    return EventTeams(home="Real Madrid", away="Al Hilal", league="FIFA Club World Cup")


def test_back_quotes_to_snapshots_names_selections() -> None:
    quotes = [
        BackQuote("home", 2.12, 9052.0),
        BackQuote("draw", 3.5, 3307.0),
        BackQuote("away", 4.0, 1307.0),
    ]
    snaps = back_quotes_to_snapshots("https://op/match", quotes, _teams(), now=NOW)
    assert [s.selection for s in snaps] == ["Real Madrid", "Draw", "Al Hilal"]
    assert all(s.bookmaker == BOOKMAKER for s in snaps)
    assert all(s.market is Market.H2H for s in snaps)
    assert [s.decimal_odds for s in snaps] == [2.12, 3.5, 4.0]
    assert [s.liquidity for s in snaps] == [9052.0, 3307.0, 1307.0]
    assert all(s.captured_at == NOW and s.ingested_at == NOW for s in snaps)
    # Aware-UTC timestamps (naive would be a bug).
    assert all(s.captured_at.tzinfo is not None for s in snaps)


def test_bookmaker_name_is_a_sharp_book() -> None:
    # The persisted name must normalize to the SHARP_BOOKS / EXCHANGE_COMMISSION
    # key so the edge engine can ever recognise it (v1 mints nothing, but the
    # name must stay aligned).
    from app.edge.value import EXCHANGE_COMMISSION, SHARP_BOOKS

    assert BOOKMAKER.lower() in SHARP_BOOKS
    assert BOOKMAKER.lower() in EXCHANGE_COMMISSION


def test_backquote_is_frozen() -> None:
    q = BackQuote("home", 2.12, 9052.0)
    with pytest.raises(FrozenInstanceError):
        q.decimal_odds = 9.9  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Reader: injected page_loader -> NO network. End-to-end token -> BACK quote.
# --------------------------------------------------------------------------- #
def _static_loader(
    tokens: Sequence[str] | None,
) -> Callable[..., Awaitable[list[str] | None]]:
    async def loader(*, url: str, proxy: ScraperProxy | None) -> list[str] | None:  # noqa: ARG001
        return list(tokens) if tokens is not None else None

    return loader


@pytest.mark.asyncio
async def test_reader_parses_live_row_via_injected_loader() -> None:
    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(_LIVE_ROW_TOKENS))
    quotes = await reader.read_back_quotes("https://op/match")
    assert [q.designation for q in quotes] == ["home", "draw", "away"]
    assert [round(q.decimal_odds, 2) for q in quotes] == [2.12, 3.5, 4.0]


def test_back_outcomes_for_segment_per_sport() -> None:
    # The reader's outcome WIDTH is driven by the URL segment: soccer 3-way,
    # basketball 2-way; an unmapped segment falls back to the 3-way widest read.
    assert back_outcomes_for_segment("football") == ("home", "draw", "away")
    assert back_outcomes_for_segment("basketball") == ("home", "away")
    assert back_outcomes_for_segment("unknown-sport") == ("home", "draw", "away")


@pytest.mark.asyncio
async def test_reader_basketball_two_way_keeps_two_back_discards_lay() -> None:
    # A 2-way basketball row: the reader keeps exactly 2 BACK quotes (home/away)
    # and DISCARDS the LAY tail — never a third (no-draw) selection.
    reader = BetfairExchangeReader(
        min_liquidity=0.0, page_loader=_static_loader(_BASKETBALL_ROW_TOKENS)
    )
    quotes = await reader.read_back_quotes("https://op/bball", outcomes=_BASKETBALL_BACK_OUTCOMES)
    assert [q.designation for q in quotes] == ["home", "away"]
    assert [round(q.decimal_odds, 2) for q in quotes] == [1.8, 2.1]
    assert [q.liquidity for q in quotes] == [8000.0, 7500.0]
    # The LAY prices (5/6 -> 1.83, 6/5 -> 2.2) must NEVER appear.
    for lay_dec in (1.83, 2.2):
        assert all(round(q.decimal_odds, 2) != lay_dec for q in quotes)
    # No "Draw" designation/selection is ever produced for a 2-way sport.
    assert "draw" not in [q.designation for q in quotes]
    snaps = back_quotes_to_snapshots("https://op/bball", quotes, _teams(), now=NOW)
    assert [s.selection for s in snaps] == ["Real Madrid", "Al Hilal"]
    assert "Draw" not in [s.selection for s in snaps]


@pytest.mark.asyncio
async def test_reader_no_betfair_row_returns_empty() -> None:
    # A thin/obscure match with no Betfair Exchange row (loader returns None) is
    # an expected gap, not an error.
    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(None))
    assert await reader.read_back_quotes("https://op/thin") == []


@pytest.mark.asyncio
async def test_reader_proxy_failover_then_raises() -> None:
    attempts = {"n": 0}

    async def boom(*, url: str, proxy: ScraperProxy | None) -> list[str] | None:  # noqa: ARG001
        attempts["n"] += 1
        raise TimeoutError("transport")

    pool = (
        ScraperProxy(url="http://h1:1", username="u", password="p"),
        ScraperProxy(url="http://h2:2", username="u", password="p"),
    )
    reader = BetfairExchangeReader(min_liquidity=0.0, proxy_pool=pool, page_loader=boom)
    with pytest.raises(BetfairExchangeError) as exc:
        await reader.read_back_quotes("https://op/match")
    assert attempts["n"] == 2  # tried every proxy
    # Error message carries the exception TYPE only — never the URL or creds.
    assert "https://op/match" not in str(exc.value)
    assert "TimeoutError" in str(exc.value)
    for secret in ("h1", "h2", "pass", "://u"):
        assert secret not in str(exc.value)


# --------------------------------------------------------------------------- #
# DOM extraction against the REAL structure (static HTML, no network).
#
# Live matches were 0/10 today (our slate is mostly obscure leagues with no
# Betfair liquidity / closed markets), so a live read cannot verify the fix.
# Instead we run the ACTUAL `_ROW_EXTRACT_JS` against a hand-built HTML fixture
# that mirrors the real ancestor chain the user probed via Playwright:
#   img[alt="Betfair Exchange"]
#     -> logo link (A)
#     -> DIV "...justify-center..."  "Betfair Exchange CLAIM BONUS"  (NO odds)
#     -> DIV  the betting-exchanges-table-row  logo+"Back"/"Lay"     (NO odds)
#     -> DIV "w-full"                          "...Back Lay"         (NO odds)
#     -> DIV "flex"  BACK triple then LAY triple                     (THE ODDS)
# The odds are odd-container cells ~2 levels ABOVE the testid'd row, so the
# extractor walks up and stops at the nearest ancestor holding >= 2 of them.
#
# This test touches a real headless browser, so it skips cleanly when Playwright
# or its Chromium build is unavailable (same pattern as the DB tests skipping
# without compose Postgres) — it is NOT part of the network-free core path.
# --------------------------------------------------------------------------- #


# Odds container holds the BACK triple (home/draw/away) then the LAY triple, each
# a fraction immediately followed by its parenthesised £ liquidity, with overround
# %s interleaved (must be dropped). Mirrors the live row text the user captured.
def _odd_cell(value: str, liq: str) -> str:
    """One [data-testid="odd-container"] price cell, mirroring the live DOM: the
    odds VALUE duplicated in a hidden <a> and a visible <p> (responsive
    show/hide), then the parenthesised £ liquidity as a sibling <div>."""
    return (
        '<div data-testid="odd-container">'
        '<div><div class="flex flex-col items-center">'
        '<div class="flex flex-row items-center">'
        f'<a class="hidden underline min-mt:!flex" href="/betslip">{value}</a>'
        f'<p class="min-mt:!hidden">{value}</p>'
        "</div>"
        f'<div class="font-main text-[10px]">{liq}</div>'
        "</div></div></div>"
    )


def _payout_cell(pct: str) -> str:
    """A [data-testid="payout-container"] cell — the overround %, which the
    odd-container-scoped extractor must NEVER pick up as an odds value."""
    return f'<div data-testid="payout-container"><div><p>{pct}</p></div></div>'


def _scroll_el(back: str, lay: str) -> str:
    """The scroll-el odds block: a BACK row then a LAY row, each three
    odd-container cells followed by a payout-container, exactly as OddsPortal
    renders the Betfair exchange row in a SIBLING of the testid row."""
    return (
        '<div class="scroll-el flex flex-col"><div class="flex flex-col">'
        f'<div class="flex h-[50px] border-b">{back}</div>'
        f'<div class="flex h-[50px]">{lay}</div>'
        "</div></div>"
    )


# FRACTIONAL render (the UK default a fresh scraper context gets): BACK
# home/draw/away 28/25, 5/2, 3/1 then the LAY triple, each + £ liquidity. The
# odd-container-scoped extractor returns the flat _LIVE_ROW_TOKENS sequence.
_BACK_LAY_ODDS_HTML = _scroll_el(
    back=(
        _odd_cell("28/25", "(9052)")
        + _odd_cell("5/2", "(3307)")
        + _odd_cell("3/1", "(1307)")
        + _payout_cell("99.3%")
    ),
    lay=(
        _odd_cell("57/50", "(11317)")
        + _odd_cell("51/20", "(41)")
        + _odd_cell("31/10", "(2683)")
        + _payout_cell("100%")
    ),
)

# DECIMAL render (the format the user's logged-in browser served — odds format
# is a per-visitor cookie). Same structure, decimal values: BACK 6.51/3.61/1.66
# then LAY 7.32/3.95/1.74. The extractor must read these directly via
# parse_odds_value, where the old fractional-only reader captured NOTHING.
_DECIMAL_ODDS_HTML = _scroll_el(
    back=(
        _odd_cell("6.51", "(1838)")
        + _odd_cell("3.61", "(29682)")
        + _odd_cell("1.66", "(7274)")
        + _payout_cell("96.7%")
    ),
    lay=(
        _odd_cell("7.32", "(2057)")
        + _odd_cell("3.95", "(2264)")
        + _odd_cell("1.74", "(17786)")
        + _payout_cell("103.5%")
    ),
)


def _exchange_section_html(odds_block: str = "") -> str:
    """A betting-exchanges section whose testid row carries ONLY the Betfair
    logo + Back/Lay header + "CLAIM BONUS" promo (no odds). ``odds_block`` (the
    scroll-el odd-container cells) sits as a SIBLING ~2 levels ABOVE that row,
    exactly as the live DOM renders; "" means no odds anywhere (a closed /
    illiquid market). The logo img[alt] identifies the Betfair row in both."""
    return f"""
    <div data-testid="{_SECTION_TESTID}">
      <div class="w-full">
        <div class="max-ms:justify-center flex w-full items-center">
          <a class="logo-link">
            <img alt="{_EXCHANGE_ALT}" src="bf.png">
          </a>
          <span>CLAIM BONUS</span>
          <div data-testid="{_ROW_TESTID}"
               class="flex-center min-h-[101px] w-full border-b border-l">
            <a class="logo-link"><img alt="{_EXCHANGE_ALT}" src="bf.png"></a>
            <span>Back</span><span>Lay</span>
          </div>
          {odds_block}
        </div>
      </div>
    </div>
    """


async def _evaluate_row_extract(html: str) -> list[str] | None:
    """Run the REAL in-page _ROW_EXTRACT_JS against static HTML via a headless
    Chromium page.set_content — no network. Skips when Playwright/Chromium is
    unavailable, mirroring the other browser-touching skips."""
    async_api = pytest.importorskip("playwright.async_api")
    try:
        pw_cm = async_api.async_playwright()
        pw = await pw_cm.__aenter__()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"playwright unavailable: {type(exc).__name__}")
    try:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # Chromium not installed
            pytest.skip(f"chromium not installed: {type(exc).__name__}")
        try:
            page = await browser.new_page()
            await page.set_content(html)
            raw = await page.evaluate(
                _ROW_EXTRACT_JS,
                [_SECTION_TESTID, _ROW_TESTID, _EXCHANGE_ALT, _ANCESTOR_WALK_UP],
            )
            return list(raw) if raw else None
        finally:
            await browser.close()
    finally:
        await pw_cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_row_extract_js_walks_up_to_ancestor_odds() -> None:
    # The odds live ~2 levels ABOVE the testid row; the extractor must climb to
    # them and return the BACK triple then LAY triple (overround %s dropped).
    tokens = await _evaluate_row_extract(_exchange_section_html(_BACK_LAY_ODDS_HTML))
    assert tokens == list(_LIVE_ROW_TOKENS)

    # tokens -> cells -> gated BACK quotes -> snapshots: BACK 2.12/3.5/4.0 with
    # liquidity 9052/3307/1307; the LAY triple is discarded.
    cells = _pair_tokens(tokens)
    quotes = extract_back_quotes(cells, min_liquidity=0.0)
    assert [q.designation for q in quotes] == ["home", "draw", "away"]
    assert [round(q.decimal_odds, 2) for q in quotes] == [2.12, 3.5, 4.0]
    assert [q.liquidity for q in quotes] == [9052.0, 3307.0, 1307.0]

    snaps = back_quotes_to_snapshots("https://op/match", quotes, _teams(), now=NOW)
    assert [round(s.decimal_odds, 2) for s in snaps] == [2.12, 3.5, 4.0]
    assert [s.liquidity for s in snaps] == [9052.0, 3307.0, 1307.0]
    # The LAY prices (57/50 -> 2.14, 51/20 -> 3.55, 31/10 -> 4.1) never appear.
    for lay_dec in (2.14, 3.55, 4.1):
        assert all(round(s.decimal_odds, 2) != lay_dec for s in snaps)


@pytest.mark.asyncio
async def test_row_extract_js_closed_market_returns_none() -> None:
    # The Betfair row renders (logo + Back/Lay header) but NO odds hydrate
    # anywhere — a closed / illiquid market. The walk-up finds nothing and the
    # extractor returns None; the reader maps that to [] (an expected gap).
    tokens = await _evaluate_row_extract(_exchange_section_html(""))
    assert tokens is None

    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(tokens))
    assert await reader.read_back_quotes("https://op/closed") == []


@pytest.mark.asyncio
async def test_row_extract_js_decimal_format() -> None:
    # OddsPortal's odds format is a per-visitor cookie: the SAME row can render
    # DECIMAL ("6.51") instead of fractional. The reader must read both — the old
    # fractional-only extractor captured NOTHING on a decimal page (the real
    # 0-capture bug). BACK home/draw/away = 6.51/3.61/1.66.
    tokens = await _evaluate_row_extract(_exchange_section_html(_DECIMAL_ODDS_HTML))
    assert tokens == [
        "6.51",
        "(1838)",
        "3.61",
        "(29682)",
        "1.66",
        "(7274)",
        "7.32",
        "(2057)",
        "3.95",
        "(2264)",
        "1.74",
        "(17786)",
    ]
    cells = _pair_tokens(tokens)
    quotes = extract_back_quotes(cells, min_liquidity=0.0)
    assert [q.designation for q in quotes] == ["home", "draw", "away"]
    assert [q.decimal_odds for q in quotes] == [6.51, 3.61, 1.66]
    assert [q.liquidity for q in quotes] == [1838.0, 29682.0, 7274.0]
    # The LAY decimal prices (7.32/3.95/1.74) are discarded.
    snaps = back_quotes_to_snapshots("https://op/match", quotes, _teams(), now=NOW)
    assert [s.decimal_odds for s in snaps] == [6.51, 3.61, 1.66]
    for lay_dec in (7.32, 3.95, 1.74):
        assert all(s.decimal_odds != lay_dec for s in snaps)


# --------------------------------------------------------------------------- #
# Capture: price change-gate + isolation (no picks/alerts; betfair_ namespace)
# --------------------------------------------------------------------------- #
def _target(url: str = "https://op/rma-hil") -> MatchTarget:
    return MatchTarget(event_id=url, url=url, teams=_teams())


@pytest.mark.asyncio
async def test_capture_change_gate_emits_once_then_on_move() -> None:
    # Build a reader whose tokens we can swap between cycles.
    state = {"tokens": list(_LIVE_ROW_TOKENS)}

    async def loader(*, url: str, proxy: ScraperProxy | None) -> list[str]:  # noqa: ARG001
        return list(state["tokens"])

    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=loader)
    capture = BetfairExchangeCapture(
        reader,
        session_factory=None,  # no DB: exercise the gate only
        targets_fn=lambda sport: [_target()],
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    # First cycle persists nothing (session_factory None) but PRIMES the gate.
    assert await capture.capture_once() == {"soccer": 0}
    # Re-reading the SAME prices yields no fresh snapshots (all gated).
    fresh = capture._select_fresh(
        "soccer",
        _namespace_event_ref(_target().event_id),
        back_quotes_to_snapshots(
            _target().event_id,
            await reader.read_back_quotes(_target().url),
            _teams(),
            now=NOW,
        ),
    )
    assert fresh == []
    # A price move on the home BACK leg (28/25 -> 6/5) re-opens that selection.
    state["tokens"][0] = "6/5"
    moved = capture._select_fresh(
        "soccer",
        _namespace_event_ref(_target().event_id),
        back_quotes_to_snapshots(
            _target().event_id,
            await reader.read_back_quotes(_target().url),
            _teams(),
            now=NOW,
        ),
    )
    assert [s.selection for s in moved] == ["Real Madrid"]


@pytest.mark.asyncio
async def test_capture_skips_unsupported_sport() -> None:
    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(_LIVE_ROW_TOKENS))
    capture = BetfairExchangeCapture(
        reader,
        session_factory=None,
        targets_fn=lambda sport: [_target()],
        sports=("tennis",),  # not in SPORT_SEGMENTS (soccer + basketball only)
        now_fn=lambda: NOW,
    )
    assert "tennis" not in SPORT_SEGMENTS
    assert await capture.capture_once() == {}  # unsupported sport skipped


@pytest.mark.asyncio
async def test_capture_supports_basketball_sport() -> None:
    # Basketball IS supported now (2-way moneyline). SPORT_SEGMENTS gates it in.
    assert SPORT_SEGMENTS["basketball"] == "basketball"
    assert SPORT_SEGMENTS["soccer"] == "football"


@pytest.mark.asyncio
async def test_capture_none_reader_writes_nothing() -> None:
    capture = BetfairExchangeCapture(
        None, session_factory=None, targets_fn=lambda sport: [], sports=("soccer",)
    )
    assert await capture.capture_once() == {"soccer": 0}


# --------------------------------------------------------------------------- #
# DB integration: rows land under the ISOLATED betfair_<sport> namespace and
# carry NO picks (capture writes odds_snapshots only). Skipped without Postgres.
#
# ISOLATION: these tests run inside ONE transaction on ONE connection that is
# ROLLED BACK at teardown, so nothing they write is ever COMMITTED to the shared
# compose Postgres (:5433). Earlier they committed fixture events + snapshots and
# never cleaned up, accumulating "betfair:https://op/..." pollution that
# corrupted the coverage report / audits. The capture's persist_odds_snapshots
# commits, but join_transaction_mode="create_savepoint" turns each commit into a
# SAVEPOINT release inside the fixture's outer transaction — exactly the
# tests/test_resolution_db.py + tests/test_betfair_clv_consumption.py pattern.
# Rows stay visible WITHIN the test (so the re-capture-writes-nothing assertion
# still holds) and vanish at teardown (assert-no-leftover below proves it).
# --------------------------------------------------------------------------- #
@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:  # noqa: BLE001
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


async def test_capture_persists_under_isolated_namespace(factory) -> None:  # type: ignore[no-untyped-def]
    # uuid keeps the external_ref unique within the (rolled-back) transaction.
    url = f"https://op/betfair-iso-{uuid4()}"

    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(_LIVE_ROW_TOKENS))
    capture = BetfairExchangeCapture(
        reader,
        session_factory=factory,
        targets_fn=lambda sport: [_target(url)],
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    written = await capture.capture_once()
    assert written["soccer"] == 3  # home/draw/away BACK rows
    async with factory() as session:
        rows = (
            await session.execute(
                select(OddsSnapshot.bookmaker, OddsSnapshot.selection, OddsSnapshot.liquidity)
                .join(Event, Event.id == OddsSnapshot.event_id)
                .where(Event.external_ref == f"betfair:{url}")
            )
        ).all()
        assert len(rows) == 3
        assert {r.bookmaker for r in rows} == {BOOKMAKER}
        assert {r.selection for r in rows} == {"Real Madrid", "Draw", "Al Hilal"}
        # Liquidity persisted as NUMERIC (Decimal at the boundary).
        assert all(r.liquidity is not None for r in rows)
        # Exactly one event row exists for this isolated-namespace external_ref.
        event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.external_ref == f"betfair:{url}")
        )
        assert event_count == 1
    # Re-capture with the SAME prices writes nothing new (change-gate + the
    # append-only unique key both hold) — visible within the same transaction.
    assert (await capture.capture_once())["soccer"] == 0


async def test_capture_does_not_graft_onto_live_event_with_same_url(factory) -> None:  # type: ignore[no-untyped-def]
    # Regression for the isolation breach the review caught: a live soccer event
    # and the Betfair capture share the SAME OddsPortal match URL. Events are
    # keyed by external_ref ALONE (globally unique, not sport-scoped), so without
    # the "betfair:" namespace the capture would REUSE the soccer event row and
    # its "Betfair Exchange" sharp BACK price would leak into that event closing
    # CLV anchor. This pins true event-level isolation.
    url = f"https://op/betfair-collision-{uuid4()}"
    teams = EventTeams(home="Real Madrid", away="Al Hilal", league="Club WC")

    soft = OddsSnapshotIn(
        event_id=url,
        bookmaker="bet365",
        market=Market.H2H,
        selection="Real Madrid",
        decimal_odds=2.0,
        captured_at=NOW,
        ingested_at=NOW,
    )
    await persist_odds_snapshots(
        factory, [soft], {url: teams}, sport="soccer", default_league="Club WC"
    )

    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(_LIVE_ROW_TOKENS))
    capture = BetfairExchangeCapture(
        reader,
        session_factory=factory,
        targets_fn=lambda sport: [_target(url)],
        sports=("soccer",),
        now_fn=lambda: NOW,
    )
    written = await capture.capture_once()
    assert written["soccer"] == 3
    async with factory() as session:
        soccer_books = (
            (
                await session.execute(
                    select(OddsSnapshot.bookmaker)
                    .join(Event, Event.id == OddsSnapshot.event_id)
                    .where(Event.external_ref == url)
                )
            )
            .scalars()
            .all()
        )
        assert set(soccer_books) == {"bet365"}
        assert BOOKMAKER not in soccer_books
        bf_books = (
            (
                await session.execute(
                    select(OddsSnapshot.bookmaker)
                    .join(Event, Event.id == OddsSnapshot.event_id)
                    .where(Event.external_ref == f"betfair:{url}")
                )
            )
            .scalars()
            .all()
        )
        assert set(bf_books) == {BOOKMAKER}
        assert len(bf_books) == 3
        live_event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.external_ref == url)
        )
        betfair_event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.external_ref == f"betfair:{url}")
        )
        assert live_event_count == 1
        assert betfair_event_count == 1


async def test_db_tests_leave_no_committed_pollution() -> None:
    # The isolation fixture rolls back, so a row a test wrote inside it must NOT
    # survive into a FRESH connection (a separate engine = a separate session,
    # outside the fixture's rolled-back transaction). This drives the same
    # capture against its OWN savepoint transaction, asserts the rows exist
    # within it, rolls back, then probes from a clean engine and asserts ZERO
    # leftover — exactly the cleanup the maintainer's one-time DELETE addresses
    # for pre-existing pollution.
    probe_engine = create_async_engine(DB_URL)
    try:
        async with probe_engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:  # noqa: BLE001
        await probe_engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")

    url = f"https://op/betfair-leak-{uuid4()}"
    betfair_ref = f"betfair:{url}"
    reader = BetfairExchangeReader(min_liquidity=0.0, page_loader=_static_loader(_LIVE_ROW_TOKENS))

    inner_engine = create_async_engine(DB_URL)
    try:
        async with inner_engine.connect() as conn:
            trans = await conn.begin()
            maker = async_sessionmaker(
                bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            capture = BetfairExchangeCapture(
                reader,
                session_factory=maker,
                targets_fn=lambda sport: [_target(url)],
                sports=("soccer",),
                now_fn=lambda: NOW,
            )
            assert (await capture.capture_once())["soccer"] == 3
            async with maker() as session:
                seen = await session.scalar(
                    select(func.count()).select_from(Event).where(Event.external_ref == betfair_ref)
                )
                assert seen == 1  # visible inside the transaction
            await trans.rollback()  # teardown: discard everything
    finally:
        await inner_engine.dispose()

    # Fresh engine = outside the rolled-back transaction: nothing must remain.
    try:
        async with async_sessionmaker(probe_engine, expire_on_commit=False)() as session:
            leftover_events = await session.scalar(
                select(func.count()).select_from(Event).where(Event.external_ref == betfair_ref)
            )
            leftover_snaps = await session.scalar(
                select(func.count())
                .select_from(OddsSnapshot)
                .join(Event, Event.id == OddsSnapshot.event_id)
                .where(Event.external_ref == betfair_ref)
            )
            assert leftover_events == 0
            assert leftover_snaps == 0
    finally:
        await probe_engine.dispose()


# --------------------------------------------------------------------------- #
# Review nits: parse_odds_value boundary contract + a DECIMAL basketball (2-way)
# extraction test (the per-visitor odds-format cookie applies to every sport).
# --------------------------------------------------------------------------- #

# A 2-way basketball moneyline rendered DECIMAL: BACK home/away 1.80/2.10 then
# LAY home/away, each + parenthesised £ liquidity. No draw cell.
_BASKETBALL_DECIMAL_HTML = _scroll_el(
    back=(_odd_cell("1.80", "(5000)") + _odd_cell("2.10", "(4200)") + _payout_cell("99.0%")),
    lay=(_odd_cell("1.83", "(900)") + _odd_cell("2.16", "(1200)") + _payout_cell("101.0%")),
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("6.51", 6.51),
        ("1.66", 1.66),
        ("100", 100.0),  # integer-format decimal (extreme underdog) is valid
        ("2", 2.0),
        ("28/25", 2.12),
        ("3/1", 4.0),
        ("0", None),
        ("1", None),  # <= 1.0 is not a backable price
        ("1.00", None),
        ("(1838)", None),  # a liquidity token must never parse as an odds value
        ("99.3%", None),  # a payout % must never parse as an odds value
        ("", None),
    ],
)
def test_parse_odds_value_decimal_and_fractional(raw: str, expected: "float | None") -> None:
    from app.ingestion.betfair_exchange import parse_odds_value

    result = parse_odds_value(raw)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=1e-9)


@pytest.mark.asyncio
async def test_row_extract_js_basketball_decimal() -> None:
    # The per-visitor odds-format cookie applies to basketball too: a 2-way
    # moneyline can render DECIMAL. With outcomes=("home","away") the reader keeps
    # the leading TWO BACK cells (1.80/2.10) and discards the LAY tail.
    tokens = await _evaluate_row_extract(_exchange_section_html(_BASKETBALL_DECIMAL_HTML))
    assert tokens == ["1.80", "(5000)", "2.10", "(4200)", "1.83", "(900)", "2.16", "(1200)"]
    cells = _pair_tokens(tokens)
    quotes = extract_back_quotes(cells, outcomes=("home", "away"), min_liquidity=0.0)
    assert [q.designation for q in quotes] == ["home", "away"]
    assert [q.decimal_odds for q in quotes] == [1.80, 2.10]
    assert [q.liquidity for q in quotes] == [5000.0, 4200.0]
