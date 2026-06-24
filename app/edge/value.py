"""Sharp-vs-soft value finder — the strategy with backtested positive CLV.

Instead of predicting outcomes with a goals model (which does NOT beat the
market — see docs/backtesting/findings.md), this prices "fair" from a SHARP
anchor (Pinnacle by preference; else a cross-book median consensus) and flags
selections where the BEST effective price elsewhere exceeds that fair value.

Review-hardened (2026-06-10 deep review):
- Exchange commission is netted out of prices BEFORE any comparison/edge/EV —
  gross exchange odds otherwise fake edges of the same size as min_edge.
- The no-Pinnacle fallback uses a MEDIAN consensus across >=3 full-market
  books (outlier-resistant), never a single lowest-overround book — a stale
  quote at one book must not contaminate fair value for every selection.
- Anchor books with implausible overround (underround or > max_overround)
  are rejected; the market is skipped rather than anchored on garbage.
- Ultra-short prices are gated by min_odds — a 1.0x "edge" is devig noise.

Pure module: no IO. Input is per-bookmaker decimal odds for one market.
"""

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.probabilities.devig import DevigMethod, devig

# Books treated as "sharp" for fair-value estimation, in priority order.
SHARP_BOOKS = ("pinnacle", "pinnacle sports", "betfair exchange", "smarkets")

# Net commission on winnings, by normalized book name. Configurable at the
# call site; these are conservative defaults (Betfair varies 2-8% by region).
EXCHANGE_COMMISSION = {
    "betfair exchange": 0.05,
    "betfair": 0.05,
    "smarkets": 0.02,
    "matchbook": 0.02,
}

CONSENSUS_ANCHOR = "consensus(median)"
MIN_CONSENSUS_BOOKS = 3

# Anchor-type tags persisted on picks (picks.anchor_type) so live CLV can be
# stratified by the anchor that produced each pick — the live verdict
# mechanism for the consensus fallback (train-only validation 2026-06-12:
# consensus selection beats its own null but underperforms the Pinnacle
# anchor on shared matches; see .claude/memory/decisions.md).
ANCHOR_TYPE_PINNACLE = "pinnacle"
ANCHOR_TYPE_SHARP = "sharp"  # named non-Pinnacle sharp (exchange) anchor
ANCHOR_TYPE_CONSENSUS = "consensus"


def anchor_type_for(anchor_book: str) -> str:
    """Anchor category for one pick: pinnacle | sharp | consensus.

    `anchor_book` is ValueBet.sharp_book — either a named sharp book from
    SHARP_BOOKS or the CONSENSUS_ANCHOR sentinel. Pure function (tested
    directly); the pipeline persists its result on every value pick.
    """
    if anchor_book == CONSENSUS_ANCHOR:
        return ANCHOR_TYPE_CONSENSUS
    if "pinnacle" in _norm(anchor_book):
        return ANCHOR_TYPE_PINNACLE
    return ANCHOR_TYPE_SHARP


def is_sharp_anchored(anchor_book: str) -> bool:
    """Whether fair value was backed by a GENUINE sharp book, not soft consensus.

    `anchor_book` is ValueBet.sharp_book — a named sharp book from SHARP_BOOKS
    (Pinnacle/Betfair/Smarkets) or the CONSENSUS_ANCHOR sentinel. The soft
    consensus(median) fallback (and any blank/unknown anchor) is NOT sharp; only
    a named sharp anchor is. Equivalent to ``anchor_type_for(...) != consensus``
    but stated as the gate predicate. Pure function (tested directly); the
    require-sharp-anchor premium gate (app/pipeline.py) consumes it.
    """
    return bool(anchor_book) and anchor_book != CONSENSUS_ANCHOR


