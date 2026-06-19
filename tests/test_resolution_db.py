"""DB integration for the cross-source resolver (compose Postgres; skip absent).

resolve_pinnacle_close_snaps strict-matches a pick's fixture to its
`pinnacle_<sport>` archive event and returns that event's close re-keyed to the
pick's event_id + selection vocabulary. These tests prove the happy path
(alias + re-key), and the cardinal-sin guards at the DB layer (no match -> [],
ambiguous -> [], out-of-window -> []). No live network.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.resolution.shadow import summarize_match_rate
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.repositories import (
    persist_odds_snapshots,
    persist_pick,
    resolve_pinnacle_close_snaps,
    shadow_match_rate_outcomes,
)

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai"
KO = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
CAPTURED = KO - timedelta(hours=2)


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


def _pin_snap(selection: str, odds: float, event: str) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id=event,
        bookmaker="Pinnacle",
        market=Market.H2H,
        selection=selection,
        decimal_odds=odds,
        captured_at=CAPTURED,
        ingested_at=CAPTURED,
    )


async def _seed_pinnacle_event(factory, ref: str, home: str, away: str) -> None:  # type: ignore[no-untyped-def]
    snaps = [_pin_snap(home, 2.10, ref), _pin_snap("Draw", 3.40, ref), _pin_snap(away, 3.60, ref)]
    teams = {ref: EventTeams(home=home, away=away, league="pin", starts_at=KO)}
    await persist_odds_snapshots(factory, snaps, teams, "pinnacle_soccer", "pinnacle_soccer")


async def test_resolver_matches_via_alias_and_rekeys_selections(factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_pinnacle_event(factory, "pin-mu-che", "Manchester United", "Chelsea")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Man Utd",  # OddsPortal-style abbreviation -> alias -> Manchester United
            away="Chelsea",
            kickoff=KO,
        )
    by_sel = {s.selection: s for s in out}
    # re-keyed to the PICK's selection vocabulary (home collapses to "Man Utd")
    assert set(by_sel) == {"Man Utd", "Draw", "Chelsea"}
    assert all(s.event_id == "evt-pick" for s in out)
    assert all(s.bookmaker == "Pinnacle" for s in out)
    assert by_sel["Man Utd"].decimal_odds == pytest.approx(2.10)
    assert by_sel["Draw"].decimal_odds == pytest.approx(3.40)


async def test_resolver_no_match_returns_empty(factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_pinnacle_event(factory, "pin-alpha-beta", "Alpha", "Beta")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Gamma",
            away="Delta",
            kickoff=KO,
        )
    assert out == []


async def test_resolver_duplicate_archive_matches_one(factory) -> None:  # type: ignore[no-untyped-def]
    # Two archive events for the SAME fixture (same teams + kickoff) are
    # DUPLICATE captures of ONE game, not two distinct fixtures (a team plays
    # once per day). The resolver now matches one deterministically and attaches
    # its Pinnacle close instead of rejecting — the old reject lost otherwise-
    # matchable fixtures whenever the archive held the same game twice.
    await _seed_pinnacle_event(factory, "pin-dup-1", "Alpha", "Beta")
    await _seed_pinnacle_event(factory, "pin-dup-2", "Alpha", "Beta")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Alpha",
            away="Beta",
            kickoff=KO,
        )
    assert out  # a close IS attached now (duplicates collapse to one fixture)
    assert all(s.bookmaker == "Pinnacle" for s in out)


async def test_resolver_kickoff_outside_window_returns_empty(factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_pinnacle_event(factory, "pin-far", "Alpha", "Beta")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Alpha",
            away="Beta",
            kickoff=KO + timedelta(days=4),  # archive event is days away
            max_day_drift=1,
        )
    assert out == []


def _pin_snap_at(selection: str, odds: float, event: str, captured: datetime) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id=event,
        bookmaker="Pinnacle",
        market=Market.H2H,
        selection=selection,
        decimal_odds=odds,
        captured_at=captured,
        ingested_at=captured,
    )


async def test_resolver_caps_cutoff_at_arcadia_kickoff(factory) -> None:  # type: ignore[no-untyped-def]
    # The arcadia event kicks off a DAY before the pick (within the match
    # window). A Pinnacle row captured AFTER the arcadia kickoff (in-play) must
    # NOT become the close — the cutoff is capped at the arcadia kickoff.
    arc_ko = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
    pick_ko = arc_ko + timedelta(days=1)
    pre = arc_ko - timedelta(hours=2)  # valid pre-kickoff close
    inplay = arc_ko + timedelta(hours=1)  # in-play; must be excluded
    ref = "pin-cutoff"
    snaps = [
        _pin_snap_at("Alpha", 2.10, ref, pre),
        _pin_snap_at("Draw", 3.40, ref, pre),
        _pin_snap_at("Beta", 3.60, ref, pre),
        _pin_snap_at("Alpha", 1.50, ref, inplay),  # later in-play home price
    ]
    teams = {ref: EventTeams(home="Alpha", away="Beta", league="pin", starts_at=arc_ko)}
    await persist_odds_snapshots(factory, snaps, teams, "pinnacle_soccer", "pinnacle_soccer")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Alpha",
            away="Beta",
            kickoff=pick_ko,
            max_day_drift=1,
        )
    by_sel = {s.selection: s for s in out}
    # the Alpha close must be the PRE-kickoff 2.10, NOT the in-play 1.50
    assert by_sel["Alpha"].decimal_odds == pytest.approx(2.10)


# A league key unique to this test so the shadow runner's outcomes can be
# isolated from any committed warehouse picks (the DB is real; only THIS
# transaction's writes roll back).
_SHADOW_LEAGUE = "shadow-test-epl"


def _shadow_pick(event_id: str, selection: str = "Home") -> PickOut:
    """Minimal soccer PickOut for shadow-match-rate seeding. Matching is
    event-level, so market/selection are irrelevant to the matcher."""
    return PickOut(
        pick_id="p-shadow",
        sport="soccer",
        league=_SHADOW_LEAGUE,
        event=f"{event_id} fixture",
        event_id=event_id,
        market=Market.H2H,
        selection=selection,
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
        reason_summary="shadow match-rate test",
        tier="premium",
        created_at=KO - timedelta(days=180),
    )


async def test_shadow_match_rate_classifies_match_alias_gap_and_coverage_gap(factory) -> None:  # type: ignore[no-untyped-def]
    """Shadow runner over three picks against one archived fixture:
    A strict-matches (alias), B has the archive in-window but different teams
    (alias/ambiguity gap), C kicks off far from any archive event (coverage
    gap). Nothing is written; only match outcomes are reported."""
    await _seed_pinnacle_event(factory, "pin-mu-che", "Manchester United", "Chelsea")
    async with factory() as session:
        # A: "Man Utd" aliases to "Manchester United", same kickoff -> matched
        await persist_pick(
            session,
            _shadow_pick("evt-shadow-A"),
            EventTeams(home="Man Utd", away="Chelsea", league=_SHADOW_LEAGUE, starts_at=KO),
            "value-sharp-vs-soft",
            "v3",
        )
        # B: archive event is in-window but teams differ -> unmatched_with_candidates
        await persist_pick(
            session,
            _shadow_pick("evt-shadow-B"),
            EventTeams(home="Gamma", away="Delta", league=_SHADOW_LEAGUE, starts_at=KO),
            "value-sharp-vs-soft",
            "v3",
        )
        # C: kickoff 30 days from any archive event -> no_archive_candidates
        await persist_pick(
            session,
            _shadow_pick("evt-shadow-C"),
            EventTeams(
                home="Alpha",
                away="Beta",
                league=_SHADOW_LEAGUE,
                starts_at=KO + timedelta(days=30),
            ),
            "value-sharp-vs-soft",
            "v3",
        )
        await session.commit()
        outcomes = await shadow_match_rate_outcomes(session)

    mine = [o for o in outcomes if o.league == _SHADOW_LEAGUE]
    report = summarize_match_rate(mine)
    assert report.total == 3
    assert report.matched == 1  # A
    assert report.unmatched_with_candidates == 1  # B: archive present, no name match
    assert report.no_archive_candidates == 1  # C: no archive event in window
    by_sport = {g.key: g for g in report.by_sport}
    assert by_sport["soccer"].total == 3
    assert by_sport["soccer"].matched == 1


async def test_shadow_match_rate_since_filters_old_kickoffs(factory) -> None:  # type: ignore[no-untyped-def]
    """`since` scopes the population by kickoff: a pick before the cutoff is
    excluded entirely from the outcomes."""
    async with factory() as session:
        await persist_pick(
            session,
            _shadow_pick("evt-shadow-old"),
            EventTeams(home="Old", away="Timer", league=_SHADOW_LEAGUE, starts_at=KO),
            "value-sharp-vs-soft",
            "v3",
        )
        await session.commit()
        outcomes = await shadow_match_rate_outcomes(session, since=KO + timedelta(days=1))

    assert all(o.league != _SHADOW_LEAGUE for o in outcomes)
