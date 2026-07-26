"""Betfair staleness guard (P3): tick math, verdict sink, mint-time demotion.

The 8 design cases + the betfair_ticks property tests. Fakes only — no network,
no DB: the pipeline consumes a STUBBED verdict loader (the composition-root
seam), the capture-side sink tests drive ``_compare`` directly with an inert
MockTransport client, and the read-time TTL aggregation is tested through its
pure extraction (``effective_staleness_verdicts``).
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.edge.betfair_ticks import (
    _TICK_LADDER,
    betfair_tick_size,
    tick_distance,
    within_one_tick,
)
from app.edge.gates import GatePolicy
from app.edge.value import CONSENSUS_ANCHOR, _named_sharp_anchor
from app.edge.value_policy import ValuePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.ingestion.betfair_api import (
    VERDICT_DEMOTE,
    VERDICT_NO_API_MATCH,
    VERDICT_NO_API_PRICE,
    VERDICT_PASS,
    AnchorVerdictObservation,
    BetfairApiClient,
    BetfairApiShadowCapture,
    BetfairMatchOdds,
    ReferenceOdds,
    verdict_decision,
)
from app.models.base import NullModel
from app.notifications.base import Alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import LAST_POLL, PipelineDeps, run_value_pipeline
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut
from app.storage.repositories import effective_staleness_verdicts

NOW = datetime.now(tz=UTC)


# --- betfair_ticks property tests (design item 1) ---------------------------


def test_tick_ladder_is_monotone() -> None:
    # Ladder bounds strictly increase and tick sizes never shrink as the
    # price climbs — the structural property the coarser-price convention
    # relies on (max(a, b) can never pick a FINER tick).
    bounds = [upper for upper, _ in _TICK_LADDER]
    ticks = [tick for _, tick in _TICK_LADDER]
    assert bounds == sorted(bounds)
    assert len(set(bounds)) == len(bounds)
    assert ticks == sorted(ticks)
    # Sampled tick size is non-decreasing in price across the whole range.
    prices = [1.01 + i * 0.37 for i in range(2700)]  # 1.01 .. ~1000
    sizes = [betfair_tick_size(p) for p in prices]
    assert all(a <= b for a, b in zip(sizes, sizes[1:], strict=False))


def test_within_one_tick_is_symmetric() -> None:
    samples = [1.01, 1.99, 2.0, 2.02, 2.5, 3.0, 3.98, 5.5, 9.8, 10.5, 19.5, 55.0, 240.0]
    for a in samples:
        for b in samples:
            assert within_one_tick(a, b) == within_one_tick(b, a)
            assert tick_distance(a, b) == tick_distance(b, a)


def test_none_never_agrees_and_never_yields_a_distance() -> None:
    # An absent price is undefined — never a silent "agree", never a distance
    # (and therefore can never demote an anchor).
    assert within_one_tick(None, 2.0) is None
    assert within_one_tick(2.0, None) is None
    assert within_one_tick(None, None) is None
    assert tick_distance(None, 2.0) is None
    assert tick_distance(2.0, None) is None


def test_tick_boundary_exactness() -> None:
    # Design case 6. 2.00 vs 2.02: tick at the coarser price 2.02 is 0.02 ->
    # exactly one tick -> within (pass). 2.00 vs 2.04: two ticks -> demote.
    assert within_one_tick(2.00, 2.02) is True
    assert tick_distance(2.00, 2.02) == pytest.approx(1.0)
    assert within_one_tick(2.00, 2.04) is False
    assert tick_distance(2.00, 2.04) == pytest.approx(2.0)
    # Asymmetric ladder case: 1.99 (tick 0.01) vs 2.02 (tick 0.02) uses the
    # COARSER tick 0.02 -> 0.03/0.02 = 1.5 ticks (not 3.0 at the finer tick).
    assert tick_distance(1.99, 2.02) == pytest.approx(1.5)
    assert within_one_tick(1.99, 2.02) is False


def test_verdict_decision_enum() -> None:
    assert verdict_decision(2.00, 2.02, max_ticks=1.0) == VERDICT_PASS
    assert verdict_decision(2.00, 2.04, max_ticks=1.0) == VERDICT_DEMOTE
    assert verdict_decision(2.00, None, max_ticks=1.0) == VERDICT_NO_API_PRICE
    assert verdict_decision(None, 2.00, max_ticks=1.0) == VERDICT_NO_API_MATCH
    # A wider threshold widens the pass band (2 ticks apart, threshold 2.0).
    assert verdict_decision(2.00, 2.04, max_ticks=2.0) == VERDICT_PASS


# --- read-time TTL aggregation (design case 4, the direction check) ---------


def test_stale_demote_verdict_reads_as_stale_api_never_demote() -> None:
    # Even a 10-tick disagreement must NOT demote once the verdict is older
    # than the TTL: the API could have been down for hours; only FRESH
    # evidence demotes. Over-TTL rows read as 'stale_api' (a mint no-op).
    stale_at = NOW - timedelta(seconds=3600)
    out = effective_staleness_verdicts([("evt-1", "demote", stale_at)], ttl_seconds=900.0, now=NOW)
    assert out == {"evt-1": "stale_api"}


def test_fresh_demote_wins_and_missing_capture_time_is_stale() -> None:
    fresh_at = NOW - timedelta(seconds=60)
    out = effective_staleness_verdicts(
        [
            ("evt-1", "pass", fresh_at),
            ("evt-1", "demote", fresh_at),  # any fresh demote wins
            ("evt-2", "no_api_price", fresh_at),  # fresh non-compare passes through
            ("evt-3", "pass", None),  # unknown capture time can never be fresh
        ],
        ttl_seconds=900.0,
        now=NOW,
    )
    assert out == {"evt-1": "demote", "evt-2": "no_api_price", "evt-3": "stale_api"}


# --- pure-layer demotion mechanics (design cases 2/3) ------------------------

_PRICES = {
    "Home FC": {"Betfair Exchange": 2.50, "Pinnacle": 2.48, "SoftA": 2.90},
    "Draw": {"Betfair Exchange": 3.30, "Pinnacle": 3.28, "SoftA": 3.20},
    "Away FC": {"Betfair Exchange": 3.10, "Pinnacle": 3.08, "SoftA": 2.95},
}
_SELECTIONS = ["Home FC", "Draw", "Away FC"]
_COMMISSIONS = {"betfair exchange": 0.05}


def test_demoted_exchange_falls_to_next_sharp_book() -> None:
    # Design case 2: with the exchange preferred FIRST, a demotion skips it and
    # the next sharp-book member (Pinnacle) anchors — the exact `continue`
    # fall-through the liquidity floor uses (never a hard drop).
    order = ("betfair exchange", "pinnacle")
    book, odds, miss = _named_sharp_anchor(
        _PRICES, _SELECTIONS, order, _COMMISSIONS, 0.12, exchange_demoted=False
    )
    assert book == "Betfair Exchange"
    assert miss is None
    book, odds, miss = _named_sharp_anchor(
        _PRICES, _SELECTIONS, order, _COMMISSIONS, 0.12, exchange_demoted=True
    )
    assert book == "Pinnacle"
    assert odds == [2.48, 3.28, 3.08]
    assert miss is None  # a later sharp book anchored — no miss


def test_demoted_exchange_with_no_other_sharp_returns_none_for_consensus() -> None:
    # Design case 3 (pure half): no other sharp book -> the named-anchor loop
    # yields nothing and the caller falls to _consensus_anchor.
    book, odds, miss = _named_sharp_anchor(
        _PRICES, _SELECTIONS, ("betfair exchange",), _COMMISSIONS, 0.12, exchange_demoted=True
    )
    assert book is None
    assert odds is None
    assert miss == "exchange_demoted"  # sub-reason names the demoting guard


# --- pipeline harness (fakes only, mirrors tests/test_value_pipeline.py) ----

_GATE = GatePolicy(
    min_edge=0.0, min_ev=0.0, min_confidence=0.0, max_odds_age_seconds=300, min_liquidity=0.0
)


def _snap(book: str, sel: str, odds: float) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-1",
        bookmaker=book,
        market=Market.H2H,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=30),
        ingested_at=now,
    )


def _market_snapshots() -> list[OddsSnapshotIn]:
    # Betfair Exchange is the only sharp book (anchors when not demoted);
    # three soft books so the consensus fallback exists after a demotion.
    return [
        _snap("Betfair Exchange", "Home FC", 2.50),
        _snap("Betfair Exchange", "Draw", 3.30),
        _snap("Betfair Exchange", "Away FC", 3.10),
        _snap("SoftA", "Home FC", 2.90),
        _snap("SoftA", "Draw", 3.20),
        _snap("SoftA", "Away FC", 2.95),
        _snap("SoftB", "Home FC", 2.60),
        _snap("SoftB", "Draw", 3.30),
        _snap("SoftB", "Away FC", 3.05),
        _snap("SoftC", "Home FC", 2.62),
        _snap("SoftC", "Draw", 3.25),
        _snap("SoftC", "Away FC", 3.00),
    ]


class _Loader:
    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        self.snapshots = snapshots
        self.last_fetch_matches: dict[str, int] = {}
        self.last_fetch_event_ids: dict[str, tuple[str, ...]] = {}

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        self.last_fetch_matches[sport_key] = len({s.event_id for s in self.snapshots})
        return self.snapshots


class _Sink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


class _VerdictLoader:
    """Stub of PipelineDeps.staleness_verdict_loader (the DB-read seam)."""

    def __init__(self, verdicts: Mapping[str, str] | None = None, *, error: bool = False) -> None:
        self.verdicts = dict(verdicts or {})
        self.error = error
        self.calls = 0

    async def __call__(self, sport_key: str) -> Mapping[str, str]:
        self.calls += 1
        if self.error:
            raise RuntimeError("verdict DB read failed")
        return self.verdicts


def _deps(
    sink: _Sink,
    loader: _Loader,
    *,
    policy: ValuePolicy,
    verdict_loader: _VerdictLoader | None,
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
        gate_policy=_GATE,
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        directory=directory,
        value_min_edge=0.015,
        value_volume_min_edge=0.005,
        value_min_odds=1.30,
        value_policy=policy,
        staleness_verdict_loader=verdict_loader,
    )


_GUARD_ENFORCE = ValuePolicy(betfair_staleness_guard=True, betfair_staleness_shadow=False)
_GUARD_SHADOW = ValuePolicy(betfair_staleness_guard=True, betfair_staleness_shadow=True)


async def test_fresh_agree_pass_keeps_the_betfair_anchor() -> None:
    # Design case 1: a fresh 'pass' verdict changes nothing — Betfair anchors,
    # the pick alerts, and the pass verdict is stamped (observability).
    sink = _Sink()
    deps = _deps(
        sink,
        _Loader(_market_snapshots()),
        policy=_GUARD_ENFORCE,
        verdict_loader=_VerdictLoader({"evt-1": "pass"}),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].anchor_book == "Betfair Exchange"
    assert picks[0].anchor_staleness_decision == "pass"
    assert len(sink.sent) == 1


async def test_fresh_disagree_demotes_to_consensus_volume_never_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Design cases 2+3 (pipeline half): a fresh 'demote' verdict under
    # require_sharp_anchor drops the exchange anchor -> consensus fair ->
    # the pick mints at the VOLUME (shadow) tier — never alerted, never
    # premium, but NEVER silently dropped (captured via the persist seam).
    minted: list[PickOut] = []

    async def _fake_persist(deps: PipelineDeps, pick: PickOut, event_id: str) -> str:
        minted.append(pick)
        return "inserted"

    import app.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_maybe_persist", _fake_persist)
    sink = _Sink()
    policy = ValuePolicy(
        betfair_staleness_guard=True, betfair_staleness_shadow=False, require_sharp_anchor=True
    )
    deps = _deps(
        sink,
        _Loader(_market_snapshots()),
        policy=policy,
        verdict_loader=_VerdictLoader({"evt-1": "demote"}),
    )
    await run_value_pipeline(deps, "soccer")
    assert sink.sent == []  # consensus-anchored => volume tier => never alerted
    assert LAST_POLL["soccer"]["picks"] == 0
    assert len(minted) == 1  # ... but the pick still minted (shadow, no drop)
    pick = minted[0]
    assert pick.tier == "volume"
    assert pick.anchor_book == CONSENSUS_ANCHOR
    assert pick.anchor_staleness_decision == "demote"


async def test_stale_api_verdict_never_demotes_at_mint() -> None:
    # Design case 4 (pipeline half): the loader classified the verdict as
    # over-TTL ('stale_api') — no matter what disagreement it stored, the
    # anchor passes exactly as today; the stamp records the stale read.
    sink = _Sink()
    deps = _deps(
        sink,
        _Loader(_market_snapshots()),
        policy=_GUARD_ENFORCE,
        verdict_loader=_VerdictLoader({"evt-1": "stale_api"}),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].anchor_book == "Betfair Exchange"
    assert picks[0].anchor_staleness_decision == "stale_api"
    assert len(sink.sent) == 1


async def test_no_api_match_and_no_api_price_are_noops() -> None:
    # Design case 5: missing-evidence decisions never alter anchoring; the
    # decision is stamped for observability.
    for decision in ("no_api_match", "no_api_price"):
        sink = _Sink()
        deps = _deps(
            sink,
            _Loader(_market_snapshots()),
            policy=_GUARD_ENFORCE,
            verdict_loader=_VerdictLoader({"evt-1": decision}),
        )
        picks = await run_value_pipeline(deps, "soccer")
        assert len(picks) == 1
        assert picks[0].anchor_book == "Betfair Exchange"
        assert picks[0].anchor_staleness_decision == decision


async def test_shadow_mode_logs_would_demote_but_never_alters_anchoring() -> None:
    # Design case 7: SHADOW (the rollout default) with a fresh 'demote' —
    # the anchor and tier are IDENTICAL to guard-off; the would-demote rides
    # the pick stamp only.
    baseline_sink = _Sink()
    baseline = _deps(
        baseline_sink,
        _Loader(_market_snapshots()),
        policy=ValuePolicy(),
        verdict_loader=None,
    )
    baseline_picks = await run_value_pipeline(baseline, "soccer")

    sink = _Sink()
    deps = _deps(
        sink,
        _Loader(_market_snapshots()),
        policy=_GUARD_SHADOW,
        verdict_loader=_VerdictLoader({"evt-1": "demote"}),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == len(baseline_picks) == 1
    assert picks[0].anchor_book == baseline_picks[0].anchor_book == "Betfair Exchange"
    assert picks[0].tier == baseline_picks[0].tier  # tier identical to guard-off
    assert picks[0].edge == pytest.approx(baseline_picks[0].edge)
    assert picks[0].anchor_staleness_decision == "demote"  # the would-demote stamp
    assert baseline_picks[0].anchor_staleness_decision is None
    assert len(sink.sent) == 1  # still alerted — shadow never suppresses


async def test_guard_off_never_calls_the_loader_and_is_byte_identical() -> None:
    # Design case 8a: flag off => the verdict set is NEVER loaded and the
    # pick is identical to today (no stamp).
    verdict_loader = _VerdictLoader({"evt-1": "demote"})
    sink = _Sink()
    deps = _deps(
        sink,
        _Loader(_market_snapshots()),
        policy=ValuePolicy(),  # guard off (default)
        verdict_loader=verdict_loader,
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert verdict_loader.calls == 0
    assert len(picks) == 1
    assert picks[0].anchor_book == "Betfair Exchange"
    assert picks[0].anchor_staleness_decision is None


async def test_loader_failure_never_blocks_minting() -> None:
    # Design case 8b: a DB-read failure yields an empty verdict map (type-only
    # log) and the cycle proceeds byte-identical — never a blocked mint.
    sink = _Sink()
    deps = _deps(
        sink,
        _Loader(_market_snapshots()),
        policy=_GUARD_ENFORCE,
        verdict_loader=_VerdictLoader(error=True),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].anchor_book == "Betfair Exchange"
    assert picks[0].anchor_staleness_decision is None
    assert len(sink.sent) == 1


# --- capture-side verdict sink (write path; fakes only, no network) ---------


def _capture(
    verdict_sink: Callable[[Sequence[AnchorVerdictObservation]], Awaitable[None]],
    reference: ReferenceOdds | None,
) -> BetfairApiShadowCapture:
    client = BetfairApiClient(
        app_key="k",
        username="u",
        password="p",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ),
    )

    async def _reference(ref: str) -> ReferenceOdds | None:
        return reference

    return BetfairApiShadowCapture(
        client,
        candidates_fn=lambda: [],
        window=timedelta(hours=1),
        reference_odds_fn=_reference,
        verdict_sink=verdict_sink,
        verdict_ticks=1.0,
    )


def _match_odds(
    home_back: float | None, draw_back: float | None, away_back: float | None
) -> BetfairMatchOdds:
    return BetfairMatchOdds(
        market_id="1.1",
        event_id="31",
        competition="League",
        kickoff=NOW + timedelta(hours=2),
        home="Home FC",
        away="Away FC",
        home_back=home_back,
        draw_back=draw_back,
        away_back=away_back,
        home_back_size=120.0,
        draw_back_size=None,
        away_back_size=44.5,
    )


async def test_compare_routes_per_selection_verdicts_to_the_sink() -> None:
    recorded: list[AnchorVerdictObservation] = []

    async def sink(observations: Sequence[AnchorVerdictObservation]) -> None:
        recorded.extend(observations)

    reference = ReferenceOdds(
        home_back=2.50,
        draw_back=None,  # inline anchor missing the draw
        away_back=2.60,
        captured_at=NOW - timedelta(hours=3),
    )
    capture = _capture(sink, reference)
    # home: 2.56 vs 2.50 -> 3 ticks at the coarser 0.02 tick -> demote;
    # draw: API priced, no inline row -> no_api_match;
    # away: no API price -> no_api_price.
    odds = _match_odds(2.56, 3.40, None)
    await capture._compare([(odds, "https://op/match-1")], NOW)
    by_role = {obs.selection_role: obs for obs in recorded}
    assert set(by_role) == {"home", "draw", "away"}
    assert by_role["home"].decision == VERDICT_DEMOTE
    assert by_role["home"].tick_diff == pytest.approx(3.0)
    assert by_role["home"].api_best_back_size == pytest.approx(120.0)
    assert by_role["home"].inline_captured_at == reference.captured_at
    assert by_role["home"].api_captured_at == NOW
    assert by_role["draw"].decision == VERDICT_NO_API_MATCH
    assert by_role["draw"].tick_diff is None
    assert by_role["away"].decision == VERDICT_NO_API_PRICE
    assert by_role["away"].api_best_back_size == pytest.approx(44.5)
    # every observation keys the canonical event + h2h market
    assert all(obs.event_ref == "https://op/match-1" for obs in recorded)
    assert all(obs.market == "h2h" for obs in recorded)


async def test_within_tick_prices_write_pass_verdicts() -> None:
    recorded: list[AnchorVerdictObservation] = []

    async def sink(observations: Sequence[AnchorVerdictObservation]) -> None:
        recorded.extend(observations)

    reference = ReferenceOdds(
        home_back=2.50, draw_back=3.30, away_back=3.10, captured_at=NOW - timedelta(hours=2)
    )
    capture = _capture(sink, reference)
    odds = _match_odds(2.52, 3.30, 3.15)  # all within one tick at the coarser price
    await capture._compare([(odds, "https://op/match-2")], NOW)
    assert [obs.decision for obs in recorded] == [VERDICT_PASS] * 3


async def test_verdict_sink_failure_never_breaks_the_capture() -> None:
    async def sink(observations: Sequence[AnchorVerdictObservation]) -> None:
        raise RuntimeError("db down")

    reference = ReferenceOdds(
        home_back=2.50, draw_back=3.30, away_back=3.10, captured_at=NOW - timedelta(hours=2)
    )
    capture = _capture(sink, reference)
    aggregate = await capture._compare([(_match_odds(2.52, 3.30, 3.15), "https://op/m")], NOW)
    assert aggregate is not None  # the compare completed despite the sink error
    assert aggregate.compared == 1
