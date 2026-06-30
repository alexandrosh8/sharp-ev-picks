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

#: CLV TAUTOLOGY epsilon — mirrors #137 (app.edge.value.CLV_TAUTOLOGY_EPS) and the
#: headline path (app.storage.repositories.CLV_TAUTOLOGY_EPS), kept local so this
#: module stays stdlib-pure. When a settled pick's CLOSE fair equals its PICK-TIME
#: fair (the SAME archived sharp line reused at pick-time and close-time), clv_log =
#: ln(fill_eff * closing_fair) merely re-encodes the pick-time edge — a TAUTOLOGY,
#: not independent close evidence. The persisted close_independent_of_fill flag was
#: fill-book-only for legacy rows, so an unmoved close that just ECHOES the pick-time
#: anchor would read as independent here without this guard. 1e-3 = the 4-dp
#: archived-line resolution.
CLV_TAUTOLOGY_EPS = 1e-3


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
    # Sport key of the pick (e.g. "soccer", "basketball_nba") for the per-sport
    # evidence split. None = dimension not joined (pre-feature row / pure-test
    # construction); the report then omits the by_sport grouping entirely, the
    # same feature-detected contract as anchor_type.
    sport: str | None = None
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
    # CLOSE-vs-PICK fair, for the TAUTOLOGY guard (mirrors #137 —
    # app.edge.value.close_moved_from_pick_fair / persisted_close_independent):
    # the persisted close_independent_of_fill flag was fill-BOOK-only for legacy
    # rows, so a close that merely ECHOES the pick-time sharp anchor (closing_fair
    # == model_probability, the SAME archived line at pick- and close-time) reads as
    # independent there even though its clv_log just re-encodes the pick-time edge.
    # Both are feature-detected (None = column absent / unknowable fair); a tautology
    # can only be PROVEN when BOTH are present, so None on either side is NEVER
    # treated as tautological (conservative, exactly like the persisted guard).
    closing_fair_probability: float | None = None
    model_probability: float | None = None  # the pick-time fair (1/fair_odds anchor)
    # P2-2 devig-fallback provenance: did the configured devig method fall back to
    # multiplicative at MINT / at CLOSE? When they DISAGREE (exactly one fell back)
    # the mint and close fairs used different effective methods, so the CLV is a
    # devig-method artifact, not a real line move — excluded from the trusted
    # subset. Feature-detected: None on either side = symmetric (not excluded).
    mint_devig_fell_back: bool | None = None
    close_devig_fell_back: bool | None = None

    @property
    def devig_fallback_asymmetric(self) -> bool:
        """True when exactly one of mint/close devig fell back to multiplicative.

        Conservative: a None on either side (provenance not recorded) is treated
        as SYMMETRIC, so historical rows are never excluded on this basis."""
        if self.mint_devig_fell_back is None or self.close_devig_fell_back is None:
            return False
        return self.mint_devig_fell_back != self.close_devig_fell_back

    @property
    def is_tautological_close(self) -> bool:
        """The CLOSE fair equals the PICK-TIME fair (the identical archived line).

        clv_log is then a TAUTOLOGY that re-encodes the pick-time edge, NOT
        independent close evidence — mirrors #137 (app.edge.value.
        close_moved_from_pick_fair, inverted) and the headline path
        (app.storage.repositories._clv_row_is_tautological). Only PROVABLE when a
        clv_log AND both fair probabilities are present; a None on either side
        (feature-detected absent / unknowable fair) is never treated as
        tautological. A row with no CLV (clv_log is None) carries no close to judge.
        """
        if self.clv_log is None:
            return False
        if self.closing_fair_probability is None or self.model_probability is None:
            return False
        return abs(self.closing_fair_probability - self.model_probability) <= CLV_TAUTOLOGY_EPS

    @property
    def sharp_close(self) -> bool:
        """A TRUSTED close for honest CLV: snapshot-sourced (not a poll-time
        revalidation fallback), anchored by a named sharp book (not a soft-book
        consensus median), independent of the fill book — the close anchor
        is NOT the pick's own fill book (a circular self-priced close is fake
        CLV, |clv_log|~0, and is what masked the -EV) — AND non-tautological:
        the close fair MOVED from the pick-time fair (an identical archived line
        re-encodes the pick-time edge — fake CLV #137). `close_independent_of_fill
        is False` and a proven tautology each EXCLUDE; None / unknowable-fair
        (pre-column) is treated as not-proven-circular and not-proven-tautological
        so historical sharp closes are unchanged. These are the closes whose CLV
        the platform can stand behind."""
        return (
            self.has_snapshot_close
            and self.closing_anchor_type in _SHARP_CLOSE_ANCHORS
            and self.close_independent_of_fill is not False
            and not self.is_tautological_close
            # P2-2: an asymmetric mint/close devig fallback is a method artifact.
            and not self.devig_fallback_asymmetric
        )


