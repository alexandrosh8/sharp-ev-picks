"""Odds-snapshot persistence: change-only appends into odds_snapshots.

The scraped multi-book odds are the dataset for backtests, line-movement
features, and CLV verification — every cycle must persist price CHANGES
(raw append-only would explode at 5-20k observations/cycle).

DB tests use the compose Postgres with savepoint isolation (skip when
absent — same pattern as tests/test_clv_trueup.py); pipeline-seam tests
use fakes and monkeypatching, no DB.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.edge.gates import GatePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import NullModel
from app.notifications.base import Alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import (
    ODDS_SEEN_TTL,
    PipelineDeps,
    _persist_snapshots,
    _sweep_odds_seen,
    run_value_pipeline,
)
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.storage.models import Event, OddsSnapshot
from app.storage.repositories import persist_odds_snapshots

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"
NOW = datetime.now(tz=UTC)
EVENT = "evt-odds-persist-1"
TEAMS = {EVENT: EventTeams(home="Persist Home FC", away="Persist Away FC")}

POLICY = GatePolicy(
    min_edge=0.0,
    min_ev=0.0,
    min_confidence=0.0,
    max_odds_age_seconds=300,
    min_liquidity=0.0,
)


def snap(
    book: str,
    sel: str,
    odds: float,
    captured: datetime = NOW,
    detail: str | None = None,
    market: Market = Market.H2H,
    event: str = EVENT,
) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id=event,
        bookmaker=book,
        market=market,
        selection=sel,
        decimal_odds=odds,
        captured_at=captured,
        ingested_at=captured,
        market_detail=detail,
    )


class FakeLoader:
    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        self.snapshots = snapshots
        self.last_fetch_matches: dict[str, int] = {}

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        self.last_fetch_matches[sport_key] = len({s.event_id for s in self.snapshots})
        return self.snapshots


class RecordingSink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


class FakeSessionFactory:
    """Minimal async-contextmanager session; DB calls against it raise and
    are swallowed by the pipeline's failure isolation."""

    def __call__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
        return False

    async def commit(self) -> None:
        return None


def make_deps(
    snapshots: list[OddsSnapshotIn],
    session_factory: Any = None,
) -> PipelineDeps:
    directory = EventDirectory()
    for s in snapshots:
        directory.register(s.event_id, TEAMS.get(s.event_id, EventTeams(home="H", away="A")))
    return PipelineDeps(
        loader=FakeLoader(snapshots),
        model=NullModel(),
        dispatcher=AlertDispatcher([RecordingSink()], InMemoryIdempotencyStore()),
        gate_policy=POLICY,
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        directory=directory,
        session_factory=session_factory,
    )


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


async def _rows_for_event(maker, event_ref: str) -> int:  # type: ignore[no-untyped-def]
    async with maker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(OddsSnapshot)
            .join(Event, OddsSnapshot.event_id == Event.id)
            .where(Event.external_ref == event_ref)
        )
    return int(count or 0)


# ---------------------------------------------------------------------------
# DB tests (compose Postgres)
# ---------------------------------------------------------------------------


async def test_change_only_same_odds_once_price_move_twice(factory) -> None:  # type: ignore[no-untyped-def]
    """Same odds re-scraped -> NO new row (the last-seen cache suppresses
    them); a price move -> a new row. captured_at differs per cycle, so only
    the cache (not the DB unique key) prevents the re-write."""
    deps = make_deps([], session_factory=factory)
    assert deps.directory is not None
    deps.directory.register(EVENT, TEAMS[EVENT])

    cycle1 = [snap("Pinnacle", "Home FC", 2.50), snap("SoftBook", "Home FC", 2.90)]
    assert await _persist_snapshots(deps, cycle1, "soccer", "test-league", NOW) == 2
    assert await _rows_for_event(factory, EVENT) == 2

    t2 = NOW + timedelta(minutes=10)
    cycle2 = [
        snap("Pinnacle", "Home FC", 2.50, captured=t2),
        snap("SoftBook", "Home FC", 2.90, captured=t2),
    ]
    assert await _persist_snapshots(deps, cycle2, "soccer", "test-league", t2) == 0
    assert await _rows_for_event(factory, EVENT) == 2  # unchanged prices: 1 row per key

    t3 = NOW + timedelta(minutes=20)
    cycle3 = [
        snap("Pinnacle", "Home FC", 2.50, captured=t3),
        snap("SoftBook", "Home FC", 2.95, captured=t3),  # price moved
    ]
    assert await _persist_snapshots(deps, cycle3, "soccer", "test-league", t3) == 1
    assert await _rows_for_event(factory, EVENT) == 3


