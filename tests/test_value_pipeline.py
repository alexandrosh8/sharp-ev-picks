"""Value pipeline: multi-book snapshots -> anchor -> value pick -> alert."""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.edge.gates import GatePolicy
from app.edge.steam import SteamPolicy
from app.edge.value_policy import ValuePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.models.base import NullModel
from app.notifications.base import Alert, build_pick_alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import PipelineDeps, run_value_pipeline
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

NOW = datetime.now(tz=UTC)

POLICY = GatePolicy(
    min_edge=0.0,
    min_ev=0.0,
    min_confidence=0.0,
    max_odds_age_seconds=300,
    min_liquidity=0.0,
)


def snap(book: str, sel: str, odds: float, age_s: float = 30.0) -> OddsSnapshotIn:
    # Stamp from a FRESH now per call, not the module-level NOW. The pipeline
    # computes odds age against datetime.now() at cycle time; a stale module NOW
    # — when a long full-suite run reaches a test minutes after collection —
    # otherwise turns a "future" (age_s < 0) or fresh snapshot stale, flaking the
    # odds-age assertions (e.g. test_value_pipeline_handles_future_captured_at).
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-1",
        bookmaker=book,
        market=Market.H2H,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=age_s),
        ingested_at=now,
    )


class FakeLoader:
    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        self.snapshots = snapshots
        # Mirrors OddsPortalLoader's liveness contract read by _record_poll.
        self.last_fetch_matches: dict[str, int] = {}
        self.last_fetch_event_ids: dict[str, tuple[str, ...]] = {}

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        self.last_fetch_matches[sport_key] = len({s.event_id for s in self.snapshots})
        return self.snapshots


class RecordingSink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def market_snapshots(age_s: float = 30.0) -> list[OddsSnapshotIn]:
    # Pinnacle prices a tight 3-way; SoftBook is too generous on Home.
    return [
        snap("Pinnacle", "Home FC", 2.50, age_s),
        snap("Pinnacle", "Draw", 3.30, age_s),
        snap("Pinnacle", "Away FC", 3.10, age_s),
        snap("SoftBook", "Home FC", 2.90, age_s),
        snap("SoftBook", "Draw", 3.20, age_s),
        snap("SoftBook", "Away FC", 2.95, age_s),
    ]


def make_deps(sink: RecordingSink, loader: FakeLoader) -> PipelineDeps:
    directory = EventDirectory()
    directory.register("evt-1", EventTeams(home="Home FC", away="Away FC"))
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
    )


def make_deps_league(
    sink: RecordingSink,
    loader: FakeLoader,
    *,
    league: str,
    value_policy: ValuePolicy,
) -> PipelineDeps:
    """Like make_deps, but the event carries a scraped league and the deps a
    value_policy — the inputs to the major-league premium gate."""
    directory = EventDirectory()
    directory.register("evt-1", EventTeams(home="Home FC", away="Away FC", league=league))
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
    )


async def test_major_league_gate_demotes_non_major_premium_to_no_alert() -> None:
    # With a major-league allowlist set, a premium-edge pick in a league OUTSIDE
    # the set is demoted to the volume (shadow) tier: never alerted, no premium
    # pick. Without the gate this exact slate mints one alerted premium pick
    # (test_value_pipeline_records_poll_liveness) — so the only difference is the
    # gate. The honest-high-ROI lever: don't alert what has no sharp coverage.
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(market_snapshots()),
        league="Obscure Regional Cup",
        value_policy=ValuePolicy(major_leagues=("Premier League",)),
    )
    await run_value_pipeline(deps, "soccer")
    assert sink.sent == []  # demoted -> never alerted
    assert LAST_POLL["soccer"]["picks"] == 0  # n_premium == 0


async def test_major_league_gate_keeps_premium_in_major_league() -> None:
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(market_snapshots()),
        league="Premier League",
        value_policy=ValuePolicy(major_leagues=("premier league",)),  # normalized match
    )
    await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1  # major league -> alerted premium pick
    assert LAST_POLL["soccer"]["picks"] == 1


async def test_experimental_sport_forces_premium_pick_to_volume() -> None:
    # An experimental (unvalidated) sport mints picks but every one is FORCED to
    # the volume/shadow tier: persisted + CLV-tracked, never alerted, no exposure
    # — honest "picks for tennis/NFL" without claiming a validated edge.
    from dataclasses import replace

    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = replace(
        make_deps(sink, FakeLoader(market_snapshots())),
        experimental_sports=frozenset({"soccer"}),
    )
    await run_value_pipeline(deps, "soccer")
    assert sink.sent == []  # experimental sport is never alerted
    assert LAST_POLL["soccer"]["picks"] == 0  # n_premium == 0 (forced to volume)


async def test_basketball_experimental_demoted_while_football_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Batch 3 DEMOTION: with basketball in experimental_sports, an identical slate
    # is shadow-only for basketball (no alert, ZERO exposure reserved) yet still
    # alerts for football. The safe direction — basketball is minted + tracked but
    # never claims a validated edge until its per-sport CLV clears.
    #
    # Exposure is reserved only on a PERSISTED premium detection (kr-1 ordering),
    # so both decks carry a session factory: basketball persists as volume (no
    # reserve), football persists as premium (reserves).
    from dataclasses import replace

    from app.pipeline import LAST_POLL

    patch_persist_recording(monkeypatch, ["inserted", "inserted"])

    # Basketball: experimental -> forced to volume/shadow.
    sink_bb = RecordingSink()
    deps_bb = replace(
        make_deps(sink_bb, FakeLoader(market_snapshots())),
        experimental_sports=frozenset({"basketball"}),
        session_factory=FakeSessionFactory(),  # type: ignore[arg-type]
    )
    await run_value_pipeline(deps_bb, "basketball")
    assert sink_bb.sent == []  # never alerted
    assert deps_bb.ledger.used(datetime.now(tz=UTC).date()) == 0.0  # zero exposure reserved
    assert LAST_POLL["basketball"]["picks"] == 0  # n_premium == 0 (forced to volume)

    # Football: the SAME slate, NOT experimental -> premium pick alerts + reserves.
    sink_fb = RecordingSink()
    deps_fb = replace(
        make_deps(sink_fb, FakeLoader(market_snapshots())),
        session_factory=FakeSessionFactory(),  # type: ignore[arg-type]
    )
    await run_value_pipeline(deps_fb, "soccer")
    assert len(sink_fb.sent) == 1  # football still alerts the identical edge
    assert deps_fb.ledger.used(datetime.now(tz=UTC).date()) > 0.0  # premium reserves exposure


