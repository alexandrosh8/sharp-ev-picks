"""DB-sourced, bounded, rotating Betfair-capture targets (CPU-aware fix).

ROOT CAUSE these tests pin: ``_betfair_targets`` previously read
``loader.last_fetch_event_ids["soccer"]``, populated ONLY when a poll_odds
full-scrape COMPLETES. On a CPU-bound box poll_odds skips every slot (one slow
scrape holds the single instance), so the Betfair reader got NO targets and
captured nothing — even £270k-liquidity majors.

``select_betfair_targets`` decouples the Betfair capture from full-scrape
completion: it sources targets from the DB (recent UPCOMING soccer events that
already have odds and have not kicked off), BOUNDED to a small N per cycle and
ROTATING by longest-since-last-Betfair-capture so the slate is covered over
cycles without ever opening all ~91 pages at once.

DB tests use the compose Postgres (:5433); skipped when absent, inside ONE
rolled-back transaction (the tests/test_betfair_exchange.py ``factory`` pattern)
so nothing commits to the shared DB. NO live network, ever.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.storage.repositories import persist_odds_snapshots, select_betfair_targets

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"
NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:  # noqa: BLE001
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


async def _create_soccer_event(
    factory: async_sessionmaker,
    *,
    url: str,
    home: str,
    away: str,
    starts_at: datetime | None,
    bookmaker: str = "bet365",
    captured_at: datetime = NOW,
) -> None:
    """Create a canonical soccer event WITH odds (the main scrape's normal
    create path), so it is a valid Betfair target."""
    await persist_odds_snapshots(
        factory,
        [
            OddsSnapshotIn(
                event_id=url,
                bookmaker=bookmaker,
                market=Market.H2H,
                selection=home,
                decimal_odds=2.0,
                captured_at=captured_at,
                ingested_at=captured_at,
            )
        ],
        {url: EventTeams(home=home, away=away, league="Club WC", starts_at=starts_at)},
        sport="soccer",
        default_league="Club WC",
    )


async def _capture_betfair(
    factory: async_sessionmaker,
    *,
    url: str,
    home: str,
    away: str,
    captured_at: datetime,
) -> None:
    """Record a Betfair Exchange BACK row on the canonical event (the same
    inline-binding path BetfairExchangeCapture uses), so the event has a
    last-Betfair-capture timestamp."""
    await persist_odds_snapshots(
        factory,
        [
            OddsSnapshotIn(
                event_id=url,
                bookmaker="Betfair Exchange",
                market=Market.H2H,
                selection=home,
                decimal_odds=2.1,
                captured_at=captured_at,
                ingested_at=captured_at,
            )
        ],
        {url: EventTeams(home=home, away=away, league="Club WC")},
        sport="soccer",
        default_league="Club WC",
        attach_only_to_existing=True,
    )


# --------------------------------------------------------------------------- #
# DECOUPLING: targets come from the DB, NOT from last_fetch_event_ids — so they
# populate even when poll_odds has NEVER completed a full scrape this run.
# --------------------------------------------------------------------------- #
async def test_targets_from_db_without_any_full_scrape_completion(factory) -> None:  # type: ignore[no-untyped-def]
    # An upcoming soccer event with odds exists in the DB. No last_fetch_event_ids
    # is consulted; the query reads the warehouse directly.
    url = f"https://www.oddsportal.com/football/eng/{uuid4()}"
    await _create_soccer_event(
        factory, url=url, home="England", away="Ghana", starts_at=NOW + timedelta(hours=3)
    )
    targets = await select_betfair_targets(
        factory, sport="soccer", now=NOW, window=timedelta(days=3), limit=20
    )
    assert [t.external_ref for t in targets] == [url]
    assert targets[0].home == "England"
    assert targets[0].away == "Ghana"


async def test_far_future_event_past_window_excluded(factory) -> None:  # type: ignore[no-untyped-def]
    # A target must fall inside the recent/upcoming window; an event 30 days out
    # is past the 3-day window and must not burn a per-cycle slot.
    url = f"https://www.oddsportal.com/football/far/{uuid4()}"
    await _create_soccer_event(
        factory, url=url, home="A", away="B", starts_at=NOW + timedelta(days=30)
    )
    targets = await select_betfair_targets(
        factory, sport="soccer", now=NOW, window=timedelta(days=3), limit=20
    )
    assert url not in {t.external_ref for t in targets}


# --------------------------------------------------------------------------- #
# KICKED-OFF events are excluded — the Betfair pre-match BACK row is gone, and
# re-reading them wastes the scarce per-cycle budget.
# --------------------------------------------------------------------------- #
async def test_kicked_off_event_excluded(factory) -> None:  # type: ignore[no-untyped-def]
    live = f"https://www.oddsportal.com/football/live/{uuid4()}"
    upcoming = f"https://www.oddsportal.com/football/up/{uuid4()}"
    await _create_soccer_event(
        factory, url=live, home="Live H", away="Live A", starts_at=NOW - timedelta(minutes=5)
    )
    await _create_soccer_event(
        factory, url=upcoming, home="Up H", away="Up A", starts_at=NOW + timedelta(hours=2)
    )
    targets = await select_betfair_targets(
        factory, sport="soccer", now=NOW, window=timedelta(days=3), limit=20
    )
    refs = {t.external_ref for t in targets}
    assert upcoming in refs
    assert live not in refs


# --------------------------------------------------------------------------- #
# BOUND: never returns more than `limit` targets, so the capture can NEVER try
# all ~91 pages at once on a CPU-bound box.
# --------------------------------------------------------------------------- #
async def test_bound_caps_targets_per_cycle(factory) -> None:  # type: ignore[no-untyped-def]
    for i in range(5):
        await _create_soccer_event(
            factory,
            url=f"https://www.oddsportal.com/football/b{i}/{uuid4()}",
            home=f"H{i}",
            away=f"A{i}",
            starts_at=NOW + timedelta(hours=1 + i),
        )
    targets = await select_betfair_targets(
        factory, sport="soccer", now=NOW, window=timedelta(days=3), limit=3
    )
    assert len(targets) == 3


# --------------------------------------------------------------------------- #
# ROTATION: never-captured events lead; among captured, the STALEST
# (longest-since-last-Betfair-capture) goes first — so a small per-cycle bound
# sweeps the whole slate over successive cycles instead of re-reading the same
# few games forever.
# --------------------------------------------------------------------------- #
async def test_rotation_never_captured_first_then_stalest(factory) -> None:  # type: ignore[no-untyped-def]
    # Three upcoming events at the SAME kickoff so ordering is decided by
    # last-Betfair-capture alone.
    kickoff = NOW + timedelta(hours=5)
    fresh = f"https://www.oddsportal.com/football/fresh/{uuid4()}"
    stale = f"https://www.oddsportal.com/football/stale/{uuid4()}"
    never = f"https://www.oddsportal.com/football/never/{uuid4()}"
    for url, home, away in (
        (fresh, "Fresh H", "Fresh A"),
        (stale, "Stale H", "Stale A"),
        (never, "Never H", "Never A"),
    ):
        await _create_soccer_event(factory, url=url, home=home, away=away, starts_at=kickoff)
    # fresh captured 1 minute ago; stale captured 2 hours ago; never never.
    await _capture_betfair(
        factory, url=fresh, home="Fresh H", away="Fresh A", captured_at=NOW - timedelta(minutes=1)
    )
    await _capture_betfair(
        factory, url=stale, home="Stale H", away="Stale A", captured_at=NOW - timedelta(hours=2)
    )
    targets = await select_betfair_targets(
        factory, sport="soccer", now=NOW, window=timedelta(days=3), limit=20
    )
    order = [t.external_ref for t in targets]
    # never-captured leads; then stalest (2h) before freshest (1m).
    assert order.index(never) < order.index(stale) < order.index(fresh)


# --------------------------------------------------------------------------- #
# A bound of 1 over a rotating slate sweeps DIFFERENT events as captures land —
# the per-cycle rotation contract that prevents starving any single fixture.
# --------------------------------------------------------------------------- #
async def test_bound_one_rotates_as_captures_land(factory) -> None:  # type: ignore[no-untyped-def]
    kickoff = NOW + timedelta(hours=4)
    a = f"https://www.oddsportal.com/football/ra/{uuid4()}"
    b = f"https://www.oddsportal.com/football/rb/{uuid4()}"
    await _create_soccer_event(factory, url=a, home="RA H", away="RA A", starts_at=kickoff)
    await _create_soccer_event(factory, url=b, home="RB H", away="RB A", starts_at=kickoff)
    # Both never captured; tie broken deterministically (by kickoff then ref).
    first = await select_betfair_targets(
        factory, sport="soccer", now=NOW, window=timedelta(days=3), limit=1
    )
    assert len(first) == 1
    picked = first[0].external_ref
    other = b if picked == a else a
    # Capture the one just picked; next cycle the OTHER (still never-captured)
    # must lead.
    await _capture_betfair(
        factory,
        url=picked,
        home=first[0].home,
        away=first[0].away,
        captured_at=NOW + timedelta(seconds=30),
    )
    second = await select_betfair_targets(
        factory, sport="soccer", now=NOW + timedelta(minutes=1), window=timedelta(days=3), limit=1
    )
    assert len(second) == 1
    assert second[0].external_ref == other


# --------------------------------------------------------------------------- #
# Non-URL refs (synthetic "home|away|date" ids) can't be navigated — excluded so
# the reader never builds a useless target.
# --------------------------------------------------------------------------- #
async def test_non_url_ref_excluded(factory) -> None:  # type: ignore[no-untyped-def]
    synthetic = f"Team A|Team B|{uuid4()}"
    await persist_odds_snapshots(
        factory,
        [
            OddsSnapshotIn(
                event_id=synthetic,
                bookmaker="bet365",
                market=Market.H2H,
                selection="Team A",
                decimal_odds=2.0,
                captured_at=NOW,
                ingested_at=NOW,
            )
        ],
        {
            synthetic: EventTeams(
                home="Team A", away="Team B", league="X", starts_at=NOW + timedelta(hours=2)
            )
        },
        sport="soccer",
        default_league="X",
    )
    targets = await select_betfair_targets(
        factory, sport="soccer", now=NOW, window=timedelta(days=3), limit=20
    )
    assert synthetic not in {t.external_ref for t in targets}


# --------------------------------------------------------------------------- #
# NULL-kickoff (TBD) events are excluded: we can't prove they haven't started,
# and the pre-match Betfair row may be gone — don't burn the scarce budget.
# --------------------------------------------------------------------------- #
async def test_null_kickoff_excluded(factory) -> None:  # type: ignore[no-untyped-def]
    url = f"https://www.oddsportal.com/football/tbd/{uuid4()}"
    await _create_soccer_event(factory, url=url, home="TBD H", away="TBD A", starts_at=None)
    targets = await select_betfair_targets(
        factory, sport="soccer", now=NOW, window=timedelta(days=3), limit=20
    )
    assert url not in {t.external_ref for t in targets}
