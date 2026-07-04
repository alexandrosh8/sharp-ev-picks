"""Unit tests for the runtime self-audit anomaly evaluation (pure — no DB).

run_self_audit reads two cheap DB aggregates (alerted-but-unsettled backlog,
newest odds-snapshot age) and hands them to evaluate_anomalies, which is pure and
tested here. The thin DB wrapper is exercised live by the scheduled job.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# self_audit_job is driven here through a monkeypatched run_self_audit that
# never touches the session factory, so a typed None sentinel satisfies the
# signature without weakening the production contract.
_NO_FACTORY = cast("async_sessionmaker[AsyncSession]", None)


def test_self_audit_evaluate_anomalies() -> None:
    from app.maintenance.self_audit import evaluate_anomalies

    now = datetime(2026, 6, 22, 20, 0, tzinfo=UTC)

    # healthy: small backlog + fresh odds -> no anomalies
    assert evaluate_anomalies(now, awaiting_backlog=3, newest_odds=now) == []

    # awaiting-result backlog over threshold -> WARN
    a = evaluate_anomalies(now, awaiting_backlog=40, newest_odds=now)
    assert [x.code for x in a] == ["awaiting_backlog"]
    assert a[0].severity == "WARN"

    # stale odds (newest snapshot too old) -> ERROR (ingestion likely down)
    b = evaluate_anomalies(now, awaiting_backlog=0, newest_odds=now - timedelta(hours=5))
    assert [x.code for x in b] == ["stale_odds"]
    assert b[0].severity == "ERROR"

    # no odds at all -> stale_odds ERROR
    c = evaluate_anomalies(now, awaiting_backlog=0, newest_odds=None)
    assert [x.code for x in c] == ["stale_odds"]

    # both problems at once
    d = evaluate_anomalies(now, awaiting_backlog=40, newest_odds=None)
    assert {x.code for x in d} == {"awaiting_backlog", "stale_odds"}


# --- P0-4 dead-man's-switch (pure) ------------------------------------------ #


def test_dead_mans_switch_fires_after_k_not_before() -> None:
    from app.maintenance.self_audit import evaluate_dead_mans_switch

    # empty cycles accumulate; nothing fires before K
    assert evaluate_dead_mans_switch(
        0, prior_streak=0, k_empty_cycles=3, already_alerted=False
    ) == (1, False, None)
    assert evaluate_dead_mans_switch(
        0, prior_streak=1, k_empty_cycles=3, already_alerted=False
    ) == (2, False, None)

    # the K-th consecutive empty cycle fires exactly once
    streak, alerted, anomaly = evaluate_dead_mans_switch(
        0, prior_streak=2, k_empty_cycles=3, already_alerted=False
    )
    assert streak == 3
    assert alerted is True
    assert anomaly is not None
    assert anomaly.code == "dead_mans_switch"
    assert anomaly.severity == "ERROR"

    # while the outage persists (already alerted) it stays quiet
    assert evaluate_dead_mans_switch(0, prior_streak=3, k_empty_cycles=3, already_alerted=True) == (
        4,
        True,
        None,
    )


def test_dead_mans_switch_rearms_after_fresh_cycle() -> None:
    from app.maintenance.self_audit import evaluate_dead_mans_switch

    # any fresh-odds cycle resets the streak AND re-arms the one-shot
    assert evaluate_dead_mans_switch(7, prior_streak=5, k_empty_cycles=3, already_alerted=True) == (
        0,
        False,
        None,
    )


# --- P0-2 log->alert bridge + P0-4 wiring (job, mocked dispatcher) ---------- #


class _FakeDispatcher:
    """Captures dispatched alert pick_ids; never touches the network."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def dispatch(self, alert):  # type: ignore[no-untyped-def]
        self.sent.append(alert.pick_id)
        return None


async def test_self_audit_job_dispatches_then_dedupes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [sa.Anomaly("ERROR", "stale_odds", "odds stale")], 5

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _FakeDispatcher()
    state = sa.SelfAuditMonitorState()

    # first sighting of the anomaly alerts
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state)
    assert disp.sent == ["self-audit-stale_odds"]

    # the SAME ongoing anomaly the next cycle is deduped (no re-alert)
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state)
    assert disp.sent == ["self-audit-stale_odds"]


async def test_self_audit_job_realerts_after_anomaly_clears(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    calls = {"n": 0}

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        # anomaly on cycles 1 and 3, healthy on cycle 2
        if calls["n"] == 2:
            return [], 5
        return [sa.Anomaly("ERROR", "stale_odds", "odds stale")], 5

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _FakeDispatcher()
    state = sa.SelfAuditMonitorState()
    for _ in range(3):
        await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state)
    # appears, clears, reappears -> two distinct transition alerts
    assert disp.sent == ["self-audit-stale_odds", "self-audit-stale_odds"]


