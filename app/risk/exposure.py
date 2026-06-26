"""Daily exposure ledger: caps the sum of recommended stake fractions per day.

Pure module. Requests beyond the remaining daily capacity are clipped; at
zero remaining capacity the grant is 0.0 (the pick should then be skipped).

An OPTIONAL per-event sub-cap (`max_event_fraction`) is a Kelly correlation
backstop: full-Kelly assumes independent bets, but multiple +EV selections on
the SAME event are correlated, so their COMBINED exposure is bounded by the
per-event cap (the tighter of the daily and per-event rooms binds). When
`max_event_fraction` is None the per-event accounting is inert and the ledger
behaves exactly as the plain daily-only ledger.
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class DailyExposureLedger:
    max_daily_fraction: float
    # OPTIONAL per-event correlation cap; None disables per-event bounding.
    max_event_fraction: float | None = None
    _used: dict[date, float] = field(default_factory=dict)
    _used_by_event: dict[tuple[date, str], float] = field(default_factory=dict)

    def used(self, day: date) -> float:
        return self._used.get(day, 0.0)

    def remaining(self, day: date) -> float:
        return max(self.max_daily_fraction - self.used(day), 0.0)

    def event_used(self, day: date, event_id: str) -> float:
        return self._used_by_event.get((day, event_id), 0.0)

    def event_remaining(self, day: date, event_id: str) -> float:
        """Per-event room left; the full daily room when per-event capping is off."""
        if self.max_event_fraction is None:
            return self.remaining(day)
        return max(self.max_event_fraction - self.event_used(day, event_id), 0.0)

    def reserve(self, day: date, fraction: float, event_id: str | None = None) -> float:
        """Reserve up to `fraction` of bankroll for `day`; returns the grant.

        The grant is bounded by the daily room AND, when `max_event_fraction`
        is set and `event_id` is given, by the remaining per-event room — the
        tighter of the two binds (correlated same-event selections cannot
        jointly exceed the per-event cap).
        """
        if fraction < 0.0:
            raise ValueError(f"requested fraction must be >= 0, got {fraction}")
        granted = min(fraction, self.remaining(day))
        if self.max_event_fraction is not None and event_id is not None:
            granted = min(granted, self.event_remaining(day, event_id))
        if granted > 0.0:
            self._used[day] = self.used(day) + granted
            if event_id is not None:
                self._used_by_event[(day, event_id)] = self.event_used(day, event_id) + granted
        return granted

    def preload(self, day: date, fraction: float) -> None:
        """SET the day's used exposure (idempotent; overwrites, never adds).

        Called at the composition root on startup with the sum of stake
        fractions already recommended today (persisted picks) — the ledger
        is in-memory, so without this a mid-day restart would forget the
        morning's exposure and double the day's recommendable total.
        """
        if fraction < 0.0:
            raise ValueError(f"preloaded fraction must be >= 0, got {fraction}")
        self._used[day] = fraction

    def release(self, day: date, fraction: float, event_id: str | None = None) -> None:
        """Hand back an unused grant (e.g. the pick was a DB duplicate).

        Without this, continuous polling re-reserves the same pick every
        cycle and exhausts the daily cap on re-detections. Clamped at zero —
        over-releasing must never mint capacity beyond the cap. When
        `event_id` is given the per-event accounting is released too.
        """
        if fraction < 0.0:
            raise ValueError(f"released fraction must be >= 0, got {fraction}")
        if fraction > 0.0:
            self._used[day] = max(self.used(day) - fraction, 0.0)
            if event_id is not None:
                self._used_by_event[(day, event_id)] = max(
                    self.event_used(day, event_id) - fraction, 0.0
                )
