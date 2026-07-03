"""Shared per-index proxy health registry with quarantine (audit 2026-07-03 §5).

The production diagnosis behind this module: dead proxies were retried forever
because every rotation site was memoryless — `_next_proxy` blind round-robin
(oddsportal.py), the Betfair feed sweep trying the FULL pool per target
(betfair_exchange.py), and `_scrape_with_failover` re-entering the dead slot
next lap. This registry gives all three sites one in-process failure memory:

* per-index counters (success / failure / consecutive failures / last success /
  last failure / last error CLASS — never a URL, IP, or credential);
* quarantine: ``consecutive_failures >= threshold`` (default 3) puts the index
  on a cooldown (default 900s), after which it is HALF-OPEN — a single probe is
  granted (a short claim window keeps concurrent selectors off it); a probe
  success fully restores the slot, a failure re-arms the full cooldown;
* FAIL-OPEN: when every index in a requested rotation is quarantined the
  registry returns the full rotation unchanged (type-only, throttled WARNING)
  — the registry must never make availability WORSE than blind round-robin.

Concurrency: this process is a single asyncio event loop and every registry
method is synchronous (no awaits inside), so each call runs atomically between
loop steps — plain dicts suffice and NO locks are needed. Do not add threads.

Secret hygiene: the registry stores and emits pool INDICES and exception class
names only. Proxy URLs/credentials never enter this module.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Defaults mirror the Settings knobs (PROXY_QUARANTINE_THRESHOLD /
# PROXY_QUARANTINE_SECONDS in app/config.py); the composition root calls
# `configure_registry` so env values apply — these keep direct/test
# construction sane without any env read here.
DEFAULT_QUARANTINE_THRESHOLD = 3
DEFAULT_QUARANTINE_SECONDS = 900.0
# Half-open probe claim: once a quarantined slot's cooldown expires, ONE
# selection claims it for this many seconds so concurrent selectors don't all
# pile onto an unproven slot; the claim is short so an untried grant costs
# seconds, not another full cooldown.
HALF_OPEN_CLAIM_SECONDS = 30.0
# A quarantined slot with no success in this long (or ever) is reported "dead"
# in diagnostics — reporting only, it changes no rotation behaviour.
DEAD_AFTER_SECONDS = 3600.0
# Ring capacity for failure timestamps (failovers_15m/1h + dominant class).
_FAILURE_LOG_MAXLEN = 4096
# Throttle for the all-quarantined fail-open WARNING (it can fire per match).
_FAIL_OPEN_LOG_INTERVAL_SECONDS = 60.0


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class ProxySlotHealth:
    """Mutable per-index counters. Holds indices + class names only (no URLs)."""

    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_class: str | None = None
    quarantined_until: datetime | None = None
    probe_claimed_until: datetime | None = None

    def state(self, now: datetime) -> str:
        """``healthy`` / ``quarantined`` / ``half_open`` at ``now``."""
        if self.quarantined_until is None:
            return "healthy"
        if now < self.quarantined_until:
            return "quarantined"
        return "half_open"


@dataclass
class ProxyHealthRegistry:
    """In-process, single-event-loop proxy health + quarantine (see module doc)."""

    threshold: int = DEFAULT_QUARANTINE_THRESHOLD
    cooldown_seconds: float = DEFAULT_QUARANTINE_SECONDS
    half_open_claim_seconds: float = HALF_OPEN_CLAIM_SECONDS
    clock: Callable[[], datetime] = _utcnow
    _slots: dict[int, ProxySlotHealth] = field(default_factory=dict)
    _failure_log: deque[tuple[datetime, str]] = field(
        default_factory=lambda: deque(maxlen=_FAILURE_LOG_MAXLEN)
    )
    _fail_open_events: int = 0
    _last_fail_open_log: datetime | None = None

    # --- recording ---------------------------------------------------------- #
    def record_success(self, index: int) -> None:
        """A request through pool ``index`` completed transport-clean: reset the
        consecutive-failure streak and lift any quarantine/half-open claim."""
        slot = self._slots.setdefault(index, ProxySlotHealth())
        slot.successes += 1
        slot.consecutive_failures = 0
        slot.last_success_at = self.clock()
        slot.quarantined_until = None
        slot.probe_claimed_until = None

    def record_failure(self, index: int, error_class: str) -> None:
        """A transport failure through pool ``index``. ``error_class`` MUST be a
        type name (e.g. ``TimeoutError``) — never a message/URL. At
        ``threshold`` consecutive failures the index is quarantined for
        ``cooldown_seconds``; a failure while already at/above threshold (the
        half-open probe failing) re-arms the full cooldown."""
        now = self.clock()
        slot = self._slots.setdefault(index, ProxySlotHealth())
        slot.failures += 1
        slot.consecutive_failures += 1
        slot.last_failure_at = now
        slot.last_error_class = error_class
        self._failure_log.append((now, error_class))
        if slot.consecutive_failures >= self.threshold:
            slot.quarantined_until = now + timedelta(seconds=self.cooldown_seconds)
            slot.probe_claimed_until = None

    # --- selection ---------------------------------------------------------- #
    def _eligible(self, index: int, now: datetime) -> bool:
        """Is ``index`` usable right now? Healthy -> yes. Quarantined -> no.
        Half-open -> yes ONCE per claim window (this check CLAIMS the probe)."""
        slot = self._slots.get(index)
        if slot is None:
            return True  # never seen = healthy
        state = slot.state(now)
        if state == "healthy":
            return True
        if state == "quarantined":
            return False
        # half-open: grant a single probe per claim window.
        if slot.probe_claimed_until is not None and now < slot.probe_claimed_until:
            return False
        slot.probe_claimed_until = now + timedelta(seconds=self.half_open_claim_seconds)
        return True

    def _note_fail_open(self, pool_size: int) -> None:
        self._fail_open_events += 1
        now = self.clock()
        last = self._last_fail_open_log
        if last is None or (now - last).total_seconds() >= _FAIL_OPEN_LOG_INTERVAL_SECONDS:
            self._last_fail_open_log = now
            # Count-only — never a proxy URL/IP/credential.
            logger.warning(
                "proxy health: all %d rotation indices quarantined — failing OPEN "
                "to full rotation (registry must never reduce availability)",
                pool_size,
            )

    def filter_rotation(self, order: Sequence[int]) -> list[int]:
        """The eligible indices of ``order``, in order. FAIL-OPEN: when every
        index is quarantined, the FULL rotation is returned unchanged (with a
        throttled type-only WARNING) — never let the registry make things worse."""
        now = self.clock()
        out = [index for index in order if self._eligible(index, now)]
        if out or not order:
            return out
        self._note_fail_open(len(order))
        return list(order)

    def select(self, order: Sequence[int]) -> int | None:
        """The first eligible index of ``order`` (claiming a half-open probe),
        or fail-open ``order[0]`` when all are quarantined; None for empty."""
        if not order:
            return None
        now = self.clock()
        for index in order:
            if self._eligible(index, now):
                return index
        self._note_fail_open(len(order))
        return order[0]

    # --- diagnostics (REDACTED: indices + class names only) ------------------ #
    def diagnostics(self, *, configured: int) -> dict[str, Any]:
        """Auth-gated /resolution/match-rate payload. Contains ONLY pool indices,
        counters, ISO timestamps, exception class names, and fixed operator
        wording — never a proxy URL, host, IP, or credential."""
        now = self.clock()
        slots: list[dict[str, Any]] = []
        quarantined = 0
        dead = 0
        for index in sorted(self._slots):
            slot = self._slots[index]
            state = slot.state(now)
            if state == "quarantined":
                quarantined += 1
                idle = (
                    slot.last_success_at is None
                    or (now - slot.last_success_at).total_seconds() > DEAD_AFTER_SECONDS
                )
                if idle:
                    dead += 1
            slots.append(
                {
                    "index": index,
                    "state": state,
                    "successes": slot.successes,
                    "failures": slot.failures,
                    "consecutive_failures": slot.consecutive_failures,
                    "last_success_at": (
                        slot.last_success_at.isoformat() if slot.last_success_at else None
                    ),
                    "last_failure_at": (
                        slot.last_failure_at.isoformat() if slot.last_failure_at else None
                    ),
                    "last_error_class": slot.last_error_class,
                }
            )
        cut_15m = now - timedelta(seconds=900)
        cut_1h = now - timedelta(seconds=3600)
        recent_1h = [(ts, cls) for ts, cls in self._failure_log if ts >= cut_1h]
        failovers_15m = sum(1 for ts, _cls in recent_1h if ts >= cut_15m)
        class_counts = Counter(cls for _ts, cls in recent_1h)
        dominant = class_counts.most_common(1)[0][0] if class_counts else None
        degraded = quarantined > 0
        return {
            "configured": configured,
            "healthy": max(configured - quarantined, 0),
            "quarantined": quarantined,
            "dead": dead,
            "failovers_15m": failovers_15m,
            "failovers_1h": len(recent_1h),
            "fail_open_events": self._fail_open_events,
            "dominant_failure_class": dominant,
            "quarantine_threshold": self.threshold,
            "quarantine_seconds": self.cooldown_seconds,
            # Operator-spec freshness-risk wording (exact strings): degradation
            # here can slow the scrape, but the 600s odds-age gate DISCARDS any
            # stale candidate — a degraded pool only throttles throughput.
            "verdict": "Proxy pool degraded" if degraded else "Proxy pool healthy",
            "freshness": (
                "Freshness protected: stale candidates are being discarded" if degraded else None
            ),
            "picks": "No stale picks minted" if degraded else None,
            "action": "Replace or expand proxy pool" if degraded else None,
            "slots": slots,
        }


# --------------------------------------------------------------------------- #
# Process-wide shared instance: the three rotation sites (oddsportal.py,
# oddsportal JSON per-match failover, betfair_exchange.py) index the SAME
# Settings-derived pool (settings.scraper_proxies()), so one registry gives a
# dead slot cross-site failure memory. Loaders take an injectable registry for
# tests and default to this singleton.
# --------------------------------------------------------------------------- #
_registry: ProxyHealthRegistry | None = None


def get_registry() -> ProxyHealthRegistry:
    """The process-shared registry (created with the module defaults; the
    composition root applies Settings via `configure_registry`)."""
    global _registry
    if _registry is None:
        _registry = ProxyHealthRegistry()
    return _registry


def configure_registry(*, threshold: int, cooldown_seconds: float) -> ProxyHealthRegistry:
    """Composition-root hook (app/scheduler.py): apply the Settings-derived
    quarantine knobs to the shared registry. Env is read ONLY in app/config.py;
    the values arrive here as plain arguments."""
    registry = get_registry()
    registry.threshold = threshold
    registry.cooldown_seconds = cooldown_seconds
    return registry


def reset_registry_for_tests() -> None:
    """Drop the shared registry (tests only — a fresh one is lazily rebuilt)."""
    global _registry
    _registry = None