async def test_major_league_gate_disabled_keeps_all_premium() -> None:
    # Empty major_leagues = gate OFF: the obscure-league pick still alerts
    # (current behavior, the non-breaking default).
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(market_snapshots()),
        league="Obscure Regional Cup",
        value_policy=ValuePolicy(),  # gate disabled
    )
    await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1
    assert LAST_POLL["soccer"]["picks"] == 1


def consensus_market_snapshots(age_s: float = 30.0) -> list[OddsSnapshotIn]:
    # Three SOFT books price the full 3-way; no Pinnacle/Betfair -> the market
    # anchors on the consensus(median), i.e. NO genuine sharp book backed fair
    # value. SoftA is generous enough on Home to clear the premium edge floor.
    return [
        snap("SoftA", "Home FC", 2.45, age_s),
        snap("SoftA", "Draw", 3.30, age_s),
        snap("SoftA", "Away FC", 3.10, age_s),
        snap("SoftB", "Home FC", 2.50, age_s),
        snap("SoftB", "Draw", 3.25, age_s),
        snap("SoftB", "Away FC", 3.05, age_s),
        snap("SoftC", "Home FC", 2.95, age_s),
        snap("SoftC", "Draw", 3.20, age_s),
        snap("SoftC", "Away FC", 2.95, age_s),
    ]


async def test_require_sharp_anchor_demotes_consensus_premium_to_no_alert() -> None:
    # require_sharp_anchor=True: a PREMIUM candidate whose fair value came from
    # the soft CONSENSUS median (no Pinnacle/Betfair anchor) is DEMOTED to the
    # volume (shadow) tier — persisted + CLV-tracked, never alerted, no premium
    # pick, no exposure. Stops obscure-league bleed by DATA (no sharp anchor),
    # not by league name. The same slate alerts with the gate off (test below).
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(consensus_market_snapshots()),
        league="GFA League",
        value_policy=ValuePolicy(require_sharp_anchor=True),
    )
    await run_value_pipeline(deps, "soccer")
    assert sink.sent == []  # consensus-anchored -> demoted -> never alerted
    assert LAST_POLL["soccer"]["picks"] == 0  # n_premium == 0 (demoted to shadow)


async def test_require_sharp_anchor_keeps_sharp_anchored_premium() -> None:
    # require_sharp_anchor=True but the market is anchored on a NAMED SHARP book
    # (Pinnacle in market_snapshots): the premium pick STAYS premium and alerts.
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(market_snapshots()),  # Pinnacle anchors the market
        league="GFA League",  # obscure league, but the gate is data-driven not name-driven
        value_policy=ValuePolicy(require_sharp_anchor=True),
    )
    await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1  # sharp anchor -> alerted premium pick
    assert LAST_POLL["soccer"]["picks"] == 1


async def test_require_sharp_anchor_disabled_keeps_consensus_premium() -> None:
    # require_sharp_anchor defaults False = gate OFF: a consensus-anchored
    # premium pick still alerts (current behavior, the non-breaking default).
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(consensus_market_snapshots()),
        league="GFA League",
        value_policy=ValuePolicy(),  # gate disabled (default)
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1  # consensus pick still alerted when gate off
    assert LAST_POLL["soccer"]["picks"] == 1
    assert all(p.anchor_type == "consensus" for p in picks)


async def test_sharp_anchor_loader_injects_betfair_as_live_anchor() -> None:
    # A soft-only scrape (no Pinnacle/Betfair in the main table — the real
    # OddsPortal case) anchors on consensus(median). Injecting the captured free
    # Betfair Exchange line via sharp_anchor_loader makes the pick SHARP-anchored
    # AT PICK TIME — the "use Betfair/Pinnacle on getting picks" fix.
    from dataclasses import replace

    soft = [
        snap("SoftA", "Home FC", 2.45),
        snap("SoftA", "Draw", 3.30),
        snap("SoftA", "Away FC", 3.10),
        snap("SoftB", "Home FC", 2.50),
        snap("SoftB", "Draw", 3.25),
        snap("SoftB", "Away FC", 3.05),
        snap("SoftC", "Home FC", 2.95),
        snap("SoftC", "Draw", 3.20),
        snap("SoftC", "Away FC", 2.95),
    ]

    # Without a loader: consensus-anchored (the current default).
    sink0 = RecordingSink()
    picks0 = await run_value_pipeline(make_deps(sink0, FakeLoader(list(soft))), "soccer")
    assert picks0 and all(p.anchor_type == "consensus" for p in picks0)

    # With the Betfair injector: the same soft scrape now anchors on Betfair.
    async def betfair_loader(sport_key, snapshots):  # type: ignore[no-untyped-def]
        return [
            snap("Betfair Exchange", "Home FC", 2.40),
            snap("Betfair Exchange", "Draw", 3.45),
            snap("Betfair Exchange", "Away FC", 3.25),
        ]

    sink = RecordingSink()
    deps = replace(make_deps(sink, FakeLoader(list(soft))), sharp_anchor_loader=betfair_loader)
    picks = await run_value_pipeline(deps, "soccer")
    assert picks, "expected a value pick"
    assert all(p.anchor_type == "sharp" for p in picks)  # anchored on Betfair, not consensus
    assert any("betfair" in p.reason_summary.lower() for p in picks)


