"""Stratified live-evidence report (app/backtesting/live_evidence.py).

Pure-module tests on synthetic settled-pick rows: score buckets around q*,
tier split, feature-detected anchor dimension, and the honesty contract —
every stratum carries n, sub-min_n strata are flagged insufficient.
"""

import math

import pytest

from app.backtesting.live_evidence import (
    MIN_STRATUM_N,
    SettledPickRow,
    live_evidence_report,
    meta_model_calibration,
)


def row(
    tier: str = "premium",
    score: float | None = None,
    clv: float | None = 0.02,
    beat: bool | None = True,
    stake: float = 10.0,
    pnl: float | None = 1.0,
    anchor: str | None = None,
    closing_anchor: str | None = None,
    has_snapshot: bool = False,
    close_independent: bool | None = True,
    sport: str | None = None,
    closing_fair: float | None = None,
    model_prob: float | None = None,
    mint_fell_back: bool | None = None,
    close_fell_back: bool | None = None,
) -> SettledPickRow:
    return SettledPickRow(
        tier=tier,
        value_filter_score=score,
        clv_log=clv,
        beat_close=beat,
        stake=stake,
        pnl=pnl,
        anchor_type=anchor,
        closing_anchor_type=closing_anchor,
        has_snapshot_close=has_snapshot,
        close_independent_of_fill=close_independent,
        sport=sport,
        closing_fair_probability=closing_fair,
        model_probability=model_prob,
        mint_devig_fell_back=mint_fell_back,
        close_devig_fell_back=close_fell_back,
    )


def test_sharp_close_stratum_counts_only_genuine_sharp_snapshot_closes() -> None:
    # Honest CLV: only a SNAPSHOT-sourced close anchored by a NAMED sharp book
    # (pinnacle/exchange) is trusted. A consensus-median close and a
    # revalidation FALLBACK close (no snapshot close = closing_odds NULL) are
    # excluded — they contaminated the headline before this fix.
    rows = [
        row(clv=0.05, beat=True, stake=10.0, closing_anchor="pinnacle", has_snapshot=True),
        row(clv=0.03, beat=True, stake=10.0, closing_anchor="sharp", has_snapshot=True),
        row(clv=0.20, beat=True, stake=10.0, closing_anchor="consensus", has_snapshot=True),
        row(clv=0.15, beat=True, stake=10.0, closing_anchor="pinnacle", has_snapshot=False),
    ]
    sc = live_evidence_report(rows, ml_threshold=None, min_n=1)["sharp_close"]
    assert sc["n"] == 2  # only the two trusted sharp snapshot closes
    assert sc["stake_weighted_clv_log"] == pytest.approx((10 * 0.05 + 10 * 0.03) / 20)
    assert sc["sufficient"] is True


def test_by_sport_stratifies_and_suppresses_thin_buckets() -> None:
    # Per-sport evidence: soccer has enough CLV obs to be sufficient at min_n=2;
    # basketball has one — flagged insufficient with estimates nulled at source,
    # so an experimental/thin sport can never borrow another sport's sufficiency.
    rows = [
        row(sport="soccer", clv=0.02),
        row(sport="soccer", clv=0.03),
        row(sport="basketball", clv=0.05),
    ]
    rep = live_evidence_report(rows, ml_threshold=None, min_n=2)
    bs = rep["by_sport"]
    assert set(bs) == {"soccer", "basketball"}
    assert bs["soccer"]["n"] == 2
    assert bs["soccer"]["sufficient"] is True
    assert bs["basketball"]["n"] == 1
    assert bs["basketball"]["sufficient"] is False
    assert bs["basketball"]["mean_clv_log"] is None  # nulled at the source


def test_by_sport_absent_when_no_row_carries_sport() -> None:
    # Feature-detected like by_anchor: until a row carries a sport key the
    # dimension is None ("not available"), not an empty grouping.
    rep = live_evidence_report([row(), row()], ml_threshold=None, min_n=1)
    assert rep["by_sport"] is None


def test_sport_market_clv_gate_defaults_to_not_promoting() -> None:
    from app.backtesting.live_evidence import SportMarketClvGate

    gate = SportMarketClvGate()
    assert gate.enabled is False
    assert gate.min_n_sharp_close == 500
    assert gate.min_clv_sigma == 2.0
    assert gate.min_beat_close_ci_lower == 0.5
    # Even with overwhelming evidence, the DEFAULT (disabled) gate NEVER promotes.
    assert (
        gate.is_ready(
            n_sharp_close=10_000,
            sharp_clv_mean=0.05,
            sharp_clv_se=0.001,
            beat_close_ci_lower=0.6,
        )
        is False
    )


