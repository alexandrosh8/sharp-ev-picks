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
