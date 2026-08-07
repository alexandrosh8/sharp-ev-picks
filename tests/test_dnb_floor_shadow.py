"""ADR-0028 sharp-DNB premium-floor SHADOW COHORT marker (measurement only).

Pre-registration: docs/adr/adr-0028-sharp-dnb-premium-floor-preregistration.md.
A sharp-anchored soccer DNB candidate whose edge sits in the
``[PREMIUM_FLOOR_SHADOW_DNB_MIN_EDGE, premium-floor)`` band failed ONLY the
premium floor — it mints at the volume (shadow) tier exactly as before, but is
MARKED with the named reason ``premium_floor_shadow_dnb`` (reason_summary note
+ candidate_evaluations slug) so the cohort's own forward trusted CLV is
queryable for the ADR-0028 promotion decision. ZERO behavior change to
tiers/alerts/stakes — the marker is telemetry, never a gate.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.edge.gates import GatePolicy
from app.edge.value import (
    PREMIUM_FLOOR_SHADOW_DNB_MIN_EDGE,
    PREMIUM_FLOOR_SHADOW_DNB_REASON,
    premium_floor_shadow_dnb,
)
from app.edge.value_policy import ValuePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import NullModel
from app.notifications.base import Alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import LAST_POLL, PickOut, PipelineDeps, run_value_pipeline
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

# --- 1. pure predicate -------------------------------------------------------


def test_predicate_marks_sharp_dnb_inside_the_band() -> None:
    assert premium_floor_shadow_dnb("dnb", 0.020, premium_floor=0.03, anchor_book="Pinnacle")
    # inclusive lower bound, exclusive upper bound (the premium floor itself)
    assert premium_floor_shadow_dnb(
        "dnb", PREMIUM_FLOOR_SHADOW_DNB_MIN_EDGE, premium_floor=0.03, anchor_book="Pinnacle"
    )
    assert premium_floor_shadow_dnb(
        "dnb", 0.0299, premium_floor=0.03, anchor_book="Betfair Exchange"
    )


def test_predicate_band_boundaries_exclude_outside() -> None:
    # below the pre-registered band floor
    assert not premium_floor_shadow_dnb("dnb", 0.0149, premium_floor=0.03, anchor_book="Pinnacle")
    # at/above the premium floor the candidate IS premium — never marked
    assert not premium_floor_shadow_dnb("dnb", 0.03, premium_floor=0.03, anchor_book="Pinnacle")
    assert not premium_floor_shadow_dnb("dnb", 0.08, premium_floor=0.03, anchor_book="Pinnacle")


def test_predicate_scopes_to_dnb_market_only() -> None:
    for market in ("h2h", "totals", "spreads", "btts", "double_chance"):
        assert not premium_floor_shadow_dnb(
            market, 0.02, premium_floor=0.03, anchor_book="Pinnacle"
        )


def test_predicate_requires_a_genuine_sharp_anchor() -> None:
    # the consensus fallback and blank/soft anchors are NOT the cohort
    assert not premium_floor_shadow_dnb(
        "dnb", 0.02, premium_floor=0.03, anchor_book="consensus(median)"
    )
    assert not premium_floor_shadow_dnb("dnb", 0.02, premium_floor=0.03, anchor_book="")
    assert not premium_floor_shadow_dnb("dnb", 0.02, premium_floor=0.03, anchor_book="SoftBook")


def test_reason_slug_is_the_preregistered_name() -> None:
    assert PREMIUM_FLOOR_SHADOW_DNB_REASON == "premium_floor_shadow_dnb"


# --- 2. pipeline wiring: marker rides the volume pick, tier unchanged --------

_GATE = GatePolicy(
    min_edge=0.0, min_ev=0.0, min_confidence=0.0, max_odds_age_seconds=300, min_liquidity=0.0
)


def _dnb_snap(book: str, sel: str, odds: float) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-dnb",
        bookmaker=book,
        market=Market.DNB,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=20),
        ingested_at=now,
        market_detail="dnb",
    )


class _Loader:
    def __init__(self, snaps: list[OddsSnapshotIn]) -> None:
        self.snaps = snaps
        self.last_fetch_matches: dict[str, int] = {}

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        self.last_fetch_matches[sport_key] = len({s.event_id for s in self.snaps})
        return self.snaps


class _Sink:
    name = "rec"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def _deps(sink: _Sink, loader: _Loader) -> PipelineDeps:
    directory = EventDirectory()
    directory.register(
        "evt-dnb",
        EventTeams(
            home="Home FC",
            away="Away FC",
            league="Test League",
            starts_at=datetime.now(tz=UTC) + timedelta(hours=6),
        ),
    )
    return PipelineDeps(
        loader=loader,
        model=NullModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=_GATE,
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        directory=directory,
        value_min_edge=0.03,
        value_volume_min_edge=0.015,
        value_min_odds=1.30,
        value_policy=ValuePolicy(),
    )


def _sharp_dnb_market(home_soft: float) -> list[OddsSnapshotIn]:
    # Pinnacle anchors the full 2-way DNB (fair Home ~0.48 after devig);
    # SoftBook's Home price sets the edge.
    return [
        _dnb_snap("Pinnacle", "Home FC", 2.00),
        _dnb_snap("Pinnacle", "Away FC", 1.85),
        _dnb_snap("SoftBook", "Home FC", home_soft),
        _dnb_snap("SoftBook", "Away FC", 1.75),
    ]


def _capture_minting(monkeypatch: pytest.MonkeyPatch) -> tuple[list[PickOut], list[tuple]]:
    """Capture minted picks (persist seam) + candidate-audit rows (audit seam)."""
    import app.pipeline as pipeline_mod

    minted: list[PickOut] = []
    audited: list[tuple] = []

    async def _fake_persist(deps: PipelineDeps, pick: PickOut, event_id: str) -> str:
        minted.append(pick)
        return "inserted"

    async def _fake_audit(
        deps: PipelineDeps,
        pick: PickOut,
        market_detail: str,
        reasons: tuple[str, ...],
        anchor_age_seconds: float | None,
        now: datetime,
    ) -> None:
        audited.append((pick.selection, pick.tier, reasons))

    monkeypatch.setattr(pipeline_mod, "_maybe_persist", _fake_persist)
    monkeypatch.setattr(pipeline_mod, "_record_candidate_audit", _fake_audit)
    return minted, audited


async def test_band_member_marked_tier_and_alerting_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Soft 2.17 vs Pinnacle-fair ~0.48 puts the Home edge in [0.015, 0.03):
    # the candidate mints at the VOLUME tier exactly as before AND carries the
    # ADR-0028 cohort marker (note + audit slug). No alert, zero premium picks.
    minted, audited = _capture_minting(monkeypatch)
    sink = _Sink()
    await run_value_pipeline(_deps(sink, _Loader(_sharp_dnb_market(home_soft=2.17))), "soccer")
    assert sink.sent == []  # volume tier: never alerted (unchanged)
    assert LAST_POLL["soccer"]["picks"] == 0  # zero premium picks (unchanged)
    marked = [p for p in minted if PREMIUM_FLOOR_SHADOW_DNB_REASON not in p.reason_summary]
    band = [p for p in minted if PREMIUM_FLOOR_SHADOW_DNB_REASON in p.reason_summary]
    assert len(band) == 1  # exactly the in-band Home candidate is marked
    pick = band[0]
    assert pick.tier == "volume"
    assert pick.selection == "Home FC"
    assert PREMIUM_FLOOR_SHADOW_DNB_MIN_EDGE <= float(pick.edge) < 0.03
    # any other minted (unmarked) pick sits OUTSIDE the band
    for other in marked:
        assert not (PREMIUM_FLOOR_SHADOW_DNB_MIN_EDGE <= float(other.edge) < 0.03)
    # ... and the cohort is queryable via the candidate-audit slug
    assert ("Home FC", "volume", (PREMIUM_FLOOR_SHADOW_DNB_REASON,)) in audited


async def test_above_the_floor_stays_premium_and_unmarked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Soft 2.30 -> edge ~0.045 >= the 0.03 premium floor: the pick is PREMIUM
    # (alerted) and carries NO cohort marker — the band is exclusive at the top.
    minted, audited = _capture_minting(monkeypatch)
    sink = _Sink()
    await run_value_pipeline(_deps(sink, _Loader(_sharp_dnb_market(home_soft=2.30))), "soccer")
    assert LAST_POLL["soccer"]["picks"] == 1
    assert len(sink.sent) == 1
    assert all(PREMIUM_FLOOR_SHADOW_DNB_REASON not in p.reason_summary for p in minted)
    assert all(PREMIUM_FLOOR_SHADOW_DNB_REASON not in reasons for _, _, reasons in audited)


async def test_consensus_anchored_band_member_not_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SAME band edge WITHOUT a sharp book (consensus median anchor) mints at
    # volume as before but is NOT the cohort — the ADR scopes strictly to
    # sharp-anchored DNB.
    minted, audited = _capture_minting(monkeypatch)
    sink = _Sink()
    snaps = [
        _dnb_snap("SoftA", "Home FC", 2.00),
        _dnb_snap("SoftA", "Away FC", 1.85),
        _dnb_snap("SoftB", "Home FC", 2.17),
        _dnb_snap("SoftB", "Away FC", 1.75),
    ]
    await run_value_pipeline(_deps(sink, _Loader(snaps)), "soccer")
    assert sink.sent == []
    assert all(PREMIUM_FLOOR_SHADOW_DNB_REASON not in p.reason_summary for p in minted)
    assert all(PREMIUM_FLOOR_SHADOW_DNB_REASON not in reasons for _, _, reasons in audited)


async def test_non_dnb_band_member_not_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    # A sharp-anchored soccer H2H candidate in the same edge band is NOT the
    # cohort — the marker is scoped to the DNB market only.
    minted, audited = _capture_minting(monkeypatch)
    now = datetime.now(tz=UTC)

    def _h2h(book: str, sel: str, odds: float) -> OddsSnapshotIn:
        return OddsSnapshotIn(
            event_id="evt-dnb",
            bookmaker=book,
            market=Market.H2H,
            selection=sel,
            decimal_odds=odds,
            captured_at=now - timedelta(seconds=20),
            ingested_at=now,
            market_detail=None,
        )

    sink = _Sink()
    snaps = [
        _h2h("Pinnacle", "Home FC", 2.00),
        _h2h("Pinnacle", "Away FC", 1.85),
        _h2h("SoftBook", "Home FC", 2.17),
        _h2h("SoftBook", "Away FC", 1.75),
    ]
    await run_value_pipeline(_deps(sink, _Loader(snaps)), "soccer")
    assert all(PREMIUM_FLOOR_SHADOW_DNB_REASON not in p.reason_summary for p in minted)
    assert all(PREMIUM_FLOOR_SHADOW_DNB_REASON not in reasons for _, _, reasons in audited)