async def test_sharp_anchor_pick_book_is_never_sharp() -> None:
    # CRITICAL (review 2026-06-21): when BOTH Betfair + Pinnacle are injected as
    # sharp anchors, the ACTIONABLE pick must still be a SOFT book — never the
    # non-anchor sharp/exchange book (you cannot bet the injected anchor line).
    from dataclasses import replace

    soft = [
        snap("SoftA", "Home FC", 2.45),
        snap("SoftA", "Draw", 3.30),
        snap("SoftA", "Away FC", 3.10),
        snap("SoftB", "Home FC", 2.50),
        snap("SoftB", "Draw", 3.25),
        snap("SoftB", "Away FC", 3.05),
        snap("SoftC", "Home FC", 2.95),
        snap("SoftC", "Draw", 3.20),
        snap("SoftC", "Away FC", 2.95),
    ]

    async def dual_sharp_loader(sport_key, snapshots):  # type: ignore[no-untyped-def]
        # Betfair carries the JUICIEST Home price (3.40) — WITHOUT the fix the
        # pick would recommend "at Betfair Exchange" (unbettable). Pinnacle anchors.
        return [
            snap("Betfair Exchange", "Home FC", 3.40),
            snap("Betfair Exchange", "Draw", 3.50),
            snap("Betfair Exchange", "Away FC", 3.20),
            snap("Pinnacle", "Home FC", 2.40),
            snap("Pinnacle", "Draw", 3.45),
            snap("Pinnacle", "Away FC", 3.25),
        ]

    sink = RecordingSink()
    deps = replace(make_deps(sink, FakeLoader(list(soft))), sharp_anchor_loader=dual_sharp_loader)
    picks = await run_value_pipeline(deps, "soccer")
    assert picks, "expected a value pick"
    _SHARP = {"pinnacle", "pinnacle sports", "betfair exchange", "smarkets"}
    for p in picks:
        assert p.bookmaker.lower() not in _SHARP, f"pick recommends a sharp book: {p.bookmaker}"


async def test_value_pipeline_records_poll_liveness() -> None:
    # The dashboard/health must be able to tell "engine alive" from "engine
    # dead showing day-old picks" — every cycle records itself, including
    # per-market snapshot counts and the loader's listing count so a selector
    # break (matches found, zero odds parsed) is visible, not silent.
    from app.pipeline import AVAILABLE_GAMES, LAST_POLL

    sink = RecordingSink()
    await run_value_pipeline(make_deps(sink, FakeLoader(market_snapshots())), "soccer")
    poll = LAST_POLL["soccer"]
    assert poll["finished_at"] is not None
    assert poll["snapshots"] > 0
    assert poll["picks"] == 1
    assert poll["matches_found"] == 1
    assert poll["per_market"] == {"h2h": 6}
    assert poll["degraded"] is False
    games = AVAILABLE_GAMES["soccer"]
    assert len(games) == 1
    assert games[0]["event"] == "Home FC vs Away FC"
    assert games[0]["snapshot_count"] == 6
    assert games[0]["market_count"] == 1
    assert games[0]["bookmaker_count"] == 2


async def test_available_games_records_listed_fixture_with_zero_odds() -> None:
    """The unrestricted games feed must show a listed fixture even when a
    scraper gap leaves it with zero parsed odds rows."""
    from app.pipeline import AVAILABLE_GAMES

    loader = FakeLoader([])
    loader.last_fetch_event_ids = {"basketball": ("evt-empty",)}
    sink = RecordingSink()
    deps = make_deps(sink, loader)
    assert deps.directory is not None
    deps.directory.register(
        "evt-empty",
        EventTeams(
            home="Home Hoops",
            away="Away Hoops",
            league="NBA",
            starts_at=NOW + timedelta(hours=2),
        ),
    )

    await run_value_pipeline(deps, "basketball")

    games = AVAILABLE_GAMES["basketball"]
    assert len(games) == 1
    assert games[0]["event"] == "Home Hoops vs Away Hoops"
    assert games[0]["league"] == "NBA"
    assert games[0]["snapshot_count"] == 0
    assert games[0]["markets"] == []


async def test_poll_record_flags_degraded_on_matches_without_snapshots() -> None:
    """Selector/DOM break (or anti-bot wall): listings parse, every odds row
    is missed. Cycles still complete, so finished_at alone looks healthy —
    the poll record must carry an explicit degraded flag for /health."""
    from app.pipeline import LAST_POLL

    class BrokenScrapeLoader(FakeLoader):
        async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
            self.last_fetch_matches[sport_key] = 7  # listings parsed fine
            return []  # ...but zero odds rows survived parsing

    sink = RecordingSink()
    await run_value_pipeline(make_deps(sink, BrokenScrapeLoader([])), "soccer")
    poll = LAST_POLL["soccer"]
    assert poll["matches_found"] == 7
    assert poll["snapshots"] == 0
    assert poll["per_market"] == {}
    assert poll["degraded"] is True


async def test_poll_record_without_listing_count_is_not_degraded() -> None:
    # Loaders that don't report listing counts (odds_api, plain fakes) must
    # not be flagged degraded on an empty day — unknown is not broken.
    from app.pipeline import LAST_POLL

    class CountlessLoader:
        async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
            return []

    sink = RecordingSink()
    await run_value_pipeline(make_deps(sink, CountlessLoader()), "soccer")  # type: ignore[arg-type]
    poll = LAST_POLL["soccer"]
    assert poll["matches_found"] is None
    assert poll["degraded"] is False


async def test_value_pipeline_produces_pick_and_alert() -> None:
    sink = RecordingSink()
    picks = await run_value_pipeline(make_deps(sink, FakeLoader(market_snapshots())), "soccer")
    assert len(picks) == 1
    pick = picks[0]
    assert pick.selection == "Home FC"
    assert pick.bookmaker == "SoftBook"
    assert pick.decimal_odds == 2.90
    # model_probability carries the SHARP fair prob; edge = fair - implied
    assert pick.model_probability > pick.fair_probability
    assert pick.edge >= 0.015
    assert pick.confidence == 0.9  # named sharp anchor (Pinnacle)
    assert pick.anchor_type == "pinnacle"  # live CLV stratification key
    assert pick.event == "Home FC vs Away FC"
    assert len(sink.sent) == 1
    assert "you place any bet" not in sink.sent[0].body  # footer removed per operator request
    assert "value: Pinnacle fair" in pick.reason_summary


