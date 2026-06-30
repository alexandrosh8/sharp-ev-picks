"""Sharp-vs-soft value finder: anchor selection, commission, gates, outliers."""

import pytest

from app.edge.value import (
    anchor_fair_probs,
    ceil_odds,
    effective_odds,
    find_value_bets,
    find_value_bets_with_fair,
    min_acceptable_odds,
)
from app.probabilities.devig import DevigMethod, devig

# --- P2-1: commission is a payout cost, NOT a probability signal -------------


def test_exchange_anchor_fair_devigs_gross_not_commission_netted_odds() -> None:
    # P2-1 (RED before fix): the FAIR probability from a Betfair-Exchange sharp
    # anchor must devig the GROSS (displayed) odds. Netting commission BEFORE the
    # devig inflates the favourite's implied prob; the bias mostly cancels on
    # renormalisation but leaves a residual on asymmetric markets (here ~33 bps on
    # the favourite). Commission netting belongs ONLY on the bet-side price used
    # for edge/EV/CLV (tested separately), never on the fair-probability estimate.
    sels = ["Home", "Draw", "Away"]
    gross = [1.45, 4.60, 7.20]
    prices = {
        "Home": {"Betfair Exchange": 1.45, "SoftBook": 1.50},
        "Draw": {"Betfair Exchange": 4.60, "SoftBook": 4.40},
        "Away": {"Betfair Exchange": 7.20, "SoftBook": 7.00},
    }
    book, fair = anchor_fair_probs(prices, devig_method=DevigMethod.POWER)
    assert book == "Betfair Exchange"
    expected_gross = dict(zip(sels, devig(gross, method=DevigMethod.POWER), strict=True))
    for sel, p in expected_gross.items():
        assert fair[sel] == pytest.approx(p, abs=1e-12)
    # And it must NOT match the (buggy) commission-netted devig.
    net = [effective_odds("Betfair Exchange", o) for o in gross]
    net_fair = dict(zip(sels, devig(net, method=DevigMethod.POWER), strict=True))
    assert fair["Home"] != pytest.approx(net_fair["Home"], abs=1e-9)


def test_pinnacle_anchor_fair_unchanged_no_commission() -> None:
    # P2-1 regression lock: Pinnacle carries NO commission, so gross == net and
    # the fair is bit-identical to the raw-odds devig. The trusted subset's
    # Pinnacle-anchored picks are provably unchanged by the gross-devig fix.
    sels = ["Home", "Draw", "Away"]
    gross = [1.45, 4.60, 7.20]
    prices = {
        "Home": {"Pinnacle": 1.45, "SoftBook": 1.50},
        "Draw": {"Pinnacle": 4.60, "SoftBook": 4.40},
        "Away": {"Pinnacle": 7.20, "SoftBook": 7.00},
    }
    book, fair = anchor_fair_probs(prices, devig_method=DevigMethod.POWER)
    assert book == "Pinnacle"
    expected = dict(zip(sels, devig(gross, method=DevigMethod.POWER), strict=True))
    for sel, p in expected.items():
        assert fair[sel] == pytest.approx(p, abs=1e-12)


def test_overround_gate_stays_on_net_odds_membership_invariant() -> None:
    # P2-1 must NOT widen anchor membership: the overround plausibility gate stays
    # on NET (commission-aware) odds. This single-book market's NET overround
    # (~7.2%) exceeds a 6% cap though its GROSS overround (~4.6%) would pass — with
    # no consensus fallback (one book), the Betfair anchor must STILL be rejected,
    # exactly as before the gross-devig fix.
    prices = {
        "Home": {"Betfair Exchange": 1.45},
        "Draw": {"Betfair Exchange": 4.60},
        "Away": {"Betfair Exchange": 7.20},
    }
    assert anchor_fair_probs(prices, devig_method=DevigMethod.POWER, max_overround=0.06) is None


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


