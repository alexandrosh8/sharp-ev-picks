"""Pinnacle mint-time anchoring funnel — audit 2026-08-02 (defects 2a/2b).

Defect 2a: injected Pinnacle ARCADIA rows kept the archive market_detail
vocabulary ("over_under_2_75", "asian_handicap_-0_25", detail-less h2h), so
under ODDS_SOURCE=oddschecker they formed their OWN devig groups beside the
scraped ("totals_2_75", "spreads_minus_0_25", "h2h") groups and never anchored
a pick. Mint grouping must normalize BOTH sides to one canonical vocabulary —
equivalent lines join the SAME group, different lines NEVER merge, and the
AH-detail sign (producer-dependent, audit 2026-07-10) is never trusted: AH
adoption is keyed on the EXACT "{team} {signed-line}" selection string.

Defect 2b: the pick-time loader re-matched abbreviated display names against
arcadia full names every cycle and failed; it must resolve through the
persisted conf-1.0 ``event_source_links`` first and fall back to the (never
loosened) name matcher only when no link exists. One INFO counter line per
cycle makes the resolution funnel diagnosable from logs.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.edge.value_policy import ValuePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.pipeline import (
    _group_and_price_markets,
    canonical_market_detail,
    fold_injected_group_details,
)
from app.probabilities.devig import DevigMethod
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
REF = "oddschecker:101"
HOME, AWAY = "Alpha FC", "Beta United"


def _snap(
    bookmaker: str,
    market: Market,
    detail: str | None,
    selection: str,
    odds: float,
    *,
    event: str = REF,
    captured_at: datetime = NOW,
) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id=event,
        bookmaker=bookmaker,
        market=market,
        market_detail=detail,
        selection=selection,
        decimal_odds=odds,
        captured_at=captured_at,
        ingested_at=captured_at,
    )


def _grouped_and_fair(snaps: list[OddsSnapshotIn]):  # type: ignore[no-untyped-def]
    return _group_and_price_markets(
        snaps,
        devig_method=DevigMethod.POWER,
        value_policy=ValuePolicy(),
        freshness_basis="provider",
        exchange_demoted_events=frozenset(),
    )


def _soft_totals(detail: str, line: str) -> list[OddsSnapshotIn]:
    return [
        _snap(book, Market.TOTALS, detail, sel, odds)
        for book in ("SoftA", "SoftB")
        for sel, odds in ((f"Over {line}", 1.90), (f"Under {line}", 1.90))
    ]


def _pin_totals(detail: str, line: str) -> list[OddsSnapshotIn]:
    return [
        _snap("Pinnacle", Market.TOTALS, detail, f"Over {line}", 1.95),
        _snap("Pinnacle", Market.TOTALS, detail, f"Under {line}", 1.95),
    ]


def _soft_spreads(detail: str, home_line: str, away_line: str) -> list[OddsSnapshotIn]:
    return [
        _snap(book, Market.SPREADS, detail, sel, odds)
        for book in ("SoftA", "SoftB")
        for sel, odds in ((f"{HOME} {home_line}", 1.90), (f"{AWAY} {away_line}", 1.90))
    ]


def _pin_spreads(detail: str, home_line: str, away_line: str) -> list[OddsSnapshotIn]:
    return [
        _snap("Pinnacle", Market.SPREADS, detail, f"{HOME} {home_line}", 1.95),
        _snap("Pinnacle", Market.SPREADS, detail, f"{AWAY} {away_line}", 1.95),
    ]


# --------------------------------------------------------------------------- #
# canonical detail — arcadia totals vocabulary folds
# --------------------------------------------------------------------------- #


def test_canonical_market_detail_folds_arcadia_games_totals() -> None:
    assert canonical_market_detail("over_under_games_220_5") == "totals_220_5"
    assert canonical_market_detail("over_under_games_221_0") == "totals_221"
    # quarter lines preserved exactly — never collapsed onto a neighbour line
    assert canonical_market_detail("over_under_2_75") == "totals_2_75"
    assert canonical_market_detail("over_under_2_5") == "totals_2_5"


# --------------------------------------------------------------------------- #
# defect 2a — injected rows join the scraped devig group
# --------------------------------------------------------------------------- #


def test_injected_totals_vocab_joins_scraped_group() -> None:
    snaps = _soft_totals("totals_2_75", "2.75") + _pin_totals("over_under_2_75", "2.75")
    grouped, _fresh, _miss, fair = _grouped_and_fair(snaps)
    key = (REF, Market.TOTALS, "totals_2_75")
    assert key in grouped
    assert (REF, Market.TOTALS, "over_under_2_75") not in grouped
    assert "Pinnacle" in grouped[key][0]["Over 2.75"]
    assert fair[key][0] == "Pinnacle"


def test_injected_h2h_detail_none_joins_scraped_h2h_group() -> None:
    soft = [
        _snap(book, Market.H2H, "h2h", sel, odds)
        for book in ("SoftA", "SoftB")
        for sel, odds in ((HOME, 2.40), ("Draw", 3.40), (AWAY, 3.20))
    ]
    pin = [
        _snap("Pinnacle", Market.H2H, None, sel, odds)
        for sel, odds in ((HOME, 2.45), ("Draw", 3.45), (AWAY, 3.15))
    ]
    grouped, _fresh, _miss, fair = _grouped_and_fair(soft + pin)
    key = (REF, Market.H2H, None)
    assert key in grouped
    assert (REF, Market.H2H, "h2h") not in grouped
    assert fair[key][0] == "Pinnacle"


def test_injected_ah_quarter_line_joins_scraped_spreads_group() -> None:
    snaps = _soft_spreads("spreads_minus_0_25", "-0.25", "+0.25") + _pin_spreads(
        "asian_handicap_-0_25", "-0.25", "+0.25"
    )
    grouped, _fresh, _miss, fair = _grouped_and_fair(snaps)
    key = (REF, Market.SPREADS, "spreads_minus_0_25")
    assert key in grouped
    assert (REF, Market.SPREADS, "asian_handicap_-0_25") not in grouped
    assert "Pinnacle" in grouped[key][0][f"{HOME} -0.25"]
    assert fair[key][0] == "Pinnacle"


def test_injected_ah_sign_flip_still_joins_by_selection() -> None:
    # The cross-provider detail SIGN is producer-dependent (pick 74637 class,
    # audit 2026-07-10): arcadia keyed home +0.5 while OddsChecker keyed the
    # same market spreads_minus_0_5. Adoption is selection-keyed, so the flip
    # must not matter as long as the "{team} {signed-line}" strings agree.
    snaps = _soft_spreads("spreads_minus_0_5", "-0.5", "+0.5") + _pin_spreads(
        "asian_handicap_0_5", "-0.5", "+0.5"
    )
    grouped, _fresh, _miss, fair = _grouped_and_fair(snaps)
    key = (REF, Market.SPREADS, "spreads_minus_0_5")
    assert key in grouped
    assert (REF, Market.SPREADS, "asian_handicap_0_5") not in grouped
    assert fair[key][0] == "Pinnacle"


def test_different_lines_never_merge() -> None:
    snaps = (
        _soft_totals("totals_2_5", "2.5")
        + _pin_totals("over_under_2_75", "2.75")
        + _soft_spreads("spreads_minus_0_25", "-0.25", "+0.25")
        + _pin_spreads("asian_handicap_-0_75", "-0.75", "+0.75")
    )
    grouped, _fresh, _miss, _fair = _grouped_and_fair(snaps)
    # totals: 2.5 vs 2.75 stay distinct devig groups
    assert (REF, Market.TOTALS, "totals_2_5") in grouped
    assert (REF, Market.TOTALS, "totals_2_75") in grouped
    assert "Pinnacle" not in grouped[(REF, Market.TOTALS, "totals_2_5")][0]["Over 2.5"]
    # spreads: -0.25 vs -0.75 stay distinct (no shared selection -> no adoption)
    assert (REF, Market.SPREADS, "spreads_minus_0_25") in grouped
    assert (REF, Market.SPREADS, "asian_handicap_-0_75") in grouped
    assert (
        "Pinnacle" not in grouped[(REF, Market.SPREADS, "spreads_minus_0_25")][0][f"{HOME} -0.25"]
    )


def test_integer_ah_line_stays_fail_closed() -> None:
    # An integer scraped spreads_* group may be a 3-way European handicap
    # (audit 2026-07-10) — a 2-way integer AH close must never be pooled in,
    # even when the selection strings coincide.
    snaps = _soft_spreads("spreads_minus_1", "-1", "+1") + _pin_spreads(
        "asian_handicap_-1_0", "-1", "+1"
    )
    grouped, _fresh, _miss, _fair = _grouped_and_fair(snaps)
    assert (REF, Market.SPREADS, "spreads_minus_1") in grouped
    assert (REF, Market.SPREADS, "asian_handicap_-1_0") in grouped
    assert "Pinnacle" not in grouped[(REF, Market.SPREADS, "spreads_minus_1")][0][f"{HOME} -1"]


def test_ambiguous_native_selection_blocks_adoption() -> None:
    # Corrupt/mirrored feed: the SAME selection string appears in TWO native
    # fractional spreads groups — adoption cannot pick one, so the injected
    # group stays separate (fail-closed, never guessed).
    snaps = (
        _soft_spreads("spreads_minus_0_25", "-0.25", "+0.25")
        + [_snap("SoftC", Market.SPREADS, "spreads_plus_0_25", f"{HOME} -0.25", 1.88)]
        + _pin_spreads("asian_handicap_-0_25", "-0.25", "+0.25")
    )
    grouped, _fresh, _miss, _fair = _grouped_and_fair(snaps)
    assert (REF, Market.SPREADS, "asian_handicap_-0_25") in grouped
    assert (
        "Pinnacle" not in grouped[(REF, Market.SPREADS, "spreads_minus_0_25")][0][f"{HOME} -0.25"]
    )


def test_fold_leaves_unrelated_details_untouched() -> None:
    snaps = [
        _snap("SoftA", Market.SPREADS, "spreads_sets_minus_1_5", "Alpha -1.5", 1.9),
        _snap("SoftA", Market.OTHER, "oc_corners_race_to_3", "Alpha", 1.5),
    ]
    out = fold_injected_group_details(snaps)
    assert [s.market_detail for s in out] == ["spreads_sets_minus_1_5", "oc_corners_race_to_3"]


def test_merged_group_prefers_pinnacle_over_betfair_anchor() -> None:
    snaps = (
        _soft_totals("totals_2_75", "2.75")
        + _pin_totals("over_under_2_75", "2.75")
        + [
            _snap("Betfair Exchange", Market.TOTALS, "totals_2_75", "Over 2.75", 1.96),
            _snap("Betfair Exchange", Market.TOTALS, "totals_2_75", "Under 2.75", 1.96),
        ]
    )
    _grouped, _fresh, _miss, fair = _grouped_and_fair(snaps)
    assert fair[(REF, Market.TOTALS, "totals_2_75")][0] == "Pinnacle"


# --------------------------------------------------------------------------- #
# defect 2b — loader telemetry (one INFO counter line per cycle)
# --------------------------------------------------------------------------- #


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


async def test_loader_emits_pinnacle_resolution_counter_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import app.storage.repositories as repositories
    from app.clv_trueup import build_sharp_anchor_loader

    now = datetime.now(tz=UTC)
    kickoff = now + timedelta(hours=2)
    fresh, stale = now - timedelta(minutes=10), now - timedelta(hours=10)

    def _pin_rows(ref: str, captured: datetime) -> list[OddsSnapshotIn]:
        return [
            _snap("Pinnacle", Market.H2H, None, sel, odds, event=ref, captured_at=captured)
            for sel, odds in ((HOME, 2.4), ("Draw", 3.4), (AWAY, 3.2))
        ]

    plan = {
        "evt-link": ("link", _pin_rows("evt-link", fresh)),  # resolved via link
        "evt-name": ("name_match", _pin_rows("evt-name", fresh)),  # resolved, no link
        "evt-stale": ("link", _pin_rows("evt-stale", stale)),  # freshness drop
        "evt-fail": ("no_match", []),  # name matcher failed
    }

    async def fake_resolver(session, **kwargs):  # type: ignore[no-untyped-def]
        ref = kwargs["pick_external_ref"]
        outcome, rows = plan[ref]
        outcome_out = kwargs.get("outcome_out")
        if outcome_out is not None:
            outcome_out[ref] = outcome
        provenance_out = kwargs.get("provenance_out")
        if provenance_out is not None and rows:
            provenance_out[ref] = (1.0, outcome)
        return rows

    monkeypatch.setattr(repositories, "resolve_pinnacle_close_snaps", fake_resolver)

    directory = EventDirectory()
    for ref in plan:
        directory.register(ref, EventTeams(home=HOME, away=AWAY, starts_at=kickoff))
    # evt-nodir has NO directory entry -> directory_miss

    loader = build_sharp_anchor_loader(
        _FakeSession,  # type: ignore[arg-type]  # factory(): async context manager
        directory,
        use_betfair=False,
        use_pinnacle=True,
        max_age_seconds=3600.0,
    )
    scrape = [
        _snap("SoftBook", Market.H2H, "h2h", HOME, 2.9, event=ref, captured_at=now)
        for ref in [*plan, "evt-nodir"]
    ]
    with caplog.at_level(logging.INFO, logger="app.clv_trueup"):
        out, provenance = await loader("soccer", scrape)

    assert {s.event_id for s in out} == {"evt-link", "evt-name"}
    lines = [
        r.getMessage() for r in caplog.records if "pinnacle anchor resolution" in r.getMessage()
    ]
    assert len(lines) == 1
    line = lines[0]
    for token in (
        "resolved=2",
        "link_miss=2",
        "match_fail=1",
        "freshness_drop=1",
        "directory_miss=1",
    ):
        assert token in line, f"{token!r} missing from {line!r}"


# --------------------------------------------------------------------------- #
# defect 2b — link-based resolution (compose Postgres; skip when absent)
# --------------------------------------------------------------------------- #

from tests.database import TEST_DATABASE_URL  # noqa: E402

KO = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
CAPTURED = KO - timedelta(hours=2)


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(TEST_DATABASE_URL)
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


async def _seed_arcadia_event(factory, ref: str, home: str, away: str) -> None:  # type: ignore[no-untyped-def]
    from app.storage.repositories import persist_odds_snapshots

    snaps = [
        _snap("Pinnacle", Market.H2H, None, sel, odds, event=ref, captured_at=CAPTURED)
        for sel, odds in ((home, 2.10), ("Draw", 3.40), (away, 3.60))
    ]
    teams = {ref: EventTeams(home=home, away=away, league="pin", starts_at=KO)}
    await persist_odds_snapshots(factory, snaps, teams, "pinnacle_soccer", "pinnacle_soccer")


async def _seed_canonical_event(factory, ref: str, home: str, away: str) -> None:  # type: ignore[no-untyped-def]
    from app.storage.repositories import persist_odds_snapshots

    snaps = [
        _snap("SoftBook", Market.H2H, "h2h", sel, odds, event=ref, captured_at=CAPTURED)
        for sel, odds in ((home, 2.20), ("Draw", 3.30), (away, 3.40))
    ]
    teams = {ref: EventTeams(home=home, away=away, league="league", starts_at=KO)}
    await persist_odds_snapshots(factory, snaps, teams, "soccer", "soccer")


async def _seed_link(factory, arcadia_ref: str, canonical_ref: str) -> None:  # type: ignore[no-untyped-def]
    from app.storage.repositories import SourceLinkByRef, upsert_event_source_links

    async with factory() as session:
        written = await upsert_event_source_links(
            session,
            [
                SourceLinkByRef(
                    source="pinnacle_arcadia",
                    source_event_id=arcadia_ref,
                    canonical_external_ref=canonical_ref,
                    confidence=1.0,
                    method="exact",
                    matched_at=datetime.now(tz=UTC),
                )
            ],
        )
        assert written == 1
        await session.commit()


async def test_resolver_link_fast_path_skips_name_matcher(  # type: ignore[no-untyped-def]
    factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Display names share NO tokens with the arcadia names: the hardened name
    # matcher can never accept them (and is stubbed to prove it is not even
    # consulted). The persisted conf-1.0 link must resolve the close anyway.
    import app.resolution as resolution
    from app.storage.repositories import resolve_pinnacle_close_snaps

    await _seed_arcadia_event(factory, "arc-elks", "Edmonton Elks", "Nashville Soccer Club")
    await _seed_canonical_event(factory, "oc:elks", "Edm Zz", "Nsh Qq")
    await _seed_link(factory, "arc-elks", "oc:elks")

    def _never_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("name matcher consulted despite persisted link")

    monkeypatch.setattr(resolution, "match_event_hardened_scored", _never_called)

    provenance: dict[str, tuple[float, str]] = {}
    outcome: dict[str, str] = {}
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="oc:elks",
            home="Edm Zz",
            away="Nsh Qq",
            kickoff=KO,
            provenance_out=provenance,
            outcome_out=outcome,
        )
    assert outcome["oc:elks"] == "link"
    by_sel = {s.selection: s for s in out}
    # re-keyed to the PICK's display vocabulary via the linked arcadia event
    assert set(by_sel) == {"Edm Zz", "Draw", "Nsh Qq"}
    assert all(s.event_id == "oc:elks" for s in out)
    assert all(s.bookmaker == "Pinnacle" for s in out)
    conf, method = provenance["oc:elks"]
    assert conf == pytest.approx(1.0)
    assert method.startswith("link_")


async def test_resolver_falls_back_to_name_match_without_link(factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_arcadia_event(factory, "arc-nm", "Gamma Rovers", "Delta City")
    await _seed_canonical_event(factory, "oc:nm", "Gamma Rovers", "Delta City")

    outcome: dict[str, str] = {}
    async with factory() as session:
        out = await resolve_via(session, "oc:nm", "Gamma Rovers", "Delta City", outcome)
    assert outcome["oc:nm"] == "name_match"
    assert {s.selection for s in out} == {"Gamma Rovers", "Draw", "Delta City"}


async def test_resolver_outcome_no_match_without_link_or_name(factory) -> None:  # type: ignore[no-untyped-def]
    await _seed_arcadia_event(factory, "arc-none", "Epsilon FC", "Zeta Town")
    await _seed_canonical_event(factory, "oc:none", "Wholly Other", "Names Here")

    outcome: dict[str, str] = {}
    async with factory() as session:
        out = await resolve_via(session, "oc:none", "Wholly Other", "Names Here", outcome)
    assert out == []
    assert outcome["oc:none"] == "no_match"


async def resolve_via(session, ref: str, home: str, away: str, outcome: dict[str, str]):  # type: ignore[no-untyped-def]
    from app.storage.repositories import resolve_pinnacle_close_snaps

    return await resolve_pinnacle_close_snaps(
        session,
        pinnacle_sport_key="pinnacle_soccer",
        pick_external_ref=ref,
        home=home,
        away=away,
        kickoff=KO,
        outcome_out=outcome,
    )