async def test_value_pipeline_alert_key_includes_strategy_identity() -> None:
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.model_name = "value-sharp-vs-soft"
    deps.model_version = "v4"

    picks = await run_value_pipeline(deps, "soccer")

    assert len(picks) == 1
    expected = build_pick_alert(
        picks[0],
        deps.value_min_edge,
        model_name=deps.model_name,
        model_version=deps.model_version,
    )
    assert sink.sent[0].dedupe_key == expected.dedupe_key


async def test_value_pipeline_tags_consensus_anchor_picks() -> None:
    # No Pinnacle: 3 soft books price the full market -> median consensus
    # anchor; the pick must carry anchor_type="consensus" (and the weaker
    # fallback confidence) so live CLV can be stratified by anchor.
    snapshots = [
        snap("BookA", "Home FC", 2.50),
        snap("BookA", "Draw", 3.30),
        snap("BookA", "Away FC", 3.10),
        snap("BookB", "Home FC", 2.52),
        snap("BookB", "Draw", 3.28),
        snap("BookB", "Away FC", 3.05),
        snap("SoftBook", "Home FC", 2.95),
        snap("SoftBook", "Draw", 3.25),
        snap("SoftBook", "Away FC", 3.00),
    ]
    sink = RecordingSink()
    picks = await run_value_pipeline(make_deps(sink, FakeLoader(snapshots)), "soccer")
    assert len(picks) == 1
    pick = picks[0]
    assert pick.anchor_type == "consensus"
    assert pick.confidence == 0.7  # consensus fallback confidence
    assert "consensus(median)" in pick.reason_summary


async def test_value_pipeline_rerun_dedupes_alert() -> None:
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    first = await run_value_pipeline(deps, "soccer")
    second = await run_value_pipeline(deps, "soccer")
    assert len(first) == len(second) == 1
    assert len(sink.sent) == 1  # same market state -> one alert


class FakeSessionFactory:
    """Minimal async-contextmanager session; revalidation calls against
    it raise and are swallowed by the pipeline's try/except."""

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

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)


async def test_duplicate_pick_releases_exposure_and_unchanged_odds_stay_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1 regression: a pick already persisted (DB dedupe) must hand its
    daily-exposure grant back — leaking one grant per cycle exhausts the
    daily cap within minutes. The alert is still DISPATCHED (so a failed
    first delivery self-heals); with unchanged odds the idempotency store
    suppresses it, so exactly one alert reaches the sink."""
    patch_persist_dedupe_after_first(monkeypatch)

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]

    day = datetime.now(tz=UTC).date()
    first = await run_value_pipeline(deps, "soccer")
    assert len(first) == 1
    used_after_first = deps.ledger.used(day)
    assert used_after_first > 0.0

    second = await run_value_pipeline(deps, "soccer")
    assert second == []  # duplicate is not a new pick this cycle
    assert deps.ledger.used(day) == pytest.approx(used_after_first)  # grant returned
    assert len(sink.sent) == 1  # idempotency (key includes odds) suppressed it


async def test_duplicate_pick_with_price_move_realerts_and_still_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alert dedupe key deliberately includes decimal_odds (notifications/
    base.py): a material price move on a pick the DB already knows must
    RE-ALERT — skipping dispatch on DB dedupe killed that design. Exposure is
    still handed back: a re-priced duplicate is not new exposure."""
    patch_persist_dedupe_after_first(monkeypatch)

    sink = RecordingSink()
    loader = FakeLoader(market_snapshots())
    deps = make_deps(sink, loader)
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]

    day = datetime.now(tz=UTC).date()
    first = await run_value_pipeline(deps, "soccer")
    assert len(first) == 1
    assert len(sink.sent) == 1
    used_after_first = deps.ledger.used(day)

    # SoftBook moves its Home price 2.90 -> 2.95: same DB row (dedupe ignores
    # odds), materially different market state.
    loader.snapshots = [
        snap("SoftBook", "Home FC", 2.95)
        if s.bookmaker == "SoftBook" and s.selection == "Home FC"
        else s
        for s in market_snapshots()
    ]
    second = await run_value_pipeline(deps, "soccer")
    assert second == []  # still not a NEW pick
    assert deps.ledger.used(day) == pytest.approx(used_after_first)  # grant returned
    assert len(sink.sent) == 2  # price move re-alerted
    assert "2.95" in sink.sent[1].title


async def test_unpersisted_premium_pick_does_not_accumulate_exposure() -> None:
    """kelly-risk-r2-1 (value path): with persistence unavailable (no session
    factory) a premium pick re-detected each cycle is 'unpersisted'. It must
    reserve NOTHING — a sustained-unpersisted pick that accumulated standing
    exposure would silently exhaust the 5% daily cap and suppress later alerts.
    The pick still flows (minted + alerted)."""
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))  # no session_factory
    day = datetime.now(tz=UTC).date()

    first = await run_value_pipeline(deps, "soccer")
    assert [p.tier for p in first] == ["premium"]
    assert deps.ledger.used(day) == 0.0  # unpersisted reserves NOTHING

    second = await run_value_pipeline(deps, "soccer")
    assert [p.tier for p in second] == ["premium"]
    assert deps.ledger.used(day) == 0.0  # still zero -> no cross-cycle accumulation


def test_pick_tier_boundaries() -> None:
    """Tier floors are INCLUSIVE (>= mirrors the backtests' gates): edge
    exactly 0.03 is premium, a hair under is volume, under 0.015 is no pick;
    equal floors disable the volume tier entirely."""
    from app.pipeline import pick_tier

    assert pick_tier(0.03, 0.03, 0.015) == "premium"
    assert pick_tier(0.0299, 0.03, 0.015) == "volume"
    assert pick_tier(0.015, 0.03, 0.015) == "volume"
    assert pick_tier(0.0149, 0.03, 0.015) is None
    assert pick_tier(0.02, 0.03, 0.03) is None  # equal floors: tier off
    assert pick_tier(0.03, 0.03, 0.03) == "premium"


def patch_persist_recording(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[str]
) -> list[tuple[str, str]]:
    """persist_pick fake returning scripted outcomes; records (selection,
    tier) per call so tests can assert what reached the repository."""
    import app.storage.repositories as repos

    seen: list[tuple[str, str]] = []
    script = iter(outcomes)

    async def fake_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        seen.append((pick.selection, pick.tier))
        return next(script)

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)
    return seen


