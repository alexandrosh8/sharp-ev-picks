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
from app.notifications.base import CORRELATED_EXPOSURE_WARNING, Alert, build_pick_alert
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
    directory.register(
        "evt-1",
        EventTeams(
            home="Home FC",
            away="Away FC",
            league=league,
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


def longshot_market_snapshots(age_s: float = 30.0) -> list[OddsSnapshotIn]:
    # A 3-way whose ONLY +EV candidate is the AWAY LONGSHOT: Pinnacle prices Away
    # at 7.0 (sharp; power-devig fair ~0.133), SoftBook is generous at 10.0 (raw
    # odds above the 5.0 ceiling; probability-space edge ~+0.033). Home/Draw carry
    # no edge (the soft implied prob exceeds the sharp fair).
    return [
        snap("Pinnacle", "Home FC", 1.50, age_s),
        snap("Pinnacle", "Draw", 4.50, age_s),
        snap("Pinnacle", "Away FC", 7.00, age_s),
        snap("SoftBook", "Home FC", 1.48, age_s),
        snap("SoftBook", "Draw", 4.30, age_s),
        snap("SoftBook", "Away FC", 10.00, age_s),
    ]


async def test_moneyline_ceiling_off_mints_longshot_pick() -> None:
    # Baseline: with the ceiling OFF (bare ValuePolicy => math.inf) the only +EV
    # candidate is the AWAY longshot at raw odds 8.0 — it mints one premium pick.
    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(longshot_market_snapshots()),
        league="Premier League",
        value_policy=ValuePolicy(),  # moneyline ceiling OFF (math.inf)
    )
    await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1
    assert LAST_POLL["soccer"]["picks"] == 1


async def test_moneyline_ceiling_caps_longshot_to_shadow(caplog) -> None:  # type: ignore[no-untyped-def]
    # The 1X2 longshot band is structurally CLV-NEGATIVE vs the sharp close
    # (research 2026-06-30, ADR-0019 H1). With the ceiling at 5.0 the same Away
    # candidate (raw odds 10.0 > 5.0) is CAPPED at the volume (shadow) tier — never
    # alerted, never a premium pick, but (with a DB) persisted + CLV-tracked so the
    # band self-validates forward on own-captured data. Distinguishing shadow-cap
    # from a hard drop needs a DB to count the volume pick; here (no DB) we assert
    # the clean alerted set AND that the CAP path fired (the log line), proving it
    # is a shadow-cap, not a silent drop.
    import logging

    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps_league(
        sink,
        FakeLoader(longshot_market_snapshots()),
        league="Premier League",
        value_policy=ValuePolicy(moneyline_max_odds=5.0),
    )
    with caplog.at_level(logging.INFO):
        await run_value_pipeline(deps, "soccer")
    assert sink.sent == []  # longshot capped -> never alerted
    assert LAST_POLL["soccer"]["picks"] == 0  # not premium
    assert "moneyline odds ceiling capped" in caplog.text  # shadow-cap, not a drop


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


def impossible_edge_snapshots(age_s: float = 30.0) -> list[OddsSnapshotIn]:
    # Pinnacle prices a sane 3-way (fair Home ~0.39); a soft book offers Home at
    # an absurd 8.0 (implied 0.125) — an impossible ~26% edge, the shape a totals
    # line-loss or a stale/mislabeled anchor mints (DC 1.19-fair vs 3.25-offered).
    return [
        snap("Pinnacle", "Home FC", 2.50, age_s),
        snap("Pinnacle", "Draw", 3.30, age_s),
        snap("Pinnacle", "Away FC", 3.10, age_s),
        snap("SoftBook", "Home FC", 8.00, age_s),
        snap("SoftBook", "Draw", 3.20, age_s),
        snap("SoftBook", "Away FC", 2.95, age_s),
    ]


async def test_structural_sanity_demotes_impossible_edge_premium_to_shadow(caplog) -> None:  # type: ignore[no-untyped-def]
    # FIX 1: an impossible-edge premium candidate (edge > sanity_max_edge 0.15)
    # is HARD-DEMOTED to the volume (shadow) tier — never alerted, never a silent
    # drop. Neutralizes the reported phantom picks (DC 1.19-fair vs 3.25-offered ·
    # +53%; totals min-acc 2.06 > offered 1.67).
    import logging

    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(impossible_edge_snapshots()))
    with caplog.at_level(logging.WARNING):
        await run_value_pipeline(deps, "soccer")
    assert sink.sent == []  # never alerted as premium
    assert LAST_POLL["soccer"]["picks"] == 0  # n_premium == 0
    # demoted (to shadow), NOT dropped — the backstop logs the demotion.
    assert any("structural-sanity net demoted" in r.message for r in caplog.records)


async def test_structural_sanity_keeps_legit_small_edge_premium() -> None:
    # No false demotion: the normal ~4.5% edge in market_snapshots (offered
    # >= its own min-acceptable floor) stays PREMIUM and alerts.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    picks = await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1
    assert all(p.tier == "premium" for p in picks)
    assert all("STRUCTURAL SANITY" not in p.reason_summary for p in picks)


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
        rows = [
            snap("Betfair Exchange", "Home FC", 2.40),
            snap("Betfair Exchange", "Draw", 3.45),
            snap("Betfair Exchange", "Away FC", 3.25),
        ]
        return rows, {("evt-1", "sharp"): (1.0, "inline_betfair_canonical")}

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
        rows = [
            snap("Betfair Exchange", "Home FC", 3.40),
            snap("Betfair Exchange", "Draw", 3.50),
            snap("Betfair Exchange", "Away FC", 3.20),
            snap("Pinnacle", "Home FC", 2.40),
            snap("Pinnacle", "Draw", 3.45),
            snap("Pinnacle", "Away FC", 3.25),
        ]
        return rows, {
            ("evt-1", "sharp"): (1.0, "inline_betfair_canonical"),
            ("evt-1", "pinnacle"): (0.97, "jw_two_tier"),
        }

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
    assert "you place any bet" in sink.sent[0].body
    assert "No profit guaranteed" in sink.sent[0].body
    assert "value: Pinnacle fair" in pick.reason_summary


