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

from app.probabilities.devig import DevigMethod


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
    # (market_key, ceiling): per-market DATA-ERROR ceiling override — the per-market
    # sibling of max_edge. Markets without an entry use the GLOBAL max_edge passed
    # alongside this policy (Settings.value_max_edge). Empty () = DISABLED — every
    # market uses the global ceiling (current behavior, the BIT-IDENTICAL default).
    # Keys are matched line-detail-first then family, exactly like min_edge_by_market
    # (most specific wins). Settings validates each ceiling > value_min_edge at the
    # composition root; a bad entry fails fast there, never reaching this frozen
    # policy. WHY per-market: 0.20 is soccer-appropriate, but a wider true-edge market
    # (e.g. a sparse AH line) may legitimately clear 0.20 while a tight 2-way market's
    # real ceiling is lower — but WHICH ceiling per market is folklore until learned,
    # so the default leaves the validated global ceiling in force everywhere.
    max_edge_by_market: tuple[tuple[str, float], ...] = ()
    # (market_key, DevigMethod): per-market-type devig override (ADR-0006 —
    # method selection is a config policy, never hardcoded in pipeline code).
    # Markets without an entry use the GLOBAL devig method passed alongside
    # this policy (Settings.value_devig). Empty () = DISABLED — every market
    # devigs with the global method (current behavior, the non-breaking
    # default). Keys are matched line-detail-first then family, exactly like
    # min_edge_by_market (most specific wins). Names are validated against the
    # 8 DevigMethod values at the composition root (Settings); a bad name fails
    # fast there, never reaching this frozen policy. Different market STRUCTURES
    # devig best with different methods (e.g. probit on symmetric totals/AH;
    # shin/multiplicative on 2-way) — but WHICH method per market is folklore
    # until learned on nested walk-forward CV (<= 2324), so the default leaves
    # the validated global method in force everywhere.
    devig_by_market: tuple[tuple[str, DevigMethod], ...] = ()
    # Markets CAPPED at the VOLUME (shadow) tier: a market in this set can NEVER
    # be premium — it is persisted + CLV-tracked but NEVER alerted and NEVER
    # reserving exposure, regardless of edge (even above the premium floor). This
    # is the per-MARKET sibling of PipelineDeps.experimental_sports (per-SPORT):
    # it lets a brand-new market (e.g. football Asian handicap) accrue FORWARD
    # shadow CLV before it is trusted to alert. Empty () = DISABLED — no market is
    # capped (current behavior, the BIT-IDENTICAL default). Matched line-detail
    # first, then the AH/totals family stem, then the market family (str(Market))
    # — so "asian_handicap" caps every "asian_handicap_<line>" line at once (see
    # is_visibility_only_market).
    visibility_only_markets: tuple[str, ...] = ()
    # AH SENTINEL/IMPLAUSIBILITY guard bounds (app/edge/value.ah_candidate_plausible).
    # A 2-way Asian-handicap candidate is REJECTED at the candidate-building
    # boundary when its RAW best price exceeds ``ah_max_odds`` OR its sharp-fair /
    # soft-implied probability ratio exceeds ``ah_max_sharp_soft_ratio`` — a
    # corrupt/sentinel feed price (a backtest found odds like 22.0) fabricates a
    # phantom edge, and a liquid 2-way AH line never sits at such a price or gap.
    # These are SANE DEFAULTS (the guard is ON for AH); they are scoped to AH
    # markets in the pipeline, so non-AH markets are untouched. Set from
    # Settings (VALUE_AH_MAX_ODDS / VALUE_AH_MAX_SHARP_SOFT_RATIO).
    ah_max_odds: float = 15.0
    ah_max_sharp_soft_ratio: float = 3.0
    # When True, the CONSENSUS FALLBACK anchor (used only when NO genuine sharp
    # book priced the full market) is a log-odds (logit) POOL across full-market
    # books instead of the median-of-prices consensus — non-extremizing and
    # tail-preserving on heavy favourites/longshots (Gneiting & Ranjan 2010).
    # False = median consensus (current behavior, the non-breaking default).
    # SCOPE: this only changes the CONSENSUS fallback fair value; when
    # require_sharp_anchor=True, consensus picks are already demoted to the
    # volume (shadow) tier, so this improves the shadow tier's fair value and
    # the consensus-vs-median comparisons, NOT premium (sharp-anchored) pricing.
    # See app/edge/value._logit_consensus_anchor (the pure implementation).
    consensus_logit_pool: bool = False


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


