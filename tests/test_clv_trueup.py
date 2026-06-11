"""CLV true-up: open picks get closing-fair/clv_log refreshed from fresh odds.

Uses the compose Postgres with savepoint isolation (skips when absent).
"""

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clv_trueup import OFFWINDOW_LINK_CAP, revalidate_offwindow_picks, true_up_clv
from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Event, Pick
from app.storage.repositories import persist_pick

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai"
NOW = datetime.now(tz=UTC)


class FakeLoader:
    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        self._snapshots = snapshots

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        return self._snapshots


def closing_snapshots(
    event_id: str,
    selections: tuple[str, str, str] = ("Home FC", "Draw", "Away FC"),
) -> list[OddsSnapshotIn]:
    # Pinnacle close: 2.20 / 3.40 / 3.30 -> devigged fair for Home ~ 0.435
    rows = []
    for book, prices in {
        "Pinnacle": (2.20, 3.40, 3.30),
        "SoftBook": (2.30, 3.35, 3.20),
    }.items():
        for sel, odds in zip(selections, prices, strict=True):
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


def make_pick(event_id: str, bookmaker: str = "SoftBook") -> PickOut:
    return PickOut(
        pick_id="p-clv",
        sport="soccer",
        league="test-league-clv",
        event="Home FC vs Away FC",
        event_id=event_id,
        market=Market.H2H,
        selection="Home FC",
        bookmaker=bookmaker,
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


def test_offwindow_links_filter_sport_before_cap() -> None:
    # H3 regression: wrong-sport refs must not burn cap slots — a basketball
    # backlog bigger than the cap used to starve football picks forever.
    from app.clv_trueup import select_offwindow_links

    basketball = [f"https://www.oddsportal.com/basketball/usa/m{i}" for i in range(30)]
    football = ["https://www.oddsportal.com/football/world/target-match"]
    links = select_offwindow_links(basketball + football, "football", set(), cap=25)
    assert links == football


def test_offwindow_links_respect_cap_covered_and_order() -> None:
    from app.clv_trueup import select_offwindow_links

    refs = [f"https://www.oddsportal.com/football/world/m{i}" for i in range(30)]
    links = select_offwindow_links(refs, "football", {refs[0]}, cap=5)
    assert len(links) == 5
    assert refs[0] not in links  # covered by the cycle scrape
    assert links[0] == refs[1]  # stalest-first query order preserved
    assert select_offwindow_links(["evt-not-a-url"], "football", set(), cap=5) == []


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
            EventTeams(home="Home FC", away="Away FC", starts_at=NOW + timedelta(days=12)),
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
    updated = await revalidate_offwindow_picks(loader, factory, "soccer", covered_event_ids=set())
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


# ---------------------------------------------------------------------------
# Round-robin starvation (attempted-vs-revalidated), commission-netted CLV,
# and the match-link host allowlist — review findings 2026-06-11.
# ---------------------------------------------------------------------------


class RecordingLoader(FakeLoader):
    """fetch_match_odds records the links it was asked to scrape."""

    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        super().__init__(snapshots)
        self.links_requested: list[str] = []

    async def fetch_match_odds(self, sport_key, match_links):  # type: ignore[no-untyped-def]
        self.links_requested = list(match_links)
        return self._snapshots


async def test_offwindow_includes_unknown_kickoff_events(factory) -> None:  # type: ignore[no-untyped-def]
    # Events whose kickoff the source never reported store starts_at=NULL.
    # NULL must NOT silently drop them from off-window revalidation (SQL
    # "NULL > now" is unknown -> row filtered): we cannot prove the game
    # started, so keep re-pricing — the attempts round-robin rotates links
    # that stop pricing to the back of the queue anyway.
    from app.clv_trueup import revalidate_offwindow_picks

    event_id = "https://www.oddsportal.com/football/world/world-cup/tbd-vs-unknown/ZZ3/"
    async with factory() as session:
        await session.execute(
            sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
        )
        await persist_pick(
            session,
            make_pick(event_id),
            EventTeams(home="Home FC", away="Away FC"),  # no starts_at -> NULL
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.commit()

    loader = RecordingLoader(closing_snapshots(event_id))
    updated = await revalidate_offwindow_picks(loader, factory, "soccer", covered_event_ids=set())
    assert updated == 1
    assert loader.links_requested == [event_id]


async def test_offwindow_excludes_stale_null_kickoff_picks(factory) -> None:  # type: ignore[no-untyped-def]
    # TBD events are re-priced for 14 days from pick creation; after that the
    # settlement engine voids them (void_stale_null_kickoff_picks) — the
    # off-window selector must stop burning scrape slots on them meanwhile.
    from app.clv_trueup import revalidate_offwindow_picks
    from app.settlement.engine import STALE_NULL_KICKOFF_AGE

    event_id = "https://www.oddsportal.com/football/world/world-cup/stale-tbd/ZZ4/"
    async with factory() as session:
        await session.execute(
            sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
        )
        await persist_pick(
            session,
            make_pick(event_id),
            EventTeams(home="Home FC", away="Away FC"),  # no starts_at -> NULL
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.execute(
            sa_update(Pick)
            .where(Pick.event_id.in_(select(Event.id).where(Event.external_ref == event_id)))
            .values(created_at=NOW - STALE_NULL_KICKOFF_AGE - timedelta(days=1))
        )
        await session.commit()

    loader = RecordingLoader(closing_snapshots(event_id))
    updated = await revalidate_offwindow_picks(loader, factory, "soccer", covered_event_ids=set())
    assert updated == 0
    assert loader.links_requested == []  # never selected, never scraped


async def _seed_offwindow_pick(
    session: AsyncSession, ref: str, attempted_at: datetime | None
) -> None:
    await persist_pick(
        session,
        make_pick(ref),
        EventTeams(home="Home FC", away="Away FC", starts_at=NOW + timedelta(days=12)),
        "value-sharp-vs-soft",
        "v2-test",
    )
    if attempted_at is not None:
        await session.execute(
            sa_update(Pick)
            .where(Pick.event_id.in_(select(Event.id).where(Event.external_ref == ref)))
            .values(revalidation_attempted_at=attempted_at)
        )


def test_offwindow_links_reject_non_oddsportal_hosts() -> None:
    # SSRF guard: external_ref drives a headless browser — only oddsportal
    # match pages may survive selection, whatever the ref claims to be.
    from app.clv_trueup import select_offwindow_links

    evil = [
        "https://evil.com/football/world/x",
        "https://oddsportal.com.evil.com/football/world/x",  # suffix trick
        "https://www.oddsportal.com@evil.com/football/world/x",  # userinfo trick
        "http://localhost/football/world/x",
        "file:///etc/passwd",
    ]
    good = [
        "https://www.oddsportal.com/football/world/m1",
        "https://oddsportal.com/football/world/m2",
    ]
    assert select_offwindow_links(evil + good, "football", set()) == good


def test_offwindow_links_reject_parser_differential_bypasses() -> None:
    # urllib/Chromium differential: browsers treat '\' as '/' (WHATWG), so
    # "host\@evil.com" parses HERE as oddsportal.com but NAVIGATES to
    # evil.com. Also: userinfo, scheme downgrade, odd/garbage ports and
    # whitespace must all die before parsing; mixed-case host is case-folded
    # and allowed.
    from app.clv_trueup import select_offwindow_links

    evil = [
        "https://www.oddsportal.com\\@evil.com/x/football/y",  # backslash trick
        # THE bypass shape: urllib parses host www.oddsportal.com (passes a
        # parser-only allowlist) while the browser, treating '\' as '/',
        # navigates to evil.com.
        "https://evil.com\\@www.oddsportal.com/football/world/x",
        "https://www.oddsportal.com\\evil.com/football/world/x",
        "https://user@www.oddsportal.com/football/world/x",  # userinfo
        "https://user:pw@www.oddsportal.com/football/world/x",
        "http://www.oddsportal.com/football/world/x",  # not https
        "https://www.oddsportal.com:8443/football/world/x",  # non-443 port
        "https://www.oddsportal.com:80x/football/world/x",  # garbage port
        "https://www.oddsportal.com/football/world/x y",  # whitespace
        "https://www.oddsportal.com/football/world/x\ty",
    ]
    good = [
        "https://WWW.OddsPortal.COM/football/world/m1",  # case-folds to allowed
        "https://www.oddsportal.com:443/football/world/m2",  # explicit https port
    ]
    assert select_offwindow_links(evil + good, "football", set()) == good


async def test_offwindow_rotation_nulls_first_then_oldest_attempt(factory) -> None:  # type: ignore[no-untyped-def]
    # Round-robin must key on ATTEMPTS, not successes: never-attempted picks
    # lead, then stalest attempt; whoever missed the cap goes first next cycle.
    cap = OFFWINDOW_LINK_CAP
    null_refs = [f"https://www.oddsportal.com/football/world/rot-null-{i}/N{i}/" for i in range(5)]
    old_refs = [
        f"https://www.oddsportal.com/football/world/rot-old-{i}/O{i}/" for i in range(cap - 5)
    ]
    fresh_ref = "https://www.oddsportal.com/football/world/rot-fresh/F1/"  # cap+1th

    async with factory() as session:
        await session.execute(
            sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
        )
        for ref in null_refs:
            await _seed_offwindow_pick(session, ref, None)
        for i, ref in enumerate(old_refs):
            await _seed_offwindow_pick(
                session, ref, NOW - timedelta(days=10) + timedelta(minutes=i)
            )
        await _seed_offwindow_pick(session, fresh_ref, NOW - timedelta(hours=1))
        await session.commit()

    loader = RecordingLoader([])  # pages fetch but price NOTHING (dead links)
    assert await revalidate_offwindow_picks(loader, factory, "soccer", covered_event_ids=set()) == 0
    assert len(loader.links_requested) == cap
    assert set(loader.links_requested[:5]) == set(null_refs)  # NULLs first
    assert loader.links_requested[5:] == old_refs  # then oldest attempt first
    assert fresh_ref not in loader.links_requested  # cap+1th waits its turn

    # Second cycle: the 25 attempted (even though none priced) rotate to the
    # back; the previously uncovered pick now leads. Pre-fix, dead links kept
    # NULL revalidated_at forever and starved everything behind them.
    loader2 = RecordingLoader([])
    await revalidate_offwindow_picks(loader2, factory, "soccer", covered_event_ids=set())
    assert loader2.links_requested[0] == fresh_ref


@pytest.mark.parametrize("gap", ["wholesale-empty", "per-market"])
async def test_attempted_unpriced_pick_advances_attempt_clock_only(factory, gap) -> None:  # type: ignore[no-untyped-def]
    # Starvation regression: an attempted-but-unpriced event must advance
    # revalidation_attempted_at (rotate to the back) while revalidated_at
    # stays NULL — the dashboard "verified" badge is success-only.
    event_id = f"https://www.oddsportal.com/football/world/dead-{gap}/XX9/"
    async with factory() as session:
        await session.execute(
            sa_update(Pick).where(Pick.status == "alerted").values(status="paused-for-test")
        )
        await _seed_offwindow_pick(session, event_id, None)
        await session.commit()

    snaps = (
        []
        if gap == "wholesale-empty"
        # page priced fine — but for OTHER selections (pick's market dropped)
        else closing_snapshots(event_id, selections=("Foo FC", "Draw", "Bar FC"))
    )
    loader = RecordingLoader(snaps)
    updated = await revalidate_offwindow_picks(loader, factory, "soccer", covered_event_ids=set())
    assert updated == 0
    assert loader.links_requested == [event_id]

    async with factory() as session:
        pick = await session.scalar(select(Pick).where(Pick.reason_summary == "clv true-up test"))
        assert pick is not None
        assert pick.revalidation_attempted_at is not None  # attempt recorded
        assert pick.revalidated_at is None  # never re-priced -> not "verified"


async def test_fallback_book_selected_by_effective_odds(factory) -> None:  # type: ignore[no-untyped-def]
    # The pick's book dropped the market. The "best remaining book" fallback
    # must pick the best EFFECTIVE (commission-netted) price — selection and
    # valuation must agree, like pick-time math in app/edge/value.py.
    event_id = "evt-effective-fallback"
    async with factory() as session:
        await persist_pick(
            session,
            make_pick(event_id, bookmaker="GoneBook"),
            EventTeams(home="Home FC", away="Away FC"),
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.commit()

    snaps = closing_snapshots(event_id)
    # Raw 2.35 beats SoftBook's raw 2.30, but 5% commission nets it to
    # 1 + 1.35*0.95 = 2.2825 < 2.30 -> SoftBook must win the fallback.
    snaps.append(
        OddsSnapshotIn(
            event_id=event_id,
            bookmaker="Betfair Exchange",
            market=Market.H2H,
            selection="Home FC",
            decimal_odds=2.35,
            captured_at=NOW,
            ingested_at=NOW,
        )
    )
    assert await true_up_clv(FakeLoader(snaps), factory, ["soccer"]) == 1

    async with factory() as session:
        pick = await session.scalar(select(Pick).where(Pick.reason_summary == "clv true-up test"))
        assert pick is not None
        assert pick.current_odds == Decimal("2.3000")  # NOT betfair's raw 2.35
        fair = float(pick.closing_fair_probability)
        assert float(pick.current_edge) == pytest.approx(fair - 1.0 / 2.30, abs=1e-4)


async def test_clv_log_uses_effective_fill_odds_for_exchange_picks(factory) -> None:  # type: ignore[no-untyped-def]
    # CLV on the GROSS exchange fill inflates every exchange pick by
    # ln(1/(1-c)); fill must be netted like pick-time edges/EV are.
    event_id = "evt-exchange-clv"
    async with factory() as session:
        await persist_pick(
            session,
            make_pick(event_id, bookmaker="betfair exchange"),  # fill 2.50 gross
            EventTeams(home="Home FC", away="Away FC"),
            "value-sharp-vs-soft",
            "v2-test",
        )
        await session.commit()

    assert await true_up_clv(FakeLoader(closing_snapshots(event_id)), factory, ["soccer"]) == 1

    async with factory() as session:
        pick = await session.scalar(select(Pick).where(Pick.reason_summary == "clv true-up test"))
        assert pick is not None
        fair = float(pick.closing_fair_probability)
        eff_fill = 1.0 + (2.50 - 1.0) * (1.0 - 0.05)  # 2.425 net of 5%
        assert float(pick.clv_log) == pytest.approx(math.log(eff_fill * fair), abs=1e-4)
        # and explicitly NOT the gross-fill value (gap is ln(2.5/2.425)~0.03)
        assert abs(float(pick.clv_log) - math.log(2.50 * fair)) > 0.01
        assert pick.beat_close is (float(pick.clv_log) > 0)