def totals_snap(book: str, sel: str, odds: float, detail: str | None = None) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-1",
        bookmaker=book,
        market=Market.TOTALS,
        market_detail=detail,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=30),
        ingested_at=now,
    )


def totals_market_snapshots(over: str, under: str) -> list[OddsSnapshotIn]:
    # Pinnacle tight; SoftBook generous on the Over -> a mintable value edge.
    return [
        totals_snap("Pinnacle", over, 1.90),
        totals_snap("Pinnacle", under, 1.90),
        totals_snap("SoftBook", over, 2.20),
        totals_snap("SoftBook", under, 1.75),
    ]


async def test_tennis_game_line_totals_candidate_dropped() -> None:
    # Our tennis results feed carries SET scores only — a GAME-line totals
    # candidate ("Over 22.5") can never be auto-settled honestly, so the
    # candidate gate must drop it before any pick is minted.
    sink = RecordingSink()
    loader = FakeLoader(totals_market_snapshots("Over 22.5", "Under 22.5"))
    picks = await run_value_pipeline(make_deps(sink, loader), "tennis")
    assert picks == []
    assert sink.sent == []


async def test_tennis_set_line_totals_candidate_kept() -> None:
    # The set-plausible sets-total line (Over/Under 2.5) stays mintable.
    sink = RecordingSink()
    loader = FakeLoader(totals_market_snapshots("Over 2.5", "Under 2.5"))
    picks = await run_value_pipeline(make_deps(sink, loader), "tennis")
    assert len(picks) == 1
    assert picks[0].selection == "Over 2.5"


async def test_integer_line_totals_candidate_dropped() -> None:
    # Observation 3232: an INTEGER-line totals group ("Over 3") has a push
    # outcome at exactly the line — the 2-way devig's exhaustive-outcomes
    # assumption fails, so the candidate gate must drop the whole group,
    # whether the detail token is the bare form, the `_0` form, or absent.
    for detail in ("totals_3", "totals_3_0", "over_under_3", None):
        sink = RecordingSink()
        snapshots = [
            totals_snap("Pinnacle", "Over 3", 1.90, detail),
            totals_snap("Pinnacle", "Under 3", 1.90, detail),
            totals_snap("SoftBook", "Over 3", 2.20, detail),
            totals_snap("SoftBook", "Under 3", 1.75, detail),
        ]
        picks = await run_value_pipeline(make_deps(sink, FakeLoader(snapshots)), "soccer")
        assert picks == [], f"integer-line totals minted under detail={detail!r}"
        assert sink.sent == []


async def test_half_line_totals_candidate_survives_integer_gate() -> None:
    # The half-line group (no push outcome) still mints — the gate is scoped
    # to integer lines only.
    sink = RecordingSink()
    snapshots = [
        totals_snap("Pinnacle", "Over 2.5", 1.90, "totals_2_5"),
        totals_snap("Pinnacle", "Under 2.5", 1.90, "totals_2_5"),
        totals_snap("SoftBook", "Over 2.5", 2.20, "totals_2_5"),
        totals_snap("SoftBook", "Under 2.5", 1.75, "totals_2_5"),
    ]
    picks = await run_value_pipeline(make_deps(sink, FakeLoader(snapshots)), "soccer")
    assert len(picks) == 1
    assert picks[0].selection == "Over 2.5"


async def test_soccer_big_line_totals_unaffected_by_tennis_gate() -> None:
    # The game-line drop is tennis-scoped: an identical big-line totals group
    # for another sport still mints (corner totals are handled separately by
    # the market_detail gate, which this test does not touch).
    sink = RecordingSink()
    loader = FakeLoader(totals_market_snapshots("Over 22.5", "Under 22.5"))
    picks = await run_value_pipeline(make_deps(sink, loader), "soccer")
    assert len(picks) == 1
    assert picks[0].selection == "Over 22.5"


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


async def test_premium_alert_flags_prior_same_event_exposure() -> None:
    # Dashboard same-game chip parity at the moment it matters: when the
    # daily-exposure ledger already carries reserved exposure for the pick's
    # event from a PRIOR grant today, the dispatched alert body carries the
    # correlation warning line. Informational only — sizing/gating untouched.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.ledger.preload_event(datetime.now(tz=UTC).date(), "evt-1", 0.01)

    picks = await run_value_pipeline(deps, "soccer")

    assert len(picks) == 1
    assert len(sink.sent) == 1
    assert CORRELATED_EXPOSURE_WARNING in sink.sent[0].body
    # The warning must NOT perturb the idempotency key: same key as the
    # un-warned rendering of the same pick (strategy identity included).
    expected = build_pick_alert(
        picks[0], deps.value_min_edge, model_name=deps.model_name, model_version=deps.model_version
    )
    assert sink.sent[0].dedupe_key == expected.dedupe_key


async def test_premium_alert_no_correlation_warning_without_prior_exposure() -> None:
    # A fresh ledger (no prior exposure on the event today) -> no warning line.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))

    picks = await run_value_pipeline(deps, "soccer")

    assert len(picks) == 1
    assert len(sink.sent) == 1
    assert CORRELATED_EXPOSURE_WARNING not in sink.sent[0].body
    expected = build_pick_alert(
        picks[0], deps.value_min_edge, model_name=deps.model_name, model_version=deps.model_version
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

    async def fake_update_pick_stake(*args, **kwargs):  # type: ignore[no-untyped-def]
        return True

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)
    monkeypatch.setattr(repos, "update_pick_stake", fake_update_pick_stake)


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


