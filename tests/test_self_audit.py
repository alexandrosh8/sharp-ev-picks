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


# --- impossible-market alarm (pure, settlement audit 2026-08-02) ------------- #


def test_impossible_market_family_fires_once_naming_families() -> None:
    """n>=30 graded with 0 wins -> exactly ONE warning per run naming every
    offending (sport, family) with counts — the alarm that would have caught
    tennis spreads_minus_2_5 at 0W/102L."""
    from app.maintenance.self_audit import evaluate_impossible_market_families

    rows = [
        ("tennis", "spreads_minus_2_5", 102, 0),
        ("tennis", "spreads_minus_1_5", 143, 41),  # has wins -> quiet
        ("soccer", "totals_2_5", 500, 260),  # healthy -> quiet
        ("tennis", "spreads_plus_0_5", 33, 0),
    ]
    anomaly = evaluate_impossible_market_families(rows)
    assert anomaly is not None
    assert anomaly.severity == "WARN"
    assert anomaly.code == "impossible_market_family"
    assert "tennis/spreads_minus_2_5: 0 wins in 102 graded" in anomaly.detail
    assert "tennis/spreads_plus_0_5: 0 wins in 33 graded" in anomaly.detail
    assert "spreads_minus_1_5" not in anomaly.detail
    # ONE anomaly object per run (both families in one warning), and the
    # detail carries counts only — no odds, stakes or identities.
    assert "totals_2_5" not in anomaly.detail


def test_impossible_market_family_quiet_below_threshold_or_with_wins() -> None:
    from app.maintenance.self_audit import evaluate_impossible_market_families

    # 0 wins but under the n>=30 floor: small-sample variance, stays quiet.
    assert evaluate_impossible_market_families([("tennis", "spreads_minus_2_5", 29, 0)]) is None
    # A single win disarms the alarm regardless of size.
    assert evaluate_impossible_market_families([("tennis", "spreads_minus_2_5", 500, 1)]) is None
    assert evaluate_impossible_market_families([]) is None


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


# --- Task 1b: OddsPortal listing-recovery watch (pure eval) ------------------ #


def test_listing_recovery_accumulates_dark_streak_and_stays_quiet() -> None:
    from app.maintenance.self_audit import evaluate_listing_recovery

    # zero-listing cycles extend the dark streak; nothing fires while dark
    assert evaluate_listing_recovery(0, prior_dark_streak=0, dark_k=3) == (1, None)
    assert evaluate_listing_recovery(0, prior_dark_streak=1, dark_k=3) == (2, None)
    assert evaluate_listing_recovery(0, prior_dark_streak=2, dark_k=3) == (3, None)


def test_listing_recovery_none_input_holds_state() -> None:
    from app.maintenance.self_audit import evaluate_listing_recovery

    # no listing signal this cycle (loader has no listing concept / no poll yet)
    # -> state holds unchanged, nothing fires, even mid-blackout
    assert evaluate_listing_recovery(None, prior_dark_streak=9, dark_k=3) == (9, None)


def test_listing_recovery_fires_once_after_dark_run() -> None:
    from app.maintenance.self_audit import evaluate_listing_recovery

    # a >0 listing after >= dark_k dark cycles fires the one-shot WARN
    streak, anomaly = evaluate_listing_recovery(42, prior_dark_streak=3, dark_k=3)
    assert streak == 0
    assert anomaly is not None
    assert anomaly.code == "listing_recovered"
    assert anomaly.severity == "WARN"

    # the streak reset means it cannot repeat until another full dark run accrues
    assert evaluate_listing_recovery(50, prior_dark_streak=0, dark_k=3) == (0, None)


def test_listing_recovery_no_alert_when_never_went_dark_enough() -> None:
    from app.maintenance.self_audit import evaluate_listing_recovery

    # a brief 1-2 cycle dip below dark_k that recovers is normal scrape noise,
    # not a blackout revert -> reset the streak but do not alert
    assert evaluate_listing_recovery(30, prior_dark_streak=2, dark_k=3) == (0, None)


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


