"""Score-stratified live CLV/ROI evidence over settled picks. Pure module.

The instrument for the VALUE_ML_FILTER flip decision (and, later, the
consensus-anchor verdict): as settled + CLV-revalidated picks accumulate,
this stratifies their CLV and ROI by

  (a) ML value-filter score bucket — >= q* / < q* / unscored (q* is the
      manifest's frozen operating point; passed in by the caller, never
      read from disk here);
  (b) tier — premium (alerted) vs volume (shadow);
  (c) anchor_type — only when the caller's schema carries it (the column
      is being added by a separate migration; rows default to None and the
      grouping is omitted entirely until real values exist).

Honesty rules (binding, mirrored by the dashboard panel):
  - every stratum reports its n (and n_clv / n_roi denominators);
  - a stratum with n_clv < min_n is marked insufficient AND its point
    estimates are nulled at the source — no consumer of GET /performance
    can read noise-level numbers for an insufficient stratum, whether or
    not it honors the flag;
  - sufficiency is judged on n_clv (CLV is the evaluation currency); ROI
    can therefore render with a thinner pnl sample — consumers must
    eyeball n_roi before leaning on a stratum's ROI;
  - aggregates are evidence, never a profit promise.

Pure: stdlib/math only — DB reads live in app/storage/repositories.py and
the composition happens in the route (app/api/routes.py).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.backtesting.calibration import (
    CalibrationObservation,
    CalibrationReport,
    calibration_report,
)

#: Below this many CLV observations a stratum is "insufficient" — the
#: dashboard shows the state instead of point estimates.
MIN_STRATUM_N = 50

#: Close anchors that make a close TRUSTABLE for honest CLV — a NAMED sharp
#: book priced it, not a soft-book consensus median. Mirrors the persisted
#: anchor_type values app/edge/value.anchor_type_for emits (pinnacle / sharp);
#: kept local so this module stays stdlib-pure. "consensus" is deliberately
#: excluded (a soft-book median close is not a sharp close).
_SHARP_CLOSE_ANCHORS = ("pinnacle", "sharp")


@dataclass(frozen=True)
class SettledPickRow:
    """One settled pick, already reduced to plain floats at the DB boundary."""

    tier: str
    value_filter_score: float | None
    clv_log: float | None  # None = never revalidated against a close
    beat_close: bool | None
    stake: float  # recommended stake (same weighting as performance_report)
    pnl: float | None  # None = outcome recorded without a pnl figure
    anchor_type: str | None = None  # CREATION anchor — None = column absent or value missing
    # CLOSE-side provenance (the anchor that produced closing_fair / clv_log):
    closing_anchor_type: str | None = None  # pinnacle / sharp / consensus; None = unknown
    has_snapshot_close: bool = False  # closing_odds present => a true snapshot close,
    #                                   not a poll-time revalidation fallback
    # INDEPENDENCE provenance (P0-1/P0-3): True  = the close anchor book differs
    # from the fill book (genuine, independent close); False = the close was
    # anchored by the pick's OWN fill book (CIRCULAR — closing == fill,
    # |clv_log|~0 — the fake-CLV that masked the -EV); None = unknown
    # (pre-column row, feature-detected). Only a definite False excludes.
    close_independent_of_fill: bool | None = None

    @property
    def sharp_close(self) -> bool:
        """A TRUSTED close for honest CLV: snapshot-sourced (not a poll-time
        revalidation fallback), anchored by a named sharp book (not a soft-book
        consensus median), AND independent of the fill book — the close anchor
        is NOT the pick's own fill book (a circular self-priced close is fake
        CLV, |clv_log|~0, and is what masked the -EV). `close_independent_of_fill
        is False` is the ONLY value that excludes; None (unknown / pre-column) is
        treated as not-proven-circular so historical sharp closes are unchanged.
        These are the closes whose CLV the platform can stand behind."""
        return (
            self.has_snapshot_close
            and self.closing_anchor_type in _SHARP_CLOSE_ANCHORS
            and self.close_independent_of_fill is not False
        )


def _stratum_stats(rows: Sequence[SettledPickRow], min_n: int) -> dict[str, Any]:
    """Aggregates for one stratum — every estimate rides with its n."""
    clv_rows = [r for r in rows if r.clv_log is not None]
    pnl_rows = [r for r in rows if r.pnl is not None]
    beat_rows = [r for r in rows if r.beat_close is not None]

    mean_clv: float | None = None
    sw_clv: float | None = None
    if clv_rows:
        mean_clv = sum(r.clv_log for r in clv_rows if r.clv_log is not None) / len(clv_rows)
        stake_total = sum(r.stake for r in clv_rows)
        if stake_total > 0.0:
            sw_clv = (
                sum(r.stake * r.clv_log for r in clv_rows if r.clv_log is not None) / stake_total
            )
    roi: float | None = None
    staked = sum(r.stake for r in pnl_rows)
    if pnl_rows and staked > 0.0:
        roi = sum(r.pnl for r in pnl_rows if r.pnl is not None) / staked
    # Sufficiency is judged on the CLV sample — CLV is the evaluation
    # currency; ROI at these n is noise either way. (n_roi can still be
    # thinner than n_clv in a sufficient stratum — consumers eyeball it.)
    sufficient = len(clv_rows) >= min_n
    if not sufficient:
        # Insufficient stratum: estimates are nulled AT THE SOURCE so no
        # consumer can mistake noise for evidence — only the denominators
        # and the flag survive (the dashboard renders the state from those).
        mean_clv = sw_clv = roi = None
        beat_rate: float | None = None
    else:
        beat_rate = (
            sum(1 for r in beat_rows if r.beat_close) / len(beat_rows) if beat_rows else None
        )
    return {
        "n": len(rows),
        "n_clv": len(clv_rows),
        "n_roi": len(pnl_rows),
        "mean_clv_log": mean_clv,
        "stake_weighted_clv_log": sw_clv,
        "beat_close_rate": beat_rate,
        "roi": roi,
        "sufficient": sufficient,
    }


def _score_bucket(score: float | None, q_star: float | None) -> str:
    if score is None:
        return "unscored"
    if q_star is None:
        return "scored"  # no operating point known: one undivided bucket
    return "score_ge_q" if score >= q_star else "score_lt_q"


def meta_model_calibration(
    rows: Sequence[SettledPickRow], *, min_n: int = MIN_STRATUM_N
) -> CalibrationReport:
    """Is the value-filter meta-model's P(beats close) CALIBRATED in production?

    For every settled pick carrying BOTH a meta-model score and a realized
    beat-close label, score `value_filter_score` (predicted P beat-close) against
    the actual outcome. Well calibrated means a 0.7 reads as a 70% beat-close
    rate. Diagnostic only — held-out CLV stays the staking arbiter (ADR-0017);
    this closes with REAL production outcomes the loop the offline calibrator
    bake-off opened on the trainer's holdout."""
    observations = [
        CalibrationObservation(fair_prob=r.value_filter_score, won=r.beat_close)
        for r in rows
        if r.value_filter_score is not None and r.beat_close is not None
    ]
    return calibration_report(observations, min_n=min_n)