async def test_revalidation_failure_logs_type_only_without_secret_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import app.clv_trueup as clv_trueup

    patch_persist_dedupe_after_first(monkeypatch)
    sentinel = "https://proxy-user:SUPER-SECRET@proxy.invalid/path?apiKey=LEAK"

    async def explode(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(sentinel)

    monkeypatch.setattr(clv_trueup, "revalidate_open_picks", explode)
    monkeypatch.setattr(clv_trueup, "revalidate_offwindow_picks", explode)
    deps = make_deps(RecordingSink(), FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]

    with caplog.at_level("ERROR", logger="app.pipeline"):
        await run_value_pipeline(deps, "soccer")

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert messages.count("RuntimeError") >= 2
    assert "SUPER-SECRET" not in messages
    assert "apiKey=LEAK" not in messages
    assert all(record.exc_info is None for record in caplog.records)


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


async def test_cap_denied_inserted_premium_zeroes_stake_and_never_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WP2 (daily-cap bypass): a brand-new premium pick the exhausted daily cap
    denies (granted == 0) was already persisted at FULL stake BEFORE the
    reservation ran — the row must be rewritten to stake 0 (the cap-denial
    marker) and the alert withheld, and the NEXT cycle's re-detection
    ('duplicate_denied') must stay silent too. Without the marker the duplicate
    path re-dispatched the alert one cycle late at full stake with zero ledger
    accounting."""
    import app.storage.repositories as repos

    patch_persist_recording(monkeypatch, ["inserted", "duplicate_denied"])
    rewrites: list[float] = []

    async def spy_update_pick_stake(  # type: ignore[no-untyped-def]
        session, pick, teams, model_name, model_version, *, persist_tier=False, **kwargs
    ):
        rewrites.append(pick.recommended_stake_fraction)
        return True

    monkeypatch.setattr(repos, "update_pick_stake", spy_update_pick_stake)

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    day = datetime.now(tz=UTC).date()
    deps.ledger.reserve(day, deps.ledger.remaining(day))  # cap fully exhausted

    first = await run_value_pipeline(deps, "soccer")
    assert first == []  # cap-denied: not a pick this cycle
    assert sink.sent == []  # ... and no alert
    assert rewrites == [pytest.approx(0.0)]  # the row now carries the denial

    second = await run_value_pipeline(deps, "soccer")
    assert second == []
    assert sink.sent == []  # the duplicate must NOT late-fire the alert
    assert rewrites == [pytest.approx(0.0)]  # ... and nothing was rewritten again


def spy_stake_rewrites(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, float, str, bool]]:
    """Record every update_pick_stake call as (tier, stake_fraction,
    reason_summary, persist_tier) so tests can assert the demotion contract."""
    import app.storage.repositories as repos

    rewrites: list[tuple[str, float, str, bool]] = []

    async def spy_update_pick_stake(  # type: ignore[no-untyped-def]
        session, pick, teams, model_name, model_version, *, persist_tier=False, **kwargs
    ):
        rewrites.append(
            (pick.tier, pick.recommended_stake_fraction, pick.reason_summary, persist_tier)
        )
        return True

    monkeypatch.setattr(repos, "update_pick_stake", spy_update_pick_stake)
    return rewrites


async def test_cap_denied_inserted_premium_is_demoted_to_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 1 (2026-07-10 plan): a zero-grant INSERTED premium pick must not
    linger as a premium-tier stake-0 marker — the persisted row is DEMOTED to
    the volume tier (CLV-tracked, re-promotable when capacity frees) with the
    'stake_zero' demotion note, and nothing is alerted."""
    patch_persist_recording(monkeypatch, ["inserted"])
    rewrites = spy_stake_rewrites(monkeypatch)

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    day = datetime.now(tz=UTC).date()
    deps.ledger.reserve(day, deps.ledger.remaining(day))  # cap fully exhausted

    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []  # cap-denied: not a pick this cycle
    assert sink.sent == []  # ... and no alert
    assert len(rewrites) == 1
    tier, stake, reason, persist_tier = rewrites[0]
    assert tier == "volume"  # demoted, not a premium stake-0 marker
    assert stake == pytest.approx(0.0)
    assert "stake_zero" in reason
    assert persist_tier is True  # the row's tier is rewritten too


async def test_cap_denied_upgraded_premium_demotes_and_never_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pick 2332's actual path: a volume->premium UPGRADE whose exposure grant
    is 0 used to fire the premium alert at stake 0 (operator noise that
    mis-states the strategy). It must instead demote the row back to the
    volume tier with the 'stake_zero' note and stay silent — the next cycle's
    re-detection retries the upgrade when capacity frees."""
    patch_persist_recording(monkeypatch, ["upgraded"])
    rewrites = spy_stake_rewrites(monkeypatch)

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    day = datetime.now(tz=UTC).date()
    deps.ledger.reserve(day, deps.ledger.remaining(day))  # cap fully exhausted

    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []  # never a stake-0 premium pick
    assert sink.sent == []  # the defect: this used to alert at stake 0
    assert len(rewrites) == 1
    tier, stake, reason, persist_tier = rewrites[0]
    assert tier == "volume"
    assert stake == pytest.approx(0.0)
    assert "stake_zero" in reason
    assert persist_tier is True


async def test_unpersisted_premium_with_persistence_configured_withholds_alert(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WP2 fail-closed: persistence is CONFIGURED but the write FAILS (DB
    outage) -> 'unpersisted'. The pick can never be settled, seeded into the
    exposure ledger, or CLV-tracked, so its premium alert is WITHHELD and the
    count logged at WARNING. (Deps WITHOUT a session factory — persistence
    deliberately unconfigured — keep the historical alert-flowing behavior:
    see test_unpersisted_premium_pick_does_not_accumulate_exposure.)"""
    import logging as _logging

    import app.storage.repositories as repos

    async def failing_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        raise RuntimeError("db outage")

    monkeypatch.setattr(repos, "persist_pick", failing_persist_pick)

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    day = datetime.now(tz=UTC).date()

    with caplog.at_level(_logging.WARNING, logger="app.pipeline"):
        picks = await run_value_pipeline(deps, "soccer")
    assert picks == []  # fail closed: no pick without a persisted row
    assert sink.sent == []  # ... and no alert
    assert deps.ledger.used(day) == 0.0  # ... and no phantom reservation
    assert "withheld 1 premium alert" in caplog.text


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

    async def fake_update_pick_stake(*args, **kwargs):  # type: ignore[no-untyped-def]
        return True

    monkeypatch.setattr(repos, "persist_pick", fake_persist_pick)
    monkeypatch.setattr(repos, "update_pick_stake", fake_update_pick_stake)
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


def test_candidate_age_fails_closed_on_unknown_capture() -> None:
    """P2 (holes audit): the odds-age gate is a SAFETY gate and must fail
    CLOSED. A candidate whose best-book capture time is unknown (None) has an
    UNKNOWABLE age — minting from it (the old ``... if cap else 0.0`` which made
    age 0.0) silently bypasses the freshness guarantee. Unknown age => +inf so
    the gate always drops it. ``now`` is taken AFTER the fetch, so a capture in
    the FUTURE relative to it is a clock/data error, NOT a fresh price — it must
    also fail closed (+inf), never clamp to 0.0 (that let an in-play/mis-stamped
    row pose as fresh)."""
    from app.pipeline import _candidate_age_seconds

    now = datetime.now(tz=UTC)
    # Unknown capture time -> +inf -> always exceeds any finite freshness cap.
    assert _candidate_age_seconds(now, None) == float("inf")
    # Old price -> positive age (would trip a 300s gate).
    assert _candidate_age_seconds(now, now - timedelta(seconds=400)) == pytest.approx(400.0)
    # Future capture -> stale/invalid (+inf), NOT clamped to fresh.
    assert _candidate_age_seconds(now, now + timedelta(seconds=90)) == float("inf")


def test_drop_post_kickoff_snapshots_excludes_in_play() -> None:
    """A snapshot captured AT OR AFTER its event's kickoff is an in-play price and
    must be dropped from candidate pricing / the CLV close; strictly-pre-kickoff
    rows and rows for a NULL/unknown kickoff are KEPT (a NULL kickoff cannot be
    proven post-KO). The surviving 'close' is the last snapshot strictly before
    kickoff, never an in-play one."""
    from app.pipeline import drop_post_kickoff_snapshots

    ko = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    def s(event_id: str, offset_s: float) -> OddsSnapshotIn:
        return OddsSnapshotIn(
            event_id=event_id,
            bookmaker="pinnacle",
            market=Market.H2H,
            selection="Home",
            decimal_odds=2.0,
            captured_at=ko + timedelta(seconds=offset_s),
            ingested_at=ko + timedelta(seconds=offset_s),
        )

    pre_old = s("evt-1", -600)
    pre_close = s("evt-1", -60)  # last strictly-pre-kickoff row -> the close
    at_ko = s("evt-1", 0)  # exactly kickoff == in-play, dropped
    in_play = s("evt-1", 45)  # after kickoff, dropped
    null_ko = s("evt-null", 120)  # unknown kickoff -> kept
    kept = drop_post_kickoff_snapshots(
        [pre_old, pre_close, at_ko, in_play, null_ko],
        {"evt-1": ko, "evt-null": None},
    )
    assert at_ko not in kept
    assert in_play not in kept
    assert null_ko in kept
    evt1 = [x for x in kept if x.event_id == "evt-1"]
    assert set(evt1) == {pre_old, pre_close}
    # the CLV close = latest surviving capture is STRICTLY before kickoff
    assert max(x.captured_at for x in evt1) == pre_close.captured_at < ko


def test_started_event_ids_prefers_persisted_kickoff() -> None:
    """An event whose kickoff (persisted-preferred) is <= now is 'started' even
    when the in-memory directory never carried its start time; a future kickoff
    and a NULL/absent kickoff are never 'started'."""
    from app.pipeline import started_event_ids

    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    kickoffs = {
        "past": now - timedelta(minutes=5),  # persisted, absent from directory
        "future": now + timedelta(minutes=30),
        "null": None,
    }
    started = started_event_ids(["past", "future", "null", "absent"], kickoffs, now)
    assert started == {"past"}


async def test_stale_drop_ratio_observable_and_warns_on_starvation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """H2 (holes audit): when a slow cycle drops most mintable candidates for
    staleness the slate silently STARVES of picks — visible before only as the
    unalerted stale_candidates count. Expose the per-cycle STALE-DROP RATIO on
    LAST_POLL and emit a loud WARNING when it exceeds the configured threshold,
    so the self-audit layer can alert on starvation."""
    import logging as _logging

    from app.pipeline import LAST_POLL

    sink = RecordingSink()
    # Entire slate older than the 300s gate -> every mintable candidate stale.
    deps = make_deps(sink, FakeLoader(market_snapshots(age_s=400.0)))
    with caplog.at_level(_logging.WARNING, logger="app.pipeline"):
        await run_value_pipeline(deps, "soccer")
    assert LAST_POLL["soccer"]["stale_drop_ratio"] == pytest.approx(1.0)
    assert any("starv" in r.getMessage().lower() for r in caplog.records)

    # Fresh slate: ratio 0.0 and NO starvation warning.
    caplog.clear()
    deps2 = make_deps(sink, FakeLoader(market_snapshots()))
    with caplog.at_level(_logging.WARNING, logger="app.pipeline"):
        await run_value_pipeline(deps2, "soccer")
    assert LAST_POLL["soccer"]["stale_drop_ratio"] == pytest.approx(0.0)
    assert not any("starv" in r.getMessage().lower() for r in caplog.records)


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


async def test_value_pipeline_skips_unknown_kickoff_events() -> None:
    """Unknown kickoff is visibility-only: no quote can be proven pre-game."""
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.directory = EventDirectory()
    deps.directory.register(
        "evt-1",
        EventTeams(home="Home FC", away="Away FC", starts_at=None),
    )

    picks = await run_value_pipeline(deps, "soccer")

    assert picks == []
    assert sink.sent == []
    assert deps.ledger.used(datetime.now(tz=UTC).date()) == 0.0


async def test_value_pipeline_keeps_future_kickoff_events() -> None:
    # A known future kickoff proves the quote is still pre-game.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    assert deps.directory is not None
    deps.directory.register(
        "evt-1",
        EventTeams(home="Home FC", away="Away FC", starts_at=NOW + timedelta(hours=3)),
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1


async def test_value_pipeline_drops_future_captured_at() -> None:
    # ``now`` is taken AFTER the fetch, so a snapshot stamped in the FUTURE is a
    # clock/data error (or an in-play row with a bad stamp), never a fresh price.
    # The odds-age gate must DROP it (age +inf), not clamp it to fresh and mint.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots(age_s=-90.0)))  # future
    picks = await run_value_pipeline(deps, "soccer")
    assert picks == []


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
    # mint-time CANONICAL group detail stamped on the pick (exact CLV matching;
    # AH details canonicalize to themselves — the spreads merge is audited OFF)
    assert picks[0].market_detail == "asian_handicap_-1_5"


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


def spy_candidate_audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, tuple[str, ...]]]:
    """Capture every _record_candidate_audit call as (pick, reasons) so no-DB
    tests can assert the demotion note + audit-slug contract for volume picks
    (which are otherwise dropped without a session factory)."""
    import app.pipeline as pl

    captured: list[tuple[object, tuple[str, ...]]] = []

    async def spy(  # type: ignore[no-untyped-def]
        deps, pick, market_detail, reasons, anchor_age_seconds, now
    ) -> None:
        captured.append((pick, reasons))

    monkeypatch.setattr(pl, "_record_candidate_audit", spy)
    return captured


