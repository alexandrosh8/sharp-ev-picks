"""DB integration for the cross-source resolver (compose Postgres; skip absent).

resolve_pinnacle_close_snaps strict-matches a pick's fixture to its
`pinnacle_<sport>` archive event and returns that event's close re-keyed to the
pick's event_id + selection vocabulary. These tests prove the happy path
(alias + re-key), and the cardinal-sin guards at the DB layer (no match -> [],
ambiguous -> [], out-of-window -> []). No live network.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.resolution.shadow import summarize_betfair_coverage, summarize_match_rate
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.repositories import (
    _betfair_full_market_rows,
    betfair_exchange_coverage_outcomes,
    betfair_inline_capture_by_sport,
    persist_odds_snapshots,
    persist_pick,
    pinnacle_archive_capture_by_sport,
    resolve_pinnacle_close_snaps,
    shadow_match_rate_outcomes,
)

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"
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


async def test_resolver_slug_fallback_refuses_mens_close_for_womens_pick(factory) -> None:  # type: ignore[no-untyped-def]
    # WRONG-GAME GUARD: a women's pick ("Lanus W"/"Union W") whose OddsPortal URL
    # slug DROPPED the "W" must NOT borrow the men's "Lanus"/"Union" Pinnacle close
    # via the slug fallback — the men's game is a DIFFERENT fixture (fake CLV).
    await _seed_pinnacle_event(factory, "pin-lanus-union", "Lanus", "Union")
    mens_slug_url = "https://www.oddsportal.com/football/argentina/l/lanus-Ab12Cd34/union-Ef56Gh78/"
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref=mens_slug_url,
            home="Lanus W",
            away="Union W",
            kickoff=KO,
        )
    assert out == []  # slug dropped the marker -> guard refuses the men's close


# --- LIVE ANCHOR PATH now runs the precision-hardened matcher (go-live flip) ---
# resolve_pinnacle_close_snaps is the SINGLE live Pinnacle anchor matcher (the
# pick-time sharp anchor AND the settlement close both route through it). The flip
# to match_event_hardened lifts recall (a fuzzy spelling variant the exact-only
# matcher rejected now resolves) WITHOUT loosening the cardinal-sin guards: a
# reserve-vs-senior remains a REJECT (a wrong-game close = fake CLV).


async def test_resolver_matches_exotic_spelling_variant_via_hardened(factory) -> None:  # type: ignore[no-untyped-def]
    # "Bayer Leverkussen" (a double-s misspelling) is NOT in the alias seed, so the
    # exact-only matcher returned [] for it. The hardened two-tier Jaro-Winkler +
    # token-sort tier (JW 0.988 >= 0.92, token-sort 97 >= 90) now resolves it to the
    # archive "Bayer Leverkusen" and attaches its Pinnacle close — the measured
    # shadow lift, live.
    await _seed_pinnacle_event(factory, "pin-b04-svw", "Bayer Leverkusen", "Werder Bremen")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Bayer Leverkussen",  # exotic misspelling — no alias, fuzzy-only recovery
            away="Werder Bremen",
            kickoff=KO,
        )
    by_sel = {s.selection: s for s in out}
    # re-keyed to the PICK's selection vocabulary (the misspelled home spelling)
    assert set(by_sel) == {"Bayer Leverkussen", "Draw", "Werder Bremen"}
    assert all(s.event_id == "evt-pick" for s in out)
    assert all(s.bookmaker == "Pinnacle" for s in out)
    assert by_sel["Bayer Leverkussen"].decimal_odds == pytest.approx(2.10)


async def test_resolver_rejects_reserve_archive_for_senior_pick(factory) -> None:  # type: ignore[no-untyped-def]
    # WRONG-GAME GUARD (reserve veto): a SENIOR pick ("Real Madrid") must NEVER
    # borrow the RESERVE side's ("Real Madrid B" = Castilla) Pinnacle close. The
    # hardened matcher's categorical marker veto REJECTS the one-sided reserve
    # marker even though the base club name matches -> [] (no fake CLV).
    await _seed_pinnacle_event(factory, "pin-rmb-getafe", "Real Madrid B", "Getafe")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Real Madrid",  # senior side — must not attach the "B" reserve close
            away="Getafe",
            kickoff=KO,
        )
    assert out == []


async def test_resolver_rejects_senior_archive_for_reserve_pick(factory) -> None:  # type: ignore[no-untyped-def]
    # The mirror direction: a RESERVE pick ("Real Madrid B") must not borrow the
    # SENIOR "Real Madrid" close either — the marker veto is symmetric.
    await _seed_pinnacle_event(factory, "pin-rm-getafe", "Real Madrid", "Getafe")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Real Madrid B",
            away="Getafe",
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


async def _seed_pinnacle_tennis_event(factory, ref: str, home: str, away: str) -> None:  # type: ignore[no-untyped-def]
    # arcadia stores full names ("Firstname Surname"); 2-way market, no Draw.
    snaps = [_pin_snap(home, 1.80, ref), _pin_snap(away, 2.05, ref)]
    teams = {ref: EventTeams(home=home, away=away, league="atp", starts_at=KO)}
    await persist_odds_snapshots(factory, snaps, teams, "pinnacle_tennis", "pinnacle_tennis")


async def test_resolver_matches_tennis_via_name_canonicalization(factory) -> None:  # type: ignore[no-untyped-def]
    # audit #7: arcadia "Firstname Surname" vs the pick's OddsPortal "Surname I." —
    # the consume path must canonicalize + match UNORDERED, or tennis CLV never
    # attaches. (Old code passed raw names ordered=True and returned [].)
    await _seed_pinnacle_tennis_event(factory, "pin-djok-alc", "Novak Djokovic", "Carlos Alcaraz")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_tennis",
            pick_external_ref="evt-tennis-pick",
            home="Djokovic N.",
            away="Alcaraz C.",
            kickoff=KO,
        )
    by_sel = {s.selection: s for s in out}
    assert set(by_sel) == {"Djokovic N.", "Alcaraz C."}  # re-keyed to the pick vocabulary
    assert all(s.bookmaker == "Pinnacle" for s in out)
    assert by_sel["Djokovic N."].decimal_odds == pytest.approx(1.80)


async def test_resolver_tennis_does_not_attach_different_same_day_player(factory) -> None:  # type: ignore[no-untyped-def]
    # a DIFFERENT same-day fixture must never attach the wrong close (the cardinal
    # sin): the canonical names don't match -> [] (audit #7 collision safety).
    await _seed_pinnacle_tennis_event(factory, "pin-djok-alc", "Novak Djokovic", "Carlos Alcaraz")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_tennis",
            pick_external_ref="evt-tennis-pick",
            home="Nadal R.",
            away="Federer R.",
            kickoff=KO,
        )
    assert out == []


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


# --- PER-MARKET re-key: H2H re-keys by OUTCOME (team name); TOTALS/SPREADS are
# source-keyed and must pass through so a totals/spreads pick can get a Pinnacle
# anchor (funnel: ~31 picks/week). The team-name selection_map ALONE silently
# drops Over/Under + Asian-handicap selections (cardinal coverage bug). ---


def _pin_snap_mkt(
    selection: str, odds: float, event: str, market: Market, detail: str | None
) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id=event,
        bookmaker="Pinnacle",
        market=market,
        selection=selection,
        decimal_odds=odds,
        captured_at=CAPTURED,
        ingested_at=CAPTURED,
        market_detail=detail,
    )


async def _seed_pinnacle_multimarket(factory, ref: str, home: str, away: str) -> None:  # type: ignore[no-untyped-def]
    """One arcadia event carrying H2H + totals (Over/Under 2.5) + Asian-handicap
    (home -1.5 / away +1.5), in OddsPortal's EXACT selection vocabulary, plus a
    STRAY h2h selection that maps to neither side (must stay dropped)."""
    snaps = [
        # H2H (team-named outcomes)
        _pin_snap(home, 2.10, ref),
        _pin_snap("Draw", 3.40, ref),
        _pin_snap(away, 3.60, ref),
        _pin_snap("Unrelated FC", 5.00, ref),  # unknown h2h vocabulary -> must DROP
        # totals — source-independent vocabulary (line on text + market_detail)
        _pin_snap_mkt("Over 2.5", 1.95, ref, Market.TOTALS, "over_under_2_5"),
        _pin_snap_mkt("Under 2.5", 1.90, ref, Market.TOTALS, "over_under_2_5"),
        # Asian handicap — "{team} {signed}"; market_detail keyed on the HOME handicap
        _pin_snap_mkt(f"{home} -1.5", 2.05, ref, Market.SPREADS, "asian_handicap_-1_5"),
        _pin_snap_mkt(f"{away} +1.5", 1.80, ref, Market.SPREADS, "asian_handicap_-1_5"),
    ]
    teams = {ref: EventTeams(home=home, away=away, league="pin", starts_at=KO)}
    await persist_odds_snapshots(factory, snaps, teams, "pinnacle_soccer", "pinnacle_soccer")


async def test_resolver_passes_through_totals_and_ah_and_rekeys_h2h(factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_pinnacle_multimarket(factory, "pin-mu-che-multi", "Manchester United", "Chelsea")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Man Utd",  # OddsPortal-style abbreviation -> alias -> Manchester United
            away="Chelsea",
            kickoff=KO,
        )
    keyed = {(str(s.market), s.market_detail, s.selection): s for s in out}
    # every row re-keyed to the pick event; bookmaker stays Pinnacle
    assert all(s.event_id == "evt-pick" and s.bookmaker == "Pinnacle" for s in out)

    # H2H: re-keyed to the PICK's selection vocabulary (home collapses to "Man Utd")
    assert ("h2h", None, "Man Utd") in keyed
    assert ("h2h", None, "Draw") in keyed
    assert ("h2h", None, "Chelsea") in keyed
    assert keyed[("h2h", None, "Man Utd")].decimal_odds == pytest.approx(2.10)
    # unknown h2h selection still dropped (safe default preserved)
    assert all(s.selection != "Unrelated FC" for s in out)

    # TOTALS: source-independent vocabulary passes through with market+line+selection
    assert ("totals", "over_under_2_5", "Over 2.5") in keyed
    assert ("totals", "over_under_2_5", "Under 2.5") in keyed
    assert keyed[("totals", "over_under_2_5", "Over 2.5")].decimal_odds == pytest.approx(1.95)
    assert keyed[("totals", "over_under_2_5", "Under 2.5")].decimal_odds == pytest.approx(1.90)

    # SPREADS/AH: team-name re-keyed to the pick vocabulary, signed handicap + line
    # preserved; NO side swap (home -1.5 -> home, away +1.5 -> away).
    assert ("spreads", "asian_handicap_-1_5", "Man Utd -1.5") in keyed
    assert ("spreads", "asian_handicap_-1_5", "Chelsea +1.5") in keyed
    assert keyed[("spreads", "asian_handicap_-1_5", "Man Utd -1.5")].decimal_odds == pytest.approx(
        2.05
    )
    # and the AH home price never lands on the away selection (no wrong-side mapping)
    assert ("spreads", "asian_handicap_-1_5", "Chelsea -1.5") not in keyed
    assert ("spreads", "asian_handicap_-1_5", "Man Utd +1.5") not in keyed


async def test_resolver_h2h_only_output_unchanged_byte_for_byte(factory) -> None:  # type: ignore[no-untyped-def]
    """REGRESSION LOCK: an H2H-only arcadia event must re-key to EXACTLY the three
    pick outcomes and nothing else — the per-market branch must not alter the H2H
    path (CLV-shared; byte-for-byte identical to the pre-fix behavior)."""
    await _seed_pinnacle_event(factory, "pin-h2h-lock", "Manchester United", "Chelsea")
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-pick",
            home="Man Utd",
            away="Chelsea",
            kickoff=KO,
        )
    by_sel = {s.selection: s for s in out}
    assert set(by_sel) == {"Man Utd", "Draw", "Chelsea"}
    assert all(s.market == Market.H2H and s.market_detail is None for s in out)
    assert by_sel["Man Utd"].decimal_odds == pytest.approx(2.10)
    assert by_sel["Draw"].decimal_odds == pytest.approx(3.40)
    assert by_sel["Chelsea"].decimal_odds == pytest.approx(3.60)


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


# --- WRONG-GAME SAFETY-NET AUDIT over the live anchor path (read-only) ----------
_AUDIT_LEAGUE = "wrong-game-audit-epl"


def _audit_pick(event_id: str, home: str, created_at: datetime) -> PickOut:
    """A recent soccer pick for the wrong-game audit sampler (created_at within the
    audit lookback so the sampler selects it)."""
    p = _shadow_pick(event_id)
    return p.model_copy(
        update={"league": _AUDIT_LEAGUE, "selection": home, "created_at": created_at}
    )


async def test_wrong_game_audit_passes_a_correct_live_anchor(factory) -> None:  # type: ignore[no-untyped-def]
    # End-to-end over the LIVE anchor path: a pick that resolves a SAME-GAME
    # Pinnacle archive event (alias match, same kickoff) raises NO wrong_game
    # anomaly. Read-only — the audit attaches no close and writes no pick.
    from app.maintenance.wrong_game_audit import audit_live_pinnacle_anchors

    now = datetime(2027, 3, 1, 12, 0, tzinfo=UTC)
    ko = now + timedelta(hours=6)
    await _seed_event(
        factory, "audit-pin-ok", "Manchester United", "Chelsea", ko, "pinnacle_soccer"
    )
    async with factory() as session:
        await persist_pick(
            session,
            _audit_pick("audit-evt-ok", "Man Utd", now - timedelta(hours=1)),
            EventTeams(home="Man Utd", away="Chelsea", league=_AUDIT_LEAGUE, starts_at=ko),
            "value-sharp-vs-soft",
            "v3",
        )
        await session.commit()
    anomalies = await audit_live_pinnacle_anchors(factory, now)
    assert [a for a in anomalies if a.code == "wrong_game_anchor"] == []


async def test_wrong_game_audit_no_anomaly_when_no_archive_coverage(factory) -> None:  # type: ignore[no-untyped-def]
    # A pick with NO pinnacle archive event in its window resolves no anchor, so
    # the audit has nothing to verify -> clean (a coverage gap is not a wrong game).
    from app.maintenance.wrong_game_audit import audit_live_pinnacle_anchors

    now = datetime(2027, 4, 1, 12, 0, tzinfo=UTC)
    ko = now + timedelta(hours=6)
    async with factory() as session:
        await persist_pick(
            session,
            _audit_pick("audit-evt-nocov", "Solo", now - timedelta(hours=1)),
            EventTeams(home="Solo", away="Lonely", league=_AUDIT_LEAGUE, starts_at=ko),
            "value-sharp-vs-soft",
            "v3",
        )
        await session.commit()
    anomalies = await audit_live_pinnacle_anchors(factory, now)
    assert [a for a in anomalies if a.code == "wrong_game_anchor"] == []


async def test_wrong_game_audit_passes_a_correct_slug_fallback_anchor(factory) -> None:  # type: ignore[no-untyped-def]
    # COVERAGE: the live loader has a SLUG-fallback second match. A CORRECT
    # slug-accepted anchor (display name too noisy for the primary, but the clean
    # OddsPortal slug matches the SAME game) must raise NO anomaly — the audit
    # exercises the slug path without false-alarming.
    from app.maintenance.wrong_game_audit import audit_live_pinnacle_anchors

    now = datetime(2027, 5, 1, 12, 0, tzinfo=UTC)
    ko = now + timedelta(hours=6)
    slug_url = (
        "https://www.oddsportal.com/football/germany/b/bayern-munich-Ab12Cd34/dortmund-Ef56Gh78/"
    )
    await _seed_event(factory, "audit-pin-slug", "Bayern Munich", "Dortmund", ko, "pinnacle_soccer")
    async with factory() as session:
        await persist_pick(
            session,
            _audit_pick(slug_url, "Bayern Munich Allianz Sponsor", now - timedelta(hours=1)),
            EventTeams(
                home="Bayern Munich Allianz Sponsor",
                away="Dortmund",
                league=_AUDIT_LEAGUE,
                starts_at=ko,
            ),
            "value-sharp-vs-soft",
            "v3",
        )
        await session.commit()
    anomalies = await audit_live_pinnacle_anchors(factory, now)
    assert [a for a in anomalies if a.code == "wrong_game_anchor"] == []


async def test_wrong_game_audit_flags_wrong_game_slug_fallback_anchor(factory) -> None:  # type: ignore[no-untyped-def]
    # THE BLIND-SPOT GUARD: a DEFECTIVE OddsPortal slug names a DIFFERENT team than
    # the pick's display ("Lazio" pick, but the URL slug parses "inter"/"milan").
    # The live loader's slug fallback then attaches the INTER v Milan Pinnacle
    # close to the LAZIO pick — a real wrong-game close (fake CLV) reaching prod via
    # a bad slug. The audit MUST re-run the slug path and FLAG it (display "Lazio"
    # != anchor "Inter"); without slug coverage in the audit this anchor is a silent
    # blind spot. Read-only.
    from app.maintenance.wrong_game_audit import audit_live_pinnacle_anchors

    now = datetime(2027, 7, 1, 12, 0, tzinfo=UTC)
    ko = now + timedelta(hours=6)
    bad_slug_url = "https://www.oddsportal.com/football/italy/i/inter-Ab12Cd34/milan-Ef56Gh78/"
    await _seed_event(factory, "audit-pin-inter", "Inter", "Milan", ko, "pinnacle_soccer")
    async with factory() as session:
        await persist_pick(
            session,
            _audit_pick(bad_slug_url, "Lazio", now - timedelta(hours=1)),
            EventTeams(home="Lazio", away="Milan", league=_AUDIT_LEAGUE, starts_at=ko),
            "value-sharp-vs-soft",
            "v3",
        )
        await session.commit()
    # Sanity: the live loader DOES attach the wrong (Inter) close to the Lazio pick
    # via the bad slug — so this is a genuine wrong-game path, not a contrivance.
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref=bad_slug_url,
            home="Lazio",
            away="Milan",
            kickoff=ko,
        )
    assert out  # wrong-game close attaches via the defective slug
    # The safety net MUST catch it.
    anomalies = await audit_live_pinnacle_anchors(factory, now)
    assert any(a.code == "wrong_game_anchor" for a in anomalies)


async def _seed_event(  # type: ignore[no-untyped-def]
    factory, ref: str, home: str, away: str, kickoff: datetime, sport_key: str
) -> None:
    """Seed ONE event (+ its h2h snapshots) under an arbitrary sport key at a
    caller-chosen kickoff — generalizes _seed_pinnacle_event (soccer/fixed-KO)
    for the per-sport coverage test, which needs both ``pinnacle_<sport>``
    archive events and our scraped fixtures at a now-relative kickoff."""
    captured = kickoff - timedelta(hours=2)
    snaps = [_pin_snap_at(home, 2.0, ref, captured), _pin_snap_at(away, 2.0, ref, captured)]
    teams = {ref: EventTeams(home=home, away=away, league=f"{sport_key}-cov", starts_at=kickoff)}
    await persist_odds_snapshots(factory, snaps, teams, sport_key, f"{sport_key}-cov")


async def test_archive_capture_close_match_coverage_per_sport(factory) -> None:  # type: ignore[no-untyped-def]
    """pinnacle_archive_capture_by_sport reports, per arcadia sport, how many of
    OUR upcoming scraped fixtures strict-match a captured Pinnacle close.

    The function aggregates over the whole DB, so we inject a FIXED far-future
    ``now`` whose [now, now+7d] window contains ONLY the fixtures seeded here
    (real scraped data sits ~a year earlier) — exact, race-free counts. Proves
    the tennis cross-format match (surname-initial vs full-name, ordered=False),
    the soccer alias match (ordered=True), that a non-matching fixture lifts
    scraped but not matched, and the matched <= scraped invariant.
    """
    now = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)
    ko = now + timedelta(days=1)  # inside the injected [now, now + 7d] window

    # tennis: archive "Firstname Surname", ours "Surname I." -> canonicalize equal
    await _seed_event(
        factory, "cov-pin-ten", "Frances Tiafoe", "Felix Auger-Aliassime", ko, "pinnacle_tennis"
    )
    await _seed_event(factory, "cov-our-ten", "Tiafoe F.", "Auger-Aliassime F.", ko, "tennis")
    # soccer: alias match (Man Utd -> Manchester United), ordered home/away
    await _seed_event(factory, "cov-pin-soc", "Manchester United", "Chelsea", ko, "pinnacle_soccer")
    await _seed_event(factory, "cov-our-soc", "Man Utd", "Chelsea", ko, "soccer")
    # soccer non-matching fixture (gibberish -> no archive) -> scraped++ only
    await _seed_event(factory, "cov-our-soc-x", "Zzqx United FC", "Yywv City FC", ko, "soccer")

    async with factory() as session:
        rows = {r["sport"]: r for r in await pinnacle_archive_capture_by_sport(session, now=now)}

    # tennis: 1 captured close, 1 our fixture, 1 matched (cross-format reconciled)
    assert rows["tennis"]["captured"] == 1
    assert rows["tennis"]["scraped"] == 1
    assert rows["tennis"]["matched"] == 1
    # soccer: 1 captured, 2 ours (Man Utd + gibberish), 1 matched (only Man Utd)
    assert rows["soccer"]["captured"] == 1
    assert rows["soccer"]["scraped"] == 2
    assert rows["soccer"]["matched"] == 1
    # untouched arcadia sports stay empty in this isolated future window
    assert rows["basketball"]["scraped"] == 0
    assert rows["american_football"]["scraped"] == 0
    # invariant: a close can't be found for more games than we scraped
    for row in rows.values():
        matched, scraped = row["matched"], row["scraped"]
        assert isinstance(matched, int) and isinstance(scraped, int)
        assert matched <= scraped


# Per-sport MONEYLINE market KEY as stored in odds_snapshots.market (the
# OddsHarvester key, NOT the canonical "h2h" enum value): soccer 1X2 -> "1x2",
# basketball moneyline -> "home_away". Mirrors repositories._MONEYLINE_MARKET_KEY.
_MONEYLINE_KEY = {"soccer": "1x2", "basketball": "home_away"}


def _soft_snap_at(
    bookmaker: str,
    selection: str,
    odds: float,
    event: str,
    captured: datetime,
    *,
    market_detail: str | None = None,
) -> OddsSnapshotIn:
    """One H2H snapshot under an ARBITRARY bookmaker (so we can seed a soft book
    and an inline 'Betfair Exchange' row on the SAME canonical event).
    ``market_detail`` sets the stored ``odds_snapshots.market`` key (e.g. the
    moneyline '1x2'/'home_away', or a non-moneyline submarket like
    'over_under_2_5') — what the inline-coverage repo filters on."""
    return OddsSnapshotIn(
        event_id=event,
        bookmaker=bookmaker,
        market=Market.H2H,
        market_detail=market_detail,
        selection=selection,
        decimal_odds=odds,
        captured_at=captured,
        ingested_at=captured,
    )


async def _seed_canonical_with_books(  # type: ignore[no-untyped-def]
    factory,
    ref: str,
    home: str,
    away: str,
    kickoff: datetime,
    sport_key: str,
    *,
    bookmakers: Sequence[str],
    betfair_market_detail: str | None = None,
) -> None:
    """Seed ONE canonical event with a full MONEYLINE market under EACH named
    bookmaker (stored under the sport's moneyline key, e.g. '1x2'), so the
    inline-coverage repo sees a real soft slate that may or may not carry an inline
    'Betfair Exchange' row. ``betfair_market_detail`` overrides the market the
    Betfair rows are stored under (to prove a Betfair price in a NON-moneyline
    market — e.g. 'over_under_2_5' — is NOT counted as a usable anchor)."""
    captured = kickoff - timedelta(hours=2)
    moneyline = _MONEYLINE_KEY[sport_key]
    sels = (("Home", 2.0), ("Draw", 3.4), ("Away", 3.6))
    snaps: list[OddsSnapshotIn] = []
    for bm in bookmakers:
        # Betfair defaults to the moneyline key (a usable anchor) unless the caller
        # overrides it to a non-moneyline market; every soft book stays moneyline.
        detail = (betfair_market_detail or moneyline) if bm == "Betfair Exchange" else moneyline
        snaps.extend(
            _soft_snap_at(bm, sel, o, ref, captured, market_detail=detail) for sel, o in sels
        )
    teams = {ref: EventTeams(home=home, away=away, league=f"{sport_key}-cov", starts_at=kickoff)}
    await persist_odds_snapshots(factory, snaps, teams, sport_key, f"{sport_key}-cov")


async def test_betfair_inline_capture_counts_canonical_event_betfair_row(factory) -> None:  # type: ignore[no-untyped-def]
    """betfair_inline_capture_by_sport reports, per sport, the REAL anchor
    availability that feeds picks: of OUR upcoming scraped fixtures carrying soft
    odds, how many ALSO carry an inline ``bookmaker='Betfair Exchange'`` MONEYLINE
    row (soccer '1x2', basketball 'home_away') on the SAME canonical event (the
    JSON-feed bind, bookie 44) — NOT the separate ``betfair:`` archive capture.

    A fixed far-future ``now`` windows [now, now+7d] onto only these fixtures."""
    now = datetime(2027, 7, 1, 12, 0, tzinfo=UTC)
    ko = now + timedelta(days=1)

    # soccer fixture WITH an inline Betfair Exchange moneyline row -> captured
    await _seed_canonical_with_books(
        factory,
        "inl-soc-bf",
        "Alpha",
        "Beta",
        ko,
        "soccer",
        bookmakers=["bet365", "Betfair Exchange"],
    )
    # soccer fixture with ONLY soft odds, no Betfair -> scraped++ only
    await _seed_canonical_with_books(
        factory,
        "inl-soc-nobf",
        "Gamma",
        "Delta",
        ko,
        "soccer",
        bookmakers=["bet365", "Unibet"],
    )
    # soccer fixture whose ONLY Betfair price is in a NON-moneyline market
    # (over/under): NOT a usable anchor -> scraped++ but NOT captured. This is the
    # exact live case the panel must not over-count (Betfair on O/U but not 1X2).
    await _seed_canonical_with_books(
        factory,
        "inl-soc-ou",
        "Iota",
        "Kappa",
        ko,
        "soccer",
        bookmakers=["bet365", "Betfair Exchange"],
        betfair_market_detail="over_under_2_5",
    )
    # basketball fixture, soft only, no Betfair -> structural 0 (Betfair renders
    # for liquid soccer majors only)
    await _seed_canonical_with_books(
        factory,
        "inl-bball",
        "Lakers",
        "Celtics",
        ko,
        "basketball",
        bookmakers=["bet365"],
    )

    async with factory() as session:
        rows = {r["sport"]: r for r in await betfair_inline_capture_by_sport(session, now=now)}

    assert rows["soccer"]["scraped"] == 3
    assert rows["soccer"]["captured"] == 1  # only inl-soc-bf has an inline Betfair MONEYLINE row
    assert rows["basketball"]["scraped"] == 1
    assert rows["basketball"]["captured"] == 0  # structural 0: no inline Betfair on basketball
    # invariant: captured can never exceed the soft-odds scraped denominator
    for row in rows.values():
        captured, scraped = row["captured"], row["scraped"]
        assert isinstance(captured, int) and isinstance(scraped, int)
        assert captured <= scraped


async def test_betfair_inline_capture_ignores_namespaced_archive_event(factory) -> None:  # type: ignore[no-untyped-def]
    """The inline metric must NOT count a separate ``betfair:``-namespaced archive
    event (the old, mostly-empty capture path). A canonical fixture with soft odds
    but whose ONLY Betfair price lives on the ``betfair:``-prefixed event is
    counted as scraped-but-NOT-captured by the inline metric."""
    now = datetime(2027, 8, 1, 12, 0, tzinfo=UTC)
    ko = now + timedelta(days=1)

    await _seed_canonical_with_books(
        factory, "arc-soc", "Alpha", "Beta", ko, "soccer", bookmakers=["bet365"]
    )
    # the Betfair price exists ONLY on the namespaced archive event, not inline
    teams = {
        "betfair:arc-soc": EventTeams(
            home="Alpha", away="Beta", league="betfair_soccer", starts_at=ko
        )
    }
    arc_snaps = [
        _soft_snap_at("Betfair Exchange", sel, o, "betfair:arc-soc", ko - timedelta(hours=2))
        for sel, o in (("Home", 2.2), ("Draw", 3.4), ("Away", 3.3))
    ]
    await persist_odds_snapshots(factory, arc_snaps, teams, "betfair_soccer", "betfair_soccer")

    async with factory() as session:
        rows = {r["sport"]: r for r in await betfair_inline_capture_by_sport(session, now=now)}

    assert rows["soccer"]["scraped"] == 1
    assert rows["soccer"]["captured"] == 0  # archive-only Betfair must NOT count as inline


# --- Betfair Exchange coverage (EXACT match -> presence/absence) ---------------

_BETFAIR_LEAGUE = "betfair-cov-epl"


def _betfair_pick(event_id: str) -> PickOut:
    """Soccer PickOut in the betfair-coverage league (persist_pick derives the
    event's league from pick.league, so it must be set HERE, not just on
    EventTeams)."""
    return PickOut(
        pick_id="p-betfair-cov",
        sport="soccer",
        league=_BETFAIR_LEAGUE,
        event=f"{event_id} fixture",
        event_id=event_id,
        market=Market.H2H,
        selection="Home",
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
        reason_summary="betfair coverage test",
        created_at=KO - timedelta(days=180),
    )


def _betfair_back_snaps(event_ref: str) -> list[OddsSnapshotIn]:
    """A full 1X2 Betfair Exchange BACK close (H2H, detail-less), captured 2h
    pre-kickoff so it clears the SNAPSHOT_CLOSE_MAX_GAP coverage gate."""
    return [
        OddsSnapshotIn(
            event_id=event_ref,
            bookmaker="Betfair Exchange",
            market=Market.H2H,
            selection=sel,
            decimal_odds=o,
            liquidity=5000.0,
            captured_at=CAPTURED,
            ingested_at=CAPTURED,
        )
        for sel, o in (("Home", 2.20), ("Draw", 3.40), ("Away", 3.30))
    ]


async def test_betfair_coverage_presence_absence(factory) -> None:  # type: ignore[no-untyped-def]
    """betfair_exchange_coverage_outcomes reports, per pick, whether the pick's
    CANONICAL event carries a usable Betfair BACK close (ADR-0015 v2 inline binding
    — bookmaker "Betfair Exchange" on the same event, NOT a "betfair:"+ref namespace).
    Pick A's canonical event carries a Betfair close (with_event + with_close); pick
    B carries none (neither). Read-only — attaches no close, writes no pick."""
    async with factory() as session:
        await persist_pick(
            session,
            _betfair_pick("evt-bf-A"),
            EventTeams(home="Alpha", away="Beta", league=_BETFAIR_LEAGUE, starts_at=KO),
            "value",
            "vbf",
        )
        await persist_pick(
            session,
            _betfair_pick("evt-bf-B"),
            EventTeams(home="Gamma", away="Delta", league=_BETFAIR_LEAGUE, starts_at=KO),
            "value",
            "vbf",
        )
        await session.commit()
    # Only A's fixture carries an INLINE Betfair close on its CANONICAL event.
    teams = {
        "evt-bf-A": EventTeams(home="Alpha", away="Beta", league=_BETFAIR_LEAGUE, starts_at=KO)
    }
    await persist_odds_snapshots(
        factory,
        _betfair_back_snaps("evt-bf-A"),
        teams,
        "soccer",
        "soccer",
        attach_only_to_existing=True,
    )

    async with factory() as session:
        outcomes = await betfair_exchange_coverage_outcomes(session)
    mine = {o.pick_id: o for o in outcomes if o.league == _BETFAIR_LEAGUE}
    assert len(mine) == 2
    a = next(o for o in mine.values() if o.has_betfair_event)
    assert a.has_betfair_event is True
    assert a.has_usable_close is True
    b = next(o for o in mine.values() if not o.has_betfair_event)
    assert b.has_betfair_event is False
    assert b.has_usable_close is False

    report = summarize_betfair_coverage(list(mine.values()))
    assert report.total == 2
    assert report.with_event == 1
    assert report.with_close == 1
    assert report.close_rate == 0.5


def test_betfair_full_market_rows_per_sport() -> None:
    # The usable-close width is 3 for soccer (home/draw/away) and 2 for
    # basketball (home/away); "basketball_nba" normalises to "basketball"; an
    # unmapped sport falls back to the conservative 3-way width.
    assert _betfair_full_market_rows("soccer") == 3
    assert _betfair_full_market_rows("basketball") == 2
    assert _betfair_full_market_rows("basketball_nba") == 2
    assert _betfair_full_market_rows("tennis") == 3


_BASKETBALL_COV_LEAGUE = "betfair-cov-nba"


def _betfair_basketball_pick(event_id: str) -> PickOut:
    pick = _betfair_pick(event_id)
    return pick.model_copy(update={"sport": "basketball", "league": _BASKETBALL_COV_LEAGUE})


def _betfair_basketball_back_snaps(event_ref: str) -> list[OddsSnapshotIn]:
    """A FULL 2-way basketball Betfair Exchange BACK close (home/away, NO draw),
    captured 2h pre-kickoff so it clears the coverage gate. Two H2H rows is the
    full market for basketball — usable only because the threshold is 2."""
    return [
        OddsSnapshotIn(
            event_id=event_ref,
            bookmaker="Betfair Exchange",
            market=Market.H2H,
            selection=sel,
            decimal_odds=o,
            liquidity=5000.0,
            captured_at=CAPTURED,
            ingested_at=CAPTURED,
        )
        for sel, o in (("Home", 1.80), ("Away", 2.10))
    ]


async def test_betfair_coverage_basketball_two_way_is_usable(factory) -> None:  # type: ignore[no-untyped-def]
    # A basketball pick whose betfair event carries a 2-row (home/away) close IS
    # usable: the per-sport threshold is 2 for basketball. The SAME 2-row close
    # under a SOCCER pick would NOT be usable (soccer needs 3) — proving the
    # threshold is genuinely per-sport, not a blanket relaxation.
    async with factory() as session:
        await persist_pick(
            session,
            _betfair_basketball_pick("evt-bf-bball"),
            EventTeams(home="Home", away="Away", league=_BASKETBALL_COV_LEAGUE, starts_at=KO),
            "value",
            "vbf",
        )
        # A soccer pick with only a 2-row betfair close: must read NOT usable.
        await persist_pick(
            session,
            _betfair_pick("evt-bf-soccer2"),
            EventTeams(home="Home", away="Away", league=_BETFAIR_LEAGUE, starts_at=KO),
            "value",
            "vbf",
        )
        await session.commit()
    # INLINE Betfair closes on each pick's CANONICAL event (ADR-0015 v2).
    bball_teams = {
        "evt-bf-bball": EventTeams(
            home="Home", away="Away", league=_BASKETBALL_COV_LEAGUE, starts_at=KO
        )
    }
    await persist_odds_snapshots(
        factory,
        _betfair_basketball_back_snaps("evt-bf-bball"),
        bball_teams,
        "basketball",
        "basketball",
        attach_only_to_existing=True,
    )
    soccer2_teams = {
        "evt-bf-soccer2": EventTeams(home="Home", away="Away", league=_BETFAIR_LEAGUE, starts_at=KO)
    }
    await persist_odds_snapshots(
        factory,
        _betfair_basketball_back_snaps("evt-bf-soccer2"),  # only 2 H2H rows
        soccer2_teams,
        "soccer",
        "soccer",
        attach_only_to_existing=True,
    )

    async with factory() as session:
        outcomes = await betfair_exchange_coverage_outcomes(session)
    by_league = {
        o.league: o for o in outcomes if o.league in (_BASKETBALL_COV_LEAGUE, _BETFAIR_LEAGUE)
    }
    bball = by_league[_BASKETBALL_COV_LEAGUE]
    assert bball.has_betfair_event is True
    assert bball.has_usable_close is True  # 2 rows >= basketball threshold (2)
    soccer2 = by_league[_BETFAIR_LEAGUE]
    assert soccer2.has_betfair_event is True
    assert soccer2.has_usable_close is False  # 2 rows < soccer threshold (3)


_COV_CANON_F = "betfair-cov-canon-f"
_COV_CANON_G = "betfair-cov-canon-g"


async def test_betfair_coverage_canonical_only_ignores_namespace_and_soft(factory) -> None:  # type: ignore[no-untyped-def]
    # REGRESSION (audit 2026-06-28): betfair_exchange_coverage_outcomes mirrors the
    # FIXED resolver (#139) — it counts INLINE Betfair rows on the pick's CANONICAL
    # event, IGNORES a stray "betfair:"+ref namespace event, and counts the H2H width
    # over BETFAIR rows ONLY (soft books must NEVER inflate Betfair coverage).
    async with factory() as session:
        await persist_pick(
            session,
            _betfair_pick("evt-bf-F").model_copy(update={"league": _COV_CANON_F}),
            EventTeams(home="Foo", away="Bar", league=_COV_CANON_F, starts_at=KO),
            "value",
            "vbf",
        )
        await persist_pick(
            session,
            _betfair_pick("evt-bf-G").model_copy(update={"league": _COV_CANON_G}),
            EventTeams(home="Baz", away="Qux", league=_COV_CANON_G, starts_at=KO),
            "value",
            "vbf",
        )
        await session.commit()

    # Pick F canonical: a FULL 3-row Betfair close PLUS a soft book on the SAME event.
    f_teams = {"evt-bf-F": EventTeams(home="Foo", away="Bar", league=_COV_CANON_F, starts_at=KO)}
    await persist_odds_snapshots(
        factory,
        _betfair_back_snaps("evt-bf-F"),
        f_teams,
        "soccer",
        "soccer",
        attach_only_to_existing=True,
    )
    soft_f = [
        _soft_snap_at("bet365", s, o, "evt-bf-F", CAPTURED)
        for s, o in (("Home", 2.2), ("Draw", 3.4), ("Away", 3.3))
    ]
    await persist_odds_snapshots(
        factory, soft_f, f_teams, "soccer", "soccer", attach_only_to_existing=True
    )

    # Pick G canonical: only TWO inline Betfair H2H rows + THREE soft H2H rows. If soft
    # rows counted toward the width, 2+3 >= 3 would read usable — they must NOT.
    g_teams = {"evt-bf-G": EventTeams(home="Baz", away="Qux", league=_COV_CANON_G, starts_at=KO)}
    await persist_odds_snapshots(
        factory,
        _betfair_basketball_back_snaps("evt-bf-G"),
        g_teams,
        "soccer",
        "soccer",
        attach_only_to_existing=True,
    )
    soft_g = [
        _soft_snap_at("bet365", s, o, "evt-bf-G", CAPTURED)
        for s, o in (("Home", 2.2), ("Draw", 3.4), ("Away", 3.3))
    ]
    await persist_odds_snapshots(
        factory, soft_g, g_teams, "soccer", "soccer", attach_only_to_existing=True
    )
    # G's FULL 3-row Betfair close exists ONLY under the DEAD "betfair:" namespace —
    # the instrument must ignore it (reading the canonical event, where G has only 2).
    g_ns = {
        "betfair:evt-bf-G": EventTeams(
            home="Baz", away="Qux", league="betfair_soccer", starts_at=KO
        )
    }
    await persist_odds_snapshots(
        factory, _betfair_back_snaps("betfair:evt-bf-G"), g_ns, "betfair_soccer", "betfair_soccer"
    )

    async with factory() as session:
        outcomes = await betfair_exchange_coverage_outcomes(session)
    by_league = {o.league: o for o in outcomes if o.league in (_COV_CANON_F, _COV_CANON_G)}
    f = by_league[_COV_CANON_F]
    assert f.has_betfair_event is True
    assert f.has_usable_close is True  # canonical 3-row Betfair close (soft books harmless)
    g = by_league[_COV_CANON_G]
    assert g.has_betfair_event is True  # 2 inline Betfair rows ARE present
    # 2 inline Betfair < 3: soft rows are NOT counted AND the dead-namespace 3-row
    # close is ignored — both would have flipped this True if the bug persisted.
    assert g.has_usable_close is False
