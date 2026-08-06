"""Spreads devig-group pair coherence (live defect: pick 960785, CFL spreads).

The OddsChecker selection-signed key space slugs BOTH teams' minus legs into
one ``spreads_minus_<line>`` key ("Toronto Argonauts -2.5" AND "Calgary
Stampeders -2.5" shared spreads_minus_2_5), so a devig group can contain a
NON-COMPLEMENTARY same-sign pair. A 2-way devig of that pair fabricates edge:
Betfair quoted Toronto -2.5 @ 1.94 and Calgary -2.5 @ 2.06 (gross sum ~1.0009
— it LOOKS like a fair two-sided book), devigging fair(Toronto -2.5) ~0.515
against a stale BOYLE 2.30 fill = a phantom +8.02% edge, while the TRUE
complement (Calgary +2.5) lived in the separate spreads_plus_2_5 group.

Fix (devig-group construction layer): a SPREADS group is devig-eligible as a
two-sided pair ONLY when its selections parse as {team_a +X, team_b -X} for
the same |X| — same-sign opposite-team legs, unparsable team+sign legs, and
any other shape REFUSE devig fail-closed under the named gate reason
``spread_pair_incoherent``. This is the orientation sibling of the EH/AH
mixed-group class fixed 2026-08-02.
"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.edge.value import (
    SPREAD_PAIR_INCOHERENT_REASON,
    parse_spread_selection,
    spread_pair_coherent,
)
from app.edge.value_policy import ValuePolicy
from app.pipeline import event_fair_probs, group_market_prices
from app.probabilities.devig import DevigMethod
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from tests.test_value_pipeline import (
    FakeLoader,
    RecordingSink,
    group_snap,
    make_deps_league,
)

# --------------------------------------------------------------------------- #
# parse_spread_selection — the selection string carries the authoritative
# signed line (detail signs are producer-dependent, audit 2026-07-10).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("Toronto Argonauts -2.5", ("toronto argonauts", -2.5)),
        ("Calgary Stampeders +2.5", ("calgary stampeders", 2.5)),
        ("AEP Paphos +0.25", ("aep paphos", 0.25)),
        ("AEP Paphos +0", ("aep paphos", 0.0)),
        ("1. FC Koln +0.5", ("1. fc koln", 0.5)),  # digits/periods inside the name
        ("Adam Walton -1.5", ("adam walton", -1.5)),
        ("  Home FC -1  ", ("home fc", -1.0)),  # whitespace-tolerant
    ],
)
def test_parse_spread_selection_valid_forms(selection: str, expected: tuple[str, float]) -> None:
    assert parse_spread_selection(selection) == expected


@pytest.mark.parametrize(
    "selection",
    [
        "Toronto Argonauts 2.5",  # missing sign — line orientation unprovable
        "Toronto Argonauts",  # no line at all
        "Over 2.5",  # totals-shaped, no signed handicap
        "Draw",  # EH artifact leg
        "-2.5",  # empty team
        "",  # empty string
    ],
)
def test_parse_spread_selection_unparsable_forms(selection: str) -> None:
    assert parse_spread_selection(selection) is None


# --------------------------------------------------------------------------- #
# spread_pair_coherent — the devig-eligibility predicate.
# --------------------------------------------------------------------------- #


def test_complementary_pair_is_coherent() -> None:
    assert spread_pair_coherent(["Home FC -1.5", "Away FC +1.5"])


def test_complementary_quarter_line_pair_is_coherent() -> None:
    assert spread_pair_coherent(["AEP Paphos -0.25", "AGF Aarhus +0.25"])


def test_level_zero_pair_is_coherent() -> None:
    # +0 is its own complement (level/DNB-style handicap) — both legs at +0.
    assert spread_pair_coherent(["Home FC +0", "Away FC +0"])


def test_same_sign_pair_refuses() -> None:
    # The 960785 defect shape: both teams' MINUS legs share one group.
    assert not spread_pair_coherent(["Toronto Argonauts -2.5", "Calgary Stampeders -2.5"])


def test_same_sign_plus_pair_refuses() -> None:
    assert not spread_pair_coherent(["Home FC +1.5", "Away FC +1.5"])


def test_mismatched_line_magnitude_refuses() -> None:
    assert not spread_pair_coherent(["Home FC -2.5", "Away FC +1.5"])


def test_same_team_both_legs_refuses() -> None:
    assert not spread_pair_coherent(["Home FC -2.5", "Home FC +2.5"])


def test_unparsable_member_refuses() -> None:
    assert not spread_pair_coherent(["Home FC -2.5", "Away FC 2.5"])


def test_single_selection_refuses() -> None:
    assert not spread_pair_coherent(["Home FC -2.5"])


def test_three_way_eh_with_draw_leg_is_coherent() -> None:
    # The NAMESPACED 3-way European handicap (arcadia/odds_api vocabulary,
    # detail "european_handicap_-1") is devig-sound: Home -1 / Draw / Away +1
    # are mutually exclusive & exhaustive. It must stay eligible.
    assert spread_pair_coherent(["Home FC -1", "Draw (-1)", "Away FC +1"])
    assert spread_pair_coherent(["Home FC -1", "Draw", "Away FC +1"])


def test_three_way_with_draw_leg_but_same_sign_teams_refuses() -> None:
    # Draw leg present but the team legs are NOT complementary — still refuse.
    assert not spread_pair_coherent(["Home FC -1", "Draw (-1)", "Away FC -1"])


def test_three_selections_without_draw_leg_refuse() -> None:
    assert not spread_pair_coherent(["Home FC -1", "Away FC +1", "Away FC +2"])


def test_four_selections_refuse() -> None:
    assert not spread_pair_coherent(["Home FC -1", "Away FC +1", "Home FC -2", "Away FC +2"])


def test_draw_prefixed_club_name_is_not_a_draw_leg() -> None:
    # A club whose name merely starts with "Draw…" must not be mistaken for
    # the EH Draw leg (cf. the draw_selection_demotion whole-leg rule).
    assert spread_pair_coherent(["Drawsko Pomorskie -1.5", "Away FC +1.5"])


# --------------------------------------------------------------------------- #
# event_fair_probs — the devig-group construction chokepoint (mint AND close
# paths both flow through it, so fill and close refuse identically).
# --------------------------------------------------------------------------- #

_NOW = datetime.now(tz=UTC)


def _snap(
    sel: str,
    book: str,
    odds: float,
    market: Market = Market.SPREADS,
    detail: str | None = "spreads_minus_2_5",
) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id="evt-cfl",
        bookmaker=book,
        market=market,
        selection=sel,
        decimal_odds=odds,
        captured_at=_NOW - timedelta(seconds=30),
        ingested_at=_NOW,
        market_detail=detail,
    )


def test_incoherent_spreads_group_refuses_devig_with_named_reason() -> None:
    grouped = group_market_prices(
        [
            _snap("Toronto Argonauts -2.5", "Betfair Exchange", 1.94),
            _snap("Calgary Stampeders -2.5", "Betfair Exchange", 2.06),
        ]
    )
    miss: dict[tuple[str, Market, str | None], str] = {}
    fair = event_fair_probs(grouped, DevigMethod.MULTIPLICATIVE, ValuePolicy(), sharp_miss_out=miss)
    key = ("evt-cfl", Market.SPREADS, "spreads_minus_2_5")
    assert key not in fair  # devig REFUSED — no fair, sharp or consensus
    assert miss[key] == SPREAD_PAIR_INCOHERENT_REASON


def test_coherent_spreads_group_still_devigs_sum_one_order_preserved() -> None:
    grouped = group_market_prices(
        [
            _snap("Toronto Argonauts -2.5", "Betfair Exchange", 1.94),
            _snap("Calgary Stampeders +2.5", "Betfair Exchange", 2.06),
        ]
    )
    miss: dict[tuple[str, Market, str | None], str] = {}
    fair = event_fair_probs(grouped, DevigMethod.MULTIPLICATIVE, ValuePolicy(), sharp_miss_out=miss)
    key = ("evt-cfl", Market.SPREADS, "spreads_minus_2_5")
    assert key in fair
    _book, fair_by_sel = fair[key]
    assert math.isclose(sum(fair_by_sel.values()), 1.0, abs_tol=1e-9)
    # favourite/longshot order preserved: the shorter price stays the favourite
    assert fair_by_sel["Toronto Argonauts -2.5"] > fair_by_sel["Calgary Stampeders +2.5"]
    assert not miss  # no miss reason for a healthy group


def test_tennis_sets_spread_coherent_pair_untouched() -> None:
    grouped = group_market_prices(
        [
            _snap("Adam Walton -1.5", "Betfair Exchange", 1.90, detail="spreads_sets_1_5"),
            _snap("Ugo Humbert +1.5", "Betfair Exchange", 2.10, detail="spreads_sets_1_5"),
        ]
    )
    fair = event_fair_probs(grouped, DevigMethod.MULTIPLICATIVE, ValuePolicy())
    assert ("evt-cfl", Market.SPREADS, "spreads_sets_1_5") in fair


def test_ah_fractional_coherent_pair_untouched() -> None:
    grouped = group_market_prices(
        [
            _snap("Home FC -0.25", "Betfair Exchange", 1.95, detail="asian_handicap_-0_25"),
            _snap("Away FC +0.25", "Betfair Exchange", 2.05, detail="asian_handicap_-0_25"),
        ]
    )
    fair = event_fair_probs(grouped, DevigMethod.MULTIPLICATIVE, ValuePolicy())
    assert ("evt-cfl", Market.SPREADS, "asian_handicap_-0_25") in fair


def test_non_spreads_markets_byte_identical() -> None:
    # TOTALS/H2H/DNB groups never see the spreads predicate — including
    # selection shapes the spread parser cannot read ("Over 2.5", bare teams).
    snaps = [
        _snap("Over 2.5", "Betfair Exchange", 1.90, market=Market.TOTALS, detail="totals_2_5"),
        _snap("Under 2.5", "Betfair Exchange", 2.10, market=Market.TOTALS, detail="totals_2_5"),
        _snap("Home FC", "Betfair Exchange", 1.90, market=Market.DNB, detail=None),
        _snap("Away FC", "Betfair Exchange", 2.10, market=Market.DNB, detail=None),
    ]
    miss: dict[tuple[str, Market, str | None], str] = {}
    fair = event_fair_probs(
        group_market_prices(snaps), DevigMethod.MULTIPLICATIVE, ValuePolicy(), sharp_miss_out=miss
    )
    assert ("evt-cfl", Market.TOTALS, "totals_2_5") in fair
    assert ("evt-cfl", Market.DNB, None) in fair
    assert not miss


def test_pick_960785_group_fixture_refuses() -> None:
    """Regression fixture reconstructed READ-ONLY from the production DB:
    event 22042 (Toronto Argonauts vs Calgary Stampeders, CFL, KO 2026-08-06
    23:30Z), group (spreads, 'spreads_minus_2_5') at mint 2026-08-06 00:12:20Z.
    Both selections are MINUS legs of opposite teams; Betfair priced both
    (1.94/2.06, gross sum ~1.0009 — passing the overround gate) and the 2-way
    devig fabricated fair(Toronto -2.5) ~0.515 vs the stale BOYLE 2.30 fill =
    the phantom +8.02% edge on pick 960785. Under the fix the group REFUSES."""
    rows = [
        ("Toronto Argonauts -2.5", "Betfair Exchange", 1.94),
        ("Toronto Argonauts -2.5", "BOYLE Sports", 2.30),
        ("Toronto Argonauts -2.5", "AK Bets", 2.20),
        ("Toronto Argonauts -2.5", "BresBet", 2.20),
        ("Toronto Argonauts -2.5", "PricedUp", 2.20),
        ("Toronto Argonauts -2.5", "Star Sports", 2.20),
        ("Toronto Argonauts -2.5", "Virgin Bet", 2.25),
        ("Toronto Argonauts -2.5", "William Hill", 2.25),
        ("Toronto Argonauts -2.5", "bet365", 2.15),
        ("Calgary Stampeders -2.5", "Betfair Exchange", 2.06),
        ("Calgary Stampeders -2.5", "BOYLE Sports", 1.95),
        ("Calgary Stampeders -2.5", "BetAhoy", 1.95),
        ("Calgary Stampeders -2.5", "Virgin Bet", 1.96),
        ("Calgary Stampeders -2.5", "William Hill", 1.91),
    ]
    grouped = group_market_prices([_snap(sel, book, odds) for sel, book, odds in rows])
    miss: dict[tuple[str, Market, str | None], str] = {}
    fair = event_fair_probs(grouped, DevigMethod.POWER, ValuePolicy(), sharp_miss_out=miss)
    key = ("evt-cfl", Market.SPREADS, "spreads_minus_2_5")
    assert key not in fair  # the 8.02% edge can never be recomputed
    assert miss[key] == SPREAD_PAIR_INCOHERENT_REASON


# --------------------------------------------------------------------------- #
# run_value_pipeline — the refusal is fail-closed AND never silent.
# --------------------------------------------------------------------------- #


def _pipeline_spread_snaps(sels: tuple[str, str]) -> list[OddsSnapshotIn]:
    # Sharp two-sided book + a generous soft price on the first leg — the
    # shape that minted 960785 when the pair was non-complementary.
    return [
        group_snap("Betfair Exchange", sels[0], 1.94, Market.SPREADS, "spreads_minus_2_5"),
        group_snap("Betfair Exchange", sels[1], 2.06, Market.SPREADS, "spreads_minus_2_5"),
        group_snap("SoftBook", sels[0], 2.30, Market.SPREADS, "spreads_minus_2_5"),
        group_snap("SoftBook", sels[1], 1.95, Market.SPREADS, "spreads_minus_2_5"),
    ]


async def test_pipeline_refuses_incoherent_spread_group(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(_pipeline_spread_snaps(("Home FC -2.5", "Away FC -2.5"))),
        league="Premier League",
        value_policy=ValuePolicy(),
    )
    with caplog.at_level(logging.INFO):
        await run_value_pipeline_for_test(deps)
    assert sink.sent == []  # no alert
    assert LAST_POLL["soccer"]["picks"] == 0  # no premium pick
    assert SPREAD_PAIR_INCOHERENT_REASON in caplog.text  # named, never silent


async def test_pipeline_mints_from_coherent_spread_group() -> None:
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(_pipeline_spread_snaps(("Home FC -2.5", "Away FC +2.5"))),
        league="Premier League",
        value_policy=ValuePolicy(),
    )
    await run_value_pipeline_for_test(deps)
    assert len(sink.sent) == 1  # the complementary sibling still mints
    assert LAST_POLL["soccer"]["picks"] == 1


async def run_value_pipeline_for_test(deps: object) -> None:
    from app.pipeline import PipelineDeps, run_value_pipeline

    assert isinstance(deps, PipelineDeps)
    await run_value_pipeline(deps, "soccer")


# --------------------------------------------------------------------------- #
# /lab/gate-reasons visibility: refused groups write candidate_evaluations
# rows under the named slug (DB-backed; skips without the compose Postgres).
# --------------------------------------------------------------------------- #

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.storage.models import CandidateEvaluation, Event  # noqa: E402
from tests.test_candidate_audit import _seed_event, factory  # noqa: E402, F401


async def test_refusal_writes_named_gate_reason_rows(
    factory: async_sessionmaker,  # noqa: F811
) -> None:
    from dataclasses import replace

    from app.pipeline import _record_spread_pair_refusal_audit

    event_pk = await _seed_event(factory)
    async with factory() as session:
        event_ref = await session.scalar(select(Event.external_ref).where(Event.id == event_pk))
    assert event_ref is not None
    deps = replace(
        make_deps_league(
            RecordingSink(),
            FakeLoader([]),
            league="CFL",
            value_policy=ValuePolicy(),
        ),
        session_factory=factory,
    )
    selections = ["Toronto Argonauts -2.5", "Calgary Stampeders -2.5"]
    await _record_spread_pair_refusal_audit(
        deps,
        "americanfootball",
        event_ref,
        Market.SPREADS,
        "spreads_minus_2_5",
        selections,
        datetime.now(tz=UTC),
    )
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(CandidateEvaluation)
                    .where(CandidateEvaluation.event_id == event_pk)
                    .order_by(CandidateEvaluation.id)
                )
            )
            .scalars()
            .all()
        )
    assert [r.selection for r in rows] == selections
    for row in rows:
        assert row.tier == "refused"  # nothing was kept or demoted — no pick
        assert row.reasons == {"reasons": [SPREAD_PAIR_INCOHERENT_REASON]}
        assert row.anchor_book is None  # no provenance fabricated
        assert row.best_odds is None
