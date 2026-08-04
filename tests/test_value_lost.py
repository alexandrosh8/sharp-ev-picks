"""Operator item 2 (2026-08-04): "when premium lost its value it has to be
mentioned" — the value-lost transition is a first-class event.

Covers:
- the PURE transition helper (hysteresis = floor crossing only: set on the
  first re-priced edge below the tier floor, cleared when a later re-price
  re-qualifies; no extra flapping guard);
- the value-lost alert template ("no longer qualifies — do not bet",
  informational wording only, one dedupe key per TRANSITION);
- the DB-backed revalidation wiring (compose Postgres, skips when absent):
  value_lost_at persisted + exactly one alert per transition, cleared on
  re-qualification, re-set (new alert, new key) on a second loss;
- the /picks serializer exposing value_lost_at.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.repositories import persist_pick
from tests.database import TEST_DATABASE_URL

NOW = datetime.now(tz=UTC)
PREMIUM_FLOOR = 0.015


# --- pure transition helper (table-driven, incl. hysteresis boundaries) ------ #


def test_value_lost_transition_sets_on_first_crossing_below_floor() -> None:
    from app.clv_trueup import value_lost_transition

    lost_at, fired = value_lost_transition(
        tier="premium",
        current_edge=Decimal("0.001"),
        edge_floor=PREMIUM_FLOOR,
        prior_value_lost_at=None,
        now=NOW,
    )
    assert lost_at == NOW
    assert fired is True


def test_value_lost_transition_holds_while_still_below_no_refire() -> None:
    from app.clv_trueup import value_lost_transition

    first = NOW - timedelta(hours=2)
    lost_at, fired = value_lost_transition(
        tier="premium",
        current_edge=Decimal("-0.02"),
        edge_floor=PREMIUM_FLOOR,
        prior_value_lost_at=first,
        now=NOW,
    )
    # One notification per TRANSITION, not per cycle: the original timestamp
    # is preserved and nothing re-fires while the pick stays below the floor.
    assert lost_at == first
    assert fired is False


def test_value_lost_transition_clears_on_requalification() -> None:
    from app.clv_trueup import value_lost_transition

    lost_at, fired = value_lost_transition(
        tier="premium",
        current_edge=Decimal("0.04"),
        edge_floor=PREMIUM_FLOOR,
        prior_value_lost_at=NOW - timedelta(hours=1),
        now=NOW,
    )
    assert lost_at is None
    assert fired is False


def test_value_lost_transition_boundary_at_floor_qualifies() -> None:
    """Hysteresis boundary: edge == floor QUALIFIES (mirrors the dashboard's
    hasQualifyingEdgeNow `edge >= floor`); only a strict crossing below sets."""
    from app.clv_trueup import value_lost_transition

    at_floor, fired = value_lost_transition(
        tier="premium",
        current_edge=Decimal(f"{PREMIUM_FLOOR}"),
        edge_floor=PREMIUM_FLOOR,
        prior_value_lost_at=NOW - timedelta(hours=1),
        now=NOW,
    )
    assert at_floor is None  # at the floor -> re-qualified, cleared
    assert fired is False
    just_below, fired2 = value_lost_transition(
        tier="premium",
        current_edge=Decimal(f"{PREMIUM_FLOOR - 0.000001}"),
        edge_floor=PREMIUM_FLOOR,
        prior_value_lost_at=None,
        now=NOW,
    )
    assert just_below == NOW
    assert fired2 is True


def test_value_lost_transition_out_of_scope_holds_state() -> None:
    from app.clv_trueup import value_lost_transition

    prior = NOW - timedelta(hours=3)
    # volume tier: never in scope (premium-only mention)
    assert value_lost_transition(
        tier="volume",
        current_edge=Decimal("-0.05"),
        edge_floor=PREMIUM_FLOOR,
        prior_value_lost_at=prior,
        now=NOW,
    ) == (prior, False)
    # no live re-price this cycle: cannot prove either way -> state holds
    assert value_lost_transition(
        tier="premium",
        current_edge=None,
        edge_floor=PREMIUM_FLOOR,
        prior_value_lost_at=prior,
        now=NOW,
    ) == (prior, False)
    # no configured floor (legacy caller): feature inert
    assert value_lost_transition(
        tier="premium",
        current_edge=Decimal("-0.05"),
        edge_floor=None,
        prior_value_lost_at=prior,
        now=NOW,
    ) == (prior, False)


# --- alert template + dedupe convention -------------------------------------- #


def test_value_lost_alert_template_and_dedupe() -> None:
    from app.notifications.base import build_value_lost_alert
    from app.schemas.picks import ALERT_FOOTER

    lost_at = datetime(2026, 8, 4, 10, 30, tzinfo=UTC)
    alert = build_value_lost_alert(
        pick_id=946692,
        event="Home FC vs Away FC",
        market="h2h",
        selection="Home FC",
        bookmaker="SoftBook",
        decimal_odds=Decimal("2.50"),
        current_edge=-0.023,
        edge_floor=PREMIUM_FLOOR,
        value_lost_at=lost_at,
    )
    assert "VALUE LOST" in alert.title
    assert "no longer qualifies — do not bet" in alert.body
    assert ALERT_FOOTER in alert.body
    # Informational wording only — never trading-desk framing (picks-only).
    assert "sell" not in alert.body.lower()
    assert "cash out" not in alert.body.lower()
    # No URLs/secrets in alert text (names/counts/types only).
    assert "http" not in alert.body.lower()

    # Same transition -> same key (per-cycle repeats dedupe at the store);
    # a NEW transition (later timestamp) -> a distinct key, so a re-loss
    # after re-qualification alerts again.
    again = build_value_lost_alert(
        pick_id=946692,
        event="Home FC vs Away FC",
        market="h2h",
        selection="Home FC",
        bookmaker="SoftBook",
        decimal_odds=Decimal("2.50"),
        current_edge=-0.023,
        edge_floor=PREMIUM_FLOOR,
        value_lost_at=lost_at,
    )
    relost = build_value_lost_alert(
        pick_id=946692,
        event="Home FC vs Away FC",
        market="h2h",
        selection="Home FC",
        bookmaker="SoftBook",
        decimal_odds=Decimal("2.50"),
        current_edge=-0.019,
        edge_floor=PREMIUM_FLOOR,
        value_lost_at=lost_at + timedelta(hours=4),
    )
    assert alert.dedupe_key == again.dedupe_key
    assert alert.dedupe_key != relost.dedupe_key


# --- DB-backed wiring (compose Postgres; skips when absent) ------------------- #


class RecordingDispatcher:
    def __init__(self) -> None:
        self.alerts: list[object] = []

    async def dispatch(self, alert):  # type: ignore[no-untyped-def]
        self.alerts.append(alert)
        return None


def make_pick(event_id: str, tier: str = "premium") -> PickOut:
    return PickOut(
        pick_id="p-value-lost",
        sport="soccer",
        league="test-league-value-lost",
        event="Home FC vs Away FC",
        event_id=event_id,
        market=Market.H2H,
        selection="Home FC",
        bookmaker="SoftBook",
        tier=tier,
        decimal_odds=2.50,
        model_probability=0.45,
        fair_probability=0.40,
        edge=0.05,
        ev=0.125,
        confidence=0.9,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary="value lost transition test",
        created_at=NOW,
    )


def snapshots(event_id: str, soft_home: float) -> list[OddsSnapshotIn]:
    """Pinnacle anchor 2.20/3.40/3.30 (fair home ~0.43); the pick's own
    SoftBook home price is `soft_home` — 2.30 re-prices the edge to ~0
    (below the 1.5% premium floor), 2.60 re-prices it to ~+4.5% (re-qualified)."""
    rows: list[OddsSnapshotIn] = []
    books: dict[str, tuple[float, float, float]] = {
        "Pinnacle": (2.20, 3.40, 3.30),
        "SoftBook": (soft_home, 3.35, 3.20),
    }
    for book, prices in books.items():
        for sel, odds in zip(("Home FC", "Draw", "Away FC"), prices, strict=True):
            rows.append(
                OddsSnapshotIn(
                    event_id=event_id,
                    bookmaker=book,
                    market=Market.H2H,
                    selection=sel,
                    decimal_odds=odds,
                    captured_at=NOW,
                    ingested_at=NOW,
                )
            )
    return rows


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


async def _seed(factory, event_id: str, tier: str = "premium") -> None:  # type: ignore[no-untyped-def]
    async with factory() as session:
        await persist_pick(
            session,
            make_pick(event_id, tier=tier),
            EventTeams(home="Home FC", away="Away FC"),
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.commit()


async def _pick_row(factory, event_id: str):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.storage.models import Event, Pick

    async with factory() as session:
        return (
            await session.execute(
                select(Pick)
                .join(Event, Pick.event_id == Event.id)
                .where(Event.external_ref == event_id)
            )
        ).scalar_one()


async def test_revalidation_persists_transition_and_alerts_once(factory) -> None:  # type: ignore[no-untyped-def]
    from app.clv_trueup import revalidate_open_picks
    from app.probabilities.devig import DevigMethod

    event_id = "evt-value-lost-1"
    await _seed(factory, event_id)
    dispatcher = RecordingDispatcher()

    # Cycle 1: re-priced edge falls below the premium floor -> transition
    # persisted + exactly ONE alert dispatched.
    assert (
        await revalidate_open_picks(
            factory,
            snapshots(event_id, soft_home=2.30),
            DevigMethod.POWER,
            premium_min_edge=PREMIUM_FLOOR,
            dispatcher=dispatcher,
        )
        == 1
    )
    pick = await _pick_row(factory, event_id)
    assert pick.value_lost_at is not None
    first_lost_at = pick.value_lost_at
    assert len(dispatcher.alerts) == 1
    first_key = dispatcher.alerts[0].dedupe_key  # type: ignore[attr-defined]
    assert "do not bet" in dispatcher.alerts[0].body  # type: ignore[attr-defined]

    # Cycle 2: still below the floor -> timestamp holds, NO second alert
    # (one notification per transition, not per cycle).
    await revalidate_open_picks(
        factory,
        snapshots(event_id, soft_home=2.30),
        DevigMethod.POWER,
        premium_min_edge=PREMIUM_FLOOR,
        dispatcher=dispatcher,
    )
    pick = await _pick_row(factory, event_id)
    assert pick.value_lost_at == first_lost_at
    assert len(dispatcher.alerts) == 1

    # Cycle 3: price drifts back out -> re-qualifies, timestamp CLEARED.
    await revalidate_open_picks(
        factory,
        snapshots(event_id, soft_home=2.60),
        DevigMethod.POWER,
        premium_min_edge=PREMIUM_FLOOR,
        dispatcher=dispatcher,
    )
    pick = await _pick_row(factory, event_id)
    assert pick.value_lost_at is None
    assert len(dispatcher.alerts) == 1

    # Cycle 4: value collapses again -> a NEW transition: fresh timestamp,
    # second alert, DISTINCT dedupe key.
    await revalidate_open_picks(
        factory,
        snapshots(event_id, soft_home=2.30),
        DevigMethod.POWER,
        premium_min_edge=PREMIUM_FLOOR,
        dispatcher=dispatcher,
    )
    pick = await _pick_row(factory, event_id)
    assert pick.value_lost_at is not None
    assert len(dispatcher.alerts) == 2
    assert dispatcher.alerts[1].dedupe_key != first_key  # type: ignore[attr-defined]


async def test_revalidation_volume_tier_never_transitions(factory) -> None:  # type: ignore[no-untyped-def]
    from app.clv_trueup import revalidate_open_picks
    from app.probabilities.devig import DevigMethod

    event_id = "evt-value-lost-volume"
    await _seed(factory, event_id, tier="volume")
    dispatcher = RecordingDispatcher()
    await revalidate_open_picks(
        factory,
        snapshots(event_id, soft_home=2.30),
        DevigMethod.POWER,
        premium_min_edge=PREMIUM_FLOOR,
        dispatcher=dispatcher,
    )
    pick = await _pick_row(factory, event_id)
    assert pick.value_lost_at is None
    assert dispatcher.alerts == []


async def test_picks_serializer_exposes_value_lost_at(factory) -> None:  # type: ignore[no-untyped-def]
    from app.clv_trueup import revalidate_open_picks
    from app.probabilities.devig import DevigMethod
    from app.storage.repositories import latest_picks_with_events

    event_id = "evt-value-lost-serialized"
    await _seed(factory, event_id)
    await revalidate_open_picks(
        factory,
        snapshots(event_id, soft_home=2.30),
        DevigMethod.POWER,
        premium_min_edge=PREMIUM_FLOOR,
    )
    async with factory() as session:
        rows = await latest_picks_with_events(
            session, tier="premium", min_edge=PREMIUM_FLOOR, volume_min_edge=0.005
        )
    row = next(r for r in rows if r["event_id"] is not None and r["event"] == "Home FC vs Away FC")
    assert row["value_lost_at"] is not None
    # ISO-8601 UTC string, consistent with every other timestamp field.
    datetime.fromisoformat(row["value_lost_at"])
