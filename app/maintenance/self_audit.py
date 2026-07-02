"""Runtime self-audit: cheap READ-ONLY DB invariant checks that surface
operational problems as WARNING/ERROR log lines (caught by the health monitor)
so issues are flagged proactively instead of discovered by hand.

Two checks today, both bounded aggregate queries:
- awaiting_backlog: alerted picks well past kickoff still unsettled (a large pile
  means the finished-score capture or the settle cycle is stuck);
- stale_odds: the newest odds snapshot is too old (odds ingestion may be down).

The threshold logic lives in the PURE `evaluate_anomalies` (unit-tested with no
DB); `run_self_audit` is the thin DB wrapper; `self_audit_job` logs the result
and NEVER raises (a monitoring job must not crash the scheduler).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.notifications.base import Alert
from app.storage.models import Event, OddsSnapshot, Pick

logger = logging.getLogger(__name__)

#: Default dead-man's-switch threshold (P0-4): alert after this many CONSECUTIVE
#: self-audit cycles see zero fresh (non-archive) odds rows. The composition root
#: (app/scheduler.py) may override per deployment.
DEAD_MANS_DEFAULT_K = 3


class _Dispatcher(Protocol):
    """Minimal alert-dispatch surface (app.notifications.dispatcher.AlertDispatcher
    satisfies it). Kept structural so the job stays trivially mockable in tests —
    no real sink/network is ever constructed under test."""

    async def dispatch(self, alert: Alert) -> object: ...


# The sharp-archive captures (Pinnacle arcadia, Betfair Exchange) ingest on their
# OWN ~60s cadence, independent of the live OddsPortal scrape. They are EXCLUDED
# from the stale-odds freshness check so it stays a true signal of LIVE (soft-book)
# ingestion — otherwise the archive heartbeat would keep MAX(ingested_at) fresh and
# silently mask a dead OddsPortal scrape (code-review finding, pre-merge).
_SHARP_ARCHIVE_BOOKMAKERS = ("Pinnacle", "Betfair Exchange")


@dataclass(frozen=True)
class Anomaly:
    severity: str  # "WARN" | "ERROR"
    code: str
    detail: str


def evaluate_anomalies(
    now: datetime,
    *,
    awaiting_backlog: int,
    newest_odds: datetime | None,
    awaiting_threshold: int = 25,
    awaiting_grace_hours: int = 6,
    stale_odds_after: timedelta = timedelta(hours=3),
) -> list[Anomaly]:
    """Pure anomaly evaluation from the two aggregates the DB wrapper reads.

    Empty list == healthy. Kept pure (no DB / no clock) so it is exhaustively
    unit-tested; the wrapper below feeds it live values."""
    found: list[Anomaly] = []
    if awaiting_backlog > awaiting_threshold:
        found.append(
            Anomaly(
                "WARN",
                "awaiting_backlog",
                f"{awaiting_backlog} alerted picks >{awaiting_grace_hours}h past kickoff "
                "still unsettled — finished-score capture/settle may be stuck",
            )
        )
    if newest_odds is None or newest_odds < now - stale_odds_after:
        age = "none" if newest_odds is None else str(now - newest_odds).split(".")[0]
        found.append(
            Anomaly(
                "ERROR",
                "stale_odds",
                f"newest odds-snapshot age {age} exceeds {stale_odds_after} "
                "— odds ingestion may be down",
            )
        )
    return found


def evaluate_dead_mans_switch(
    fresh_odds_rows: int,
    *,
    prior_streak: int,
    k_empty_cycles: int,
    already_alerted: bool,
) -> tuple[int, bool, Anomaly | None]:
    """Pure dead-man's-switch step (P0-4): distinguishes a quiet night from a dead
    scraper over a RUN of cycles.

    `fresh_odds_rows` is the count of NEW non-archive odds rows this cycle. The
    switch fires EXACTLY ONCE when the consecutive-empty streak first reaches
    `k_empty_cycles` (not before), stays quiet while the outage persists, and
    re-arms after any fresh cycle resets the streak.

    Returns (new_streak, new_already_alerted, anomaly|None)."""
    if fresh_odds_rows > 0:
        return 0, False, None
    streak = prior_streak + 1
    if streak >= k_empty_cycles and not already_alerted:
        return (
            streak,
            True,
            Anomaly(
                "ERROR",
                "dead_mans_switch",
                f"{streak} consecutive self-audit cycles produced ZERO fresh odds "
                "rows — the live scrape appears DEAD (not merely a quiet slate)",
            ),
        )
    return streak, already_alerted, None


@dataclass
class SelfAuditMonitorState:
    """Process-local state the scheduled self-audit carries across cycles.

    Rebuilt on every process start (so a persisting anomaly re-alerts once after
    a restart — accepted). `active_anomalies` powers per-anomaly-type transition
    dedupe (P0-2: alert when an anomaly APPEARS, stay quiet while it persists,
    re-alert if it clears then recurs). `empty_odds_streak`/`dead_man_alerted`
    drive the dead-man's-switch one-shot (P0-4)."""

    active_anomalies: set[str] = field(default_factory=set)
    empty_odds_streak: int = 0
    dead_man_alerted: bool = False


