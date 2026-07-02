"""WP5 — sharp-anchor quality (audit fixes, no IO / no network).

FIX 1  Exchange liquidity floor at anchor-USE time: a Betfair row with KNOWN
       matched liquidity below the floor must never serve as the named sharp
       anchor (falls through to consensus), while UNKNOWN (None) liquidity
       stays anchor-eligible — the dominant main-scrape Betfair rows carry
       liquidity=None and anchor 59/62 Betfair events (project memory:
       do-not-remove-main-scrape-betfair). Wired end-to-end from Settings
       (VALUE_EXCHANGE_MIN_LIQUIDITY) through ValuePolicy into
       event_fair_probs.
FIX 2  Pinnacle Arcadia positive-AH key mismatch: the OddsPortal JSON feed key
       is UNSIGNED for positive lines ("asian_handicap_1_5"); arcadia's old
       "+"-signed token ("asian_handicap_+1_5") could never share a devig
       group with the soft pick.
FIX 3  Tennis first-initial fuzzy hole: 'cerundolo f' vs 'cerundolo j' passes
       the JW/token-sort tier (siblings!) — a first-initial mismatch on two
       tennis-canonical-shaped names is a hard contradiction.
"""

import re
from datetime import UTC, datetime

import pytest

from app.edge.value import CONSENSUS_ANCHOR, anchor_fair_probs
from app.edge.value_policy import ValuePolicy
from app.resolution.matching import (
    AliasTable,
    EventCandidate,
    match_event_hardened,
)
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

_EX_PRICES = {
    "H": {"betfair exchange": 1.50, "SoftA": 1.55, "SoftB": 1.52, "SoftC": 1.53},
    "D": {"betfair exchange": 4.00, "SoftA": 4.10, "SoftB": 4.05, "SoftC": 4.08},
    "A": {"betfair exchange": 7.00, "SoftA": 6.80, "SoftB": 6.90, "SoftC": 6.85},
}


# --- FIX 1: known-thin exchange liquidity rejects the anchor -----------------
def test_known_thin_betfair_liquidity_is_not_the_sharp_anchor() -> None:
    # liquidity=5 (KNOWN, below the 50 floor) -> Betfair must NOT anchor;
    # the market falls through to the consensus median (fail-closed anchoring).
    thin = {s: {"betfair exchange": 5.0} for s in _EX_PRICES}
    res = anchor_fair_probs(_EX_PRICES, liquidity=thin, exchange_min_liquidity=50.0)
    assert res is not None
    assert res[0] == CONSENSUS_ANCHOR


def test_unknown_none_liquidity_keeps_betfair_anchor_eligible() -> None:
    # liquidity=None (the main-scrape consensus rows) stays anchor-ELIGIBLE:
    # rejecting NULL would gut Betfair coverage (59/62 anchored events carry
    # NULL liquidity — only the dedicated gated capture sets it).
    res = anchor_fair_probs(_EX_PRICES, liquidity=None, exchange_min_liquidity=50.0)
    assert res is not None
    assert res[0] == "betfair exchange"
    # A liquidity map that simply has no Betfair entry is the same unknown case.
    other_book = {s: {"SoftA": 900.0} for s in _EX_PRICES}
    res2 = anchor_fair_probs(_EX_PRICES, liquidity=other_book, exchange_min_liquidity=50.0)
    assert res2 is not None
    assert res2[0] == "betfair exchange"


def test_liquid_betfair_passes_the_floor() -> None:
    liquid = {s: {"betfair exchange": 500.0} for s in _EX_PRICES}
    res = anchor_fair_probs(_EX_PRICES, liquidity=liquid, exchange_min_liquidity=50.0)
    assert res is not None
    assert res[0] == "betfair exchange"


def test_one_known_thin_selection_rejects_the_whole_anchor() -> None:
    # Mixed: two selections liquid, one KNOWN-thin -> the market's anchor is
    # rejected (a one-sided thin book is not a trustworthy full-market anchor).
    mixed = {
        "H": {"betfair exchange": 500.0},
        "D": {"betfair exchange": 5.0},
        "A": {"betfair exchange": 500.0},
    }
    res = anchor_fair_probs(_EX_PRICES, liquidity=mixed, exchange_min_liquidity=50.0)
    assert res is not None
    assert res[0] == CONSENSUS_ANCHOR