def live_evidence_report(
    rows: Sequence[SettledPickRow],
    *,
    ml_threshold: float | None,
    min_n: int = MIN_STRATUM_N,
) -> dict[str, Any]:
    """Stratified live evidence over settled picks (see module docstring).

    `ml_threshold` is the manifest's frozen operating point q* (None when no
    artifact is configured: scores then fall into one "scored" bucket).
    `by_anchor` is None — meaning "dimension not available" — until at least
    one row carries an anchor_type value; consumers must distinguish that
    from an empty grouping.
    """
    if ml_threshold is not None and not math.isfinite(ml_threshold):
        raise ValueError(f"ml_threshold must be finite, got {ml_threshold}")

    by_score: dict[str, list[SettledPickRow]] = {}
    by_tier: dict[str, list[SettledPickRow]] = {}
    by_anchor: dict[str, list[SettledPickRow]] = {}
    by_close_anchor: dict[str, list[SettledPickRow]] = {}
    for row in rows:
        by_score.setdefault(_score_bucket(row.value_filter_score, ml_threshold), []).append(row)
        by_tier.setdefault(row.tier, []).append(row)
        if row.anchor_type is not None:
            by_anchor.setdefault(row.anchor_type, []).append(row)
        if row.closing_anchor_type is not None:
            by_close_anchor.setdefault(row.closing_anchor_type, []).append(row)
    # The TRUSTED subset: closes the platform can stand behind for honest CLV
    # (a genuine sharp snapshot close, not a consensus median or a poll-time
    # revalidation fallback). Always reported — n=0 honestly says "none yet".
    sharp_rows = [r for r in rows if r.sharp_close]

    cal = meta_model_calibration(rows, min_n=min_n)
    return {
        "n_settled": len(rows),
        "q_star": ml_threshold,
        "min_n": min_n,
        # Is the meta-model's P(beats close) calibrated against realized outcomes?
        "meta_model_calibration": {
            "n": cal.n,
            "insufficient": cal.insufficient,
            "log_loss": cal.log_loss,
            "brier": cal.brier,
            "ece": cal.ece,
            "base_rate": cal.base_rate,
            "mean_pred": cal.mean_pred,
        },
        "by_score": {k: _stratum_stats(v, min_n) for k, v in sorted(by_score.items())},
        "by_tier": {k: _stratum_stats(v, min_n) for k, v in sorted(by_tier.items())},
        # by_anchor stratifies on the CREATION anchor (the consensus-fallback
        # forward test); by_close_anchor stratifies on the anchor that produced
        # each CLOSE — the anchor CLV is actually measured against, so a
        # pinnacle-created/consensus-closed pick lands in the consensus stratum.
        "by_anchor": (
            {k: _stratum_stats(v, min_n) for k, v in sorted(by_anchor.items())}
            if by_anchor
            else None
        ),
        "by_close_anchor": (
            {k: _stratum_stats(v, min_n) for k, v in sorted(by_close_anchor.items())}
            if by_close_anchor
            else None
        ),
        "sharp_close": _stratum_stats(sharp_rows, min_n),
    }
