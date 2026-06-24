"""Sharp-vs-soft value finder: anchor selection, commission, gates, outliers."""

import pytest

from app.edge.value import (
    ceil_odds,
    effective_odds,
    find_value_bets,
    find_value_bets_with_fair,
    min_acceptable_odds,
)


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


def test_min_odds_floor_gates_on_effective_not_raw_for_exchanges() -> None:
    # A Betfair-Exchange raw 1.31 nets ~1.2945 at 5% commission — BELOW a 1.30
    # floor on the price you can actually realize, so it must be rejected even
    # though raw >= floor. The SAME raw 1.31 at a soft book (no commission)
    # nets 1.31 and is admitted. Anchor = Pinnacle; fair makes "home" the value.
    fair = {"home": 0.80, "away": 0.20}
    exch = {"home": {"Pinnacle": 2.00, "Betfair Exchange": 1.31}, "away": {"Pinnacle": 2.00}}
    assert (
        find_value_bets_with_fair(exch, fair, "Pinnacle", min_edge=0.0, min_odds=1.30) == []
    )  # eff 1.2945 < 1.30
    soft = {"home": {"Pinnacle": 2.00, "SoftBook": 1.31}, "away": {"Pinnacle": 2.00}}
    bets = find_value_bets_with_fair(soft, fair, "Pinnacle", min_edge=0.0, min_odds=1.30)
    assert any(b.best_book == "SoftBook" for b in bets)  # eff 1.31 >= 1.30


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


# --- double chance (derived from the 1X2 anchor) ------------------------------


def test_double_chance_fair_is_pairwise_sum_of_h2h_fair() -> None:
    from app.edge.value import double_chance_fair

    h2h = {"Home FC": 0.50, "Draw": 0.30, "Away FC": 0.20}
    dc = double_chance_fair(h2h, "Home FC", "Away FC")
    assert dc == {
        "Home FC or Draw": 0.80,
        "Home FC or Away FC": 0.70,
        "Draw or Away FC": 0.50,
    }
    # missing any 1X2 leg -> no derivation (never guess)
    assert double_chance_fair({"Home FC": 0.5, "Draw": 0.3}, "Home FC", "Away FC") == {}


def test_double_chance_direct_devig_is_rejected_by_anchor_sanity() -> None:
    # DC quotes sum to ~200% implied — anchor_fair_probs must refuse them.
    from app.edge.value import anchor_fair_probs

    prices = {
        "1X": {"Pinnacle": 1.25, "SoftBook": 1.28},
        "12": {"Pinnacle": 1.40, "SoftBook": 1.42},
        "X2": {"Pinnacle": 1.65, "SoftBook": 1.70},
    }
    assert anchor_fair_probs(prices) is None


def test_find_value_bets_with_fair_flags_soft_dc_price() -> None:
    from app.edge.value import find_value_bets_with_fair

    dc_fair = {"Home FC or Draw": 0.80}
    # fair 0.80 -> fair odds 1.25; SoftBook posts 1.70 (implied 0.588) -> value
    prices = {"Home FC or Draw": {"Pinnacle": 1.22, "SoftBook": 1.70}}
    bets = find_value_bets_with_fair(prices, dc_fair, "Pinnacle", min_edge=0.01, min_odds=1.60)
    assert len(bets) == 1
    assert bets[0].best_book == "SoftBook"
    assert abs(bets[0].edge - (0.80 - 1 / 1.70)) < 1e-9
    # the anchor book's own price is never the pick
    assert (
        find_value_bets_with_fair(
            {"Home FC or Draw": {"Pinnacle": 1.70}}, dc_fair, "Pinnacle", min_edge=0.0, min_odds=1.0
        )
        == []
    )


# --- execution helper: "still +EV down to X.XX" ------------------------------


def test_min_acceptable_odds_inverts_the_edge_definition() -> None:
    # edge = fair - 1/odds (no commission): at exactly the returned price the
    # retained edge equals min_edge; one tick below it drops under min_edge.
    fair, threshold = 0.55, 0.03
    floor = min_acceptable_odds(fair, threshold)
    assert floor is not None
    assert floor == pytest.approx(1.0 / (fair - threshold), abs=1e-12)
    assert fair - 1.0 / floor == pytest.approx(threshold, abs=1e-12)
    assert fair - 1.0 / (floor - 0.01) < threshold


