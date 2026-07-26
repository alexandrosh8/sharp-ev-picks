"""Shots-totals GAP-screen SHADOW wiring in the value pipeline.

The screen (app/models/football_shots) is annotation/veto-ONLY: with everything
at defaults it changes NOTHING; when wired it TAGS soccer totals-2.5 candidates
(p_over25/lean/reason ride reason_summary) and, only once the veto is armed
(ValuePolicy.shots_totals_veto or the module's ShotsPolicy.veto_enabled via
signal.veto), a DISAGREEING premium candidate is DEMOTED to volume (shadow)
under the named reason 'shots_totals_veto' — never alerted, never dropped,
never a fair-price source. No network; everything stubbed in-memory.
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.edge.gates import GatePolicy
from app.edge.value_policy import ValuePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import NullModel
from app.models.football_shots import ShotsTotalsSignal
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import LAST_POLL, PipelineDeps, ShotsSignalLookup, run_value_pipeline
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from tests.test_value_pipeline import FakeLoader, RecordingSink

POLICY = GatePolicy(
    min_edge=0.0,
    min_ev=0.0,
    min_confidence=0.0,
    max_odds_age_seconds=300,
    min_liquidity=0.0,
)
BARE_POLICY = ValuePolicy()  # frozen — safe as a shared default


def totals_25_snapshots() -> list[OddsSnapshotIn]:
    # Pinnacle tight on the 2.5 line; SoftBook generous on the Over -> a
    # premium-edge "Over 2.5" value candidate.
    now = datetime.now(tz=UTC)
    return [
        OddsSnapshotIn(
            event_id="evt-1",
            bookmaker=book,
            market=Market.TOTALS,
            selection=sel,
            decimal_odds=odds,
            captured_at=now - timedelta(seconds=30),
            ingested_at=now,
        )
        for book, sel, odds in [
            ("Pinnacle", "Over 2.5", 1.90),
            ("Pinnacle", "Under 2.5", 1.90),
            ("SoftBook", "Over 2.5", 2.20),
            ("SoftBook", "Under 2.5", 1.75),
        ]
    ]


class RecordingShotsLookup:
    """Typed stub for PipelineDeps.shots_signal_lookup that records calls."""

    def __init__(self, signal: ShotsTotalsSignal | None) -> None:
        self.signal = signal
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, home: str, away: str, pick_side: str) -> ShotsTotalsSignal | None:
        self.calls.append((home, away, pick_side))
        return self.signal


def make_deps(
    sink: RecordingSink,
    loader: FakeLoader,
    *,
    value_policy: ValuePolicy = BARE_POLICY,
    shots_signal_lookup: ShotsSignalLookup | None = None,
) -> PipelineDeps:
    directory = EventDirectory()
    directory.register(
        "evt-1",
        EventTeams(
            home="Home FC",
            away="Away FC",
            starts_at=datetime.now(tz=UTC) + timedelta(hours=6),
        ),
    )
    return PipelineDeps(
        loader=loader,
        model=NullModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=POLICY,
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        directory=directory,
        value_min_edge=0.015,
        value_min_odds=1.30,
        value_policy=value_policy,
        shots_signal_lookup=shots_signal_lookup,
    )


DISAGREE_TAG = ShotsTotalsSignal(
    p_over25=0.35, lean="under", veto=False, reason="shadow_tag_disagrees"
)
DISAGREE_VETO = ShotsTotalsSignal(p_over25=0.35, lean="under", veto=True, reason="veto_disagrees")
AGREE = ShotsTotalsSignal(p_over25=0.72, lean="over", veto=False, reason="agrees")
INSUFFICIENT = ShotsTotalsSignal(p_over25=None, lean=None, veto=False, reason="insufficient_data")


async def test_shots_veto_flag_demotes_disagreeing_premium_to_shadow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ValuePolicy.shots_totals_veto=True: the premium "Over 2.5" candidate the
    # screen leans UNDER on is demoted under the NAMED reason — never alerted,
    # never dropped.
    sink = RecordingSink()
    lookup = RecordingShotsLookup(DISAGREE_TAG)
    deps = make_deps(
        sink,
        FakeLoader(totals_25_snapshots()),
        value_policy=ValuePolicy(shots_totals_veto=True),
        shots_signal_lookup=lookup,
    )
    with caplog.at_level(logging.INFO):
        await run_value_pipeline(deps, "soccer")
    assert lookup.calls == [("Home FC", "Away FC", "over")]
    assert sink.sent == []  # demoted: never alerted
    assert LAST_POLL["soccer"]["picks"] == 0  # n_premium == 0 (shadow, not dropped)
    assert "shots_totals_veto" in caplog.text  # the named gate reason


async def test_shots_module_veto_signal_demotes_without_policy_flag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The module-side arm (ShotsPolicy.veto_enabled -> signal.veto True) also
    # demotes, with the bare ValuePolicy default untouched.
    sink = RecordingSink()
    deps = make_deps(
        sink,
        FakeLoader(totals_25_snapshots()),
        shots_signal_lookup=RecordingShotsLookup(DISAGREE_VETO),
    )
    with caplog.at_level(logging.INFO):
        await run_value_pipeline(deps, "soccer")
    assert sink.sent == []
    assert "shots_totals_veto" in caplog.text


async def test_shots_default_is_tag_only_premium_still_alerts() -> None:
    # DEFAULT (veto off everywhere): the disagreeing signal only TAGS — the
    # premium pick still alerts, with the shadow annotation on reason_summary.
    sink = RecordingSink()
    deps = make_deps(
        sink,
        FakeLoader(totals_25_snapshots()),
        shots_signal_lookup=RecordingShotsLookup(DISAGREE_TAG),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1  # tag-only: still premium, still alerted
    assert LAST_POLL["soccer"]["picks"] == 1
    assert "shots(shadow)" in picks[0].reason_summary
    assert "p_over25=0.350" in picks[0].reason_summary
    assert "shots veto" not in picks[0].reason_summary


async def test_shots_agreeing_signal_never_demotes_even_armed() -> None:
    # Veto armed but the screen AGREES with the pick side: premium untouched.
    sink = RecordingSink()
    deps = make_deps(
        sink,
        FakeLoader(totals_25_snapshots()),
        value_policy=ValuePolicy(shots_totals_veto=True),
        shots_signal_lookup=RecordingShotsLookup(AGREE),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1
    assert "shots(shadow)" in picks[0].reason_summary
    assert "(agrees)" in picks[0].reason_summary


async def test_shots_insufficient_data_is_a_no_op() -> None:
    # An unmatched team / cold ratings state yields the honest no-op signal:
    # no tag, no demotion, behavior identical to the unwired pipeline.
    sink = RecordingSink()
    deps = make_deps(
        sink,
        FakeLoader(totals_25_snapshots()),
        value_policy=ValuePolicy(shots_totals_veto=True),
        shots_signal_lookup=RecordingShotsLookup(INSUFFICIENT),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1
    assert "shots" not in picks[0].reason_summary


async def test_shots_unwired_lookup_is_byte_identical() -> None:
    # shots_signal_lookup=None (the default): no tag, premium alerts as before.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(totals_25_snapshots()))
    picks = await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1
    assert "shots" not in picks[0].reason_summary


async def test_shots_lookup_failure_never_blocks_minting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BoomLookup:
        def __call__(self, home: str, away: str, pick_side: str) -> ShotsTotalsSignal | None:
            raise RuntimeError("ratings state corrupted")

    sink = RecordingSink()
    deps = make_deps(
        sink,
        FakeLoader(totals_25_snapshots()),
        value_policy=ValuePolicy(shots_totals_veto=True),
        shots_signal_lookup=BoomLookup(),
    )
    with caplog.at_level(logging.ERROR):
        picks = await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1  # fail-open on the SHADOW screen: pick minted
    assert len(picks) == 1
    assert "shots signal lookup failed" in caplog.text
    assert "ratings state corrupted" not in caplog.text  # type-only log


async def test_shots_scope_excludes_other_lines_and_sports() -> None:
    # A 3.5-line totals group and a basketball cycle never consult the lookup.
    sink = RecordingSink()
    lookup = RecordingShotsLookup(DISAGREE_TAG)
    now = datetime.now(tz=UTC)
    snapshots = [
        OddsSnapshotIn(
            event_id="evt-1",
            bookmaker=book,
            market=Market.TOTALS,
            selection=sel,
            decimal_odds=odds,
            captured_at=now - timedelta(seconds=30),
            ingested_at=now,
        )
        for book, sel, odds in [
            ("Pinnacle", "Over 3.5", 1.90),
            ("Pinnacle", "Under 3.5", 1.90),
            ("SoftBook", "Over 3.5", 2.20),
            ("SoftBook", "Under 3.5", 1.75),
        ]
    ]
    deps = make_deps(
        sink,
        FakeLoader(snapshots),
        value_policy=ValuePolicy(shots_totals_veto=True),
        shots_signal_lookup=lookup,
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1  # 3.5 line: out of the screen's scope, untouched
    assert lookup.calls == []

    sink2 = RecordingSink()
    lookup2 = RecordingShotsLookup(DISAGREE_TAG)
    deps2 = make_deps(
        sink2,
        FakeLoader(totals_25_snapshots()),
        value_policy=ValuePolicy(shots_totals_veto=True),
        shots_signal_lookup=lookup2,
    )
    picks2 = await run_value_pipeline(deps2, "basketball")
    assert len(picks2) == 1  # soccer-scoped: basketball never consults it
    assert lookup2.calls == []