async def test_conflict_idempotency_same_key_and_captured_at(factory) -> None:  # type: ignore[no-untyped-def]
    """Cold-cache double write of the SAME observation (key + captured_at):
    ON CONFLICT DO NOTHING on uq_odds_snapshot_observation keeps one row —
    the restart re-write the change-only design accepts dedupes here."""
    rows = [snap("Pinnacle", "Home FC", 2.50)]
    assert await persist_odds_snapshots(factory, rows, TEAMS, "soccer", "test-league") == 1
    assert await persist_odds_snapshots(factory, rows, TEAMS, "soccer", "test-league") == 0
    assert await _rows_for_event(factory, EVENT) == 1


async def test_attach_only_skips_when_event_absent(factory) -> None:  # type: ignore[no-untyped-def]
    """attach_only_to_existing=True must NEVER create the event: a ref with no
    pre-existing Event row is SKIPPED this cycle (0 rows written, 0 events
    created), not grafted from the snapshot's own team data. This is the
    Betfair inline-binding safety contract — the capture may only ATTACH odds to
    a canonical event the MAIN scrape already created, never mint a partial one.
    """
    ref = "evt-attach-absent-1"
    teams = {ref: EventTeams(home="Attach Home FC", away="Attach Away FC")}
    rows = [snap("Betfair Exchange", "Attach Home FC", 2.10, event=ref)]

    written = await persist_odds_snapshots(
        factory, rows, teams, "soccer", "test-league", attach_only_to_existing=True
    )
    assert written == 0
    assert await _rows_for_event(factory, ref) == 0
    # No event row was minted from the Betfair-only data.
    async with factory() as session:
        event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.external_ref == ref)
        )
    assert event_count == 0


async def test_attach_only_attaches_to_existing_event(factory) -> None:  # type: ignore[no-untyped-def]
    """attach_only_to_existing=True attaches odds to a canonical event that
    ALREADY exists (created by a prior normal persist) — same external_ref, new
    bookmaker rows land on it, and NO second event row is created."""
    ref = "evt-attach-present-1"
    teams = {ref: EventTeams(home="Attach Home FC", away="Attach Away FC")}

    # The MAIN scrape creates the canonical event first (normal create path).
    seed = [snap("SoftBook", "Attach Home FC", 2.30, event=ref)]
    assert await persist_odds_snapshots(factory, seed, teams, "soccer", "test-league") == 1

    # Betfair attaches inline under attach-only: its row lands on the SAME event.
    betfair = [snap("Betfair Exchange", "Attach Home FC", 2.12, event=ref)]
    written = await persist_odds_snapshots(
        factory, betfair, teams, "soccer", "test-league", attach_only_to_existing=True
    )
    assert written == 1
    assert await _rows_for_event(factory, ref) == 2  # soft + betfair on one event

    async with factory() as session:
        books = (
            (
                await session.execute(
                    select(OddsSnapshot.bookmaker)
                    .join(Event, OddsSnapshot.event_id == Event.id)
                    .where(Event.external_ref == ref)
                )
            )
            .scalars()
            .all()
        )
        event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.external_ref == ref)
        )
    assert set(books) == {"SoftBook", "Betfair Exchange"}
    assert event_count == 1  # exactly ONE canonical event row


