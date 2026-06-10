"""Schema contracts: UTC discipline, immutability, strictness, odds bounds."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas.base import Market
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
    return OddsSnapshotIn(**base)  # type: ignore[arg-type]


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


@pytest.mark.parametrize("bad_odds", [1.0, 0.5, -2.0])
def test_odds_at_or_below_one_rejected(bad_odds: float) -> None:
    with pytest.raises(ValidationError):
        snapshot(decimal_odds=bad_odds)


def test_age_seconds_uses_provider_time() -> None:
    snap = snapshot(captured_at=NOW - timedelta(seconds=120))
    assert snap.age_seconds(NOW) == pytest.approx(120.0)


def test_unknown_market_rejected() -> None:
    with pytest.raises(ValidationError):
        snapshot(market="lay_the_draw")
