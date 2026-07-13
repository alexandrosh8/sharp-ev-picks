"""End-to-end pipeline cycle with fakes: snapshots -> devig -> model -> gates
-> stake -> alert. No network, no DB."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.edge.gates import GatePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import PredictedProbability
from app.notifications.base import Alert, build_pick_alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import PipelineDeps, run_pick_pipeline
from app.probabilities.devig import DevigMethod
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

NOW = datetime.now(tz=UTC)

POLICY = GatePolicy(
    min_edge=0.03,
    min_ev=0.01,
    min_confidence=0.60,
    max_odds_age_seconds=300,
    min_liquidity=0.0,
)


class FakeLoader:
    """Two-way totals book: Over/Under both at 2.10 (fair ~0.50 each)."""

    def __init__(self) -> None:
        self.snapshots = [
            OddsSnapshotIn(
                event_id="evt-1",
                bookmaker="bookie",
                market=Market.TOTALS,
                selection=name,
                decimal_odds=2.10,
                captured_at=NOW - timedelta(seconds=30),
                ingested_at=NOW,
            )
            for name in ("Over 2.5", "Under 2.5")
        ]

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        return self.snapshots


class FakeModel:
    name = "fake"
    version = "1"

    async def predict(self, event_id: str) -> Sequence[PredictedProbability]:
        return (
            PredictedProbability(
                market=Market.TOTALS, selection="Over 2.5", probability=0.58, confidence=0.8
            ),
        )


class RecordingSink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def make_deps(sink: RecordingSink) -> PipelineDeps:
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
        loader=FakeLoader(),
        model=FakeModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=POLICY,
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        devig_method=DevigMethod.MULTIPLICATIVE,
        directory=directory,
    )


def test_non_settleable_market_details_are_filtered() -> None:
    """Period / corner / card sub-markets have no score in the results feed, so a
    pick on them can only ever void — the candidate gate must reject them while
    passing the full-match markets the settler can grade."""
    from app.pipeline import _is_settleable_market_detail

    assert _is_settleable_market_detail(None) is True
    assert _is_settleable_market_detail("over_under_2_5") is True
    assert _is_settleable_market_detail("spreads_minus_1_5") is True
    assert _is_settleable_market_detail("asian_handicap_minus_0_5") is True
    assert _is_settleable_market_detail("totals_1st_half_0_5") is False
    assert _is_settleable_market_detail("totals_2nd_half_1") is False
    assert _is_settleable_market_detail("h2h_1st_quarter") is False
    assert _is_settleable_market_detail("spreads_4th_quarter_minus_5_5") is False
    assert _is_settleable_market_detail("totals_1st_set_5_5") is False
    assert _is_settleable_market_detail("oc_total_corners_3_5") is False
    assert _is_settleable_market_detail("oc_cards_over_3_5") is False


def test_tennis_game_line_groups_are_filtered() -> None:
    """Our tennis results feed carries SET scores only, so a totals/spreads
    candidate on a GAME-sized line (totals > 4.5, |spread| > 2.5) can never be
    auto-settled honestly — the candidate gate must drop it while keeping
    set-plausible tennis lines and every non-tennis sport untouched."""
    from app.pipeline import _is_tennis_game_line_group
    from app.schemas.base import Market

    games_total = {"Over 22.5": {"b": 1.9}, "Under 22.5": {"b": 1.9}}
    sets_total = {"Over 2.5": {"b": 1.9}, "Under 2.5": {"b": 1.9}}
    games_spread = {"Karolina Muchova -4.5": {"b": 1.9}}
    sets_spread = {"Karolina Muchova -1.5": {"b": 1.9}}
    assert _is_tennis_game_line_group("tennis", Market.TOTALS, games_total) is True
    assert _is_tennis_game_line_group("tennis", Market.TOTALS, sets_total) is False
    assert _is_tennis_game_line_group("tennis", Market.SPREADS, games_spread) is True
    assert _is_tennis_game_line_group("tennis", Market.SPREADS, sets_spread) is False
    # Non-tennis sports and non-line markets are never dropped by this gate
    # (soccer corner totals are already handled by the detail gate above).
    assert _is_tennis_game_line_group("soccer", Market.TOTALS, games_total) is False
    assert _is_tennis_game_line_group("basketball", Market.TOTALS, games_total) is False
    assert _is_tennis_game_line_group("tennis", Market.H2H, {"A": {"b": 2.0}}) is False


async def test_pipeline_produces_pick_and_alert() -> None:
    sink = RecordingSink()
    picks = await run_pick_pipeline(make_deps(sink), "soccer_epl")
    # fair prob = 0.5 each; model 0.58 -> edge 0.08, EV = 0.58*1.1-0.42 = 0.218
    assert len(picks) == 1
    pick = picks[0]
    assert pick.selection == "Over 2.5"
    assert pick.edge > 0.03
    assert pick.ev > 0.01
    assert pick.recommended_stake_fraction <= 0.02
    assert len(sink.sent) == 1
    assert "you place any bet" in sink.sent[0].body  # mandatory decision-support footer


async def test_model_pipeline_nets_exchange_commission_for_ev_and_stake() -> None:
    """Exchange-priced picks must gate EV and size Kelly on commission-netted
    odds (audit 2026-07-09), mirroring the value strategy's effective_odds."""
    from app.risk.staking import kelly_fraction

    sink = RecordingSink()
    deps = make_deps(sink)
    deps.gate_policy = GatePolicy(
        min_edge=0.03,
        min_ev=0.01,
        min_confidence=0.60,
        max_odds_age_seconds=300,
        min_liquidity=0.0,
        commission_by_book=(("betfair exchange", 0.05),),
    )
    for snap in deps.loader.snapshots:  # type: ignore[attr-defined]
        object.__setattr__(snap, "bookmaker", "Betfair Exchange")
    picks = await run_pick_pipeline(deps, "soccer_epl")
    assert len(picks) == 1
    pick = picks[0]
    # d=2.10 at 5% commission -> d_eff = 2.045; EV = 0.58*1.045 - 0.42 = 0.1861
    assert pick.decimal_odds == pytest.approx(2.10)  # displayed price stays gross
    assert pick.ev == pytest.approx(0.58 * 1.045 - 0.42, abs=1e-12)
    assert pick.stake_breakdown is not None
    assert pick.stake_breakdown.raw_kelly == pytest.approx(kelly_fraction(0.58, 2.045), abs=1e-12)


