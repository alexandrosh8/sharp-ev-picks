"""Proxy health registry (app/ingestion/proxy_health.py) — quarantine,
half-open probe, fail-open, counters, and the REDACTED diagnostics payload
(audit 2026-07-03 §5: "No quarantine anywhere — dead proxies are retried
forever"). No network, no DB, no env."""

import json
import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.ingestion.proxy_health import (
    ProxyHealthRegistry,
    configure_registry,
    get_registry,
    reset_registry_for_tests,
)


class FakeClock:
    """Deterministic, manually-advanced UTC clock."""

    def __init__(self) -> None:
        self.now = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_registry(**kwargs: object) -> tuple[ProxyHealthRegistry, FakeClock]:
    clock = FakeClock()
    registry = ProxyHealthRegistry(clock=clock, **kwargs)  # type: ignore[arg-type]
    return registry, clock


def test_quarantine_trips_exactly_at_threshold() -> None:
    registry, _clock = make_registry(threshold=3, cooldown_seconds=900.0)
    registry.record_failure(0, "TimeoutError")
    registry.record_failure(0, "TimeoutError")
    assert registry.filter_rotation([0, 1]) == [0, 1]  # 2 < 3: still eligible
    registry.record_failure(0, "TimeoutError")  # 3rd consecutive -> quarantined
    assert registry.filter_rotation([0, 1]) == [1]
    assert registry.select([0, 1]) == 1


def test_success_resets_consecutive_streak() -> None:
    registry, _clock = make_registry(threshold=3)
    registry.record_failure(0, "TimeoutError")
    registry.record_failure(0, "TimeoutError")
    registry.record_success(0)
    registry.record_failure(0, "TimeoutError")
    registry.record_failure(0, "TimeoutError")
    # streak was reset by the success — never reached 3 in a row
    assert registry.filter_rotation([0]) == [0]


def test_cooldown_expiry_grants_single_half_open_probe() -> None:
    registry, clock = make_registry(threshold=3, cooldown_seconds=900.0)
    for _ in range(3):
        registry.record_failure(0, "ConnectionError")
    assert registry.select([0, 1]) == 1  # quarantined -> skipped
    clock.advance(901)  # cooldown expired -> HALF-OPEN
    assert registry.select([0, 1]) == 0  # the single probe is granted...
    assert registry.select([0, 1]) == 1  # ...and a second selector is kept off it


def test_half_open_probe_failure_rearms_full_cooldown() -> None:
    registry, clock = make_registry(threshold=3, cooldown_seconds=900.0)
    for _ in range(3):
        registry.record_failure(0, "ConnectionError")
    clock.advance(901)
    assert registry.select([0, 1]) == 0  # probe granted
    registry.record_failure(0, "ConnectionError")  # probe FAILED
    clock.advance(600)  # inside the re-armed cooldown
    assert registry.select([0, 1]) == 1


def test_half_open_probe_success_restores_slot() -> None:
    registry, clock = make_registry(threshold=3, cooldown_seconds=900.0)
    for _ in range(3):
        registry.record_failure(0, "ConnectionError")
    clock.advance(901)
    assert registry.select([0, 1]) == 0
    registry.record_success(0)  # probe SUCCEEDED
    assert registry.select([0, 1]) == 0  # fully healthy again, no claim window
    assert registry.select([0, 1]) == 0


def test_filter_rotation_does_not_burn_half_open_probe() -> None:
    # Review 2026-07-03 (major): filter_rotation used to CLAIM the half-open
    # probe for every listed index, but its callers attempt only a prefix and
    # exit on first success — the claim was consumed with no probe sent, and
    # the next sweep re-claimed it, starving a recovered slot out of rotation
    # far past the cooldown design. filter_rotation must be claim-free.
    registry, clock = make_registry(threshold=3, cooldown_seconds=900.0)
    for _ in range(3):
        registry.record_failure(0, "ConnectionError")
    clock.advance(901)  # half-open
    assert registry.filter_rotation([0, 1]) == [1, 0]  # listed, claim NOT burned
    assert registry.filter_rotation([0, 1]) == [1, 0]  # ...repeatedly
    assert registry.select([0, 1]) == 0  # the probe is still grantable


def test_filter_rotation_excludes_actively_claimed_probe() -> None:
    # While a selector holds the single half-open probe claim, sweep callers
    # must not pile onto the unproven slot.
    registry, clock = make_registry(threshold=3, cooldown_seconds=900.0)
    for _ in range(3):
        registry.record_failure(0, "ConnectionError")
    clock.advance(901)
    assert registry.select([0, 1]) == 0  # claims the probe
    assert registry.filter_rotation([0, 1]) == [1]


def test_filter_rotation_orders_half_open_last() -> None:
    # Half-open slots are last-resort probes for sweep callers: healthy slots
    # keep rotation order, unproven slots go to the tail (a capped sweep
    # prefers proven transports; the high-frequency select() path probes).
    registry, clock = make_registry(threshold=3, cooldown_seconds=900.0)
    for _ in range(3):
        registry.record_failure(1, "ConnectionError")
    clock.advance(901)
    assert registry.filter_rotation([1, 2, 3]) == [2, 3, 1]