async def test_per_market_floor_demotion_is_noted_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The candidate (~0.045 edge) clears the GLOBAL premium floor (0.015) but
    # not the h2h override (0.50): the demotion must be surfaced — a
    # "market floor" note on reason_summary (dashboard chips) and a
    # "market_floor" slug in the candidate-audit reasons — never silent.
    captured = spy_candidate_audit(monkeypatch)
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.value_policy = ValuePolicy(min_edge_by_market=(("h2h", 0.50),))
    await run_value_pipeline(deps, "soccer")
    assert sink.sent == []  # still demoted, never alerted
    assert len(captured) == 1
    pick, reasons = captured[0]
    assert pick.tier == "volume"  # type: ignore[attr-defined]
    assert "market floor: edge 0.0" in pick.reason_summary  # type: ignore[attr-defined]
    assert "h2h floor 0.5" in pick.reason_summary  # type: ignore[attr-defined]
    assert "market_floor" in reasons


async def test_ordinary_volume_pick_below_global_floor_has_no_market_floor_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pick below the GLOBAL premium floor (no per-market override involved)
    # is ordinary volume — the market-floor note must stay silent.
    captured = spy_candidate_audit(monkeypatch)
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.value_min_edge = 0.50  # global floor above the ~0.045 edge
    await run_value_pipeline(deps, "soccer")
    assert sink.sent == []
    assert len(captured) == 1
    pick, reasons = captured[0]
    assert pick.tier == "volume"  # type: ignore[attr-defined]
    assert "market floor" not in pick.reason_summary  # type: ignore[attr-defined]
    assert "market_floor" not in reasons


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


