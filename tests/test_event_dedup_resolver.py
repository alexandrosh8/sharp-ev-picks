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
from decimal import Decimal

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
from tests.database import TEST_DATABASE_URL

DB_URL = TEST_DATABASE_URL

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


# --- PR1b: Tier-2 fixture_pair_key resolver ---------------------------------
# A name-twin / [In Running] fork mints DIFFERENT home/away team_ids across
# sources (so Tier-1's exact-id key misses), yet the normalized UNORDERED team
# pair is identical. Constructed here with a club-form noise token ("FC") that
# normalize_name drops: "Res Alpha FC" is a DISTINCT team row from "Res Alpha"
# (distinct normalized_name), but fixture_pair_key("Res Alpha FC", "Res Beta")
# == fixture_pair_key("Res Alpha", "Res Beta"). (The live-status marker route is
# no longer usable — _get_or_create_team strips it, folding the id at Tier-0.)


async def test_name_twin_fork_resolves_via_fixture_pair_key(session) -> None:  # type: ignore[no-untyped-def]
    """Different team_ids but identical fixture_pair_key within tolerance ->
    Tier-2 folds onto the existing canonical (no new event, link written with the
    'fixture_pair_key' method)."""
    ids1 = await _ids(session, home="Res Alpha", away="Res Beta")
    ids2 = await _ids(session, home="Res Alpha FC", away="Res Beta")
    assert ids1[2] != ids2[2], "the 'FC' twin must be a distinct team row (Tier-1 misses)"
    before = await _event_count(session)
    a = await _mint(session, ids1, "oddsportal:100", starts_at=KICKOFF)
    b = await _mint(session, ids2, "oddschecker:101", starts_at=KICKOFF + timedelta(hours=1))
    assert a == b, "Tier-2 fixture_pair_key must fold the name-twin fork"
    assert await _event_count(session) == before + 1, "no duplicate event minted"
    link = await session.scalar(
        select(EventSourceLink).where(EventSourceLink.source_event_id == "oddschecker:101")
    )
    assert link is not None
    assert link.canonical_event_id == a
    assert link.source == "oddschecker"
    assert link.match_method == "fixture_pair_key"


async def test_name_twin_fork_beyond_tolerance_mints_separate(session) -> None:  # type: ignore[no-untyped-def]
    """Same fixture_pair_key but kickoffs >2h apart (team sport) are DISTINCT
    fixtures -> Tier-2 must NOT merge."""
    ids1 = await _ids(session, home="Res Alpha", away="Res Beta")
    ids2 = await _ids(session, home="Res Alpha FC", away="Res Beta")
    before = await _event_count(session)
    a = await _mint(session, ids1, "oddsportal:110", starts_at=KICKOFF)
    b = await _mint(session, ids2, "oddschecker:111", starts_at=KICKOFF + timedelta(hours=3))
    assert a != b
    assert await _event_count(session) == before + 2


async def test_name_twin_fork_cross_sport_mints_separate(session) -> None:  # type: ignore[no-untyped-def]
    """Same names/pair-key but DIFFERENT sport -> Tier-2 never matches across
    sport_id (a same-named basketball and soccer fixture are unrelated)."""
    ids1 = await _ids(session, home="Res Alpha", away="Res Beta", sport="soccer")
    ids2 = await _ids(session, home="Res Alpha FC", away="Res Beta", sport="basketball")
    before = await _event_count(session)
    a = await _mint(session, ids1, "oddsportal:120", starts_at=KICKOFF)
    b = await _mint(session, ids2, "oddschecker:121", starts_at=KICKOFF)
    assert a != b
    assert await _event_count(session) == before + 2


async def test_distinct_club_near_normalization_mints_separate(session) -> None:  # type: ignore[no-untyped-def]
    """Guards the normalization-collision trap: two clubs sharing a base token
    ('CD Nacional' vs 'Nacional') have DIFFERENT fixture_pair_keys ('cd' is NOT a
    noise token), so Tier-2 (exact normalized pair only) must NOT merge them."""
    ids1 = await _ids(session, home="CD Nacional", away="Res Beta")
    ids2 = await _ids(session, home="Nacional", away="Res Beta")
    before = await _event_count(session)
    a = await _mint(session, ids1, "oddsportal:130", starts_at=KICKOFF)
    b = await _mint(session, ids2, "oddschecker:131", starts_at=KICKOFF)
    assert a != b
    assert await _event_count(session) == before + 2


