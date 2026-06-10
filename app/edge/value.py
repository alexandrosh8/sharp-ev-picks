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
    selections = list(prices.keys())
    if len(selections) < 2 or len(set(selections)) != len(selections):
        return []

    anchor_book, anchor_odds = _named_sharp_anchor(
        prices, selections, sharp_books, commissions, max_overround
    )
    if anchor_book is None:
        anchor_book, anchor_odds = _consensus_anchor(prices, selections, commissions, max_overround)
    if anchor_book is None or anchor_odds is None:
        return []

    sharp_fair = devig(anchor_odds, method=devig_method)
    fair_by_sel = dict(zip(selections, sharp_fair, strict=True))

    out: list[ValueBet] = []
    for sel in selections:
        best = _best_other_book(prices[sel], anchor_book, commissions)
        if best is None:
            continue
        best_book, raw, eff = best
        if raw < min_odds:
            continue
        implied = 1.0 / eff
        fair_p = fair_by_sel[sel]
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
        eff = [effective_odds(b, o, commissions) for b, o in prices[s].items() if _norm(b) in books]
        med.append(statistics.median(eff))
    if not 0.0 <= _overround(med) <= max_overround:
        return None, None
    return CONSENSUS_ANCHOR, med


def _best_other_book(
    book_odds: Mapping[str, float],
    anchor_book: str,
    commissions: Mapping[str, float],
) -> tuple[str, float, float] | None:
    """Best EFFECTIVE odds among books other than the anchor.
    Returns (book, raw_odds, effective_odds)."""
    anchor_norm = _norm(anchor_book)
    best: tuple[str, float, float] | None = None
    for book, odds in book_odds.items():
        if _norm(book) == anchor_norm:
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
