"""Schema contracts: UTC discipline, immutability, strictness, odds bounds."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.base import Market
from app.schemas.events import ResultIn
from app.schemas.odds import OddsSnapshotIn

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def snapshot(**overrides: object) -> OddsSnapshotIn:
    base: dict[str, object] = {
        "event_id": "evt-1",
        "bookmaker": "bookie",
        "market": Market.H2H,
        "selection": "home",
        "decimal_odds": 2.1,
        "captured_at": NOW,
        "ingested_at": NOW,
    }
    base.update(overrides)
    return OddsSnapshotIn(**base)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        snapshot(captured_at=datetime(2026, 6, 10, 12, 0, 0))


def test_aware_non_utc_converted_to_utc() -> None:
    cet = timezone(timedelta(hours=2))
    snap = snapshot(captured_at=datetime(2026, 6, 10, 14, 0, 0, tzinfo=cet))
    assert snap.captured_at == NOW
    assert snap.captured_at.tzinfo == UTC


def test_frozen_mutation_raises() -> None:
    snap = snapshot()
    with pytest.raises(ValidationError):
        snap.decimal_odds = 3.0  # type: ignore[misc]


def test_extra_field_forbidden_on_internal_models() -> None:
    with pytest.raises(ValidationError):
        snapshot(surprise_field="nope")


@pytest.mark.parametrize("bad_odds", [1.0, 0.5, -2.0, float("inf"), float("nan"), 1_000.01])
def test_odds_at_or_below_one_rejected(bad_odds: float) -> None:
    with pytest.raises(ValidationError):
        snapshot(decimal_odds=bad_odds)


def test_age_seconds_uses_provider_time() -> None:
    snap = snapshot(captured_at=NOW - timedelta(seconds=120))
    assert snap.age_seconds(NOW) == pytest.approx(120.0)


@pytest.mark.parametrize("bad_liquidity", [float("inf"), float("nan"), -1.0, 10_000_000_000.0])
def test_invalid_liquidity_rejected(bad_liquidity: float) -> None:
    with pytest.raises(ValidationError):
        snapshot(liquidity=bad_liquidity)


def test_provider_clock_skew_within_five_minutes_allowed() -> None:
    snap = snapshot(captured_at=NOW + timedelta(minutes=5))
    assert snap.captured_at == NOW + timedelta(minutes=5)


def test_materially_future_provider_timestamp_rejected() -> None:
    with pytest.raises(ValidationError, match="captured_at cannot be more than"):
        snapshot(captured_at=NOW + timedelta(minutes=5, seconds=1))


def test_unknown_market_rejected() -> None:
    with pytest.raises(ValidationError):
        snapshot(market="lay_the_draw")


def test_result_odds_preserve_decimal_precision() -> None:
    result = ResultIn(
        pick_id="1",
        outcome="won",
        bet_placed=True,
        actual_stake=Decimal("12.34"),
        actual_odds=Decimal("2.1234"),
        settled_at=NOW,
    )

    assert result.actual_stake == Decimal("12.34")
    assert result.actual_odds == Decimal("2.1234")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", "e" * 513),
        ("bookmaker", "b" * 513),
        ("selection", "s" * 1025),
        ("market_detail", "m" * 513),
    ],
)
def test_snapshot_provider_identity_byte_limits(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="UTF-8 bytes"):
        snapshot(**{field: value})


def test_snapshot_provider_identity_limits_count_utf8_bytes() -> None:
    assert snapshot(bookmaker="é" * 256).bookmaker == "é" * 256
    with pytest.raises(ValidationError, match="512 UTF-8 bytes"):
        snapshot(bookmaker="é" * 257)
