"""Backtest-gated sports enablement (2026-06-16).

Doctrine: a new sport earns LIVE ALERTS only after a held-out backtest clears
incremental CLV vs the closing line > 2 SE; otherwise it is VISIBILITY-ONLY,
marked UNVALIDATED, and mints NO picks/alerts. Tennis is visibility-only (its
held-out CLV is undefined — no closing source); NFL is rejected (no free
sharp+close source) and gets no live code at all.

These tests cover, per the task brief:
1. config validation for the new tennis sport keys (devig-sound, budget guard,
   OFF by default);
2. the loader maps the devig-sound tennis markets and rejects the rest;
3. the unvalidated flag flows into AVAILABLE_GAMES and visibility-only sports
   mint NO picks / send NO alerts / reserve NO exposure;
4. scheduler wiring enables tennis only when leagues are set and tags it
   visibility-only.

No win-rate claims anywhere — evaluation currency is held-out CLV/ROI.
"""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.edge.gates import GatePolicy
from app.ingestion.base import EventDirectory, EventTeams
from app.ingestion.oddsportal import (
    _line_from_key,
    _market_for_key,
    _selections,
    _validate_markets,
)
from app.models.base import NullModel
from app.notifications.base import Alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import (
    AVAILABLE_GAMES,
    LAST_POLL,
    PipelineDeps,
    run_pick_pipeline,
    run_value_pipeline,
)
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

NOW = datetime.now(tz=UTC)


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Config validation for the new tennis sport keys
# ---------------------------------------------------------------------------


def test_tennis_polling_off_by_default() -> None:
    # Visibility-only AND a third sport across 151 leagues -> OFF by default.
    assert make_settings().oddsportal_tennis_leagues == ""


def test_default_tennis_markets_are_devig_sound() -> None:
    # The configured default tennis market list must pass the loader's
    # devig-soundness gate (match_winner + half-line totals/handicaps).
    markets = tuple(m.strip() for m in make_settings().oddsportal_tennis_markets.split(","))
    _validate_markets(markets)  # raises on any unsound key


def test_tennis_all_leagues_market_budget_is_enforced() -> None:
    # 'all' leagues + too many markets is a fatal config error, same guard as
    # football/basketball — otherwise a worldwide tennis scrape runs for hours.
    over_budget = (
        "match_winner,over_under_sets_2_5,over_under_sets_3_5,"
        "over_under_games_22_5,over_under_games_23_5"
    )
    with pytest.raises(ValueError, match="TENNIS"):
        make_settings(oddsportal_tennis_leagues="all", oddsportal_tennis_markets=over_budget)


def test_tennis_all_leagues_within_budget_loads() -> None:
    s = make_settings(
        oddsportal_tennis_leagues="all",
        oddsportal_tennis_markets="match_winner,over_under_sets_2_5,over_under_games_22_5",
    )
    assert s.oddsportal_tennis_leagues == "all"


def test_no_nfl_config_flags_exist() -> None:
    # NFL is REJECTED (no free sharp+close source) — it must have no config
    # slots so it cannot be toggled on by accident.
    assert not any("nfl" in name.lower() for name in Settings.model_fields)
    assert not any("american_football" in name.lower() for name in Settings.model_fields)


# ---------------------------------------------------------------------------
# 2. Loader maps the devig-sound tennis markets, rejects the rest
# ---------------------------------------------------------------------------


def test_tennis_match_winner_maps_to_h2h_with_player_labels() -> None:
    assert _market_for_key("match_winner") is Market.H2H
    assert _selections("match_winner", "Alcaraz", "Sinner") == [
        ("player_1", "Alcaraz"),
        ("player_2", "Sinner"),
    ]


@pytest.mark.parametrize(
    ("key", "expected_line"),
    [
        ("over_under_sets_2_5", 2.5),
        ("over_under_games_22_5", 22.5),
        ("asian_handicap_-1_5_sets", -1.5),
        ("asian_handicap_+2_5_games", 2.5),
    ],
)
def test_tennis_line_parsing_strips_axis_suffix(key: str, expected_line: float) -> None:
    assert _line_from_key(key) == expected_line


