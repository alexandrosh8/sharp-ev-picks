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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.notifications.base import Alert
from app.storage.models import (
    CandidateEvaluation,
    Event,
    OddsSnapshot,
    Pick,
    ResultTracking,
    Sport,
)

logger = logging.getLogger(__name__)

#: Default dead-man's-switch threshold (P0-4): alert after this many CONSECUTIVE
#: self-audit cycles see zero fresh (non-archive) odds rows. The composition root
#: (app/scheduler.py) may override per deployment.
DEAD_MANS_DEFAULT_K = 3

#: Task B5: minimum interval between proxy-headroom warnings. The condition
#: (healthy <= concurrency floor + 1) typically PERSISTS for hours once a slot
#: quarantines, so this is a time throttle, not transition dedupe — at most one
#: warning per window while the pool stays tight.
PROXY_HEADROOM_ALERT_INTERVAL = timedelta(hours=6)

#: Listing-recovery watch (OddsPortal listing blackout, 2026-07-04): how many
#: CONSECUTIVE self-audit cycles must see a zero-match listing before the
#: listing counts as DARK — only then does the first >0-match listing fire the
#: one-shot "recovered" notice. Aligned with DEAD_MANS_DEFAULT_K so both
#: switches agree on what a real outage (vs a quiet gap) looks like.
LISTING_DARK_K = 3

#: Sharp-anchor cliff (gate-reason telemetry follow-up, 2026-07-26): a drop in
#: sharp-anchored candidate evaluations (last 24h vs the prior 24h) EXCEEDING
#: this fraction is a regression signal — the shape of the PR #164 liquidity-
#: floor bug, which silently pushed 99.8% of candidates to 'no_sharp_anchor'
#: for 13 days. Requires a prior-window count of at least
#: SHARP_ANCHOR_CLIFF_MIN_PRIOR so a quiet slate can never fake a cliff.
SHARP_ANCHOR_CLIFF_DROP = 0.80
SHARP_ANCHOR_CLIFF_MIN_PRIOR = 50

#: Impossible-market alarm (settlement audit 2026-08-02): a (sport,
#: market_detail-family) with at least this many GRADED results (won/lost/
#: half_*) and ZERO wins is a grading defect, not variance — tennis
#: spreads_minus_2_5 ran 0W/102L (p ~ 1e-24 at its fair ~0.42) for weeks
#: because game handicaps were graded against set scores. At fair odds ~0.4+,
#: 30 graded picks with 0 wins has p < 1e-6.
IMPOSSIBLE_MARKET_MIN_GRADED = 30


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


def evaluate_proxy_headroom(
    healthy: int,
    concurrency_floor: int,
    *,
    now: datetime,
    last_alerted: datetime | None,
    throttle: timedelta = PROXY_HEADROOM_ALERT_INTERVAL,
) -> Anomaly | None:
    """Pure proxy-headroom check (task B5): WARN when the healthy proxy count is
    at (or within one of) the scrape's own concurrency floor
    (ODDSPORTAL_CONCURRENCY) — the pool then has no spare slots and one more
    quarantine slows every cycle. Time-throttled to one warning per `throttle`
    window (the condition persists; per-cycle repeats would be noise).

    Counters only — never a proxy URL/IP/credential. Returns None when healthy
    is comfortably above the floor or the throttle window hasn't elapsed."""
    if healthy > concurrency_floor + 1:
        return None
    if last_alerted is not None and now - last_alerted < throttle:
        return None
    return Anomaly(
        "WARN",
        "proxy_headroom",
        f"proxy pool headroom exhausted: {healthy} healthy vs concurrency floor "
        f"{concurrency_floor} (ODDSPORTAL_CONCURRENCY) — one more quarantine slows "
        "every scrape cycle; expand or replace the proxy pool",
    )