def test_sport_market_clv_gate_enabled_requires_every_bar() -> None:
    from app.backtesting.live_evidence import SportMarketClvGate

    enabled = SportMarketClvGate(enabled=True)
    # Clears every bar -> the ONLY True path.
    assert (
        enabled.is_ready(
            n_sharp_close=600, sharp_clv_mean=0.05, sharp_clv_se=0.001, beat_close_ci_lower=0.6
        )
        is True
    )
    # Thin sample -> not ready.
    assert (
        enabled.is_ready(
            n_sharp_close=10, sharp_clv_mean=0.05, sharp_clv_se=0.001, beat_close_ci_lower=0.6
        )
        is False
    )
    # CLV not > 2 SE -> not ready.
    assert (
        enabled.is_ready(
            n_sharp_close=600, sharp_clv_mean=0.05, sharp_clv_se=0.05, beat_close_ci_lower=0.6
        )
        is False
    )
    # Beat-close CI lower bound at coin-flip -> not ready; missing inputs fail closed.
    assert (
        enabled.is_ready(
            n_sharp_close=600, sharp_clv_mean=0.05, sharp_clv_se=0.001, beat_close_ci_lower=0.5
        )
        is False
    )
    assert (
        enabled.is_ready(
            n_sharp_close=600, sharp_clv_mean=None, sharp_clv_se=None, beat_close_ci_lower=None
        )
        is False
    )


def test_sharp_close_excludes_asymmetric_devig_fallback() -> None:
    # P2-2: a genuine independent sharp snapshot close is dropped from the trusted
    # sharp_close stratum when the MINT devig fell back but the CLOSE did not (or
    # vice versa) — the CLV is a devig-method artifact. A SYMMETRIC fallback (both
    # True) and unknown provenance (None) are kept.
    asymmetric = [
        row(
            closing_anchor="pinnacle", has_snapshot=True, mint_fell_back=True, close_fell_back=False
        )
        for _ in range(3)
    ]
    assert live_evidence_report(asymmetric, ml_threshold=None, min_n=1)["sharp_close"]["n"] == 0
    symmetric = [
        row(closing_anchor="pinnacle", has_snapshot=True, mint_fell_back=True, close_fell_back=True)
        for _ in range(3)
    ]
    assert live_evidence_report(symmetric, ml_threshold=None, min_n=1)["sharp_close"]["n"] == 3
    unknown = [row(closing_anchor="pinnacle", has_snapshot=True) for _ in range(3)]
    assert live_evidence_report(unknown, ml_threshold=None, min_n=1)["sharp_close"]["n"] == 3


def test_sharp_close_stratum_is_zero_when_no_trusted_closes() -> None:
    rows = [row(clv=0.5, closing_anchor="consensus", has_snapshot=True)]
    sc = live_evidence_report(rows, ml_threshold=None, min_n=1)["sharp_close"]
    assert sc["n"] == 0
    assert sc["sufficient"] is False


def test_sharp_close_excludes_circular_close_anchored_by_fill_book() -> None:
    """P0-1/P0-3 independence guard: a 'sharp' close whose anchor book IS the
    fill book is CIRCULAR (the pick's own book pricing its own close,
    closing == fill, |clv_log|~0) and must NOT count as genuine CLV — it is
    what masked the -EV. A named-sharp snapshot close that is NOT independent
    of the fill is excluded from the sharp subset; only independent ones enter,
    so closing_anchor != fill_book holds across the whole sharp_close subset."""
    rows = [
        # circular: fill book == close anchor book -> excluded despite being a
        # named sharp snapshot close with a (fake, ~0) clv.
        row(clv=0.001, closing_anchor="pinnacle", has_snapshot=True, close_independent=False),
        # genuine: a DIFFERENT sharp book priced the close -> trusted.
        row(clv=0.04, closing_anchor="pinnacle", has_snapshot=True, close_independent=True),
    ]
    sc = live_evidence_report(rows, ml_threshold=None, min_n=1)["sharp_close"]
    assert sc["n"] == 1  # only the independent close survives
    assert sc["stake_weighted_clv_log"] == pytest.approx(0.04)
    # The invariant the guard guarantees: every row in the sharp subset is
    # independent of its fill book (no circular close contaminates the subset).
    assert all(r.close_independent_of_fill is not False for r in rows if r.sharp_close)


