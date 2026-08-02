"""P2-1: headline min-n suppression in _aggregate_settled (pure helper).

The blended headline roi / beat_close_rate / stake-weighted CLV had no min-n
guard — a 10-pick -8.7% read as signal. Below MIN_HEADLINE_N the point
estimates are nulled at the source and flagged roi_status="insufficient"; the
honest denominators (n_settled, counts, totals) survive. The trusted sharp
subset is gated independently on its own n (n_sharp_close).

Pure: _aggregate_settled takes plain row tuples, so no DB is needed.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.storage.repositories import (
    MIN_HEADLINE_N,
    _aggregate_settled,
    _aggregate_settled_by_sport,
)


def _row(
    outcome: str = "won",
    pnl: float = 1.0,
    stake: float = 10.0,
    clv_log: float | None = 0.02,
    beat_close: bool | None = True,
    closing_odds: float | None = 2.0,
    closing_anchor: str | None = "pinnacle",
    close_independent: bool | None = True,
    has_snapshot_close: bool | None = True,
    decimal_odds: float | None = 2.0,
    closing_fair_probability: float | None = None,
    model_probability: float | None = None,
    mint_devig_fell_back: bool | None = None,
    close_devig_fell_back: bool | None = None,
    bookmaker: str | None = None,
) -> tuple[object, ...]:
    # (outcome, pnl, stake, clv_log, beat_close, closing_odds, closing_anchor,
    #  close_independent, has_snapshot_close, decimal_odds,
    #  closing_fair_probability, model_probability, mint_devig_fell_back,
    #  close_devig_fell_back) — the tuple shape performance_report._tier_rows
    #  builds. decimal_odds + closing_fair_probability feed the CLV-1 implausible
    #  close-implied-edge guard; closing_fair_probability + model_probability feed
    #  the TAUTOLOGY guard; the two trailing P2-2 flags default None (symmetric).
    return (
        outcome,
        Decimal(str(pnl)),
        Decimal(str(stake)),
        Decimal(str(clv_log)) if clv_log is not None else None,
        beat_close,
        Decimal(str(closing_odds)) if closing_odds is not None else None,
        closing_anchor,
        close_independent,
        has_snapshot_close,
        Decimal(str(decimal_odds)) if decimal_odds is not None else None,
        Decimal(str(closing_fair_probability)) if closing_fair_probability is not None else None,
        Decimal(str(model_probability)) if model_probability is not None else None,
        mint_devig_fell_back,
        close_devig_fell_back,
        None,  # close_snapshot_captured_at (D3, unused here)
        None,  # kickoff (D4, unused here)
        None,  # close_exclusion_reason (A4, unused here)
        bookmaker,  # fill book — effective-odds CLV-1 guard input
    )


def test_headline_suppressed_below_min_n() -> None:
    # 10 settled picks (< MIN_HEADLINE_N=50): a -8.7% ROI here is NOISE. The
    # numeric roi/beat_close_rate/CLV are nulled and flagged insufficient.
    agg = _aggregate_settled([_row(outcome="lost", pnl=-0.87) for _ in range(10)])
    assert agg["n_settled"] == 10
    assert agg["roi_status"] == "insufficient"
    assert agg["roi"] is None  # numeric headline suppressed at the source
    assert agg["stake_weighted_clv_log"] is None
    assert agg["beat_close_rate"] is None
    # honest denominators survive so the dashboard can render the "n too small" state
    assert agg["lost"] == 10
    assert Decimal(agg["total_staked"]) == Decimal("100")  # 10 * 10.0 stake
    assert agg["min_headline_n"] == MIN_HEADLINE_N


def test_headline_reported_at_or_above_min_n() -> None:
    # Exactly MIN_HEADLINE_N settled picks: the headline is now trustworthy
    # enough to report — roi_status flips to "ok" and the numeric roi appears.
    agg = _aggregate_settled([_row(outcome="won", pnl=1.0) for _ in range(MIN_HEADLINE_N)])
    assert agg["n_settled"] == MIN_HEADLINE_N
    assert agg["roi_status"] == "ok"
    assert agg["roi"] is not None
    assert agg["roi"] == "0.1"  # 50 * 1.0 pnl / (50 * 10.0 staked)
    assert agg["stake_weighted_clv_log"] is not None


def test_sharp_only_close_with_no_soft_price_enters_trusted_subset() -> None:
    # clv-1: a close anchored by a sharp book that NO soft book quoted has
    # closing_odds=None (there is no soft display price), yet it is a GENUINE
    # snapshot close (has_snapshot_close=True). Gating on closing_odds wrongly
    # excluded it; gating on has_snapshot_close admits it to the trusted subset.
    rows = [
        _row(closing_odds=None, has_snapshot_close=True, clv_log=0.04, close_independent=True)
        for _ in range(MIN_HEADLINE_N)
    ]
    agg = _aggregate_settled(rows)
    assert agg["n_sharp_close"] == MIN_HEADLINE_N  # sharp-only closes counted
    assert agg["sharp_status"] == "ok"
    assert agg["sharp_stake_weighted_clv_log"] == "0.04"


def test_asymmetric_devig_fallback_excluded_from_trusted_subset() -> None:
    # P2-2: a genuine independent sharp snapshot close whose MINT devig fell back
    # to multiplicative but whose CLOSE devig did NOT (asymmetric) is a
    # devig-method artifact, not a real line move — excluded from n_sharp_close.
    rows = [
        _row(clv_log=0.04, mint_devig_fell_back=True, close_devig_fell_back=False)
        for _ in range(MIN_HEADLINE_N)
    ]
    agg = _aggregate_settled(rows)
    assert agg["n_sharp_close"] == 0


def test_symmetric_devig_fallback_stays_in_trusted_subset() -> None:
    # Both sides fell back the SAME way (or both None): the fairs were devigged by
    # the same effective method, so the CLV is honest — kept in the trusted subset.
    both_fell = [
        _row(clv_log=0.04, mint_devig_fell_back=True, close_devig_fell_back=True)
        for _ in range(MIN_HEADLINE_N)
    ]
    assert _aggregate_settled(both_fell)["n_sharp_close"] == MIN_HEADLINE_N
    # NULL provenance (historical rows) is treated as symmetric — not excluded.
    unknown = [_row(clv_log=0.04) for _ in range(MIN_HEADLINE_N)]
    assert _aggregate_settled(unknown)["n_sharp_close"] == MIN_HEADLINE_N


def test_revalidation_fallback_close_excluded_even_with_soft_price() -> None:
    # clv-1 converse: a poll-time revalidation FALLBACK close (no snapshot close,
    # has_snapshot_close=False) is NOT trusted even though a soft closing_odds
    # exists — closing_odds is now purely a display price, not the gate.
    rows = [_row(closing_odds=2.0, has_snapshot_close=False) for _ in range(MIN_HEADLINE_N)]
    agg = _aggregate_settled(rows)
    assert agg["n_sharp_close"] == 0


def test_sharp_tagged_but_self_priced_close_excluded_from_trusted_subset() -> None:
    # WELWALO REGRESSION PIN (data-integrity, lever-2 safe subset): a close that
    # is anchor_type='sharp' but was priced by the pick's OWN source
    # (close_independent_of_fill=False — circular, closing==fill, |clv_log|~0) is
    # FAKE CLV. It must NEVER enter the trusted sharp subset even though it is a
    # genuine snapshot close (has_snapshot_close=True) carrying a sharp anchor tag.
    # This is exactly the obscure-league self-priced 'sharp' case (e.g. an
    # Ethiopian-league pick with no real Pinnacle/Betfair close): the tag exists
    # but the close cannot be trusted. Existing behavior is correct; this pins it
    # so a future refactor of the gate cannot silently re-admit the fake and
    # inflate n_sharp / the proof-of-edge headline.
    fakes = [
        _row(closing_anchor="sharp", has_snapshot_close=True, close_independent=False)
        for _ in range(MIN_HEADLINE_N)
    ]
    agg_fakes = _aggregate_settled(fakes)
    assert agg_fakes["n_sharp_close"] == 0  # circular self-priced closes never counted
    assert agg_fakes["sharp_status"] == "insufficient"

    # CONVERSE (lever-2 test 3): a GENUINE independent sharp close IS counted, so
    # the exclusion is targeted at circularity, not at the 'sharp' tag itself.
    genuine = [
        _row(closing_anchor="sharp", has_snapshot_close=True, close_independent=True)
        for _ in range(MIN_HEADLINE_N)
    ]
    agg_genuine = _aggregate_settled(genuine)
    assert agg_genuine["n_sharp_close"] == MIN_HEADLINE_N
    assert agg_genuine["sharp_status"] == "ok"


def test_blended_clv_excludes_implausible_close_implied_edge() -> None:
    # CLV-1: a settled pick whose close-implied edge is physically impossible
    # (closing_fair_probability - 1/decimal_odds = 0.891433 - 1/6.50 = 0.737 >>
    # value_max_edge=0.20) is the favorite-prob-on-underdog-leg residue of the
    # since-fixed double-chance orientation bug. Its outcome/pnl stay counted (it
    # IS a real settled pick) but its fabricated clv_log (1.756877) and beat_close
    # must NOT inflate the blended stake_weighted_clv_log / beat_close_rate.
    honest = [
        _row(
            outcome="lost",
            pnl=0.0,
            clv_log=0.0,
            beat_close=False,
            decimal_odds=2.0,
            closing_fair_probability=0.50,
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    poison = _row(
        outcome="won",
        pnl=5.5,
        stake=10.0,
        clv_log=1.756877,
        beat_close=True,
        decimal_odds=6.50,
        closing_fair_probability=0.891433,
    )
    agg = _aggregate_settled(honest + [poison])
    # The poison row survives as a real settled pick (honest denominator).
    assert agg["n_settled"] == MIN_HEADLINE_N + 1
    assert agg["won"] == 1
    assert Decimal(agg["total_pnl"]) == Decimal("5.5")
    # ... but its fabricated CLV/beat_close are dropped: only the 50 honest zeros
    # remain, so the blended headline reads 0, not a flattered positive.
    assert agg["stake_weighted_clv_log"] == "0"
    assert agg["beat_close_rate"] == "0"


def test_implausible_clv_excluded_from_trusted_sharp_subset() -> None:
    # Defense-in-depth: even a sharp-anchored, independent, genuine-snapshot close
    # cannot enter the trusted sharp subset if its close-implied edge is impossible
    # — fabricated CLV must never reach the proof-of-edge headline by any path.
    honest = [
        _row(
            closing_anchor="pinnacle",
            clv_log=0.03,
            decimal_odds=2.0,
            closing_fair_probability=0.51,
            close_independent=True,
            has_snapshot_close=True,
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    poison = _row(
        closing_anchor="pinnacle",
        clv_log=1.5,
        decimal_odds=6.50,
        closing_fair_probability=0.891433,
        close_independent=True,
        has_snapshot_close=True,
    )
    agg = _aggregate_settled(honest + [poison])
    # 50 honest sharp closes counted; the impossible one excluded.
    assert agg["n_sharp_close"] == MIN_HEADLINE_N
    assert agg["sharp_stake_weighted_clv_log"] == "0.03"


def test_clv_log_fallback_excludes_when_close_edge_uncomputable() -> None:
    # Fallback path: when closing_fair_probability is absent the close-implied edge
    # cannot be computed, so an implausibly large |clv_log| (1.76) is itself the
    # tripwire. A genuinely small clv_log with no fair prob is kept.
    big = _aggregate_settled(
        [_row(clv_log=1.76, closing_fair_probability=None) for _ in range(MIN_HEADLINE_N)]
    )
    assert big["stake_weighted_clv_log"] == "0" or big["stake_weighted_clv_log"] is None
    assert big["n_sharp_close"] == 0  # all fabricated -> none trusted

    ok = _aggregate_settled(
        [_row(clv_log=0.05, closing_fair_probability=None) for _ in range(MIN_HEADLINE_N)]
    )
    assert ok["stake_weighted_clv_log"] == "0.05"
    assert ok["n_sharp_close"] == MIN_HEADLINE_N


def test_plausible_large_clv_is_not_excluded() -> None:
    # Converse guard: a row with a real, in-bounds close-implied edge (0.55 - 1/2.0
    # = 0.05 <= 0.20) and a normal clv_log is NOT touched — the guard targets
    # impossibility, not merely positive CLV.
    rows = [
        _row(clv_log=0.10, decimal_odds=2.0, closing_fair_probability=0.55)
        for _ in range(MIN_HEADLINE_N)
    ]
    agg = _aggregate_settled(rows)
    assert agg["stake_weighted_clv_log"] == "0.1"


def test_by_sport_split_aggregates_and_suppresses_per_sport() -> None:
    # PER-SPORT split: soccer has enough settled picks to report a headline ROI;
    # basketball has only 3 — gated on its OWN n (MIN_HEADLINE_N), so it reads
    # insufficient. A thin/experimental sport can never borrow soccer's n.
    soccer = [("soccer", _row(outcome="won", pnl=1.0)) for _ in range(MIN_HEADLINE_N)]
    basketball = [("basketball", _row(outcome="lost", pnl=-0.5)) for _ in range(3)]
    by_sport = _aggregate_settled_by_sport(soccer + basketball)
    assert set(by_sport) == {"soccer", "basketball"}
    assert by_sport["soccer"]["roi_status"] == "ok"
    assert by_sport["soccer"]["roi"] is not None
    assert by_sport["basketball"]["roi_status"] == "insufficient"
    assert by_sport["basketball"]["roi"] is None  # nulled at the source
    assert by_sport["basketball"]["n_settled"] == 3  # honest denominator survives


def test_by_sport_split_empty_when_no_rows() -> None:
    assert _aggregate_settled_by_sport([]) == {}


def test_sharp_clv_odds_split_partitions_trusted_subset() -> None:
    # Live review 2026-08-02 (item 1): the trusted subset splits at the 4.0
    # odds threshold — the decision-relevant partition (the >= 4.0 tail is
    # where negative trusted CLV concentrated). Bands partition n_sharp_close.
    rows = [_row(clv_log=0.02, decimal_odds=2.0) for _ in range(MIN_HEADLINE_N)] + [
        _row(clv_log=-0.10, decimal_odds=4.5) for _ in range(MIN_HEADLINE_N)
    ]
    agg = _aggregate_settled(rows)
    split = agg["sharp_clv_odds_split"]
    assert split["threshold_odds"] == 4.0
    assert split["below"]["n"] == MIN_HEADLINE_N
    assert split["at_or_above"]["n"] == MIN_HEADLINE_N
    assert split["n_unknown_odds"] == 0
    assert split["below"]["n"] + split["at_or_above"]["n"] == agg["n_sharp_close"]
    assert split["below"]["status"] == "ok"
    assert split["below"]["stake_weighted_clv_log"] == "0.02"
    assert split["at_or_above"]["stake_weighted_clv_log"] == "-0.1"
    # at/above the floor the CI fields are populated for the dashboard's
    # shared ciEntryText renderer
    assert split["below"]["mean_clv_log"] is not None
    assert split["below"]["ci_low"] is not None
    assert split["below"]["ci_high"] is not None


def test_sharp_clv_odds_split_boundary_min_n_nulling_and_unknown_odds() -> None:
    # Exactly-threshold odds land in the tail band; a sub-floor band carries
    # ONLY its n (estimates nulled at the source, status "insufficient"); a
    # trusted row with unusable odds enters neither band (counted unknown).
    rows = [_row(clv_log=0.03, decimal_odds=4.0) for _ in range(10)] + [
        _row(clv_log=0.01, decimal_odds=None) for _ in range(5)
    ]
    split = _aggregate_settled(rows)["sharp_clv_odds_split"]
    assert split["at_or_above"]["n"] == 10  # boundary 4.0 -> tail
    assert split["below"]["n"] == 0
    assert split["n_unknown_odds"] == 5
    assert split["at_or_above"]["status"] == "insufficient"
    assert split["at_or_above"]["stake_weighted_clv_log"] is None
    assert split["at_or_above"]["mean_clv_log"] is None
    assert split["at_or_above"]["ci_low"] is None
    assert split["at_or_above"]["significant"] is False


def test_sharp_clv_odds_split_excludes_untrusted_rows() -> None:
    # Consensus-anchored (untrusted) rows never enter either band — the split
    # is of the TRUSTED subset only, so it can never diverge from n_sharp_close.
    rows = [
        _row(clv_log=0.02, decimal_odds=5.0, closing_anchor="consensus")
        for _ in range(MIN_HEADLINE_N + 10)
    ]
    agg = _aggregate_settled(rows)
    split = agg["sharp_clv_odds_split"]
    assert agg["n_sharp_close"] == 0
    assert split["below"]["n"] == 0
    assert split["at_or_above"]["n"] == 0
    assert split["n_unknown_odds"] == 0


def test_sharp_subset_gated_on_its_own_n_not_n_settled() -> None:
    # A big settled population (headline OK) but only a FEW genuine sharp closes:
    # the sharp metrics stay suppressed on their own n_sharp_close floor — a
    # thin trusted subset must not borrow the headline's sufficiency.
    rows = [_row(closing_anchor="consensus", closing_odds=2.0) for _ in range(MIN_HEADLINE_N)]
    rows += [_row(closing_anchor="pinnacle") for _ in range(3)]  # only 3 sharp closes
    agg = _aggregate_settled(rows)
    assert agg["roi_status"] == "ok"  # headline has enough n
    assert agg["n_sharp_close"] == 3
    assert agg["sharp_status"] == "insufficient"
    assert agg["sharp_stake_weighted_clv_log"] is None
    assert agg["sharp_beat_close_rate"] is None


def test_tautological_close_excluded_from_blended_and_trusted() -> None:
    # Audit 2026-06-28 P2: a row whose close fair EQUALS its pick-time fair
    # (model_probability == closing_fair_probability — the SAME archived sharp line
    # reused at pick-time and close-time) carries a clv_log that merely re-encodes
    # the pick-time edge: a TAUTOLOGY. It must be dropped from BOTH the blended
    # headline AND the trusted sharp subset, even though it is a sharp-anchored,
    # independent, genuine snapshot close.
    honest = [
        _row(
            closing_anchor="pinnacle",
            clv_log=0.04,
            decimal_odds=2.0,
            closing_fair_probability=0.51,
            model_probability=0.46,  # close MOVED from pick fair -> real CLV
            close_independent=True,
            has_snapshot_close=True,
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    tautology = [
        _row(
            closing_anchor="pinnacle",
            clv_log=0.30,  # would flatter both aggregates if admitted
            decimal_odds=2.0,
            closing_fair_probability=0.50,
            model_probability=0.5004,  # identical archived line (delta 0.0004 <= 1e-3)
            close_independent=True,
            has_snapshot_close=True,
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    agg = _aggregate_settled(honest + tautology)
    # Tautological rows stay real settled picks (honest denominator) ...
    assert agg["n_settled"] == 2 * MIN_HEADLINE_N
    # ... but contribute to NEITHER the trusted subset ...
    assert agg["n_sharp_close"] == MIN_HEADLINE_N
    assert agg["sharp_stake_weighted_clv_log"] == "0.04"
    # ... NOR the blended headline CLV (only the honest 0.04 rows remain).
    assert agg["stake_weighted_clv_log"] == "0.04"


def test_significance_surfaced_for_positive_large_n_blended_and_sharp() -> None:
    # P2 significance: a large-n, clearly-positive CLV series surfaces a one-sample
    # t-stat > 0, a 95% CI excluding 0, and significant=True on BOTH the blended and
    # the trusted sharp stratum. The Wilson beat-close CI is present and (here)
    # clears 0.5. These are the proof-of-edge fields the headline previously lacked.
    rows = [
        _row(
            clv_log=0.10 + (0.02 if i % 2 else -0.02),  # mean ~0.10, tight spread
            beat_close=True,
            closing_anchor="pinnacle",
            close_independent=True,
            has_snapshot_close=True,
            closing_fair_probability=0.55,  # in-bounds close-implied edge
            decimal_odds=2.0,
        )
        for i in range(200)
    ]
    agg = _aggregate_settled(rows)
    # blended stratum
    assert agg["clv_n"] == 200
    assert agg["clv_tstat"] > 0
    assert agg["clv_ci_low"] > 0
    assert agg["clv_ci_high"] > agg["clv_ci_low"]
    assert agg["clv_significant"] is True
    assert agg["clv_alpha"] == 0.05
    assert 0.0 <= agg["beat_close_wilson_low"] <= agg["beat_close_wilson_high"] <= 1.0
    assert agg["beat_close_wilson_significant"] is True  # 200/200 beat -> low > 0.5
    # trusted sharp stratum (same rows are all genuine independent sharp closes)
    assert agg["sharp_clv_n"] == 200
    assert agg["sharp_clv_tstat"] > 0
    assert agg["sharp_clv_ci_low"] > 0
    assert agg["sharp_clv_significant"] is True
    assert agg["sharp_beat_close_wilson_significant"] is True


def test_significance_not_significant_for_tiny_sharp_n() -> None:
    # HONEST live state: only 3 genuine sharp closes. The sharp significance fields
    # ARE computed (n=3) but read NOT significant — a wide CI straddling 0 — so the
    # platform never claims a real edge off 3 picks. Blended has a big-n base.
    rows = [_row(closing_anchor="consensus", clv_log=0.01) for _ in range(MIN_HEADLINE_N)]
    rows += [
        _row(
            closing_anchor="pinnacle", clv_log=clv, close_independent=True, has_snapshot_close=True
        )
        for clv in (0.30, -0.10, 0.05)  # 3 sharp closes, noisy
    ]
    agg = _aggregate_settled(rows)
    assert agg["sharp_clv_n"] == 3
    assert agg["sharp_clv_significant"] is False
    assert agg["sharp_clv_ci_low"] < 0 < agg["sharp_clv_ci_high"]


def test_significance_none_for_empty_and_singleton_strata() -> None:
    # Empty stratum: significance fields are None (not a crash); flags read False.
    empty = _aggregate_settled([])
    assert empty["clv_n"] == 0
    assert empty["clv_tstat"] is None
    assert empty["clv_ci_low"] is None
    assert empty["clv_significant"] is False
    assert empty["beat_close_wilson_low"] is None
    assert empty["beat_close_wilson_significant"] is False
    assert empty["sharp_clv_tstat"] is None
    assert empty["sharp_clv_significant"] is False

    # Singleton blended stratum: t-test undefined (n<2) -> None, flag False.
    one = _aggregate_settled([_row(closing_anchor="consensus", clv_log=0.05)])
    assert one["clv_n"] == 1
    assert one["clv_tstat"] is None
    assert one["clv_significant"] is False


def test_significance_computed_on_clean_subset_only() -> None:
    # Significance must use the SAME clean subset as the point estimates: fabricated
    # (impossible close-implied edge) and tautological (identical-line) rows are
    # dropped BEFORE the t-test, so they cannot manufacture significance.
    honest = [
        _row(
            clv_log=0.04,
            closing_anchor="pinnacle",
            close_independent=True,
            has_snapshot_close=True,
            decimal_odds=2.0,
            closing_fair_probability=0.51,
            model_probability=0.46,  # close moved -> real CLV
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    poison = _row(
        clv_log=1.756877,  # fabricated, would blow up mean/tstat if admitted
        closing_anchor="pinnacle",
        close_independent=True,
        has_snapshot_close=True,
        decimal_odds=6.50,
        closing_fair_probability=0.891433,  # impossible close-implied edge
    )
    agg = _aggregate_settled(honest + [poison])
    # Only the 50 honest rows feed significance (the poison row is excluded).
    assert agg["clv_n"] == MIN_HEADLINE_N
    assert agg["sharp_clv_n"] == MIN_HEADLINE_N
    assert agg["clv_mean"] == 0.04  # not inflated by the 1.76 poison
    assert agg["sharp_clv_mean"] == 0.04


def test_blended_headline_explicitly_marked_non_evidential() -> None:
    # CLV audit P1 / H5: the blended stake_weighted_clv_log / beat_close_rate mix
    # EVERY non-excluded close — including consensus-anchored and poll-time
    # re-scrape FALLBACK closes — which are NOT independent sharp evidence. They are
    # kept for continuity, but the payload MUST carry an unambiguous machine-readable
    # marker so a consumer cannot read the blended number as the strategy's proven
    # edge. The trusted sharp subset is THE evidential edge metric.
    rows = [_row(closing_anchor="consensus", clv_log=0.05) for _ in range(MIN_HEADLINE_N)]
    rows += [
        _row(
            closing_anchor="pinnacle", clv_log=0.03, close_independent=True, has_snapshot_close=True
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    agg = _aggregate_settled(rows)
    # blended fields PRESERVED for continuity ...
    assert "stake_weighted_clv_log" in agg
    assert "beat_close_rate" in agg
    assert agg["stake_weighted_clv_log"] is not None
    # ... but explicitly flagged indicative / non-evidential.
    assert agg["blended_clv_evidential"] is False
    # the trusted sharp subset is THE evidential edge metric.
    assert agg["sharp_clv_evidential"] is True
    assert agg["sharp_stake_weighted_clv_log"] is not None


def test_evidential_markers_are_structural_not_data_dependent() -> None:
    # The evidential markers are STRUCTURAL (constant per stratum), not data-
    # dependent: a consumer can rely on them even on an empty population. This is
    # what lets a "does it work?" reader distinguish indicative (blended) from
    # evidential (sharp_*) WITHOUT inspecting which closes were mixed in.
    agg = _aggregate_settled([])
    assert agg["blended_clv_evidential"] is False
    assert agg["sharp_clv_evidential"] is True


def test_evidential_markers_propagate_per_sport() -> None:
    # The per-sport split runs through _aggregate_settled, so every sport stratum
    # inherits the same non-evidential blended / evidential sharp markers.
    soccer = [("soccer", _row(outcome="won", pnl=1.0)) for _ in range(MIN_HEADLINE_N)]
    by_sport = _aggregate_settled_by_sport(soccer)
    assert by_sport["soccer"]["blended_clv_evidential"] is False
    assert by_sport["soccer"]["sharp_clv_evidential"] is True


def test_null_independence_excluded_from_trusted_subset() -> None:
    # Audit 2026-06-28 P2: the trusted subset now requires close_independent_of_fill
    # IS TRUE (not merely "IS NOT FALSE"). A NULL/unknown independence (pre-column or
    # un-stamped row) is no longer admitted — unproven independence is not trusted.
    null_indep = [
        _row(
            closing_anchor="pinnacle",
            clv_log=0.04,
            close_independent=None,  # unknown independence
            has_snapshot_close=True,
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    agg_null = _aggregate_settled(null_indep)
    assert agg_null["n_sharp_close"] == 0  # NULL no longer leaks into the trusted subset

    true_indep = [
        _row(
            closing_anchor="pinnacle",
            clv_log=0.04,
            close_independent=True,
            has_snapshot_close=True,
        )
        for _ in range(MIN_HEADLINE_N)
    ]
    agg_true = _aggregate_settled(true_indep)
    assert agg_true["n_sharp_close"] == MIN_HEADLINE_N  # only definite True counts


def test_clv_row_fabricated_magnitude_cutoff_is_fallback_only() -> None:
    # REGRESSION (2026-07-08): the |clv_log| > CLV_IMPLAUSIBLE_LOG cutoff is a
    # FALLBACK for rows that LACK the real inputs (odds or close prob), NOT an
    # unconditional verdict. A legitimate plausible-close longshot that HAS both
    # decimal_odds and closing_fair_probability and a modest close-implied edge
    # (<= CLV_IMPLAUSIBLE_CLOSE_EDGE) must NOT be classed fabricated just because
    # |clv_log| exceeds 0.5 — that silently deleted NEGATIVE sharp-close evidence.
    from app.storage.repositories import _clv_row_is_fabricated

    # decimal_odds=8.0 -> implied 0.125; closing_fair 0.30 -> close_edge 0.175 <= 0.20.
    # clv_log magnitude 0.92 > 0.5, yet the close is genuine: NOT fabricated.
    assert (
        _clv_row_is_fabricated(clv_log=0.92, decimal_odds=8.0, closing_fair_probability=0.30)
        is False
    )
    # Real fabrication by an impossible close-implied edge still trips.
    assert (
        _clv_row_is_fabricated(clv_log=1.5, decimal_odds=6.5, closing_fair_probability=0.891433)
        is True
    )
    # Fallback still fires when the close prob is absent (edge uncomputable).
    assert (
        _clv_row_is_fabricated(clv_log=1.76, decimal_odds=8.0, closing_fair_probability=None)
        is True
    )
    # Fallback still fires when the odds are absent.
    assert (
        _clv_row_is_fabricated(clv_log=1.76, decimal_odds=None, closing_fair_probability=0.30)
        is True
    )
    # No CLV at all -> never fabricated.
    assert (
        _clv_row_is_fabricated(clv_log=None, decimal_odds=None, closing_fair_probability=None)
        is False
    )


def test_clv_row_fabricated_judges_exchange_fills_on_effective_odds() -> None:
    # Read/write alignment (audit 2026-07-10): the write side computes clv_log
    # from the COMMISSION-NETTED fill (effective_odds), so the read-side
    # plausibility check must judge the SAME price. An exchange fill at raw 2.0
    # nets to 1.95 (Betfair Exchange 5%): implied 0.5128, so a close fair of
    # 0.71 is a 0.197 close-implied edge — PLAUSIBLE (<= 0.20) — while the raw
    # price would misread it as 0.21 > 0.20 and fabricate-flag honest evidence.
    from app.storage.repositories import _clv_row_is_fabricated

    assert (
        _clv_row_is_fabricated(
            clv_log=0.3,
            decimal_odds=2.0,
            closing_fair_probability=0.71,
            bookmaker="Betfair Exchange",
        )
        is False
    )
    # The SAME inputs at a commission-free book keep the raw verdict (edge 0.21).
    assert (
        _clv_row_is_fabricated(
            clv_log=0.3, decimal_odds=2.0, closing_fair_probability=0.71, bookmaker="SoftBook"
        )
        is True
    )
    # No bookmaker (pre-threading callers / feature-detected absent): raw price,
    # bit-identical to the old behavior.
    assert (
        _clv_row_is_fabricated(clv_log=0.3, decimal_odds=2.0, closing_fair_probability=0.71) is True
    )


def test_aggregate_settled_threads_fill_bookmaker_to_fabrication_guard() -> None:
    # End-to-end through the tuple contract: the SAME exchange-fill row keeps
    # its clv_log in the blended sample when the trailing bookmaker element is
    # present (effective-odds judgment) and is dropped as fabricated without it.
    def rows(bookmaker: str | None) -> list[tuple[object, ...]]:
        return [
            _row(
                clv_log=0.3,
                decimal_odds=2.0,
                closing_fair_probability=0.71,
                model_probability=0.60,
                bookmaker=bookmaker,
            )
            for _ in range(MIN_HEADLINE_N)
        ]

    netted = _aggregate_settled(rows("Betfair Exchange"))
    assert netted["clv_quality"]["clv_excluded_fabricated"] == 0
    raw = _aggregate_settled(rows(None))
    assert raw["clv_quality"]["clv_excluded_fabricated"] == MIN_HEADLINE_N


# ===== Claims-ledger ETA (2026-07-11): _trusted_close_eta pure helper ===========


def _eta_now() -> datetime:
    return datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)


def test_trusted_close_eta_rate_and_projection() -> None:
    from app.storage.repositories import _trusted_close_eta

    now = _eta_now()
    # 20 settled premium picks, alternating trusted -> trusted_rate 0.5 over the
    # last-30 window (all 20 inside it).
    settled = [(now - timedelta(days=20 - i), i % 2 == 0) for i in range(20)]
    # floor 10, 8 trusted so far -> 2 more needed -> at rate 0.5, 4 open picks
    # must kick off; the 4th kickoff is now+4d.
    kicks = [now + timedelta(days=d) for d in range(1, 8)]
    eta = _trusted_close_eta(settled, n_trusted=8, floor=10, open_kickoffs=kicks, now=now)
    assert eta["trusted_rate"] == pytest.approx(0.5)
    assert eta["n_rate_window"] == 20
    assert eta["open_premium"] == 7
    assert eta["projected_days"] == pytest.approx(4.0)
    # last trusted settle: the newest trusted row (i=18 -> now-2d)
    assert eta["last_trusted_settled_at"] == (now - timedelta(days=2)).isoformat()


def test_trusted_close_eta_rate_window_is_trailing_30() -> None:
    from app.storage.repositories import TRUSTED_CLOSE_RATE_WINDOW, _trusted_close_eta

    now = _eta_now()
    # 10 OLD trusted picks followed by 30 recent untrusted: the trailing-30
    # window sees ONLY untrusted -> rate 0.0 -> no projection, but the last
    # trusted settle time is still reported.
    old_trusted = [(now - timedelta(days=100 - i), True) for i in range(10)]
    recent_untrusted = [(now - timedelta(days=30 - i), False) for i in range(30)]
    eta = _trusted_close_eta(
        old_trusted + recent_untrusted, n_trusted=10, floor=50, open_kickoffs=[], now=now
    )
    assert eta["rate_window"] == TRUSTED_CLOSE_RATE_WINDOW == 30
    assert eta["n_rate_window"] == 30
    assert eta["trusted_rate"] == pytest.approx(0.0)
    assert eta["projected_days"] is None
    assert eta["last_trusted_settled_at"] == (now - timedelta(days=91)).isoformat()


def test_trusted_close_eta_nulls_rate_below_min_n_and_projection_without_pipeline() -> None:
    from app.storage.repositories import TRUSTED_CLOSE_RATE_MIN_N, _trusted_close_eta

    now = _eta_now()
    # below the 10-settled floor: the rate (and anything projected from it) is
    # nulled honestly; the denominators survive.
    thin = [(now - timedelta(days=i + 1), True) for i in range(TRUSTED_CLOSE_RATE_MIN_N - 1)]
    kicks = [now + timedelta(days=1)]
    eta = _trusted_close_eta(thin, n_trusted=9, floor=50, open_kickoffs=kicks, now=now)
    assert eta["trusted_rate"] is None
    assert eta["projected_days"] is None
    assert eta["n_rate_window"] == TRUSTED_CLOSE_RATE_MIN_N - 1
    assert eta["open_premium"] == 1
    # enough rate history but TOO FEW open picks to ever reach the floor:
    # the projection is nulled (never extrapolated past the real pipeline).
    settled = [(now - timedelta(days=20 - i), True) for i in range(20)]
    eta2 = _trusted_close_eta(settled, n_trusted=20, floor=50, open_kickoffs=kicks, now=now)
    assert eta2["trusted_rate"] == pytest.approx(1.0)
    assert eta2["projected_days"] is None  # 30 more needed; 1 open pick


def test_trusted_close_eta_empty_and_floor_met() -> None:
    from app.storage.repositories import _trusted_close_eta

    now = _eta_now()
    empty = _trusted_close_eta([], n_trusted=0, floor=50, open_kickoffs=[], now=now)
    assert empty["last_trusted_settled_at"] is None
    assert empty["trusted_rate"] is None
    assert empty["projected_days"] is None
    assert empty["open_premium"] == 0
    # floor already met: nothing to project (the tile is no longer accruing)
    settled = [(now - timedelta(days=20 - i), True) for i in range(20)]
    met = _trusted_close_eta(
        settled,
        n_trusted=50,
        floor=50,
        open_kickoffs=[now + timedelta(days=1)],
        now=now,
    )
    assert met["projected_days"] is None


# ===== 2026-07-12 Task 1: ADR-0022 crit-2 promotion-readiness cells ==============


def _trust_tuple(
    sport: str = "basketball",
    market: str = "spreads",
    clv: float | None = 0.02,
    settled_at: datetime | None = None,
    closing_anchor: str | None = "pinnacle",
) -> tuple[object, ...]:
    """A promotion_distance_cells row tuple (trusted unless closing_anchor says
    otherwise): (sport, market, settled_at, clv_log, closing_anchor,
    close_independent, has_snapshot_close, decimal_odds, closing_fair,
    model_prob, mint_fb, close_fb, bookmaker)."""
    return (
        sport,
        market,
        settled_at or datetime(2026, 7, 1, tzinfo=UTC),
        clv,
        closing_anchor,
        True,
        True,
        2.0,
        None,
        None,
        None,
        None,
        None,
    )


def test_promotion_cells_carry_ci_at_or_above_ok_n() -> None:
    from app.storage.repositories import SPORT_MARKET_OK_N, promotion_distance_cells

    now = datetime(2026, 7, 12, tzinfo=UTC)
    rows = [_trust_tuple(clv=0.02 + 0.001 * (i % 7)) for i in range(SPORT_MARKET_OK_N)]
    (cell,) = promotion_distance_cells(rows, now=now)
    assert cell["mean_clv_log"] is not None
    assert cell["ci_low_clv_log"] is not None
    assert cell["ci_high_clv_log"] is not None
    assert cell["ci_low_clv_log"] < cell["mean_clv_log"] < cell["ci_high_clv_log"]


def test_promotion_cells_ci_nulled_below_ok_n() -> None:
    from app.storage.repositories import promotion_distance_cells

    now = datetime(2026, 7, 12, tzinfo=UTC)
    (cell,) = promotion_distance_cells([_trust_tuple() for _ in range(5)], now=now)
    assert cell["ci_low_clv_log"] is None
    assert cell["ci_high_clv_log"] is None


def test_promotion_readiness_cell_shape_and_null_semantics() -> None:
    from app.storage.repositories import promotion_distance_cells, promotion_readiness_cells

    now = datetime(2026, 7, 12, tzinfo=UTC)
    # 23 trusted + 2 untrusted (consensus) settled -> coverage 23/25 = 92%... use
    # a thinner trusted share: 2 trusted of 25 settled -> coverage 8%.
    rows = [_trust_tuple() for _ in range(2)] + [
        _trust_tuple(closing_anchor="consensus") for _ in range(23)
    ]
    cells = promotion_distance_cells(rows, now=now)
    (entry,) = promotion_readiness_cells(cells)
    assert entry["sport"] == "basketball"
    assert entry["market"] == "spreads"
    assert entry["n_trusted"] == 2
    assert entry["needed_n"] == 50
    # below the per-cell CI floor: honestly pending, never fabricated
    assert entry["ci_low_gt_zero"] is None
    # not yet instrumented: null, never fabricated
    assert entry["source_agreement"] is None
    assert entry["freshness"] is None
    assert entry["coverage_pct"] == pytest.approx(8.0)
    assert entry["ready"] is False


def test_promotion_readiness_ci_flag_and_never_ready_while_uninstrumented() -> None:
    from app.storage.repositories import (
        SPORT_MARKET_OK_N,
        promotion_distance_cells,
        promotion_readiness_cells,
    )

    now = datetime(2026, 7, 12, tzinfo=UTC)
    positive = [_trust_tuple(clv=0.02 + 0.001 * (i % 7)) for i in range(SPORT_MARKET_OK_N + 5)]
    negative = [
        _trust_tuple(sport="soccer", market="h2h", clv=-0.02 - 0.001 * (i % 7))
        for i in range(SPORT_MARKET_OK_N + 5)
    ]
    cells = promotion_distance_cells(positive + negative, now=now)
    entries = {(e["sport"], e["market"]): e for e in promotion_readiness_cells(cells)}
    assert entries[("basketball", "spreads")]["ci_low_gt_zero"] is True
    assert entries[("soccer", "h2h")]["ci_low_gt_zero"] is False
    # ready requires EVERY condition instrumented AND holding — source_agreement
    # and freshness are still null, so nothing can be ready yet.
    assert all(e["ready"] is False for e in entries.values())


# ===== 2026-07-12 Task 2: uncertainty-shrink 30-day review (ADR-0022 crit 5) =====


def test_shrink_review_counts_and_null_below_floor() -> None:
    from app.storage.repositories import _shrink_review

    breakdowns: list[dict[str, object] | None] = [
        # pre-annotation rows (no phi key at all): not annotated
        {"raw_kelly": 0.1, "fractional": 0.02, "capped": False, "final": 0.02},
        None,
        # annotated, but no n_eff source was wired -> phi None (still annotated)
        {"final": 0.02, "phi": None, "n_eff": None, "shrunk_fraction": None},
        # annotated with real values
        {"final": 0.02, "phi": 0.5, "n_eff": 30, "shrunk_fraction": 0.01},
    ]
    rev = _shrink_review(breakdowns)
    assert rev["annotations_since"] == "2026-07-11"
    assert rev["review_due"] == "2026-08-10"
    assert rev["n_annotated"] == 2
    assert rev["n_with_phi"] == 1
    # below the n=10 floor: estimates nulled at the source
    assert rev["mean_phi"] is None
    assert rev["mean_shrunk_vs_final_ratio"] is None


def test_shrink_review_estimates_at_or_above_floor() -> None:
    from app.storage.repositories import _shrink_review

    breakdowns = [
        {"final": 0.02, "phi": 0.4 + 0.02 * i, "n_eff": 20, "shrunk_fraction": 0.01}
        for i in range(10)
    ]
    rev = _shrink_review(breakdowns)
    assert rev["n_annotated"] == 10
    assert rev["n_with_phi"] == 10
    assert rev["mean_phi"] == pytest.approx(sum(0.4 + 0.02 * i for i in range(10)) / 10)
    assert rev["mean_shrunk_vs_final_ratio"] == pytest.approx(0.5)


def test_shrink_review_ratio_skips_zero_final() -> None:
    from app.storage.repositories import _shrink_review

    # a zero final fraction can never divide; the row still counts as annotated
    breakdowns = [{"final": 0.0, "phi": 0.5, "n_eff": 10, "shrunk_fraction": 0.0}] * 12
    rev = _shrink_review(breakdowns)
    assert rev["n_annotated"] == 12
    assert rev["mean_phi"] == pytest.approx(0.5)
    assert rev["mean_shrunk_vs_final_ratio"] is None


# ===== 2026-07-12 Task 4: close-age histogram per close anchor ===================


def test_close_age_histogram_buckets_per_anchor() -> None:
    from app.storage.repositories import CLOSE_AGE_BUCKETS, _close_age_histogram

    kick = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)

    def cap(minutes: float) -> datetime:
        return kick - timedelta(minutes=minutes)

    rows: list[tuple[object, object, object]] = [
        ("pinnacle", cap(10.0), kick),  # <30m
        ("pinnacle", cap(45.0), kick),  # 30-60m
        ("pinnacle", cap(120.0), kick),  # 1-3h
        ("consensus", cap(400.0), kick),  # 3-12h
        (None, cap(800.0), kick),  # >12h, unknown anchor
        # unknowable rows are skipped, never guessed
        ("pinnacle", None, kick),
        ("pinnacle", cap(10.0), None),
    ]
    hist = _close_age_histogram(rows)
    assert hist["buckets"] == list(CLOSE_AGE_BUCKETS)
    assert hist["n"] == 5
    assert hist["by_anchor"]["pinnacle"] == {
        "<30m": 1,
        "30-60m": 1,
        "1-3h": 1,
        "3-12h": 0,
        ">12h": 0,
    }
    assert hist["by_anchor"]["consensus"]["3-12h"] == 1
    assert hist["by_anchor"]["unknown"][">12h"] == 1
    assert "capture" in hist["note"]


def test_close_age_histogram_boundaries_and_negative_age() -> None:
    from app.storage.repositories import _close_age_histogram

    kick = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)

    def cap(minutes: float) -> datetime:
        return kick - timedelta(minutes=minutes)

    rows = [
        ("sharp", cap(30.0), kick),  # exactly 30m -> 30-60m
        ("sharp", cap(60.0), kick),  # exactly 1h -> 1-3h
        ("sharp", cap(180.0), kick),  # exactly 3h -> 3-12h
        ("sharp", cap(720.0), kick),  # exactly 12h -> >12h
        ("sharp", cap(-5.0), kick),  # captured AFTER kickoff -> <30m (age <= 0)
    ]
    hist = _close_age_histogram(rows)
    assert hist["by_anchor"]["sharp"] == {
        "<30m": 1,
        "30-60m": 1,
        "1-3h": 1,
        "3-12h": 1,
        ">12h": 1,
    }


def test_close_age_histogram_empty() -> None:
    from app.storage.repositories import _close_age_histogram

    hist = _close_age_histogram([])
    assert hist["n"] == 0
    assert hist["by_anchor"] == {}