def anomaly_alert(anomaly: Anomaly, now: datetime) -> Alert:
    """Render an anomaly as a decision-support alert (never a bet).

    The dedupe key is minute-bucketed per anomaly code: unique enough that the
    dispatcher's own idempotency store can never wrongly suppress a genuine later
    recurrence, while the in-process transition tracker (SelfAuditMonitorState)
    is what prevents per-cycle repeats of an ONGOING anomaly."""
    mark = "🛑" if anomaly.severity == "ERROR" else "⚠️"
    return Alert(
        pick_id=f"self-audit-{anomaly.code}",
        title=f"{mark} Self-audit: {anomaly.code}",
        body=(
            f"{mark} {anomaly.detail}\n\n"
            "(automated monitor — decision-support only, no bets are placed)"
        ),
        dedupe_key=f"self-audit:{anomaly.code}:{now.strftime('%Y%m%dT%H%M')}",
    )


async def run_self_audit(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    *,
    awaiting_grace: timedelta = timedelta(hours=6),
    cycle_window: timedelta | None = None,
) -> tuple[list[Anomaly], int]:
    """READ-ONLY DB self-audit.

    Returns (anomalies, fresh_odds_rows): the anomaly list (empty == healthy)
    plus the count of NEW non-archive odds rows ingested within `cycle_window`
    (0 when no window is given) — the dead-man's-switch input. The fresh count
    EXCLUDES the sharp archives, matching the stale-odds check, so an archive
    heartbeat can never mask a dead live (OddsPortal) scrape."""
    now = now or datetime.now(tz=UTC)
    async with session_factory() as session:
        backlog = (
            await session.scalar(
                select(func.count(Pick.id))
                .join(Event, Pick.event_id == Event.id)
                .where(
                    Pick.status == "alerted",
                    Event.starts_at.is_not(None),
                    Event.starts_at < now - awaiting_grace,
                )
            )
        ) or 0
        newest = await session.scalar(
            select(func.max(OddsSnapshot.ingested_at)).where(
                OddsSnapshot.bookmaker.notin_(_SHARP_ARCHIVE_BOOKMAKERS)
            )
        )
        fresh_odds = 0
        if cycle_window is not None:
            fresh_odds = (
                await session.scalar(
                    select(func.count())
                    .select_from(OddsSnapshot)
                    .where(
                        OddsSnapshot.bookmaker.notin_(_SHARP_ARCHIVE_BOOKMAKERS),
                        OddsSnapshot.ingested_at >= now - cycle_window,
                    )
                )
            ) or 0
    found = evaluate_anomalies(
        now,
        awaiting_backlog=backlog,
        newest_odds=newest,
        awaiting_grace_hours=int(awaiting_grace.total_seconds() // 3600),
    )
    # WRONG-GAME SAFETY NET (go-live, hardened Pinnacle matcher): independently
    # re-verify recently-accepted live Pinnacle anchors are the SAME fixture. A
    # wrong-game close is fake CLV — the cardinal sin — so any mismatch surfaces
    # here as an ERROR through the same monitor channel. Read-only; imported lazily
    # to keep the resolution import out of the hot self-audit path.
    from app.maintenance.wrong_game_audit import audit_live_pinnacle_anchors

    found.extend(await audit_live_pinnacle_anchors(session_factory, now))
    return found, fresh_odds


async def self_audit_job(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    *,
    dispatcher: _Dispatcher | None = None,
    monitor_state: SelfAuditMonitorState | None = None,
    cycle_window: timedelta = timedelta(seconds=600),
    dead_mans_k: int = DEAD_MANS_DEFAULT_K,
) -> int:
    """Run the self-audit, emit one WARNING/ERROR per anomaly (so the health
    monitor catches them) or one INFO when clean, AND (P0-2) dispatch an alert
    per NEW anomaly through the injected dispatcher. NEVER raises — a monitoring
    job must not crash the scheduler. Returns the anomaly count (-1 on failure).

    Alerting is transition-deduped via `monitor_state`: an anomaly alerts when it
    APPEARS and stays quiet while it persists; it re-alerts only after it clears
    and recurs. The dead-man's-switch (P0-4) rides the same dispatcher with its
    own one-shot. Unconfigured channels degrade gracefully (the dispatcher
    no-ops on sinks with no token/url); `dispatcher=None` skips alerting entirely."""
    now = now or datetime.now(tz=UTC)
    try:
        anomalies, fresh_odds = await run_self_audit(
            session_factory, now, cycle_window=cycle_window
        )
    except Exception as exc:  # a monitoring job must never take the scheduler down
        logger.error("self_audit failed: %s", type(exc).__name__)
        return -1

    # P0-4 dead-man's-switch: stateful across cycles, so it needs monitor_state.
    # WP7 fix: with a dispatcher wired, the one-shot is consumed ONLY after a
    # CONFIRMED dispatch (see _dispatch_anomalies) — a failed delivery must
    # leave it armed so the next cycle retries instead of silently swallowing
    # the exact alert that reports the outage.
    dead_man: Anomaly | None = None
    if monitor_state is not None:
        streak, alerted, dead_man = evaluate_dead_mans_switch(
            fresh_odds,
            prior_streak=monitor_state.empty_odds_streak,
            k_empty_cycles=dead_mans_k,
            already_alerted=monitor_state.dead_man_alerted,
        )
        monitor_state.empty_odds_streak = streak
        if dispatcher is None or dead_man is None:
            # nothing to deliver (or no channel to confirm against): keep the
            # pre-existing consume-now semantics so logs stay one-shot too.
            monitor_state.dead_man_alerted = alerted

    all_found = [*anomalies, *([dead_man] if dead_man is not None else [])]
    for anomaly in all_found:
        emit = logger.error if anomaly.severity == "ERROR" else logger.warning
        emit("self_audit %s: %s", anomaly.code, anomaly.detail)
    if not all_found:
        logger.info("self_audit: ok — no anomalies")

    if dispatcher is not None:
        await _dispatch_anomalies(anomalies, dead_man, dispatcher, monitor_state, now)
    return len(all_found)


def _dispatch_confirmed(result: object) -> bool:
    """True when a dispatch result shows the alert REACHED a channel.

    `skipped_duplicate` counts as confirmed — the idempotency store only keeps
    the key when an earlier dispatch reached a channel (the dispatcher releases
    the claim on total failure). A result without the DispatchResult delivery
    surface (e.g. a bare stub returning None) is trusted as delivered."""
    if not hasattr(result, "sink_results"):
        return True
    if bool(getattr(result, "skipped_duplicate", False)):
        return True
    sink_results = getattr(result, "sink_results", ()) or ()
    return any(delivered for _name, delivered in sink_results)


async def _dispatch_anomalies(
    anomalies: list[Anomaly],
    dead_man: Anomaly | None,
    dispatcher: _Dispatcher,
    monitor_state: SelfAuditMonitorState | None,
    now: datetime,
) -> None:
    """Dispatch alerts for newly-APPEARED anomalies (transition dedupe) plus the
    dead-man's-switch one-shot. Per-alert failures are logged (type only) and
    never propagate — alerting must not crash the monitor.

    WP7 confirm-before-consume: dedupe/one-shot state flips ONLY on a confirmed
    dispatch. An anomaly whose alert reached no sink is NOT marked active (so it
    re-dispatches next cycle), and a failed dead-man's-switch delivery leaves
    `dead_man_alerted` False so the switch retries instead of going silent."""
    prior = monitor_state.active_anomalies if monitor_state is not None else set()
    codes = {a.code for a in anomalies}
    to_send = [a for a in anomalies if a.code not in prior]
    if dead_man is not None:
        to_send.append(dead_man)
    # Anomalies that PERSIST stay active; cleared ones drop out. Newly-seen
    # codes join below only once their alert delivery is confirmed.
    if monitor_state is not None:
        monitor_state.active_anomalies = codes & prior
    for anomaly in to_send:
        confirmed = False
        try:
            confirmed = _dispatch_confirmed(await dispatcher.dispatch(anomaly_alert(anomaly, now)))
        except Exception as exc:  # belt-and-braces — sinks shouldn't raise
            logger.error(
                "self_audit alert dispatch failed for %s: %s",
                anomaly.code,
                type(exc).__name__,
            )
        if monitor_state is None or not confirmed:
            continue
        if dead_man is not None and anomaly.code == dead_man.code:
            monitor_state.dead_man_alerted = True
        else:
            monitor_state.active_anomalies.add(anomaly.code)
