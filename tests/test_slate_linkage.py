"""DB integration for the proactive Pinnacle slate-linkage pass (compose
Postgres; skip absent).

The demand path (clv_trueup -> resolve_pinnacle_close_snaps) mints
event_source_links rows only at CLV true-up/close time, so today's slate shows
0 linked events all day. link_upcoming_slate walks the upcoming soft-priced
canonical slate and runs the SAME strict resolver (identical matcher + safety
guards + link persistence), so links exist pre-close.

Covers: a matching Pinnacle_* shadow event mints one link with the demand
path's source/confidence fields; a one-sided women marker VETOES the link (no
loosened matching); the pass is idempotent (second run mints nothing new); and
sharp_slate_coverage counts a linked-only Pinnacle event as covered.
No live network.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.resolution.slate_linkage import link_upcoming_slate
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.storage.models import Event, EventSourceLink
from app.storage.repositories import (
    persist_odds_snapshots,
    sharp_slate_coverage,
)
from tests.database import TEST_DATABASE_URL

DB_URL = TEST_DATABASE_URL
KO = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
CAPTURED = KO - timedelta(hours=2)
NOW = KO - timedelta(hours=6)  # slate pass runs pre-kickoff, inside 48h horizon


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


def _snap(event: str, bookmaker: str, selection: str, odds: float) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id=event,
        bookmaker=bookmaker,
        market=Market.H2H,
        selection=selection,
        decimal_odds=odds,
        captured_at=CAPTURED,
        ingested_at=CAPTURED,
    )


async def _seed_event(  # type: ignore[no-untyped-def]
    factory,
    *,
    ref: str,
    home: str,
    away: str,
    sport: str,
    bookmaker: str,
) -> None:
    snaps = [
        _snap(ref, bookmaker, home, 2.10),
        _snap(ref, bookmaker, "Draw", 3.40),
        _snap(ref, bookmaker, away, 3.60),
    ]
    teams = {ref: EventTeams(home=home, away=away, league="slate_league", starts_at=KO)}
    await persist_odds_snapshots(factory, snaps, teams, sport, "slate_league")


async def _links(factory):  # type: ignore[no-untyped-def]
    async with factory() as session:
        return (
            (
                await session.execute(
                    select(EventSourceLink).where(EventSourceLink.source == "pinnacle_arcadia")
                )
            )
            .scalars()
            .all()
        )


async def test_matching_shadow_event_mints_link_like_demand_path(factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_event(
        factory,
        ref="evt-slate-mu",
        home="Manchester United",
        away="Chelsea",
        sport="soccer",
        bookmaker="softbook",
    )
    await _seed_event(
        factory,
        ref="pin-slate-mu",
        home="Manchester United",
        away="Chelsea",
        sport="pinnacle_soccer",
        bookmaker="Pinnacle",
    )
    async with factory() as session:
        report = await link_upcoming_slate(session, now=NOW)
        await session.commit()
    assert report.attempted == 1
    assert report.linked == 1
    assert report.already_linked == 0
    links = await _links(factory)
    assert len(links) == 1
    link = links[0]
    assert link.source == "pinnacle_arcadia"
    assert link.source_event_id == "pin-slate-mu"
    assert link.match_method == "exact_canonical"
    assert link.confidence_score == Decimal("1.000000")
    assert link.active is True
    async with factory() as session:
        canonical_id = await session.scalar(
            select(Event.id).where(Event.external_ref == "evt-slate-mu")
        )
    assert link.canonical_event_id == canonical_id


async def test_one_sided_women_marker_vetoes_link(factory) -> None:  # type: ignore[no-untyped-def]
    # A women's canonical fixture must NEVER link to the marker-less men's
    # arcadia twin (wrong-game linkage, the cardinal sin).
    await _seed_event(
        factory,
        ref="evt-slate-w",
        home="Arsenal W",
        away="Chelsea W",
        sport="soccer",
        bookmaker="softbook",
    )
    await _seed_event(
        factory,
        ref="pin-slate-men",
        home="Arsenal",
        away="Chelsea",
        sport="pinnacle_soccer",
        bookmaker="Pinnacle",
    )
    async with factory() as session:
        report = await link_upcoming_slate(session, now=NOW)
        await session.commit()
    assert report.attempted == 1
    assert report.linked == 0
    assert await _links(factory) == []


async def test_second_run_is_idempotent(factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_event(
        factory,
        ref="evt-slate-idem",
        home="Manchester United",
        away="Chelsea",
        sport="soccer",
        bookmaker="softbook",
    )
    await _seed_event(
        factory,
        ref="pin-slate-idem",
        home="Manchester United",
        away="Chelsea",
        sport="pinnacle_soccer",
        bookmaker="Pinnacle",
    )
    async with factory() as session:
        first = await link_upcoming_slate(session, now=NOW)
        await session.commit()
    assert first.linked == 1
    first_links = await _links(factory)
    async with factory() as session:
        second = await link_upcoming_slate(session, now=NOW)
        await session.commit()
    # The already-linked event is skipped outright: nothing re-attempted,
    # nothing minted, the stored row untouched (matched_at unchanged).
    assert second.attempted == 0
    assert second.linked == 0
    assert second.already_linked == 1
    second_links = await _links(factory)
    assert len(second_links) == 1
    assert second_links[0].id == first_links[0].id
    assert second_links[0].matched_at == first_links[0].matched_at


async def test_sharp_slate_coverage_counts_linked_pinnacle_event(factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_event(
        factory,
        ref="evt-slate-cov",
        home="Manchester United",
        away="Chelsea",
        sport="soccer",
        bookmaker="softbook",
    )
    await _seed_event(
        factory,
        ref="pin-slate-cov",
        home="Manchester United",
        away="Chelsea",
        sport="pinnacle_soccer",
        bookmaker="Pinnacle",
    )
    window_now = CAPTURED + timedelta(minutes=30)
    async with factory() as session:
        before = await sharp_slate_coverage(session, window_minutes=60, now=window_now)
    # Pre-link: the arcadia rows live on the SHADOW event, so the soft slate
    # event has no Pinnacle coverage yet (the dashboard's "Pinnacle 0%").
    assert before.soft_events == 1
    assert before.pinnacle_events == 0
    async with factory() as session:
        report = await link_upcoming_slate(session, now=NOW)
        await session.commit()
    assert report.linked == 1
    async with factory() as session:
        after = await sharp_slate_coverage(session, window_minutes=60, now=window_now)
    # Post-link: the event_source_links row carries the shadow event's fresh
    # Pinnacle rows onto the slate event -> counted as covered.
    assert after.soft_events == 1
    assert after.pinnacle_events == 1