async def test_model_pipeline_drops_future_captured_at() -> None:
    # ``now`` is taken AFTER the fetch, so a snapshot stamped in the FUTURE is a
    # clock/data error (provider clock skew), never a fresh price. The odds-age
    # gate must DROP it (age +inf, fail closed — mirroring the value path's
    # _candidate_age_seconds), not let the raw negative age sail through.
    sink = RecordingSink()
    deps = make_deps(sink)
    future = datetime.now(tz=UTC) + timedelta(seconds=90)
    deps.loader.snapshots = [  # type: ignore[attr-defined]
        s.model_copy(update={"captured_at": future})
        for s in deps.loader.snapshots  # type: ignore[attr-defined]
    ]
    picks = await run_pick_pipeline(deps, "soccer_epl")
    assert picks == []
    assert sink.sent == []


async def test_model_pipeline_alert_key_includes_strategy_identity() -> None:
    sink = RecordingSink()
    deps = make_deps(sink)
    deps.model_name = "football-dixon-coles"
    deps.model_version = "v4"

    picks = await run_pick_pipeline(deps, "soccer_epl")

    assert len(picks) == 1
    expected = build_pick_alert(
        picks[0],
        model_name=deps.model_name,
        model_version=deps.model_version,
    )
    assert sink.sent[0].dedupe_key == expected.dedupe_key


async def test_pipeline_rerun_suppresses_duplicate_alert() -> None:
    sink = RecordingSink()
    deps = make_deps(sink)
    first = await run_pick_pipeline(deps, "soccer_epl")
    second = await run_pick_pipeline(deps, "soccer_epl")
    # the edge is re-detected each cycle (pick produced) but the alert key is
    # stable market state, so only ONE alert reaches the sink
    assert len(first) == 1
    assert len(second) == 1
    assert len(sink.sent) == 1


