"""League-derived marker veto — the NBL1 double-header wrong-game class.

Audit 2026-07-11 (docs/research/2026-07-11-arcadia-match-audit.md): 9/292
usable Pinnacle-archive matches were WRONG-GAME, all one class — Australian
NBL1 men-plus-women double-headers where arcadia lists the WOMEN'S fixture
under marker-less club names identical to the men's, and the women marker
lives ONLY in the arcadia league label ("Australia - NBL1 Women"). The team
name marker veto is structurally blind (neither compared name carries a
marker) and a 105-120 min kickoff drift is inside the 6h accept bound.

Fix under test (TIGHTENING-ONLY — may only ever REFUSE matches that used to
succeed, never accept anything new):

1. ``resolve_pinnacle_close_snaps`` derives distinguishing markers from the
   matched arcadia event's LEAGUE label (reusing matching.py's existing marker
   vocabulary) and REFUSES the close when the pick's team names carry no
   matching marker (non-tennis only — tennis fixtures are person-named, so the
   double-header identical-names class cannot exist there).
2. The warehouse mint-time dedup resolvers (Tier-1 exact-team-id / Tier-2
   fixture_pair_key) refuse to MERGE two events whose league labels disagree
   on markers, so a double-header's two games mint separate event rows
   instead of one contaminated row carrying both games' markets.

Ordinal exclusion: division-numbered league labels ("Bundesliga 2", "Serie B",
"Liga II") are SENIOR competitions — the league-marker derivation must never
treat the trailing-ordinal/roman reserve rules (correct for TEAM names) as
league markers.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.storage.models import Event
from app.storage.repositories import (
    _league_marker_set,
    persist_odds_snapshots,
    resolve_pinnacle_close_snaps,
    resolver_quarantine_stats,
)

# Same compose test DB as tests/test_resolution_db.py; overridable so the
# suite can also run from an environment where the compose port mapping is
# not on localhost (e.g. a sibling container reaching postgres directly).
DB_URL = os.environ.get(
    "BETTING_AI_TEST_DB_URL",
    "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test",
)
# The women's tip of the double-header; the men's game tips 2h later (the
# audit class showed 105-120 min gaps).
KO_W = datetime(2026, 12, 3, 8, 0, tzinfo=UTC)
KO_M = KO_W + timedelta(hours=2)


# --------------------------------------------------------------------------- #
# Pure: _league_marker_set derives women/youth/reserve markers from a LEAGUE
# label, deliberately excluding the positional ordinal-reserve rules.
# --------------------------------------------------------------------------- #
def test_league_marker_set_women_league_labels() -> None:
    assert _league_marker_set("Australia - NBL1 Women") == frozenset({"women"})
    assert _league_marker_set("England - FA Women's Super League") == frozenset({"women"})
    assert _league_marker_set("Germany - Frauen Bundesliga") == frozenset({"women"})


def test_league_marker_set_youth_and_reserve_word_labels() -> None:
    assert _league_marker_set("UEFA Youth League") == frozenset({"youth"})
    assert _league_marker_set("Argentina - U20 Liga") == frozenset({"youth"})
    assert _league_marker_set("Australia - NPL Victoria Reserves") == frozenset({"reserve"})


def test_league_marker_set_division_numbered_leagues_are_not_reserve() -> None:
    # The TEAM-name reserve rules (trailing "2"/"3"/"b", roman "ii"/"iii") are
    # WRONG for league labels: second divisions are senior competitions. A
    # false "reserve" here would veto every correct match in these leagues.
    assert _league_marker_set("Germany - Bundesliga 2") == frozenset()
    assert _league_marker_set("Italy - Serie B") == frozenset()
    assert _league_marker_set("Romania - Liga II") == frozenset()
    assert _league_marker_set("England - League 2") == frozenset()


def test_league_marker_set_empty_and_namespace_defaults() -> None:
    assert _league_marker_set(None) == frozenset()
    assert _league_marker_set("") == frozenset()
    assert _league_marker_set("pinnacle_basketball") == frozenset()
    assert _league_marker_set("Australia - NBL1 South") == frozenset()


# --------------------------------------------------------------------------- #
# DB integration (compose Postgres; skip absent) — mirrors test_resolution_db
# --------------------------------------------------------------------------- #
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


def _h2h_snap(selection: str, odds: float, event: str, captured: datetime) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id=event,
        bookmaker="Pinnacle",
        market=Market.H2H,
        selection=selection,
        decimal_odds=odds,
        captured_at=captured,
        ingested_at=captured,
    )


async def _seed_arcadia_basketball(  # type: ignore[no-untyped-def]
    factory,
    ref: str,
    home: str,
    away: str,
    league: str,
    kickoff: datetime,
    *,
    home_odds: float = 2.10,
    away_odds: float = 1.75,
) -> None:
    captured = kickoff - timedelta(minutes=30)
    snaps = [
        _h2h_snap(home, home_odds, ref, captured),
        _h2h_snap(away, away_odds, ref, captured),
    ]
    teams = {ref: EventTeams(home=home, away=away, league=league, starts_at=kickoff)}
    await persist_odds_snapshots(
        factory, snaps, teams, "pinnacle_basketball", "pinnacle_basketball"
    )


# ---- Part 1: the consume-side league-marker veto ---------------------------- #


async def test_resolver_refuses_womens_league_close_for_markerless_pick(factory) -> None:  # type: ignore[no-untyped-def]
    # THE AUDIT CLASS (9/292 wrong): arcadia lists the WOMEN'S NBL1 fixture under
    # marker-less club names identical to the men's; the women marker lives ONLY
    # in the league label; kickoff drift 120 min is inside the 6h accept bound.
    # The men's (marker-less) pick MUST NOT attach this close — fake CLV.
    await _seed_arcadia_basketball(
        factory,
        "arc-nbl1-w",
        "Ringwood Hawks",
        "Kilsyth Cobras",
        "Australia - NBL1 Women",
        KO_W,
    )
    before = resolver_quarantine_stats()
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_basketball",
            pick_external_ref="evt-mens-pick",
            home="Ringwood Hawks",
            away="Kilsyth Cobras",
            kickoff=KO_M,  # the men's tip, 120 min after the women's
        )
    assert out == []  # league carries {women}; the pick side does not -> REFUSED
    # Monitor-only quarantine counter (rides /health): the refusal increments
    # ITS counter and only its counter.
    after = resolver_quarantine_stats()
    assert after["marker_veto"] == before["marker_veto"] + 1  # type: ignore[operator]
    assert after["same_pair_ambiguity"] == before["same_pair_ambiguity"]


async def test_resolver_womens_league_close_still_attaches_for_women_marked_pick(factory) -> None:  # type: ignore[no-untyped-def]
    # ONE-SIDEDNESS of the veto: when the pick side DOES carry the marker the
    # league carries, the league veto must NOT fire — a women's pick against a
    # women-marked archive event in a women's league keeps its close.
    await _seed_arcadia_basketball(
        factory,
        "arc-nbl1-w-marked",
        "Ringwood Hawks W",
        "Kilsyth Cobras W",
        "Australia - NBL1 Women",
        KO_W,
    )
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_basketball",
            pick_external_ref="evt-womens-pick",
            home="Ringwood Hawks W",
            away="Kilsyth Cobras W",
            kickoff=KO_W,
        )
    by_sel = {s.selection: s for s in out}
    assert set(by_sel) == {"Ringwood Hawks W", "Kilsyth Cobras W"}
    assert by_sel["Ringwood Hawks W"].decimal_odds == pytest.approx(2.10)


async def test_resolver_mens_league_close_unaffected(factory) -> None:  # type: ignore[no-untyped-def]
    # TIGHTENING-ONLY regression: a normal marker-less league must keep matching
    # exactly as before.
    await _seed_arcadia_basketball(
        factory,
        "arc-nbl1-m",
        "Hobart Chargers",
        "Melbourne Tigers",
        "Australia - NBL1 South",
        KO_M,
    )
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_basketball",
            pick_external_ref="evt-mens-pick",
            home="Hobart Chargers",
            away="Melbourne Tigers",
            kickoff=KO_M,
        )
    by_sel = {s.selection: s for s in out}
    assert set(by_sel) == {"Hobart Chargers", "Melbourne Tigers"}


async def test_resolver_division_numbered_league_is_not_a_reserve_veto(factory) -> None:  # type: ignore[no-untyped-def]
    # "Bundesliga 2" must NOT derive a {reserve} league marker (senior second
    # division) — a false veto here would kill every correct match in the league.
    await _seed_arcadia_basketball(
        factory,
        "arc-bl2",
        "Alpha Club",
        "Beta Club",
        "Germany - Bundesliga 2",
        KO_M,
    )
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_basketball",
            pick_external_ref="evt-bl2-pick",
            home="Alpha Club",
            away="Beta Club",
            kickoff=KO_M,
        )
    assert out  # still matched — no false reserve veto from the league label


async def test_resolver_tennis_exempt_from_league_marker_veto(factory) -> None:  # type: ignore[no-untyped-def]
    # Tennis fixtures are PERSON-named: a women's player cannot have a
    # marker-less men's twin, so the double-header class cannot exist and the
    # league-label veto must not fire (women's-tour labels would otherwise
    # veto every correct women's tennis close — pure recall loss).
    captured = KO_M - timedelta(minutes=30)
    ref = "arc-itf-w"
    snaps = [
        _h2h_snap("Ann Li", 1.80, ref, captured),
        _h2h_snap("Iga Swiatek", 2.05, ref, captured),
    ]
    teams = {
        ref: EventTeams(
            home="Ann Li", away="Iga Swiatek", league="ITF Women - Wimbledon", starts_at=KO_M
        )
    }
    await persist_odds_snapshots(factory, snaps, teams, "pinnacle_tennis", "pinnacle_tennis")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_tennis",
            pick_external_ref="evt-tennis-pick",
            home="Li A.",
            away="Swiatek I.",
            kickoff=KO_M,
        )
    by_sel = {s.selection: s for s in out}
    assert set(by_sel) == {"Li A.", "Swiatek I."}


# ---- Part 2: mint-time dedup must not merge the double-header --------------- #


async def test_capture_double_header_mints_separate_events(factory) -> None:  # type: ignore[no-untyped-def]
    # THE CAPTURE CONTAMINATION (audit evidence #4): arcadia lists the women's
    # and men's games as SEPARATE matchups with IDENTICAL club names, 2h apart.
    # Tier-1 (exact team-id, +/-2h) used to MERGE the second matchup into the
    # first event row, producing ONE event ref carrying BOTH games' markets
    # (two totals clusters 30+ points apart, h2h updating past kickoff). The
    # league-marker split must mint two separate event rows.
    await _seed_arcadia_basketball(
        factory,
        "arc-dh-women",
        "Ringwood Hawks",
        "Kilsyth Cobras",
        "Australia - NBL1 Women",
        KO_W,
        home_odds=1.30,
        away_odds=3.40,
    )
    await _seed_arcadia_basketball(
        factory,
        "arc-dh-men",
        "Ringwood Hawks",
        "Kilsyth Cobras",
        "Australia - NBL1 South",
        KO_M,
        home_odds=2.10,
        away_odds=1.75,
    )
    async with factory() as session:
        rows = (
            await session.execute(
                select(Event.external_ref, Event.starts_at).where(
                    Event.external_ref.in_(["arc-dh-women", "arc-dh-men"])
                )
            )
        ).all()
    by_ref = {ref: ko for ref, ko in rows}
    assert set(by_ref) == {"arc-dh-women", "arc-dh-men"}  # NOT merged into one row
    assert by_ref["arc-dh-women"].replace(tzinfo=UTC) == KO_W
    assert by_ref["arc-dh-men"].replace(tzinfo=UTC) == KO_M


async def test_capture_same_league_duplicates_still_merge(factory) -> None:  # type: ignore[no-untyped-def]
    # REGRESSION: the mint-time dedup (PR1a) must keep collapsing genuine
    # duplicate captures of ONE fixture — same teams, same (marker-less)
    # league vocabulary, kickoffs 30 min apart -> ONE canonical event row.
    await _seed_arcadia_basketball(
        factory,
        "arc-dup-a",
        "Alpha Club",
        "Beta Club",
        "Australia - NBL1 South",
        KO_M,
    )
    await _seed_arcadia_basketball(
        factory,
        "arc-dup-b",
        "Alpha Club",
        "Beta Club",
        "Australia - NBL1 South",
        KO_M + timedelta(minutes=30),
    )
    async with factory() as session:
        rows = (
            await session.execute(
                select(Event.external_ref).where(Event.external_ref.in_(["arc-dup-a", "arc-dup-b"]))
            )
        ).all()
    # the second capture resolved onto the first row; no second event minted
    assert [r[0] for r in rows] == ["arc-dup-a"]


# ---- Part 3: out-of-vocabulary double-header AMBIGUITY refusal --------------- #
# The league-marker veto only catches labels whose women/youth/reserve token is
# in matching.py's vocabulary. A same-club-pair double-header under an UNMARKED
# / out-of-vocabulary arcadia label (e.g. "NBL1 Girls" — "girls" is not a
# vocabulary token) is invisible to it. Hardening 2026-07-11 (TIGHTENING-ONLY):
# when MORE THAN ONE distinct arcadia event with the same normalized club pair
# sits inside the matcher's 6h ACCEPT window and the siblings are NOT
# marker-distinguished from each other, the attachment is REFUSED (fail-closed)
# — a coin-flip close is fake CLV. Single candidates and marker-distinguished
# siblings keep today's behavior exactly.

# 3h apart: outside the 2h mint-dedup tolerance (two event rows mint) but
# inside the 6h accept bound (both attachable -> ambiguous).
KO_B = KO_W + timedelta(hours=3)


async def test_resolver_refuses_unmarked_same_pair_ambiguity(factory) -> None:  # type: ignore[no-untyped-def]
    # OUT-OF-VOCABULARY double-header: "Australia - NBL1 Girls" carries NO
    # vocabulary marker (frozenset()) — exactly like the men's "NBL1 South" —
    # so the league-marker veto is blind. Two same-pair events inside the 6h
    # accept window, not marker-distinguished from each other -> REFUSE.
    await _seed_arcadia_basketball(
        factory,
        "arc-oov-girls",
        "Frankston Blues",
        "Sandringham Sabres",
        "Australia - NBL1 Girls",  # out-of-vocabulary label: derives NO marker
        KO_W,
        home_odds=1.30,
        away_odds=3.40,
    )
    await _seed_arcadia_basketball(
        factory,
        "arc-oov-mens",
        "Frankston Blues",
        "Sandringham Sabres",
        "Australia - NBL1 South",
        KO_B,
        home_odds=2.10,
        away_odds=1.75,
    )
    before = resolver_quarantine_stats()
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_basketball",
            pick_external_ref="evt-oov-pick",
            home="Frankston Blues",
            away="Sandringham Sabres",
            kickoff=KO_B,
        )
    # nearest-collapse would pick the men's event — but the sibling is NOT
    # marker-distinguished, so which game the pick belongs to is a coin flip.
    assert out == []
    # Monitor-only quarantine counter (rides /health): the refusal increments
    # ITS counter and only its counter.
    after = resolver_quarantine_stats()
    assert after["same_pair_ambiguity"] == before["same_pair_ambiguity"] + 1  # type: ignore[operator]
    assert after["marker_veto"] == before["marker_veto"]


async def test_resolver_same_pair_rematch_outside_accept_window_still_attaches(factory) -> None:  # type: ignore[no-untyped-def]
    # TIGHTENING-ONLY: a genuine series rematch 2 days earlier (same clubs,
    # same league) sits inside the ±2-day candidate-FETCH window but OUTSIDE
    # the 6h accept window — only one event is attachable, so there is no
    # ambiguity and the close keeps attaching exactly as before.
    await _seed_arcadia_basketball(
        factory,
        "arc-series-g1",
        "Geelong Supercats",
        "Ballarat Miners",
        "Australia - NBL1 South",
        KO_M - timedelta(days=2),
        home_odds=1.50,
        away_odds=2.60,
    )
    await _seed_arcadia_basketball(
        factory,
        "arc-series-g2",
        "Geelong Supercats",
        "Ballarat Miners",
        "Australia - NBL1 South",
        KO_M,
        home_odds=2.10,
        away_odds=1.75,
    )
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_basketball",
            pick_external_ref="evt-series-pick",
            home="Geelong Supercats",
            away="Ballarat Miners",
            kickoff=KO_M,
        )
    by_sel = {s.selection: s for s in out}
    assert set(by_sel) == {"Geelong Supercats", "Ballarat Miners"}
    # game 2's close (2.10/1.75), never game 1's
    assert by_sel["Geelong Supercats"].decimal_odds == pytest.approx(2.10)
    assert by_sel["Ballarat Miners"].decimal_odds == pytest.approx(1.75)


async def test_resolver_attaches_mens_close_from_split_double_header(factory) -> None:  # type: ignore[no-untyped-def]
    # END-TO-END after the split: with BOTH double-header events present, the
    # men's pick collapses to the NEAREST candidate (its own men's event, delta
    # 0) and attaches the MEN'S close — not the women's — and the league veto
    # stays silent on the marker-less men's league.
    await _seed_arcadia_basketball(
        factory,
        "arc-dh2-women",
        "Willetton Tigers",
        "Joondalup Wolves",
        "Australia - NBL1 Women",
        KO_W,
        home_odds=1.30,
        away_odds=3.40,
    )
    await _seed_arcadia_basketball(
        factory,
        "arc-dh2-men",
        "Willetton Tigers",
        "Joondalup Wolves",
        "Australia - NBL1 West",
        KO_M,
        home_odds=2.10,
        away_odds=1.75,
    )
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_basketball",
            pick_external_ref="evt-mens-pick",
            home="Willetton Tigers",
            away="Joondalup Wolves",
            kickoff=KO_M,
        )
    by_sel = {s.selection: s for s in out}
    assert set(by_sel) == {"Willetton Tigers", "Joondalup Wolves"}
    # the MEN'S odds (2.10/1.75), never the women's (1.30/3.40)
    assert by_sel["Willetton Tigers"].decimal_odds == pytest.approx(2.10)
    assert by_sel["Joondalup Wolves"].decimal_odds == pytest.approx(1.75)