def test_max_edge_cap_rejects_implausible_data_error_edges() -> None:
    # fair(home)=0.80 vs a soft book offering 2.50 => edge 0.80 - 0.40 = +0.40
    # (40%): impossible value on a liquid market, the signature of a corrupted
    # anchor (the live 1X2-swap data error). Default (no cap) admits it; a 0.20
    # cap drops it as a data error so a feed defect can never mint a phantom pick.
    fair = {"home": 0.80, "away": 0.20}
    prices = {"home": {"Pinnacle": 2.50, "SoftBook": 2.50}, "away": {"Pinnacle": 5.0}}
    uncapped = find_value_bets_with_fair(prices, fair, "Pinnacle", min_edge=0.01, min_odds=1.30)
    assert any(b.selection == "home" and b.edge > 0.20 for b in uncapped)
    capped = find_value_bets_with_fair(
        prices, fair, "Pinnacle", min_edge=0.01, min_odds=1.30, max_edge=0.20
    )
    assert all(b.edge <= 0.20 for b in capped)
    assert not any(b.selection == "home" for b in capped)


def test_anchor_swap_vs_consensus_mints_zero_picks() -> None:
    # edge-ev-devig-r2-2: the named sharp anchor (Pinnacle) has its Draw and Away
    # prices SWAPPED relative to the cross-book consensus (the live 1X2-swap data
    # error). The swapped anchor makes Draw look fairly-priced at ~0.40 while soft
    # books offer Draw at 5.00 (~0.20) — a phantom +0.20 edge that sits UNDER any
    # sane per-leg max_edge cap. A market-level anchor-swap guard must cross-check
    # the anchor's devig ordering against the consensus and mint NOTHING for the
    # whole market when they disagree.
    prices = {
        "home": {"Pinnacle": 2.00, "SoftA": 2.00, "SoftB": 2.00, "SoftC": 2.00},
        "draw": {"Pinnacle": 2.50, "SoftA": 5.00, "SoftB": 5.00, "SoftC": 5.00},
        "away": {"Pinnacle": 5.00, "SoftA": 2.50, "SoftB": 2.50, "SoftC": 2.50},
    }
    assert find_value_bets(prices, min_edge=0.01) == []


def test_anchor_agreeing_with_consensus_still_mints() -> None:
    # Control: when the anchor's ordering AGREES with the consensus, the swap guard
    # is a no-op and a genuine soft-book value still mints (home generous at SoftA).
    prices = {
        "home": {"Pinnacle": 2.00, "SoftA": 2.30, "SoftB": 2.02, "SoftC": 2.01},
        "draw": {"Pinnacle": 5.00, "SoftA": 5.00, "SoftB": 5.00, "SoftC": 5.00},
        "away": {"Pinnacle": 2.50, "SoftA": 2.50, "SoftB": 2.50, "SoftC": 2.50},
    }
    bets = find_value_bets(prices, min_edge=0.01)
    assert any(b.selection == "home" and b.best_book == "SoftA" for b in bets)


def test_close_is_independent_when_anchor_books_differ_even_if_same_type() -> None:
    # CLV-3: a Smarkets-anchored PICK validated by a Betfair-exchange CLOSE is a
    # GENUINELY independent close — two DIFFERENT sharp books — even though both
    # collapse to anchor_type 'sharp'. Type-equality alone would wrongly call it
    # circular; with both anchor BOOKS known, book inequality wins.
    from app.edge.value import close_is_independent_of_fill

    assert (
        close_is_independent_of_fill(
            "Betfair Exchange",  # close anchor book
            "Bet365",  # fill book (soft) — not either sharp
            pick_anchor_type="sharp",
            close_anchor_type="sharp",
            pick_anchor_book="Smarkets",
        )
        is True
    )
    # same sharp BOOK on both sides is still circular even with books known
    assert (
        close_is_independent_of_fill(
            "Betfair Exchange",
            "Bet365",
            pick_anchor_type="sharp",
            close_anchor_type="sharp",
            pick_anchor_book="Betfair Exchange",
        )
        is False
    )
    # book unknown -> fall back to anchor-TYPE equality (back-compat)
    assert (
        close_is_independent_of_fill(
            "Pinnacle", "Bet365", pick_anchor_type="pinnacle", close_anchor_type="pinnacle"
        )
        is False
    )


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


