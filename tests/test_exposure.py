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


def test_release_returns_capacity() -> None:
    # H1 regression: a grant for a pick that turns out to be a DB duplicate
    # must be handed back, or continuous polling burns the cap on re-detections.
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    granted = ledger.reserve(DAY, 0.02)
    ledger.release(DAY, granted)
    assert ledger.used(DAY) == pytest.approx(0.0)
    assert ledger.remaining(DAY) == pytest.approx(0.05)


def test_release_clamps_at_zero() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    ledger.reserve(DAY, 0.01)
    ledger.release(DAY, 0.04)  # over-release must not create capacity debt
    assert ledger.used(DAY) == pytest.approx(0.0)
    assert ledger.remaining(DAY) == pytest.approx(0.05)


def test_release_negative_raises() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    with pytest.raises(ValueError):
        ledger.release(DAY, -0.01)


def test_preload_sets_used_and_caps_further_reservations() -> None:
    # Restart regression: today's persisted exposure must count against the
    # cap from the first cycle after a restart, not restart at zero.
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    ledger.preload(DAY, 0.03)
    assert ledger.used(DAY) == pytest.approx(0.03)
    assert ledger.remaining(DAY) == pytest.approx(0.02)
    assert ledger.reserve(DAY, 0.04) == pytest.approx(0.02)  # clipped at cap


def test_preload_overwrites_rather_than_accumulates() -> None:
    # SETS the day's used amount: a re-seed must be idempotent.
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    ledger.preload(DAY, 0.03)
    ledger.preload(DAY, 0.03)
    assert ledger.used(DAY) == pytest.approx(0.03)
    ledger.preload(DAY, 0.01)
    assert ledger.used(DAY) == pytest.approx(0.01)


def test_preload_other_days_untouched_and_negative_raises() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    ledger.preload(DAY, 0.02)
    assert ledger.used(OTHER_DAY) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        ledger.preload(DAY, -0.01)


# --- Per-event sub-cap (Kelly correlation backstop) --------------------------


def test_per_event_subcap_bounds_combined_reservations() -> None:
    # Two +EV selections on ONE event_id must not jointly exceed the per-event
    # cap, even though the daily cap has plenty of room. Kelly assumes
    # independent bets; multiple selections on the same match are correlated,
    # so their COMBINED exposure is bounded.
    ledger = DailyExposureLedger(max_daily_fraction=0.05, max_event_fraction=0.03)
    assert ledger.reserve(DAY, 0.02, "evt-1") == pytest.approx(0.02)
    # second selection on the same event is clipped to the remaining event room
    assert ledger.reserve(DAY, 0.02, "evt-1") == pytest.approx(0.01)
    # the per-event cap is now exhausted: a third selection gets nothing
    assert ledger.reserve(DAY, 0.02, "evt-1") == 0.0
    assert ledger.event_used(DAY, "evt-1") == pytest.approx(0.03)
    # daily ledger still tracks the real consumed total
    assert ledger.used(DAY) == pytest.approx(0.03)


def test_per_event_subcap_independent_across_events() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05, max_event_fraction=0.03)
    assert ledger.reserve(DAY, 0.02, "evt-1") == pytest.approx(0.02)
    assert ledger.reserve(DAY, 0.02, "evt-2") == pytest.approx(0.02)
    assert ledger.used(DAY) == pytest.approx(0.04)
    assert ledger.event_used(DAY, "evt-1") == pytest.approx(0.02)
    assert ledger.event_used(DAY, "evt-2") == pytest.approx(0.02)


def test_per_event_subcap_still_bounded_by_daily() -> None:
    # The per-event cap never overrides the daily cap: the tighter of the two
    # binds. Daily room (0.01) is smaller than per-event room here.
    ledger = DailyExposureLedger(max_daily_fraction=0.05, max_event_fraction=0.04)
    ledger.reserve(DAY, 0.04, "evt-1")
    assert ledger.reserve(DAY, 0.04, "evt-2") == pytest.approx(0.01)  # daily binds


def test_per_event_subcap_disabled_when_none() -> None:
    # max_event_fraction=None (default) keeps the plain daily-only behaviour:
    # multiple selections on one event accumulate up to the daily cap.
    ledger = DailyExposureLedger(max_daily_fraction=0.05)
    assert ledger.reserve(DAY, 0.02, "evt-1") == pytest.approx(0.02)
    assert ledger.reserve(DAY, 0.02, "evt-1") == pytest.approx(0.02)
    assert ledger.used(DAY) == pytest.approx(0.04)


def test_per_event_release_returns_event_capacity() -> None:
    ledger = DailyExposureLedger(max_daily_fraction=0.05, max_event_fraction=0.03)
    granted = ledger.reserve(DAY, 0.02, "evt-1")
    ledger.release(DAY, granted, "evt-1")
    assert ledger.used(DAY) == pytest.approx(0.0)
    assert ledger.event_used(DAY, "evt-1") == pytest.approx(0.0)
    # full event room is available again
    assert ledger.reserve(DAY, 0.03, "evt-1") == pytest.approx(0.03)