def evaluate_listing_recovery(
    listed_matches: int | None,
    *,
    prior_dark_streak: int,
    dark_k: int = LISTING_DARK_K,
) -> tuple[int, Anomaly | None]:
    """Pure listing-recovery step (OddsPortal blackout 2026-07-04): notice the
    moment the upstream LISTING serves fixtures again after a dark run.

    `listed_matches` is the latest scrape cycle's listing count (summed across
    sports) — the scrape cycle itself is the probe, no extra request is ever
    made. ``None`` means no listing signal this cycle (no poll finished yet, or
    the active loader doesn't report listing counts): state holds unchanged.
    Zero extends the dark streak. A >0 count after >= `dark_k` consecutive dark
    cycles fires the one-shot WARN "listing recovered" (WARN so it reaches the
    health monitor AND the alert channel — the operator wants the revert the
    moment it lands); any >0 count resets the streak, so the notice cannot
    repeat until another full dark run accrues.

    Returns (new_dark_streak, anomaly|None)."""
    if listed_matches is None:
        return prior_dark_streak, None
    if listed_matches <= 0:
        return prior_dark_streak + 1, None
    if prior_dark_streak >= dark_k:
        return 0, Anomaly(
            "WARN",
            "listing_recovered",
            f"oddsportal listing RECOVERED: {listed_matches} matches listed after "
            f"{prior_dark_streak} consecutive dark self-audit cycles — live odds "
            "ingestion should resume; verify fresh picks follow",
        )
    return 0, None


def evaluate_sharp_anchor_cliff(
    *,
    recent_24h: int,
    prior_24h: int,
    drop_threshold: float = SHARP_ANCHOR_CLIFF_DROP,
    min_prior: int = SHARP_ANCHOR_CLIFF_MIN_PRIOR,
) -> Anomaly | None:
    """Pure sharp-anchor cliff check (gate-reason telemetry follow-up,
    2026-07-26): WARN when sharp-anchored candidate evaluations in the last 24h
    dropped by MORE than ``drop_threshold`` vs the prior 24h — the shape of the
    PR #164 liquidity-floor regression, where 'no_sharp_anchor' silently became
    99.8% of gate reasons for 13 days. A prior-window count below ``min_prior``
    stays quiet (a thin/off-season slate is not a cliff); an ordinary dip
    (e.g. 100 -> 90) never fires. Counters only — no odds, no identities."""
    if prior_24h < min_prior:
        return None
    if recent_24h >= prior_24h * (1.0 - drop_threshold):
        return None
    drop_pct = 100.0 * (1.0 - recent_24h / prior_24h)
    return Anomaly(
        "WARN",
        "sharp_anchor_cliff",
        f"sharp-anchored evaluations fell {drop_pct:.0f}%: {recent_24h} in the last "
        f"24h vs {prior_24h} in the prior 24h — sharp-anchor coverage may have "
        "regressed (liquidity floor / exchange demotion / sharp feed loss); check "
        "the no_sharp_anchor:<cause> reason mix in candidate_evaluations",
    )


def evaluate_impossible_market_families(
    families: Sequence[tuple[str, str, int, int]],
    *,
    min_graded: int = IMPOSSIBLE_MARKET_MIN_GRADED,
) -> Anomaly | None:
    """Pure impossible-market check (settlement audit 2026-08-02): ONE warning
    per run naming every (sport, market_detail-family) with >= ``min_graded``
    graded results (won/lost/half_won/half_lost — push/void excluded) and ZERO
    wins. Such a family is statistically impossible under honest grading and
    signals a settlement-axis defect (the alarm that would have caught tennis
    spreads_minus_2_5 at 0W/102L). Input rows are
    ``(sport_key, family, graded_count, win_count)`` — counts only, no odds,
    no identities. Returns None when every family has at least one win."""
    offenders = [
        (sport, family, graded)
        for sport, family, graded, wins in families
        if graded >= min_graded and wins == 0
    ]
    if not offenders:
        return None
    named = "; ".join(
        f"{sport}/{family}: 0 wins in {graded} graded" for sport, family, graded in offenders
    )
    return Anomaly(
        "WARN",
        "impossible_market_family",
        f"impossible market family(ies) — {named} — a family that NEVER wins is a "
        "grading defect (e.g. game handicaps graded on set scores, audit "
        "2026-08-02): audit the settlement axis before trusting these results",
    )