async def test_model_pipeline_stamps_polled_sport_not_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: run_pick_pipeline stamped persisted snapshots + picks with the
    # (now removed) deps.sport default "soccer" even for a basketball poll.
    import app.pipeline as pl

    captured: dict[str, str] = {}

    async def spy_persist(deps, snapshots, sport, league, now):  # type: ignore[no-untyped-def]
        captured["sport"] = sport
        return 0

    monkeypatch.setattr(pl, "_persist_snapshots", spy_persist)
    picks = await run_pick_pipeline(make_deps(RecordingSink()), "basketball")
    assert captured["sport"] == "basketball"  # the warehouse persist arg
    assert len(picks) == 1
    assert picks[0].sport == "basketball"  # PickOut.sport


class FakeSessionFactory:
    """Minimal async-contextmanager session for the persistence seam."""

    def __call__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
        return False

    async def commit(self) -> None:
        return None


def patch_persist_dedupe_after_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """persist_pick inserts on the FIRST call, dedupes every later call —
    the DB unique key (event, market, selection, model) ignores odds."""
    import app.storage.repositories as repos

    calls = {"n": 0}

    async def fake_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return "inserted" if calls["n"] == 1 else "duplicate"

    async def fake_update_pick_stake(*args, **kwargs):  # type: ignore[no-untyped-def]
        return True

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)
    monkeypatch.setattr(repos, "update_pick_stake", fake_update_pick_stake)


def make_persisting_deps(sink: RecordingSink) -> PipelineDeps:
    deps = make_deps(sink)
    directory = EventDirectory()
    directory.register(
        "evt-1",
        EventTeams(
            home="Over Town",
            away="Under City",
            starts_at=NOW + timedelta(hours=6),
        ),
    )
    deps.directory = directory
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    return deps


async def test_model_cycle_cancellation_waits_for_persist_and_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model call site must finish its atomic DB/ledger child before the
    cancelled cycle returns control to scheduler teardown."""
    import asyncio

    import app.storage.repositories as repos

    update_started = asyncio.Event()
    release_update = asyncio.Event()
    update_finished = asyncio.Event()
    persisted: list[str] = []

    async def fake_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        persisted.append(pick.event_id)
        return "inserted"

    async def blocking_update_pick_stake(*args, **kwargs):  # type: ignore[no-untyped-def]
        update_started.set()
        await release_update.wait()
        update_finished.set()
        return True

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)
    monkeypatch.setattr(repos, "update_pick_stake", blocking_update_pick_stake)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)
    day = datetime.now(tz=UTC).date()
    task = asyncio.create_task(run_pick_pipeline(deps, "soccer_epl"))
    await asyncio.wait_for(update_started.wait(), timeout=5.0)
    reserved = deps.ledger.used(day)
    assert reserved > 0.0

    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    assert not update_finished.is_set()
    assert deps.ledger.used(day) == pytest.approx(reserved)

    release_update.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert update_finished.is_set()
    assert persisted == ["evt-1"]
    assert deps.ledger.used(day) == pytest.approx(reserved)
    assert sink.sent == []


async def test_pick_pipeline_duplicate_releases_exposure_and_unchanged_odds_stay_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port of the value-pipeline H1 regression to run_pick_pipeline: a DB
    duplicate hands its exposure grant back (no daily-cap leak) but the alert
    is still dispatched — unchanged odds are suppressed by the idempotency
    store, so exactly one alert reaches the sink."""
    patch_persist_dedupe_after_first(monkeypatch)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)

    day = datetime.now(tz=UTC).date()
    first = await run_pick_pipeline(deps, "soccer_epl")
    assert len(first) == 1
    used_after_first = deps.ledger.used(day)
    assert used_after_first > 0.0

    second = await run_pick_pipeline(deps, "soccer_epl")
    assert second == []  # duplicate is not a new pick this cycle
    assert deps.ledger.used(day) == pytest.approx(used_after_first)  # grant returned
    assert len(sink.sent) == 1  # idempotency (key includes odds) suppressed it