def close_is_independent_of_fill(close_anchor_book: str, fill_book: str) -> bool:
    """Whether the CLOSE was priced INDEPENDENTLY of the fill book (P0-1/P0-3).

    A "sharp" close whose anchor book IS the pick's own fill book is CIRCULAR —
    the pick's own book pricing its own close (closing == fill, |clv_log|~0). That
    fake CLV is what masked the -EV, so it must never count as genuine CLV.

    `close_anchor_book` is the book that anchored the close (a named sharp book
    from SHARP_BOOKS, or the CONSENSUS_ANCHOR sentinel); `fill_book` is the pick's
    own bookmaker. Independent iff the close anchor is NOT (a normalized match to)
    the fill book. The CONSENSUS_ANCHOR is a >= MIN_CONSENSUS_BOOKS median, not a
    single book, so it is independent of any single fill by construction (the
    sentinel never normalizes to a real book name -> True). A blank/unknown close
    anchor is treated as independent (it is not a self-priced close). Pure
    function (tested directly); the CLV true-up persists its result on each pick.
    """
    if not close_anchor_book or close_anchor_book == CONSENSUS_ANCHOR:
        return True
    return _norm(close_anchor_book) != _norm(fill_book)


@dataclass(frozen=True)
class ValueBet:
    selection: str
    best_book: str
    best_odds: float  # raw price as displayed at the book
    best_odds_effective: float  # net of exchange commission
    sharp_book: str
    sharp_fair_prob: float
    implied_prob: float  # from the EFFECTIVE price
    edge: float  # sharp_fair_prob - implied_prob
    ev: float  # per unit stake at the effective price


def _norm(s: str) -> str:
    return s.strip().lower()


def effective_odds(
    book: str, odds: float, commissions: Mapping[str, float] = EXCHANGE_COMMISSION
) -> float:
    """Decimal odds net of exchange commission on winnings."""
    c = commissions.get(_norm(book), 0.0)
    return 1.0 + (odds - 1.0) * (1.0 - c)


def _overround(odds: Sequence[float]) -> float:
    return sum(1.0 / o for o in odds) - 1.0


def min_acceptable_odds(
    fair_prob: float,
    min_edge: float,
    *,
    book: str = "",
    commissions: Mapping[str, float] = EXCHANGE_COMMISSION,
) -> float | None:
    """Minimum RAW (displayed) decimal odds at which a pick still retains
    `min_edge` of edge against `fair_prob` — the execution helper behind
    "still +EV down to X.XX".

    Derived from THIS module's edge definition (`_scan_against_fair`):
        edge = fair_prob - 1/effective_odds,  retained while edge >= min_edge
        =>  effective_odds >= 1 / (fair_prob - min_edge)
    then converted back to the displayed price through the book's exchange
    commission (inverse of `effective_odds`): raw = 1 + (eff - 1)/(1 - c).

    Returns None when fair_prob - min_edge <= 0: no price retains the edge.
    Pure function; informational only — nothing here places bets.
    """
    if not 0.0 < fair_prob < 1.0:
        raise ValueError(f"fair_prob must be in (0, 1), got {fair_prob}")
    if min_edge < 0.0:
        raise ValueError(f"min_edge must be >= 0, got {min_edge}")
    headroom = fair_prob - min_edge
    if headroom <= 0.0:
        return None
    eff_min = 1.0 / headroom
    c = commissions.get(_norm(book), 0.0)
    return 1.0 + (eff_min - 1.0) / (1.0 - c)


def ceil_odds(value: float, decimals: int = 2) -> float:
    """Round odds UP at `decimals` places — display helper for
    `min_acceptable_odds`: the printed minimum must still RETAIN the edge,
    so the exact threshold may never be rounded down. The inner round()
    absorbs float-representation noise (1.85 -> 185.00000000000003) so an
    exactly-representable threshold is not bumped a tick up."""
    scale = 10.0**decimals
    return math.ceil(round(value * scale, 6)) / scale


def anchor_fair_probs(
    prices: Mapping[str, Mapping[str, float]],
    *,
    max_overround: float = 0.12,
    devig_method: DevigMethod = DevigMethod.POWER,
    sharp_books: Sequence[str] = SHARP_BOOKS,
    commissions: Mapping[str, float] = EXCHANGE_COMMISSION,
) -> tuple[str, dict[str, float]] | None:
    """Trustworthy fair probabilities for one market, or None.

    Returns (anchor_book, {selection: fair_prob}). Shared by the value finder
    and the live CLV true-up (closing fair probability per pick).
    """
    selections = list(prices.keys())
    if len(selections) < 2 or len(set(selections)) != len(selections):
        return None
    anchor_book, anchor_odds = _named_sharp_anchor(
        prices, selections, sharp_books, commissions, max_overround
    )
    if anchor_book is None:
        anchor_book, anchor_odds = _consensus_anchor(prices, selections, commissions, max_overround)
    if anchor_book is None or anchor_odds is None:
        return None
    fair = devig(anchor_odds, method=devig_method)
    return anchor_book, dict(zip(selections, fair, strict=True))