def test_sharp_close_excludes_tautological_close_echoing_pick_anchor() -> None:
    """#137 mirror: a named-sharp snapshot close that the persisted independence
    flag calls independent (close_independent_of_fill=True — a DIFFERENT book)
    but whose CLOSE fair merely ECHOES the pick-time fair (closing == model, the
    SAME archived sharp line reused at pick- and close-time) is a TAUTOLOGY:
    clv_log re-encodes the pick-time edge, not real CLV. It must NOT enter the
    trusted sharp subset even though the fill-book-only flag passed it."""
    rows = [
        # tautological: closing_fair == model_probability (line did NOT move) ->
        # excluded despite an 'independent' flag and a named sharp snapshot close.
        row(
            clv=0.03,
            closing_anchor="pinnacle",
            has_snapshot=True,
            close_independent=True,
            closing_fair=0.50,
            model_prob=0.50,
        ),
        # genuine: the close fair MOVED from the pick-time fair -> real CLV.
        row(
            clv=0.04,
            closing_anchor="pinnacle",
            has_snapshot=True,
            close_independent=True,
            closing_fair=0.55,
            model_prob=0.50,
        ),
    ]
    sc = live_evidence_report(rows, ml_threshold=None, min_n=1)["sharp_close"]
    assert sc["n"] == 1  # only the MOVED-line close survives
    assert sc["stake_weighted_clv_log"] == pytest.approx(0.04)
    assert all(not r.is_tautological_close for r in rows if r.sharp_close)


def test_tautological_close_excluded_from_close_anchor_clv() -> None:
    """A pinnacle-CLOSED row whose close fair equals its pick-time fair (identical
    archived line, |move|<=eps) carries a TAUTOLOGICAL clv_log. Like the circular
    guard, it must NOT move by_close_anchor['pinnacle'].mean_clv_log — _stratum_stats
    drops proven-tautological closes from the CLV/beat samples (pnl_rows untouched)."""
    moved = row(
        clv=0.02,
        beat=True,
        pnl=1.0,
        closing_anchor="pinnacle",
        has_snapshot=True,
        close_independent=True,
        closing_fair=0.62,
        model_prob=0.60,
    )
    tautological = row(
        clv=0.99,
        beat=True,
        pnl=-1.0,
        closing_anchor="pinnacle",
        has_snapshot=True,
        close_independent=True,  # the fill-book-only flag says "independent"...
        closing_fair=0.60,
        model_prob=0.60,  # ...but the line did NOT move => tautology
    )
    pin = live_evidence_report([moved, tautological], ml_threshold=None, min_n=1)[
        "by_close_anchor"
    ]["pinnacle"]
    assert pin["n"] == 2  # both rows still in the honest n
    assert pin["n_clv"] == 1  # ...but only the MOVED close in the CLV sample
    assert pin["mean_clv_log"] == pytest.approx(0.02)  # tautological 0.99 did NOT move it
    assert pin["n_roi"] == 2  # pnl_rows untouched: ROI still sees both realized P&Ls


def test_tautology_guard_needs_both_fairs_present() -> None:
    """Feature-detection contract (mirrors the persisted guard): a tautology is
    only PROVABLE when a clv_log AND BOTH fair probabilities are present. A row
    with a missing fair (pre-column / unknowable) is NOT treated as tautological,
    so historical sharp closes keep their existing trusted status."""
    # closing_fair present but model_prob absent -> cannot prove tautology -> kept.
    kept = row(
        clv=0.03,
        closing_anchor="sharp",
        has_snapshot=True,
        close_independent=True,
        closing_fair=0.50,
        model_prob=None,
    )
    assert kept.is_tautological_close is False
    sc = live_evidence_report([kept], ml_threshold=None, min_n=1)["sharp_close"]
    assert sc["n"] == 1


def test_sharp_close_independence_unknown_does_not_exclude() -> None:
    """Feature-detection contract: a pre-column row carries
    close_independent_of_fill=None (unknown). Unknown is NOT treated as
    circular — only a definite False (proven circular) excludes — so historical
    sharp snapshot closes keep their existing trusted status."""
    rows = [row(clv=0.03, closing_anchor="sharp", has_snapshot=True, close_independent=None)]
    sc = live_evidence_report(rows, ml_threshold=None, min_n=1)["sharp_close"]
    assert sc["n"] == 1