def test_diagnostics_half_open_is_probing_not_healthy() -> None:
    # Review 2026-07-03 (minor): a half-open slot was folded into "healthy",
    # flapping /health to "Proxy pool healthy" every cooldown for a dead proxy.
    # Unproven slots count as `probing`, the verdict stays degraded, and the
    # dead annotation persists through the half-open window.
    registry, clock = make_registry(threshold=3, cooldown_seconds=900.0)
    for _ in range(3):
        registry.record_failure(4, "TimeoutError")
    diag = registry.diagnostics(configured=2)
    assert (diag["quarantined"], diag["probing"], diag["dead"]) == (1, 0, 1)
    clock.advance(901)  # half-open — still unproven
    diag = registry.diagnostics(configured=2)
    assert (diag["quarantined"], diag["probing"], diag["dead"]) == (0, 1, 1)
    assert diag["healthy"] == 1
    assert diag["verdict"] == "Proxy pool degraded"
    assert diag["action"] == "Replace or expand proxy pool"
    registry.record_success(4)  # probe passed — NOW it is healthy
    diag = registry.diagnostics(configured=2)
    assert (diag["quarantined"], diag["probing"], diag["dead"]) == (0, 0, 0)
    assert diag["healthy"] == 2
    assert diag["verdict"] == "Proxy pool healthy"


def test_all_quarantined_fails_open_to_full_rotation() -> None:
    # The registry must NEVER make availability worse than blind round-robin.
    registry, _clock = make_registry(threshold=1)
    registry.record_failure(0, "TimeoutError")
    registry.record_failure(1, "TimeoutError")
    assert registry.filter_rotation([0, 1]) == [0, 1]  # FAIL-OPEN: full order
    assert registry.select([1, 0]) == 1  # FAIL-OPEN: first of the given order
    diag = registry.diagnostics(configured=2)
    assert diag["fail_open_events"] == 2


def test_counters_and_error_class() -> None:
    registry, _clock = make_registry()
    registry.record_success(2)
    registry.record_failure(2, "TimeoutError")
    registry.record_failure(2, "ConnectionError")
    slot = registry._slots[2]
    assert slot.successes == 1
    assert slot.failures == 2
    assert slot.consecutive_failures == 2
    assert slot.last_error_class == "ConnectionError"
    assert slot.last_success_at is not None
    assert slot.last_failure_at is not None
    assert slot.last_success_at.tzinfo is not None  # UTC-aware, never naive


def test_failover_windows_15m_1h_and_dominant_class() -> None:
    registry, clock = make_registry(threshold=99)  # counters only, no quarantine
    registry.record_failure(0, "TimeoutError")  # t0 (falls out of the 1h window)
    clock.advance(3000)
    registry.record_failure(1, "ConnectionError")  # 700s ago at read time
    clock.advance(600)
    registry.record_failure(2, "ConnectionError")  # 100s ago at read time
    clock.advance(100)
    diag = registry.diagnostics(configured=3)
    assert diag["failovers_15m"] == 2
    assert diag["failovers_1h"] == 2  # the t0 failure aged out (3700s ago)
    assert diag["dominant_failure_class"] == "ConnectionError"


def test_diagnostics_shape_and_operator_wording() -> None:
    registry, _clock = make_registry(threshold=3, cooldown_seconds=900.0)
    diag = registry.diagnostics(configured=12)
    assert diag["verdict"] == "Proxy pool healthy"
    assert diag["freshness"] is None
    assert diag["picks"] is None
    assert diag["action"] is None
    assert diag["configured"] == 12
    assert diag["healthy"] == 12
    # degrade one slot (never succeeded -> also "dead")
    for _ in range(3):
        registry.record_failure(4, "TimeoutError")
    diag = registry.diagnostics(configured=12)
    assert diag["quarantined"] == 1
    assert diag["dead"] == 1
    assert diag["healthy"] == 11
    # operator-spec freshness-risk wording, EXACT strings
    assert diag["verdict"] == "Proxy pool degraded"
    assert diag["freshness"] == "Freshness protected: stale candidates are being discarded"
    assert diag["picks"] == "No stale picks minted"
    assert diag["action"] == "Replace or expand proxy pool"
    (slot,) = diag["slots"]
    assert slot["index"] == 4
    assert slot["state"] == "quarantined"
    assert slot["consecutive_failures"] == 3
    assert slot["last_error_class"] == "TimeoutError"


def test_diagnostics_payload_is_redacted() -> None:
    # The payload must carry pool INDICES only — never an IP-like string or an
    # inline-credential proxy URL, even after real-looking traffic.
    registry, _clock = make_registry(threshold=2)
    for index in range(12):
        registry.record_success(index)
    for _ in range(3):
        registry.record_failure(3, "ProxyError")
    payload = json.dumps(registry.diagnostics(configured=12))
    assert not re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", payload)  # no IPs
    assert not re.search(r"\w+://\S*:\S*@", payload)  # no scheme://user:pass@
    assert "http" not in payload.lower()


def test_singleton_configure_and_reset() -> None:
    reset_registry_for_tests()
    registry = configure_registry(threshold=5, cooldown_seconds=120.0)
    assert registry is get_registry()
    assert get_registry().threshold == 5
    assert get_registry().cooldown_seconds == 120.0
    reset_registry_for_tests()
    assert get_registry() is not registry


def test_dashboard_has_proxy_pool_tile() -> None:
    # Sources view carries the proxy-pool row in the source matrix (a
    # null-safe renderer fed straight from the /health proxy_pool payload).
    from tests.test_api import make_app

    text = TestClient(make_app()).get("/").text
    assert 'id="view-sources"' in text
    assert 'id="source-rows"' in text
    assert "function renderProxyRow" in text
    assert "proxy_pool" in text