def test_tennis_totals_use_over_under_labels() -> None:
    # Sets and games totals both carry the upstream odds_over/odds_under labels.
    assert _selections("over_under_sets_2_5", "A", "B") == [
        ("odds_over", "Over 2.5"),
        ("odds_under", "Under 2.5"),
    ]
    assert _selections("over_under_games_22_5", "A", "B") == [
        ("odds_over", "Over 22.5"),
        ("odds_under", "Under 22.5"),
    ]


def test_tennis_ah_sets_and_games_use_distinct_player_labels() -> None:
    # Tennis AH labels differ from football's team1_handicap — a mismatch here
    # would silently drop every tennis-AH snapshot.
    assert _selections("asian_handicap_-1_5_sets", "Alcaraz", "Sinner") == [
        ("sets_handicap_player_1", "Alcaraz -1.5"),
        ("sets_handicap_player_2", "Sinner +1.5"),
    ]
    assert _selections("asian_handicap_+2_5_games", "Alcaraz", "Sinner") == [
        ("games_handicap_player_1", "Alcaraz +2.5"),
        ("games_handicap_player_2", "Sinner -2.5"),
    ]


def test_tennis_integer_and_zero_ah_lines_are_rejected() -> None:
    # Integer / zero AH lines have push outcomes -> not pairwise devig-sound.
    for key in ("asian_handicap_-1_0_sets", "asian_handicap_0_sets"):
        with pytest.raises(ValueError, match="half line"):
            _validate_markets([key])


def test_tennis_correct_score_is_unsupported() -> None:
    # Many-outcome market with no pairwise devig path.
    assert _market_for_key("correct_score_2_0") is None
    with pytest.raises(ValueError, match="unsupported"):
        _validate_markets(["correct_score_2_0"])


def test_basketball_markets_unchanged_by_tennis_branches() -> None:
    # Regression guard: tennis axis handling must not disturb basketball keys.
    assert _line_from_key("over_under_games_220_5") == 220.5
    assert _selections("over_under_games_220_5", "A", "B") == [
        ("odds_over", "Over 220.5"),
        ("odds_under", "Under 220.5"),
    ]
    assert _selections("asian_handicap_games_-7_5_games", "Home", "Away") == [
        ("handicap_team_1", "Home -7.5"),
        ("handicap_team_2", "Away +7.5"),
    ]


# ---------------------------------------------------------------------------
# 3. Visibility-only behaviour in the pipeline (unvalidated flag, no picks)
# ---------------------------------------------------------------------------


class FakeLoader:
    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        self.snapshots = snapshots
        self.last_fetch_matches: dict[str, int] = {}
        self.last_fetch_event_ids: dict[str, tuple[str, ...]] = {}

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        self.last_fetch_matches[sport_key] = len({s.event_id for s in self.snapshots})
        self.last_fetch_event_ids[sport_key] = tuple(
            dict.fromkeys(s.event_id for s in self.snapshots)
        )
        return self.snapshots


class RecordingSink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def tennis_snapshots() -> list[OddsSnapshotIn]:
    # A multi-book match-winner market with an obvious soft outlier — under the
    # value strategy this WOULD mint a pick for a validated sport.
    captured = NOW - timedelta(seconds=30)

    def s(book: str, sel: str, odds: float) -> OddsSnapshotIn:
        return OddsSnapshotIn(
            event_id="tennis-evt-1",
            bookmaker=book,
            market=Market.H2H,
            selection=sel,
            decimal_odds=odds,
            captured_at=captured,
            ingested_at=NOW,
            market_detail="match_winner",
        )

    return [
        s("Pinnacle", "Alcaraz", 1.50),
        s("Pinnacle", "Sinner", 2.60),
        s("SoftBook", "Alcaraz", 1.55),
        s("SoftBook", "Sinner", 3.40),  # soft outlier on Sinner
    ]


def make_tennis_deps(sink: RecordingSink, loader: FakeLoader) -> PipelineDeps:
    directory = EventDirectory()
    directory.register(
        "tennis-evt-1",
        EventTeams(home="Alcaraz", away="Sinner", league="ATP US Open"),
    )
    return PipelineDeps(
        loader=loader,
        model=NullModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=GatePolicy(
            min_edge=0.0,
            min_ev=0.0,
            min_confidence=0.0,
            max_odds_age_seconds=300,
            min_liquidity=0.0,
        ),
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        directory=directory,
        value_min_edge=0.0,  # would alert any edge for a validated sport
        value_min_odds=1.30,
        visibility_only_sports=frozenset({"tennis"}),
    )