async def test_name_twin_fork_null_or_midnight_incoming_not_tier2_matched(session) -> None:  # type: ignore[no-untyped-def]
    """A NULL or date-only-midnight incoming kickoff is an unsafe merge key -> the
    Tier-2 pair resolver is never attempted -> mints separately (same exclusion
    Tier-1 applies)."""
    ids1 = await _ids(session, home="Res Alpha", away="Res Beta")
    ids2 = await _ids(session, home="Res Alpha FC", away="Res Beta")
    canon = await _mint(session, ids1, "oddsportal:140", starts_at=KICKOFF)
    midnight = await _mint(
        session, ids2, "oddschecker:141", starts_at=datetime(2026, 6, 10, 0, 0, tzinfo=UTC)
    )
    nul = await _mint(session, ids2, "oddschecker:142", starts_at=None)
    assert midnight != canon, "midnight sentinel must not Tier-2-match"
    assert nul != canon, "NULL kickoff must not Tier-2-match"


async def test_tier2_excludes_midnight_candidate(session) -> None:  # type: ignore[no-untyped-def]
    """A stored canonical carrying the date-only-midnight sentinel is a
    placeholder, so a real-kickoff incoming twin must NOT fold onto it (candidate
    side excluded) -> mints separately."""
    ids1 = await _ids(session, home="Res Alpha", away="Res Beta")
    ids2 = await _ids(session, home="Res Alpha FC", away="Res Beta")
    midnight_canon = await _mint(
        session, ids1, "oddsportal:150", starts_at=datetime(2026, 6, 10, 0, 0, tzinfo=UTC)
    )
    real = await _mint(session, ids2, "oddschecker:151", starts_at=KICKOFF)
    assert real != midnight_canon


async def test_tennis_fork_beyond_2h_folds_via_wider_tolerance(session) -> None:  # type: ignore[no-untyped-def]
    """Tennis uses the wider 6h dedup tolerance (a 1v1 pair meets once/day), so a
    fork whose start drifted ~2h47m still folds via Tier-2 — where a team sport at
    the same gap would mint separately (see beyond_tolerance test)."""
    ids1 = await _ids(session, home="Jiri Lehecka", away="Alexander Zverev", sport="tennis")
    ids2 = await _ids(session, home="Jiri Lehecka FC", away="Alexander Zverev", sport="tennis")
    before = await _event_count(session)
    a = await _mint(session, ids1, "oddsportal:160", starts_at=KICKOFF)
    b = await _mint(
        session, ids2, "oddschecker:161", starts_at=KICKOFF + timedelta(hours=2, minutes=47)
    )
    assert a == b, "tennis wider tolerance must fold the drifted fork"
    assert await _event_count(session) == before + 1


# --- PR2b redirect-link fold-shell guard ------------------------------------
# PR2b could not delete a fold event still referenced by an actively-live
# fixture, so it leaves an active REDIRECT event_source_link (fold's own ref ->
# keep). Without the Stage-0b consult below, the own-row external_ref fast-path
# re-selects the lingering fold shell and re-mints duplicate picks on it (live
# example: fold 11686 -> keep 11685 kept re-minting dup spread/total picks). The
# consult must resolve the incoming fold ref to the keep event BEFORE the
# fast-path, sport-fenced and exact-ref only.


async def _insert_event(  # type: ignore[no-untyped-def]
    session, ids, external_ref: str, starts_at=KICKOFF
) -> int:
    """Insert a standalone event row directly (a pre-existing fold shell) —
    bypassing the resolver so it is a genuine separate row, not an auto-merge."""
    sport_id, league_id, home_id, away_id = ids
    ev = Event(
        sport_id=sport_id,
        league_id=league_id,
        home_team_id=home_id,
        away_team_id=away_id,
        external_ref=external_ref,
        starts_at=starts_at,
    )
    session.add(ev)
    await session.flush()
    return ev.id


