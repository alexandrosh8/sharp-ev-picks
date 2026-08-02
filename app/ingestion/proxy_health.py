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
        consecutive-failure streak and lift any quarantine/half-open claim.

        KNOWN, ACCEPTED race (review 2026-07-03, deferred): a success recorded
        by an attempt that STARTED before a quarantine was armed clears that
        quarantine on completion. Fixing it needs per-attempt tokens; the
        window is seconds wide, the cleared slot just re-quarantines after
        `threshold` failures, and a genuinely-working proxy being un-quarantined
        is the desired outcome anyway."""
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
    def _eligible(self, index: int, now: datetime, *, claim: bool) -> bool:
        """Is ``index`` usable right now? Healthy -> yes. Quarantined -> no.
        Half-open -> yes while unclaimed; with ``claim=True`` the check also
        TAKES the single probe claim. Only attempt-coupled selection may claim
        (review 2026-07-03: filter_rotation claimed for every listed index but
        its callers attempt only a prefix and exit on first success — the claim
        was burned with no probe sent, then instantly re-claimed by the next
        sweep, starving a recovered slot out of rotation past the cooldown)."""
        slot = self._slots.get(index)
        if slot is None:
            return True  # never seen = healthy
        state = slot.state(now)
        if state == "healthy":
            return True
        if state == "quarantined":
            return False
        # half-open: a single probe per claim window.
        if slot.probe_claimed_until is not None and now < slot.probe_claimed_until:
            return False
        if claim:
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
        """The eligible indices of ``order``: healthy slots first in rotation
        order, then UNCLAIMED half-open slots at the tail (last-resort probes —
        a capped sweep prefers proven transports; this call never claims the
        probe, so an unattempted listing costs the slot nothing). FAIL-OPEN:
        when every index is quarantined, the FULL rotation is returned
        unchanged (throttled type-only WARNING) — never let the registry make
        things worse."""
        now = self.clock()
        healthy: list[int] = []
        half_open: list[int] = []
        for index in order:
            if not self._eligible(index, now, claim=False):
                continue
            slot = self._slots.get(index)
            if slot is not None and slot.state(now) == "half_open":
                half_open.append(index)
            else:
                healthy.append(index)
        out = healthy + half_open
        if out or not order:
            return out
        self._note_fail_open(len(order))
        return list(order)

    def select(self, order: Sequence[int]) -> int | None:
        """The first eligible index of ``order`` (CLAIMING a half-open probe —
        this path is attempt-coupled: the returned index is always tried), or
        fail-open ``order[0]`` when all are quarantined; None for empty."""
        if not order:
            return None
        now = self.clock()
        for index in order:
            if self._eligible(index, now, claim=True):
                return index
        self._note_fail_open(len(order))
        return order[0]

    # --- diagnostics (REDACTED: indices + class names only) ------------------ #
    def diagnostics(self, *, configured: int, concurrency_floor: int) -> dict[str, Any]:
        """Auth-gated /resolution/match-rate payload. Contains ONLY pool indices,
        counters, ISO timestamps, exception class names, and fixed operator
        wording — never a proxy URL, host, IP, or credential.

        ``concurrency_floor`` is the scrape's concurrent-task count
        (Settings.oddsportal_concurrency / ODDSPORTAL_CONCURRENCY), passed in by
        the caller — env is read ONLY in app/config.py. ``headroom`` =
        ``healthy - concurrency_floor`` and CAN be negative: at/below zero the
        scrape has no spare proxies over its own parallelism (task B5).

        COUNTER PARTITION (live review 2026-08-02, P3): ``healthy`` +
        ``quarantined`` + ``probing`` + ``dead`` == ``configured`` — the
        categories are DISJOINT. A never-successful (idle > DEAD_AFTER_SECONDS)
        quarantined/half-open slot counts ONLY as ``dead`` (previously it was
        double-counted dead AND quarantined, so healthy+quarantined+dead could
        sum past the pool size). ``slots`` lists EVERY configured index — a
        never-attempted slot appears as ``healthy`` with zero counters (it was
        previously omitted, so 12 configured slots could list only 7). The
        per-slot ``state`` string is unchanged (a dead slot still reads its
        raw quarantined/half_open state; deadness is the top-level category).
        Recorded indices >= ``configured`` (possible only after a pool shrink
        without restart) are listed but excluded from the category counts so
        the partition invariant holds."""
        now = self.clock()
        slots: list[dict[str, Any]] = []
        quarantined = 0
        probing = 0
        dead = 0
        for index in sorted(set(range(configured)) | set(self._slots)):
            slot = self._slots.get(index)
            state = "healthy" if slot is None else slot.state(now)
            # Review 2026-07-03: a half-open slot is UNPROVEN, not healthy —
            # folding it into "healthy" flapped the verdict to "Proxy pool
            # healthy" every cooldown for a permanently dead proxy. It counts
            # as `probing` (and stays dead-annotated) until a probe SUCCEEDS.
            if slot is not None and state in ("quarantined", "half_open") and index < configured:
                idle = (
                    slot.last_success_at is None
                    or (now - slot.last_success_at).total_seconds() > DEAD_AFTER_SECONDS
                )
                if idle:
                    dead += 1
                elif state == "quarantined":
                    quarantined += 1
                else:
                    probing += 1
            slots.append(
                {
                    "index": index,
                    "state": state,
                    "successes": slot.successes if slot is not None else 0,
                    "failures": slot.failures if slot is not None else 0,
                    "consecutive_failures": (slot.consecutive_failures if slot is not None else 0),
                    "last_success_at": (
                        slot.last_success_at.isoformat()
                        if slot is not None and slot.last_success_at
                        else None
                    ),
                    "last_failure_at": (
                        slot.last_failure_at.isoformat()
                        if slot is not None and slot.last_failure_at
                        else None
                    ),
                    "last_error_class": slot.last_error_class if slot is not None else None,
                }
            )
        cut_15m = now - timedelta(seconds=900)
        cut_1h = now - timedelta(seconds=3600)
        recent_1h = [(ts, cls) for ts, cls in self._failure_log if ts >= cut_1h]
        failovers_15m = sum(1 for ts, _cls in recent_1h if ts >= cut_15m)
        class_counts = Counter(cls for _ts, cls in recent_1h)
        dominant = class_counts.most_common(1)[0][0] if class_counts else None
        # Disjoint categories (see docstring): dead is its own bucket now, so
        # it joins the degraded test and the healthy remainder subtraction.
        degraded = (quarantined + probing + dead) > 0
        healthy = max(configured - quarantined - probing - dead, 0)
        return {
            "configured": configured,
            "healthy": healthy,
            # Explicit headroom vs the scrape's own parallelism (task B5):
            # healthy - floor, negative when the pool can't even cover the
            # concurrent task count.
            "concurrency_floor": concurrency_floor,
            "headroom": healthy - concurrency_floor,
            "quarantined": quarantined,
            "probing": probing,
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
