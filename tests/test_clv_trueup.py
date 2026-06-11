"""CLV true-up: open picks get closing-fair/clv_log refreshed from fresh odds.

Uses the compose Postgres with savepoint isolation (skips when absent).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clv_trueup import true_up_clv
from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Pick
from app.storage.repositories import persist_pick

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai"
NOW = datetime.now(tz=UTC)


class FakeLoader:
    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        self._snapshots = snapshots

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        return self._snapshots


def closing_snapshots(event_id: str) -> list[OddsSnapshotIn]:
    # Pinnacle close: 2.20 / 3.40 / 3.30 -> devigged fair for Home ~ 0.435
    rows = []
    for book, prices in {
        "Pinnacle": (2.20, 3.40, 3.30),
        "SoftBook": (2.30, 3.35, 3.20),
    }.items():
        for sel, odds in zip(("Home FC", "Draw", "Away FC"), prices, strict=True):
            rows.append(
                OddsSnapshotIn(
                    event_id=event_id,
                    bookmaker=book,
                    market=Market.H2H,
                    selection=sel,
                    decimal_odds=odds,
                    captured_at=NOW,
                    ingested_at=NOW,
                )
            )
    return rows


def make_pick(event_id: str) -> PickOut:
    return PickOut(
        pick_id="p-clv",
        sport="soccer",
        league="test-league-clv",
        event="Home FC vs Away FC",
        event_id=event_id,
        market=Market.H2H,
        selection="Home FC",
        bookmaker="SoftBook",
        decimal_odds=2.50,  # we got 2.50; close fair will be shorter -> +CLV
        model_probability=0.45,
        fair_probability=0.40,
        edge=0.05,
        ev=0.125,
        confidence=0.9,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary="clv true-up test",
        created_at=NOW,
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


async def test_true_up_fills_clv_fields(factory) -> None:  # type: ignore[no-untyped-def]
    event_id = "evt-clv-trueup"
    async with factory() as session:
        await persist_pick(
            session,
            make_pick(event_id),
            EventTeams(home="Home FC", away="Away FC"),
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.commit()

    loader = FakeLoader(closing_snapshots(event_id))
    updated = await true_up_clv(loader, factory, ["soccer"])
    assert updated == 1

    async with factory() as session:
        pick = await session.scalar(select(Pick).where(Pick.reason_summary == "clv true-up test"))
        assert pick is not None
        assert pick.closing_fair_probability is not None
        assert pick.clv_log is not None
        # fill 2.50 vs close fair ~0.435 -> clv_log = ln(2.50*0.435) > 0
        assert float(pick.clv_log) > 0
        assert pick.beat_close is True


async def test_true_up_ignores_unmatched_events(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session:
        await persist_pick(
            session,
            make_pick("evt-clv-unmatched"),
            EventTeams(home="Home FC", away="Away FC"),
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.commit()
    loader = FakeLoader(closing_snapshots("a-DIFFERENT-event"))
    updated = await true_up_clv(loader, factory, ["soccer"])
    assert updated == 0


async def test_true_up_revalidates_current_odds_and_edge(factory) -> None:  # type: ignore[no-untyped-def]
    # Every refresh must answer "is this pick still worth betting NOW?":
    # current price at the pick's own book + edge vs the fresh fair prob.
    event_id = "evt-revalidate"
    async with factory() as session:
        await persist_pick(
            session,
            make_pick(event_id),
            EventTeams(home="Home FC", away="Away FC"),
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.commit()

    loader = FakeLoader(closing_snapshots(event_id))
    assert await true_up_clv(loader, factory, ["soccer"]) == 1

    async with factory() as session:
        pick = await session.scalar(select(Pick).where(Pick.reason_summary == "clv true-up test"))
        assert pick is not None
        # SoftBook (the pick's book) quotes Home FC at 2.30 in the fresh scrape
        assert pick.current_odds == Decimal("2.3000")
        assert pick.revalidated_at is not None
        assert pick.current_edge is not None
        fair = float(pick.closing_fair_probability)
        assert float(pick.current_edge) == pytest.approx(fair - 1.0 / 2.30, abs=1e-4)


async def test_offwindow_open_picks_revalidated_via_match_links(factory) -> None:  # type: ignore[no-untyped-def]
    # A pick taken weeks ahead is OUTSIDE the dated scrape window; its match
    # page must be scraped directly so "still worth betting?" stays fresh.
    from datetime import timedelta

    from app.clv_trueup import revalidate_offwindow_picks

    event_id = "https://www.oddsportal.com/football/world/world-cup/far-vs-future/ZZ1/"
    async with factory() as session:
        # the dev warehouse may hold real open picks; pause them inside this
        # rolled-back transaction so only the seeded pick is off-window
        from sqlalchemy import update as sa_update

        await session.execute(
            sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
        )
        await persist_pick(
            session,
            make_pick(event_id),
            EventTeams(
                home="Home FC", away="Away FC", starts_at=NOW + timedelta(days=12)
            ),
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.commit()

    class LinkLoader(FakeLoader):
        def __init__(self, snapshots) -> None:  # type: ignore[no-untyped-def]
            super().__init__(snapshots)
            self.links_requested: list[str] = []

        async def fetch_match_odds(self, sport_key, match_links):  # type: ignore[no-untyped-def]
            self.links_requested = list(match_links)
            return self._snapshots

    loader = LinkLoader(closing_snapshots(event_id))
    updated = await revalidate_offwindow_picks(
        loader, factory, "soccer", covered_event_ids=set()
    )
    assert updated == 1
    assert loader.links_requested == [event_id]

    async with factory() as session:
        pick = await session.scalar(select(Pick).where(Pick.reason_summary == "clv true-up test"))
        assert pick is not None
        assert pick.current_odds is not None
        assert pick.revalidated_at is not None


async def test_offwindow_skips_picks_already_covered_by_cycle(factory) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from app.clv_trueup import revalidate_offwindow_picks

    event_id = "https://www.oddsportal.com/football/world/world-cup/covered-vs-game/ZZ2/"
    async with factory() as session:
        from sqlalchemy import update as sa_update

        await session.execute(
            sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
        )
        await persist_pick(
            session,
            make_pick(event_id),
            EventTeams(home="Home FC", away="Away FC", starts_at=NOW + timedelta(days=12)),
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.commit()

    class NoScrapeLoader(FakeLoader):
        async def fetch_match_odds(self, sport_key, match_links):  # type: ignore[no-untyped-def]
            raise AssertionError("covered events must not be re-scraped")

    updated = await revalidate_offwindow_picks(
        NoScrapeLoader([]), factory, "soccer", covered_event_ids={event_id}
    )
    assert updated == 0