def test_by_close_anchor_groups_on_the_close_anchor_not_creation() -> None:
    # A pick CREATED pinnacle-anchored but CLOSED on consensus belongs in the
    # consensus CLOSE bucket — that is the anchor CLV actually measures against.
    rows = [
        row(clv=0.02, anchor="pinnacle", closing_anchor="consensus", has_snapshot=True),
        row(clv=0.04, anchor="consensus", closing_anchor="pinnacle", has_snapshot=True),
    ]
    report = live_evidence_report(rows, ml_threshold=None, min_n=1)
    assert set(report["by_close_anchor"]) == {"consensus", "pinnacle"}
    assert report["by_close_anchor"]["consensus"]["n"] == 1
    assert report["by_close_anchor"]["pinnacle"]["n"] == 1
    # by_anchor (CREATION anchor) keeps its existing contract, unchanged
    assert set(report["by_anchor"]) == {"pinnacle", "consensus"}


def test_circular_close_excluded_from_close_anchor_clv() -> None:
    # CLV-2: a pinnacle-anchored, pinnacle-CLOSED row whose close is NON-independent
    # (circular self-priced, close_independent_of_fill=False) carries a FAKE positive
    # clv_log. It must NOT move by_close_anchor['pinnacle'].mean_clv_log — _stratum_stats
    # excludes proven-circular closes from the CLV/beat samples (pnl_rows untouched, so
    # ROI still sees the row's realized P&L).
    indep = row(
        clv=0.02,
        beat=True,
        pnl=1.0,
        closing_anchor="pinnacle",
        has_snapshot=True,
        close_independent=True,
    )
    circular = row(
        clv=0.99,
        beat=True,
        pnl=-1.0,
        closing_anchor="pinnacle",
        has_snapshot=True,
        close_independent=False,
    )
    pin = live_evidence_report([indep, circular], ml_threshold=None, min_n=1)["by_close_anchor"][
        "pinnacle"
    ]
    assert pin["n"] == 2  # both rows still counted in the honest n
    assert pin["n_clv"] == 1  # ...but only the INDEPENDENT close in the CLV sample
    assert pin["mean_clv_log"] == pytest.approx(0.02)  # circular 0.99 did NOT move it
    assert pin["stake_weighted_clv_log"] == pytest.approx(0.02)
    assert pin["n_roi"] == 2  # pnl_rows untouched: ROI still sees both realized P&Ls


def test_score_buckets_split_on_q_star_inclusive() -> None:
    rows = [
        row(score=0.80),  # >= q*
        row(score=0.725),  # exactly q* -> >= bucket (gate parity: >= keeps)
        row(score=0.70),  # < q*
        row(score=None),  # unscored
    ]
    report = live_evidence_report(rows, ml_threshold=0.725)
    assert report["q_star"] == 0.725
    assert report["by_score"]["score_ge_q"]["n"] == 2
    assert report["by_score"]["score_lt_q"]["n"] == 1
    assert report["by_score"]["unscored"]["n"] == 1


def test_no_threshold_means_one_scored_bucket() -> None:
    report = live_evidence_report([row(score=0.9), row(score=0.1)], ml_threshold=None)
    assert report["q_star"] is None
    assert set(report["by_score"]) == {"scored"}
    assert report["by_score"]["scored"]["n"] == 2


def test_tier_split_and_clv_roi_math() -> None:
    rows = [
        row(tier="premium", clv=0.10, beat=True, stake=10.0, pnl=5.0),
        row(tier="premium", clv=-0.02, beat=False, stake=30.0, pnl=-10.0),
        row(tier="volume", clv=0.01, beat=True, stake=5.0, pnl=0.5),
    ]
    report = live_evidence_report(rows, ml_threshold=0.725, min_n=1)
    premium = report["by_tier"]["premium"]
    assert premium["n"] == 2
    assert premium["n_clv"] == 2
    assert premium["mean_clv_log"] == pytest.approx((0.10 - 0.02) / 2)
    # stake-weighted: (10*0.10 + 30*-0.02) / 40
    assert premium["stake_weighted_clv_log"] == pytest.approx(0.4 / 40.0)
    assert premium["beat_close_rate"] == pytest.approx(0.5)
    assert premium["roi"] == pytest.approx(-5.0 / 40.0)
    assert premium["sufficient"] is True
    assert report["by_tier"]["volume"]["n"] == 1


def test_unrevalidated_rows_stay_in_n_but_out_of_estimates() -> None:
    rows = [
        row(clv=None, beat=None, pnl=None),  # settled but never revalidated
        row(clv=0.03, beat=True, pnl=2.0),
    ]
    report = live_evidence_report(rows, ml_threshold=None, min_n=1)
    stats = report["by_tier"]["premium"]
    assert stats["n"] == 2  # honest n: every settled row counts
    assert stats["n_clv"] == 1  # ...but only CLV rows enter CLV estimates
    assert stats["n_roi"] == 1
    assert stats["mean_clv_log"] == pytest.approx(0.03)