async def test_listing_blackout_shape_is_detected_and_recovery_fires(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Task 1d regression — the exact 2026-07-04 failure shape: the OddsPortal
    # LISTING returns 0 matches every cycle while the self-audit itself stays
    # healthy (per-match/ARCADIA paths keep serving, so run_self_audit finds no
    # anomaly and reports fresh odds). Assert the blackout is DETECTED (dark
    # streak accrues, quiet until the bar) and that the upstream revert fires
    # exactly once when the listing serves fixtures again — so recovery no
    # longer depends on someone noticing the quiet cycle by hand.
    from app.maintenance import self_audit as sa

    async def healthy_audit(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return [], 5  # no anomaly, fresh odds — the audit cannot see the listing hole

    monkeypatch.setattr(sa, "run_self_audit", healthy_audit)
    # Drive the listing-probe input directly (the poll registry the watch reads).
    listing = {"n": 0}
    monkeypatch.setattr(sa, "_listing_matches_from_last_poll", lambda: listing["n"])

    disp = _FakeDispatcher()
    state = sa.SelfAuditMonitorState()

    # blackout: 4 consecutive dark cycles — streak accrues, NOTHING fires
    for _ in range(4):
        await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state)
    assert state.listing_dark_streak == 4
    assert disp.sent == []

    # upstream reverts: the listing serves fixtures again -> one recovery alert
    listing["n"] = 37
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state)
    assert state.listing_dark_streak == 0
    assert disp.sent == ["self-audit-listing_recovered"]

    # a second healthy cycle does not re-alert (one-shot until another dark run)
    await sa.self_audit_job(_NO_FACTORY, dispatcher=disp, monitor_state=state)
    assert disp.sent == ["self-audit-listing_recovered"]


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


# --- sharp-anchor cliff (gate-reason telemetry follow-up, 2026-07-26) --------- #


def test_sharp_anchor_cliff_fires_on_collapse() -> None:
    # The 13-day regression shape: 468 sharp-anchored evaluations in the prior
    # 24h, 0 in the last 24h -> WARN through the standard anomaly channel.
    from app.maintenance.self_audit import evaluate_sharp_anchor_cliff

    warn = evaluate_sharp_anchor_cliff(recent_24h=0, prior_24h=468)
    assert warn is not None
    assert (warn.severity, warn.code) == ("WARN", "sharp_anchor_cliff")
    assert "468" in warn.detail and "0" in warn.detail


def test_sharp_anchor_cliff_quiet_on_ordinary_dip_and_thin_prior() -> None:
    from app.maintenance.self_audit import evaluate_sharp_anchor_cliff

    # 100 -> 90 is a 10% dip — quiet.
    assert evaluate_sharp_anchor_cliff(recent_24h=90, prior_24h=100) is None
    # A drop must EXCEED 80%: exactly 80% (100 -> 20) stays quiet...
    assert evaluate_sharp_anchor_cliff(recent_24h=20, prior_24h=100) is None
    # ...one fewer trips it.
    assert evaluate_sharp_anchor_cliff(recent_24h=19, prior_24h=100) is not None
    # Prior below the significance floor (50): a quiet slate, never a cliff.
    assert evaluate_sharp_anchor_cliff(recent_24h=0, prior_24h=49) is None
    assert evaluate_sharp_anchor_cliff(recent_24h=0, prior_24h=0) is None
    # At the floor exactly, the check is live.
    assert evaluate_sharp_anchor_cliff(recent_24h=0, prior_24h=50) is not None


# --- Pinnacle capture dead-man (arcadia silence, operator item 1 2026-08-04) -- #


def test_pinnacle_capture_silence_fires_on_zero_new_events() -> None:
    """Jul 18-26 shape: the arcadia feed went silent for 8 days (zero NEW
    pinnacle_* namespace events created) with no alarm. Zero new events inside
    the window -> WARN through the standard anomaly channel."""
    from app.maintenance.self_audit import evaluate_pinnacle_capture_silence

    warn = evaluate_pinnacle_capture_silence(0, window_hours=6.0)
    assert warn is not None
    assert (warn.severity, warn.code) == ("WARN", "pinnacle_capture_silent")
    # Counts + window only — never odds, URLs, or credentials.
    assert "6" in warn.detail
    assert "http" not in warn.detail.lower()


def test_pinnacle_capture_silence_quiet_when_events_flow() -> None:
    from app.maintenance.self_audit import evaluate_pinnacle_capture_silence

    # Any new pinnacle-namespace event inside the window -> healthy.
    assert evaluate_pinnacle_capture_silence(1, window_hours=6.0) is None
    assert evaluate_pinnacle_capture_silence(424, window_hours=6.0) is None