@pytest.fixture(autouse=True)
def _clear_registries() -> Iterator[None]:
    yield
    AVAILABLE_GAMES.pop("tennis", None)
    LAST_POLL.pop("tennis", None)


async def test_visibility_only_sport_mints_no_picks_or_alerts() -> None:
    sink = RecordingSink()
    deps = make_tennis_deps(sink, FakeLoader(tennis_snapshots()))
    picks = await run_value_pipeline(deps, "tennis")
    assert picks == []
    assert sink.sent == []  # no alerts dispatched
    # exposure ledger untouched
    assert deps.ledger.used(NOW.date()) == 0.0


async def test_visibility_only_rows_tagged_unvalidated() -> None:
    sink = RecordingSink()
    await run_value_pipeline(make_tennis_deps(sink, FakeLoader(tennis_snapshots())), "tennis")
    games = AVAILABLE_GAMES["tennis"]
    assert games, "tennis slate should still publish to AVAILABLE GAMES"
    assert all(row["unvalidated"] is True for row in games)
    assert all(row["sport"] == "tennis" for row in games)
    assert all(row["sport_label"] == "Tennis" for row in games)
    # and the poll headline records zero picks
    assert LAST_POLL["tennis"]["picks"] == 0


async def test_validated_sport_rows_not_flagged_unvalidated() -> None:
    # A normal (soccer) cycle must carry unvalidated=False so the flag is a
    # reliable discriminator the dashboard can badge on.
    sink = RecordingSink()
    directory = EventDirectory()
    directory.register("evt-soccer", EventTeams(home="Home FC", away="Away FC"))
    captured = NOW - timedelta(seconds=20)
    snaps = [
        OddsSnapshotIn(
            event_id="evt-soccer",
            bookmaker=book,
            market=Market.H2H,
            selection=sel,
            decimal_odds=odds,
            captured_at=captured,
            ingested_at=NOW,
            market_detail="1x2",
        )
        for book, sel, odds in (
            ("Pinnacle", "Home FC", 2.5),
            ("Pinnacle", "Draw", 3.3),
            ("Pinnacle", "Away FC", 3.1),
        )
    ]
    deps = PipelineDeps(
        loader=FakeLoader(snaps),
        model=NullModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=GatePolicy(
            min_edge=0.0,
            min_ev=0.0,
            min_confidence=0.0,
            max_odds_age_seconds=300,
            min_liquidity=0.0,
        ),
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        directory=directory,
        value_min_edge=0.015,
        value_min_odds=1.30,
    )
    try:
        await run_value_pipeline(deps, "soccer")
        assert all(row["unvalidated"] is False for row in AVAILABLE_GAMES["soccer"])
    finally:
        AVAILABLE_GAMES.pop("soccer", None)
        LAST_POLL.pop("soccer", None)


async def test_visibility_only_blocks_picks_under_model_strategy_too() -> None:
    # Defense in depth: even the model pipeline must not mint a pick for a
    # visibility-only sport.
    sink = RecordingSink()
    deps = make_tennis_deps(sink, FakeLoader(tennis_snapshots()))
    picks = await run_pick_pipeline(deps, "tennis")
    assert picks == []
    assert sink.sent == []
    assert all(row["unvalidated"] is True for row in AVAILABLE_GAMES["tennis"])


# ---------------------------------------------------------------------------
# 4. Scheduler wiring
# ---------------------------------------------------------------------------


async def test_scheduler_omits_tennis_when_leagues_unset() -> None:
    import fakeredis.aioredis as fakeredis

    from app.scheduler import build_scheduler

    redis = fakeredis.FakeRedis()
    async with httpx.AsyncClient() as client:
        scheduler = build_scheduler(make_settings(), client, redis)
    # The poll job exists, but tennis is not configured anywhere.
    assert any(job.id == "poll_odds" for job in scheduler.get_jobs())


async def test_scheduler_wires_tennis_when_leagues_set() -> None:
    import fakeredis.aioredis as fakeredis

    from app.scheduler import build_scheduler

    redis = fakeredis.FakeRedis()
    settings = make_settings(oddsportal_tennis_leagues="atp-us-open,wta-wimbledon")
    async with httpx.AsyncClient() as client:
        scheduler = build_scheduler(settings, client, redis)
    assert any(job.id == "poll_odds" for job in scheduler.get_jobs())
