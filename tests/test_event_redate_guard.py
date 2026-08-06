"""Event re-date guard (_get_or_create_event) against compose Postgres.

Live defect 2026-08: an OddsChecker NUMERIC external_ref was re-issued ~6 days
later for a DIFFERENT real-world fixture (settled Washington picks + a fresh
Montreal pick fused onto one canonical event). The existing-row fast-path used
to apply prefer_kickoff unconditionally, silently re-dating the settled event.

Guard contract:
- a starts_at move within the 48h window updates as before (postponements);
- a move > 48h on an event WITH settled picks is REFUSED — starts_at stays,
  a match_review_queue row (reason 'redate_settled_event') routes it to the
  operator;
- a move > 48h on a pick-less event is allowed with an INFO line (no settled
  money at risk; blocking would just strand a re-scheduled fixture).

Same savepoint-isolated fixture pattern as tests/test_odds_snapshot_persistence.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Event, MatchReviewQueue, Pick
from app.storage.repositories import persist_odds_snapshots, persist_pick
from tests.database import TEST_DATABASE_URL

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
T0 = NOW + timedelta(hours=30)


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


def _snap(ref: str, sel: str, odds: float, captured: datetime) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id=ref,
        bookmaker="SoftBook",
        market=Market.H2H,
        selection=sel,
        decimal_odds=odds,
        captured_at=captured,
        ingested_at=captured,
    )


async def _seed_event(maker, ref: str, home: str, away: str, starts_at: datetime) -> None:  # type: ignore[no-untyped-def]
    teams = {ref: EventTeams(home=home, away=away, league="redate-league", starts_at=starts_at)}
    written = await persist_odds_snapshots(
        maker, [_snap(ref, home, 2.10, NOW)], teams, "soccer", "redate-league"
    )
    assert written == 1


async def _repersist(maker, ref: str, home: str, away: str, starts_at: datetime) -> None:  # type: ignore[no-untyped-def]
    teams = {ref: EventTeams(home=home, away=away, league="redate-league", starts_at=starts_at)}
    await persist_odds_snapshots(
        maker,
        [_snap(ref, home, 2.35, NOW + timedelta(minutes=10))],
        teams,
        "soccer",
        "redate-league",
    )


async def _stored_starts_at(maker, ref: str) -> datetime | None:  # type: ignore[no-untyped-def]
    async with maker() as session:
        return await session.scalar(select(Event.starts_at).where(Event.external_ref == ref))


async def _seed_settled_pick(maker, ref: str, home: str, away: str, starts_at: datetime) -> None:  # type: ignore[no-untyped-def]
    pick = PickOut(
        pick_id=f"p-{ref}",
        sport="soccer",
        league="redate-league",
        event=f"{home} vs {away}",
        event_id=ref,
        market=Market.H2H,
        selection=home,
        bookmaker="SoftBook",
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
        reason_summary="redate guard test",
        tier="volume",
        created_at=NOW,
    )
    teams = EventTeams(home=home, away=away, league="redate-league", starts_at=starts_at)
    async with maker() as session:
        assert await persist_pick(session, pick, teams, "value", "test-v")
        # Column-targeted flip (no full-row Pick load — keeps the test tolerant
        # of in-flight model columns not yet migrated on the scratch DB).
        pick_id = await session.scalar(
            select(Pick.id).join(Event, Pick.event_id == Event.id).where(Event.external_ref == ref)
        )
        assert pick_id is not None
        await session.execute(sa_update(Pick).where(Pick.id == pick_id).values(status="settled"))
        await session.commit()


async def test_redate_within_window_updates(factory) -> None:  # type: ignore[no-untyped-def]
    """A <=48h kickoff move (ordinary postponement / better source time) keeps
    the pre-guard behavior: prefer_kickoff updates the stored starts_at."""
    ref = "evt-redate-within-1"
    await _seed_event(factory, ref, "Redate Home A", "Redate Away A", T0)
    moved = T0 + timedelta(hours=24)
    await _repersist(factory, ref, "Redate Home A", "Redate Away A", moved)
    assert await _stored_starts_at(factory, ref) == moved


async def test_redate_beyond_window_with_settled_picks_refused_and_queued(  # type: ignore[no-untyped-def]
    factory,
) -> None:
    """>48h move on an event with a SETTLED pick: refuse the re-date (starts_at
    unchanged) and enqueue a match_review_queue row for the operator — the ref
    was likely re-issued for a different real-world fixture."""
    ref = "evt-redate-settled-1"
    home, away = "Redate Home B", "Redate Away B"
    await _seed_event(factory, ref, home, away, T0)
    await _seed_settled_pick(factory, ref, home, away, T0)

    moved = T0 + timedelta(days=6)
    await _repersist(factory, ref, home, away, moved)

    assert await _stored_starts_at(factory, ref) == T0  # unchanged
    async with factory() as session:
        event_id = await session.scalar(select(Event.id).where(Event.external_ref == ref))
        review = await session.scalar(
            select(MatchReviewQueue).where(
                MatchReviewQueue.source_event_id == ref,
                MatchReviewQueue.reason == "redate_settled_event",
            )
        )
    assert review is not None
    assert review.candidate_canonical_event_id == event_id

    # Idempotent across cycles: a re-observation of the same move adds no
    # second queue row (uq_match_review_queue_dedupe).
    await _repersist(factory, ref, home, away, moved)
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(MatchReviewQueue.id).where(
                        MatchReviewQueue.source_event_id == ref,
                        MatchReviewQueue.reason == "redate_settled_event",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_redate_beyond_window_without_picks_allowed(  # type: ignore[no-untyped-def]
    factory, caplog: pytest.LogCaptureFixture
) -> None:
    """>48h move on a PICK-LESS event is allowed (re-scheduled fixture, no
    settled money) — with an INFO line for the audit trail."""
    ref = "evt-redate-pickless-1"
    await _seed_event(factory, ref, "Redate Home C", "Redate Away C", T0)
    moved = T0 + timedelta(days=6)
    with caplog.at_level("INFO", logger="app.storage.repositories"):
        await _repersist(factory, ref, "Redate Home C", "Redate Away C", moved)
    assert await _stored_starts_at(factory, ref) == moved
    assert any(
        "re-date" in record.getMessage() and ref in record.getMessage() for record in caplog.records
    )