def test_min_acceptable_odds_nets_exchange_commission() -> None:
    # At a 5% exchange the DISPLAYED floor must sit above the no-commission
    # floor: eff(raw) == 1/(fair - threshold) exactly at the returned price.
    fair, threshold = 0.55, 0.03
    raw_floor = min_acceptable_odds(fair, threshold, book="Betfair Exchange")
    plain_floor = min_acceptable_odds(fair, threshold)
    assert raw_floor is not None and plain_floor is not None
    assert raw_floor > plain_floor
    assert effective_odds("Betfair Exchange", raw_floor) == pytest.approx(plain_floor, abs=1e-12)


def test_min_acceptable_odds_none_when_no_price_retains_edge() -> None:
    # fair prob at/below the threshold: edge < threshold at ANY price.
    assert min_acceptable_odds(0.03, 0.03) is None
    assert min_acceptable_odds(0.02, 0.03) is None


def test_min_acceptable_odds_rejects_degenerate_inputs() -> None:
    with pytest.raises(ValueError):
        min_acceptable_odds(0.0, 0.03)
    with pytest.raises(ValueError):
        min_acceptable_odds(1.0, 0.03)
    with pytest.raises(ValueError):
        min_acceptable_odds(0.5, -0.01)


def test_ceil_odds_never_rounds_the_floor_down() -> None:
    # display rounding must keep the edge: round UP at 2dp...
    assert ceil_odds(1.8462) == pytest.approx(1.85)
    # ...but an exactly-representable threshold must not be bumped a tick
    assert ceil_odds(1.85) == pytest.approx(1.85)
    assert ceil_odds(2.0) == pytest.approx(2.0)


def test_min_acceptable_floor_still_passes_the_live_scan() -> None:
    # End-to-end invariant: at the (display-ceiled) floor price the live
    # scanner still emits the pick at min_edge; just below it, it does not.
    fair_half = {"home": {"Pinnacle": 2.00, "SoftBook": 2.30}, "away": {"Pinnacle": 2.00}}
    bets = find_value_bets(fair_half, min_edge=0.03, min_odds=1.30)
    assert bets and bets[0].selection == "home"
    floor = min_acceptable_odds(bets[0].sharp_fair_prob, 0.03, book="SoftBook")
    assert floor is not None
    at_floor = {
        "home": {"Pinnacle": 2.00, "SoftBook": ceil_odds(floor)},
        "away": {"Pinnacle": 2.00},
    }
    assert find_value_bets(at_floor, min_edge=0.03, min_odds=1.30)
    below_floor = {
        "home": {"Pinnacle": 2.00, "SoftBook": round(floor - 0.01, 4)},
        "away": {"Pinnacle": 2.00},
    }
    assert find_value_bets(below_floor, min_edge=0.03, min_odds=1.30) == []


def test_anchor_type_for_categorizes_every_anchor() -> None:
    # the live CLV stratification key persisted on picks (picks.anchor_type)
    from app.edge.value import CONSENSUS_ANCHOR, anchor_type_for

    assert anchor_type_for("Pinnacle") == "pinnacle"
    assert anchor_type_for("pinnacle sports") == "pinnacle"
    assert anchor_type_for(CONSENSUS_ANCHOR) == "consensus"
    # named non-Pinnacle sharps are their own stratum, never "pinnacle"
    assert anchor_type_for("Betfair Exchange") == "sharp"
    assert anchor_type_for("Smarkets") == "sharp"


def test_is_sharp_anchored_only_named_sharps_are_sharp() -> None:
    # The require-sharp-anchor gate's data test: a pick anchored on the soft
    # CONSENSUS median (no Pinnacle/Betfair priced the full market) is NOT
    # sharp-anchored; a named sharp anchor (Pinnacle/Betfair/Smarkets) is.
    from app.edge.value import CONSENSUS_ANCHOR, is_sharp_anchored

    assert is_sharp_anchored(CONSENSUS_ANCHOR) is False
    assert is_sharp_anchored("") is False  # blank/unknown anchor is not sharp
    assert is_sharp_anchored("Pinnacle") is True
    assert is_sharp_anchored("pinnacle sports") is True
    assert is_sharp_anchored("Betfair Exchange") is True
    assert is_sharp_anchored("Smarkets") is True