def test_anchor_type_for_blank_and_soft_are_not_sharp() -> None:
    # Robustness/honesty pin: anchor_type_for must mint "sharp" ONLY for a
    # genuine SHARP_BOOKS member (or "pinnacle" for Pinnacle). A blank or a
    # SOFT bookmaker name (e.g. Bet365) is NOT sharp — it must fall through to
    # the not-trusted "consensus" bucket, so no refactored/future call site can
    # silently fake an honest-premium "sharp" tag from a soft/blank anchor. The
    # twin predicates must AGREE on the blank case (they previously contradicted:
    # anchor_type_for("")=="sharp" while is_sharp_anchored("") is False).
    from app.edge.value import anchor_type_for, is_sharp_anchored

    assert anchor_type_for("") != "sharp"
    assert anchor_type_for("Bet365") != "sharp"
    assert anchor_type_for("William Hill") != "sharp"
    # twins must agree on the blank/soft cases
    assert (anchor_type_for("") == "sharp") == is_sharp_anchored("")
    assert (anchor_type_for("Bet365") == "sharp") == is_sharp_anchored("Bet365")
    # genuine sharps are unaffected
    assert anchor_type_for("Betfair Exchange") == "sharp"
    assert anchor_type_for("Smarkets") == "sharp"
    assert anchor_type_for("Pinnacle") == "pinnacle"


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


def test_close_is_independent_of_fill_rejects_same_sharp_source_as_pick() -> None:
    # Audit finding #2: pick-time and close-time inject the SAME archived sharp line,
    # so a close from the SAME sharp SOURCE as the pick is circular (close_fair ~= pick
    # fair, |clv|~0 fake CLV). A trustworthy close must come from a DIFFERENT sharp
    # source than the one that anchored the pick.
    from app.edge.value import close_is_independent_of_fill

    # same sharp source (pick anchored Pinnacle, close also Pinnacle) -> circular
    assert (
        close_is_independent_of_fill(
            "Pinnacle", "Bet365", pick_anchor_type="pinnacle", close_anchor_type="pinnacle"
        )
        is False
    )
    # DIFFERENT sharp sources (pick Pinnacle, close Betfair/sharp) -> independent
    assert (
        close_is_independent_of_fill(
            "Betfair Exchange", "Bet365", pick_anchor_type="pinnacle", close_anchor_type="sharp"
        )
        is True
    )
    # a consensus-anchored PICK validated by a real sharp close -> independent
    assert (
        close_is_independent_of_fill(
            "Pinnacle", "Bet365", pick_anchor_type="consensus", close_anchor_type="pinnacle"
        )
        is True
    )
    # back-compat: with no anchor-type args, only the fill-book check applies
    assert close_is_independent_of_fill("Pinnacle", "Bet365") is True


def test_close_moved_from_pick_fair_gates_on_value_delta() -> None:
    # Audit 2026-06-28: the close is real CLV only if the line MOVED from the
    # pick-time fair. closing_fair == pick_fair is the identical-archived-line
    # TAUTOLOGY (clv_log re-encodes the pick-time edge), so it is NOT moved.
    from app.edge.value import CLV_TAUTOLOGY_EPS, close_moved_from_pick_fair

    assert CLV_TAUTOLOGY_EPS == 1e-3
    # genuine movement (Betfair 0.471 -> 0.516) -> moved
    assert close_moved_from_pick_fair(0.471, 0.516) is True
    # identical line (delta == 0) -> not moved (tautology)
    assert close_moved_from_pick_fair(0.45, 0.45) is False
    # de-minimis drift well within the epsilon -> still a tautology
    assert close_moved_from_pick_fair(0.45, 0.4505) is False
    # a clear move beyond the epsilon -> moved
    assert close_moved_from_pick_fair(0.45, 0.455) is True
    # unknowable pick-time fair cannot prove movement -> conservative False
    assert close_moved_from_pick_fair(None, 0.5) is False