def test_settings_field_and_value_policy_thread_the_floor() -> None:
    from app.config import Settings, value_policy

    s = Settings(_env_file=None)
    assert s.value_exchange_min_liquidity == 50.0
    policy = value_policy(s)
    assert policy.exchange_min_liquidity == 50.0
    # The pure-policy default stays inert (empty policy == no-op, as everywhere).
    assert ValuePolicy().exchange_min_liquidity == 0.0


def _snap(
    selection: str,
    bookmaker: str,
    odds: float,
    liquidity: float | None = None,
) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id="ev1",
        bookmaker=bookmaker,
        market=Market.H2H,
        selection=selection,
        decimal_odds=odds,
        liquidity=liquidity,
        captured_at=NOW,
        ingested_at=NOW,
        market_detail="1x2",
    )


def _h2h_snaps(betfair_liquidity: float | None) -> list[OddsSnapshotIn]:
    snaps: list[OddsSnapshotIn] = []
    for sel, books in _EX_PRICES.items():
        for book, odds in books.items():
            lq = betfair_liquidity if book == "betfair exchange" else None
            snaps.append(_snap(sel, book, odds, lq))
    return snaps


def test_event_fair_probs_wires_liquidity_floor_from_policy() -> None:
    from app.pipeline import event_fair_probs, group_market_liquidity, group_market_prices
    from app.probabilities.devig import DevigMethod

    policy = ValuePolicy(exchange_min_liquidity=50.0)
    key = ("ev1", Market.H2H, "1x2")

    thin_snaps = _h2h_snaps(betfair_liquidity=5.0)
    fair_thin = event_fair_probs(
        group_market_prices(thin_snaps),
        DevigMethod.POWER,
        policy,
        liquidity_by_market=group_market_liquidity(thin_snaps),
    )
    assert fair_thin[key][0] == CONSENSUS_ANCHOR  # known-thin rejected

    null_snaps = _h2h_snaps(betfair_liquidity=None)
    fair_null = event_fair_probs(
        group_market_prices(null_snaps),
        DevigMethod.POWER,
        policy,
        liquidity_by_market=group_market_liquidity(null_snaps),
    )
    assert fair_null[key][0] == "betfair exchange"  # unknown unchanged

    liquid_snaps = _h2h_snaps(betfair_liquidity=500.0)
    fair_liquid = event_fair_probs(
        group_market_prices(liquid_snaps),
        DevigMethod.POWER,
        policy,
        liquidity_by_market=group_market_liquidity(liquid_snaps),
    )
    assert fair_liquid[key][0] == "betfair exchange"  # liquid passes


# --- FIX 2: arcadia positive-AH key matches the JSON feed vocabulary ---------
def _feed_ah_detail(line: float) -> str:
    """The EXACT market_detail the OddsPortal JSON feed builds for a football
    AH line: the signed line rides the feed key's 5th segment (`(-?\\d+...)` —
    positive lines carry NO '+') and '.' maps to '_'. Mirrors
    app/ingestion/oddsportal_json.py's wildcard-family detail construction."""
    feed_key = f"E-5-2-0-{line:g}-0"
    m = re.match(r"^E-5-2-\d+-(-?\d+(?:\.\d+)?)-\d+$", feed_key)
    assert m is not None
    return f"asian_handicap_{m.group(1).replace('.', '_')}"


def _arcadia_spread_detail(home_line: float) -> str:
    from app.ingestion.pinnacle_arcadia import extract_spread_quotes, parse_matchups

    matchup = {
        "id": 555,
        "type": "matchup",
        "participants": [
            {"alignment": "home", "name": "Alpha FC"},
            {"alignment": "away", "name": "Beta FC"},
        ],
        "startTime": "2026-07-01T18:00:00Z",
        "league": {"name": "Test League", "sport": {"name": "Soccer"}},
    }
    market = {
        "key": f"s;0;s;{home_line:g}",
        "matchupId": 555,
        "type": "spread",
        "period": 0,
        "status": "open",
        "isAlternate": False,
        "version": 1,
        "prices": [
            {"designation": "home", "price": -110, "points": home_line},
            {"designation": "away", "price": -110, "points": -home_line},
        ],
    }
    matchups = parse_matchups([matchup], now=NOW, horizon_end=datetime(2026, 7, 2, tzinfo=UTC))
    quotes = extract_spread_quotes(matchups, [market], now=NOW, sport="soccer")
    assert quotes, "spread quote must be extracted"
    details = {s.market_detail for s in quotes[0].snapshots}
    assert len(details) == 1
    detail = details.pop()
    assert detail is not None
    return detail