def find_value_bets(
    prices: Mapping[str, Mapping[str, float]],
    *,
    min_edge: float = 0.01,
    min_odds: float = 1.30,
    max_overround: float = 0.12,
    devig_method: DevigMethod = DevigMethod.POWER,
    sharp_books: Sequence[str] = SHARP_BOOKS,
    commissions: Mapping[str, float] = EXCHANGE_COMMISSION,
) -> list[ValueBet]:
    """Find value selections for one market.

    `prices` maps selection -> {bookmaker: decimal_odds}. Selections are the
    market's mutually-exclusive outcomes (e.g. home/Draw/away). Returns value
    bets sorted by edge (desc). Returns [] when no trustworthy fair-value
    anchor exists (better no pick than a contaminated one).
    """
    anchored = anchor_fair_probs(
        prices,
        max_overround=max_overround,
        devig_method=devig_method,
        sharp_books=sharp_books,
        commissions=commissions,
    )
    if anchored is None:
        return []
    anchor_book, fair_by_sel = anchored
    return _scan_against_fair(prices, fair_by_sel, anchor_book, min_edge, min_odds, commissions)


def double_chance_fair(
    h2h_fair: Mapping[str, float], home: str, away: str, draw: str = "Draw"
) -> dict[str, float]:
    """Fair double-chance probabilities DERIVED from an anchored 1X2 market.

    DC outcomes overlap (each covers two 1X2 outcomes; the three quotes sum
    to ~200%), so devigging a DC book directly is invalid — the overround
    sanity check rightly rejects it. Fair DC value is the pairwise sum of the
    1X2 anchor's fair probabilities. Selection names mirror the oddsportal
    loader ("{home} or Draw", "{home} or {away}", "Draw or {away}")."""
    h, d, a = h2h_fair.get(home), h2h_fair.get(draw), h2h_fair.get(away)
    if h is None or d is None or a is None:
        return {}
    return {
        f"{home} or {draw}": h + d,
        f"{home} or {away}": h + a,
        f"{draw} or {away}": d + a,
    }


def find_value_bets_with_fair(
    prices: Mapping[str, Mapping[str, float]],
    fair_by_sel: Mapping[str, float],
    anchor_book: str,
    *,
    min_edge: float = 0.01,
    min_odds: float = 1.30,
    commissions: Mapping[str, float] = EXCHANGE_COMMISSION,
) -> list[ValueBet]:
    """Value scan against EXTERNALLY-derived fair probabilities (e.g. double
    chance derived from the 1X2 anchor via `double_chance_fair`). Selections
    without a derived fair probability are skipped."""
    return _scan_against_fair(prices, fair_by_sel, anchor_book, min_edge, min_odds, commissions)


def _scan_against_fair(
    prices: Mapping[str, Mapping[str, float]],
    fair_by_sel: Mapping[str, float],
    anchor_book: str,
    min_edge: float,
    min_odds: float,
    commissions: Mapping[str, float],
) -> list[ValueBet]:
    out: list[ValueBet] = []
    for sel in prices:
        fair_p = fair_by_sel.get(sel)
        if fair_p is None:
            continue
        best = _best_other_book(prices[sel], anchor_book, commissions)
        if best is None:
            continue
        best_book, raw, eff = best
        # Floor on the REALIZABLE price: net exchange commission first so a raw
        # 1.31 that nets 1.295 at a 5% exchange is correctly rejected under a
        # 1.30 floor (edge/EV/Kelly all run on eff, and min_acceptable_odds
        # reasons in eff space — keep the floor consistent with them). For soft
        # books eff == raw, so this is unchanged there.
        if eff < min_odds:
            continue
        implied = 1.0 / eff
        edge = fair_p - implied
        if edge < min_edge:
            continue
        ev = fair_p * (eff - 1.0) - (1.0 - fair_p)
        out.append(
            ValueBet(
                selection=sel,
                best_book=best_book,
                best_odds=raw,
                best_odds_effective=eff,
                sharp_book=anchor_book,
                sharp_fair_prob=fair_p,
                implied_prob=implied,
                edge=edge,
                ev=ev,
            )
        )
    out.sort(key=lambda v: v.edge, reverse=True)
    return out