async def test_volume_tier_pick_persists_without_alert_or_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shadow tier's contract: persisted (with the informational stake
    breakdown computed) but (a) NO alert dispatch and (b) NO exposure-ledger
    reservation — it must never consume the cap premium picks need. (Volume
    alerting was trialed then reverted 2026-06-23: live CLV ~0 showed no edge.)"""
    seen = patch_persist_recording(monkeypatch, ["inserted"])

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    deps.value_min_edge = 0.10  # the ~4.5% edge cannot reach premium
    deps.value_volume_min_edge = 0.015

    day = datetime.now(tz=UTC).date()
    picks = await run_value_pipeline(deps, "soccer")

    assert [p.tier for p in picks] == ["volume"]
    assert seen == [("Home FC", "volume")]
    assert sink.sent == []  # (a) shadow tier: never alerted (premium-only alerts)
    assert deps.ledger.used(day) == 0.0  # (b) never on the ledger
    assert picks[0].stake_breakdown.final > 0.0  # stake computed, informational
    from app.pipeline import LAST_POLL

    assert LAST_POLL["soccer"]["picks"] == 0  # headline count stays premium
    assert LAST_POLL["soccer"]["volume_picks"] == 1


async def test_volume_tier_dropped_when_persistence_unavailable() -> None:
    # A volume pick that cannot reach the DB accumulates no CLV evidence —
    # its only purpose — so it is dropped silently: no pick, no alert.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))  # no session_factory
    deps.value_min_edge = 0.10
    deps.value_volume_min_edge = 0.015
    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []
    assert sink.sent == []
    assert deps.ledger.used(datetime.now(tz=UTC).date()) == 0.0


async def test_volume_redetection_of_existing_key_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'duplicate' covers both a volume re-detection AND a key already held
    by a PREMIUM row — the shadow tier must never alert, never touch the
    ledger, and never displace the premium row."""
    patch_persist_recording(monkeypatch, ["duplicate"])

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    deps.value_min_edge = 0.10
    deps.value_volume_min_edge = 0.015

    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []
    assert sink.sent == []
    assert deps.ledger.used(datetime.now(tz=UTC).date()) == 0.0


async def test_volume_to_premium_upgrade_alerts_and_reserves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upgrade transition: a key first persisted as volume (tracked silently
    — no alert, no exposure) later clears the premium threshold -> the repository
    promotes the row ('upgraded') and the pipeline treats it as a NEW premium
    pick — THIS is the alert moment (⭐ PREMIUM) and exposure is reserved (the
    shadow row never held one)."""
    seen = patch_persist_recording(monkeypatch, ["inserted", "upgraded"])

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    deps.value_min_edge = 0.10  # cycle 1: candidate lands in the volume band
    deps.value_volume_min_edge = 0.015

    day = datetime.now(tz=UTC).date()
    first = await run_value_pipeline(deps, "soccer")
    assert [p.tier for p in first] == ["volume"]
    assert sink.sent == []  # cycle 1: volume tracked silently (not alerted)
    assert deps.ledger.used(day) == 0.0  # ...and takes no exposure

    # cycle 2: the same candidate now clears premium (threshold change here;
    # a price move in production) — the volume row upgrades in place.
    deps.value_min_edge = 0.03
    second = await run_value_pipeline(deps, "soccer")
    assert [p.tier for p in second] == ["premium"]
    assert seen == [("Home FC", "volume"), ("Home FC", "premium")]
    assert len(sink.sent) == 1  # the premium upgrade IS the alert moment
    assert "⭐ PREMIUM" in sink.sent[0].title  # tagged premium
    assert deps.ledger.used(day) > 0.0  # exposure reserved on upgrade


async def test_value_pipeline_skips_stale_odds() -> None:
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots(age_s=400.0)))  # > 300s gate
    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []
    assert sink.sent == []


async def test_stale_age_gate_discards_are_counted_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The odds-age gate's failure mode is SILENT slate collapse: when a
    scrape outlasts MAX_ODDS_AGE_SECONDS (live leagues=all incident,
    2026-06-12: multi-hour cycles) nearly every candidate is dropped with no
    trace. Discards must be counted into the poll record and warned about."""
    import logging as _logging

    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots(age_s=400.0)))
    with caplog.at_level(_logging.WARNING, logger="app.pipeline"):
        await run_value_pipeline(deps, "soccer")
    assert LAST_POLL["soccer"]["stale_candidates"] == 1
    assert any("odds-age gate" in r.getMessage() for r in caplog.records)

    # Fresh odds: explicit zero, and no warning noise.
    caplog.clear()
    deps2 = make_deps(sink, FakeLoader(market_snapshots()))
    with caplog.at_level(_logging.WARNING, logger="app.pipeline"):
        await run_value_pipeline(deps2, "soccer")
    assert LAST_POLL["soccer"]["stale_candidates"] == 0
    assert not any("odds-age gate" in r.getMessage() for r in caplog.records)