def test_positive_home_line_groups_with_the_feed_key() -> None:
    # +1.5 home line: arcadia and the feed must build the IDENTICAL detail —
    # the old '+'-signed key ("asian_handicap_+1_5") never grouped/anchored.
    assert _arcadia_spread_detail(1.5) == _feed_ah_detail(1.5) == "asian_handicap_1_5"


def test_negative_home_line_detail_unchanged() -> None:
    assert _arcadia_spread_detail(-1.5) == _feed_ah_detail(-1.5) == "asian_handicap_-1_5"


def test_zero_home_line_is_unsigned() -> None:
    # Level AH: unsigned, matching the feed convention (the feed key segment is
    # `(-?\d+...)` so a '+' can never appear; the feed drops non-half lines
    # anyway, so this is a vocabulary-consistency guard, not a grouping path).
    assert _arcadia_spread_detail(0.0) == "asian_handicap_0_0"


# --- FIX 3: tennis first-initial mismatch is a hard veto ---------------------
KO = datetime(2026, 7, 1, 18, 0, tzinfo=UTC)


def _cand(ref: str, home: str, away: str) -> EventCandidate:
    return EventCandidate(ref=ref, home=home, away=away, kickoff=KO)


def test_tennis_first_initial_mismatch_is_rejected() -> None:
    # 'cerundolo f' vs 'cerundolo j' (siblings) scores JW 0.964 / token_sort
    # 90.9 — above BOTH accept tiers — but the first-initial mismatch on two
    # tennis-canonical names is a categorical contradiction: REJECT.
    cands = [_cand("1", "cerundolo j", "djokovic n")]
    assert (
        match_event_hardened(
            "cerundolo f",
            "djokovic n",
            KO,
            cands,
            aliases=AliasTable(),
            ordered=False,
        )
        is None
    )


def test_tennis_same_initial_still_matches() -> None:
    cands = [_cand("1", "cerundolo f", "djokovic n")]
    match = match_event_hardened(
        "cerundolo f",
        "djokovic n",
        KO,
        cands,
        aliases=AliasTable(),
        ordered=False,
    )
    assert match is not None
    assert match.ref == "1"


def test_team_names_without_initial_tokens_are_unaffected() -> None:
    # No single-letter token on either side -> the veto never fires; the
    # existing fuzzy tier decides ("manchester utd" ~ "manchester united").
    cands = [_cand("1", "Manchester Utd", "Chelsea")]
    match = match_event_hardened(
        "Manchester United",
        "Chelsea",
        KO,
        cands,
        aliases=AliasTable(),
    )
    assert match is not None
    assert match.ref == "1"


def test_one_sided_initial_shape_does_not_trip_the_veto() -> None:
    # Only ONE side looks tennis-canonical -> no veto; equality/fuzzy decides.
    from app.resolution.matching import _base_name_ok

    assert _base_name_ok("cerundolo f", "cerundolo f")
    assert not _base_name_ok("cerundolo f", "cerundolo j")
    # 'internazionale' has no trailing initial: the veto must not fire
    # (the fuzzy tier rejects on its own merits).
    assert not _base_name_ok("cerundolo f", "internazionale")


@pytest.mark.parametrize(
    ("a", "b"),
    [("cerundolo f", "cerundolo j"), ("tsitsipas s", "tsitsipas p")],
)
def test_sibling_pairs_score_in_the_accept_band_but_are_vetoed(a: str, b: str) -> None:
    # Prove the hole exists (scores clear both tiers) AND that the veto closes it.
    from app.resolution.matching import _base_name_ok, jaro_winkler, token_sort_ratio

    assert jaro_winkler(a, b) >= 0.92
    assert token_sort_ratio(a, b) >= 90.0
    assert not _base_name_ok(a, b)
