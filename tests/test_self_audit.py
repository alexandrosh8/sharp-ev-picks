"""Unit tests for the runtime self-audit anomaly evaluation (pure — no DB).

run_self_audit reads two cheap DB aggregates (alerted-but-unsettled backlog,
newest odds-snapshot age) and hands them to evaluate_anomalies, which is pure and
tested here. The thin DB wrapper is exercised live by the scheduled job.
"""

from datetime import UTC, datetime, timedelta


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
