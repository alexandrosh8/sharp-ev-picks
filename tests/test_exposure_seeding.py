"""Exposure-ledger startup seeding: today's persisted picks must preload the
in-memory daily cap, or a mid-day restart doubles recommendable exposure.

Uses the compose Postgres with savepoint isolation (skips when absent) —
same fixture pattern as tests/test_clv_trueup.py. The dev warehouse may
already hold real picks created today, so assertions are baseline-relative.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.risk.exposure import DailyExposureLedger
from app.scheduler import seed_exposure_ledger
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Pick
from app.storage.repositories import persist_pick
from tests.database import TEST_DATABASE_URL

DB_URL = TEST_DATABASE_URL
NOW = datetime.now(tz=UTC)


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
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


def make_pick(
    event_id: str,
    stake_fraction: float,
    marker: str,
    tier: str = "premium",
    selection: str = "Home FC",
) -> PickOut:
    return PickOut(
        pick_id=f"p-{marker}",
        sport="soccer",
        league="test-league-seeding",
        event="Home FC vs Away FC",
        event_id=event_id,
        market=Market.H2H,
        selection=selection,
        bookmaker="SoftBook",
        decimal_odds=2.50,
        model_probability=0.45,
        fair_probability=0.40,
        edge=0.05,
        ev=0.125,
        confidence=0.9,
        recommended_stake_fraction=stake_fraction,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(
            raw_kelly=0.1, fractional=0.025, capped=True, final=stake_fraction
        ),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary=marker,
        tier=tier,
        created_at=NOW,
    )


async def _persist_with_created_at(
    factory: async_sessionmaker,
    event_id: str,
    stake_fraction: float,
    marker: str,
    created_at: datetime,
    tier: str = "premium",
    selection: str = "Home FC",
) -> None:
    async with factory() as session:
        await persist_pick(
            session,
            make_pick(event_id, stake_fraction, marker, tier=tier, selection=selection),
            EventTeams(home="Home FC", away="Away FC"),
            "value-sharp-vs-soft",
            "v-seeding-test",
        )
        # server_default now() stamps transaction time; pin the row to the
        # intended day explicitly so the boundary query is exercised.
        await session.execute(
            sa_update(Pick).where(Pick.reason_summary == marker).values(created_at=created_at)
        )
        await session.commit()


async def test_seeding_counts_only_todays_picks(factory) -> None:  # type: ignore[no-untyped-def]
    today_utc = NOW.date()

    baseline_ledger = DailyExposureLedger(max_daily_fraction=1.0)
    await seed_exposure_ledger(baseline_ledger, factory)
    baseline = baseline_ledger.used(today_utc)

    now = datetime.now(tz=UTC)
    await _persist_with_created_at(factory, "evt-seed-today-1", 0.02, "seeding-today-1", now)
    await _persist_with_created_at(factory, "evt-seed-today-2", 0.01, "seeding-today-2", now)
    await _persist_with_created_at(
        factory, "evt-seed-yesterday", 0.04, "seeding-yesterday", now - timedelta(days=1)
    )

    ledger = DailyExposureLedger(max_daily_fraction=1.0)
    await seed_exposure_ledger(ledger, factory)
    # today's two picks count; yesterday's 0.04 must NOT leak into today
    assert ledger.used(today_utc) == pytest.approx(baseline + 0.03, abs=1e-9)


async def test_seeding_prefers_durable_charge_over_created_at_and_recommendation(factory) -> None:  # type: ignore[no-untyped-def]
    today = datetime.now(tz=UTC).date()
    baseline_ledger = DailyExposureLedger(max_daily_fraction=1.0)
    await seed_exposure_ledger(baseline_ledger, factory)
    baseline = baseline_ledger.used(today)

    marker = "seeding-durable-charge"
    await _persist_with_created_at(
        factory,
        "evt-seed-durable",
        0.02,
        marker,
        datetime.now(tz=UTC) - timedelta(days=1),
    )
    async with factory() as session:
        await session.execute(
            sa_update(Pick)
            .where(Pick.reason_summary == marker)
            .values(
                exposure_reserved_on=today,
                exposure_reserved_fraction=Decimal("0.005"),
            )
        )
        await session.commit()

    ledger = DailyExposureLedger(max_daily_fraction=1.0, max_event_fraction=1.0)
    await seed_exposure_ledger(ledger, factory)
    assert ledger.used(today) == pytest.approx(baseline + 0.005, abs=1e-9)
    assert ledger.event_used(today, "evt-seed-durable") == pytest.approx(0.005, abs=1e-9)


async def test_seeding_excludes_volume_tier(factory) -> None:  # type: ignore[no-untyped-def]
    # Volume picks never reserved exposure (their stake fields are
    # informational); counting them on restart would shrink the cap the
    # premium tier is entitled to for the rest of the day.
    today_utc = NOW.date()
    baseline_ledger = DailyExposureLedger(max_daily_fraction=1.0)
    await seed_exposure_ledger(baseline_ledger, factory)
    baseline = baseline_ledger.used(today_utc)

    now = datetime.now(tz=UTC)
    await _persist_with_created_at(
        factory, "evt-seed-volume", 0.02, "seeding-volume", now, tier="volume"
    )
    await _persist_with_created_at(factory, "evt-seed-premium-3", 0.01, "seeding-premium-3", now)

    ledger = DailyExposureLedger(max_daily_fraction=1.0)
    await seed_exposure_ledger(ledger, factory)
    # only the premium 0.01 counts; the volume 0.02 must not leak in
    assert ledger.used(today_utc) == pytest.approx(baseline + 0.01, abs=1e-9)


async def test_seeding_is_idempotent_preload_not_accumulate(factory) -> None:  # type: ignore[no-untyped-def]
    today_utc = NOW.date()
    now = datetime.now(tz=UTC)
    await _persist_with_created_at(factory, "evt-seed-idem", 0.02, "seeding-idem", now)

    ledger = DailyExposureLedger(max_daily_fraction=1.0)
    await seed_exposure_ledger(ledger, factory)
    first = ledger.used(today_utc)
    await seed_exposure_ledger(ledger, factory)
    assert ledger.used(today_utc) == pytest.approx(first, abs=1e-9)


async def test_seeding_rebuilds_per_event_subcap(factory) -> None:  # type: ignore[no-untyped-def]
    # BUG 1: a mid-day restart must reconstruct the per-event correlation
    # sub-cap from the SAME persisted picks as the daily total — not just the
    # daily total. Two premium picks on ONE event must seed event_used to their
    # sum, so the sub-cap keeps bounding correlated same-event exposure from the
    # first post-restart cycle. Property: seeded event_used == sum over today's
    # persisted premium picks for that event.
    today_utc = NOW.date()
    ledger0 = DailyExposureLedger(max_daily_fraction=1.0, max_event_fraction=0.03)
    await seed_exposure_ledger(ledger0, factory)
    base_event = ledger0.event_used(today_utc, "evt-seed-corr")

    now = datetime.now(tz=UTC)
    # two selections on the SAME event (distinct unique key -> both INSERT)
    await _persist_with_created_at(
        factory, "evt-seed-corr", 0.02, "seed-corr-a", now, selection="Home FC"
    )
    await _persist_with_created_at(
        factory, "evt-seed-corr", 0.01, "seed-corr-b", now, selection="Away FC"
    )

    ledger = DailyExposureLedger(max_daily_fraction=1.0, max_event_fraction=0.03)
    await seed_exposure_ledger(ledger, factory)
    # the per-event sub-cap is rebuilt: both same-event picks sum into event_used
    assert ledger.event_used(today_utc, "evt-seed-corr") == pytest.approx(
        base_event + 0.03, abs=1e-9
    )


async def test_seeding_skips_per_event_when_subcap_disabled(factory) -> None:  # type: ignore[no-untyped-def]
    # max_event_fraction=None: the per-event accounting is inert, so seeding
    # must not populate it (no wasted work, and event_remaining stays the full
    # daily room as documented).
    today_utc = NOW.date()
    now = datetime.now(tz=UTC)
    await _persist_with_created_at(
        factory, "evt-seed-nocap", 0.02, "seed-nocap", now, selection="Home FC"
    )

    ledger = DailyExposureLedger(max_daily_fraction=1.0)  # no per-event cap
    await seed_exposure_ledger(ledger, factory)
    assert ledger.event_used(today_utc, "evt-seed-nocap") == pytest.approx(0.0)
