"""Optional value-strategy gate refinements — every knob defaults to NO-OP.

Pure module (stdlib only — pure-math boundary, CLAUDE.md). Policy values are
constructed from Settings at the composition root ONLY (app/config.py
``value_policy``); this module never reads the environment.

These knobs implement the 2026-06 premium-tier adjustment candidates from the
strategy research (favourite-longshot bands, per-market thresholds, market
expansion — see docs/backtesting/value-findings.md and the spent-holdout
discipline in .claude/memory/decisions.md). NONE of them is evidence-backed
yet at per-market granularity: enabling any knob requires nested
season-blocked walk-forward evidence WITHIN train seasons <= 2324, or a
never-consulted fresh domain — seasons 2425+2526 are SPENT as a holdout.
Defaults (empty policy) reproduce the validated global-threshold behavior
exactly.

Market keys are matched against the line-qualified source market first
(``market_detail``, e.g. "1x2", "over_under_2_5", "asian_handicap_-1_5" —
the same keys the dashboard's per-market counters use), then the market
family (``str(Market)``, e.g. "h2h", "totals"). Most specific wins.
"""

import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ValuePolicy:
    """Optional refinements applied on top of the global value-gate floors.

    Immutable by construction (tuples, not dicts) so the policy can ride a
    frozen dataclass across the pure-math boundary. An all-empty policy —
    the default everywhere — changes nothing.
    """

    # (market_key, premium_min_edge): per-market PREMIUM-tier floor override;
    # markets without an entry use the global value_min_edge. Settings
    # validates each override >= value_volume_min_edge (tier ordering).
    min_edge_by_market: tuple[tuple[str, float], ...] = ()
    # ((lo, hi), ...): candidate's RAW best odds must fall inside at least
    # one band (inclusive). Empty = only the global value_min_odds floor.
    # FLB research says margin is loaded onto longshots; WHICH bands are
    # structurally soft is folklore until learned on nested CV (<= 2324).
    odds_bands: tuple[tuple[float, float], ...] = ()
    # (market_key, min_books): minimum distinct bookmakers quoting the
    # market before its candidates are considered. 0/absent = no floor.
    min_books_by_market: tuple[tuple[str, int], ...] = ()
    # Scraped league names eligible for the PREMIUM (alerting + exposure) tier.
    # Empty = gate DISABLED — every league is premium-eligible (current
    # behavior). When set, a premium candidate whose scraped per-event league
    # name does not normalize into this set is DEMOTED to the volume (shadow)
    # tier: still persisted + CLV-tracked, never alerted, never reserving
    # exposure. This is the honest-high-ROI lever — concentrate alerts on
    # leagues with real sharp-anchor coverage + liquidity (majors), not obscure
    # slates where no free sharp close exists (.claude/memory/pitfalls.md,
    # 2026-06-20: ~37% sharp coverage is structural; scope, don't fuzzy-match).
    # Stored as given; normalized at compare time (see is_major_league).
    major_leagues: tuple[str, ...] = ()
    # When True, a PREMIUM candidate whose fair-value anchor is the soft
    # CONSENSUS median (no genuine sharp book — Pinnacle or Betfair — priced
    # the full market) is DEMOTED to the volume (shadow) tier: still persisted +
    # CLV-tracked, never alerted, never reserving exposure. False = gate
    # DISABLED (every anchor premium-eligible — current behavior, the
    # non-breaking default). This is the season-proof, name-proof sibling of
    # major_leagues: it stops obscure-league bleed (e.g. "GFA League") by DATA
    # (no sharp anchor backed the price) rather than by league name, so it needs
    # no per-season league curation. The anchor test is pure: see
    # app/edge/value.is_sharp_anchored (consensus/blank => not sharp).
    require_sharp_anchor: bool = False
    # Upper sanity bound on edge: a value above this is a DATA ERROR (a corrupted
    # or mislabeled anchor — e.g. a swapped 1X2 feed), never real value on a
    # liquid market, so the value scan rejects it (a feed defect can't mint a
    # phantom +EV pick). Default math.inf = OFF; set from Settings.value_max_edge.
    max_edge: float = math.inf


def market_lookup_keys(market: str, market_detail: str | None) -> tuple[str, ...]:
    """Lookup keys for one market, most specific first (detail, then family)."""
    keys: list[str] = []
    if market_detail and market_detail.strip():
        keys.append(market_detail.strip().lower())
    family = market.strip().lower()
    if family not in keys:
        keys.append(family)
    return tuple(keys)


def min_edge_for(
    policy: ValuePolicy, market: str, market_detail: str | None, default: float
) -> float:
    """Premium-tier min-edge floor for a market: override or the global default."""
    by_key = dict(policy.min_edge_by_market)
    for key in market_lookup_keys(market, market_detail):
        value = by_key.get(key)
        if value is not None:
            return value
    return default


def min_books_for(policy: ValuePolicy, market: str, market_detail: str | None) -> int:
    """Minimum distinct-bookmaker count for a market; 0 means no floor."""
    by_key = dict(policy.min_books_by_market)
    for key in market_lookup_keys(market, market_detail):
        value = by_key.get(key)
        if value is not None:
            return value
    return 0


def normalize_league(name: str) -> str:
    """League name canonicalized for exact comparison.

    Accents folded to ASCII (NFKD), punctuation collapsed to spaces, whitespace
    collapsed, casefolded — so "Série A", " ENGLAND - Premier League " and
    "premier league" compare predictably. Deliberately NOT fuzzy: membership is
    exact on this normal form (mirroring the strict cross-source matcher) so the
    gate can never falsely promote a minor league.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    tokens = "".join(ch if ch.isalnum() else " " for ch in ascii_name).split()
    return " ".join(tokens).casefold()


def is_major_league(policy: ValuePolicy, league_name: str) -> bool:
    """Whether `league_name` qualifies for the PREMIUM (alerting + exposure) tier.

    Empty ``policy.major_leagues`` DISABLES the gate — every league is
    premium-eligible (current behavior, the non-breaking default). When the set
    is configured, only a league whose scraped name normalizes to a member is
    major; a blank/unknown league name is NOT major (it cannot be confirmed to
    sit in a sharp-covered major league, so it is demoted to the shadow tier —
    never alerted).
    """
    if not policy.major_leagues:
        return True
    norm = normalize_league(league_name)
    if not norm:
        return False
    return norm in {normalize_league(major) for major in policy.major_leagues}


def odds_in_bands(odds: float, bands: tuple[tuple[float, float], ...]) -> bool:
    """True when no bands are configured or `odds` falls inside one (inclusive)."""
    if not bands:
        return True
    return any(lo <= odds <= hi for lo, hi in bands)


def distinct_book_count(
    prices: Mapping[str, Mapping[str, float]],
    exclude: frozenset[str] = frozenset(),
) -> int:
    """Distinct normalized bookmakers quoting ANY selection of one market.

    Union (not full-market intersection) deliberately: the knob is a
    liquidity proxy for "is this market priced widely enough to trust",
    while full-market completeness is already enforced by the anchor rules
    in app/edge/value.py.

    `exclude` (normalized book names) is dropped from the count so the thin-
    coverage gate measures SOFT liquidity only — injected sharp-anchor lines
    (Pinnacle/Betfair) must not inflate it into passing the floor (review
    2026-06-21).
    """
    return len(
        {
            norm
            for selection in prices.values()
            for book in selection
            if (norm := book.strip().lower()) not in exclude
        }
    )