# --- A5: steam SHADOW-VERDICT stamping (observability only) ------------------
# The four steam_* fields record what the gate saw/decided at mint; they must
# never gate, demote, reorder, or raise. NULLs = not evaluated (no policy /
# consensus anchor / eval error) — never fabricated.


async def test_steam_shadow_verdict_stamped_when_tripped() -> None:
    # The converging SHADOW candidate stays premium (behavior unchanged) AND the
    # verdict is persisted on the pick: tripped, reason slugs, numeric detail.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.steam_policy = SteamPolicy(enabled=False)  # shadow
    deps.steam_history_loader = _steam_history_loader(3.80)

    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    p = picks[0]
    assert p.tier == "premium"  # tier UNCHANGED — observability only
    assert p.steam_tripped is True
    assert p.steam_reasons == "soft_toward_anchor"
    assert p.steam_closed_fraction is not None
    assert p.steam_closed_fraction >= 0.5  # >= policy close_frac by construction
    assert p.steam_anchor_age_seconds is not None
    assert 0.0 <= p.steam_anchor_age_seconds < 3600.0  # fresh anchor this cycle


async def test_steam_shadow_verdict_stamped_when_clean() -> None:
    # Policy configured but only the current cycle's points exist: the gate
    # cannot judge movement and the anchor is fresh -> EVALUATED and clean.
    # tripped=False (not NULL) distinguishes "looked and passed" from "never
    # looked"; the unavailable numerics stay None (never fabricated).
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.steam_policy = SteamPolicy(enabled=False)  # shadow, no history loader

    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    p = picks[0]
    assert p.tier == "premium"
    assert p.steam_tripped is False
    assert p.steam_reasons is None  # no component flag raised
    assert p.steam_closed_fraction is None  # < min_points: no movement judgement
    assert p.steam_anchor_age_seconds is not None  # anchor observed this cycle


async def test_steam_shadow_nulls_when_gate_unconfigured() -> None:
    # steam_policy None (gate absent): the verdict is NEVER computed -> all four
    # fields NULL, exactly like a pre-column row.
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))

    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    p = picks[0]
    assert p.steam_tripped is None
    assert p.steam_reasons is None
    assert p.steam_closed_fraction is None
    assert p.steam_anchor_age_seconds is None


