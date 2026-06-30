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

# Normalized membership set for the classifier: ONLY these names earn a sharp/
# pinnacle anchor type. Anything else (blank, soft book) is consensus-grade.
_SHARP_BOOK_NORMS = frozenset(b.strip().lower() for b in SHARP_BOOKS)

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

# edge-ev-devig-r2-2: an anchor whose devigged probability ORDERING inverts the
# cross-book consensus ordering by more than this margin (in probability units) is
# treated as untrustworthy (a mislabeled/swapped line, e.g. the 1X2 Draw<->Away
# swap) and the WHOLE market is skipped — a market-level guard the per-leg max_edge
# cap cannot provide (a swap can sit under the cap). Generous enough that ordinary
# soft-vs-sharp disagreement never trips it; only a genuine rank inversion does.
ANCHOR_SWAP_TOLERANCE = 0.10

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

    "sharp" is minted ONLY for a genuine SHARP_BOOKS member (exchange), and
    "pinnacle" only for Pinnacle. A blank/unknown/SOFT book name is NOT a sharp
    anchor — it falls through to the not-trusted ANCHOR_TYPE_CONSENSUS bucket
    (consistent with ``is_sharp_anchored`` and excluded by the trusted CLV
    subset). This closes a latent honest-premium-faking hole: every live path
    feeds a SHARP_BOOKS member or CONSENSUS_ANCHOR, so live tags are unchanged,
    but no refactored/future call site can silently mint "sharp" from a soft or
    blank anchor.
    """
    if anchor_book == CONSENSUS_ANCHOR:
        return ANCHOR_TYPE_CONSENSUS
    norm = _norm(anchor_book)
    if "pinnacle" in norm:
        return ANCHOR_TYPE_PINNACLE
    if norm in _SHARP_BOOK_NORMS:
        return ANCHOR_TYPE_SHARP
    return ANCHOR_TYPE_CONSENSUS


def is_sharp_anchored(anchor_book: str) -> bool:
    """Whether fair value was backed by a GENUINE sharp book, not soft consensus.

    `anchor_book` is ValueBet.sharp_book — a named sharp book from SHARP_BOOKS
    (Pinnacle/Betfair/Smarkets) or the CONSENSUS_ANCHOR sentinel. The soft
    consensus(median) fallback (and any blank/unknown/soft anchor) is NOT sharp;
    only a named sharp anchor is. Defined as ``anchor_type_for(...) != consensus``
    so the twin predicates can never contradict each other (audit 2026-06-27: a
    blank/soft name previously passed this gate as True while routing to a sharp
    type — now both agree). Pure function (tested directly); the
    require-sharp-anchor premium gate (app/pipeline.py) consumes it.
    """
    return anchor_type_for(anchor_book) != ANCHOR_TYPE_CONSENSUS


def close_is_independent_of_fill(
    close_anchor_book: str,
    fill_book: str,
    *,
    pick_anchor_type: str = "",
    close_anchor_type: str = "",
    pick_anchor_book: str = "",
) -> bool:
    """Whether the CLOSE was priced INDEPENDENTLY of the pick (P0-1/P0-3).

    A close is CIRCULAR (fake CLV, |clv_log|~0) two ways:
    (1) it is anchored by the pick's OWN fill book (the book pricing its own close), or
    (2) it is anchored by the SAME sharp SOURCE that set pick-time fair — pick-time and
        close-time inject the same archived sharp line (audit 2026-06-25 finding #2), so
        close_fair ~= the number the edge was measured against and CLV is mechanical.
        A trustworthy close must come from a DIFFERENT source than the pick's anchor.

    `close_anchor_book` is the book that anchored the close (a named sharp book from
    SHARP_BOOKS, or the CONSENSUS_ANCHOR sentinel); `fill_book` is the pick's own
    bookmaker. The optional keyword `pick_anchor_type`/`close_anchor_type` are the anchor
    TYPES (pinnacle/sharp/consensus, via ``anchor_type_for``) of the pick and the close;
    when both are present and equal, the close is same-source => NOT independent. A
    CONSENSUS or blank close anchor is a >= MIN_CONSENSUS_BOOKS median, independent of any
    single fill by construction. Pure function (tested directly); the CLV true-up persists
    its result on each pick.
    """
    if not close_anchor_book or close_anchor_book == CONSENSUS_ANCHOR:
        return True
    # (1) the pick's own fill book pricing its own close
    if _norm(close_anchor_book) == _norm(fill_book):
        return False
    # (2) the SAME sharp source at pick-time and close-time (same archived line) is
    # circular; only a close from a DIFFERENT source is independent. When BOTH anchor
    # BOOKS are known (CLV-3), a DIFFERENT book is a genuinely different source even if
    # both collapse to the same anchor TYPE — e.g. a Smarkets-anchored pick validated
    # by a Betfair-exchange close (both 'sharp'): independent. Book equality is the
    # precise test; anchor-TYPE equality is the fallback only when a book is unknown.
    if (
        pick_anchor_book
        and pick_anchor_book != CONSENSUS_ANCHOR
        and close_anchor_book != CONSENSUS_ANCHOR
    ):
        return _norm(pick_anchor_book) != _norm(close_anchor_book)
    return not (
        pick_anchor_type
        and close_anchor_type
        and _norm(pick_anchor_type) == _norm(close_anchor_type)
    )


# Pick-time fair vs close-time fair must DIFFER by more than this (probability
# units) for the close to be independent CLV evidence. When closing_fair ==
# pick_fair (the SAME archived sharp line reused at pick-time and close-time),
# clv_log = ln(fill_eff * closing_fair) just re-encodes the pick-time edge — a
# TAUTOLOGY, not a verdict (live audit 2026-06-28: 133/272 settled picks had
# round(model_probability,4) == round(closing_fair_probability,4) yet carried a
# nonzero clv_log). 1e-3 mirrors the 4-dp archived-line resolution.
CLV_TAUTOLOGY_EPS = 1e-3


def close_moved_from_pick_fair(pick_fair: float | None, closing_fair: float) -> bool:
    """Whether the CLOSE fair MOVED from the pick-time fair by more than a
    de-minimis epsilon (``CLV_TAUTOLOGY_EPS``).

    An UNMOVED close (``closing_fair == pick_fair``) is the identical-archived-line
    TAUTOLOGY: clv_log = ln(fill_eff * closing_fair) merely re-encodes the pick-time
    edge (= pick_fair - 1/fill_eff), so it is NOT independent close evidence. A close
    is real CLV only if the line moved. ``pick_fair is None`` (unknowable pick-time
    fair) cannot prove movement => treated as NOT moved (conservative: no fake CLV).
    Pure function; the CLV true-up persists its AND with the fill-book check."""
    if pick_fair is None:
        return False
    return abs(closing_fair - pick_fair) > CLV_TAUTOLOGY_EPS


def persisted_close_independent(
    *,
    close_anchor_book: str,
    fill_book: str,
    pick_fair: float | None,
    closing_fair: float,
) -> bool:
    """The value persisted to ``picks.close_independent_of_fill`` (audit 2026-06-28).

    A close is INDEPENDENT (trustworthy CLV) only when BOTH hold:
      (1) it is NOT anchored by the pick's OWN fill book — a book pricing its own
          close is circular (``close_is_independent_of_fill``); AND
      (2) the close fair MOVED from the pick-time fair (``close_moved_from_pick_fair``)
          — an identical archived line makes clv_log a tautology.

    Gating (2) on the VALUE DELTA, not on the anchor BOOK NAME, is the fix: it
    RECOVERS a legitimate same-book MOVED-line close (e.g. a Pinnacle pick validated
    by a later, moved Pinnacle close — Betfair 0.471->0.516) that the old book-name
    same-source test structurally dropped (Pinnacle is SHARP_BOOKS[0] for both pick
    and close, so every Pinnacle-anchored pick was excluded), while still rejecting
    the identical-line tautology that the book test let through. Pure function."""
    if not close_is_independent_of_fill(close_anchor_book, fill_book):
        return False
    return close_moved_from_pick_fair(pick_fair, closing_fair)


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


def ah_candidate_plausible(
    bet: ValueBet,
    *,
    max_odds: float,
    max_sharp_soft_ratio: float,
) -> bool:
    """Sentinel/implausibility guard for a 2-way Asian-handicap value candidate.

    The read-only OddsPortal feed has been seen to carry SENTINEL AH prices (a
    backtest found odds like 22.0) that fabricate ~45% phantom edges. A liquid
    2-way AH line sits near pick'em — neither side is a deep longshot, and the
    sharp fair never disagrees with the soft price by a large multiple. Rejects
    a candidate when EITHER guard trips:

      (a) ODDS CEILING — the RAW best price exceeds ``max_odds`` (a 22.0 cannot
          be one side of a near-even 2-way market; it is a feed defect).
      (b) SHARP-vs-SOFT RATIO — the sharp fair probability exceeds the soft
          best-price implied probability by more than ``max_sharp_soft_ratio``×
          (the implied-prob gap is implausibly large for a liquid AH line, so the
          "edge" is a data artefact, not real value).

    Pure function (no IO); informational-only platform — nothing here places a
    bet. Applied ONLY to Asian-handicap candidates at the candidate-building
    boundary, so non-AH markets are untouched.
    """
    if bet.best_odds > max_odds:
        return False
    if bet.implied_prob <= 0.0:
        return False
    return bet.sharp_fair_prob / bet.implied_prob <= max_sharp_soft_ratio


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
    consensus_logit_pool: bool = False,
    liquidity: Mapping[str, Mapping[str, float]] | None = None,
    exchange_min_liquidity: float = 0.0,
) -> tuple[str, dict[str, float]] | None:
    """Trustworthy fair probabilities for one market, or None.

    Returns (anchor_book, {selection: fair_prob}). Shared by the value finder
    and the live CLV true-up (closing fair probability per pick).
    """
    selections = list(prices.keys())
    if len(selections) < 2 or len(set(selections)) != len(selections):
        return None
    anchor_book, anchor_odds = _named_sharp_anchor(
        prices,
        selections,
        sharp_books,
        commissions,
        max_overround,
        liquidity=liquidity,
        exchange_min_liquidity=exchange_min_liquidity,
    )
    if anchor_book is None:
        if consensus_logit_pool:
            anchor_book, anchor_odds = _logit_consensus_anchor(
                prices, selections, commissions, max_overround, devig_method
            )
        else:
            anchor_book, anchor_odds = _consensus_anchor(
                prices, selections, commissions, max_overround
            )
    if anchor_book is None or anchor_odds is None:
        return None
    fair = devig(anchor_odds, method=devig_method)
    return anchor_book, dict(zip(selections, fair, strict=True))


def find_value_bets(
    prices: Mapping[str, Mapping[str, float]],
    *,
    min_edge: float = 0.01,
    min_odds: float = 1.30,
    max_edge: float = math.inf,
    max_overround: float = 0.12,
    devig_method: DevigMethod = DevigMethod.POWER,
    sharp_books: Sequence[str] = SHARP_BOOKS,
    commissions: Mapping[str, float] = EXCHANGE_COMMISSION,
    consensus_logit_pool: bool = False,
    liquidity: Mapping[str, Mapping[str, float]] | None = None,
    exchange_min_liquidity: float = 0.0,
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
        consensus_logit_pool=consensus_logit_pool,
        liquidity=liquidity,
        exchange_min_liquidity=exchange_min_liquidity,
    )
    if anchored is None:
        return []
    anchor_book, fair_by_sel = anchored
    return _scan_against_fair(
        prices, fair_by_sel, anchor_book, min_edge, min_odds, commissions, max_edge=max_edge
    )


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
    max_edge: float = math.inf,
    commissions: Mapping[str, float] = EXCHANGE_COMMISSION,
) -> list[ValueBet]:
    """Value scan against EXTERNALLY-derived fair probabilities (e.g. double
    chance derived from the 1X2 anchor via `double_chance_fair`). Selections
    without a derived fair probability are skipped."""
    return _scan_against_fair(
        prices, fair_by_sel, anchor_book, min_edge, min_odds, commissions, max_edge=max_edge
    )


def _consensus_implied(
    prices: Mapping[str, Mapping[str, float]],
    fair_by_sel: Mapping[str, float],
    commissions: Mapping[str, float],
) -> dict[str, float] | None:
    """Cross-book reference probabilities for the swap guard: per selection, the
    MEDIAN effective-implied probability across the distinct books pricing it,
    normalized to sum 1. Returns None when any priced selection has fewer than
    MIN_CONSENSUS_BOOKS books — too thin to cross-check the anchor. Outlier-
    resistant by construction (a single swapped book cannot move the median),
    so a swapped anchor stands out against it."""
    ref: dict[str, float] = {}
    for sel in fair_by_sel:
        by_norm: dict[str, float] = {}
        for book, odds in prices.get(sel, {}).items():
            if odds <= 1.0:
                continue
            nb = _norm(book)
            implied = 1.0 / effective_odds(book, odds, commissions)
            # keep the LOWEST implied (best price) per book — mirrors consensus dedupe
            if nb not in by_norm or implied < by_norm[nb]:
                by_norm[nb] = implied
        if len(by_norm) < MIN_CONSENSUS_BOOKS:
            return None
        ref[sel] = statistics.median(by_norm.values())
    total = sum(ref.values())
    if total <= 0.0:
        return None
    return {sel: p / total for sel, p in ref.items()}


def _anchor_disagrees_with_consensus(
    prices: Mapping[str, Mapping[str, float]],
    fair_by_sel: Mapping[str, float],
    commissions: Mapping[str, float],
    tolerance: float,
) -> bool:
    """True when the anchor's devigged ORDERING inverts the cross-book consensus
    ordering by more than `tolerance` — the signature of a mislabeled/swapped
    anchor line (e.g. 1X2 Draw<->Away). Whenever the consensus separates A above B
    by more than the tolerance yet the anchor ranks A at-or-below B, the anchor is
    untrustworthy. No-op when there is no consensus to cross-check against."""
    ref = _consensus_implied(prices, fair_by_sel, commissions)
    if ref is None:
        return False
    sels = list(fair_by_sel)
    for a in sels:
        for b in sels:
            if ref[a] - ref[b] > tolerance and fair_by_sel[a] <= fair_by_sel[b]:
                return True
    return False


def _scan_against_fair(
    prices: Mapping[str, Mapping[str, float]],
    fair_by_sel: Mapping[str, float],
    anchor_book: str,
    min_edge: float,
    min_odds: float,
    commissions: Mapping[str, float],
    max_edge: float = math.inf,
    anchor_swap_tolerance: float = ANCHOR_SWAP_TOLERANCE,
) -> list[ValueBet]:
    # edge-ev-devig-r2-2: MARKET-LEVEL anchor-swap guard. If the anchor's devigged
    # ordering inverts the cross-book consensus ordering beyond tolerance, the anchor
    # is untrustworthy (a swapped/mislabeled line) — mint NOTHING for the whole
    # market. Complements the per-leg max_edge cap, which a swap can slip under.
    if _anchor_disagrees_with_consensus(prices, fair_by_sel, commissions, anchor_swap_tolerance):
        return []
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
        # Reject below the floor AND above the ceiling: an edge above max_edge is
        # a DATA ERROR (a mislabeled/garbage anchor — e.g. the 1X2 Draw<->away
        # swap), never real value on a liquid market, so it must never mint/alert.
        if edge < min_edge or edge > max_edge:
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
    liquidity: Mapping[str, Mapping[str, float]] | None = None,
    exchange_min_liquidity: float = 0.0,
) -> tuple[str | None, list[float] | None]:
    """First preferred sharp book that prices the FULL market with a sane
    overround. Returns the GROSS (displayed) anchor odds for the fair-probability
    devig.

    edge-ev-devig-P2-1: commission is a PAYOUT cost, not a probability signal, so
    the fair-probability estimate devigs the GROSS odds — netting commission before
    the devig inflates the favourite's implied probability (the bias mostly cancels
    on renormalisation but leaves a residual on ASYMMETRIC markets). Commission
    netting stays ONLY on the bet-side price (``_best_other_book`` / ``effective_odds``
    in ``_scan_against_fair``) used for edge/EV/CLV. The overround PLAUSIBILITY gate
    still runs on the NET odds, so anchor MEMBERSHIP (which markets earn a sharp
    anchor) is unchanged — only the returned fair MAGNITUDE moves, and only on
    commissioned (exchange) anchors; Pinnacle (no commission) is bit-identical.

    Build #3 Phase 1 (default OFF): when ``exchange_min_liquidity > 0``, an EXCHANGE
    candidate (Betfair/Smarkets/Matchbook) must show matched ``liquidity`` >= that
    floor on EVERY selection to earn 'sharp' grade — a thin / just-firmed / unknown-
    liquidity exchange line is not trustworthy-sharp (the Welwalo lesson). At the
    default floor 0 the gate is inert and behaviour is bit-for-bit unchanged."""
    raw_by_norm: dict[str, str] = {}
    for s in selections:
        for b in prices[s]:
            raw_by_norm.setdefault(_norm(b), b)

    for pref in sharp_books:
        net_odds: list[float] = []  # commission-netted — overround gate only
        gross_odds: list[float] = []  # displayed odds — devigged for the fair prob
        complete = True
        for s in selections:
            o = _lookup(prices[s], pref)
            if o is None:
                complete = False
                break
            net_odds.append(effective_odds(pref, o, commissions))
            gross_odds.append(o)
        if not complete:
            continue
        if exchange_min_liquidity > 0.0 and _norm(pref) in commissions:
            # Exchange anchor: require matched liquidity >= floor on EVERY selection.
            # Unknown (None) liquidity does NOT qualify a 'sharp' exchange anchor.
            ok = liquidity is not None
            for s in selections:
                if not ok:
                    break
                sel_liq = liquidity.get(s) if liquidity is not None else None
                lq = _lookup(sel_liq, _norm(pref)) if sel_liq is not None else None
                if lq is None or lq < exchange_min_liquidity:
                    ok = False
            if not ok:
                continue
        # Gate on NET overround (membership unchanged); devig the GROSS odds.
        if 0.0 <= _overround(net_odds) <= max_overround:
            return raw_by_norm[pref], gross_odds
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


def _logit_consensus_anchor(
    prices: Mapping[str, Mapping[str, float]],
    selections: Sequence[str],
    commissions: Mapping[str, float],
    max_overround: float,
    devig_method: DevigMethod,
) -> tuple[str | None, list[float] | None]:
    """Log-odds (logit) POOL consensus across full-market books (build #1).

    Devig EACH book to a fair distribution, then pool per selection in LOGIT
    space: ``consensus_p = sigmoid(mean_b logit(p_b))``, renormalized to sum 1.
    Unlike the median-of-PRICES consensus, a logit pool is non-extremizing and
    order-invariant — it does not lose tail sharpness on heavy favourites /
    longshots, where a linear price average is provably under-confident
    (Gneiting & Ranjan 2010). Returns synthetic NO-VIG prices (1/p) so the shared
    downstream devig in ``anchor_fair_probs`` recovers p unchanged. Requires
    >= MIN_CONSENSUS_BOOKS books pricing the full market. Outlier handling matches
    ``_consensus_anchor``: dedupe by normalized book, keep best effective price."""
    books = set.intersection(*[{_norm(b) for b in prices[s]} for s in selections])
    if len(books) < MIN_CONSENSUS_BOOKS:
        return None, None
    book_vec: dict[str, dict[str, float]] = {nb: {} for nb in books}
    for s in selections:
        by_norm: dict[str, float] = {}
        for b, o in prices[s].items():
            nb = _norm(b)
            if nb not in books:
                continue
            e = effective_odds(b, o, commissions)
            if nb not in by_norm or e > by_norm[nb]:
                by_norm[nb] = e
        for nb, e in by_norm.items():
            book_vec[nb][s] = e
    logit_sum = {s: 0.0 for s in selections}
    k = 0
    for vec in book_vec.values():
        if any(s not in vec for s in selections):
            continue
        fair_b = devig([vec[s] for s in selections], method=devig_method)
        k += 1
        for s, p in zip(selections, fair_b, strict=True):
            p = min(max(p, 1e-12), 1.0 - 1e-12)
            logit_sum[s] += math.log(p / (1.0 - p))
    if k < MIN_CONSENSUS_BOOKS:
        return None, None
    pooled = {s: 1.0 / (1.0 + math.exp(-logit_sum[s] / k)) for s in selections}
    total = sum(pooled.values())
    if total <= 0.0:
        return None, None
    # Synthetic NO-VIG prices (renormalized): 1 / (pooled/total) = total/pooled.
    synth_prices = [total / pooled[s] for s in selections]
    # The synthetic vector is no-vig by construction (overround ~ 0 +/- float dust).
    # Only the UPPER bound is a meaningful sanity check: a 0.0 lower bound made this
    # hash-seed-flaky, because the logit sum accumulates in set-iteration order and
    # float rounding could push the overround a hair negative. Reject only a result
    # that is genuinely over-margined.
    if _overround(synth_prices) > max_overround:
        return None, None
    return CONSENSUS_ANCHOR, synth_prices


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