def _named_sharp_anchor(
    prices: Mapping[str, Mapping[str, float]],
    selections: Sequence[str],
    sharp_books: Sequence[str],
    commissions: Mapping[str, float],
    max_overround: float,
) -> tuple[str | None, list[float] | None]:
    """First preferred sharp book that prices the FULL market with a sane
    overround. Odds are commission-netted before devig."""
    raw_by_norm: dict[str, str] = {}
    for s in selections:
        for b in prices[s]:
            raw_by_norm.setdefault(_norm(b), b)

    for pref in sharp_books:
        odds: list[float] = []
        complete = True
        for s in selections:
            o = _lookup(prices[s], pref)
            if o is None:
                complete = False
                break
            odds.append(effective_odds(pref, o, commissions))
        if not complete:
            continue
        if 0.0 <= _overround(odds) <= max_overround:
            return raw_by_norm[pref], odds
        # implausible anchor (stale/arb-looking) -> try next sharp / consensus
    return None, None


def _consensus_anchor(
    prices: Mapping[str, Mapping[str, float]],
    selections: Sequence[str],
    commissions: Mapping[str, float],
    max_overround: float,
) -> tuple[str | None, list[float] | None]:
    """Median effective price per selection across books that price the full
    market. Outlier-resistant: one bad quote cannot move the median much.
    Requires >= MIN_CONSENSUS_BOOKS full-market books."""
    books = set.intersection(*[{_norm(b) for b in prices[s]} for s in selections])
    if len(books) < MIN_CONSENSUS_BOOKS:
        return None, None
    med: list[float] = []
    for s in selections:
        # Deduplicate by NORMALIZED book name before the median: two raw keys that
        # normalize to the same book ('Bet365' + 'bet365') must count ONCE, or they
        # skew the median (audit #5). Keep the best (max effective) price per book,
        # matching the normalized intersection above and distinct_book_count.
        by_norm: dict[str, float] = {}
        for b, o in prices[s].items():
            nb = _norm(b)
            if nb not in books:
                continue
            e = effective_odds(b, o, commissions)
            if nb not in by_norm or e > by_norm[nb]:
                by_norm[nb] = e
        med.append(statistics.median(by_norm.values()))
    if not 0.0 <= _overround(med) <= max_overround:
        return None, None
    return CONSENSUS_ANCHOR, med


def _best_other_book(
    book_odds: Mapping[str, float],
    anchor_book: str,
    commissions: Mapping[str, float],
) -> tuple[str, float, float] | None:
    """Best EFFECTIVE odds among SOFT books — never the anchor AND never any sharp
    book. The actionable pick must be a price you bet at a SOFT bookmaker; a sharp
    book (Pinnacle/Betfair/Smarkets, incl. an injected sharp-anchor line) sets the
    fair value, so betting it is not the edge and may be unbettable (review
    2026-06-21). Returns (book, raw_odds, effective_odds)."""
    anchor_norm = _norm(anchor_book)
    sharp_norm = {_norm(b) for b in SHARP_BOOKS}
    best: tuple[str, float, float] | None = None
    for book, odds in book_odds.items():
        norm_book = _norm(book)
        if norm_book == anchor_norm or norm_book in sharp_norm:
            continue
        if odds <= 1.0:
            continue
        eff = effective_odds(book, odds, commissions)
        if best is None or eff > best[2]:
            best = (book, odds, eff)
    return best


def _lookup(book_odds: Mapping[str, float], norm_book: str) -> float | None:
    for book, odds in book_odds.items():
        if _norm(book) == norm_book:
            return odds
    return None