async def test_steam_shadow_eval_error_stamps_nulls_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A steam-eval crash must NEVER break picking: the pick mints unchanged
    # (premium, alerted) with NULL steam fields — fail-safe observability.
    def _boom(**_kwargs: object) -> object:
        raise RuntimeError("synthetic steam-eval failure")

    monkeypatch.setattr("app.pipeline.evaluate_steam", _boom)
    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.steam_policy = SteamPolicy(enabled=False)
    deps.steam_history_loader = _steam_history_loader(3.80)

    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    p = picks[0]
    assert p.tier == "premium"
    assert "steam" not in p.reason_summary
    assert p.steam_tripped is None
    assert p.steam_reasons is None
    assert p.steam_closed_fraction is None
    assert p.steam_anchor_age_seconds is None
    assert len(sink.sent) == 1


async def test_steam_shadow_does_not_change_picks_tiers_or_order() -> None:
    # Proof of no behavior change: the SAME market with the shadow eval ON
    # (verdict tripping) yields the same picks, tiers, and order as with the
    # gate absent — only the stamped fields and the reason note differ.
    def _key(p: object) -> tuple[object, ...]:
        return (
            p.event_id,  # type: ignore[attr-defined]
            str(p.market),  # type: ignore[attr-defined]
            p.selection,  # type: ignore[attr-defined]
            p.tier,  # type: ignore[attr-defined]
            p.edge,  # type: ignore[attr-defined]
        )

    baseline_sink = RecordingSink()
    baseline_deps = make_deps(baseline_sink, FakeLoader(market_snapshots()))
    baseline = await run_value_pipeline(baseline_deps, "soccer")

    shadow_sink = RecordingSink()
    shadow_deps = make_deps(shadow_sink, FakeLoader(market_snapshots()))
    shadow_deps.steam_policy = SteamPolicy(enabled=False)
    shadow_deps.steam_history_loader = _steam_history_loader(3.80)
    shadowed = await run_value_pipeline(shadow_deps, "soccer")

    assert [_key(p) for p in shadowed] == [_key(p) for p in baseline]
    assert len(shadow_sink.sent) == len(baseline_sink.sent)
    # and the shadow run actually evaluated (guards against a vacuous pass)
    assert shadowed[0].steam_tripped is True


def two_event_snapshots(soft_home_a: float, soft_home_b: float) -> list[OddsSnapshotIn]:
    """Two independent H2H events, each Pinnacle-anchored (identical sharp lines)
    with SoftBook overpricing Home. evt-A is iterated FIRST (snapshot order ->
    dict insertion order in group_market_prices). A larger SoftBook Home price
    means a larger raw Kelly at the SAME sharp fair prob, so the caller sets the
    raw_kelly ordering purely through soft_home_a vs soft_home_b."""
    now = datetime.now(tz=UTC)

    def s(ev: str, book: str, sel: str, odds: float) -> OddsSnapshotIn:
        return OddsSnapshotIn(
            event_id=ev,
            bookmaker=book,
            market=Market.H2H,
            selection=sel,
            decimal_odds=odds,
            captured_at=now - timedelta(seconds=30),
            ingested_at=now,
        )

    out: list[OddsSnapshotIn] = []
    for ev, soft_home in (("evt-A", soft_home_a), ("evt-B", soft_home_b)):
        out += [
            s(ev, "Pinnacle", "Home FC", 2.50),
            s(ev, "Pinnacle", "Draw", 3.30),
            s(ev, "Pinnacle", "Away FC", 3.10),
            s(ev, "SoftBook", "Home FC", soft_home),
        ]
    return out


def make_deps_two_events(
    sink: RecordingSink, loader: FakeLoader, *, max_daily: float
) -> PipelineDeps:
    """make_deps with BOTH evt-A and evt-B registered and a caller-set daily
    exposure cap so the cap can be made to bind on a 2-pick slate."""
    directory = EventDirectory()
    kickoff = datetime.now(tz=UTC) + timedelta(hours=6)
    directory.register("evt-A", EventTeams(home="Home FC", away="Away FC", starts_at=kickoff))
    directory.register("evt-B", EventTeams(home="Home FC", away="Away FC", starts_at=kickoff))
    deps = PipelineDeps(
        loader=loader,
        model=NullModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=POLICY,
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=max_daily),
        bankroll=Decimal("1000"),
        directory=directory,
        value_min_edge=0.015,
        value_min_odds=1.30,
    )
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    return deps


async def test_daily_cap_funds_highest_raw_kelly_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the daily-exposure cap binds, the HIGHEST-raw_kelly (highest-growth)
    pick must fund first — even when it is iterated AFTER a lower-raw_kelly pick.

    evt-A (SoftBook Home 3.20, raw_kelly ~0.115) iterates BEFORE evt-B (SoftBook
    Home 4.00, raw_kelly ~0.189); both cap to final 0.02. With a 0.02 daily cap
    exactly one funds. Under the old iteration-order reservation evt-A won the
    budget and evt-B was skipped; ranking the ledger reservation by raw_kelly
    funds evt-B (the higher-growth pick) instead."""
    patch_persist_recording(monkeypatch, ["inserted", "inserted"])

    sink = RecordingSink()
    deps = make_deps_two_events(sink, FakeLoader(two_event_snapshots(3.20, 4.00)), max_daily=0.02)
    day = datetime.now(tz=UTC).date()

    picks = await run_value_pipeline(deps, "soccer")

    assert [p.event_id for p in picks] == ["evt-B"]  # higher raw_kelly funded
    assert picks[0].tier == "premium"
    assert [a.title for a in sink.sent].count("evt-A") == 0  # evt-A never alerted
    assert len(sink.sent) == 1  # exactly the funded pick alerted
    assert deps.ledger.used(day) == pytest.approx(0.02)  # cap fully bound by one pick


async def test_equal_raw_kelly_keeps_deterministic_iteration_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ties break by iteration order: the raw_kelly sort is STABLE, so two picks
    with IDENTICAL raw_kelly fund in iteration order. evt-A (iterated first) wins
    the single 0.02 slot when both carry the same SoftBook Home price."""
    patch_persist_recording(monkeypatch, ["inserted", "inserted"])

    sink = RecordingSink()
    deps = make_deps_two_events(sink, FakeLoader(two_event_snapshots(4.00, 4.00)), max_daily=0.02)
    day = datetime.now(tz=UTC).date()

    picks = await run_value_pipeline(deps, "soccer")

    assert [p.event_id for p in picks] == ["evt-A"]  # tie -> first iterated funds
    assert deps.ledger.used(day) == pytest.approx(0.02)


