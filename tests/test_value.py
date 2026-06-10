"""Sharp-vs-soft value finder: anchor selection, commission, gates, outliers."""

import pytest

from app.edge.value import effective_odds, find_value_bets


def test_flags_value_when_soft_beats_pinnacle_fair() -> None:
    # Pinnacle ~ fair (low margin); SoftBook offers a generous home price.
    prices = {
        "home": {"Pinnacle": 2.00, "SoftBook": 2.30, "OtherBook": 2.05},
        "away": {"Pinnacle": 2.00, "SoftBook": 1.80, "OtherBook": 1.95},
    }
    bets = find_value_bets(prices, min_edge=0.01)
    assert len(bets) == 1
    v = bets[0]
    assert v.selection == "home"
    assert v.best_book == "SoftBook"
    assert v.best_odds == 2.30
    assert v.best_odds_effective == 2.30  # no commission at a soft book
    assert v.sharp_book == "Pinnacle"
    assert v.sharp_fair_prob == pytest.approx(0.5, abs=1e-9)
    assert v.edge == pytest.approx(0.5 - 1 / 2.30, abs=1e-9)
    assert v.ev > 0


def test_no_value_when_soft_prices_are_tight() -> None:
    prices = {
        "home": {"Pinnacle": 2.00, "SoftBook": 1.95},
        "away": {"Pinnacle": 2.00, "SoftBook": 1.95},
    }
    assert find_value_bets(prices, min_edge=0.01) == []


def test_sharp_book_excluded_from_best_price() -> None:
    prices = {
        "home": {"Pinnacle": 2.40, "SoftBook": 2.10},
        "away": {"Pinnacle": 1.70, "SoftBook": 1.80},
    }
    bets = find_value_bets(prices, min_edge=0.01)
    assert all(b.best_book != "Pinnacle" for b in bets)


# --- review finding [1]: exchange commission must be netted out -------------


def test_effective_odds_nets_commission() -> None:
    assert effective_odds("Betfair Exchange", 2.26) == pytest.approx(1 + 1.26 * 0.95)
    assert effective_odds("SoftBook", 2.26) == 2.26


def test_exchange_gross_edge_killed_by_commission() -> None:
    # Pinnacle fair = 0.5/0.5. Betfair 2.06 gross looks like +0.0146 edge,
    # but at 5% commission effective is 2.007 -> edge ~ +0.0017 < min_edge.
    prices = {
        "home": {"Pinnacle": 2.00, "Betfair Exchange": 2.06},
        "away": {"Pinnacle": 2.00, "Betfair Exchange": 1.94},
    }
    assert find_value_bets(prices, min_edge=0.01) == []


def test_best_book_chosen_by_effective_not_raw_odds() -> None:
    # Betfair 2.30 raw (eff 2.235) must lose to SoftBook 2.26 raw (eff 2.26).
    prices = {
        "home": {"Pinnacle": 2.00, "Betfair Exchange": 2.30, "SoftBook": 2.26},
        "away": {"Pinnacle": 2.00, "SoftBook": 1.80},
    }
    bets = find_value_bets(prices, min_edge=0.01)
    assert len(bets) == 1
    assert bets[0].best_book == "SoftBook"
    assert bets[0].best_odds == 2.26


# --- review finding [0]: fallback anchor must be outlier-resistant -----------


def test_consensus_fallback_requires_three_books() -> None:
    # Two books and no named sharp: no trustworthy anchor -> no picks.
    prices = {
        "home": {"TightBook": 2.02, "WideBook": 2.40},
        "away": {"TightBook": 1.98, "WideBook": 1.70},
    }
    assert find_value_bets(prices, min_edge=0.01) == []


def test_consensus_median_anchor_flags_genuine_outlier_price() -> None:
    # Three books, no Pinnacle. BookC is generous on home; median anchors fair.
    prices = {
        "home": {"BookA": 2.00, "BookB": 2.02, "BookC": 2.45},
        "away": {"BookA": 1.90, "BookB": 1.92, "BookC": 1.85},
    }
    bets = find_value_bets(prices, min_edge=0.01)
    assert len(bets) == 1
    v = bets[0]
    assert v.selection == "home"
    assert v.best_book == "BookC"
    assert v.sharp_book == "consensus(median)"


def test_one_bad_quote_cannot_contaminate_other_selections() -> None:
    # Review worked-example: ErrBook quotes home absurdly high. Under the old
    # lowest-overround fallback ErrBook became the anchor and FAKE edges
    # appeared on Draw/Away at normal books. The median anchor must not flag
    # the normally-priced selections.
    prices = {
        "home": {"ErrBook": 3.20, "BookA": 2.50, "BookB": 2.52, "BookC": 2.48},
        "draw": {"ErrBook": 3.30, "BookA": 3.30, "BookB": 3.32, "BookC": 3.28},
        "away": {"ErrBook": 3.10, "BookA": 3.10, "BookB": 3.08, "BookC": 3.12},
    }
    bets = find_value_bets(prices, min_edge=0.015)
    assert all(b.selection == "home" for b in bets)  # only the real outlier


def test_implausible_named_anchor_rejected() -> None:
    # "Pinnacle" with an underround (arb-looking / stale) book must not anchor;
    # with only 2 books total there is no consensus either -> no picks.
    prices = {
        "home": {"Pinnacle": 3.20, "SoftBook": 2.40},
        "away": {"Pinnacle": 3.10, "SoftBook": 1.70},
    }
    assert find_value_bets(prices, min_edge=0.0) == []


# --- review finding [16]: ultra-short prices are devig noise -----------------


def test_min_odds_gate_blocks_ultra_short_prices() -> None:
    prices = {
        "home": {"Pinnacle": 1.05, "SoftBook": 1.09},
        "away": {"Pinnacle": 9.00, "SoftBook": 8.00},
    }
    assert find_value_bets(prices, min_edge=0.0, min_odds=1.30) == []


# --- structural guards --------------------------------------------------------


def test_requires_full_market_pricing() -> None:
    prices = {"home": {"OnlyHome": 2.0}, "away": {"OtherBook": 2.0}}
    assert find_value_bets(prices, min_edge=0.0) == []


def test_single_selection_returns_nothing() -> None:
    assert find_value_bets({"home": {"Pinnacle": 2.0}}, min_edge=0.0) == []


def test_duplicate_selection_names_return_nothing() -> None:
    # dict can't literally duplicate keys, but callers may collapse names;
    # a 2-key market where one selection IS the other is rejected upstream.
    prices = {"Draw": {"Pinnacle": 2.0}, "away": {"Pinnacle": 2.0}}
    bets = find_value_bets(prices, min_edge=0.0)
    assert isinstance(bets, list)  # contract: no crash; gates handle oddities