async def test_persisted_row_values_and_line_qualified_market(factory) -> None:  # type: ignore[no-untyped-def]
    """Odds cross the boundary Decimal-via-string; captured_at is the scrape
    observation time (NOT now()); the market column stores the line-qualified
    submarket key so distinct lines stay distinct observations."""
    captured = NOW - timedelta(minutes=7)
    rows = [
        snap("Pinnacle", "Home FC -1.5", 2.05, captured, "asian_handicap_-1_5", Market.SPREADS),
        snap("Pinnacle", "Home FC", 2.50, captured),  # no detail -> enum value
    ]
    assert await persist_odds_snapshots(factory, rows, TEAMS, "soccer", "test-league") == 2

    async with factory() as session:
        stored = (
            (
                await session.execute(
                    select(OddsSnapshot)
                    .join(Event, OddsSnapshot.event_id == Event.id)
                    .where(Event.external_ref == EVENT)
                    .order_by(OddsSnapshot.market)
                )
            )
            .scalars()
            .all()
        )
    assert [r.market for r in stored] == ["asian_handicap_-1_5", "h2h"]
    ah = stored[0]
    assert ah.decimal_odds == Decimal("2.0500")
    assert ah.selection == "Home FC -1.5"
    assert ah.captured_at == captured
    assert ah.is_closing is False


async def test_unresolvable_event_skipped_and_not_cached(factory) -> None:  # type: ignore[no-untyped-def]
    """An event the directory cannot resolve is skipped WITHOUT caching its
    odds — once it becomes resolvable the same price must still be written."""
    deps = make_deps([], session_factory=factory)
    assert deps.directory is not None  # empty directory: nothing registered

    rows = [snap("Pinnacle", "Home FC", 2.50)]
    assert await _persist_snapshots(deps, rows, "soccer", "test-league", NOW) == 0
    assert deps.odds_seen == {}
    assert await _rows_for_event(factory, EVENT) == 0

    deps.directory.register(EVENT, TEAMS[EVENT])
    assert await _persist_snapshots(deps, rows, "soccer", "test-league", NOW) == 1
    assert await _rows_for_event(factory, EVENT) == 1


async def test_poisoned_event_does_not_kill_other_events_history(factory) -> None:  # type: ignore[no-untyped-def]
    """Per-event SAVEPOINT isolation: ONE event whose insert fails (here an
    external_ref longer than events.external_ref String(128) — live max is
    already 112 chars) must not abort the whole batch. Pre-fix the entire
    cycle's odds history died, every cycle, for as long as the bad match
    stayed in the scrape window. The poisoned event leads the batch to prove
    recovery after its savepoint rollback."""
    bad_ref = "https://www.oddsportal.com/football/world/" + "x" * 120  # > 128 chars
    teams = {
        bad_ref: EventTeams(home="Poison Home FC", away="Poison Away FC"),
        EVENT: TEAMS[EVENT],
    }
    rows = [
        snap("Pinnacle", "Poison Sel", 2.10, event=bad_ref),
        snap("Pinnacle", "Home FC", 2.50),
    ]
    written = await persist_odds_snapshots(factory, rows, teams, "soccer", "test-league")
    assert written == 1  # the healthy event persisted despite the poison
    assert await _rows_for_event(factory, EVENT) == 1
    assert await _rows_for_event(factory, bad_ref) == 0


async def test_overlong_free_text_fields_are_clamped_not_fatal(factory) -> None:  # type: ignore[no-untyped-def]
    """bookmaker/selection are display strings: clamping to their column
    lengths (64) beats losing the event's whole history to one oversized
    'TeamName +10.5'-style selection (leagues=all surfaces long slugs)."""
    rows = [snap("B" * 80, "S" * 80, 2.50)]
    assert await persist_odds_snapshots(factory, rows, TEAMS, "soccer", "test-league") == 1

    async with factory() as session:
        stored = await session.scalar(
            select(OddsSnapshot)
            .join(Event, OddsSnapshot.event_id == Event.id)
            .where(Event.external_ref == EVENT)
        )
    assert stored is not None
    assert stored.bookmaker == "B" * 64
    assert stored.selection == "S" * 64


# ---------------------------------------------------------------------------
# Pipeline-seam tests (fakes, no DB)
# ---------------------------------------------------------------------------


def market_snapshots(odds_home_soft: float = 2.90) -> list[OddsSnapshotIn]:
    return [
        snap("Pinnacle", "Home FC", 2.50, NOW - timedelta(seconds=30)),
        snap("Pinnacle", "Draw", 3.30, NOW - timedelta(seconds=30)),
        snap("Pinnacle", "Away FC", 3.10, NOW - timedelta(seconds=30)),
        snap("SoftBook", "Home FC", odds_home_soft, NOW - timedelta(seconds=30)),
        snap("SoftBook", "Draw", 3.20, NOW - timedelta(seconds=30)),
        snap("SoftBook", "Away FC", 2.95, NOW - timedelta(seconds=30)),
    ]


