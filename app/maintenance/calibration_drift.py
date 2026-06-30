"""Scheduled calibration-drift monitor (P2 ops).

Identity-calibration of the devigged fair_prob anchor is load-bearing for EVERY
edge: the platform deliberately runs NO recalibration "haircut" because the
walk-forward recalibration-gain detector says one would not transfer
out-of-sample (docs/research/calibration-haircut-decision-2026-06-24.md). That
verdict is a PROPERTY OF THE DATA — and data drifts. Until now the detector ran
only via a MANUAL probe (scripts/ml/calibration_haircut_probe.py); nothing ran
it unattended, so a real, stable miscalibration could silently degrade pick
quality until a human happened to re-run the probe.

This module closes that hole. `calibration_drift_job` re-runs the SAME pure
detector (app.backtesting.calibration.walk_forward_beta_gain — the math is NOT
reimplemented here) on SETTLED picks on a daily cadence and, if the verdict ever
flips to `warrants_recalibration`, DISPATCHES a single deduped operator alert. It
NEVER auto-retrains — retraining stays a human/owner decision (model_versions is
written by a deliberate, reproducible script, not by a monitor).

Degrades gracefully: thin/absent settled data yields an `insufficient` report
(the common case while live data accrues) -> log + skip, no alert. The job NEVER
raises — a monitoring job must not crash the scheduler.

Read-only: it reads settled picks; it writes nothing and places no bets.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from itertools import groupby
from typing import Protocol

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtesting.calibration import (
    CalibrationObservation,
    RecalibrationGainReport,
    walk_forward_beta_gain,
)
from app.notifications.base import Alert
from app.storage.models import Event, Pick, ResultTracking

logger = logging.getLogger(__name__)

#: Stable code for the alert pick_id / log lines.
_DRIFT_CODE = "calibration_drift"

#: Detector thresholds — mirror scripts/ml/calibration_haircut_probe.py and the
#: walk_forward_beta_gain defaults so the unattended verdict matches the manual
#: probe exactly.
DEFAULT_MIN_TRAIN = 2000
DEFAULT_MIN_TEST = 200
DEFAULT_MIN_WARRANT_REL_PCT = 0.5


class _Dispatcher(Protocol):
    """Minimal alert-dispatch surface (app.notifications.dispatcher.AlertDispatcher
    satisfies it). Structural so the job stays trivially mockable — no real
    sink/network is ever constructed under test."""

    async def dispatch(self, alert: Alert) -> object: ...


def drift_alert(report: RecalibrationGainReport, now: datetime) -> Alert:
    """Render a flipped drift verdict as a decision-support alert (never a bet).

    The dedupe key is DATE-bucketed: a same-day re-run (e.g. a restart re-firing
    the daily job) is suppressed by the dispatcher's idempotency store, while an
    unresolved drift re-alerts once the next day — a daily reminder, not per-cycle
    noise. The body is explicit that nothing is automated: the operator decides
    whether to retrain."""
    rel = report.pooled_rel_gain_pct or 0.0
    body = (
        "⚠️ Calibration drift detected on settled picks.\n\n"
        f"A fit-on-past beta recalibration of the devigged fair_prob now beats the "
        f"identity OUT-OF-SAMPLE by {rel:+.2f}% pooled log-loss "
        f"({report.n_folds} folds, n={report.n_total}). Identity-calibration is "
        "load-bearing for every edge, so this warrants OPERATOR review.\n\n"
        "Next step is a deliberate, reproducible retrain (a human/owner decision) — "
        "this monitor does NOT retrain and NO bets are placed."
    )
    return Alert(
        pick_id=f"{_DRIFT_CODE}-{now.strftime('%Y%m%d')}",
        title="⚠️ Calibration drift: recalibration now warranted",
        body=body,
        dedupe_key=f"{_DRIFT_CODE}:{now.strftime('%Y%m%d')}",
    )


async def load_calibration_periods(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> list[tuple[str, list[CalibrationObservation]]]:
    """READ-ONLY: settled, BINARY-outcome PREMIUM picks reduced to chronological
    walk-forward periods (one bucket per fixture year-month).

    Each pick maps to (fair_prob=model_probability — for value picks this carries
    the devigged sharp fair anchor probability, the exact quantity the detector
    calibrates; won=outcome=='won'). Only binary settlements (won/lost) carry a
    calibration label — push/void/half_* are excluded. Scoped to the PREMIUM tier
    so the verdict tracks the ACTUALLY-ALERTED strategy (matching the headline
    premium scope and bet_band_observations). Ordered by fixture kickoff so the
    detector's fit-on-past walk-forward is genuinely temporal (no look-ahead);
    fixtures with no kickoff are excluded (no chronological key). Plain floats
    out — the math stays in the pure module."""
    del now  # accepted for a uniform signature / future windowing; unused today
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    Event.starts_at,
                    Pick.model_probability,
                    ResultTracking.outcome,
                )
                .join(Pick, ResultTracking.pick_id == Pick.id)
                .join(Event, Pick.event_id == Event.id)
                .where(
                    ResultTracking.outcome.in_(("won", "lost")),
                    Pick.tier == "premium",
                    Event.starts_at.is_not(None),
                )
                .order_by(Event.starts_at)
            )
        ).all()

    def _bucket(row: Row[tuple[datetime | None, Decimal, str]]) -> str:
        starts_at = row[0]
        assert starts_at is not None  # filtered by Event.starts_at.is_not(None)
        return f"{starts_at.year:04d}-{starts_at.month:02d}"

    periods: list[tuple[str, list[CalibrationObservation]]] = []
    for label, group in groupby(rows, key=_bucket):
        obs = [
            CalibrationObservation(fair_prob=float(model_probability), won=(outcome == "won"))
            for _starts_at, model_probability, outcome in group
        ]
        periods.append((label, obs))
    return periods


async def calibration_drift_job(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    dispatcher: _Dispatcher | None = None,
    now: datetime | None = None,
    min_train: int = DEFAULT_MIN_TRAIN,
    min_test: int = DEFAULT_MIN_TEST,
    min_warrant_rel_pct: float = DEFAULT_MIN_WARRANT_REL_PCT,
) -> RecalibrationGainReport | None:
    """Daily calibration-drift watch. Loads settled calibration periods, runs the
    pure recalibration-gain detector, and dispatches ONE deduped operator alert
    iff the verdict flips to `warrants_recalibration`. Never auto-retrains.

    Returns the detector's report, or None when there was nothing to check (no
    session factory / no settled data / a load failure). NEVER raises — every
    failure path degrades to log + skip so the scheduler stays up. An unconfigured
    alert channel degrades gracefully (the dispatcher no-ops on token-less sinks);
    `dispatcher=None` skips alerting entirely."""
    now = now or datetime.now(tz=UTC)
    if session_factory is None:
        return None

    try:
        periods = await load_calibration_periods(session_factory, now)
    except Exception as exc:  # a monitoring job must never take the scheduler down
        logger.error("calibration_drift load failed: %s", type(exc).__name__)
        return None

    if not periods:
        logger.info("calibration_drift: no settled calibration data; skipping")
        return None

    try:
        report = walk_forward_beta_gain(
            periods,
            min_train=min_train,
            min_test=min_test,
            min_warrant_rel_pct=min_warrant_rel_pct,
        )
    except Exception as exc:
        logger.error("calibration_drift detector failed: %s", type(exc).__name__)
        return None

    if report.insufficient:
        logger.info(
            "calibration_drift: insufficient settled data (n=%d, no eligible "
            "walk-forward fold); skipping",
            report.n_total,
        )
        return report

    rel = report.pooled_rel_gain_pct or 0.0
    if not report.warrants_recalibration:
        logger.info(
            "calibration_drift: OK — fair_prob still calibrated OOS (pooled rel-gain %.4f%%, n=%d)",
            rel,
            report.n_total,
        )
        return report

    logger.warning(
        "calibration_drift: DRIFT — recalibration now warranted OOS "
        "(pooled rel-gain %.4f%%, n=%d). Operator review required; retraining "
        "stays a human decision.",
        rel,
        report.n_total,
    )
    if dispatcher is not None:
        try:
            await dispatcher.dispatch(drift_alert(report, now))
        except Exception as exc:  # belt-and-braces — sinks shouldn't raise
            logger.error("calibration_drift alert dispatch failed: %s", type(exc).__name__)
    return report