async def test_deferred_premium_keeps_volume_and_funds_all_when_cap_loose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No behaviour change when the cap does not bind: BOTH premium picks fund and
    alert (deferral never drops a fundable pick), and a same-cycle VOLUME pick
    still persists inline with NO alert and NO exposure (volume path unchanged)."""
    # evt-A Home premium, evt-B Home premium, evt-A Draw lands in the volume band.
    now = datetime.now(tz=UTC)

    def s(ev: str, book: str, sel: str, odds: float) -> OddsSnapshotIn:
        return OddsSnapshotIn(
            event_id=ev,
            bookmaker=book,
            market=Market.H2H,
            selection=sel,
            decimal_odds=odds,
            captured_at=now - timedelta(seconds=30),
            ingested_at=now,
        )

    snaps = [
        s("evt-A", "Pinnacle", "Home FC", 2.50),
        s("evt-A", "Pinnacle", "Draw", 3.30),
        s("evt-A", "Pinnacle", "Away FC", 3.10),
        s("evt-A", "SoftBook", "Home FC", 3.20),
        s("evt-A", "SoftBook", "Draw", 3.65),  # ~2.1% edge on Draw -> volume tier
        s("evt-B", "Pinnacle", "Home FC", 2.50),
        s("evt-B", "Pinnacle", "Draw", 3.30),
        s("evt-B", "Pinnacle", "Away FC", 3.10),
        s("evt-B", "SoftBook", "Home FC", 4.00),
    ]
    seen = patch_persist_recording(monkeypatch, ["inserted", "inserted", "inserted"])

    sink = RecordingSink()
    # Generous daily cap: nothing binds.
    deps = make_deps_two_events(sink, FakeLoader(snaps), max_daily=0.05)
    deps.value_min_edge = 0.05  # Draw (~3.5% edge) cannot reach premium
    deps.value_volume_min_edge = 0.015
    day = datetime.now(tz=UTC).date()

    picks = await run_value_pipeline(deps, "soccer")

    # Both Home premium picks funded (ranked B before A), the Draw stays volume.
    premium = [p for p in picks if p.tier == "premium"]
    volume = [p for p in picks if p.tier == "volume"]
    assert {p.event_id for p in premium} == {"evt-A", "evt-B"}
    assert [p.event_id for p in premium] == ["evt-B", "evt-A"]  # raw_kelly order
    assert [(p.event_id, p.selection) for p in volume] == [("evt-A", "Draw")]
    # Volume persisted inline (recorded) but never alerted; only the 2 premium
    # picks alerted; the volume pick reserved no exposure.
    assert ("Draw", "volume") in seen
    assert len(sink.sent) == 2
    assert deps.ledger.used(day) == pytest.approx(0.04)  # two 0.02 premium reserves only


async def test_anchor_match_provenance_per_path() -> None:
    """R1 per-path anchor_match_confidence/method contract on minted picks:
    pinnacle -> the injector's map entry; pinnacle WITHOUT a map entry ->
    None/'unscored' (fail honest, never fabricate 1.0); inline sharp (Betfair)
    -> 1.0/'inline_betfair_canonical'; consensus -> None/None."""
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

    # CONSENSUS path: no injector -> None/None.
    sink0 = RecordingSink()
    picks0 = await run_value_pipeline(make_deps(sink0, FakeLoader(list(soft))), "soccer")
    assert picks0 and all(p.anchor_type == "consensus" for p in picks0)
    assert all(p.anchor_match_confidence is None for p in picks0)
    assert all(p.anchor_match_method is None for p in picks0)

    # PINNACLE path WITH a provenance entry -> the map's (confidence, method).
    async def pinnacle_loader(sport_key, snapshots):  # type: ignore[no-untyped-def]
        rows = [
            snap("Pinnacle", "Home FC", 2.40),
            snap("Pinnacle", "Draw", 3.45),
            snap("Pinnacle", "Away FC", 3.25),
        ]
        return rows, {("evt-1", "pinnacle"): (0.9765, "jw_two_tier")}

    sink1 = RecordingSink()
    deps1 = replace(make_deps(sink1, FakeLoader(list(soft))), sharp_anchor_loader=pinnacle_loader)
    picks1 = await run_value_pipeline(deps1, "soccer")
    assert picks1 and all(p.anchor_type == "pinnacle" for p in picks1)
    assert all(p.anchor_match_confidence == 0.9765 for p in picks1)
    assert all(p.anchor_match_method == "jw_two_tier" for p in picks1)

    # PINNACLE path with NO provenance entry -> None + 'unscored' (honest).
    async def unscored_pinnacle_loader(sport_key, snapshots):  # type: ignore[no-untyped-def]
        rows = [
            snap("Pinnacle", "Home FC", 2.40),
            snap("Pinnacle", "Draw", 3.45),
            snap("Pinnacle", "Away FC", 3.25),
        ]
        return rows, {}

    sink2 = RecordingSink()
    deps2 = replace(
        make_deps(sink2, FakeLoader(list(soft))), sharp_anchor_loader=unscored_pinnacle_loader
    )
    picks2 = await run_value_pipeline(deps2, "soccer")
    assert picks2 and all(p.anchor_type == "pinnacle" for p in picks2)
    assert all(p.anchor_match_confidence is None for p in picks2)
    assert all(p.anchor_match_method == "unscored" for p in picks2)

    # INLINE SHARP (Betfair) path -> 1.0/'inline_betfair_canonical' (constant —
    # holds even for inline rows arriving in the MAIN scrape with no map entry).
    async def betfair_loader(sport_key, snapshots):  # type: ignore[no-untyped-def]
        rows = [
            snap("Betfair Exchange", "Home FC", 2.40),
            snap("Betfair Exchange", "Draw", 3.45),
            snap("Betfair Exchange", "Away FC", 3.25),
        ]
        return rows, {}

    sink3 = RecordingSink()
    deps3 = replace(make_deps(sink3, FakeLoader(list(soft))), sharp_anchor_loader=betfair_loader)
    picks3 = await run_value_pipeline(deps3, "soccer")
    assert picks3 and all(p.anchor_type == "sharp" for p in picks3)
    assert all(p.anchor_match_confidence == 1.0 for p in picks3)
    assert all(p.anchor_match_method == "inline_betfair_canonical" for p in picks3)

    # PROMOTE-ON honest downgrade (verifier finding): with the Betfair API
    # promoted, an exchange row on the canonical event may have been attached
    # by the FUZZY ingestion matcher and is indistinguishable per-row from an
    # inline row — sharp anchors must stop claiming 1.0 and store the explicit
    # unattributed marker instead (never a fabricated confidence).
    sink4 = RecordingSink()
    deps4 = replace(
        make_deps(sink4, FakeLoader(list(soft))),
        sharp_anchor_loader=betfair_loader,
    )
    deps4 = replace(deps4, value_policy=replace(deps4.value_policy, betfair_api_promote=True))
    picks4 = await run_value_pipeline(deps4, "soccer")
    assert picks4 and all(p.anchor_type == "sharp" for p in picks4)
    assert all(p.anchor_match_confidence is None for p in picks4)
    assert all(p.anchor_match_method == "inline_or_promoted_unattributed" for p in picks4)


async def test_kickoff_moved_earlier_is_guarded_in_the_same_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted kickoff goes STALE when the match is rescheduled EARLIER: the
    fresh scrape (directory) carries the corrected time, but _load_kickoffs
    prefers the persisted ``events.starts_at``. The kickoff REFRESH must
    therefore run BEFORE the post-kickoff guard loads kickoffs — with the old
    refresh-after-guard ordering, the cycle that first saw the corrected time
    still priced the started match from in-play odds (one-cycle mint window)."""
    import app.storage.repositories as repos

    patch_persist_recording(monkeypatch, ["inserted"])
    now = datetime.now(tz=UTC)
    # The DB still holds the ORIGINAL (stale, future) kickoff.
    db_kickoffs: dict[str, datetime | None] = {"evt-1": now + timedelta(hours=2)}

    async def fake_load_event_kickoffs(session, event_ids):  # type: ignore[no-untyped-def]
        return {event_id: db_kickoffs.get(event_id) for event_id in event_ids}

    async def fake_refresh_event_kickoffs(session, kickoffs):  # type: ignore[no-untyped-def]
        # Real->real refresh is accepted (prefer_kickoff same-quality update).
        changed = 0
        for event_id, starts_at in kickoffs.items():
            if db_kickoffs.get(event_id) != starts_at:
                db_kickoffs[event_id] = starts_at
                changed += 1
        return changed

    monkeypatch.setattr(repos, "load_event_kickoffs", fake_load_event_kickoffs)
    monkeypatch.setattr(repos, "refresh_event_kickoffs", fake_refresh_event_kickoffs)

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    deps.session_factory = FakeSessionFactory()  # type: ignore[assignment]
    assert deps.directory is not None
    # The fresh scrape knows the match was moved earlier and already kicked off.
    deps.directory.register(
        "evt-1",
        EventTeams(home="Home FC", away="Away FC", starts_at=now - timedelta(minutes=10)),
    )

    picks = await run_value_pipeline(deps, "soccer")

    assert picks == []  # in-play prices on a started match must never mint
    assert sink.sent == []