def _stratum_stats(rows: Sequence[SettledPickRow], min_n: int) -> dict[str, Any]:
    """Aggregates for one stratum — every estimate rides with its n."""
    # CLV-2: a CIRCULAR close (close_independent_of_fill is False — the pick's own
    # fill book pricing its own close, |clv_log|~0 fake CLV) OR a TAUTOLOGICAL close
    # (is_tautological_close — the close fair equals the pick-time fair, the SAME
    # archived line re-encoding the pick-time edge, #137) must NOT enter the CLV or
    # beat-close samples of ANY stratum; either would drag a per-anchor mean toward a
    # mechanical zero (or a fabricated value). Only a definite False / proven tautology
    # excludes; None (pre-column / unknown) and unknowable-fair are treated as
    # not-proven-circular and not-proven-tautological. pnl_rows is left untouched —
    # realized P&L is real regardless of how the close was priced.
    clv_rows = [
        r
        for r in rows
        if r.clv_log is not None
        and r.close_independent_of_fill is not False
        and not r.is_tautological_close
    ]
    pnl_rows = [r for r in rows if r.pnl is not None]
    beat_rows = [
        r
        for r in rows
        if r.beat_close is not None
        and r.close_independent_of_fill is not False
        and not r.is_tautological_close
    ]

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
    by_sport: dict[str, list[SettledPickRow]] = {}
    for row in rows:
        by_score.setdefault(_score_bucket(row.value_filter_score, ml_threshold), []).append(row)
        by_tier.setdefault(row.tier, []).append(row)
        if row.anchor_type is not None:
            by_anchor.setdefault(row.anchor_type, []).append(row)
        if row.closing_anchor_type is not None:
            by_close_anchor.setdefault(row.closing_anchor_type, []).append(row)
        if row.sport is not None:
            by_sport.setdefault(row.sport, []).append(row)
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
        # PER-SPORT evidence (Batch 3): each sport accumulates its OWN CLV/ROI on
        # its OWN n — a thin/experimental sport (e.g. basketball, shadow-only) can
        # never borrow another sport's sufficiency (min-n suppression is per
        # stratum). None = no row carries a sport key (feature-detected, mirrors
        # by_anchor), distinct from an empty grouping.
        "by_sport": (
            {k: _stratum_stats(v, min_n) for k, v in sorted(by_sport.items())} if by_sport else None
        ),
        "sharp_close": _stratum_stats(sharp_rows, min_n),
    }


@dataclass(frozen=True)
class SportMarketClvGate:
    """Per-(sport, market) CLV-READINESS gate — a DOCUMENTED policy scaffold that
    is default-OFF and shadow-only. It NEVER promotes anything on its own.

    A (sport, market) is promotion-READY (eligible to leave the experimental
    shadow tier and earn alerts) ONLY when, on its OWN trusted sharp-close sample:

      - ``n_sharp_close >= min_n_sharp_close`` — enough genuine, independent sharp
        closes to measure CLV at all;
      - the sport-scoped sharp stake-weighted CLV is positive by more than
        ``min_clv_sigma`` standard errors (the held-out > 2 SE doctrine bar,
        measured PER sport/market — never borrowed from football);
      - the beat-close rate's CI lower bound exceeds ``min_beat_close_ci_lower``
        (a coin-flip beat rate is no edge).

    ``enabled`` defaults False: this is reporting-only scaffolding. No code path
    flips ``enabled`` or auto-promotes a sport; an operator must both enable the
    gate AND the evidence must clear every bar. Promotion stays a deliberate,
    human, ADR-logged act. Pure: stdlib only, no env/DB/HTTP — policy enters as a
    frozen dataclass from the composition root, like every other gate here.
    """

    enabled: bool = False
    min_n_sharp_close: int = 500
    min_clv_sigma: float = 2.0
    min_beat_close_ci_lower: float = 0.5

    def is_ready(
        self,
        *,
        n_sharp_close: int,
        sharp_clv_mean: float | None,
        sharp_clv_se: float | None,
        beat_close_ci_lower: float | None,
    ) -> bool:
        """True ONLY if the gate is enabled AND every readiness bar is cleared.

        Disabled (the default) always returns False — the scaffold cannot promote.
        A missing/degenerate input (None mean/SE, non-positive SE, None CI bound)
        is treated as NOT ready: the gate fails closed, never open.
        """
        if not self.enabled:
            return False
        if n_sharp_close < self.min_n_sharp_close:
            return False
        if sharp_clv_se is None or sharp_clv_se <= 0.0:
            return False
        if sharp_clv_mean is None or sharp_clv_mean <= self.min_clv_sigma * sharp_clv_se:
            return False
        return beat_close_ci_lower is not None and (
            beat_close_ci_lower > self.min_beat_close_ci_lower
        )