async def test_pick_pipeline_duplicate_price_move_realerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A price move on a pick the DB already knows must re-alert (the alert
    dedupe key includes decimal_odds by design) while the exposure grant for
    the duplicate is still released."""
    patch_persist_dedupe_after_first(monkeypatch)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)

    day = datetime.now(tz=UTC).date()
    first = await run_pick_pipeline(deps, "soccer_epl")
    assert len(first) == 1
    assert len(sink.sent) == 1
    used_after_first = deps.ledger.used(day)

    # the book moves both totals prices 2.10 -> 2.20: same DB row, new state
    deps.loader.snapshots = [  # type: ignore[attr-defined]
        OddsSnapshotIn(
            event_id="evt-1",
            bookmaker="bookie",
            market=Market.TOTALS,
            selection=name,
            decimal_odds=2.20,
            captured_at=NOW - timedelta(seconds=30),
            ingested_at=NOW,
        )
        for name in ("Over 2.5", "Under 2.5")
    ]
    second = await run_pick_pipeline(deps, "soccer_epl")
    assert second == []  # still not a NEW pick
    assert deps.ledger.used(day) == pytest.approx(used_after_first)  # grant returned
    assert len(sink.sent) == 2  # price move re-alerted
    assert "2.20" in sink.sent[1].title


async def test_model_pipeline_withholds_picks_from_incomplete_source_cycle() -> None:
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps(sink)
    deps.loader.last_fetch_complete = {"soccer_epl": False}  # type: ignore[attr-defined]
    deps.loader.last_fetch_completeness_reason = {  # type: ignore[attr-defined]
        "soccer_epl": "row count 2 below completeness floor"
    }

    assert await run_pick_pipeline(deps, "soccer_epl") == []
    assert sink.sent == []
    poll = LAST_POLL["soccer_epl"]
    assert poll["degraded"] is True
    assert poll["source_complete"] is False
    assert poll["completeness_reason"] == "row count 2 below completeness floor"
    assert poll["snapshots"] == 2  # partial evidence remains visible


async def test_model_pipeline_drops_post_kickoff_snapshot() -> None:
    """Post-kickoff leakage guard parity with run_value_pipeline (shared
    helpers): a snapshot captured AT OR AFTER its event's kickoff is an in-play
    price and must mint NO model pick and send NO alert — the operator cannot
    take a pre-match price on a started game. Mirrors
    test_value_pipeline_skips_started_events for PICK_STRATEGY=model."""
    sink = RecordingSink()
    deps = make_deps(sink)
    directory = EventDirectory()
    directory.register(
        "evt-1",
        EventTeams(home="Over Town", away="Under City", starts_at=NOW - timedelta(minutes=20)),
    )
    deps.directory = directory
    picks = await run_pick_pipeline(deps, "soccer_epl")
    assert picks == []
    assert sink.sent == []
    assert deps.ledger.used(datetime.now(tz=UTC).date()) == 0.0


async def test_model_pipeline_keeps_pre_kickoff_snapshot() -> None:
    """A known, strictly-future kickoff proves the quote is pre-game."""
    sink = RecordingSink()
    deps = make_deps(sink)
    directory = EventDirectory()
    directory.register(
        "evt-1",
        EventTeams(home="Over Town", away="Under City", starts_at=NOW + timedelta(hours=3)),
    )
    deps.directory = directory
    picks = await run_pick_pipeline(deps, "soccer_epl")
    assert len(picks) == 1
    assert len(sink.sent) == 1


@pytest.mark.parametrize("kickoff_state", ["absent", "none"])
async def test_model_pipeline_rejects_unknown_kickoff(kickoff_state: str) -> None:
    """Absent/NULL kickoff cannot prove a quote is actionable pre-game."""
    sink = RecordingSink()
    deps = make_deps(sink)
    directory = EventDirectory()
    if kickoff_state == "none":
        directory.register(
            "evt-1",
            EventTeams(home="Over Town", away="Under City", starts_at=None),
        )
    deps.directory = directory

    picks = await run_pick_pipeline(deps, "soccer_epl")

    assert picks == []
    assert sink.sent == []
    assert deps.ledger.used(datetime.now(tz=UTC).date()) == 0.0


async def test_unpersisted_premium_pick_does_not_accumulate_exposure() -> None:
    """kelly-risk-r2-1: with persistence unavailable (no session factory),
    _maybe_persist returns 'unpersisted'. A premium pick re-detected every
    cycle must NOT accumulate standing daily exposure — otherwise a sustained-
    unpersisted pick silently exhausts the 5% cap and suppresses later alerts.
    The pick still flows (alerted) but reserves nothing it can never release."""
    sink = RecordingSink()
    deps = make_deps(sink)  # no session_factory -> outcome == "unpersisted"
    day = datetime.now(tz=UTC).date()

    first = await run_pick_pipeline(deps, "soccer_epl")
    assert len(first) == 1
    assert deps.ledger.used(day) == 0.0  # unpersisted reserves NOTHING

    second = await run_pick_pipeline(deps, "soccer_epl")
    assert len(second) == 1
    assert deps.ledger.used(day) == 0.0  # still zero -> no cross-cycle accumulation


async def test_duplicate_realert_uses_persisted_stake_even_when_cap_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kr-1: an already-persisted pick re-detected as a DB duplicate must
    re-alert with the stake from its persisted row (breakdown.final, never
    daily-clipped) and must NOT be skipped just because the daily cap is now
    exhausted (a fresh reserve would grant 0)."""
    patch_persist_dedupe_after_first(monkeypatch)

    import app.pipeline as pl

    real_build = pl.build_pick_alert
    captured: list[float] = []

    def spy_build(pick, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(pick.recommended_stake_fraction)
        return real_build(pick, *args, **kwargs)

    monkeypatch.setattr(pl, "build_pick_alert", spy_build)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)
    day = datetime.now(tz=UTC).date()

    first = await run_pick_pipeline(deps, "soccer_epl")
    assert len(first) == 1
    persisted_stake = first[0].recommended_stake_fraction
    assert persisted_stake > 0.0
    assert first[0].stake_breakdown.daily_clipped is False

    # Exhaust the rest of the daily cap so a fresh reserve would grant ~0.
    deps.ledger.reserve(day, deps.ledger.remaining(day))
    assert deps.ledger.remaining(day) == pytest.approx(0.0)

    second = await run_pick_pipeline(deps, "soccer_epl")
    assert second == []  # a duplicate is not a NEW pick this cycle
    assert len(captured) == 2  # re-alert NOT skipped despite the exhausted cap
    # the re-alert stake equals the persisted row's stake, not a daily-clipped 0
    assert captured[1] == pytest.approx(persisted_stake)