async def test_value_pipeline_skips_started_events() -> None:
    """In-play gate: matches flip in-play between page listing and scrape
    (long cycles); OddsPortal then serves in-play prices. A started event
    must produce NO pick and NO alert — a pre-match price no longer exists
    for the operator to take (live incident: a premium pick minted 76 min
    after kickoff from the in-play URL fork)."""
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    assert deps.directory is not None
    deps.directory.register(
        "evt-1",
        EventTeams(home="Home FC", away="Away FC", starts_at=NOW - timedelta(minutes=20)),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []
    assert sink.sent == []
    assert deps.ledger.used(datetime.now(tz=UTC).date()) == 0.0


async def test_value_pipeline_keeps_future_kickoff_events() -> None:
    # The gate keys on starts_at <= now: future kickoffs (and NULL — cannot
    # prove the game started) keep flowing.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    assert deps.directory is not None
    deps.directory.register(
        "evt-1",
        EventTeams(home="Home FC", away="Away FC", starts_at=NOW + timedelta(hours=3)),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1


async def test_value_pipeline_handles_future_captured_at() -> None:
    # Live scrapes stamp captured_at DURING the multi-minute fetch; a snapshot
    # "newer than now" must clamp to age 0, not crash PickOut validation.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots(age_s=-90.0)))  # future
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].odds_age_seconds == 0.0


async def test_value_pipeline_prices_half_line_handicap_directly() -> None:
    # Half-line AH is a full 2-way market: direct devig anchor, line kept
    # separate via market_detail. Pinnacle tight, SoftBook generous on home.
    def ah(book: str, sel: str, odds: float) -> OddsSnapshotIn:
        return OddsSnapshotIn(
            event_id="evt-1",
            bookmaker=book,
            market=Market.SPREADS,
            selection=sel,
            decimal_odds=odds,
            captured_at=NOW - timedelta(seconds=30),
            ingested_at=NOW,
            market_detail="asian_handicap_-1_5",
        )

    snaps = [
        ah("Pinnacle", "Home FC -1.5", 2.00),
        ah("Pinnacle", "Away FC +1.5", 1.95),
        ah("SoftBook", "Home FC -1.5", 2.35),
        ah("SoftBook", "Away FC +1.5", 1.70),
    ]
    sink = RecordingSink()
    picks = await run_value_pipeline(make_deps(sink, FakeLoader(snaps)), "soccer")
    assert len(picks) == 1
    assert picks[0].selection == "Home FC -1.5"
    assert picks[0].market == Market.SPREADS
    assert picks[0].bookmaker == "SoftBook"


async def test_value_pipeline_no_anchor_no_picks() -> None:
    # Only two books and neither is a named sharp -> no trustworthy anchor.
    snaps = [s for s in market_snapshots() if s.bookmaker != "Pinnacle"]
    snaps += [
        snap("OtherBook", "Home FC", 2.55),
        # OtherBook prices only one selection -> not a full-market book
    ]
    sink = RecordingSink()
    picks = await run_value_pipeline(make_deps(sink, FakeLoader(snaps)), "soccer")
    assert picks == []


def _detail_snap(
    book: str, market: Market, sel: str, odds: float, detail: str | None
) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id="evt-1",
        bookmaker=book,
        market=market,
        selection=sel,
        decimal_odds=odds,
        captured_at=NOW - timedelta(seconds=30),
        ingested_at=NOW,
        market_detail=detail,
    )


def test_event_fair_probs_expanded_markets_devig_per_line_and_derive_dc() -> None:
    """The expanded market set round-trips devig per (market, line) group:
    every direct group sums to 1.0 within ITS line; double chance is never
    devigged directly (legs overlap, quotes sum ~200%) — its fair value is
    DERIVED from the 1X2 anchor's pairwise sums."""
    from app.pipeline import event_fair_probs, group_market_prices
    from app.probabilities.devig import DevigMethod

    snaps = [
        # 1X2 anchor (full 3-way at the sharp book)
        _detail_snap("Pinnacle", Market.H2H, "Home FC", 2.50, None),
        _detail_snap("Pinnacle", Market.H2H, "Draw", 3.30, None),
        _detail_snap("Pinnacle", Market.H2H, "Away FC", 3.10, None),
        # two totals lines — must anchor as separate 2-way books
        _detail_snap("Pinnacle", Market.TOTALS, "Over 2.5", 1.95, "over_under_2_5"),
        _detail_snap("Pinnacle", Market.TOTALS, "Under 2.5", 1.95, "over_under_2_5"),
        _detail_snap("Pinnacle", Market.TOTALS, "Over 3.5", 2.80, "over_under_3_5"),
        _detail_snap("Pinnacle", Market.TOTALS, "Under 3.5", 1.45, "over_under_3_5"),
        # 3-way European handicap line (devig-sound at any integer line)
        _detail_snap("Pinnacle", Market.SPREADS, "Home FC -1", 3.10, "european_handicap_-1"),
        _detail_snap("Pinnacle", Market.SPREADS, "Draw (-1)", 3.60, "european_handicap_-1"),
        _detail_snap("Pinnacle", Market.SPREADS, "Away FC +1", 2.10, "european_handicap_-1"),
        # double-chance quotes: NEVER a direct devig input
        _detail_snap("SoftBook", Market.DOUBLE_CHANCE, "Home FC or Draw", 1.42, "double_chance"),
        _detail_snap("SoftBook", Market.DOUBLE_CHANCE, "Home FC or Away FC", 1.36, "double_chance"),
        _detail_snap("SoftBook", Market.DOUBLE_CHANCE, "Draw or Away FC", 1.60, "double_chance"),
    ]
    fair = event_fair_probs(group_market_prices(snaps), DevigMethod.POWER)

    for market, detail, n_outcomes in (
        (Market.H2H, None, 3),
        (Market.TOTALS, "over_under_2_5", 2),
        (Market.TOTALS, "over_under_3_5", 2),
        (Market.SPREADS, "european_handicap_-1", 3),
    ):
        anchor_book, by_sel = fair[("evt-1", market, detail)]
        assert anchor_book == "Pinnacle"
        assert len(by_sel) == n_outcomes
        assert sum(by_sel.values()) == pytest.approx(1.0)
    # symmetric 2.5-line book devigs to exactly 0.5 within its OWN line
    assert fair[("evt-1", Market.TOTALS, "over_under_2_5")][1]["Over 2.5"] == pytest.approx(0.5)

    h2h_fair = fair[("evt-1", Market.H2H, None)][1]
    dc_anchor, dc_fair = fair[("evt-1", Market.DOUBLE_CHANCE, "double_chance")]
    assert dc_anchor == "Pinnacle"  # inherited from the 1X2 anchor
    assert dc_fair["Home FC or Draw"] == pytest.approx(h2h_fair["Home FC"] + h2h_fair["Draw"])
    assert dc_fair["Home FC or Away FC"] == pytest.approx(h2h_fair["Home FC"] + h2h_fair["Away FC"])
    assert dc_fair["Draw or Away FC"] == pytest.approx(h2h_fair["Draw"] + h2h_fair["Away FC"])
    # overlapping legs by design: DC fair sums to 2.0, not 1.0
    assert sum(dc_fair.values()) == pytest.approx(2.0)