def test_persisted_close_independent_recovers_same_book_moved_line() -> None:
    # P1 fix: independence is gated on the VALUE DELTA, not the anchor BOOK NAME.
    # A legitimate same-book MOVED-line close (a Pinnacle pick validated by a later,
    # MOVED Pinnacle close) is now INDEPENDENT — the old book-name same-source test
    # structurally dropped every Pinnacle-anchored pick (Pinnacle is SHARP_BOOKS[0]).
    from app.edge.value import persisted_close_independent

    # same SHARP source on both sides, but the line MOVED -> independent (recovered).
    # fill book is a SOFT book (Bet365), distinct from the Pinnacle close anchor.
    assert (
        persisted_close_independent(
            close_anchor_book="Pinnacle",
            fill_book="Bet365",
            pick_fair=0.471,
            closing_fair=0.516,
        )
        is True
    )


def test_persisted_close_independent_rejects_identical_line_tautology() -> None:
    # The identical-archived-line tautology (closing_fair == pick_fair) is CIRCULAR
    # even when anchored by a DIFFERENT book than the fill — the book test let it
    # through; the value-delta gate closes it.
    from app.edge.value import persisted_close_independent

    assert (
        persisted_close_independent(
            close_anchor_book="Pinnacle",
            fill_book="Bet365",
            pick_fair=0.50,
            closing_fair=0.5005,  # delta 0.0005 <= 1e-3 -> tautology
        )
        is False
    )


def test_persisted_close_independent_keeps_fill_book_circular() -> None:
    # The existing fill-book check is KEPT: a close anchored by the pick's OWN fill
    # book is circular (the book pricing its own close) even if the line moved.
    from app.edge.value import persisted_close_independent

    assert (
        persisted_close_independent(
            close_anchor_book="Bet365",
            fill_book="Bet365",
            pick_fair=0.40,
            closing_fair=0.52,  # moved, but fill book priced its own close
        )
        is False
    )


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


def test_logit_pool_consensus_matches_standard_on_identical_books() -> None:
    # build #1: zero cross-book spread -> the logit pool collapses to the same fair
    # as the median-of-prices consensus. Proves no regression on the default path.
    from app.edge.value import CONSENSUS_ANCHOR, anchor_fair_probs

    prices = {
        "H": {"B1": 1.50, "B2": 1.50, "B3": 1.50},
        "D": {"B1": 4.00, "B2": 4.00, "B3": 4.00},
        "A": {"B1": 6.50, "B2": 6.50, "B3": 6.50},
    }
    std = anchor_fair_probs(prices, consensus_logit_pool=False)
    lp = anchor_fair_probs(prices, consensus_logit_pool=True)
    assert std is not None and lp is not None
    assert std[0] == lp[0] == CONSENSUS_ANCHOR
    for s in prices:
        assert lp[1][s] == pytest.approx(std[1][s], abs=1e-9)


def test_logit_pool_consensus_is_valid_and_differs_on_spread() -> None:
    # build #1: with cross-book spread on a favourite the logit (geometric) pool is a
    # valid, order-preserving distribution that genuinely differs from the linear
    # median-of-prices consensus — it does not lose tail sharpness (Gneiting-Ranjan).
    from app.edge.value import anchor_fair_probs

    prices = {
        "H": {"B1": 1.45, "B2": 1.50, "B3": 1.40},
        "D": {"B1": 4.20, "B2": 4.00, "B3": 4.50},
        "A": {"B1": 7.00, "B2": 6.50, "B3": 7.50},
    }
    std = anchor_fair_probs(prices, consensus_logit_pool=False)
    lp = anchor_fair_probs(prices, consensus_logit_pool=True)
    assert std is not None and lp is not None
    fair = lp[1]
    assert sum(fair.values()) == pytest.approx(1.0, abs=1e-9)
    assert fair["H"] > fair["D"] > fair["A"]  # order preserved
    assert all(0.0 < fair[s] < 1.0 for s in prices)
    assert any(abs(fair[s] - std[1][s]) > 1e-4 for s in prices)  # genuinely different pool