async def test_self_audit_job_dead_mans_switch_fires_after_k(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [], 0  # zero fresh odds rows every cycle

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _FakeDispatcher()
    state = sa.SelfAuditMonitorState()

    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state, dead_mans_k=3)
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state, dead_mans_k=3)
    assert disp.sent == []  # not before K consecutive empty cycles

    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state, dead_mans_k=3)
    assert disp.sent == ["self-audit-dead_mans_switch"]  # fires on the K-th

    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state, dead_mans_k=3)
    assert disp.sent == ["self-audit-dead_mans_switch"]  # quiet while ongoing


async def test_self_audit_job_no_dispatcher_is_safe(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [sa.Anomaly("ERROR", "stale_odds", "odds stale")], 0

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    # no channels wired: the job still runs, logs, and never raises
    count = await sa.self_audit_job(
        _NO_FACTORY, dispatcher=None, monitor_state=sa.SelfAuditMonitorState()
    )
    assert count == 1


# --- WP7 fix 2: the one-shot/dedupe state must flip AFTER delivery, not before #


class _ShapedResult:
    """Mimics app.notifications.dispatcher.DispatchResult's delivery surface."""

    def __init__(self, *, skipped_duplicate: bool, delivered: bool) -> None:
        self.skipped_duplicate = skipped_duplicate
        self.sink_results = (("telegram", delivered),)


class _ScriptedDispatcher:
    """Returns a scripted DispatchResult per call; captures dispatched pick_ids."""

    def __init__(self, results: list[_ShapedResult]) -> None:
        self._results = results
        self.sent: list[str] = []

    async def dispatch(self, alert):  # type: ignore[no-untyped-def]
        self.sent.append(alert.pick_id)
        return self._results.pop(0)


async def test_anomaly_alert_retries_next_cycle_when_no_sink_delivered(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [sa.Anomaly("ERROR", "stale_odds", "odds stale")], 5

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _ScriptedDispatcher(
        [
            _ShapedResult(skipped_duplicate=False, delivered=False),  # Telegram down
            _ShapedResult(skipped_duplicate=False, delivered=True),  # back up
            _ShapedResult(skipped_duplicate=False, delivered=True),  # (unused if deduped)
        ]
    )
    state = sa.SelfAuditMonitorState()

    # cycle 1: dispatch reaches NO sink -> the anomaly must NOT be marked active
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state)
    assert disp.sent == ["self-audit-stale_odds"]
    assert "stale_odds" not in state.active_anomalies  # alert not consumed by failure

    # cycle 2: retried and delivered -> marked active
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state)
    assert disp.sent == ["self-audit-stale_odds", "self-audit-stale_odds"]
    assert "stale_odds" in state.active_anomalies

    # cycle 3: still ongoing -> deduped, no third send
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state)
    assert disp.sent == ["self-audit-stale_odds", "self-audit-stale_odds"]


async def test_dead_mans_switch_retries_next_cycle_when_no_sink_delivered(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [], 0  # zero fresh odds rows every cycle

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _ScriptedDispatcher(
        [
            _ShapedResult(skipped_duplicate=False, delivered=False),  # total failure
            _ShapedResult(skipped_duplicate=False, delivered=True),  # retry lands
        ]
    )
    state = sa.SelfAuditMonitorState()

    # K=1: the switch trips on the first empty cycle, but delivery FAILS ->
    # the one-shot must stay unconsumed so the next cycle retries.
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state, dead_mans_k=1)
    assert disp.sent == ["self-audit-dead_mans_switch"]
    assert state.dead_man_alerted is False

    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state, dead_mans_k=1)
    assert disp.sent == ["self-audit-dead_mans_switch", "self-audit-dead_mans_switch"]
    assert state.dead_man_alerted is True

    # delivered once -> quiet while the outage persists (no spam)
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state, dead_mans_k=1)
    assert disp.sent == ["self-audit-dead_mans_switch", "self-audit-dead_mans_switch"]


async def test_dead_mans_switch_one_shot_consumed_on_skipped_duplicate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [], 0

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    # The idempotency store already holds the key: skipped_duplicate means the
    # alert REACHED the channel earlier — the one-shot must be consumed.
    disp = _ScriptedDispatcher([_ShapedResult(skipped_duplicate=True, delivered=False)])
    state = sa.SelfAuditMonitorState()
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state, dead_mans_k=1)
    assert state.dead_man_alerted is True


# --- Task B5: proxy-headroom warning (pure eval + job wiring) ---------------- #