async def test_self_audit_job_pinnacle_silence_threads_window(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """self_audit_job forwards pinnacle_silence_window to run_self_audit; the
    anomaly rides the standard transition-dedupe dispatch channel (alert on
    APPEAR, quiet while it persists)."""
    from app.maintenance import self_audit as sa

    seen_windows: list[timedelta | None] = []
    sent: list[object] = []

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        seen_windows.append(kwargs.get("pinnacle_silence_window"))
        anomaly = sa.evaluate_pinnacle_capture_silence(0, window_hours=6.0)
        assert anomaly is not None
        return [anomaly], 100

    class Dispatcher:
        async def dispatch(self, alert):  # type: ignore[no-untyped-def]
            sent.append(alert)
            return None

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    state = sa.SelfAuditMonitorState()
    window = timedelta(hours=6)
    n = await sa.self_audit_job(
        _NO_FACTORY,
        dispatcher=Dispatcher(),
        monitor_state=state,
        pinnacle_silence_window=window,
    )
    assert n == 1
    assert seen_windows == [window]
    assert len(sent) == 1  # alert on APPEAR
    # Outage persists -> transition dedupe keeps the channel quiet.
    await sa.self_audit_job(
        _NO_FACTORY,
        dispatcher=Dispatcher(),
        monitor_state=state,
        pinnacle_silence_window=window,
    )
    assert len(sent) == 1


async def test_run_self_audit_counts_new_pinnacle_namespace_events() -> None:
    """DB-backed (compose Postgres, skips when absent): the pinnacle dead-man
    counts EVENTS CREATED in pinnacle_* sport namespaces inside the window —
    an event older than the window (or in a live namespace) must not silence
    the alarm; a fresh pinnacle_* event must."""
    import pytest
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.maintenance.self_audit import run_self_audit
    from app.storage.models import Base, Event, League, Sport, Team
    from tests.database import TEST_DATABASE_URL

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    async with engine.connect() as conn:
        trans = await conn.begin()
        await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        now = datetime.now(tz=UTC)
        async with factory() as session:
            sport = Sport(key="pinnacle_soccer", name="pinnacle_soccer")
            session.add(sport)
            await session.flush()
            league = League(sport_id=sport.id, key="pinnacle_soccer", name="pinnacle_soccer")
            session.add(league)
            await session.flush()
            h = Team(sport_id=sport.id, name="Home FC", normalized_name="home fc pin")
            a = Team(sport_id=sport.id, name="Away FC", normalized_name="away fc pin")
            session.add_all([h, a])
            await session.flush()
            session.add(
                Event(
                    sport_id=sport.id,
                    league_id=league.id,
                    home_team_id=h.id,
                    away_team_id=a.id,
                    external_ref="pin-evt-1",
                    starts_at=now + timedelta(hours=12),
                )
            )
            await session.commit()

        window = timedelta(hours=6)
        anomalies, _ = await run_self_audit(factory, now, pinnacle_silence_window=window)
        assert "pinnacle_capture_silent" not in {x.code for x in anomalies}

        # Backdate the event beyond the window -> the alarm fires.
        async with factory() as session:
            await session.execute(update(Event).values(created_at=now - timedelta(hours=7)))
            await session.commit()
        anomalies, _ = await run_self_audit(factory, now, pinnacle_silence_window=window)
        assert "pinnacle_capture_silent" in {x.code for x in anomalies}

        # Window None (ARCADIA_ENABLED=false at the composition root) -> quiet.
        anomalies, _ = await run_self_audit(factory, now, pinnacle_silence_window=None)
        assert "pinnacle_capture_silent" not in {x.code for x in anomalies}
        await trans.rollback()
    await engine.dispose()


# --- wrong_game_anchor per-pick daily alert dedupe (2026-08-06) ------------- #
# The Zandschulo cascade: ONE audit false-positive re-alerted 43x in ~30h
# because (a) the code-level transition key made every wrong-game pick share
# one dedupe slot and (b) the 200-pick sample window flaps, clearing and
# re-firing the code each time. Wrong-game anomalies now key per PICK (detail)
# and carry a bounded 24h suppression floor.


def _wg(detail: str):  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    return sa.Anomaly("ERROR", "wrong_game_anchor", detail)


async def test_wrong_game_alerts_per_pick_not_per_code(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    cycle = {"anoms": [_wg("pick A"), _wg("pick B")]}

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return list(cycle["anoms"]), 5

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _FakeDispatcher()
    state = sa.SelfAuditMonitorState()
    t0 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

    # two DISTINCT flagged picks in one cycle -> two alerts (not one per code)
    await sa.self_audit_job(_NO_FACTORY, t0, dispatcher=disp, monitor_state=state)
    assert disp.sent == ["self-audit-wrong_game_anchor"] * 2

    # both persist -> quiet (transition dedupe)
    await sa.self_audit_job(
        _NO_FACTORY, t0 + timedelta(minutes=10), dispatcher=disp, monitor_state=state
    )
    assert len(disp.sent) == 2

    # a THIRD pick appears while the first two persist -> exactly one new alert
    cycle["anoms"] = [_wg("pick A"), _wg("pick B"), _wg("pick C")]
    await sa.self_audit_job(
        _NO_FACTORY, t0 + timedelta(minutes=20), dispatcher=disp, monitor_state=state
    )
    assert len(disp.sent) == 3


async def test_wrong_game_flapping_pick_alerts_once_per_day(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    cycle = {"anoms": [_wg("pick A")]}

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return list(cycle["anoms"]), 5

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _FakeDispatcher()
    state = sa.SelfAuditMonitorState()
    t0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)

    await sa.self_audit_job(_NO_FACTORY, t0, dispatcher=disp, monitor_state=state)
    assert len(disp.sent) == 1

    # the pick falls OUT of the audit sample (flap), then reappears within 24h:
    # the daily floor suppresses the re-alert the transition dedupe would fire
    for i in range(1, 6):
        cycle["anoms"] = [] if i % 2 else [_wg("pick A")]
        await sa.self_audit_job(
            _NO_FACTORY, t0 + timedelta(hours=i), dispatcher=disp, monitor_state=state
        )
    assert len(disp.sent) == 1

    # after the 24h floor elapses a still-flagged pick re-alerts once
    cycle["anoms"] = [_wg("pick A")]
    await sa.self_audit_job(
        _NO_FACTORY, t0 + timedelta(hours=25), dispatcher=disp, monitor_state=state
    )
    assert len(disp.sent) == 2

    # other codes keep pure transition semantics (clear -> recur re-alerts)
    assert state.wrong_game_alerted  # the daily stamp map is in use


async def test_wrong_game_daily_map_is_bounded_and_pruned(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.maintenance import self_audit as sa

    cycle = {"anoms": [_wg("old pick")]}

    async def fake_run(session_factory, now=None, **kwargs):  # type: ignore[no-untyped-def]
        return list(cycle["anoms"]), 5

    monkeypatch.setattr(sa, "run_self_audit", fake_run)
    disp = _FakeDispatcher()
    state = sa.SelfAuditMonitorState()
    t0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)
    await sa.self_audit_job(_NO_FACTORY, t0, dispatcher=disp, monitor_state=state)
    assert len(state.wrong_game_alerted) == 1

    # stamps older than the re-alert interval are pruned (bounded by expiry)
    cycle["anoms"] = [_wg("new pick")]
    await sa.self_audit_job(
        _NO_FACTORY, t0 + timedelta(hours=25), dispatcher=disp, monitor_state=state
    )
    assert all("new pick" in k for k in state.wrong_game_alerted)

    # hard cap: the map never exceeds the documented bound
    cycle["anoms"] = [_wg(f"pick {i}") for i in range(sa._WRONG_GAME_DAILY_MAX_KEYS + 50)]
    await sa.self_audit_job(
        _NO_FACTORY, t0 + timedelta(hours=26), dispatcher=disp, monitor_state=state
    )
    await sa.self_audit_job(
        _NO_FACTORY, t0 + timedelta(hours=27), dispatcher=disp, monitor_state=state
    )
    assert len(state.wrong_game_alerted) <= sa._WRONG_GAME_DAILY_MAX_KEYS


def test_wrong_game_alert_dedupe_keys_are_per_pick() -> None:
    from app.maintenance import self_audit as sa

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    k_a = sa.anomaly_alert(_wg("pick A"), now).dedupe_key
    k_b = sa.anomaly_alert(_wg("pick B"), now).dedupe_key
    # two picks flagged in the SAME minute must not collapse in the
    # dispatcher's idempotency store
    assert k_a != k_b
    # deterministic per pick (retries next cycle still dedupe downstream)
    assert k_a == sa.anomaly_alert(_wg("pick A"), now).dedupe_key