async def _redirect_link(  # type: ignore[no-untyped-def]
    session, keep_id: int, fold_ref: str, source: str = "oddschecker"
) -> None:
    """Write an active redirect link (fold's own ref -> keep), the marker PR2b
    Phase 4 leaves for a live fold it could not delete."""
    session.add(
        EventSourceLink(
            canonical_event_id=keep_id,
            source=source,
            source_event_id=fold_ref,
            confidence_score=Decimal("1.0"),
            match_method="pr2b_redirect",
            matched_at=datetime.now(UTC),
            active=True,
        )
    )
    await session.flush()


async def test_redirect_link_resolves_to_keep_not_fold(session) -> None:  # type: ignore[no-untyped-def]
    """(a) An active redirect link for a lingering fold ref resolves to the KEEP
    event, never the fold shell — and mints nothing new, so no duplicate pick is
    re-minted on the fold."""
    ids = await _ids(session)
    keep = await _mint(session, ids, "oddsportal:keepA", starts_at=KICKOFF)
    # A genuine separate fold shell row (beyond tolerance so it did NOT auto-merge
    # — a pre-resolver duplicate PR2b later mapped fold -> keep).
    fold_ref = "oddschecker:foldA"
    fold_id = await _insert_event(session, ids, fold_ref, starts_at=KICKOFF + timedelta(hours=3))
    assert fold_id != keep
    fold_start_before = (await session.get(Event, fold_id)).starts_at
    await _redirect_link(session, keep, fold_ref)

    before = await _event_count(session)
    got = await _mint(session, ids, fold_ref, starts_at=KICKOFF + timedelta(hours=3))
    assert got == keep, "redirect must resolve to the keep event"
    assert got != fold_id, "the fold shell must never be re-selected"
    assert await _event_count(session) == before, "no new event minted"
    # the fold shell is untouched (the resolver upgrades the keep, not the fold)
    assert (await session.get(Event, fold_id)).starts_at == fold_start_before


async def test_no_redirect_normal_ref_uses_fast_path(session) -> None:  # type: ignore[no-untyped-def]
    """(b) Regression guard: with NO redirect link, an existing own-row ref still
    resolves via the external_ref fast-path to its OWN event (the fold-shell
    consult must not disturb normal ingestion)."""
    ids = await _ids(session)
    a = await _mint(session, ids, "oddsportal:normalB", starts_at=KICKOFF)
    before = await _event_count(session)
    again = await _mint(session, ids, "oddsportal:normalB", starts_at=KICKOFF)
    assert again == a
    assert await _event_count(session) == before, "fast-path returns own row, mints nothing"
    n_links = await session.scalar(
        select(func.count())
        .select_from(EventSourceLink)
        .where(EventSourceLink.source_event_id == "oddsportal:normalB")
    )
    assert n_links == 0, "the fast-path writes no source link"


async def test_redirect_cross_sport_ignored(session) -> None:  # type: ignore[no-untyped-def]
    """(c) A redirect whose target is a DIFFERENT sport is ignored (never resolve
    across sport): the soccer fold ref falls through to its own-row fast-path (the
    fold shell), never the basketball keep."""
    soccer_ids = await _ids(session, home="Res Alpha", away="Res Beta", sport="soccer")
    bball_ids = await _ids(session, home="Res Alpha", away="Res Beta", sport="basketball")
    keep_bball = await _mint(session, bball_ids, "oddsportal:xsportKeep", starts_at=KICKOFF)
    fold_ref = "oddschecker:xsportFold"
    fold_id = await _insert_event(session, soccer_ids, fold_ref, starts_at=KICKOFF)
    # a (wrong) redirect pointing the soccer fold ref at the basketball keep
    await _redirect_link(session, keep_bball, fold_ref)

    got = await _mint(session, soccer_ids, fold_ref, starts_at=KICKOFF)
    assert got != keep_bball, "must never resolve across sport"
    assert got == fold_id, "cross-sport redirect ignored -> own-row fast-path"