def test_evaluate_proxy_headroom_fires_at_floor_plus_one_not_above() -> None:
    from app.maintenance.self_audit import evaluate_proxy_headroom

    now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    # comfortably above the floor -> quiet
    assert evaluate_proxy_headroom(14, 12, now=now, last_alerted=None) is None
    # healthy == floor + 1 -> WARN (one quarantine from zero headroom)
    warn = evaluate_proxy_headroom(13, 12, now=now, last_alerted=None)
    assert warn is not None
    assert (warn.severity, warn.code) == ("WARN", "proxy_headroom")
    assert "13" in warn.detail and "12" in warn.detail
    # at and below the floor -> WARN too
    assert evaluate_proxy_headroom(12, 12, now=now, last_alerted=None) is not None
    assert evaluate_proxy_headroom(0, 12, now=now, last_alerted=None) is not None


def test_evaluate_proxy_headroom_throttles_to_one_per_window() -> None:
    from app.maintenance.self_audit import (
        PROXY_HEADROOM_ALERT_INTERVAL,
        evaluate_proxy_headroom,
    )

    t0 = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    assert PROXY_HEADROOM_ALERT_INTERVAL.total_seconds() == 6 * 3600  # max 1 per 6h
    # inside the window since the last alert -> suppressed
    inside = t0 + PROXY_HEADROOM_ALERT_INTERVAL - timedelta(minutes=1)
    assert evaluate_proxy_headroom(12, 12, now=inside, last_alerted=t0) is None
    # window elapsed -> fires again while the condition persists
    after = t0 + PROXY_HEADROOM_ALERT_INTERVAL
    assert evaluate_proxy_headroom(12, 12, now=after, last_alerted=t0) is not None


async def test_self_audit_job_proxy_headroom_alerts_once_per_window(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [], 5  # DB audit healthy — only the proxy warning is in play

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _FakeDispatcher()
    state = sa.SelfAuditMonitorState()
    t0 = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)

    # no headroom -> warn + dispatch, throttle stamped
    await sa.self_audit_job(
        _NO_FACTORY, t0, dispatcher=disp, monitor_state=state, proxy_headroom=(12, 12)
    )
    assert disp.sent == ["self-audit-proxy_headroom"]
    assert state.last_proxy_headroom_alert == t0

    # next cycle inside the 6h window -> throttled (no repeat)
    await sa.self_audit_job(
        _NO_FACTORY,
        t0 + timedelta(minutes=10),
        dispatcher=disp,
        monitor_state=state,
        proxy_headroom=(12, 12),
    )
    assert disp.sent == ["self-audit-proxy_headroom"]

    # window elapsed and the pool is still tight -> one more warning
    await sa.self_audit_job(
        _NO_FACTORY,
        t0 + timedelta(hours=6),
        dispatcher=disp,
        monitor_state=state,
        proxy_headroom=(12, 12),
    )
    assert disp.sent == ["self-audit-proxy_headroom", "self-audit-proxy_headroom"]

    # healthy pool -> quiet, throttle untouched
    await sa.self_audit_job(
        _NO_FACTORY,
        t0 + timedelta(hours=13),
        dispatcher=disp,
        monitor_state=state,
        proxy_headroom=(14, 12),
    )
    assert disp.sent == ["self-audit-proxy_headroom", "self-audit-proxy_headroom"]
    assert state.last_proxy_headroom_alert == t0 + timedelta(hours=6)


async def test_self_audit_job_proxy_headroom_dispatch_failure_is_fail_safe(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [], 5

    monkeypatch.setattr(sa, "run_self_audit", fake_run)

    class _ExplodingDispatcher:
        async def dispatch(self, alert):  # type: ignore[no-untyped-def]
            raise RuntimeError("sink down")

    state = sa.SelfAuditMonitorState()
    t0 = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    # a raising sink must not break the cycle; the throttle still stamps so the
    # warning stays capped at one per window (advisory — no delivery retry).
    count = await sa.self_audit_job(
        _NO_FACTORY,
        t0,
        dispatcher=_ExplodingDispatcher(),
        monitor_state=state,
        proxy_headroom=(12, 12),
    )
    assert count == 0  # DB audit clean; the job completed despite the sink error
    assert state.last_proxy_headroom_alert == t0


async def test_self_audit_job_proxy_headroom_none_or_stateless_is_quiet(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [], 5

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _FakeDispatcher()
    # no proxy input (e.g. direct egress, no pool configured) -> nothing fires
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=sa.SelfAuditMonitorState())
    # no monitor_state -> no throttle store -> the check is skipped entirely
    await sa.self_audit_job(
        _NO_FACTORY, dispatcher=disp, monitor_state=None, proxy_headroom=(0, 12)
    )
    assert disp.sent == []