def test_logit_consensus_anchor_returns_novig_order_preserving_prices() -> None:
    # build #1: the private pool returns SYNTHETIC prices that are (a) all > 1.0
    # (so the shared downstream devig accepts them), (b) no-vig by construction
    # (overround ~ 0), and (c) order-preserving (favourite cheapest). Exercises a
    # strongly skewed favourite where a linear price mean would under-confidence.
    from app.edge.value import (
        CONSENSUS_ANCHOR,
        _logit_consensus_anchor,
        _overround,
    )
    from app.probabilities.devig import DevigMethod

    prices = {
        "H": {"B1": 1.20, "B2": 1.22, "B3": 1.18},
        "D": {"B1": 7.00, "B2": 6.50, "B3": 7.50},
        "A": {"B1": 15.0, "B2": 14.0, "B3": 16.0},
    }
    selections = list(prices.keys())
    anchor, synth = _logit_consensus_anchor(
        prices, selections, {}, 0.12, DevigMethod.MULTIPLICATIVE
    )
    assert anchor == CONSENSUS_ANCHOR
    assert synth is not None
    assert all(o > 1.0 for o in synth)  # every synthetic price is bettable-shaped
    assert _overround(synth) == pytest.approx(0.0, abs=1e-9)  # no-vig by construction
    # cheapest synthetic price == biggest implied prob == the favourite
    assert synth[0] < synth[1] < synth[2]  # H favourite, A longshot — order preserved


_EX_PRICES = {
    "H": {"betfair exchange": 1.50, "SoftA": 1.55, "SoftB": 1.52},
    "D": {"betfair exchange": 4.00, "SoftA": 4.10, "SoftB": 4.05},
    "A": {"betfair exchange": 7.00, "SoftA": 6.80, "SoftB": 6.90},
}


def test_exchange_liquidity_gate_off_by_default_keeps_exchange_anchor() -> None:
    # build #3: floor 0 (default) -> the Betfair anchor is chosen, no liquidity needed.
    from app.edge.value import anchor_fair_probs

    res = anchor_fair_probs(_EX_PRICES)
    assert res is not None and res[0] == "betfair exchange"


def test_exchange_liquidity_gate_demotes_thin_or_unknown_liquidity() -> None:
    # build #3: floor > 0 + liquidity below floor (or absent) -> Betfair does NOT
    # earn 'sharp'; the anchor falls back to the consensus median.
    from app.edge.value import CONSENSUS_ANCHOR, anchor_fair_probs

    thin = {s: {"betfair exchange": 5.0} for s in _EX_PRICES}  # below a 100 floor
    res = anchor_fair_probs(_EX_PRICES, liquidity=thin, exchange_min_liquidity=100.0)
    assert res is not None and res[0] == CONSENSUS_ANCHOR
    # unknown liquidity (no map) also demotes the exchange under a positive floor
    res2 = anchor_fair_probs(_EX_PRICES, exchange_min_liquidity=100.0)
    assert res2 is not None and res2[0] == CONSENSUS_ANCHOR


def test_exchange_liquidity_gate_keeps_liquid_exchange() -> None:
    # build #3: liquidity >= floor on every selection -> Betfair stays the sharp anchor.
    from app.edge.value import anchor_fair_probs

    liquid = {s: {"betfair exchange": 5000.0} for s in _EX_PRICES}
    res = anchor_fair_probs(_EX_PRICES, liquidity=liquid, exchange_min_liquidity=100.0)
    assert res is not None and res[0] == "betfair exchange"


def test_liquidity_gate_exempts_pinnacle() -> None:
    # build #3: Pinnacle is fixed-odds (not an exchange) -> the liquidity floor never
    # applies; it stays the anchor regardless of any floor.
    from app.edge.value import anchor_fair_probs

    pinn = {
        "H": {"pinnacle": 1.50, "SoftA": 1.55, "SoftB": 1.52},
        "D": {"pinnacle": 4.00, "SoftA": 4.10, "SoftB": 4.05},
        "A": {"pinnacle": 7.00, "SoftA": 6.80, "SoftB": 6.90},
    }
    res = anchor_fair_probs(pinn, exchange_min_liquidity=100.0)  # no liquidity map
    assert res is not None and res[0] == "pinnacle"