def test_insufficient_stratum_is_flagged_below_min_n() -> None:
    # 49 CLV rows < default 50 -> insufficient; the 50th flips it.
    rows = [row(clv=0.01) for _ in range(MIN_STRATUM_N - 1)]
    report = live_evidence_report(rows, ml_threshold=None)
    assert report["min_n"] == MIN_STRATUM_N
    assert report["by_tier"]["premium"]["sufficient"] is False
    report = live_evidence_report(rows + [row(clv=0.01)], ml_threshold=None)
    assert report["by_tier"]["premium"]["sufficient"] is True


def test_insufficient_stratum_nulls_estimates_at_source() -> None:
    """Validator-confirmed hardening: an insufficient stratum must carry NO
    point estimates in the payload — the dashboard honors the flag, but any
    other consumer of GET /performance would otherwise read noise-level
    numbers. Denominators and the flag survive; estimates are nulled."""
    rows = [row(clv=0.05, beat=True, pnl=3.0) for _ in range(MIN_STRATUM_N - 1)]
    stats = live_evidence_report(rows, ml_threshold=None)["by_tier"]["premium"]
    assert stats["sufficient"] is False
    assert stats["mean_clv_log"] is None
    assert stats["stake_weighted_clv_log"] is None
    assert stats["beat_close_rate"] is None
    assert stats["roi"] is None
    # honest denominators stay visible for the insufficient-state render
    assert stats["n"] == MIN_STRATUM_N - 1
    assert stats["n_clv"] == MIN_STRATUM_N - 1
    assert stats["n_roi"] == MIN_STRATUM_N - 1
    # ...and the same rows above the floor keep their estimates
    full = live_evidence_report(rows + [row(clv=0.05, pnl=3.0)], ml_threshold=None)
    assert full["by_tier"]["premium"]["sufficient"] is True
    assert full["by_tier"]["premium"]["mean_clv_log"] == pytest.approx(0.05)


def test_anchor_dimension_is_feature_detected() -> None:
    # No row carries anchor_type (column not landed) -> dimension is None,
    # distinguishable from an empty grouping; once values exist, it appears.
    without = live_evidence_report([row(), row()], ml_threshold=None)
    assert without["by_anchor"] is None
    with_anchor = live_evidence_report(
        [row(anchor="sharp"), row(anchor="consensus"), row()], ml_threshold=None, min_n=1
    )
    assert with_anchor["by_anchor"] is not None
    assert with_anchor["by_anchor"]["sharp"]["n"] == 1
    assert with_anchor["by_anchor"]["consensus"]["n"] == 1


def test_empty_rows_yield_empty_but_valid_report() -> None:
    report = live_evidence_report([], ml_threshold=0.725)
    assert report["n_settled"] == 0
    assert report["by_score"] == {}
    assert report["by_tier"] == {}
    assert report["by_anchor"] is None


def test_non_finite_threshold_rejected() -> None:
    with pytest.raises(ValueError):
        live_evidence_report([], ml_threshold=math.nan)


def test_meta_model_calibration_scores_score_vs_beat_close() -> None:
    # value_filter_score is the predicted P(beat close); beat_close is the realized
    # outcome. 0.7-scored picks beat close 70% of the time, 0.3-scored 30% -> the
    # meta-model is well calibrated in production (low ECE).
    rows = (
        [row(score=0.7, beat=True) for _ in range(7)]
        + [row(score=0.7, beat=False) for _ in range(3)]
        + [row(score=0.3, beat=True) for _ in range(3)]
        + [row(score=0.3, beat=False) for _ in range(7)]
    )
    rep = meta_model_calibration(rows, min_n=10)
    assert rep.n == 20
    assert rep.insufficient is False
    assert rep.base_rate == pytest.approx(0.5)
    assert rep.ece is not None and rep.ece < 0.1  # well calibrated


def test_meta_model_calibration_excludes_rows_without_score_or_label() -> None:
    # Only picks carrying BOTH a meta-model score and a realized beat-close label
    # are scorable — a missing score or an unrevalidated pick is dropped.
    rows = [
        row(score=0.6, beat=True),
        row(score=None, beat=True),  # no meta-model score
        row(score=0.6, beat=None),  # never revalidated against a close
    ]
    rep = meta_model_calibration(rows, min_n=1)
    assert rep.n == 1


def test_meta_model_calibration_insufficient_below_min_n() -> None:
    rep = meta_model_calibration([row(score=0.6, beat=True)], min_n=50)
    assert rep.insufficient is True
    assert rep.ece is None