def max_edge_for(
    policy: ValuePolicy, market: str, market_detail: str | None, default: float
) -> float:
    """Per-market DATA-ERROR ceiling for a market: an override or the global default.

    Mirrors ``min_edge_for`` / ``devig_method_for`` — line-detail key first, then
    market family; most specific wins. An empty ``policy.max_edge_by_market`` (the
    default) always returns ``default`` (the global Settings.value_max_edge), so every
    market keeps the validated global ceiling unless an override is explicitly
    configured — bit-identical to the pre-knob behavior.
    """
    by_key = dict(policy.max_edge_by_market)
    for key in market_lookup_keys(market, market_detail):
        value = by_key.get(key)
        if value is not None:
            return value
    return default


def devig_method_for(
    policy: ValuePolicy, market: str, market_detail: str | None, default: DevigMethod
) -> DevigMethod:
    """Per-market devig method: a configured override or the global default.

    Mirrors ``min_edge_for`` — line-detail key first, then market family; most
    specific wins. An empty ``policy.devig_by_market`` (the default) always
    returns ``default`` (the global Settings.value_devig), so every market keeps
    the validated global method unless an override is explicitly configured.
    """
    by_key = dict(policy.devig_by_market)
    for key in market_lookup_keys(market, market_detail):
        method = by_key.get(key)
        if method is not None:
            return method
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


def _split_sport_market(entry: str) -> tuple[str | None, str]:
    """Split one visibility key into ``(sport_or_none, market)``.

    ``"soccer:asian_handicap"`` -> ``("soccer", "asian_handicap")`` (caps the
    market for that sport only); ``"asian_handicap"`` -> ``(None,
    "asian_handicap")`` (caps the market for ANY sport — the backward-compatible
    shape). Only the FIRST colon separates sport from market; both sides are
    lowercased/stripped. A blank market yields ``(sport, "")`` and is ignored by
    the caller.
    """
    key = entry.strip().lower()
    if ":" in key:
        sport, _, market = key.partition(":")
        sport = sport.strip()
        return (sport or None), market.strip()
    return None, key


def _sport_in_scope(entry_sport: str | None, candidate_sport: str) -> bool:
    """Whether a key's sport prefix applies to the candidate's sport.

    ``entry_sport is None`` (an unqualified key) applies to EVERY sport. A
    qualified prefix applies when the candidate sport equals it or is a more
    specific key under it (``"soccer"`` scopes ``"soccer"`` and ``"soccer_epl"``).
    A qualified key is INERT when no candidate sport is known.
    """
    if entry_sport is None:
        return True
    if not candidate_sport:
        return False
    return candidate_sport == entry_sport or candidate_sport.startswith(f"{entry_sport}_")


def is_visibility_only_market(
    policy: ValuePolicy,
    market: str,
    market_detail: str | None,
    sport: str | None = None,
) -> bool:
    """Whether this market is CAPPED at the volume (shadow) tier — never premium.

    Empty ``policy.visibility_only_markets`` DISABLES the cap — no market is
    capped (current behavior, the bit-identical default). Each configured entry
    is SPORT-QUALIFIED ("soccer:asian_handicap" — caps that market only for that
    sport) or a plain market key ("asian_handicap" — caps the market for ANY
    sport, the backward-compatible shape). The candidate's ``sport`` (the
    pipeline's ``sport_key``, e.g. "soccer"/"basketball") is matched
    case-insensitively, and a sport prefix also scopes more specific keys under
    it ("soccer" -> "soccer", "soccer_epl"). A plain key still matches with no
    ``sport`` supplied (the pre-existing call shape).

    Given an in-scope sport, an entry's MARKET matches if it equals
    (case-insensitively):
      * the line-qualified market_detail ("asian_handicap_-1_5"), OR
      * the market family ``str(Market)`` ("spreads"), OR
      * a FAMILY STEM of the detail — entry market E matches when the detail
        equals E or starts with ``E + "_"`` ("asian_handicap" caps every
        "asian_handicap_<line>" at once).
    Most-specific-first is preserved (detail/family exact match before the stem
    prefix), mirroring the other per-market policies while letting one family key
    cap a whole line ladder.
    """
    if not policy.visibility_only_markets:
        return False
    candidate_sport = (sport or "").strip().lower()
    keys = market_lookup_keys(market, market_detail)
    detail = (market_detail or "").strip().lower()
    for entry in policy.visibility_only_markets:
        entry_sport, entry_market = _split_sport_market(entry)
        if not entry_market:
            continue
        if not _sport_in_scope(entry_sport, candidate_sport):
            continue
        if entry_market in keys:
            return True
        if detail and (detail == entry_market or detail.startswith(f"{entry_market}_")):
            return True
    return False


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
