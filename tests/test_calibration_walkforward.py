"""Walk-forward recalibration-gain detector (pure module).

The question this answers: would ANY leakage-free, fit-on-past recalibration of
the devigged fair_prob beat the identity out-of-sample? On an already-calibrated
sharp anchor the honest answer is "no" — and the detector must SAY no (so the
platform never bolts on a noise-fitting 'haircut'); but if a real, stable
miscalibration ever appears in the data, the same detector must flag it.

Pure math — no DB/IO. Synthetic data with a fixed RNG seed for determinism.
"""

import numpy as np

from app.backtesting.calibration import (
    CalibrationObservation,
    RecalibrationGainReport,
    walk_forward_beta_gain,
)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return np.log(p / (1 - p))


def _period(rng: np.random.Generator, n: int, shrink: float) -> list[CalibrationObservation]:
    """A period of n observations. `shrink` distorts the STATED prob away from
    truth: true_p = sigmoid(shrink * logit(stated_p)). shrink == 1.0 => the
    stated prob IS the truth (perfectly calibrated); shrink < 1.0 => the stated
    prob is OVERCONFIDENT (too extreme), the case a haircut would fix."""
    stated = rng.uniform(0.05, 0.95, size=n)
    true_p = _sigmoid(shrink * _logit(stated))
    won = rng.uniform(size=n) < true_p
    return [
        CalibrationObservation(fair_prob=float(s), won=bool(w))
        for s, w in zip(stated, won, strict=True)
    ]


def _periods(
    shrink: float, *, seed: int, k: int = 5, n: int = 4000
) -> list[tuple[str, list[CalibrationObservation]]]:
    rng = np.random.default_rng(seed)
    return [(f"S{i}", _period(rng, n, shrink)) for i in range(k)]


def test_well_calibrated_data_does_not_warrant_recalibration() -> None:
    report = walk_forward_beta_gain(_periods(1.0, seed=1))
    assert isinstance(report, RecalibrationGainReport)
    assert report.insufficient is False
    assert report.n_folds >= 1
    # The recalibrator should land on ~identity and earn no out-of-sample gain.
    assert report.warrants_recalibration is False
    assert report.pooled_rel_gain_pct is not None
    assert report.pooled_rel_gain_pct < 0.5  # below the warrant threshold = noise
    for fold in report.folds:
        assert fold.slope is not None
        assert 0.8 < fold.slope < 1.2  # converged to identity


def test_overconfident_data_warrants_recalibration() -> None:
    # stated prob too extreme (shrink 0.6) => a fit-on-past beta with slope ~0.6
    # genuinely improves out-of-sample log-loss; the detector must catch it.
    report = walk_forward_beta_gain(_periods(0.6, seed=2))
    assert report.insufficient is False
    assert report.warrants_recalibration is True
    assert report.pooled_oos_gain is not None and report.pooled_oos_gain > 0.0
    assert report.pooled_rel_gain_pct is not None and report.pooled_rel_gain_pct > 1.0
    # the fitted slope should pull the over-extreme probs inward (slope < 1)
    assert all(f.slope is not None and f.slope < 0.9 for f in report.folds)


def test_insufficient_data_marks_insufficient_and_makes_no_claim() -> None:
    # Two tiny periods, nothing clears min_train => no eligible fold.
    rng = np.random.default_rng(3)
    tiny = [("S0", _period(rng, 50, 1.0)), ("S1", _period(rng, 50, 1.0))]
    report = walk_forward_beta_gain(tiny, min_train=2000, min_test=200)
    assert report.insufficient is True
    assert report.n_folds == 0
    assert report.warrants_recalibration is False
    assert report.pooled_oos_gain is None


def test_fit_uses_only_past_periods_no_lookahead() -> None:
    # A fold's result must not depend on periods that come AFTER it. Corrupt the
    # final period heavily; the earlier folds must be byte-identical to a run
    # that never saw the corrupt future period.
    good = _periods(1.0, seed=4, k=4, n=4000)
    corrupt_future = good + [("S_BAD", _period(np.random.default_rng(99), 4000, 0.2))]
    full = walk_forward_beta_gain(corrupt_future)
    truncated = walk_forward_beta_gain(good)
    trunc_by_period = {f.period: f for f in truncated.folds}
    overlap = 0
    for fold in full.folds:
        if fold.period in trunc_by_period:
            overlap += 1
            t = trunc_by_period[fold.period]
            assert fold.slope == t.slope
            assert fold.intercept == t.intercept
            assert fold.oos_log_loss_gain == t.oos_log_loss_gain
    assert overlap >= 1  # the leakage guard was actually exercised