async def test_daily_clip_persists_clipped_stake(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG 2: when the daily-exposure cap clips an INSERTED pick's stake at
    reservation, the value persisted to the DB row must be the CLIPPED
    (actually-reserved) stake, not the pre-clip per-bet-capped Kelly amount.
    Otherwise the persisted/reported stake escapes the daily cap and the
    ledger's reserved-vs-persisted diverge."""
    import app.storage.repositories as repos

    async def fake_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        return "inserted"

    persisted_clips: list[float] = []

    async def spy_update_pick_stake(  # type: ignore[no-untyped-def]
        session, pick, teams, model_name, model_version, *, persist_tier=False, **kwargs
    ):
        persisted_clips.append(pick.recommended_stake_fraction)
        return True

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)
    monkeypatch.setattr(repos, "update_pick_stake", spy_update_pick_stake)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)
    day = datetime.now(tz=UTC).date()

    # Pre-exhaust the daily cap to leave only 0.005 room. The pick is per-bet
    # capped at 0.02, so the reservation clips it down to 0.005.
    deps.ledger.reserve(day, deps.ledger.remaining(day) - 0.005)
    remaining_at_reservation = deps.ledger.remaining(day)
    assert remaining_at_reservation == pytest.approx(0.005)

    picks = await run_pick_pipeline(deps, "soccer_epl")
    assert len(picks) == 1
    pick = picks[0]
    # the returned (alerted) pick reflects the daily clip ...
    assert pick.recommended_stake_fraction == pytest.approx(0.005)
    assert pick.stake_breakdown.daily_clipped is True
    # ... and the SAME clipped stake was persisted back to the row
    assert persisted_clips == [pytest.approx(0.005)]
    # property: the persisted stake never exceeds the daily room at reservation
    assert persisted_clips[0] <= remaining_at_reservation + 1e-12