def test_event_fair_probs_routes_per_market_devig_override() -> None:
    """FEATURE A: a per-market devig override changes ONLY the targeted market's
    fair value; every other market keeps the global method (CLV-safe: the same
    map flows to the close path, so fill and close share one method)."""
    from app.edge.value_policy import ValuePolicy
    from app.pipeline import event_fair_probs, group_market_prices
    from app.probabilities.devig import DevigMethod, devig

    snaps = [
        # overround 1X2 book — power and multiplicative give DIFFERENT fair
        _detail_snap("Pinnacle", Market.H2H, "Home FC", 2.50, None),
        _detail_snap("Pinnacle", Market.H2H, "Draw", 3.30, None),
        _detail_snap("Pinnacle", Market.H2H, "Away FC", 3.10, None),
        # asymmetric overround totals line — method choice is observable
        _detail_snap("Pinnacle", Market.TOTALS, "Over 2.5", 1.80, "over_under_2_5"),
        _detail_snap("Pinnacle", Market.TOTALS, "Under 2.5", 2.05, "over_under_2_5"),
    ]
    grouped = group_market_prices(snaps)

    base = event_fair_probs(grouped, DevigMethod.MULTIPLICATIVE)
    # override ONLY the totals line to POWER; h2h keeps the global multiplicative
    policy = ValuePolicy(devig_by_market=(("over_under_2_5", DevigMethod.POWER),))
    routed = event_fair_probs(grouped, DevigMethod.MULTIPLICATIVE, policy)

    # h2h untouched by the totals override
    h2h_base = base[("evt-1", Market.H2H, None)][1]
    h2h_routed = routed[("evt-1", Market.H2H, None)][1]
    for sel in h2h_base:
        assert h2h_routed[sel] == pytest.approx(h2h_base[sel], abs=1e-12)

    # totals line now devigged with POWER, not the global multiplicative
    tot_routed = routed[("evt-1", Market.TOTALS, "over_under_2_5")][1]
    expected_power = devig([1.80, 2.05], method=DevigMethod.POWER)
    assert tot_routed["Over 2.5"] == pytest.approx(expected_power[0], abs=1e-12)
    # and it genuinely differs from the global-method result
    tot_base = base[("evt-1", Market.TOTALS, "over_under_2_5")][1]
    assert abs(tot_routed["Over 2.5"] - tot_base["Over 2.5"]) > 1e-6
    assert sum(tot_routed.values()) == pytest.approx(1.0, abs=1e-9)


def test_event_fair_probs_threads_consensus_logit_pool_flag() -> None:
    """FEATURE B: the consensus_logit_pool flag reaches anchor_fair_probs through
    event_fair_probs. On a consensus-anchored market (no sharp book) with
    cross-book spread, the pooled fair differs from the median consensus."""
    from app.edge.value_policy import ValuePolicy
    from app.pipeline import event_fair_probs, group_market_prices
    from app.probabilities.devig import DevigMethod

    # three SOFT books (no sharp anchor) with spread on a heavy favourite
    snaps = [
        _detail_snap("SoftA", Market.H2H, "Home FC", 1.45, None),
        _detail_snap("SoftA", Market.H2H, "Draw", 4.20, None),
        _detail_snap("SoftA", Market.H2H, "Away FC", 7.00, None),
        _detail_snap("SoftB", Market.H2H, "Home FC", 1.50, None),
        _detail_snap("SoftB", Market.H2H, "Draw", 4.00, None),
        _detail_snap("SoftB", Market.H2H, "Away FC", 6.50, None),
        _detail_snap("SoftC", Market.H2H, "Home FC", 1.40, None),
        _detail_snap("SoftC", Market.H2H, "Draw", 4.50, None),
        _detail_snap("SoftC", Market.H2H, "Away FC", 7.50, None),
    ]
    grouped = group_market_prices(snaps)
    median = event_fair_probs(grouped, DevigMethod.POWER)[("evt-1", Market.H2H, None)][1]
    pooled = event_fair_probs(grouped, DevigMethod.POWER, ValuePolicy(consensus_logit_pool=True))[
        ("evt-1", Market.H2H, None)
    ][1]

    assert sum(pooled.values()) == pytest.approx(1.0, abs=1e-9)
    assert pooled["Home FC"] > pooled["Draw"] > pooled["Away FC"]  # order preserved
    assert any(abs(pooled[s] - median[s]) > 1e-4 for s in median)  # flag took effect


def test_event_fair_probs_skips_dc_when_h2h_middle_outcome_is_not_the_draw() -> None:
    """DC fair = pairwise sums of the 1X2 anchor, valid ONLY for the canonical
    home/Draw/away order. If a feed/label reorder (cf. the 1X2 Draw<->away swap)
    puts the draw off the middle, the DC fair must be SKIPPED (fail safe), never
    mis-derived from a wrong home/away."""
    from app.pipeline import event_fair_probs, group_market_prices
    from app.probabilities.devig import DevigMethod

    snaps = [
        # H2H emitted in a NON-canonical order: Home, Away, Draw (draw not middle)
        _detail_snap("Pinnacle", Market.H2H, "Home FC", 2.50, None),
        _detail_snap("Pinnacle", Market.H2H, "Away FC", 3.10, None),
        _detail_snap("Pinnacle", Market.H2H, "Draw", 3.30, None),
        _detail_snap("SoftBook", Market.DOUBLE_CHANCE, "Home FC or Draw", 1.42, "double_chance"),
        _detail_snap("SoftBook", Market.DOUBLE_CHANCE, "Home FC or Away FC", 1.36, "double_chance"),
        _detail_snap("SoftBook", Market.DOUBLE_CHANCE, "Draw or Away FC", 1.60, "double_chance"),
    ]
    fair = event_fair_probs(group_market_prices(snaps), DevigMethod.POWER)
    # H2H itself still anchored, but DC is skipped — the middle outcome != "Draw".
    assert ("evt-1", Market.H2H, None) in fair
    assert ("evt-1", Market.DOUBLE_CHANCE, "double_chance") not in fair


# --- optional ValuePolicy knobs (premium-tier adjustments, default OFF) ------
# Evidence requirements before enabling any of these live in
# docs/backtesting/value-findings.md (spent-holdout discipline).


