"""Calibration of devigged fair-anchor probabilities vs realized outcomes.

Pure module (math/stdlib only) — DB reads live in app/storage/repositories.py
or a report script's composition root, never here.

DOCTRINE NOTE — this is a DIAGNOSTIC, not the live validator. CLV stays the
evaluation currency for the line-shopping edge (app/backtesting/clv.py); this
only checks the secondary question "when the anchor says X%, do those bets land
~X%?". It is computed over SETTLED PICKS, so it is pick-CONDITIONAL
(selection-biased toward selections where we found value): it answers "are the
fair probs on the bets I actually make well-calibrated?", NOT "is the anchor
calibrated unconditionally". A consumer must read it as a sanity check on the
fair-prob anchor, never as a profit signal.

Honesty (mirrors app/backtesting/live_evidence.py): below MIN_CALIBRATION_N
binary observations the report is `insufficient` and every point estimate is
nulled at the source — no consumer can read noise-level calibration numbers.

Metrics (all on a binary outcome y in {0,1} and predicted P(win) p):
  - log_loss  = -mean( y·ln p + (1-y)·ln(1-p) )   [strictly proper, local;
                the penaltyblog author's preferred score — pena.lt/y blog]
  - ignorance =  log_loss / ln(2)  — the same score in BITS (Good 1952), the
                units penaltyblog.metrics.ignorance reports
  - brier     =  mean( (p - y)^2 )
  - ece       =  sum_bins (n_b/N)·|mean_pred_b - observed_b|   (n-weighted)
  - mce       =  max_bins |mean_pred_b - observed_b|
  - reliability bins: equal-width [0,1] partition; each carries its own n so
    a consumer can see which probability ranges are thin.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Below this many binary observations a (sub)report is "insufficient" — point
#: estimates are nulled so noise can never read as a calibration verdict. Same
#: floor as the live-evidence CLV strata.
MIN_CALIBRATION_N = 50

#: Clamp predicted probabilities into (eps, 1-eps) before log-loss so a 0/1
#: prediction can never produce an infinite score.
_EPS = 1e-12


@dataclass(frozen=True)
class CalibrationObservation:
    """One settled, binary-outcome pick reduced to plain floats at the DB
    boundary. Non-binary settlements (push/void/quarter-line half_*) are
    excluded upstream — they have no win/lose label to calibrate against."""

    fair_prob: float  # devigged anchor P(selection wins), expected in (0, 1)
    won: bool  # True = selection won, False = lost


@dataclass(frozen=True)
class BetBandObservation:
    """One settled, binary-outcome pick for the CLAIMED-FAIR reliability monitor
    (P1-1). `claimed_fair` is the probability the strategy claimed when it took
    the bet (``Pick.model_probability``); `won` is the realized outcome
    (``ResultTracking.outcome == 'won'``); `fill_odds` is the price actually
    taken, used to scope the report to the odds band the platform actually bets.
    Reduced to plain floats at the DB boundary (this stays a pure module)."""

    claimed_fair: float  # P(selection wins) claimed at bet time, expected in (0, 1)
    won: bool
    fill_odds: float  # decimal odds the bet was struck at (> 1.0)


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    n: int
    mean_pred: float | None  # mean predicted prob in the bin (None if empty)
    observed: float | None  # observed win frequency in the bin (None if empty)


@dataclass(frozen=True)
class CalibrationReport:
    n: int  # binary observations scored
    insufficient: bool  # n < min_n -> all estimates below are None
    log_loss: float | None
    # ignorance score (Good 1952) = log_loss in BITS = log_loss / ln(2). The same
    # strictly-proper local score the penaltyblog author prefers, in the units his
    # penaltyblog.metrics.ignorance reports — exposed beside Brier so the report
    # carries both proper scores (nats + bits) and the calibration diagnostics.
    ignorance: float | None
    brier: float | None
    ece: float | None
    mce: float | None
    base_rate: float | None  # observed overall win frequency
    mean_pred: float | None  # mean predicted probability
    bins: tuple[ReliabilityBin, ...]


def _clamp01(p: float) -> float:
    return min(1.0 - _EPS, max(_EPS, p))


def calibration_report(
    observations: Sequence[CalibrationObservation],
    *,
    n_bins: int = 10,
    min_n: int = MIN_CALIBRATION_N,
) -> CalibrationReport:
    """Score a set of (fair_prob, won) observations. Empty/insufficient input
    returns a report with `insufficient=True` and nulled estimates (n is always
    reported). `n_bins` equal-width reliability bins span [0, 1]."""
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")

    n = len(observations)
    if n < min_n:
        return CalibrationReport(
            n=n,
            insufficient=True,
            log_loss=None,
            ignorance=None,
            brier=None,
            ece=None,
            mce=None,
            base_rate=None,
            mean_pred=None,
            bins=(),
        )

    ys = [1.0 if o.won else 0.0 for o in observations]
    ps = [_clamp01(o.fair_prob) for o in observations]

    log_loss = (
        -sum(y * math.log(p) + (1.0 - y) * math.log(1.0 - p) for y, p in zip(ys, ps, strict=True))
        / n
    )
    ignorance = log_loss / math.log(2)  # log_loss in bits — Good's ignorance score
    brier = sum((p - y) ** 2 for y, p in zip(ys, ps, strict=True)) / n
    base_rate = sum(ys) / n
    mean_pred = sum(ps) / n

    # Equal-width bins over [0, 1]; the top edge is inclusive so p == 1.0 lands
    # in the last bin rather than overflowing. Interior left edges are subject
    # to float representation (e.g. int(0.3/0.1) == 2 because 0.3/0.1 ==
    # 2.9999...), so a prob exactly on a boundary can land one bin low — benign:
    # every obs is still counted once, and ECE/MCE use each bin's OWN mean_pred
    # (not the nominal edge), so aggregate metrics are unaffected.
    width = 1.0 / n_bins
    bin_pred_sum = [0.0] * n_bins
    bin_obs_sum = [0.0] * n_bins
    bin_count = [0] * n_bins
    for y, p in zip(ys, ps, strict=True):
        idx = min(n_bins - 1, int(p / width))
        bin_pred_sum[idx] += p
        bin_obs_sum[idx] += y
        bin_count[idx] += 1

    bins: list[ReliabilityBin] = []
    ece = 0.0
    mce = 0.0
    for i in range(n_bins):
        lo = i * width
        hi = (i + 1) * width
        cnt = bin_count[i]
        if cnt == 0:
            bins.append(ReliabilityBin(lo=lo, hi=hi, n=0, mean_pred=None, observed=None))
            continue
        mp = bin_pred_sum[i] / cnt
        ob = bin_obs_sum[i] / cnt
        gap = abs(mp - ob)
        ece += (cnt / n) * gap
        mce = max(mce, gap)
        bins.append(ReliabilityBin(lo=lo, hi=hi, n=cnt, mean_pred=mp, observed=ob))

    return CalibrationReport(
        n=n,
        insufficient=False,
        log_loss=log_loss,
        ignorance=ignorance,
        brier=brier,
        ece=ece,
        mce=mce,
        base_rate=base_rate,
        mean_pred=mean_pred,
        bins=tuple(bins),
    )


def _quantile_sorted(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted, non-empty sequence."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def bet_band_reliability(
    observations: Sequence[BetBandObservation],
    *,
    band_lo_q: float = 0.10,
    band_hi_q: float = 0.90,
    n_bins: int = 10,
    min_n: int = MIN_CALIBRATION_N,
) -> dict[str, Any]:
    """CLAIMED-FAIR reliability monitor (P1-1): does the strategy's claimed fair
    probability match the realized win-rate, IN THE ODDS BAND IT ACTUALLY BETS?

    Closes the loop the headline never did: from the probability claimed at bet
    time (``Pick.model_probability``) to the realized outcome
    (``ResultTracking.outcome``). Scoping to the actually-bet band keeps the
    verdict honest — a model can look "well calibrated" overall while being
    overconfident exactly in the prices it bets; the band is the inter-quantile
    range [band_lo_q, band_hi_q] of the population's own fill odds, so a stray
    longshot or heavy favourite cannot define (or escape) the band.

    MONITOR / REPORT ONLY — this is NOT a release gate and NOT a recalibration
    haircut (those are a separate modeling follow-up). It surfaces reliability +
    ECE beside ROI/CLV so a calibration drift is VISIBLE. Returns the band
    bounds, the in/out-of-band counts, and a CalibrationReport (its own
    insufficient-n honesty gate applies). Pure: stdlib/math only.
    """
    if not 0.0 <= band_lo_q < band_hi_q <= 1.0:
        raise ValueError(
            f"need 0 <= band_lo_q < band_hi_q <= 1, got ({band_lo_q}, {band_hi_q})"
        )
    odds = sorted(o.fill_odds for o in observations)
    band_lo = _quantile_sorted(odds, band_lo_q) if odds else None
    band_hi = _quantile_sorted(odds, band_hi_q) if odds else None
    in_band = (
        [o for o in observations if band_lo <= o.fill_odds <= band_hi]
        if band_lo is not None and band_hi is not None
        else []
    )
    report = calibration_report(
        [CalibrationObservation(fair_prob=o.claimed_fair, won=o.won) for o in in_band],
        n_bins=n_bins,
        min_n=min_n,
    )
    return {
        "n_total": len(observations),
        "n_in_band": len(in_band),
        "band_lo_q": band_lo_q,
        "band_hi_q": band_hi_q,
        "band_lo_odds": band_lo,
        "band_hi_odds": band_hi,
        "insufficient": report.insufficient,
        "n": report.n,
        "ece": report.ece,
        "mce": report.mce,
        "log_loss": report.log_loss,
        "brier": report.brier,
        "base_rate": report.base_rate,  # realized win-rate in band
        "mean_pred": report.mean_pred,  # mean claimed fair in band
        "bins": tuple(
            {
                "lo": b.lo,
                "hi": b.hi,
                "n": b.n,
                "mean_pred": b.mean_pred,
                "observed": b.observed,
            }
            for b in report.bins
        ),
    }


# --------------------------------------------------------------------------- #
# Walk-forward recalibration-gain detector
# --------------------------------------------------------------------------- #
# The "tail-bias haircut" question, answered honestly and reproducibly: would
# ANY leakage-free, fit-on-past recalibration of the devigged fair_prob beat the
# identity OUT-OF-SAMPLE? On an already-calibrated sharp anchor the answer is no
# — and this detector says no, so the platform never bolts on a noise-fitting
# haircut that would degrade log-loss and demote genuine +EV picks. If a real,
# stable miscalibration ever appears in the data, the same detector flags it
# (warrants_recalibration=True). It is the natural companion to the report-only
# bet_band_reliability monitor above: that one SHOWS drift; this one decides
# whether correcting it transfers to held-out data.
#
# Method: standard 2-parameter beta/Platt recalibration p' = sigmoid(a*logit(p)
# + b), fitted by exact Newton/IRLS on the CUMULATIVE PAST periods only and
# scored on the next (held-out) period. The closing line is never a feature
# (leakage rule) — only (fair_prob, won) pairs enter. numpy is used purely as a
# vectorised numeric kernel; the function takes no env/DB/HTTP.


@dataclass(frozen=True)
class RecalibrationFold:
    """One walk-forward fold: a beta recalibration fitted on all PRIOR periods
    and scored on this `period`. `oos_log_loss_gain` = identity - recal (a
    positive value means recalibration helped out-of-sample)."""

    period: Any
    n: int  # held-out observations scored in this fold
    slope: float | None  # beta a (a < 1 shrinks extreme probs inward)
    intercept: float | None  # beta b
    identity_log_loss: float | None  # log-loss of the raw fair_prob
    recal_log_loss: float | None  # log-loss after the fitted beta
    oos_log_loss_gain: float | None  # identity - recal


@dataclass(frozen=True)
class RecalibrationGainReport:
    n_folds: int
    n_total: int  # observations across all scored folds (or whole input if none)
    insufficient: bool  # True -> no eligible fold; every estimate below is None
    pooled_identity_log_loss: float | None
    pooled_recal_log_loss: float | None
    pooled_oos_gain: float | None  # n-weighted (identity - recal)
    pooled_rel_gain_pct: float | None  # 100 * pooled_oos_gain / pooled_identity
    warrants_recalibration: bool  # the verdict (see thresholds in the function)
    folds: tuple[RecalibrationFold, ...]


def _fit_beta(
    z: np.ndarray, y: np.ndarray, *, iters: int = 50, tol: float = 1e-10
) -> tuple[float, float]:
    """Logistic regression won ~ sigmoid(a*z + b) by exact Newton/IRLS on the
    2x2 Hessian. Starts at the identity (a=1, b=0); converges in a handful of
    iterations. Returns (a, b)."""
    a, b = 1.0, 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(a * z + b)))
        p = np.clip(p, _EPS, 1.0 - _EPS)
        g = p - y
        ga = float(np.mean(g * z))
        gb = float(np.mean(g))
        w = p * (1.0 - p)
        haa = float(np.mean(w * z * z)) + 1e-12
        hab = float(np.mean(w * z))
        hbb = float(np.mean(w)) + 1e-12
        det = haa * hbb - hab * hab
        if abs(det) < 1e-18:
            da, db = ga / haa, gb / hbb
        else:
            da = (hbb * ga - hab * gb) / det
            db = (haa * gb - hab * ga) / det
        a -= da
        b -= db
        if abs(da) < tol and abs(db) < tol:
            break
    return a, b


def _log_loss_arr(y: "np.ndarray", p: "np.ndarray") -> float:
    p = np.clip(p, _EPS, 1.0 - _EPS)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def walk_forward_beta_gain(
    periods: Sequence[tuple[Any, Sequence[CalibrationObservation]]],
    *,
    min_train: int = 2000,
    min_test: int = 200,
    min_warrant_rel_pct: float = 0.5,
    require_all_folds_positive: bool = True,
) -> RecalibrationGainReport:
    """Does a fit-on-past beta recalibration of fair_prob beat the identity
    out-of-sample? `periods` is a CHRONOLOGICALLY-ORDERED sequence of
    (label, observations). For each period whose cumulative history is at least
    `min_train` and whose own size is at least `min_test`, a beta is fitted on
    the prior periods and scored on this one. The verdict
    `warrants_recalibration` is True iff the pooled out-of-sample relative
    log-loss gain clears `min_warrant_rel_pct` AND (when
    `require_all_folds_positive`) every fold improved — so a single lucky fold
    or sub-threshold wobble never mints a haircut.

    Pure: numpy/stdlib only, no IO. Caller assembles the periods at a
    composition root (e.g. a report script grouping settled picks by season)."""
    folds: list[RecalibrationFold] = []
    hist: list[CalibrationObservation] = []
    wsum_id = 0.0  # n-weighted identity log-loss across scored folds
    wsum_rc = 0.0  # n-weighted recalibrated log-loss
    n_tot = 0
    for period, obs in periods:
        if len(hist) >= min_train and len(obs) >= min_test:
            z_tr = _logit_observations(hist)
            y_tr = np.array([1.0 if o.won else 0.0 for o in hist])
            a, b = _fit_beta(z_tr, y_tr)
            p_te = np.array([_clamp01(o.fair_prob) for o in obs])
            y_te = np.array([1.0 if o.won else 0.0 for o in obs])
            id_ll = _log_loss_arr(y_te, p_te)
            z_te = np.log(p_te / (1.0 - p_te))
            rc_ll = _log_loss_arr(y_te, 1.0 / (1.0 + np.exp(-(a * z_te + b))))
            n = len(obs)
            folds.append(
                RecalibrationFold(
                    period=period,
                    n=n,
                    slope=a,
                    intercept=b,
                    identity_log_loss=id_ll,
                    recal_log_loss=rc_ll,
                    oos_log_loss_gain=id_ll - rc_ll,
                )
            )
            wsum_id += id_ll * n
            wsum_rc += rc_ll * n
            n_tot += n
        hist.extend(obs)

    if not folds:
        return RecalibrationGainReport(
            n_folds=0,
            n_total=sum(len(o) for _, o in periods),
            insufficient=True,
            pooled_identity_log_loss=None,
            pooled_recal_log_loss=None,
            pooled_oos_gain=None,
            pooled_rel_gain_pct=None,
            warrants_recalibration=False,
            folds=(),
        )

    pooled_id = wsum_id / n_tot
    pooled_rc = wsum_rc / n_tot
    pooled_gain = pooled_id - pooled_rc
    pooled_rel = 100.0 * pooled_gain / pooled_id if pooled_id > 0 else 0.0
    all_pos = all((f.oos_log_loss_gain or 0.0) > 0.0 for f in folds)
    warrants = pooled_rel >= min_warrant_rel_pct and (
        all_pos if require_all_folds_positive else True
    )
    return RecalibrationGainReport(
        n_folds=len(folds),
        n_total=n_tot,
        insufficient=False,
        pooled_identity_log_loss=pooled_id,
        pooled_recal_log_loss=pooled_rc,
        pooled_oos_gain=pooled_gain,
        pooled_rel_gain_pct=pooled_rel,
        warrants_recalibration=warrants,
        folds=tuple(folds),
    )


def _logit_observations(obs: Sequence[CalibrationObservation]) -> "np.ndarray":
    p = np.array([_clamp01(o.fair_prob) for o in obs])
    return np.log(p / (1.0 - p))
