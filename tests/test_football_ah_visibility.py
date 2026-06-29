"""Football Asian-handicap as a VISIBILITY-ONLY market.

Covers the four pillars wired in feat/ah-visibility-only:
  1. CAPTURE — the JSON feed parses football AH (betType 5 / scope 2), signed
     half-line ladder ``E-5-2-0-{line}-0``, both DICT (negative lines) and LIST
     (zero/positive lines) outcome shapes; integer lines are dropped.
  2. EDGE/DEVIG — reuse of the existing 2-way devig + value scan (no new math).
  3. SANITIZATION — the AH sentinel/implausibility guard rejects bad feed odds
     (e.g. 22.0) and an implausibly large sharp-vs-soft implied-prob gap.
  4. VISIBILITY-ONLY SCOPING — a market in ValuePolicy.visibility_only_markets
     is CAPPED at the 'volume' tier regardless of edge.

Pure parse + pure-policy contracts — synthetic payloads, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.edge.value import ValueBet, ah_candidate_plausible
from app.edge.value_policy import ValuePolicy, is_visibility_only_market
from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal import _market_for_key, _validate_markets
from app.ingestion.oddsportal_bookmakers import static_bookmaker_map
from app.ingestion.oddsportal_json import build_feed_url, parse_feed_payload
from app.pipeline import pick_tier
from app.schemas.base import Market

REGISTRY = static_bookmaker_map()
BOOKIE = "707"  # -> a real soft book in the static map
BOOK = REGISTRY[BOOKIE]


def _multi(back_blocks: dict[str, Any]) -> dict:
    """Decrypted-feed payload with several back keys (one bookie each)."""
    return {"d": {"oddsdata": {"back": {k: {"odds": {BOOKIE: v}} for k, v in back_blocks.items()}}}}


def _parse(payload: Any, *, market: str, home: str, away: str) -> Any:
    # Football AH binds to a PINNED betType 5 / scope 2 (Full Time), like the
    # static soccer markets — independent of the event bootstrap defaults, which
    # are passed as 0 here to prove the pin does not rely on them.
    return parse_feed_payload(
        payload,
        event_url="https://www.oddsportal.com/football/x/h-a-EVENTID1/",
        home=home,
        away=away,
        league="L",
        starts_at=None,
        markets=(market,),
        directory=EventDirectory(),
        now=datetime.now(tz=UTC),
        bookmakers=REGISTRY,
        default_bet_id=0,
        default_scope_id=0,
    )


# --- 1. CAPTURE -------------------------------------------------------------


def test_football_ah_wildcard_emits_signed_half_lines_dict_and_list() -> None:
    snaps = _parse(
        _multi(
            {
                # negative lines arrive as a DICT, positive/zero as a LIST —
                # both tolerated by _outcome_at.
                "E-5-2-0--1.5-0": {"0": 2.05, "1": 1.78},  # home -1.5 / away +1.5
                "E-5-2-0-0.5-0": [1.90, 1.95],  # home +0.5 / away -0.5
                "E-5-2-0--2-0": {"0": 1.50, "1": 2.50},  # integer line -> PUSH -> excluded
            }
        ),
        market="asian_handicap",
        home="Arsenal",
        away="Chelsea",
    )
    assert {s.market_detail for s in snaps} == {
        "asian_handicap_-1_5",
        "asian_handicap_0_5",
    }
    got = {(s.selection, s.decimal_odds, s.market, s.market_detail) for s in snaps}
    assert ("Arsenal -1.5", 2.05, Market.SPREADS, "asian_handicap_-1_5") in got  # idx0=home
    assert ("Chelsea +1.5", 1.78, Market.SPREADS, "asian_handicap_-1_5") in got  # idx1=away
    assert ("Arsenal +0.5", 1.90, Market.SPREADS, "asian_handicap_0_5") in got
    assert all(s.bookmaker == BOOK for s in snaps)


def test_football_ah_build_feed_url_pins_bettype5_scope2() -> None:
    # No bootstrap defaults needed — scope is pinned to 2 (Full Time).
    url = build_feed_url(1, "EVT", "asian_handicap", default_bet_id=0, default_scope_id=0)
    assert url is not None
    assert "1-1-EVT-5-2-" in url


def test_validate_and_classify_accept_bare_asian_handicap_family() -> None:
    _validate_markets(["1x2", "asian_handicap"])  # must not raise
    assert _market_for_key("asian_handicap") is Market.SPREADS
    assert _market_for_key("asian_handicap_-1_5") is Market.SPREADS


# --- 3. SANITIZATION --------------------------------------------------------


def _bet(best_odds: float, sharp_fair: float, implied: float) -> ValueBet:
    return ValueBet(
        selection="Arsenal -1.5",
        best_book=BOOK,
        best_odds=best_odds,
        best_odds_effective=best_odds,
        sharp_book="pinnacle",
        sharp_fair_prob=sharp_fair,
        implied_prob=implied,
        edge=sharp_fair - implied,
        ev=0.0,
    )


def test_ah_guard_rejects_sentinel_odds() -> None:
    # 22.0-style sentinel: implied 1/22 ~= 0.045, sharp says ~0.50 => phantom edge.
    sentinel = _bet(best_odds=22.0, sharp_fair=0.50, implied=1.0 / 22.0)
    assert not ah_candidate_plausible(sentinel, max_odds=15.0, max_sharp_soft_ratio=3.0)


def test_ah_guard_rejects_implausible_sharp_soft_gap_below_odds_ceiling() -> None:
    # Price under the odds ceiling but sharp/soft implied ratio is implausible.
    bad = _bet(best_odds=6.0, sharp_fair=0.60, implied=1.0 / 6.0)  # ratio 0.60/0.167 = 3.6
    assert not ah_candidate_plausible(bad, max_odds=15.0, max_sharp_soft_ratio=3.0)


def test_ah_guard_accepts_plausible_liquid_line() -> None:
    good = _bet(best_odds=2.10, sharp_fair=0.50, implied=1.0 / 2.10)  # edge ~0.024
    assert ah_candidate_plausible(good, max_odds=15.0, max_sharp_soft_ratio=3.0)


def test_ah_guard_default_policy_rejects_22_sentinel() -> None:
    # Sane defaults on the empty policy still sanitize AH (guard is ON).
    pol = ValuePolicy()
    sentinel = _bet(best_odds=22.0, sharp_fair=0.50, implied=1.0 / 22.0)
    assert not ah_candidate_plausible(
        sentinel, max_odds=pol.ah_max_odds, max_sharp_soft_ratio=pol.ah_max_sharp_soft_ratio
    )


# --- 4. VISIBILITY-ONLY SCOPING ---------------------------------------------


def test_visibility_only_empty_policy_is_noop() -> None:
    pol = ValuePolicy()
    assert not is_visibility_only_market(pol, str(Market.SPREADS), "asian_handicap_-1_5")
    assert not is_visibility_only_market(pol, str(Market.H2H), "1x2")


def test_visibility_only_matches_ah_family_for_every_line() -> None:
    pol = ValuePolicy(visibility_only_markets=("asian_handicap",))
    # family stem matches every line-detail key
    assert is_visibility_only_market(pol, str(Market.SPREADS), "asian_handicap_-1_5")
    assert is_visibility_only_market(pol, str(Market.SPREADS), "asian_handicap_0_5")
    # non-AH markets untouched
    assert not is_visibility_only_market(pol, str(Market.H2H), "1x2")
    assert not is_visibility_only_market(pol, str(Market.TOTALS), "over_under_2_5")
    # european handicap (also SPREADS) is NOT asian_handicap
    assert not is_visibility_only_market(pol, str(Market.SPREADS), "european_handicap_-1")


def test_visibility_only_matches_exact_line_detail_and_family() -> None:
    by_line = ValuePolicy(visibility_only_markets=("asian_handicap_-1_5",))
    assert is_visibility_only_market(by_line, str(Market.SPREADS), "asian_handicap_-1_5")
    assert not is_visibility_only_market(by_line, str(Market.SPREADS), "asian_handicap_0_5")
    by_family = ValuePolicy(visibility_only_markets=("spreads",))
    assert is_visibility_only_market(by_family, str(Market.SPREADS), "asian_handicap_-1_5")


def test_visibility_only_sport_qualified_caps_only_that_sport() -> None:
    # "soccer:asian_handicap" caps FOOTBALL AH but leaves BASKETBALL AH
    # (asian_handicap_games_*) premium-eligible — the family stem alone would
    # have caught both, so the sport prefix is what scopes the cap.
    pol = ValuePolicy(visibility_only_markets=("soccer:asian_handicap",))
    assert is_visibility_only_market(
        pol, str(Market.SPREADS), "asian_handicap_-1_5", sport="soccer"
    )
    assert not is_visibility_only_market(
        pol, str(Market.SPREADS), "asian_handicap_games_-7_5", sport="basketball"
    )
    # sport match is case-insensitive
    assert is_visibility_only_market(
        pol, str(Market.SPREADS), "asian_handicap_-1_5", sport="SOCCER"
    )
    # a sport-scoped key is INERT when no sport is supplied (can't confirm scope)
    assert not is_visibility_only_market(pol, str(Market.SPREADS), "asian_handicap_-1_5")


def test_visibility_only_sport_prefix_matches_subkeyed_sport() -> None:
    # "soccer:..." also matches a more specific sport_key like "soccer_epl".
    pol = ValuePolicy(visibility_only_markets=("soccer:asian_handicap",))
    assert is_visibility_only_market(
        pol, str(Market.SPREADS), "asian_handicap_-1_5", sport="soccer_epl"
    )
    assert not is_visibility_only_market(
        pol, str(Market.SPREADS), "asian_handicap_games_-7_5", sport="basketball_nba"
    )


def test_visibility_only_plain_key_caps_all_sports() -> None:
    # Backward-compatible: an unqualified key caps the market for EVERY sport.
    pol = ValuePolicy(visibility_only_markets=("asian_handicap",))
    assert is_visibility_only_market(
        pol, str(Market.SPREADS), "asian_handicap_-1_5", sport="soccer"
    )
    assert is_visibility_only_market(
        pol, str(Market.SPREADS), "asian_handicap_games_-7_5", sport="basketball"
    )
    # ...and still matches with NO sport supplied (the pre-existing call shape).
    assert is_visibility_only_market(pol, str(Market.SPREADS), "asian_handicap_-1_5")


def test_pick_tier_premium_then_visibility_cap_demotes_to_volume() -> None:
    # An AH candidate with a premium-level edge still caps at 'volume'.
    pol = ValuePolicy(visibility_only_markets=("asian_handicap",))
    premium_edge = 0.08  # well above any premium floor
    tier = pick_tier(premium_edge, premium_min_edge=0.03, volume_min_edge=0.015)
    assert tier == "premium"
    if is_visibility_only_market(pol, str(Market.SPREADS), "asian_handicap_-1_5"):
        tier = "volume"
    assert tier == "volume"


# --- pipeline integration: cap + sanitization through run_value_pipeline ------

from collections.abc import Sequence  # noqa: E402
from datetime import timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402

from app.edge.gates import GatePolicy  # noqa: E402
from app.ingestion.base import EventTeams  # noqa: E402
from app.models.base import NullModel  # noqa: E402
from app.notifications.base import Alert  # noqa: E402
from app.notifications.dedupe import InMemoryIdempotencyStore  # noqa: E402
from app.notifications.dispatcher import AlertDispatcher  # noqa: E402
from app.pipeline import LAST_POLL, PipelineDeps, run_value_pipeline  # noqa: E402
from app.risk.exposure import DailyExposureLedger  # noqa: E402
from app.risk.staking import StakePolicy  # noqa: E402
from app.schemas.odds import OddsSnapshotIn  # noqa: E402

_GATE = GatePolicy(
    min_edge=0.0, min_ev=0.0, min_confidence=0.0, max_odds_age_seconds=300, min_liquidity=0.0
)


def _ah_snap(book: str, sel: str, odds: float) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-ah",
        bookmaker=book,
        market=Market.SPREADS,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=20),
        ingested_at=now,
        market_detail="asian_handicap_-0_5",
    )


class _Loader:
    def __init__(self, snaps: list[OddsSnapshotIn]) -> None:
        self.snaps = snaps
        self.last_fetch_matches: dict[str, int] = {}
        self.last_fetch_event_ids: dict[str, tuple[str, ...]] = {}

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


def _ah_deps(sink: _Sink, loader: _Loader, policy: ValuePolicy) -> PipelineDeps:
    directory = EventDirectory()
    directory.register("evt-ah", EventTeams(home="Home FC", away="Away FC", league="Test League"))
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
        value_volume_min_edge=0.015,
        value_min_odds=1.30,
        value_policy=policy,
    )


def _ah_market(home_soft: float) -> list[OddsSnapshotIn]:
    # Pinnacle anchors a tight 2-way AH; SoftBook is generous on the home -0.5
    # side (premium-level edge). away +0.5 carries no edge.
    return [
        _ah_snap("Pinnacle", "Home FC -0.5", 2.00),
        _ah_snap("Pinnacle", "Away FC +0.5", 1.85),
        _ah_snap("SoftBook", "Home FC -0.5", home_soft),
        _ah_snap("SoftBook", "Away FC +0.5", 1.75),
    ]


async def test_pipeline_visibility_cap_forces_premium_ah_to_volume() -> None:
    # A genuine premium-edge AH pick is CAPPED at the volume (shadow) tier: never
    # alerted, no premium pick. CONTRAST with the no-cap control below — the only
    # difference is VALUE_VISIBILITY_ONLY_MARKETS, proving the cap (not absence of
    # value) suppressed the alert. (volume_picks is only COUNTED when persisted to
    # a DB; this no-DB harness mirrors the experimental-sport test and asserts the
    # alert suppression, the observable effect of the cap.)
    sink = _Sink()
    deps = _ah_deps(
        sink,
        _Loader(_ah_market(home_soft=2.30)),  # edge ~0.055 -> premium without the cap
        ValuePolicy(visibility_only_markets=("asian_handicap",)),
    )
    await run_value_pipeline(deps, "soccer")
    assert sink.sent == []  # capped -> never alerted
    assert LAST_POLL["soccer"]["picks"] == 0  # zero premium picks


async def test_pipeline_premium_ah_alerts_without_the_cap() -> None:
    # Control: the SAME premium-edge AH slate WITHOUT the visibility cap mints an
    # alerted premium pick — so the only difference vs the test above is the cap.
    sink = _Sink()
    deps = _ah_deps(sink, _Loader(_ah_market(home_soft=2.30)), ValuePolicy())
    await run_value_pipeline(deps, "soccer")
    assert len(sink.sent) == 1
    assert LAST_POLL["soccer"]["picks"] == 1


async def test_pipeline_ah_guard_rejects_sentinel_no_pick() -> None:
    # The 22.0 sentinel on the home side fabricates a ~45% edge; the AH guard
    # rejects it at the candidate boundary -> no pick at all (premium OR volume).
    sink = _Sink()
    deps = _ah_deps(
        sink,
        _Loader(_ah_market(home_soft=22.0)),  # sentinel
        ValuePolicy(),  # sane-default guard (ah_max_odds=15.0) is ON
    )
    await run_value_pipeline(deps, "soccer")
    assert sink.sent == []
    assert LAST_POLL["soccer"]["picks"] == 0
    assert LAST_POLL["soccer"]["volume_picks"] == 0


def _ah_snap_detail(book: str, sel: str, odds: float, detail: str) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-ah",
        bookmaker=book,
        market=Market.SPREADS,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=20),
        ingested_at=now,
        market_detail=detail,
    )


def _basketball_ah_market(home_soft: float) -> list[OddsSnapshotIn]:
    # Basketball AH games line: detail "asian_handicap_games_*" — its family stem
    # is "asian_handicap", so a PLAIN "asian_handicap" key would catch it too.
    detail = "asian_handicap_games_-7_5"
    return [
        _ah_snap_detail("Pinnacle", "Home FC -7.5", 2.00, detail),
        _ah_snap_detail("Pinnacle", "Away FC +7.5", 1.85, detail),
        _ah_snap_detail("SoftBook", "Home FC -7.5", home_soft, detail),
        _ah_snap_detail("SoftBook", "Away FC +7.5", 1.75, detail),
    ]


async def test_pipeline_soccer_scoped_cap_demotes_football_ah_only() -> None:
    # VALUE_VISIBILITY_ONLY_MARKETS=soccer:asian_handicap caps FOOTBALL AH to
    # the volume (shadow) tier — no alert, zero premium picks.
    sink = _Sink()
    deps = _ah_deps(
        sink,
        _Loader(_ah_market(home_soft=2.30)),
        ValuePolicy(visibility_only_markets=("soccer:asian_handicap",)),
    )
    await run_value_pipeline(deps, "soccer")
    assert sink.sent == []
    assert LAST_POLL["soccer"]["picks"] == 0


async def test_pipeline_soccer_scoped_cap_leaves_basketball_ah_premium() -> None:
    # The SAME sport-scoped key leaves BASKETBALL AH premium-eligible: a genuine
    # premium-edge basketball AH pick is alerted as a premium pick. Proves the
    # cap is scoped to football AH and basketball AH reverts to prior behavior.
    sink = _Sink()
    deps = _ah_deps(
        sink,
        _Loader(_basketball_ah_market(home_soft=2.30)),
        ValuePolicy(visibility_only_markets=("soccer:asian_handicap",)),
    )
    await run_value_pipeline(deps, "basketball")
    assert len(sink.sent) == 1
    assert LAST_POLL["basketball"]["picks"] == 1


async def test_pipeline_plain_key_caps_basketball_ah_too() -> None:
    # Backward-compat control: the UNqualified "asian_handicap" key caps
    # basketball AH as well (the pre-existing all-sports behavior).
    sink = _Sink()
    deps = _ah_deps(
        sink,
        _Loader(_basketball_ah_market(home_soft=2.30)),
        ValuePolicy(visibility_only_markets=("asian_handicap",)),
    )
    await run_value_pipeline(deps, "basketball")
    assert sink.sent == []
    assert LAST_POLL["basketball"]["picks"] == 0


def _h2h_snap(book: str, sel: str, odds: float) -> OddsSnapshotIn:
    now = datetime.now(tz=UTC)
    return OddsSnapshotIn(
        event_id="evt-ah",
        bookmaker=book,
        market=Market.H2H,
        selection=sel,
        decimal_odds=odds,
        captured_at=now - timedelta(seconds=20),
        ingested_at=now,
        market_detail="dnb",  # a 2-way H2H-family line; NOT asian_handicap
    )


async def test_pipeline_non_ah_market_unaffected_by_ah_guard() -> None:
    # Control: a non-AH 2-way market with the SAME generous soft price is NOT
    # subject to the AH guard — it mints its premium pick. Proves the guard is
    # scoped to asian_handicap only.
    sink = _Sink()
    h2h = [
        _h2h_snap("Pinnacle", "Home FC", 2.00),
        _h2h_snap("Pinnacle", "Away FC", 1.85),
        _h2h_snap("SoftBook", "Home FC", 2.30),
        _h2h_snap("SoftBook", "Away FC", 1.75),
    ]
    deps = _ah_deps(sink, _Loader(h2h), ValuePolicy())
    await run_value_pipeline(deps, "soccer")
    assert LAST_POLL["soccer"]["picks"] == 1  # premium pick minted, guard not applied