async def test_default_value_policy_is_a_strict_noop() -> None:
    # PipelineDeps' default policy must reproduce the baseline exactly: one
    # premium pick, one alert (same fixture as the liveness test above).
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    assert deps.value_policy == ValuePolicy()
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].tier == "premium"
    assert len(sink.sent) == 1


async def test_per_market_premium_floor_demotes_to_volume() -> None:
    # An h2h-specific premium floor far above the candidate's ~0.045 edge
    # demotes it to the volume (shadow) tier: never alerted; without a DB the
    # shadow pick is dropped entirely (no evidence row to accumulate).
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.value_policy = ValuePolicy(min_edge_by_market=(("h2h", 0.50),))
    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []
    assert sink.sent == []


async def test_per_market_floor_on_another_market_changes_nothing() -> None:
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.value_policy = ValuePolicy(min_edge_by_market=(("totals", 0.50),))
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].tier == "premium"


async def test_odds_band_gate_rejects_out_of_band_prices() -> None:
    # SoftBook's 2.90 best price sits outside a 3.0-4.0 band -> no pick, and
    # the rejection happens AFTER the edge scan (it is a price-shape gate).
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.value_policy = ValuePolicy(odds_bands=((3.0, 4.0),))
    assert await run_value_pipeline(deps, "soccer") == []
    assert sink.sent == []


async def test_odds_band_gate_passes_in_band_prices() -> None:
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.value_policy = ValuePolicy(odds_bands=((2.5, 3.0),))
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].decimal_odds == 2.90


async def test_min_books_floor_skips_thinly_quoted_markets() -> None:
    # The fixture quotes h2h at 1 SOFT book (Pinnacle is a sharp anchor — NOT
    # counted toward soft liquidity); a 2-book floor skips the whole market
    # before any anchoring/scanning happens.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.value_policy = ValuePolicy(min_books_by_market=(("h2h", 2),))
    assert await run_value_pipeline(deps, "soccer") == []
    assert sink.sent == []


async def test_min_books_floor_at_actual_count_changes_nothing() -> None:
    # 1 soft book -> a 1-book floor is a no-op (the sharp anchor never counts
    # toward the soft-liquidity gate).
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.value_policy = ValuePolicy(min_books_by_market=(("h2h", 1),))
    assert len(await run_value_pipeline(deps, "soccer")) == 1


# --- line-movement / steam-awareness gate (app/edge/steam.py) ---------------


def _steam_history_loader(
    old_soft_home_odds: float,
) -> Callable[[str, Sequence[OddsSnapshotIn]], Awaitable[list[OddsSnapshotIn]]]:
    """A stub PipelineDeps.steam_history_loader: returns ONE older SoftBook Home
    observation so the fill book shows a trajectory (old generous -> current
    less generous = converging toward the Pinnacle anchor)."""

    async def _loader(sport_key: str, snapshots: Sequence[OddsSnapshotIn]) -> list[OddsSnapshotIn]:
        return [snap("SoftBook", "Home FC", old_soft_home_odds, age_s=7200.0)]

    return _loader


def stale_anchor_market() -> list[OddsSnapshotIn]:
    # Pinnacle anchors but its prices are 3h old (the freshness window is 2h);
    # the soft book is fresh. The anchor is STALE -> phantom edge.
    return [
        snap("Pinnacle", "Home FC", 2.50, age_s=10800.0),
        snap("Pinnacle", "Draw", 3.30, age_s=10800.0),
        snap("Pinnacle", "Away FC", 3.10, age_s=10800.0),
        snap("SoftBook", "Home FC", 2.90, age_s=30.0),
        snap("SoftBook", "Draw", 3.20, age_s=30.0),
        snap("SoftBook", "Away FC", 2.95, age_s=30.0),
    ]


async def test_steam_gate_enabled_demotes_converging_premium_to_no_alert() -> None:
    # The soft Home price has corrected 3.80 -> 2.90 toward the Pinnacle anchor
    # (>50% of the original edge gone): an evaporating edge. ENABLED steam gate
    # DEMOTES it to volume (shadow) -> no premium pick, no alert.
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.steam_policy = SteamPolicy(enabled=True)
    deps.steam_history_loader = _steam_history_loader(3.80)

    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []  # demoted (unpersisted volume) -> not a premium pick
    assert sink.sent == []
    assert LAST_POLL["soccer"]["picks"] == 0


async def test_steam_gate_enabled_demotes_on_stale_anchor() -> None:
    # The anchor's prices are 3h old (> 2h freshness window): a stale anchor =
    # phantom edge. ENABLED steam gate demotes -> no alert.
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(stale_anchor_market()))
    deps.steam_policy = SteamPolicy(enabled=True)

    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []
    assert sink.sent == []
    assert LAST_POLL["soccer"]["picks"] == 0


async def test_steam_gate_shadow_keeps_tier_but_annotates() -> None:
    # SHADOW (enabled=False, the default): the SAME converging candidate stays
    # PREMIUM and alerts, but the verdict is surfaced on the pick for measurement.
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.steam_policy = SteamPolicy(enabled=False)  # shadow
    deps.steam_history_loader = _steam_history_loader(3.80)

    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].tier == "premium"  # tier UNCHANGED in shadow
    assert "steam(shadow)" in picks[0].reason_summary
    assert "soft_toward_anchor" in picks[0].reason_summary
    assert len(sink.sent) == 1
    assert LAST_POLL["soccer"]["picks"] == 1


async def test_steam_gate_enabled_inert_without_history() -> None:
    # With only the current cycle's single point per book (no history loader),
    # the gate cannot judge movement and the anchor is fresh -> no trip, even
    # ENABLED. The premium pick alerts unchanged.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.steam_policy = SteamPolicy(enabled=True)  # no steam_history_loader

    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].tier == "premium"
    assert "steam" not in picks[0].reason_summary


async def test_steam_gate_absent_is_strict_noop() -> None:
    # Default deps.steam_policy is None: the gate is ABSENT (no history read, no
    # verdict) — behaviour is byte-for-byte the pre-feature pick.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert "steam" not in picks[0].reason_summary
