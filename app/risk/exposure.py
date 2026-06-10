"""Daily exposure ledger: caps the sum of recommended stake fractions per day.

Pure module. Requests beyond the remaining daily capacity are clipped; at
zero remaining capacity the grant is 0.0 (the pick should then be skipped).
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class DailyExposureLedger:
    max_daily_fraction: float
    _used: dict[date, float] = field(default_factory=dict)

    def used(self, day: date) -> float:
        return self._used.get(day, 0.0)

    def remaining(self, day: date) -> float:
        return max(self.max_daily_fraction - self.used(day), 0.0)

    def reserve(self, day: date, fraction: float) -> float:
        """Reserve up to `fraction` of bankroll for `day`; returns the grant."""
        if fraction < 0.0:
            raise ValueError(f"requested fraction must be >= 0, got {fraction}")
        granted = min(fraction, self.remaining(day))
        if granted > 0.0:
            self._used[day] = self.used(day) + granted
        return granted
