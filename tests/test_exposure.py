"""Daily exposure ledger: accumulation, clipping at the cap, zero-remaining."""

from datetime import date

import pytest

from app.risk.exposure import DailyExposureLedger

DAY = date(2026, 6, 10)
OTHER_DAY = date(2026, 6, 11)


def test_reservations_accumulate() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    assert ledger.reserve(DAY, 0.02) == pytest.approx(0.02)
    assert ledger.reserve(DAY, 0.02) == pytest.approx(0.02)
    assert ledger.used(DAY) == pytest.approx(0.04)
    assert ledger.remaining(DAY) == pytest.approx(0.01)


def test_breaching_request_is_clipped_to_remaining() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    ledger.reserve(DAY, 0.04)
    granted = ledger.reserve(DAY, 0.02)
    assert granted == pytest.approx(0.01)
    assert ledger.remaining(DAY) == pytest.approx(0.0)


def test_zero_remaining_grants_zero() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    ledger.reserve(DAY, 0.05)
    assert ledger.reserve(DAY, 0.02) == 0.0


def test_days_are_independent() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    ledger.reserve(DAY, 0.05)
    assert ledger.reserve(OTHER_DAY, 0.02) == pytest.approx(0.02)


def test_negative_request_raises() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    with pytest.raises(ValueError):
        ledger.reserve(DAY, -0.01)