def _listing_matches_from_last_poll() -> int | None:
    """Latest listing count summed across sports from the pipeline's LAST_POLL
    liveness registry — the self-audit's listing-probe input. The poll cycle
    already performs the dated listing scrape and records `matches_found` per
    sport, so reading it here adds ZERO upstream requests (politeness). ``None``
    until a poll that reports listing counts has run (startup, or a loader such
    as odds_api that has no listing concept). Lazy import + broad guard: a
    monitoring input must never break the audit (type-name-only logging)."""
    try:
        from app.pipeline import LAST_POLL

        known = [
            int(poll["matches_found"])
            for poll in LAST_POLL.values()
            if poll.get("matches_found") is not None
        ]
        return sum(known) if known else None
    except Exception as exc:  # fail-safe: no listing signal this cycle
        logger.warning("self_audit listing probe input unavailable: %s", type(exc).__name__)
        return None


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
    #: Task B5: last time the proxy-headroom warning was emitted (time throttle,
    #: max one per PROXY_HEADROOM_ALERT_INTERVAL while the pool stays tight).
    last_proxy_headroom_alert: datetime | None = None
    #: Listing-recovery watch: consecutive self-audit cycles whose latest poll
    #: listed ZERO matches. Reset by any >0 listing; the recovery notice fires
    #: only when a >0 listing follows a streak >= LISTING_DARK_K. Restart
    #: semantics match the dead-man's switch: the streak rebuilds from 0.
    listing_dark_streak: int = 0


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
        # Sharp-anchor cliff input (2026-07-26): sharp-anchored candidate
        # evaluations in the last 24h vs the prior 24h. Bounded aggregate on
        # the idx-covered evaluated_at windows; 'consensus' rows are exactly
        # the no-sharp-anchor misses, so they are excluded from the count.
        sharp_recent = (
            await session.scalar(
                select(func.count())
                .select_from(CandidateEvaluation)
                .where(
                    CandidateEvaluation.anchor_type.in_(("pinnacle", "sharp")),
                    CandidateEvaluation.evaluated_at >= now - timedelta(hours=24),
                )
            )
        ) or 0
        sharp_prior = (
            await session.scalar(
                select(func.count())
                .select_from(CandidateEvaluation)
                .where(
                    CandidateEvaluation.anchor_type.in_(("pinnacle", "sharp")),
                    CandidateEvaluation.evaluated_at >= now - timedelta(hours=48),
                    CandidateEvaluation.evaluated_at < now - timedelta(hours=24),
                )
            )
        ) or 0
        # Impossible-market alarm input (settlement audit 2026-08-02): graded
        # (won/lost/half_*) result counts + win counts per (sport,
        # market_detail-family). NULL details fall back to the market string so
        # pre-vocabulary picks still form a family. Bounded aggregate; the
        # HAVING floor keeps the row set tiny. Counts only — no odds/stakes.
        family_col = func.coalesce(Pick.market_detail, Pick.market)
        graded_col = func.count()
        wins_col = func.sum(case((ResultTracking.outcome.in_(("won", "half_won")), 1), else_=0))
        family_rows = [
            (str(sport), str(family), int(graded), int(wins))
            for sport, family, graded, wins in (
                await session.execute(
                    select(Sport.key, family_col, graded_col, wins_col)
                    .select_from(ResultTracking)
                    .join(Pick, ResultTracking.pick_id == Pick.id)
                    .join(Event, Pick.event_id == Event.id)
                    .join(Sport, Event.sport_id == Sport.id)
                    .where(ResultTracking.outcome.in_(("won", "lost", "half_won", "half_lost")))
                    .group_by(Sport.key, family_col)
                    .having(graded_col >= IMPOSSIBLE_MARKET_MIN_GRADED)
                )
            ).all()
        ]
    found = evaluate_anomalies(
        now,
        awaiting_backlog=backlog,
        newest_odds=newest,
        awaiting_grace_hours=int(awaiting_grace.total_seconds() // 3600),
    )
    # Sharp-anchor cliff (2026-07-26): rides the standard anomaly channel, so
    # transition dedupe / dispatch / logging come for free from the job wrapper.
    cliff = evaluate_sharp_anchor_cliff(recent_24h=sharp_recent, prior_24h=sharp_prior)
    if cliff is not None:
        found.append(cliff)
    impossible = evaluate_impossible_market_families(family_rows)
    if impossible is not None:
        found.append(impossible)
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
    proxy_headroom: tuple[int, int] | None = None,
) -> int:
    """Run the self-audit, emit one WARNING/ERROR per anomaly (so the health
    monitor catches them) or one INFO when clean, AND (P0-2) dispatch an alert
    per NEW anomaly through the injected dispatcher. NEVER raises — a monitoring
    job must not crash the scheduler. Returns the anomaly count (-1 on failure).

    Alerting is transition-deduped via `monitor_state`: an anomaly alerts when it
    APPEARS and stays quiet while it persists; it re-alerts only after it clears
    and recurs. The dead-man's-switch (P0-4) rides the same dispatcher with its
    own one-shot. Unconfigured channels degrade gracefully (the dispatcher
    no-ops on sinks with no token/url); `dispatcher=None` skips alerting entirely.

    `proxy_headroom` (task B5) is an optional `(healthy, concurrency_floor)`
    pair from the proxy registry diagnostics; when the healthy count is within
    one of the floor a time-throttled WARN (max one per
    PROXY_HEADROOM_ALERT_INTERVAL) is logged and dispatched. It runs BEFORE the
    DB audit so a DB outage can't mask it, requires `monitor_state` (the
    throttle lives there), and is fully fail-safe."""
    now = now or datetime.now(tz=UTC)
    if proxy_headroom is not None and monitor_state is not None:
        healthy, floor = proxy_headroom
        headroom_warn = evaluate_proxy_headroom(
            healthy, floor, now=now, last_alerted=monitor_state.last_proxy_headroom_alert
        )
        if headroom_warn is not None:
            # Throttle stamps on FIRE (not on confirmed delivery — deliberate
            # divergence from the WP7 pattern): this warning is advisory, and a
            # strict 1-per-window cap beats delivery retries that could spam
            # the log while a sink is down.
            monitor_state.last_proxy_headroom_alert = now
            logger.warning("self_audit %s: %s", headroom_warn.code, headroom_warn.detail)
            if dispatcher is not None:
                try:
                    await dispatcher.dispatch(anomaly_alert(headroom_warn, now))
                except Exception as exc:  # alerting must never break the cycle
                    logger.error(
                        "self_audit alert dispatch failed for %s: %s",
                        headroom_warn.code,
                        type(exc).__name__,
                    )
    # Listing-recovery watch (upstream-revert detection for the 2026-07-04
    # OddsPortal listing blackout): rides the LAST_POLL listing counts the poll
    # cycle already records — zero extra upstream requests. Runs BEFORE the DB
    # audit (like proxy_headroom) so a DB outage can't mask the revert; fully
    # fail-safe (a broken input yields None -> state holds, nothing fires).
    if monitor_state is not None:
        dark_streak, recovered = evaluate_listing_recovery(
            _listing_matches_from_last_poll(),
            prior_dark_streak=monitor_state.listing_dark_streak,
        )
        monitor_state.listing_dark_streak = dark_streak
        if recovered is not None:
            # One-shot by construction (the streak resets on the >0 listing),
            # so no confirm-before-consume machinery is needed here.
            logger.warning("self_audit %s: %s", recovered.code, recovered.detail)
            if dispatcher is not None:
                try:
                    await dispatcher.dispatch(anomaly_alert(recovered, now))
                except Exception as exc:  # alerting must never break the cycle
                    logger.error(
                        "self_audit alert dispatch failed for %s: %s",
                        recovered.code,
                        type(exc).__name__,
                    )
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
