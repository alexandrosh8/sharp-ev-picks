"""Scheduled calibration-drift monitor (P2 ops).

Identity-calibration of the devigged fair_prob anchor is load-bearing for every
edge (the platform runs NO recalibration haircut precisely because the
walk-forward recalibration-gain detector says one would not transfer
out-of-sample). That verdict is a property of the DATA, and data drifts. The job
under test re-runs the SAME pure detector (app.backtesting.calibration.
walk_forward_beta_gain) on settled picks and DISPATCHES a deduped operator alert
if the verdict ever flips — it never auto-retrains.

The detector math is exhaustively tested in test_calibration_walkforward.py;
here we test the job's WIRING: it calls the real detector, alerts on drift,
dedupes a repeat, and is a safe no-op with no settled data / no dispatcher.
"""

from datetime import UTC, datetime

import numpy as np

from app.backtesting.calibration import (
    CalibrationObservation,
    RecalibrationGainReport,
)
from app.maintenance import calibration_drift as cd

_NOW = datetime(2026, 6, 30, 5, 30, tzinfo=UTC)


# --- synthetic settled-data periods (mirrors test_calibration_walkforward) --- #


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return np.log(p / (1 - p))


def _period(rng: np.random.Generator, n: int, shrink: float) -> list[CalibrationObservation]:
    """`shrink == 1.0` => perfectly calibrated; `shrink < 1.0` => overconfident
    (the case a recalibration would fix → the detector must flag drift)."""
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
    return [(f"2026-{i + 1:02d}", _period(rng, n, shrink)) for i in range(k)]


class _FakeDispatcher:
    """Records dispatched alerts; never touches the network."""

    def __init__(self) -> None:
        self.sent: list = []

    async def dispatch(self, alert):  # type: ignore[no-untyped-def]
        self.sent.append(alert)
        return None


def _drift_report() -> RecalibrationGainReport:
    return RecalibrationGainReport(
        n_folds=3,
        n_total=12000,
        insufficient=False,
        pooled_identity_log_loss=0.60,
        pooled_recal_log_loss=0.58,
        pooled_oos_gain=0.02,
        pooled_rel_gain_pct=3.3,
        warrants_recalibration=True,
        folds=(),
    )


# --- pure alert builder ----------------------------------------------------- #


def test_drift_alert_is_decision_support_and_dedupes_per_day() -> None:
    alert = cd.drift_alert(_drift_report(), _NOW)
    assert cd._DRIFT_CODE in alert.pick_id
    # informational only — never presents betting/retraining as automatic
    assert "no bets" in alert.body.lower()
    assert "operator" in alert.body.lower()
    # dedupe key is per-DAY: a same-day re-run is suppressed by the store; a new
    # day re-alerts (the operator is reminded an unresolved drift persists).
    same_day = cd.drift_alert(_drift_report(), _NOW.replace(hour=23, minute=59))
    assert alert.dedupe_key == same_day.dedupe_key
    next_day = cd.drift_alert(_drift_report(), datetime(2026, 7, 1, 5, 30, tzinfo=UTC))
    assert alert.dedupe_key != next_day.dedupe_key


# --- job: real detector wiring ---------------------------------------------- #


async def test_job_dispatches_alert_on_drift(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # overconfident settled data => the REAL detector warrants recalibration
    async def fake_load(session_factory, now=None):  # type: ignore[no-untyped-def]
        return _periods(0.6, seed=2)

    monkeypatch.setattr(cd, "load_calibration_periods", fake_load)
    disp = _FakeDispatcher()
    report = await cd.calibration_drift_job(object(), dispatcher=disp, now=_NOW)
    assert report is not None and report.warrants_recalibration is True
    assert len(disp.sent) == 1
    assert cd._DRIFT_CODE in disp.sent[0].pick_id


async def test_job_no_alert_when_still_calibrated(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_load(session_factory, now=None):  # type: ignore[no-untyped-def]
        return _periods(1.0, seed=1)  # well calibrated

    monkeypatch.setattr(cd, "load_calibration_periods", fake_load)
    disp = _FakeDispatcher()
    report = await cd.calibration_drift_job(object(), dispatcher=disp, now=_NOW)
    assert report is not None and report.warrants_recalibration is False
    assert disp.sent == []


async def test_job_dedupes_repeat_same_day(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A real dispatcher with an in-memory idempotency store must suppress the
    # SAME-day second run (e.g. a restart re-firing the daily job).
    from app.notifications.dedupe import InMemoryIdempotencyStore
    from app.notifications.dispatcher import AlertDispatcher

    class _RecordingSink:
        name = "recording"
        configured = True

        def __init__(self) -> None:
            self.count = 0

        async def send(self, alert) -> bool:  # type: ignore[no-untyped-def]
            self.count += 1
            return True

    async def fake_load(session_factory, now=None):  # type: ignore[no-untyped-def]
        return _periods(0.6, seed=2)

    monkeypatch.setattr(cd, "load_calibration_periods", fake_load)
    sink = _RecordingSink()
    dispatcher = AlertDispatcher(sinks=[sink], store=InMemoryIdempotencyStore())
    await cd.calibration_drift_job(object(), dispatcher=dispatcher, now=_NOW)
    await cd.calibration_drift_job(object(), dispatcher=dispatcher, now=_NOW)
    assert sink.count == 1  # second same-day dispatch deduped by the store


# --- job: graceful degradation ---------------------------------------------- #


async def test_job_no_settled_data_is_safe_noop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_load(session_factory, now=None):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(cd, "load_calibration_periods", fake_load)
    disp = _FakeDispatcher()
    report = await cd.calibration_drift_job(object(), dispatcher=disp, now=_NOW)
    assert report is None
    assert disp.sent == []


async def test_job_insufficient_data_skips(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Some settled rows, but not enough to clear min_train/min_test => the
    # detector reports `insufficient` and NO alert is dispatched.
    async def fake_load(session_factory, now=None):  # type: ignore[no-untyped-def]
        return _periods(0.6, seed=2, k=2, n=50)

    monkeypatch.setattr(cd, "load_calibration_periods", fake_load)
    disp = _FakeDispatcher()
    report = await cd.calibration_drift_job(object(), dispatcher=disp, now=_NOW)
    assert report is not None and report.insufficient is True
    assert disp.sent == []


async def test_job_none_session_factory_is_safe() -> None:
    disp = _FakeDispatcher()
    assert await cd.calibration_drift_job(None, dispatcher=disp, now=_NOW) is None
    assert disp.sent == []


async def test_job_load_failure_degrades_gracefully(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def boom(session_factory, now=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")

    monkeypatch.setattr(cd, "load_calibration_periods", boom)
    disp = _FakeDispatcher()
    report = await cd.calibration_drift_job(object(), dispatcher=disp, now=_NOW)
    assert report is None  # logged + skipped, never raised
    assert disp.sent == []


async def test_job_no_dispatcher_is_safe(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_load(session_factory, now=None):  # type: ignore[no-untyped-def]
        return _periods(0.6, seed=2)

    monkeypatch.setattr(cd, "load_calibration_periods", fake_load)
    # drift present but no channel wired: still runs, returns the report, no raise
    report = await cd.calibration_drift_job(object(), dispatcher=None, now=_NOW)
    assert report is not None and report.warrants_recalibration is True
