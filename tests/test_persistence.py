"""Pick persistence against a real Postgres (compose). Skips if DB is absent.

Uses a savepoint-style rollback: each test runs in a transaction that is
rolled back, so the warehouse is never mutated by the suite.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Event, ModelVersion, OddsSnapshot, Pick, Sport
from app.storage.repositories import (
    latest_available_games_with_events,
    latest_picks_with_events,
    persist_pick,
    refresh_event_kickoffs,
)

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"


def make_pick(
    event_id: str = "evt-persist-test",
    tier: str = "premium",
    decimal_odds: float = 2.10,
    edge: float = 0.05,
    league: str = "test-league-persist",
) -> PickOut:
    return PickOut(
        pick_id="p-1",
        sport="soccer",
        league=league,
        event="Alpha FC vs Beta United",
        event_id=event_id,
        market=Market.H2H,
        selection="Alpha FC",
        bookmaker="testbook",
        decimal_odds=decimal_odds,
        model_probability=0.55,
        fair_probability=0.50,
        edge=edge,
        ev=0.155,
        confidence=0.70,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary="persistence test",
        tier=tier,
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


@pytest.fixture
async def committing_session():  # type: ignore[no-untyped-def]
    """Savepoint-isolated session that CONTAINS commits — for code under test
    that calls session.commit() (e.g. record_result). The outer transaction is
    rolled back at teardown, so a committing handler never leaks into the shared
    dev DB (unlike the plain `session` fixture, which only rolls back UNcommitted
    work)."""
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
        async with maker() as s:
            try:
                yield s
            finally:
                await trans.rollback()
    await engine.dispose()


async def test_persist_pick_inserts_then_dedupes(session) -> None:  # type: ignore[no-untyped-def]
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")

    inserted = await persist_pick(session, make_pick(), teams, "dixon-coles", "test-1")
    assert inserted == "inserted"

    count = await session.scalar(
        select(func.count()).select_from(Pick).where(Pick.bookmaker == "testbook")
    )
    assert count == 1

    # Same natural key -> deduped (no second row)
    again = await persist_pick(session, make_pick(), teams, "dixon-coles", "test-1")
    assert again == "duplicate"
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


async def test_shared_strategy_gets_per_sport_model_version_rows(session) -> None:  # type: ignore[no-untyped-def]
    # The value strategy reuses ONE name/version ("value-sharp-vs-soft"/"v3")
    # for soccer AND basketball. Keying the model_versions lookup on
    # (name, version) alone gave the basketball pick the soccer row's
    # sport_id (one shared, mis-tagged row); the (sport_id, name, version)
    # key gives each sport its own correctly-tagged row.
    #
    # A test-unique strategy name isolates the assertion from the dev
    # warehouse, which already holds committed value-sharp-vs-soft rows from
    # live runs (and would otherwise leak into the count).
    strategy = "test-shared-strategy-mv"
    soccer_teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    bball_teams = EventTeams(home="Court Cats", away="Hoop Dogs", league="test-bball-persist")
    soccer = make_pick("evt-mv-soccer")
    bball = make_pick("evt-mv-bball").model_copy(
        update={"sport": "basketball", "league": "test-bball-persist"}
    )

    await persist_pick(session, soccer, soccer_teams, strategy, "v3")
    await persist_pick(session, bball, bball_teams, strategy, "v3")

    rows = (
        await session.execute(
            select(ModelVersion.id, Sport.key)
            .join(Sport, Sport.id == ModelVersion.sport_id)
            .where(ModelVersion.name == strategy, ModelVersion.version == "v3")
        )
    ).all()
    # Two distinct rows (not the soccer row reused), one per sport, each
    # tagged with the sport that actually produced it (Sport.key == pick.sport).
    assert len({mv_id for mv_id, _ in rows}) == 2
    assert {sport for _, sport in rows} == {"soccer", "basketball"}


async def test_volume_pick_persists_with_tier_and_serializes_it(session) -> None:  # type: ignore[no-untyped-def]
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    outcome = await persist_pick(
        session,
        make_pick("evt-tier-vol", tier="volume", edge=0.02),
        teams,
        "value-sharp-vs-soft",
        "tier-t1",
    )
    assert outcome == "inserted"
    row = await session.scalar(select(Pick).where(Pick.bookmaker == "testbook"))
    assert row is not None
    assert row.tier == "volume"
    assert row.status == "alerted"  # lifecycle is shared; tier scopes behavior

    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["bookmaker"] == "testbook"]
    assert ours and ours[0]["tier"] == "volume"  # API exposes the tier


async def test_min_acceptable_odds_uses_live_fair_after_reprice(session) -> None:  # type: ignore[no-untyped-def]
    # Audit 2026-06-26: "ok >= X" must reason about the SAME fair the LIVE edge uses.
    # After a re-price drops the live fair (closing_fair_probability) below the entry
    # fair (model_probability) at an unchanged price, the value floor must rise from the
    # LIVE fair — otherwise the card says "price clears the floor (ok >= X)" while
    # current_edge says "no value now" at the same odds.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(
        session, make_pick("evt-livefair", decimal_odds=2.00), teams, "value-sharp-vs-soft", "lf-1"
    )
    row = await session.scalar(select(Pick).where(Pick.bookmaker == "testbook"))
    assert row is not None
    # live re-price: fair dropped 0.55 -> 0.45 at the same 2.00 price => value gone
    row.closing_fair_probability = Decimal("0.450000")
    row.current_odds = Decimal("2.0000")
    row.current_edge = Decimal("-0.050000")
    row.current_bookmaker = "testbook"
    await session.flush()

    payload = await latest_picks_with_events(session, limit=200, min_edge=0.03)
    ours = [p for p in payload if p["bookmaker"] == "testbook"][0]
    # floor from the LIVE fair 0.45: 1/(0.45-0.03)=2.381 -> ceil 2.39 (NOT entry-fair 1.93)
    assert ours["min_acceptable_odds"] == "2.39"


async def test_min_acceptable_odds_uses_volume_floor_for_volume_tier(session) -> None:  # type: ignore[no-untyped-def]
    # Audit #2: a volume-tier pick is minted at value_volume_min_edge (0.015), so its
    # "ok >= X" floor must use 0.015 — not the premium value_min_edge (0.03), which would
    # show a floor stricter than the one the pick was actually held to.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(
        session,
        make_pick("evt-volfloor", tier="volume", decimal_odds=2.00, edge=0.02),
        teams,
        "value-sharp-vs-soft",
        "vf-1",
    )
    payload = await latest_picks_with_events(
        session, limit=200, min_edge=0.03, volume_min_edge=0.015
    )
    ours = [p for p in payload if p["bookmaker"] == "testbook"][0]
    # volume floor from entry fair 0.55 at 0.015: 1/(0.55-0.015)=1.869 -> ceil 1.87 (NOT 1.93)
    assert ours["min_acceptable_odds"] == "1.87"


async def test_premium_key_is_shielded_from_volume_redetection(session) -> None:  # type: ignore[no-untyped-def]
    # The unique key (event, market, selection, model) collides across tiers
    # BY DESIGN; tier may only ratchet upward. A premium row must never be
    # downgraded or touched by a later volume candidate.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    assert (
        await persist_pick(
            session, make_pick("evt-tier-shield"), teams, "value-sharp-vs-soft", "tier-t2"
        )
        == "inserted"
    )
    outcome = await persist_pick(
        session,
        make_pick("evt-tier-shield", tier="volume", decimal_odds=2.40, edge=0.02),
        teams,
        "value-sharp-vs-soft",
        "tier-t2",
    )
    assert outcome == "duplicate"
    row = await session.scalar(select(Pick).where(Pick.bookmaker == "testbook"))
    assert row is not None
    assert row.tier == "premium"  # untouched
    assert row.decimal_odds == Decimal("2.1000")  # original market numbers kept


async def test_volume_to_premium_upgrade_promotes_row_in_place(session) -> None:  # type: ignore[no-untyped-def]
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    assert (
        await persist_pick(
            session,
            make_pick("evt-tier-upgrade", tier="volume", decimal_odds=2.10, edge=0.02),
            teams,
            "value-sharp-vs-soft",
            "tier-t3",
        )
        == "inserted"
    )
    before = await session.scalar(select(Pick).where(Pick.bookmaker == "testbook"))
    assert before is not None
    created_before = before.created_at

    outcome = await persist_pick(
        session,
        make_pick("evt-tier-upgrade", tier="premium", decimal_odds=2.30, edge=0.05),
        teams,
        "value-sharp-vs-soft",
        "tier-t3",
    )
    assert outcome == "upgraded"
    rows = (await session.execute(select(Pick).where(Pick.bookmaker == "testbook"))).scalars().all()
    assert len(rows) == 1  # promoted IN PLACE — never a second row
    row = rows[0]
    assert row.tier == "premium"
    assert row.status == "alerted"
    assert row.decimal_odds == Decimal("2.3000")  # the alert quotes the row
    assert row.edge == Decimal("0.050000")
    # created_at advances to the upgrade moment (exposure-seeding invariant)
    assert row.created_at > created_before
    # stale revalidation verdicts (priced on the old odds) are reset
    assert row.clv_log is None
    assert row.current_odds is None
    assert row.revalidated_at is None


async def test_settled_volume_row_is_not_upgraded(session) -> None:  # type: ignore[no-untyped-def]
    # Once the lifecycle moved past "alerted" the market moment is gone —
    # a late premium detection on the same key is a plain duplicate.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(
        session,
        make_pick("evt-tier-settled", tier="volume", edge=0.02),
        teams,
        "value-sharp-vs-soft",
        "tier-t4",
    )
    await session.execute(
        sa_update(Pick).where(Pick.bookmaker == "testbook").values(status="settled")
    )
    outcome = await persist_pick(
        session,
        make_pick("evt-tier-settled", tier="premium", edge=0.05),
        teams,
        "value-sharp-vs-soft",
        "tier-t4",
    )
    assert outcome == "duplicate"
    row = await session.scalar(select(Pick).where(Pick.bookmaker == "testbook"))
    assert row is not None
    assert row.tier == "volume"
    assert row.status == "settled"


async def test_volume_insert_never_supersedes_open_premium(session) -> None:  # type: ignore[no-untyped-def]
    # Cross-version supersede respects tier: a NEW volume row (new strategy
    # version) must not flip an older OPEN premium row to 'superseded' —
    # while a premium insert supersedes any older open row, volume included.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(session, make_pick("evt-tier-sup"), teams, "value-sharp-vs-soft", "v-old")
    assert (
        await persist_pick(
            session,
            make_pick("evt-tier-sup", tier="volume", edge=0.02),
            teams,
            "value-sharp-vs-soft",
            "v-new",
        )
        == "inserted"
    )
    statuses = dict(
        (
            await session.execute(
                select(Pick.tier, Pick.status).where(Pick.bookmaker == "testbook")
            )
        ).all()
    )
    assert statuses == {"premium": "alerted", "volume": "alerted"}  # premium survives

    # ...and the reverse: a premium insert under yet another version
    # supersedes BOTH older open rows (premium and volume).
    assert (
        await persist_pick(
            session, make_pick("evt-tier-sup"), teams, "value-sharp-vs-soft", "v-newest"
        )
        == "inserted"
    )
    rows = (
        await session.execute(
            select(Pick.status).where(Pick.bookmaker == "testbook").order_by(Pick.id)
        )
    ).scalars()
    assert sorted(rows) == ["alerted", "superseded", "superseded"]


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


async def test_get_or_create_event_real_time_not_clobbered_by_midnight(session) -> None:  # type: ignore[no-untyped-def]
    # A fixture first priced with a REAL kickoff, then re-detected on a cycle that
    # only knew the DATE (00:00:00 UTC), must KEEP the real time. The DB upsert is
    # the second overwrite layer (after EventDirectory) and must apply the same
    # precedence rule, else the residual-tail midnight clobbers a real time.
    real = datetime(2026, 6, 25, 2, 0, tzinfo=UTC)
    midnight = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)
    teams_real = EventTeams(home="P R Real", away="P R Date", starts_at=real)
    teams_midnight = EventTeams(home="P R Real", away="P R Date", starts_at=midnight)
    await persist_pick(session, make_pick("evt-prec-rt"), teams_real, "value-sharp-vs-soft", "pr1")
    await persist_pick(
        session, make_pick("evt-prec-rt"), teams_midnight, "value-sharp-vs-soft", "pr1"
    )
    row = await session.scalar(select(Event).where(Event.external_ref == "evt-prec-rt"))
    assert row is not None
    assert row.starts_at == real  # real time survived the later midnight upsert


async def test_get_or_create_event_real_time_upgrades_stored_midnight(session) -> None:  # type: ignore[no-untyped-def]
    # The reverse: first seen date-only (midnight), then a REAL time arrives — the
    # real time must upgrade the stored midnight.
    midnight = datetime(2026, 6, 26, 0, 0, tzinfo=UTC)
    real = datetime(2026, 6, 26, 23, 0, tzinfo=UTC)
    teams_midnight = EventTeams(home="Up A", away="Up B", starts_at=midnight)
    teams_real = EventTeams(home="Up A", away="Up B", starts_at=real)
    await persist_pick(
        session, make_pick("evt-prec-up"), teams_midnight, "value-sharp-vs-soft", "pu1"
    )
    await persist_pick(session, make_pick("evt-prec-up"), teams_real, "value-sharp-vs-soft", "pu1")
    row = await session.scalar(select(Event).where(Event.external_ref == "evt-prec-up"))
    assert row is not None
    assert row.starts_at == real  # midnight upgraded to the real time


async def test_refresh_event_kickoffs_midnight_does_not_clobber_real(session) -> None:  # type: ignore[no-untyped-def]
    # The per-cycle refresh must honour precedence too: a date-only midnight in
    # the refresh map must NOT downgrade an event that already has a real time,
    # but a real time DOES upgrade a stored midnight.
    real = datetime(2026, 6, 27, 18, 30, tzinfo=UTC)
    midnight = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    teams_real = EventTeams(home="Ref A", away="Ref B", starts_at=real)
    await persist_pick(session, make_pick("evt-prec-ref"), teams_real, "value-sharp-vs-soft", "pf1")
    # midnight refresh => NO change (real time protected)
    assert await refresh_event_kickoffs(session, {"evt-prec-ref": midnight}) == 0
    row = await session.scalar(select(Event).where(Event.external_ref == "evt-prec-ref"))
    assert row is not None
    assert row.starts_at == real

    # a stored midnight event, refreshed with a real time => upgraded
    teams_midnight = EventTeams(home="Ref C", away="Ref D", starts_at=midnight)
    await persist_pick(
        session, make_pick("evt-prec-ref2"), teams_midnight, "value-sharp-vs-soft", "pf2"
    )
    assert await refresh_event_kickoffs(session, {"evt-prec-ref2": real}) == 1
    row2 = await session.scalar(select(Event).where(Event.external_ref == "evt-prec-ref2"))
    assert row2 is not None
    assert row2.starts_at == real


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


async def test_available_games_fallback_reads_current_warehouse_events(session) -> None:  # type: ignore[no-untyped-def]
    # Restart regression: /games used to read only the in-memory latest poll
    # registry, so a process restart showed "NO GAMES LOADED" while /picks
    # still rendered from Postgres. The fallback query rebuilds the current
    # unrestricted football/NBA fixture view from events + odds_snapshots.
    now = datetime.now(tz=UTC)
    kickoff = now + timedelta(hours=3)
    teams = EventTeams(
        home="Games Fallback Home",
        away="Games Fallback Away",
        league="test-league-games",
        starts_at=kickoff,
    )
    await persist_pick(
        session,
        make_pick("evt-games-fallback", league="test-league-games"),
        teams,
        "value-sharp-vs-soft",
        "t-games",
    )
    event = await session.scalar(select(Event).where(Event.external_ref == "evt-games-fallback"))
    assert event is not None
    captured = now - timedelta(minutes=5)
    session.add_all(
        [
            OddsSnapshot(
                event_id=event.id,
                bookmaker="Pinnacle",
                market="h2h",
                selection="Games Fallback Home",
                decimal_odds=Decimal("2.1000"),
                liquidity=None,
                captured_at=captured,
                ingested_at=captured + timedelta(seconds=10),
            ),
            OddsSnapshot(
                event_id=event.id,
                bookmaker="SoftBook",
                market="totals",
                selection="Over 2.5",
                decimal_odds=Decimal("1.9500"),
                liquidity=None,
                captured_at=captured,
                ingested_at=captured + timedelta(seconds=20),
            ),
        ]
    )
    await session.flush()

    # High limit so a POPULATED shared dev DB (the running app accumulates
    # hundreds of soccer events ordered by kickoff before this now+3h fixture)
    # can't truncate our fixture out of the top-N — the assertion is about
    # PRESENCE in the unrestricted warehouse view, not top-50 ranking.
    rows = await latest_available_games_with_events(session, limit=5000, sport="soccer", now=now)
    ours = [row for row in rows if row["event_id"] == "evt-games-fallback"]
    assert ours, "warehouse fallback did not include the current fixture"
    row = ours[0]
    assert row["sport"] == "soccer"
    assert row["sport_label"] == "Football"
    assert row["event"] == "Games Fallback Home vs Games Fallback Away"
    assert row["league"] == "test-league-games"
    assert row["starts_at"] == kickoff.isoformat()
    assert row["markets"] == ["h2h", "totals"]
    assert row["bookmakers"] == ["Pinnacle", "SoftBook"]
    assert row["market_count"] == 2
    assert row["bookmaker_count"] == 2
    assert row["snapshot_count"] == 2
    assert row["first_captured_at"] == captured.isoformat()
    assert row["last_captured_at"] == captured.isoformat()
    assert row["updated_at"] == (captured + timedelta(seconds=20)).isoformat()


async def test_available_games_excludes_finished_fixtures(session) -> None:  # type: ignore[no-untyped-def]
    # GET /games must NOT show an already-FINISHED game as bettable: a fixture
    # kicked off >3h30m ago is over, even if it still carries recent odds rows
    # (the old query had no upper bound, so those leaked in). Live + upcoming stay.
    now = datetime.now(tz=UTC)
    captured = now - timedelta(minutes=10)  # recent odds (passes the OR clause)

    async def _seed(ref: str, kickoff: datetime) -> None:
        teams = EventTeams(
            home=f"{ref} H", away=f"{ref} A", league="test-league-fin", starts_at=kickoff
        )
        await persist_pick(
            session, make_pick(ref, league="test-league-fin"), teams, "value-sharp-vs-soft", "t-fin"
        )
        ev = await session.scalar(select(Event).where(Event.external_ref == ref))
        assert ev is not None
        session.add(
            OddsSnapshot(
                event_id=ev.id,
                bookmaker="Pinnacle",
                market="h2h",
                selection="X",
                decimal_odds=Decimal("2.0000"),
                liquidity=None,
                captured_at=captured,
                ingested_at=captured,
            )
        )
        await session.flush()

    await _seed("evt-fin-over", now - timedelta(hours=5))  # finished -> excluded
    await _seed("evt-fin-live", now - timedelta(hours=1))  # in-play  -> kept
    await _seed("evt-fin-soon", now + timedelta(hours=2))  # upcoming -> kept

    rows = await latest_available_games_with_events(session, limit=5000, now=now)
    refs = {r["event_id"] for r in rows}
    assert "evt-fin-over" not in refs  # finished game hidden
    assert "evt-fin-live" in refs  # live fixture still visible
    assert "evt-fin-soon" in refs  # upcoming visible


async def test_latest_picks_tier_scope_protects_premium_window(session) -> None:  # type: ignore[no-untyped-def]
    """Volume-flood regression: the volume shadow tier runs ~6x premium, so
    an unscoped latest-N window fills with volume rows and an open premium
    pick falls out of the feed entirely. The server-side tier scope must
    keep the premium window premium-only."""
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(
        session, make_pick("evt-tier-window-prem"), teams, "value-sharp-vs-soft", "tw1"
    )
    for i in range(3):  # newer volume picks (persist_pick stamps created_at=now)
        await persist_pick(
            session,
            make_pick(f"evt-tier-window-vol-{i}", tier="volume", edge=0.02),
            teams,
            "value-sharp-vs-soft",
            "tw1",
        )

    # Unscoped narrow window: ALL volume — the open premium pick is invisible.
    unscoped = await latest_picks_with_events(session, limit=2)
    assert all(p["tier"] == "volume" for p in unscoped)

    # Premium-scoped window of the SAME size: the premium pick stays served.
    premium = await latest_picks_with_events(session, limit=2, tier="premium")
    assert any(p["bookmaker"] == "testbook" for p in premium)
    assert all(p["tier"] == "premium" for p in premium)

    volume = await latest_picks_with_events(session, limit=200, tier="volume")
    assert all(p["tier"] == "volume" for p in volume)
    assert sum(1 for p in volume if p["bookmaker"] == "testbook") == 3


async def test_latest_picks_min_acceptable_odds_execution_helper(session) -> None:  # type: ignore[no-untyped-def]
    # "still +EV down to X.XX": with min_edge the payload carries the floor
    # (model_probability 0.55, threshold 0.03 -> 1/0.52 = 1.923 -> "1.93"
    # after the round-UP display rule); without min_edge the field is null.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(session, make_pick("evt-minacc"), teams, "value-sharp-vs-soft", "t-minacc")

    payload = await latest_picks_with_events(session, limit=200, min_edge=0.03)
    ours = [p for p in payload if p["bookmaker"] == "testbook"]
    assert ours and ours[0]["min_acceptable_odds"] == "1.93"

    plain = await latest_picks_with_events(session, limit=200)
    ours_plain = [p for p in plain if p["bookmaker"] == "testbook"]
    assert ours_plain and ours_plain[0]["min_acceptable_odds"] is None


async def test_live_evidence_rows_reduce_settled_picks_to_floats(session) -> None:  # type: ignore[no-untyped-def]
    # The DB half of the /performance live-evidence section: settled picks
    # come back as plain-float rows; anchor_type stays None until the column
    # lands (feature-detected on the ORM attribute, never assumed).
    from sqlalchemy import insert as sa_insert
    from sqlalchemy import text

    from app.storage.models import ResultTracking
    from app.storage.repositories import live_evidence_rows

    if hasattr(Pick, "anchor_type"):
        # Track A3 transition window: the ORM attribute can land one commit
        # before its alembic migration is applied to this DB. Selecting the
        # ORM model would fail on EVERY query then — skip honestly instead
        # of failing on another track's migration sequencing.
        cols = {
            row[0]
            for row in await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'picks'"
                )
            )
        }
        if "anchor_type" not in cols:
            pytest.skip("picks.anchor_type ORM attr present but migration not yet applied")

    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(session, make_pick("evt-evidence"), teams, "value-sharp-vs-soft", "t-ev")
    pick_row = await session.scalar(
        select(Pick).where(Pick.bookmaker == "testbook").order_by(Pick.id.desc())
    )
    assert pick_row is not None
    await session.execute(
        sa_update(Pick)
        .where(Pick.id == pick_row.id)
        .values(
            status="settled",
            clv_log=Decimal("0.042"),
            beat_close=True,
            value_filter_score=Decimal("0.81"),
            closing_anchor_type="pinnacle",
            closing_odds=Decimal("2.1000"),  # snapshot-close marker => trusted
        )
    )
    await session.execute(
        sa_insert(ResultTracking).values(
            pick_id=pick_row.id,
            outcome="won",
            pnl=Decimal("22.00"),
            roi=Decimal("1.1"),
            settled_at=datetime(2026, 6, 11, 22, 0, tzinfo=UTC),
        )
    )

    rows = await live_evidence_rows(session)
    ours = [r for r in rows if r.stake == 20.0 and r.clv_log == 0.042]
    assert ours, "settled pick missing from live-evidence rows"
    r = ours[0]
    assert r.tier == "premium"
    assert r.value_filter_score == 0.81
    assert r.beat_close is True
    assert r.pnl == 22.0
    assert r.anchor_type is None  # creation anchor not set on this pick
    # Close-side provenance: a snapshot-sourced (closing_odds set) Pinnacle close
    # is a TRUSTED sharp close — the honest-CLV subset.
    assert r.closing_anchor_type == "pinnacle"
    assert r.has_snapshot_close is True
    assert r.sharp_close is True


async def test_anchor_type_roundtrips_and_serializes(session) -> None:  # type: ignore[no-untyped-def]
    # Track A live verdict mechanism: the anchor that produced a pick is
    # persisted (picks.anchor_type) and served to the dashboard so live CLV
    # can be stratified PIN/SHARP/CONS.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    pick = make_pick("evt-anchor-rt").model_copy(update={"anchor_type": "consensus"})
    assert await persist_pick(session, pick, teams, "value-sharp-vs-soft", "t-anchor") == "inserted"
    row = await session.scalar(
        select(Pick).where(Pick.bookmaker == "testbook").order_by(Pick.id.desc())
    )
    assert row is not None
    assert row.anchor_type == "consensus"
    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["event"] == "Alpha FC vs Beta United"]
    assert ours and ours[0]["anchor_type"] == "consensus"


async def test_anchor_type_follows_volume_to_premium_upgrade(session) -> None:  # type: ignore[no-untyped-def]
    # the upgraded row must describe the alert the operator acts on — the
    # promoting detection's anchor replaces the shadow row's.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    shadow = make_pick("evt-anchor-up", tier="volume", edge=0.02).model_copy(
        update={"anchor_type": "consensus"}
    )
    assert await persist_pick(session, shadow, teams, "value-sharp-vs-soft", "t-au") == "inserted"
    premium = make_pick("evt-anchor-up", tier="premium", edge=0.05).model_copy(
        update={"anchor_type": "pinnacle"}
    )
    assert await persist_pick(session, premium, teams, "value-sharp-vs-soft", "t-au") == "upgraded"
    row = await session.scalar(
        select(Pick).where(Pick.bookmaker == "testbook").order_by(Pick.id.desc())
    )
    assert row is not None
    assert row.tier == "premium"
    assert row.anchor_type == "pinnacle"


async def test_settled_pick_carries_outcome_and_pnl_from_result_tracking(session) -> None:  # type: ignore[no-untyped-def]
    # SETTLED view regression: the /picks payload must LEFT JOIN ResultTracking
    # so the dashboard's Result/P&L columns render the recorded outcome and the
    # realized P&L. Before the join these keys were absent and every settled
    # pick fell into the cellResult/cellPnl else-branch ("SETTLED" / "—").
    from sqlalchemy import insert as sa_insert

    from app.storage.models import ResultTracking

    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(session, make_pick("evt-settled-pnl"), teams, "value-sharp-vs-soft", "t-set")
    pick_row = await session.scalar(
        select(Pick).where(Pick.bookmaker == "testbook").order_by(Pick.id.desc())
    )
    assert pick_row is not None
    await session.execute(sa_update(Pick).where(Pick.id == pick_row.id).values(status="settled"))
    await session.execute(
        sa_insert(ResultTracking).values(
            pick_id=pick_row.id,
            outcome="won",
            pnl=Decimal("1.00"),
            roi=Decimal("0.05"),
            settled_at=datetime(2026, 6, 11, 22, 0, tzinfo=UTC),
        )
    )
    await session.flush()

    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["event_id"] == pick_row.event_id]
    assert ours, "settled pick missing from /picks payload"
    p = ours[0]
    assert p["outcome"] == "won"  # the Result column's badge value
    assert p["pnl"] == "1.00"  # realized P&L, Decimal-stringified at the boundary


async def test_open_pick_has_null_outcome_and_pnl(session) -> None:  # type: ignore[no-untyped-def]
    # Open/unverified picks have no ResultTracking row; the LEFT JOIN must keep
    # outcome/pnl NULL (not raise, not invent a result).
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(
        session, make_pick("evt-open-noresult"), teams, "value-sharp-vs-soft", "t-op"
    )
    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["event"] == "Alpha FC vs Beta United"]
    assert ours
    assert ours[0]["outcome"] is None
    assert ours[0]["pnl"] is None


async def test_record_result_repost_is_idempotent(committing_session) -> None:  # type: ignore[no-untyped-def]
    # P2a regression: re-posting a manual result (a correction or duplicate
    # submit) must UPDATE the existing row, not 500 on the unique pick_id
    # constraint (uq_result_tracking_pick). Uses the savepoint-isolated session
    # because record_result COMMITS — the plain `session` fixture would leak it.
    from app.api.routes import record_result
    from app.schemas.base import Outcome
    from app.schemas.events import ResultIn
    from app.storage.models import ResultTracking

    session = committing_session
    teams = EventTeams(home="Repost Home", away="Repost Away", league="test-repost")
    await persist_pick(
        session, make_pick("evt-repost", league="test-repost"), teams, "value-sharp-vs-soft", "t-rp"
    )
    pick = await session.scalar(
        select(Pick)
        .join(Event, Pick.event_id == Event.id)
        .where(Event.external_ref == "evt-repost")
    )
    assert pick is not None
    now = datetime.now(tz=UTC)

    first = await record_result(
        pick.id, ResultIn(pick_id=str(pick.id), outcome=Outcome.WON, settled_at=now), session
    )
    assert first["outcome"] == "won"
    # re-post a CORRECTED outcome: must not raise, must update the row in place
    second = await record_result(
        pick.id, ResultIn(pick_id=str(pick.id), outcome=Outcome.LOST, settled_at=now), session
    )
    assert second["outcome"] == "lost"

    rows = (
        (await session.execute(select(ResultTracking).where(ResultTracking.pick_id == pick.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1  # ONE result row, updated (no duplicate, no crash)
    assert rows[0].outcome == "lost"


async def test_available_games_fallback_includes_tennis_with_unvalidated_flag(session) -> None:  # type: ignore[no-untyped-def]
    # Doctrine-safety regression: the restart-durability fallback must include
    # VISIBILITY-ONLY tennis AND tag it unvalidated=True, so the dashboard's
    # UNVALIDATED badge survives a restart. The default unscoped query used to
    # filter to soccer/basketball only and emit no unvalidated key, so a
    # tennis row served from the warehouse rendered as a validated sport.
    now = datetime.now(tz=UTC)
    kickoff = now + timedelta(hours=3)
    tennis_teams = EventTeams(
        home="Tennis Player A",
        away="Tennis Player B",
        league="atp-test-tour",
        starts_at=kickoff,
    )
    tennis_pick = make_pick("evt-games-tennis", league="atp-test-tour").model_copy(
        update={"sport": "tennis"}
    )
    await persist_pick(session, tennis_pick, tennis_teams, "value-sharp-vs-soft", "t-games-tennis")
    event = await session.scalar(select(Event).where(Event.external_ref == "evt-games-tennis"))
    assert event is not None
    captured = now - timedelta(minutes=5)
    session.add(
        OddsSnapshot(
            event_id=event.id,
            bookmaker="Pinnacle",
            market="h2h",
            selection="Tennis Player A",
            decimal_odds=Decimal("1.9000"),
            liquidity=None,
            captured_at=captured,
            ingested_at=captured + timedelta(seconds=10),
        )
    )
    await session.flush()

    # Unscoped (sport=None) is the path the /games route uses for the fallback.
    # High limit so a POPULATED shared dev DB (the running app accumulates
    # hundreds of events ordered by kickoff before this now+3h fixture) can't
    # truncate our fixture out of the top-N — the assertion is about PRESENCE
    # in the unscoped warehouse view + the unvalidated flag, not top-N ranking
    # (mirrors the soccer sibling above, which already uses limit=5000).
    rows = await latest_available_games_with_events(session, limit=5000, now=now)
    ours = [row for row in rows if row["event_id"] == "evt-games-tennis"]
    assert ours, "warehouse fallback excluded the tennis fixture"
    row = ours[0]
    assert row["sport"] == "tennis"
    assert row["sport_label"] == "Tennis"
    assert row["unvalidated"] is True  # the doctrine-safety badge driver

    # And a validated sport from the same path must carry unvalidated=False so
    # the flag is a reliable discriminator the dashboard can badge on.
    soccer_teams = EventTeams(
        home="Soccer Home", away="Soccer Away", league="test-league-games", starts_at=kickoff
    )
    await persist_pick(
        session,
        make_pick("evt-games-soccer", league="test-league-games"),
        soccer_teams,
        "value-sharp-vs-soft",
        "t-games-soccer",
    )
    soccer_event = await session.scalar(
        select(Event).where(Event.external_ref == "evt-games-soccer")
    )
    assert soccer_event is not None
    session.add(
        OddsSnapshot(
            event_id=soccer_event.id,
            bookmaker="Pinnacle",
            market="h2h",
            selection="Soccer Home",
            decimal_odds=Decimal("2.0000"),
            liquidity=None,
            captured_at=captured,
            ingested_at=captured + timedelta(seconds=10),
        )
    )
    await session.flush()
    # High limit (see above) so a populated shared dev DB can't crowd the
    # soccer comparison fixture out of the top-N either.
    rows2 = await latest_available_games_with_events(session, limit=5000, now=now)
    soccer_rows = [row for row in rows2 if row["event_id"] == "evt-games-soccer"]
    assert soccer_rows and soccer_rows[0]["unvalidated"] is False


async def test_settled_pick_persists_and_serializes_final_score(session) -> None:  # type: ignore[no-untyped-def]
    # Final-score regression: settling an event must persist the game's final
    # score on result_tracking (home_score/away_score) AND the /picks payload
    # must serialize it as "HOME-AWAY" so the dashboard SETTLED view's Score
    # column renders it. Drives the real settlement path (settle_event_picks),
    # not a hand-written ResultTracking insert, so the engine wiring is covered.
    from app.settlement.engine import settle_event_picks
    from app.storage.models import ResultTracking

    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(session, make_pick("evt-final-score"), teams, "value-sharp-vs-soft", "t-fs")
    event = await session.scalar(select(Event).where(Event.external_ref == "evt-final-score"))
    assert event is not None

    now = datetime.now(tz=UTC)
    settled, skipped = await settle_event_picks(
        session, event.id, home_score=2, away_score=1, now=now
    )
    assert settled == 1
    assert skipped == 0

    # result_tracking row carries the plain-int scores
    result = await session.scalar(
        select(ResultTracking)
        .join(Pick, ResultTracking.pick_id == Pick.id)
        .where(Pick.event_id == event.id)
    )
    assert result is not None
    assert result.home_score == 2
    assert result.away_score == 1

    # and the /picks payload serializes it HOME-first as a "2-1" string
    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["event_id"] == event.id]
    assert ours, "settled pick missing from /picks payload"
    assert ours[0]["score"] == "2-1"


async def test_open_pick_has_null_score(session) -> None:  # type: ignore[no-untyped-def]
    # An open/unsettled pick has no ResultTracking row; the LEFT JOIN must keep
    # `score` null (CLOSED view renders "—"), never invent a score.
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(
        session, make_pick("evt-open-noscore"), teams, "value-sharp-vs-soft", "t-ons"
    )
    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["event"] == "Alpha FC vs Beta United"]
    assert ours
    assert ours[0]["score"] is None


async def test_persist_pick_does_not_write_scraped_score(session) -> None:  # type: ignore[no-untyped-def]
    # CORRECTNESS (review 2026-06-21 critical): the routine scrape/persist path
    # must NOT write Event.scraped_* even when EventTeams carries a score — a
    # pre-kickoff/in-play scrape's "score" can be an in-play PARTIAL (OddsPortal
    # shows a live running score), which would corrupt settlement + ROI. Only the
    # finished-gated capture_finished_scores path writes scraped_*.
    teams = EventTeams(
        home="Alpha FC",
        away="Beta United",
        league="test-league-persist",
        home_score=2,  # an in-play partial the routine path must IGNORE
        away_score=1,
    )
    await persist_pick(
        session, make_pick("evt-scraped-score"), teams, "value-sharp-vs-soft", "t-ss"
    )

    event = await session.scalar(select(Event).where(Event.external_ref == "evt-scraped-score"))
    assert event is not None
    assert event.scraped_home_score is None  # routine persist never writes scraped scores
    assert event.scraped_away_score is None

    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["event_id"] == event.id]
    assert ours, "pick missing from /picks payload"
    assert ours[0]["scraped_score"] is None
    assert ours[0]["score"] is None


async def test_scraped_score_absent_serializes_null(session) -> None:  # type: ignore[no-untyped-def]
    # The common case: the match was never scraped after finishing, so no score
    # is captured and the payload's scraped_score is null (the settle prompt then
    # has nothing to pre-fill and the user types it, as today).
    teams = EventTeams(home="Alpha FC", away="Beta United", league="test-league-persist")
    await persist_pick(session, make_pick("evt-noscrape"), teams, "value-sharp-vs-soft", "t-ns")

    event = await session.scalar(select(Event).where(Event.external_ref == "evt-noscrape"))
    assert event is not None
    assert event.scraped_home_score is None
    assert event.scraped_away_score is None

    payload = await latest_picks_with_events(session, limit=200)
    ours = [p for p in payload if p["event_id"] == event.id]
    assert ours
    assert ours[0]["scraped_score"] is None
