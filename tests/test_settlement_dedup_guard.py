"""Settlement-side duplicate-settle guard (real-money double-count fix).

Cross-source event dedup mints SEPARATE ``events`` rows for one real fixture
(keyed on ``external_ref`` only), so the SAME bet becomes two ``picks`` rows on
two event rows. ``uq_result_tracking_pick`` is per ``pick_id``, so BOTH settle
and BOTH count into pnl/ROI/CLV — a phantom double-counted bet. The guard
refuses to write a second ``result_tracking`` row when an equivalent pick (same
market+selection+model) on a DIFFERENT event of the SAME real fixture is already
settled. Fail-safe: it can only ever SKIP a true duplicate, never suppress a
legitimate distinct pick (the ±2h same-teams bound makes a distinct-fixture
false match physically impossible — two teams cannot meet twice within 2h).

Rollback-isolated against the compose Postgres; skips when the DB is absent.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.settlement.engine import settle_open_picks
from app.settlement.results import FinalScore, ScoreBook
from app.storage.models import Event, Pick, ResultTracking, Team
from app.storage.repositories import persist_pick

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
KICKOFF = NOW - timedelta(hours=6)
HOME = "Dedup Alpha"
AWAY = "Dedup Beta"


def make_pick(
    event_id: str,
    event_label: str,
    market: Market = Market.TOTALS,
    selection: str = "Over 2.5",
    sport: str = "soccer",
) -> PickOut:
    return PickOut(
        pick_id="p-dedup",
        sport=sport,
        league="test-league-dedup",
        event=event_label,
        event_id=event_id,
        market=market,
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
        reason_summary="dedup-guard test",
        tier="premium",
        created_at=NOW - timedelta(hours=8),
    )


def book_with_score(hs: int = 2, as_: int = 1) -> ScoreBook:
    # Two scored fixtures so distinct-fixture tests can settle both.
    return ScoreBook(
        [
            FinalScore(HOME, AWAY, KICKOFF.date(), hs, as_),
            FinalScore("Other Home", "Other Away", KICKOFF.date(), hs, as_),
        ]
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


async def seed_pick(  # type: ignore[no-untyped-def]
    session,
    event_id: str,
    *,
    home: str = HOME,
    away: str = AWAY,
    starts_at: datetime = KICKOFF,
    market: Market = Market.TOTALS,
    selection: str = "Over 2.5",
    sport: str = "soccer",
) -> Pick:
    teams = EventTeams(home=home, away=away, league="test-league-dedup", starts_at=starts_at)
    ok = await persist_pick(
        session,
        make_pick(event_id, f"{home} vs {away}", market=market, selection=selection, sport=sport),
        teams,
        "value",
        "test-v",
    )
    assert ok
    pick = await session.scalar(
        select(Pick)
        .join(Event, Pick.event_id == Event.id)
        .where(Event.external_ref == event_id, Pick.selection == selection)
    )
    assert pick is not None
    return pick


async def _result_rows(session, *pick_ids):  # type: ignore[no-untyped-def]
    return (
        (await session.execute(select(ResultTracking).where(ResultTracking.pick_id.in_(pick_ids))))
        .scalars()
        .all()
    )


async def _insert_dup_pick(  # type: ignore[no-untyped-def]
    session, canonical, ref: str, *, selection=None, starts_at=None, away_name=None
) -> Pick:
    """Directly insert a duplicate Event (bypassing the mint-time resolver, which
    now correctly merges same-fixture events) plus an alerted Pick on it,
    mirroring ``canonical``. Reproduces the legacy / cross-source / forked-team
    dup the settlement guard must still catch after the resolver ships."""
    ev = await session.get(Event, canonical.event_id)
    away_id = ev.away_team_id
    if away_name is not None:
        t = Team(
            sport_id=ev.sport_id,
            league_id=ev.league_id,
            name=away_name,
            normalized_name=away_name.lower(),
        )
        session.add(t)
        await session.flush()
        away_id = t.id
    dup_event = Event(
        sport_id=ev.sport_id,
        league_id=ev.league_id,
        home_team_id=ev.home_team_id,
        away_team_id=away_id,
        external_ref=ref,
        starts_at=starts_at or ev.starts_at,
    )
    session.add(dup_event)
    await session.flush()
    pick = Pick(
        event_id=dup_event.id,
        model_version_id=canonical.model_version_id,
        market=canonical.market,
        selection=selection or canonical.selection,
        bookmaker=canonical.bookmaker,
        decimal_odds=canonical.decimal_odds,
        model_probability=canonical.model_probability,
        fair_probability=canonical.fair_probability,
        edge=canonical.edge,
        ev=canonical.ev,
        confidence=canonical.confidence,
        recommended_stake_fraction=canonical.recommended_stake_fraction,
        recommended_stake_amount=canonical.recommended_stake_amount,
        reason_summary="dedup-guard test",
        status="alerted",
        tier="premium",
    )
    session.add(pick)
    await session.flush()
    return pick


async def test_duplicate_pick_on_sibling_event_settles_only_once(session) -> None:  # type: ignore[no-untyped-def]
    """Two picks, same market+selection+model, on two event rows of ONE fixture
    (the cross-source dup) — only ONE settles; no second result_tracking row."""
    p1 = await seed_pick(session, "evt-dup-A")
    p2 = await _insert_dup_pick(session, p1, "evt-dup-B")
    assert p1.id != p2.id
    assert p1.event_id != p2.event_id  # genuinely two event rows for one fixture

    n = await settle_open_picks(session, book_with_score(2, 1), NOW)

    assert n == 1, "exactly one of the duplicate pair must settle"
    rows = await _result_rows(session, p1.id, p2.id)
    assert len(rows) == 1, "the sibling duplicate must NOT write a second result row"
    await session.refresh(p1)
    await session.refresh(p2)
    # The skipped duplicate is closed as 'superseded' (terminal), NOT left
    # 'alerted' — else it lingers on the dashboard as a pending pick asking for a
    # manual result even though its twin already settled.
    assert {p1.status, p2.status} == {"settled", "superseded"}


async def test_duplicate_skipped_across_settlement_cycles(session) -> None:  # type: ignore[no-untyped-def]
    """The already-settled sibling can come from a PRIOR cycle: settle p1, then a
    duplicate p2 alerts — p2 must be skipped, not double-settled."""
    p1 = await seed_pick(session, "evt-xc-A")
    assert await settle_open_picks(session, book_with_score(2, 1), NOW) == 1

    p2 = await _insert_dup_pick(session, p1, "evt-xc-B")
    assert await settle_open_picks(session, book_with_score(2, 1), NOW) == 0, (
        "duplicate must be skipped"
    )
    assert len(await _result_rows(session, p1.id, p2.id)) == 1


async def test_in_running_fork_duplicate_is_deduped(session) -> None:  # type: ignore[no-untyped-def]
    """A ``[In Running]`` live-fork (a pre-existing forked team row with a
    DIFFERENT team_id) still folds onto its clean twin, so its duplicate pick is
    skipped. Insert the forked team+event directly (the team-upsert now strips the
    marker, so persist_pick can no longer create the fork — but legacy fork rows
    remain in production)."""
    from app.storage.models import Event, Team

    clean = await seed_pick(session, "evt-fork-clean")  # HOME vs AWAY
    # Forked away-team entity carrying the live-status marker (distinct id).
    forked_away = Team(
        sport_id=(await session.scalar(select(Event.sport_id).where(Event.id == clean.event_id))),
        league_id=(await session.scalar(select(Event.league_id).where(Event.id == clean.event_id))),
        name=f"{AWAY} [In Running]",
        normalized_name=f"{AWAY.lower()} [in running]",
    )
    session.add(forked_away)
    await session.flush()
    home_id = await session.scalar(select(Event.home_team_id).where(Event.id == clean.event_id))
    fork_event = Event(
        sport_id=forked_away.sport_id,
        league_id=forked_away.league_id,
        home_team_id=home_id,
        away_team_id=forked_away.id,
        external_ref="evt-fork-live",
        starts_at=KICKOFF,
    )
    session.add(fork_event)
    await session.flush()
    fork_pick = Pick(
        event_id=fork_event.id,
        model_version_id=clean.model_version_id,
        market=clean.market,
        selection=clean.selection,
        bookmaker=clean.bookmaker,
        decimal_odds=clean.decimal_odds,
        model_probability=clean.model_probability,
        fair_probability=clean.fair_probability,
        edge=clean.edge,
        ev=clean.ev,
        confidence=clean.confidence,
        recommended_stake_fraction=clean.recommended_stake_fraction,
        recommended_stake_amount=clean.recommended_stake_amount,
        reason_summary="dedup-guard test",
        status="alerted",
        tier="premium",
    )
    session.add(fork_pick)
    await session.flush()

    n = await settle_open_picks(session, book_with_score(2, 1), NOW)
    assert n == 1, "clean twin settles once; the live-fork duplicate is skipped"
    assert len(await _result_rows(session, clean.id, fork_pick.id)) == 1


async def test_duplicate_superseded_even_without_own_score(session) -> None:  # type: ignore[no-untyped-def]
    """A duplicate whose OWN event never gets a matched score (e.g. an
    `[In Running]`-named fork the score book cannot match) must STILL be
    superseded once its twin has settled — otherwise it lingers 'alerted' /
    pending forever. Requires the guard to run BEFORE the score lookup."""
    clean = await seed_pick(session, "evt-noscore-A")
    await _mark_settled(session, clean)  # the settled sibling
    dup = await _insert_dup_pick(session, clean, "evt-noscore-B")  # same fixture, still alerted

    # Non-empty book that does NOT contain this fixture -> dup's own lookup is None.
    book = ScoreBook([FinalScore("Zzz Home", "Zzz Away", KICKOFF.date(), 1, 0)])
    n = await settle_open_picks(session, book, NOW)

    assert n == 0  # clean already settled; dup has no own score to settle from
    await session.refresh(dup)
    assert dup.status == "superseded", "duplicate must supersede without its own score"


async def test_distinct_fixtures_both_settle(session) -> None:  # type: ignore[no-untyped-def]
    """Fail-safe: two DIFFERENT fixtures (different teams), same market+selection,
    both settle — the guard must not over-skip."""
    p1 = await seed_pick(session, "evt-distinct-A")
    p2 = await seed_pick(session, "evt-distinct-B", home="Other Home", away="Other Away")
    n = await settle_open_picks(session, book_with_score(2, 1), NOW)
    assert n == 2
    assert len(await _result_rows(session, p1.id, p2.id)) == 2


async def test_same_teams_beyond_tolerance_both_settle(session) -> None:  # type: ignore[no-untyped-def]
    """Fail-safe: same teams but kickoffs >2h apart (a leg reversal / rematch /
    doubleheader) are DISTINCT fixtures — both settle. Proves the ±2h bound."""
    p1 = await seed_pick(session, "evt-tol-A", starts_at=KICKOFF)
    p2 = await seed_pick(session, "evt-tol-B", starts_at=KICKOFF - timedelta(hours=3))
    n = await settle_open_picks(session, book_with_score(2, 1), NOW)
    assert n == 2, "same teams >2h apart are distinct fixtures — no dedup"
    assert len(await _result_rows(session, p1.id, p2.id)) == 2


async def test_different_selection_not_deduped(session) -> None:  # type: ignore[no-untyped-def]
    """Fail-safe: two picks on the same fixture with DIFFERENT selections are
    distinct bets — both settle (the guard keys on selection)."""
    p1 = await seed_pick(session, "evt-sel-A", selection="Over 2.5")
    p2 = await _insert_dup_pick(session, p1, "evt-sel-B", selection="Under 2.5")
    n = await settle_open_picks(session, book_with_score(2, 1), NOW)
    assert n == 2
    assert len(await _result_rows(session, p1.id, p2.id)) == 2


def test_fixture_pair_key_folds_live_status_fork() -> None:
    from app.resolution.matching import fixture_pair_key

    assert fixture_pair_key("England", "Mexico") == fixture_pair_key(
        "England [In Running]", "Mexico"
    )


def test_fixture_pair_key_is_unordered() -> None:
    from app.resolution.matching import fixture_pair_key

    assert fixture_pair_key("England", "Mexico") == fixture_pair_key("Mexico", "England")


def test_fixture_pair_key_preserves_women_marker() -> None:
    from app.resolution.matching import fixture_pair_key

    assert fixture_pair_key("Arsenal Women", "Chelsea") != fixture_pair_key("Arsenal", "Chelsea")


def test_fixture_pair_key_degenerate_returns_none() -> None:
    from app.resolution.matching import fixture_pair_key

    assert fixture_pair_key("", "Mexico") is None
    assert fixture_pair_key("Mexico", "Mexico") is None


async def _mark_settled(session, pick, outcome: str = "won") -> None:  # type: ignore[no-untyped-def]
    from app.storage.models import ResultTracking as RT

    session.add(
        RT(pick_id=pick.id, outcome=outcome, pnl=Decimal("0"), roi=Decimal("0"), settled_at=NOW)
    )
    pick.status = "settled"
    await session.flush()


async def test_tennis_fork_beyond_2h_is_deduped_by_wider_window(session) -> None:  # type: ignore[no-untyped-def]
    """A tennis pair meets once per day, so an in-running fork whose start drifted
    ~2h47m (the live Lehecka/Zverev case) must STILL be recognised as the same
    fixture — the tight ±2h team-sport bound would miss it and double-settle."""
    from app.resolution.matching import fixture_pair_key
    from app.settlement.engine import _settled_sibling_exists

    settled = await seed_pick(
        session, "evt-tn-A", sport="tennis", home="Jiri Lehecka", away="Alexander Zverev"
    )
    await _mark_settled(session, settled)
    sport_id = await session.scalar(select(Event.sport_id).where(Event.id == settled.event_id))
    target = fixture_pair_key("Jiri Lehecka", "Alexander Zverev [In Running]")
    assert target is not None

    found = await _settled_sibling_exists(
        session,
        pick_id=-1,
        event_id=-1,
        sport_id=sport_id,
        starts_at=KICKOFF + timedelta(hours=2, minutes=47),
        market=settled.market,
        selection=settled.selection,
        model_version_id=settled.model_version_id,
        target_pair=target,
        sport_key="tennis",
    )
    assert found is True


async def test_team_sport_fork_beyond_2h_is_not_deduped(session) -> None:  # type: ignore[no-untyped-def]
    """A team sport keeps the tight ±2h bound (same-day doubleheaders/legs exist),
    so a 2h47m gap is treated as a distinct fixture — no dedup."""
    from app.resolution.matching import fixture_pair_key
    from app.settlement.engine import _settled_sibling_exists

    settled = await seed_pick(session, "evt-sc-A", sport="soccer", home="Alpha FC", away="Beta FC")
    await _mark_settled(session, settled)
    sport_id = await session.scalar(select(Event.sport_id).where(Event.id == settled.event_id))
    target = fixture_pair_key("Alpha FC", "Beta FC")
    assert target is not None

    found = await _settled_sibling_exists(
        session,
        pick_id=-1,
        event_id=-1,
        sport_id=sport_id,
        starts_at=KICKOFF + timedelta(hours=2, minutes=47),
        market=settled.market,
        selection=settled.selection,
        model_version_id=settled.model_version_id,
        target_pair=target,
        sport_key="soccer",
    )
    assert found is False


def test_dedup_tolerance_is_sport_aware() -> None:
    from app.settlement.engine import _dedup_tolerance

    assert _dedup_tolerance("tennis") == timedelta(hours=6)
    assert _dedup_tolerance("soccer") == timedelta(hours=2)
    assert _dedup_tolerance("basketball") == timedelta(hours=2)
    assert _dedup_tolerance(None) == timedelta(hours=2)