async def test_uncapped_pick_finalizes_durable_exposure_in_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even without clipping, the final stake update persists the durable
    exposure charge in the same transaction as the inserted pick."""
    import app.storage.repositories as repos

    async def fake_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        return "inserted"

    update_calls: list[float] = []

    async def spy_update_pick_stake(  # type: ignore[no-untyped-def]
        session, pick, teams, model_name, model_version, *, persist_tier=False, **kwargs
    ):
        update_calls.append(pick.recommended_stake_fraction)
        return True

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)
    monkeypatch.setattr(repos, "update_pick_stake", spy_update_pick_stake)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)  # full 0.05 daily room, pick caps at 0.02

    picks = await run_pick_pipeline(deps, "soccer_epl")
    assert len(picks) == 1
    assert picks[0].stake_breakdown.daily_clipped is False
    assert update_calls == [pytest.approx(picks[0].recommended_stake_fraction)]


async def test_cap_denied_inserted_pick_zeroes_stake_and_never_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WP2 mirror of the value-pipeline daily-cap bypass: an INSERTED pick the
    exhausted cap denies (granted == 0) was already persisted at full stake —
    the row must be zeroed (cap-denial marker), no alert this cycle, and the
    next cycle's 'duplicate_denied' re-detection must stay silent too."""
    import app.storage.repositories as repos

    outcomes = iter(["inserted", "duplicate_denied"])

    async def fake_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        return next(outcomes)

    rewrites: list[float] = []

    async def spy_update_pick_stake(  # type: ignore[no-untyped-def]
        session, pick, teams, model_name, model_version, *, persist_tier=False, **kwargs
    ):
        rewrites.append(pick.recommended_stake_fraction)
        return True

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)
    monkeypatch.setattr(repos, "update_pick_stake", spy_update_pick_stake)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)
    day = datetime.now(tz=UTC).date()
    deps.ledger.reserve(day, deps.ledger.remaining(day))  # cap fully exhausted

    first = await run_pick_pipeline(deps, "soccer_epl")
    assert first == []  # cap-denied: not a pick this cycle
    assert sink.sent == []  # ... and no alert
    assert rewrites == [pytest.approx(0.0)]  # the row now carries the denial

    second = await run_pick_pipeline(deps, "soccer_epl")
    assert second == []
    assert sink.sent == []  # the duplicate must NOT late-fire the alert
    assert rewrites == [pytest.approx(0.0)]  # ... and nothing was rewritten again


