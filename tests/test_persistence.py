"""Pick persistence against a real Postgres (compose). Skips if DB is absent.

Uses a savepoint-style rollback: each test runs in a transaction that is
rolled back, so the warehouse is never mutated by the suite.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Event, Pick
from app.storage.repositories import (
    latest_picks_with_events,
    persist_pick,
    refresh_event_kickoffs,
)

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai"


def make_pick(event_id: str = "evt-persist-test") -> PickOut:
    return PickOut(
        pick_id="p-1",
        sport="soccer",
        league="test-league-persist",
        event="Alpha FC vs Beta United",
        event_id=event_id,
        market=Market.H2H,
        selection="Alpha FC",
        bookmaker="testbook",
        decimal_odds=2.10,
        model_probability=0.55,
        fair_probability=0.50,
        edge=0.05,
        ev=0.155,
        confidence=0.70,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary="persistence test",
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.begin()
        try:
            yield s
        finally:
            await s.rollback()
    await engine.dispose()


async def test_persist_pick_inserts_then_dedupes(session) -> None:  # type: ignore[no-untyped-def]
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")

    inserted = await persist_pick(session, make_pick(), teams, "dixon-coles", "test-1")
    assert inserted is True

    count = await session.scalar(
        select(func.count()).select_from(Pick).where(Pick.bookmaker == "testbook")
    )
    assert count == 1

    # Same natural key -> deduped (no second row)
    again = await persist_pick(session, make_pick(), teams, "dixon-coles", "test-1")
    assert again is False
    count2 = await session.scalar(
        select(func.count()).select_from(Pick).where(Pick.bookmaker == "testbook")
    )
    assert count2 == 1


async def test_persisted_pick_roundtrips_fields(session) -> None:  # type: ignore[no-untyped-def]
    teams = EventTeams(home="Alpha FC", away="Beta United")
    await persist_pick(session, make_pick("evt-roundtrip"), teams, "dixon-coles", "test-2")
    row = await session.scalar(
        select(Pick).where(Pick.bookmaker == "testbook").order_by(Pick.id.desc())
    )
    assert row is not None
    assert row.selection == "Alpha FC"
    assert row.market == "h2h"
    assert row.status == "alerted"
    assert row.decimal_odds == Decimal("2.1000")
    assert row.stake_breakdown["final"] == 0.02


async def test_version_bump_supersedes_older_open_pick(session) -> None:  # type: ignore[no-untyped-def]
    # Same (event, market, selection) under a NEW strategy version must not
    # show twice on the dashboard: the old open row flips to 'superseded'.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(session, make_pick("evt-supersede"), teams, "value-sharp-vs-soft", "v2")
    await persist_pick(session, make_pick("evt-supersede"), teams, "value-sharp-vs-soft", "v3")

    rows = (
        (
            await session.execute(
                select(Pick.status).where(Pick.bookmaker == "testbook").order_by(Pick.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == ["superseded", "alerted"]


async def test_refresh_event_kickoffs_upgrades_placeholder(session) -> None:  # type: ignore[no-untyped-def]
    # Events created before the source kickoff was known carry a pick-time
    # placeholder; a later scrape with the real kickoff must correct them.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(session, make_pick("evt-kickoff-fix"), teams, "value-sharp-vs-soft", "t-4")

    real_kickoff = datetime(2026, 6, 25, 1, 0, tzinfo=UTC)
    changed = await refresh_event_kickoffs(session, {"evt-kickoff-fix": real_kickoff})
    assert changed == 1
    row = await session.scalar(select(Event).where(Event.external_ref == "evt-kickoff-fix"))
    assert row is not None
    assert row.starts_at == real_kickoff
    # idempotent: same kickoff again -> no change
    assert await refresh_event_kickoffs(session, {"evt-kickoff-fix": real_kickoff}) == 0


async def test_unknown_kickoff_persists_null_and_serializes_null(session) -> None:  # type: ignore[no-untyped-def]
    # When the source reports no kickoff, the event stores NULL — the REAL
    # "kickoff unknown" signal. The old placeholder (pick-time timestamp) was
    # undetectable client-side: Pick.created_at is a separate clock read, so
    # starts_at == created_at never held and TBD rows rendered as started
    # matches with live settle buttons.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    assert teams.starts_at is None
    await persist_pick(session, make_pick("evt-kickoff-null"), teams, "value-sharp-vs-soft", "t-5")

    row = await session.scalar(select(Event).where(Event.external_ref == "evt-kickoff-null"))
    assert row is not None
    assert row.starts_at is None  # NULL, not a fake pick-time kickoff

    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["event_id"] == row.id]
    assert ours, "persisted pick missing from the /picks payload"
    assert ours[0]["starts_at"] is None  # dashboard renders TBD, no settle


async def test_unknown_kickoff_upgrades_once_source_reports_it(session) -> None:  # type: ignore[no-untyped-def]
    # A NULL kickoff must heal through BOTH paths: a later pick that knows
    # the kickoff (_get_or_create_event) and the per-cycle refresh.
    teams_unknown = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(
        session, make_pick("evt-kickoff-heal"), teams_unknown, "value-sharp-vs-soft", "t-6"
    )

    kickoff = datetime(2026, 6, 27, 18, 30, tzinfo=UTC)
    teams_known = EventTeams(
        home="Alpha FC", away="Beta United", league="test-league-persist", starts_at=kickoff
    )
    # same event re-detected, now with a known kickoff (dedupe path)
    await persist_pick(
        session, make_pick("evt-kickoff-heal"), teams_known, "value-sharp-vs-soft", "t-6"
    )
    row = await session.scalar(select(Event).where(Event.external_ref == "evt-kickoff-heal"))
    assert row is not None
    assert row.starts_at == kickoff

    # and refresh_event_kickoffs upgrades a NULL row too
    await persist_pick(
        session, make_pick("evt-kickoff-heal2"), teams_unknown, "value-sharp-vs-soft", "t-6"
    )
    assert await refresh_event_kickoffs(session, {"evt-kickoff-heal2": kickoff}) == 1
    row2 = await session.scalar(select(Event).where(Event.external_ref == "evt-kickoff-heal2"))
    assert row2 is not None
    assert row2.starts_at == kickoff


async def test_latest_picks_payload_carries_event_fields(session) -> None:  # type: ignore[no-untyped-def]
    # The dashboard needs match label / league / kickoff — not bare event ids.
    kickoff = datetime(2026, 6, 11, 19, 0, tzinfo=UTC)
    teams = EventTeams(
        home="Alpha FC", away="Beta United", league="test-league-persist", starts_at=kickoff
    )
    await persist_pick(session, make_pick("evt-dashboard"), teams, "value-sharp-vs-soft", "t-3")

    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["bookmaker"] == "testbook"]
    assert ours, "persisted pick missing from the /picks payload"
    p = ours[0]
    assert p["event"] == "Alpha FC vs Beta United"
    assert p["league"] == "test-league-persist"
    assert p["starts_at"] == kickoff.isoformat()  # real kickoff, UTC ISO-8601
    assert p["selection"] == "Alpha FC"
    assert p["reason_summary"] == "persistence test"
    # API payload is data-only; the safety reminder lives in alerts
    # (app/schemas/picks.py, safety_audit check 8) and the dashboard banner.
    assert "manual_betting_reminder" not in p