def patch_persist_counting(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """persist_odds_snapshots fake that records the batch sizes it was
    handed and reports every row as newly written."""
    import app.storage.repositories as repos

    batches: list[int] = []

    async def fake_persist(session_factory, snapshots, teams_by_event, sport, default_league):  # type: ignore[no-untyped-def]
        batches.append(len(snapshots))
        return len(snapshots)

    monkeypatch.setattr(repos, "persist_odds_snapshots", fake_persist)
    return batches


async def test_last_poll_carries_snapshots_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The poll record exposes how many odds rows the cycle persisted, so
    /health and the dashboard ingestion strip can show it. A second cycle
    with unchanged odds writes (and reports) zero."""
    from app.pipeline import LAST_POLL

    batches = patch_persist_counting(monkeypatch)
    deps = make_deps(market_snapshots(), session_factory=FakeSessionFactory())

    await run_value_pipeline(deps, "soccer")
    assert LAST_POLL["soccer"]["snapshots_persisted"] == 6
    assert batches == [6]

    await run_value_pipeline(deps, "soccer")  # same odds -> change-only skips all
    assert LAST_POLL["soccer"]["snapshots_persisted"] == 0
    assert batches == [6]  # repo not even called for an all-unchanged cycle


async def test_price_move_is_persisted_again(monkeypatch: pytest.MonkeyPatch) -> None:
    batches = patch_persist_counting(monkeypatch)
    deps = make_deps(market_snapshots(), session_factory=FakeSessionFactory())

    await run_value_pipeline(deps, "soccer")
    assert batches == [6]

    deps.loader.snapshots = market_snapshots(odds_home_soft=2.95)  # type: ignore[attr-defined]
    await run_value_pipeline(deps, "soccer")
    assert batches == [6, 1]  # only the moved price is re-written


async def test_pipeline_completes_when_snapshot_persistence_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure isolation: snapshot persistence blowing up must never break
    pick generation — and must NOT poison the last-seen cache (the failed
    batch is retried next cycle)."""
    import app.storage.repositories as repos

    async def boom(session_factory, snapshots, teams_by_event, sport, default_league):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")

    monkeypatch.setattr(repos, "persist_odds_snapshots", boom)

    from app.pipeline import LAST_POLL

    deps = make_deps(market_snapshots(), session_factory=FakeSessionFactory())
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1  # the cycle still produced its pick
    assert LAST_POLL["soccer"]["snapshots_persisted"] is None  # unknown, not 0
    assert deps.odds_seen == {}  # failed write never marks odds as seen


async def test_no_session_factory_records_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.pipeline import LAST_POLL

    batches = patch_persist_counting(monkeypatch)
    deps = make_deps(market_snapshots(), session_factory=None)
    await run_value_pipeline(deps, "soccer")
    assert LAST_POLL["soccer"]["snapshots_persisted"] is None
    assert batches == []


def test_odds_seen_sweep_evicts_stale_then_oldest() -> None:
    """Bounded-cache policy: nothing happens under the cap; over the cap,
    TTL-stale entries go first, then oldest-seen down to the cap."""
    stale = NOW - ODDS_SEEN_TTL - timedelta(hours=1)
    cache: dict[tuple[str, str, str, str], tuple[float, datetime]] = {}
    for i in range(6):
        cache[("e", "b", "m", f"old-{i}")] = (2.0, stale)
    for i in range(6):
        cache[("e", "b", "m", f"recent-{i}")] = (2.0, NOW - timedelta(minutes=6 - i))

    _sweep_odds_seen(cache, NOW, max_size=4)
    assert len(cache) == 4
    assert set(cache) == {("e", "b", "m", f"recent-{i}") for i in range(2, 6)}

    small = {("e", "b", "m", "x"): (2.0, stale)}
    _sweep_odds_seen(small, NOW, max_size=4)
    assert len(small) == 1  # under the cap: even stale entries survive