async def test_unpersisted_with_persistence_configured_withholds_alert(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WP2 fail-closed mirror: persistence CONFIGURED but the write FAILS (DB
    outage) -> 'unpersisted' — the alert is withheld and counted at WARNING
    (an unpersisted pick can never be settled, seeded, or CLV-tracked)."""
    import logging as _logging

    import app.storage.repositories as repos

    async def failing_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        raise RuntimeError("db outage")

    monkeypatch.setattr(repos, "persist_pick", failing_persist_pick)

    sink = RecordingSink()
    deps = make_persisting_deps(sink)
    day = datetime.now(tz=UTC).date()

    with caplog.at_level(_logging.WARNING, logger="app.pipeline"):
        picks = await run_pick_pipeline(deps, "soccer_epl")
    assert picks == []  # fail closed: no pick without a persisted row
    assert sink.sent == []  # ... and no alert
    assert deps.ledger.used(day) == 0.0  # ... and no phantom reservation
    assert "withheld 1 premium alert" in caplog.text


async def test_commit_failure_releases_atomic_exposure_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.storage.repositories as repos

    async def fake_persist_pick(*args, **kwargs):  # type: ignore[no-untyped-def]
        return "inserted"

    async def fake_update_pick_stake(*args, **kwargs):  # type: ignore[no-untyped-def]
        return True

    class FailingCommitFactory(FakeSessionFactory):
        async def commit(self) -> None:
            raise RuntimeError("commit failed")

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)
    monkeypatch.setattr(repos, "update_pick_stake", fake_update_pick_stake)
    sink = RecordingSink()
    deps = make_persisting_deps(sink)
    deps.session_factory = FailingCommitFactory()  # type: ignore[assignment]
    day = datetime.now(tz=UTC).date()

    assert await run_pick_pipeline(deps, "soccer_epl") == []
    assert sink.sent == []
    assert deps.ledger.used(day) == pytest.approx(0.0)


async def test_pipeline_no_model_predictions_no_picks() -> None:
    sink = RecordingSink()
    deps = make_deps(sink)

    class SilentModel:
        name = "silent"
        version = "0"

        async def predict(self, event_id: str) -> Sequence[PredictedProbability]:
            return ()

    deps.model = SilentModel()
    picks = await run_pick_pipeline(deps, "soccer_epl")
    assert picks == []
    assert sink.sent == []


def _line_snap(detail: str, selection: str, odds: float) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id="evt-1",
        bookmaker="bookie",
        market=Market.TOTALS,
        selection=selection,
        decimal_odds=odds,
        captured_at=NOW - timedelta(seconds=30),
        ingested_at=NOW,
        market_detail=detail,
    )


def test_fair_probabilities_devig_each_line_separately() -> None:
    """Two totals lines at ONE book are TWO 2-leg markets, never one 4-leg
    book — mixing lines corrupts devig (the value pipeline already groups by
    market_detail; the model pipeline must apply the same rule)."""
    from app.pipeline import _fair_probabilities

    snapshots = [
        _line_snap("over_under_2_5", "Over 2.5", 2.0),
        _line_snap("over_under_2_5", "Under 2.5", 2.0),
        _line_snap("over_under_3_5", "Over 3.5", 2.60),
        _line_snap("over_under_3_5", "Under 3.5", 1.55),
    ]
    fair = _fair_probabilities(snapshots, DevigMethod.MULTIPLICATIVE)

    # 2.0/2.0 devigs to exactly 0.5 within its OWN line; pooled with the
    # 3.5-line legs it would come out ~0.246.
    assert fair[("evt-1", "bookie", Market.TOTALS, "Over 2.5")] == pytest.approx(0.5)
    assert fair[("evt-1", "bookie", Market.TOTALS, "Under 2.5")] == pytest.approx(0.5)
    line_35 = [
        fair[("evt-1", "bookie", Market.TOTALS, "Over 3.5")],
        fair[("evt-1", "bookie", Market.TOTALS, "Under 3.5")],
    ]
    assert sum(line_35) == pytest.approx(1.0)


def test_fair_probabilities_single_leg_line_is_skipped() -> None:
    """A line with only one priced side cannot be devigged — per-line
    grouping must not let another line's legs make it look complete."""
    from app.pipeline import _fair_probabilities

    snapshots = [
        _line_snap("over_under_2_5", "Over 2.5", 2.0),
        _line_snap("over_under_2_5", "Under 2.5", 2.0),
        _line_snap("over_under_3_5", "Over 3.5", 2.60),  # Under 3.5 missing
    ]
    fair = _fair_probabilities(snapshots, DevigMethod.MULTIPLICATIVE)
    assert ("evt-1", "bookie", Market.TOTALS, "Over 3.5") not in fair
    assert fair[("evt-1", "bookie", Market.TOTALS, "Over 2.5")] == pytest.approx(0.5)
