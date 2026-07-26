"""Sub-reason telemetry for the no-sharp-anchor gate (telemetry finding, 2026-07-26).

'no_sharp_anchor' was 99.8% of gate reasons and hid a 13-day regression (the
PR #164 exchange liquidity-floor NULL rejection): the single label could not
distinguish "no sharp book prices this market at all" from "every sharp
candidate was REJECTED by the liquidity floor / staleness demotion / overround
gate". The named-anchor loop now reports WHICH guard exhausted the sharp
candidates, and the pipeline emits it as a 'no_sharp_anchor:<cause>' slug
ALONGSIDE the legacy 'no_sharp_anchor' (dashboards keep matching the bare
label). Pure layer only here — no DB, no network.
"""

from app.edge.value import (
    SHARP_MISS_EXCHANGE_DEMOTED,
    SHARP_MISS_LIQUIDITY_FLOOR,
    SHARP_MISS_NO_FULL_MARKET,
    SHARP_MISS_OVERROUND,
    _named_sharp_anchor,
    anchor_fair_probs_with_provenance,
)
from app.probabilities.devig import DevigMethod
from app.schemas.base import Market

_COMMISSIONS = {"betfair exchange": 0.05}
_SELECTIONS = ["Home FC", "Draw", "Away FC"]

_SHARP_PRICES = {
    "Home FC": {"Betfair Exchange": 2.50, "Pinnacle": 2.48, "SoftA": 2.90},
    "Draw": {"Betfair Exchange": 3.30, "Pinnacle": 3.28, "SoftA": 3.20},
    "Away FC": {"Betfair Exchange": 3.10, "Pinnacle": 3.08, "SoftA": 2.95},
}

_SOFT_ONLY_PRICES = {
    "Home FC": {"SoftA": 2.45, "SoftB": 2.50, "SoftC": 2.95},
    "Draw": {"SoftA": 3.30, "SoftB": 3.25, "SoftC": 3.20},
    "Away FC": {"SoftA": 3.10, "SoftB": 3.05, "SoftC": 2.95},
}


def test_success_returns_no_miss_reason() -> None:
    book, odds, miss = _named_sharp_anchor(
        _SHARP_PRICES, _SELECTIONS, ("pinnacle",), _COMMISSIONS, 0.12
    )
    assert book == "Pinnacle"
    assert odds == [2.48, 3.28, 3.08]
    assert miss is None


def test_no_sharp_book_at_all_reports_no_full_market() -> None:
    book, odds, miss = _named_sharp_anchor(
        _SOFT_ONLY_PRICES, _SELECTIONS, ("pinnacle", "betfair exchange"), _COMMISSIONS, 0.12
    )
    assert book is None
    assert miss == SHARP_MISS_NO_FULL_MARKET


def test_partial_sharp_coverage_reports_no_full_market() -> None:
    # Pinnacle prices ONE leg only — an incomplete book is "does not price the
    # full market", not a rejection by any guard.
    prices = {
        "Home FC": {"Pinnacle": 2.48, "SoftA": 2.90},
        "Draw": {"SoftA": 3.20},
        "Away FC": {"SoftA": 2.95},
    }
    book, _odds, miss = _named_sharp_anchor(prices, _SELECTIONS, ("pinnacle",), _COMMISSIONS, 0.12)
    assert book is None
    assert miss == SHARP_MISS_NO_FULL_MARKET


def test_liquidity_floor_rejection_reports_exchange_liquidity_floor() -> None:
    # The PR #164 regression shape: the exchange PRICES the full market but a
    # known-thin selection trips the liquidity floor; no other sharp book.
    liquidity = {"Home FC": {"betfair exchange": 5.0}}
    book, _odds, miss = _named_sharp_anchor(
        _SHARP_PRICES,
        _SELECTIONS,
        ("betfair exchange",),
        _COMMISSIONS,
        0.12,
        liquidity=liquidity,
        exchange_min_liquidity=50.0,
    )
    assert book is None
    assert miss == SHARP_MISS_LIQUIDITY_FLOOR


def test_staleness_demotion_reports_exchange_demoted() -> None:
    book, _odds, miss = _named_sharp_anchor(
        _SHARP_PRICES,
        _SELECTIONS,
        ("betfair exchange",),
        _COMMISSIONS,
        0.12,
        exchange_demoted=True,
    )
    assert book is None
    assert miss == SHARP_MISS_EXCHANGE_DEMOTED


def test_implausible_overround_reports_overround_implausible() -> None:
    # Pinnacle prices the full market but at an implausible overround.
    prices = {
        "Home FC": {"Pinnacle": 1.50, "SoftA": 2.90},
        "Draw": {"Pinnacle": 2.00, "SoftA": 3.20},
        "Away FC": {"Pinnacle": 2.00, "SoftA": 2.95},
    }
    book, _odds, miss = _named_sharp_anchor(prices, _SELECTIONS, ("pinnacle",), {}, 0.12)
    assert book is None
    assert miss == SHARP_MISS_OVERROUND


def test_specific_rejection_wins_over_incomplete_books() -> None:
    # Exchange rejected by the floor + Pinnacle absent: the SPECIFIC guard
    # rejection names the cause (that is the signal a cliff regression needs),
    # never the generic "no full market" of the absent book.
    prices = {
        "Home FC": {"Betfair Exchange": 2.50, "SoftA": 2.90},
        "Draw": {"Betfair Exchange": 3.30, "SoftA": 3.20},
        "Away FC": {"Betfair Exchange": 3.10, "SoftA": 2.95},
    }
    liquidity = {"Draw": {"betfair exchange": 1.0}}
    book, _odds, miss = _named_sharp_anchor(
        prices,
        _SELECTIONS,
        ("betfair exchange", "pinnacle"),
        _COMMISSIONS,
        0.12,
        liquidity=liquidity,
        exchange_min_liquidity=50.0,
    )
    assert book is None
    assert miss == SHARP_MISS_LIQUIDITY_FLOOR


def test_anchor_fair_probs_populates_sharp_miss_out_on_consensus_fallback() -> None:
    miss_out: list[str] = []
    result = anchor_fair_probs_with_provenance(_SOFT_ONLY_PRICES, sharp_miss_out=miss_out)
    assert result is not None
    anchor_book, _fair, _fell_back = result
    assert anchor_book == "consensus(median)"
    assert miss_out == [SHARP_MISS_NO_FULL_MARKET]


def test_anchor_fair_probs_leaves_sharp_miss_out_empty_on_sharp_anchor() -> None:
    miss_out: list[str] = []
    result = anchor_fair_probs_with_provenance(_SHARP_PRICES, sharp_miss_out=miss_out)
    assert result is not None
    assert miss_out == []


def test_event_fair_probs_reports_miss_reason_per_market() -> None:
    from datetime import datetime

    from app.pipeline import event_fair_probs

    _Captured = dict[tuple[str, str], datetime]
    _Grouped = dict[tuple[str, Market, str | None], tuple[dict[str, dict[str, float]], _Captured]]
    grouped: _Grouped = {
        ("ev-soft", Market.H2H, None): (_SOFT_ONLY_PRICES, {}),
        ("ev-sharp", Market.H2H, None): (_SHARP_PRICES, {}),
    }
    miss_out: dict[tuple[str, Market, str | None], str] = {}
    out = event_fair_probs(grouped, DevigMethod.POWER, sharp_miss_out=miss_out)
    assert ("ev-soft", Market.H2H, None) in out
    assert miss_out == {("ev-soft", Market.H2H, None): SHARP_MISS_NO_FULL_MARKET}
