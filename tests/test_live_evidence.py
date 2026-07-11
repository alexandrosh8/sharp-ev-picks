"""Stratified live-evidence report (app/backtesting/live_evidence.py).

Pure-module tests on synthetic settled-pick rows: score buckets around q*,
tier split, feature-detected anchor dimension, and the honesty contract —
every stratum carries n, sub-min_n strata are flagged insufficient.
"""

import math
from datetime import datetime, timedelta

import pytest

from app.backtesting.live_evidence import (
    MIN_STRATUM_N,
    PREMIUM_SELECTION_FIX_AT,
    SettledPickRow,
    live_evidence_report,
    mc_null_record,
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
    decimal_odds: float | None = None,
    minted_at: datetime | None = None,
) -> SettledPickRow:
    return SettledPickRow(
        decimal_odds=decimal_odds,
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
        minted_at=minted_at,
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


def test_fabricated_clv_close_is_excluded_from_panel_and_sharp_subset() -> None:
    # CLV-1: a physically-impossible close (close-implied edge closing_fair - 1/odds
    # exceeds 0.20) must not pollute the CLV panel or the trusted sharp subset —
    # mirrors the headline _clv_row_is_fabricated guard (deep-review #4).
    genuine = row(
        clv=0.05, closing_anchor="pinnacle", has_snapshot=True, closing_fair=0.55, decimal_odds=2.0
    )  # close-implied edge 0.05 — plausible
    fabricated = row(
        clv=0.05, closing_anchor="pinnacle", has_snapshot=True, closing_fair=0.90, decimal_odds=5.0
    )  # close-implied edge 0.70 — physically impossible
    assert genuine.is_fabricated is False
    assert fabricated.is_fabricated is True
    sc = live_evidence_report([genuine, fabricated], ml_threshold=None, min_n=1)["sharp_close"]
    assert sc["n"] == 1  # the fabricated close is excluded; only the genuine one is trusted
    assert sc["sufficient"] is True


def test_fabricated_magnitude_cutoff_is_fallback_only() -> None:
    # REGRESSION (2026-07-08): with BOTH decimal_odds and closing_fair_probability
    # present and a modest close-implied edge (<= CLV_IMPLAUSIBLE_CLOSE_EDGE), a
    # large |clv_log| must NOT mark the row fabricated. The magnitude cutoff is a
    # FALLBACK for rows that LACK those inputs; firing it unconditionally deleted
    # legitimate NEGATIVE sharp-close evidence from the trusted subset.
    # decimal_odds=8.0 -> implied 0.125; closing_fair 0.30 -> close_edge 0.175.
    genuine_longshot = row(
        clv=0.92,
        closing_fair=0.30,
        decimal_odds=8.0,
        closing_anchor="pinnacle",
        has_snapshot=True,
    )
    assert genuine_longshot.is_fabricated is False
    # Fallback: close prob absent -> |clv_log| > 0.5 still trips.
    no_close = row(clv=1.76, closing_fair=None, decimal_odds=8.0)
    assert no_close.is_fabricated is True
    # Real edge-based fabrication still trips (close-implied edge 0.70 >> 0.20).
    impossible = row(clv=0.05, closing_fair=0.90, decimal_odds=5.0)
    assert impossible.is_fabricated is True


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


def test_meta_model_calibration_stratifies_by_close_anchor() -> None:
    # Review #5: the score predicts P(beat the MAX-of-books close), but beat_close
    # is realized vs whatever close anchor each pick got. The monitor must
    # stratify by closing_anchor_type so consensus (max-equivalent) and pinnacle
    # closes are not conflated into one apples-to-oranges aggregate.
    from app.backtesting.live_evidence import meta_model_calibration_by_close_anchor

    rows = [row(score=0.8, beat=True, closing_anchor="consensus") for _ in range(4)] + [
        row(score=0.2, beat=False, closing_anchor="pinnacle") for _ in range(4)
    ]
    strata = meta_model_calibration_by_close_anchor(rows, min_n=1)
    assert set(strata) == {"consensus", "pinnacle"}
    assert strata["consensus"].n == 4
    assert strata["pinnacle"].n == 4
    # rows missing a score or a beat-close label are excluded entirely
    none_score = [row(score=None, beat=True, closing_anchor="consensus")]
    assert meta_model_calibration_by_close_anchor(none_score, min_n=1) == {}


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


def test_null_independence_row_is_not_sharp_close() -> None:
    """Alignment pin (2026-07-10): the TRUSTED sharp subset requires
    close_independent_of_fill EXACTLY True — a NULL (unknown) independence row
    is NOT sharp_close, exactly like the headline predicate
    app.storage.repositories._settled_close_is_trusted (``is True``), so the
    two trusted-n figures can never drift. The per-stratum CLV samples keep
    their looser only-a-proven-False-excludes contract (unchanged)."""
    unknown = row(clv=0.03, closing_anchor="sharp", has_snapshot=True, close_independent=None)
    assert unknown.sharp_close is False
    rep = live_evidence_report([unknown], ml_threshold=None, min_n=1)
    assert rep["sharp_close"]["n"] == 0
    # per-stratum CLV sample (NOT the trusted subset): unknown still admitted.
    assert rep["by_close_anchor"]["sharp"]["n_clv"] == 1


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


# ===== Task 4 (2026-07-10): trusted-CLV-first operator report ==================


def trusted_row(
    clv: float,
    tier: str = "premium",
    pnl: float | None = 1.0,
    stake: float = 10.0,
    beat: bool | None = True,
) -> SettledPickRow:
    """A row that passes every trusted sharp-close guard."""
    return row(
        tier=tier,
        clv=clv,
        beat=beat,
        stake=stake,
        pnl=pnl,
        closing_anchor="pinnacle",
        has_snapshot=True,
        close_independent=True,
    )


def test_trusted_clv_ci_reports_per_tier_headline_with_ci_and_n() -> None:
    rows = [trusted_row(0.02 + 0.001 * i) for i in range(10)] + [trusted_row(-0.01, tier="volume")]
    tc = live_evidence_report(rows, ml_threshold=None, min_n=5)["trusted_clv_ci"]
    assert tc["overall"]["n"] == 11
    prem = tc["by_tier"]["premium"]
    assert prem["n"] == 10
    assert prem["sufficient"] is True
    expected_mean = sum(0.02 + 0.001 * i for i in range(10)) / 10
    assert prem["mean_clv_log"] == pytest.approx(expected_mean)
    assert prem["ci_low"] is not None and prem["ci_high"] is not None
    assert prem["ci_low"] < expected_mean < prem["ci_high"]
    # the thin tier is nulled at the source, exactly like every other stratum
    vol = tc["by_tier"]["volume"]
    assert vol["n"] == 1
    assert vol["sufficient"] is False
    assert vol["mean_clv_log"] is None
    assert vol["ci_low"] is None and vol["ci_high"] is None


def test_trusted_clv_ci_counts_only_trusted_rows() -> None:
    # a consensus close and a NULL-independence close never enter the headline
    rows = [trusted_row(0.03) for _ in range(3)] + [
        row(clv=0.9, closing_anchor="consensus", has_snapshot=True),
        row(clv=0.9, closing_anchor="pinnacle", has_snapshot=True, close_independent=None),
    ]
    tc = live_evidence_report(rows, ml_threshold=None, min_n=3)["trusted_clv_ci"]
    assert tc["overall"]["n"] == 3


def test_clv_yield_ratio_on_the_same_trusted_subset() -> None:
    # trusted CLV 5% (fractional, expm1 of the log), flat-stake yield 4% ->
    # ratio 0.8x, exactly the RebelBetting public benchmark.
    rows = [trusted_row(math.log(1.05), pnl=0.4, stake=10.0) for _ in range(10)]
    yr = live_evidence_report(rows, ml_threshold=None, min_n=5)["clv_yield_ratio"]
    assert yr["n_clv"] == 10
    assert yr["n_yield"] == 10
    assert yr["trusted_clv"] == pytest.approx(0.05)
    assert yr["flat_yield"] == pytest.approx(0.04)
    assert yr["ratio"] == pytest.approx(0.8)
    assert yr["benchmark"] == pytest.approx(0.8)
    assert "2026-07-10-whole-internet-research" in yr["benchmark_source"]


def test_clv_yield_ratio_nulled_below_floor_or_near_zero_clv() -> None:
    # below the floor: BOTH sides and the ratio are nulled at the source
    thin = [trusted_row(0.05) for _ in range(3)]
    yr = live_evidence_report(thin, ml_threshold=None, min_n=5)["clv_yield_ratio"]
    assert yr["ratio"] is None
    assert yr["trusted_clv"] is None
    assert yr["flat_yield"] is None
    assert yr["n_clv"] == 3
    # |trusted CLV| < CLV_YIELD_MIN_ABS_CLV (0.005): the ratio is nulled (a
    # near-zero denominator amplifies noise without bound — live 2026-07-10
    # example: fractional-mean CLV +0.0016 rendered a meaningless -59.8x) even
    # though both sides cleared the floor
    zero = [trusted_row(1e-3, pnl=0.5) for _ in range(6)]
    yr0 = live_evidence_report(zero, ml_threshold=None, min_n=5)["clv_yield_ratio"]
    assert yr0["trusted_clv"] is not None
    assert yr0["flat_yield"] is not None
    assert yr0["ratio"] is None
    # pnl side below the floor (no realized P&L): ratio nulled
    no_pnl = [trusted_row(0.05, pnl=None) for _ in range(6)]
    yrp = live_evidence_report(no_pnl, ml_threshold=None, min_n=5)["clv_yield_ratio"]
    assert yrp["trusted_clv"] is not None
    assert yrp["flat_yield"] is None
    assert yrp["ratio"] is None


def test_evidence_verdict_driven_by_existing_significance_gates() -> None:
    # 60 trusted rows, clearly positive varied CLV -> t-CI excludes 0 (and the
    # Wilson lower bound clears 0.5): sufficient.
    strong = [trusted_row(0.03 + 0.001 * (i % 5)) for i in range(60)]
    verdict = live_evidence_report(strong, ml_threshold=None)["evidence_verdict"]
    assert verdict.startswith("evidence sufficient to judge profitability at current n")
    # below the floor -> insufficient, with the honest denominators in the text
    thin = live_evidence_report(strong[:10], ml_threshold=None)["evidence_verdict"]
    assert thin.startswith("evidence insufficient to judge profitability at current n")
    # above the floor but the CI straddles 0 and beat-close is a coin flip ->
    # neither gate establishes anything: insufficient.
    mixed = [trusted_row(0.05 if i % 2 == 0 else -0.05, beat=i % 2 == 0) for i in range(60)]
    straddle = live_evidence_report(mixed, ml_threshold=None)["evidence_verdict"]
    assert straddle.startswith("evidence insufficient to judge profitability at current n")


def test_new_report_keys_present_and_nulled_on_empty_report() -> None:
    rep = live_evidence_report([], ml_threshold=None)
    tc = rep["trusted_clv_ci"]
    assert tc["overall"]["n"] == 0
    assert tc["overall"]["sufficient"] is False
    assert tc["overall"]["mean_clv_log"] is None
    assert tc["by_tier"] == {}
    yr = rep["clv_yield_ratio"]
    assert yr["ratio"] is None
    assert yr["trusted_clv"] is None
    assert yr["flat_yield"] is None
    assert rep["evidence_verdict"].startswith(
        "evidence insufficient to judge profitability at current n"
    )
    # ADR-0022 crit 3/4 cohorts + the MC null probe ride the same empty report
    cohorts = tc["premium_cohorts"]
    assert cohorts["pre_fix"]["n"] == 0
    assert cohorts["post_fix"]["n"] == 0
    assert cohorts["pre_fix"]["sufficient"] is False
    assert rep["mc_null"] == {"n": 0, "observed_units": None, "p_luck": None, "sims": 10_000}


# ===== ADR-0022 crit 3/4 (2026-07-11): post-fix premium cohort split ============


def trusted_minted(clv: float, minted_at: datetime, tier: str = "premium") -> SettledPickRow:
    return row(
        tier=tier,
        clv=clv,
        closing_anchor="pinnacle",
        has_snapshot=True,
        close_independent=True,
        minted_at=minted_at,
    )


def test_premium_cohorts_split_on_the_selection_fix_boundary() -> None:
    # ADR-0022 crit 3: the post-fix cohort is picks minted AFTER 2026-07-07;
    # crit 4 requires the split reported. Boundary semantics: minted exactly at
    # the boundary instant is POST-fix (>=), before it is PRE-fix.
    pre = [
        trusted_minted(0.02, PREMIUM_SELECTION_FIX_AT - timedelta(hours=1 + i)) for i in range(6)
    ]
    post = [
        trusted_minted(-0.01, PREMIUM_SELECTION_FIX_AT + timedelta(hours=i)) for i in range(6)
    ]  # i=0 = exactly the boundary -> post_fix
    tc = live_evidence_report(pre + post, ml_threshold=None, min_n=5)["trusted_clv_ci"]
    cohorts = tc["premium_cohorts"]
    assert cohorts["pre_fix"]["n"] == 6
    assert cohorts["pre_fix"]["sufficient"] is True
    assert cohorts["pre_fix"]["mean_clv_log"] == pytest.approx(0.02)
    assert cohorts["post_fix"]["n"] == 6
    assert cohorts["post_fix"]["mean_clv_log"] == pytest.approx(-0.01)
    # same entry shape as the tier entries
    assert set(cohorts["pre_fix"]) == set(tc["by_tier"]["premium"])


def test_premium_cohorts_premium_trusted_only_and_nulled_below_floor() -> None:
    rows = [
        trusted_minted(0.02, PREMIUM_SELECTION_FIX_AT - timedelta(days=1)),
        trusted_minted(0.02, PREMIUM_SELECTION_FIX_AT - timedelta(days=2)),
        # volume-tier trusted row: never enters the PREMIUM cohorts
        trusted_minted(0.9, PREMIUM_SELECTION_FIX_AT - timedelta(days=1), tier="volume"),
        # untrusted premium row (consensus close): never enters
        row(
            clv=0.9,
            closing_anchor="consensus",
            has_snapshot=True,
            minted_at=PREMIUM_SELECTION_FIX_AT,
        ),
        # unknown mint time: excluded from BOTH cohorts (cannot be assigned honestly)
        row(
            clv=0.9,
            closing_anchor="pinnacle",
            has_snapshot=True,
            close_independent=True,
            minted_at=None,
        ),
    ]
    cohorts = live_evidence_report(rows, ml_threshold=None, min_n=5)["trusted_clv_ci"][
        "premium_cohorts"
    ]
    assert cohorts["pre_fix"]["n"] == 2
    assert cohorts["post_fix"]["n"] == 0
    # below the floor: estimates nulled at the source, denominators survive
    assert cohorts["pre_fix"]["sufficient"] is False
    assert cohorts["pre_fix"]["mean_clv_log"] is None
    assert cohorts["pre_fix"]["ci_low"] is None


def test_premium_cohort_naive_mint_time_is_read_as_utc() -> None:
    # The DB is TIMESTAMPTZ (aware); a naive minted_at can only come from a
    # caller bug or a fixture — read it as UTC instead of raising.
    naive_pre = trusted_minted(0.02, datetime(2026, 7, 6, 12, 0, 0))  # noqa: DTZ001
    cohorts = live_evidence_report([naive_pre], ml_threshold=None, min_n=1)["trusted_clv_ci"][
        "premium_cohorts"
    ]
    assert cohorts["pre_fix"]["n"] == 1


# ===== Task 8 probe (2026-07-11): Monte Carlo zero-edge null record =============


def mc_row(odds: float, units: float, clv: float = 0.01) -> SettledPickRow:
    """A trusted settled row with a flat-stake outcome of ``units``."""
    return row(
        clv=clv,
        closing_anchor="pinnacle",
        has_snapshot=True,
        close_independent=True,
        decimal_odds=odds,
        stake=10.0,
        pnl=units * 10.0,
    )


def test_mc_null_p_luck_matches_analytic_probability_and_is_deterministic() -> None:
    # 3 winning even-money picks: observed +3 units. Under the zero-edge null
    # each wins w.p. 1/2, and only the all-win path reaches >= +3 -> p = 0.125.
    rows = [mc_row(2.0, 1.0) for _ in range(3)]
    mc = mc_null_record(rows, min_n=1)
    assert mc["n"] == 3
    assert mc["sims"] == 10_000
    assert mc["observed_units"] == pytest.approx(3.0)
    assert mc["p_luck"] == pytest.approx(0.125, abs=0.015)
    # deterministic: the fixed seed reproduces the exact same p
    assert mc_null_record(rows, min_n=1) == mc


def test_mc_null_certain_outcome_has_p_luck_one() -> None:
    # An all-lost record: EVERY null sim totals >= observed -> p_luck = 1.0
    # (the record is fully consistent with zero-edge luck).
    rows = [mc_row(2.0, -1.0) for _ in range(3)]
    mc = mc_null_record(rows, min_n=1)
    assert mc["observed_units"] == pytest.approx(-3.0)
    assert mc["p_luck"] == pytest.approx(1.0)


def test_mc_null_nulled_below_floor_and_skips_incomplete_rows() -> None:
    rows = [mc_row(2.0, 1.0) for _ in range(3)]
    thin = mc_null_record(rows, min_n=5)
    assert thin == {"n": 3, "observed_units": None, "p_luck": None, "sims": 10_000}
    # rows lacking pnl or odds cannot enter the flat-stake sample
    incomplete = [
        row(clv=0.01, closing_anchor="pinnacle", has_snapshot=True, pnl=None, decimal_odds=2.0),
        row(clv=0.01, closing_anchor="pinnacle", has_snapshot=True, pnl=1.0, decimal_odds=None),
    ]
    assert mc_null_record(incomplete, min_n=1)["n"] == 0


def test_mc_null_rides_the_trusted_subset_in_the_report() -> None:
    trusted = [mc_row(2.0, 1.0) for _ in range(3)]
    untrusted = [row(clv=0.01, closing_anchor="consensus", has_snapshot=True, decimal_odds=2.0)]
    rep = live_evidence_report(trusted + untrusted, ml_threshold=None, min_n=1)
    assert rep["mc_null"]["n"] == 3  # only the trusted subset is resampled