async def test_cycle_cancellation_never_leaks_persisted_unreserved_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watchdog-cancellation safety (2026-07-08 audit): a premium pick's DB
    persist and its daily-exposure reservation must land as ONE unit. Cancel the
    cycle while the FIRST premium persist is in flight: cancellation must not
    propagate until the pair completes (row persisted AND stake on the ledger),
    and the not-yet-started candidate must not have been persisted at all —
    never a persisted full-stake row the daily/per-event caps don't count."""
    import asyncio

    import app.storage.repositories as repos

    persist_started = asyncio.Event()
    release_persist = asyncio.Event()
    persisted: list[str] = []

    async def blocking_persist_pick(session, pick, teams, model_name, model_version):  # type: ignore[no-untyped-def]
        persisted.append(pick.event_id)
        if len(persisted) == 1:
            persist_started.set()
            await release_persist.wait()  # the watchdog fires mid-persist
        return "inserted"

    async def fake_update_pick_stake(*args, **kwargs):  # type: ignore[no-untyped-def]
        return True

    monkeypatch.setattr(repos, "persist_pick", blocking_persist_pick)
    monkeypatch.setattr(repos, "update_pick_stake", fake_update_pick_stake)

    sink = RecordingSink()
    deps = make_deps_two_events(sink, FakeLoader(two_event_snapshots(3.20, 4.00)), max_daily=0.05)
    day = datetime.now(tz=UTC).date()

    task = asyncio.create_task(run_value_pipeline(deps, "soccer"))
    await asyncio.wait_for(persist_started.wait(), timeout=5.0)
    task.cancel()
    await asyncio.sleep(0)

    # Cancellation is held at the call site while the atomic child owns DB and
    # ledger resources; teardown cannot begin with that child still running.
    assert not task.done()
    assert deps.ledger.used(day) == 0.0  # persist is still blocked before reserve

    release_persist.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    # Only the in-flight (highest-raw_kelly: evt-B) persist ran; the deferred
    # evt-A candidate was cancelled BEFORE its persist -> nothing to orphan.
    assert persisted == ["evt-B"]
    # ...and the persisted pick's stake IS reserved on the ledger (no leak).
    assert deps.ledger.used(day) == pytest.approx(0.02)
    assert sink.sent == []  # cancelled before dispatch; next cycle re-alerts