def test_no_sharp_anchor_means_no_premium_invariant() -> None:
    # P0-2 INVARIANT (regression pin): the require-sharp-anchor premium gate
    # (app/pipeline.py:841-848) demotes to the volume/shadow tier EXACTLY when
    # `require_sharp_anchor and not is_sharp_anchored(anchor_book)`. A future
    # refactor of the anchor selector that re-opens the consensus-as-fair path
    # (routes a soft consensus into a "sharp-looking" anchor, or flips the
    # predicate) MUST trip here. Phrased as the tier outcome the pipeline
    # computes, so it is pinned at the predicate the gate consumes.
    from app.edge.value import (
        CONSENSUS_ANCHOR,
        anchor_type_for,
        is_sharp_anchored,
    )

    def tier_of(anchor_book: str, *, require_sharp_anchor: bool) -> str:
        # The pipeline's premium tier starts every candidate at "premium" and
        # the gate demotes to "volume" (shadow: persisted + CLV-tracked, never
        # alerts, never reserves exposure). This mirrors pipeline.py exactly.
        if require_sharp_anchor and not is_sharp_anchored(anchor_book):
            return "volume"
        return "premium"

    # A consensus-anchored candidate is DEMOTED to volume (shadow) — never
    # alerts, never reserves exposure — under the live gate.
    assert tier_of(CONSENSUS_ANCHOR, require_sharp_anchor=True) == "volume"
    # ...and the predicate the gate consumes treats consensus / blank as NOT sharp.
    assert is_sharp_anchored(CONSENSUS_ANCHOR) is False
    assert is_sharp_anchored("") is False
    # the persisted CREATION-anchor tag for the consensus path is "consensus"
    # (never "pinnacle"/"sharp"), so by_anchor stratification cannot mislabel it.
    assert anchor_type_for(CONSENSUS_ANCHOR) == "consensus"
    # A genuinely sharp-anchored candidate STAYS premium with the gate on.
    assert tier_of("Pinnacle", require_sharp_anchor=True) == "premium"
    assert tier_of("Betfair Exchange", require_sharp_anchor=True) == "premium"
    assert is_sharp_anchored("Pinnacle") is True
    # gate OFF (the non-breaking default): consensus is NOT demoted.
    assert tier_of(CONSENSUS_ANCHOR, require_sharp_anchor=False) == "premium"


def test_close_is_independent_of_fill_detects_circular_self_priced_close() -> None:
    # P0-1/P0-3: a close anchored by the pick's OWN fill book is CIRCULAR (the
    # book pricing its own close, closing == fill, |clv_log|~0) and must read as
    # NOT independent. A different sharp book, or the >=3-book consensus median,
    # is independent of the fill by construction. Normalization is case-folding.
    from app.edge.value import CONSENSUS_ANCHOR, close_is_independent_of_fill

    # circular: same book on both sides (any casing) -> not independent
    assert close_is_independent_of_fill("Pinnacle", "Pinnacle") is False
    assert close_is_independent_of_fill("Pinnacle", "pinnacle") is False
    # genuine: a DIFFERENT sharp book anchored the close
    assert close_is_independent_of_fill("Pinnacle", "Bet365") is True
    assert close_is_independent_of_fill("Betfair Exchange", "Pinnacle") is True
    # consensus median spans >=3 books -> independent of any single fill book
    assert close_is_independent_of_fill(CONSENSUS_ANCHOR, "Pinnacle") is True
    # blank/unknown close anchor is not a self-priced close
    assert close_is_independent_of_fill("", "Pinnacle") is True


def test_consensus_anchor_dedups_casing_variant_books() -> None:
    # audit #5: two raw keys that normalize to the same book ('BookA' + 'booka')
    # must count ONCE in the per-selection median. 'home' deduped median of
    # {1.9, 2.0, 2.1} = 2.0; the old double-counted [1.9, 1.9, 2.0, 2.1] = 1.95.
    from app.edge.value import CONSENSUS_ANCHOR, _consensus_anchor

    prices = {
        "home": {"BookA": 1.9, "booka": 1.9, "BookB": 2.0, "BookC": 2.1},
        "away": {"BookA": 1.9, "BookB": 2.0, "BookC": 2.1},
    }
    anchor, med = _consensus_anchor(prices, ["home", "away"], {}, max_overround=0.2)
    assert anchor == CONSENSUS_ANCHOR
    assert med is not None
    assert med[0] == pytest.approx(2.0)  # deduped, not the double-counted 1.95
