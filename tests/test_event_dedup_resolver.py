"""Forward mint-time canonical-event dedup resolver (PR1a, Tier-1).

Cross-source scrapes mint SEPARATE ``events`` rows for one real fixture because
``_get_or_create_event`` keyed only on ``external_ref``. Tier-1 resolves a new
ref to an existing canonical event by the DETERMINISTIC oriented team key
``(sport_id, home_team_id, away_team_id)`` within a kickoff tolerance — no fuzzy
matching, so it is false-merge-proof (the same two teams cannot start a second
meeting within the window). The merged ref is recorded in ``event_source_links``
and a fast-path redirects it on later cycles.

Rollback-isolated against the compose Postgres; skips when the DB is absent.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.storage.models import Event, EventSourceLink
from app.storage.repositories import (
    _get_or_create_event,
    _get_or_create_league,
    _get_or_create_sport,
    _get_or_create_team,
)

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"

KICKOFF = datetime(2026, 6, 10, 18, 0, tzinfo=UTC)


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


async def _ids(  # type: ignore[no-untyped-def]
    session, home: str = "Res Alpha", away: str = "Res Beta", sport: str = "soccer"
):
    sport_id = await _get_or_create_sport(session, sport, sport.title())
    league_id = await _get_or_create_league(session, sport_id, "res-league")
    home_id = await _get_or_create_team(session, sport_id, league_id, home)
    away_id = await _get_or_create_team(session, sport_id, league_id, away)
    return sport_id, league_id, home_id, away_id


async def _mint(  # type: ignore[no-untyped-def]
    session, ids, external_ref: str, starts_at=KICKOFF
) -> int:
    sport_id, league_id, home_id, away_id = ids
    return await _get_or_create_event(
        session, sport_id, league_id, home_id, away_id, external_ref, starts_at
    )


async def _event_count(session) -> int:  # type: ignore[no-untyped-def]
    return await session.scalar(select(func.count()).select_from(Event)) or 0


async def test_cross_source_same_key_merges(session) -> None:  # type: ignore[no-untyped-def]
    """A second-source ref for the same (sport, home, away, kickoff) resolves to
    the SAME canonical event and records an event_source_links row."""
    ids = await _ids(session)
    before = await _event_count(session)
    a = await _mint(session, ids, "oddsportal:1")
    b = await _mint(session, ids, "oddschecker:2")

    assert a == b, "the second source must resolve to the same canonical event"
    assert await _event_count(session) == before + 1, "no duplicate event minted"
    link = await session.scalar(
        select(EventSourceLink).where(EventSourceLink.source_event_id == "oddschecker:2")
    )
    assert link is not None
    assert link.canonical_event_id == a
    assert link.source == "oddschecker"


async def test_start_drift_within_tolerance_merges(session) -> None:  # type: ignore[no-untyped-def]
    """Same teams, kickoffs 1h apart (source estimate vs actual) -> one event."""
    ids = await _ids(session)
    a = await _mint(session, ids, "oddsportal:10", starts_at=KICKOFF)
    b = await _mint(session, ids, "oddschecker:11", starts_at=KICKOFF + timedelta(hours=1))
    assert a == b


async def test_beyond_tolerance_mints_separate(session) -> None:  # type: ignore[no-untyped-def]
    """Same teams but kickoffs >2h apart (leg reversal / rematch / doubleheader)
    are DISTINCT fixtures -> two events."""
    ids = await _ids(session)
    before = await _event_count(session)
    a = await _mint(session, ids, "oddsportal:20", starts_at=KICKOFF)
    b = await _mint(session, ids, "oddschecker:21", starts_at=KICKOFF + timedelta(hours=3))
    assert a != b
    assert await _event_count(session) == before + 2


async def test_distinct_teams_mint_separate(session) -> None:  # type: ignore[no-untyped-def]
    """Different away team -> distinct fixture -> two events (no false merge)."""
    ids1 = await _ids(session, home="Res Alpha", away="Res Beta")
    ids2 = await _ids(session, home="Res Alpha", away="Res Gamma")
    before = await _event_count(session)
    a = await _mint(session, ids1, "oddsportal:30")
    b = await _mint(session, ids2, "oddschecker:31")
    assert a != b
    assert await _event_count(session) == before + 2


async def test_null_starts_at_mints_and_never_merges(session) -> None:  # type: ignore[no-untyped-def]
    """A NULL kickoff has no time anchor -> never keyed on -> mints."""
    ids = await _ids(session)
    a = await _mint(session, ids, "oddsportal:40", starts_at=KICKOFF)
    b = await _mint(session, ids, "oddschecker:41", starts_at=None)
    assert a != b


async def test_date_only_midnight_incoming_mints(session) -> None:  # type: ignore[no-untyped-def]
    """A date-only midnight sentinel (OddsPortal basketball header) is a
    placeholder, not a real kickoff -> not keyed on -> mints separately."""
    ids = await _ids(session, sport="basketball")
    a = await _mint(session, ids, "oddsportal:50", starts_at=KICKOFF)
    b = await _mint(
        session, ids, "oddschecker:51", starts_at=datetime(2026, 6, 10, 0, 0, tzinfo=UTC)
    )
    assert a != b


async def test_exact_ref_fast_path_unchanged(session) -> None:  # type: ignore[no-untyped-def]
    """The same external_ref twice returns the same id with no source link
    (the unchanged Stage-0 fast path)."""
    ids = await _ids(session)
    a = await _mint(session, ids, "oddsportal:60")
    b = await _mint(session, ids, "oddsportal:60")
    assert a == b
    n_links = await session.scalar(
        select(func.count())
        .select_from(EventSourceLink)
        .where(EventSourceLink.source_event_id == "oddsportal:60")
    )
    assert n_links == 0


async def test_link_fast_path_idempotent(session) -> None:  # type: ignore[no-untyped-def]
    """After ref B merges into A, calling with B again returns A via the link
    fast-path and mints nothing new."""
    ids = await _ids(session)
    a = await _mint(session, ids, "oddsportal:70")
    await _mint(session, ids, "oddschecker:71")  # merges into a, writes link
    before = await _event_count(session)
    again = await _mint(session, ids, "oddschecker:71")
    assert again == a
    assert await _event_count(session) == before
