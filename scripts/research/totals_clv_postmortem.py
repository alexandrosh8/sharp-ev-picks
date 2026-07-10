"""Soccer totals CLV post-mortem — is the -6.0% trusted-CLV drag a bug or a signal?

CONTEXT (strategy-revision plan Task 3, 2026-07-10)
  Soccer totals trusted sharp-CLV measured -0.0602 (SE 0.0241, n=24) while the
  soccer spreads CONTROL cell measured +0.0518 (SE 0.0212, n=34). Three
  hypotheses, mutually distinguishable by splits:

    (a) close-matching defect — a pick at line 2.5 graded against a close from
        a DIFFERENT line (e.g. 3.0/3.5). The mint-time ``market_detail`` stamp
        (exact-key matching, app/clv_trueup.py) only began 2026-07-10; every
        settled row went through the LEGACY line-blind (market, selection)
        path. Test: re-derive the close row's line from odds_snapshots via the
        persisted D3 close provenance (close_anchor_book +
        close_snapshot_captured_at) and compare against the line embedded in
        the pick's selection string ("Under 2.5" -> 2.5).
    (b) devig structure — 2-way totals devigged with a method whose fallback
        behavior differs between mint and close. Test: split trusted CLV by
        mint_devig_fell_back x close_devig_fell_back (the trusted subset
        already excludes ASYMMETRIC pairs; within it, compare the symmetric
        cells).
    (c) genuine market signal — sharp books price totals efficiently and the
        drag survives every split. Test: per-anchor-book and per-line splits
        stay uniformly negative while the SAME splits on spreads stay positive.

TRUST RULES (mirrors app/storage/repositories.py — the trusted sharp subset)
  clv_log NOT NULL, NOT tautological (|close_fair - model_prob| > 1e-3), NOT
  fabricated (close-implied edge <= 0.20 when both inputs exist, else
  |clv_log| <= 0.5), has_snapshot_close IS TRUE, closing_anchor_type IN
  ('pinnacle','sharp'), close_independent_of_fill IS TRUE, and SYMMETRIC devig
  fallback flags. Splits are also reported on the broader settled population
  for funnel context, clearly labeled.

READ-ONLY: SELECT-only SQL. Writes nothing, flips no config, never places a
bet. Nothing here re-tunes a threshold.

    uv run python scripts/research/totals_clv_postmortem.py
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

# Mirrors app/storage/repositories.py constants (kept local: this script must
# report the SAME subset the /performance trusted number is built from).
CLV_TAUTOLOGY_EPS = 1e-3
CLV_IMPLAUSIBLE_CLOSE_EDGE = 0.20
CLV_IMPLAUSIBLE_LOG = 0.5
SHARP_CLOSE_ANCHORS = ("pinnacle", "sharp")
CONSENSUS_BOOK = "consensus(median)"

MARKETS = ("totals", "spreads")  # totals = subject, spreads = control cell

# --------------------------------------------------------------------------- #
# SQL (SELECT only)
# --------------------------------------------------------------------------- #
PICKS_SQL = text(
    """
    SELECT p.id,
           p.event_id,
           p.market,
           p.market_detail,
           p.selection,
           p.bookmaker,
           p.decimal_odds::float8      AS decimal_odds,
           p.model_probability::float8 AS model_probability,
           p.closing_fair_probability::float8 AS closing_fair_probability,
           p.clv_log::float8           AS clv_log,
           p.closing_anchor_type,
           p.has_snapshot_close,
           p.close_independent_of_fill,
           p.mint_devig_fell_back,
           p.close_devig_fell_back,
           p.anchor_type,
           p.anchor_book,
           p.close_anchor_book,
           p.close_snapshot_captured_at,
           p.close_exclusion_reason,
           e.starts_at,
           rt.outcome
    FROM picks p
    JOIN result_tracking rt ON rt.pick_id = p.id
    JOIN events e            ON e.id = p.event_id
    JOIN sports s            ON s.id = e.sport_id
    WHERE s.key = 'soccer'
      AND p.market = ANY(:markets)
    ORDER BY p.id
    """
)

# Close-anchor book's snapshot rows for one pick's (event, selection):
# every pre-kickoff observation, latest first. The line each row belongs to is
# encoded in the SNAPSHOT market key (totals_2_5, asian_handicap_-1_5, ...).
CLOSE_ROWS_SQL = text(
    """
    SELECT os.market, os.captured_at
    FROM odds_snapshots os
    WHERE os.event_id = :event_id
      AND os.bookmaker = :book
      AND os.selection = :selection
      AND (CAST(:kickoff AS timestamptz) IS NULL
           OR os.captured_at <= CAST(:kickoff AS timestamptz))
    ORDER BY os.captured_at DESC
    """
)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Row:
    pick_id: int
    event_id: int
    market: str
    market_detail: str | None
    selection: str
    bookmaker: str
    decimal_odds: float | None
    model_probability: float | None
    closing_fair_probability: float | None
    clv_log: float | None
    closing_anchor_type: str | None
    has_snapshot_close: bool | None
    close_independent_of_fill: bool | None
    mint_devig_fell_back: bool | None
    close_devig_fell_back: bool | None
    anchor_type: str | None
    anchor_book: str | None
    close_anchor_book: str | None
    close_snapshot_captured_at: datetime | None
    close_exclusion_reason: str | None
    starts_at: datetime | None
    outcome: str | None


def is_tautological(r: Row) -> bool:
    if r.clv_log is None or r.closing_fair_probability is None or r.model_probability is None:
        return False
    return abs(r.closing_fair_probability - r.model_probability) <= CLV_TAUTOLOGY_EPS


def is_fabricated(r: Row) -> bool:
    if r.clv_log is None:
        return False
    if r.decimal_odds is not None and r.closing_fair_probability is not None and r.decimal_odds:
        return (r.closing_fair_probability - 1.0 / r.decimal_odds) > CLV_IMPLAUSIBLE_CLOSE_EDGE
    return abs(r.clv_log) > CLV_IMPLAUSIBLE_LOG


def fallback_asymmetric(r: Row) -> bool:
    if r.mint_devig_fell_back is None or r.close_devig_fell_back is None:
        return False
    return r.mint_devig_fell_back != r.close_devig_fell_back


def is_trusted(r: Row) -> bool:
    """The trusted sharp-CLV subset gate (repositories._aggregate_settled)."""
    return (
        r.clv_log is not None
        and not is_tautological(r)
        and not is_fabricated(r)
        and bool(r.has_snapshot_close)
        and r.closing_anchor_type in SHARP_CLOSE_ANCHORS
        and r.close_independent_of_fill is True
        and not fallback_asymmetric(r)
    )


_SEL_LINE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*$")


def selection_line(selection: str) -> float | None:
    """Line embedded in the pick's selection string ('Under 2.5' -> 2.5,
    'Annan -1.5' -> -1.5). None = lineless ('Over', 'Draw', bare team)."""
    m = _SEL_LINE.search(selection.strip())
    return float(m.group(1)) if m else None


_TOTALS_KEY = re.compile(r"^(?:totals|over_under)_(\d+)(?:_(\d+))?$")
_AH_KEY = re.compile(r"^asian_handicap_(-?\d+)(?:_(\d+))?$")
_SPREAD_KEY = re.compile(r"^spreads_(minus|plus)_(\d+)(?:_(\d+))?$")


def snapshot_market_line(market: str) -> float | None:
    """Line encoded in a full-match snapshot market key. None = not a
    full-match totals/handicap key (period/team/corner/oc_ keys excluded)."""
    m = _TOTALS_KEY.match(market)
    if m:
        return float(f"{m.group(1)}.{m.group(2) or '0'}")
    m = _AH_KEY.match(market)
    if m:
        whole = float(m.group(1))
        frac = float(f"0.{m.group(2)}") if m.group(2) else 0.0
        return whole - frac if whole < 0 or m.group(1).startswith("-") else whole + frac
    m = _SPREAD_KEY.match(market)
    if m:
        sign = -1.0 if m.group(1) == "minus" else 1.0
        return sign * float(f"{m.group(2)}.{m.group(3) or '0'}")
    return None


def mean_se(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, float("nan")
    var = sum((x - m) ** 2 for x in xs) / (n - 1)  # ddof=1
    return m, math.sqrt(var / n)


def cell(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    m, se = mean_se(xs)
    se_s = f"{se:.4f}" if not math.isnan(se) else "n/a"
    return f"n={len(xs):>3}  mean={m:+.4f}  SE={se_s}"


# --------------------------------------------------------------------------- #
# Close-line verification (hypothesis a)
# --------------------------------------------------------------------------- #
async def verify_close_line(session, r: Row) -> str:  # noqa: ANN001
    """Classify one trusted-close pick's close-line consistency.

    Returns one of:
      exact_stamped     — mint market_detail stamp present (exact-key matched)
      line_consistent   — close-book snapshot line == pick's selection line
      LINE_MISMATCH     — close-book rows exist ONLY at a different |line|
      multi_line        — same selection string spans >1 line at the close book
      lineless_selection— selection carries no line (bare Over/Under/Draw)
      unverifiable      — consensus close / no provenance / no snapshot rows
    """
    if r.market_detail is not None:
        return "exact_stamped"
    if not r.close_anchor_book or r.close_anchor_book == CONSENSUS_BOOK:
        return "unverifiable"
    sel_line = selection_line(r.selection)
    if sel_line is None:
        return "lineless_selection"
    rows = (
        await session.execute(
            CLOSE_ROWS_SQL,
            {
                "event_id": r.event_id,
                "book": r.close_anchor_book,
                "selection": r.selection,
                "kickoff": r.starts_at,
            },
        )
    ).all()
    if not rows:
        return "unverifiable"
    # Prefer the exact provenance capture instant; else the latest pre-kickoff.
    at_stamp = [m for (m, cap) in rows if cap == r.close_snapshot_captured_at]
    markets = at_stamp if at_stamp else [rows[0][0]]
    lines = {ln for m in markets if (ln := snapshot_market_line(m)) is not None}
    if not lines:
        return "unverifiable"
    if len(lines) > 1:
        return "multi_line"
    (close_line,) = lines
    # |line| compare: spreads selection sign is side-oriented, the snapshot key
    # sign is home-oriented; a 2.5-vs-3.0 grading defect shows in magnitude.
    return "line_consistent" if abs(close_line) == abs(sel_line) else "LINE_MISMATCH"


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def split_by(rows: list[Row], key) -> dict[str, list[float]]:  # noqa: ANN001
    out: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        out[str(key(r))].append(float(r.clv_log))  # trusted rows: clv_log set
    return out


def print_split(title: str, buckets: dict[str, list[float]]) -> None:
    print(f"  {title}")
    for k in sorted(buckets):
        print(f"    {k:<28} {cell(buckets[k])}")


# Global warehouse integrity check for hypothesis (a): does ANY snapshot pair a
# line-bearing selection string with a market key of a DIFFERENT line? If the
# selection literal always pins its own line, the legacy line-blind close match
# cannot have graded a 2.5 pick against a 3.0-line close.
GLOBAL_PAIRS_SQL = text(
    r"""
    SELECT DISTINCT os.market, os.selection
    FROM odds_snapshots os
    WHERE os.market ~ '^(totals|over_under)_-?[0-9]+(_[0-9]+)?$'
       OR os.market ~ '^asian_handicap_-?[0-9]+(_[0-9]+)?$'
       OR os.market ~ '^spreads_(minus|plus)_[0-9]+(_[0-9]+)?$'
    """
)


async def global_line_integrity(session) -> None:  # noqa: ANN001
    pairs = (await session.execute(GLOBAL_PAIRS_SQL)).all()
    checked = mismatched = lineless = 0
    examples: list[tuple[str, str]] = []
    for market, selection in pairs:
        mline = snapshot_market_line(market)
        sline = selection_line(selection)
        if mline is None:
            continue
        if sline is None:
            lineless += 1
            continue
        checked += 1
        if abs(sline) != abs(mline):
            mismatched += 1
            if len(examples) < 10:
                examples.append((market, selection))
    print("\n### GLOBAL warehouse line integrity (all full-match totals/handicap keys) ###")
    print(f"  distinct (market, selection) pairs with a line-bearing selection: {checked}")
    print(f"  |selection line| != |market-key line| (MISMATCH pairs)          : {mismatched}")
    print(f"  lineless selections (bare Over/Under/team — line-blind exposure): {lineless}")
    for m, s in examples:
        print(f"    MISMATCH example: market={m!r} selection={s!r}")


async def run() -> None:
    from app.config import get_settings
    from app.database import create_engine, create_session_factory

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            raw = (await session.execute(PICKS_SQL, {"markets": list(MARKETS)})).all()
            rows = [Row(*r) for r in raw]
            print("=" * 76)
            print("SOCCER TOTALS CLV POST-MORTEM (spreads = control cell) — READ-ONLY")
            print(f"settled soccer picks loaded: {len(rows)}  (markets: {', '.join(MARKETS)})")
            print("=" * 76)
            await global_line_integrity(session)

            for market in MARKETS:
                mrows = [r for r in rows if r.market == market]
                trusted = [r for r in mrows if is_trusted(r)]
                print(f"\n### market = {market} ###")
                print(f"  settled n={len(mrows)}  trusted-close n={len(trusted)}")
                print(f"  outcomes: {dict(Counter(r.outcome for r in mrows))}")
                print(f"  TRUSTED headline: {cell([float(r.clv_log) for r in trusted])}")

                # (1) mint-detail stamp + close-line verification
                stamp = Counter(
                    "mint_detail_stamped" if r.market_detail is not None else "legacy_line_blind"
                    for r in trusted
                )
                print_split(
                    "(1) mint market_detail stamp (trusted subset):",
                    {k: [] for k in ()},  # header only
                )
                for k, v in sorted(stamp.items()):
                    print(f"    {k:<28} n={v}")
                verd: dict[str, list[float]] = defaultdict(list)
                for r in trusted:
                    verd[await verify_close_line(session, r)].append(float(r.clv_log))
                print_split("(1) close-line verification vs close_anchor_book snapshots:", verd)

                # (2) devig fallback structure
                print_split(
                    "(2) trusted CLV by mint_devig_fell_back x close_devig_fell_back:",
                    split_by(
                        trusted,
                        lambda r: f"mint={r.mint_devig_fell_back} close={r.close_devig_fell_back}",
                    ),
                )
                excl = Counter(
                    r.close_exclusion_reason
                    for r in mrows
                    if r.clv_log is not None and not is_trusted(r)
                )
                print(f"  (2b) non-trusted rows w/ CLV, exclusion reasons: {dict(excl)}")

                # (3) anchor-book and line splits
                print_split(
                    "(3) trusted CLV by CLOSE anchor book:",
                    split_by(trusted, lambda r: r.close_anchor_book or "(null)"),
                )
                print_split(
                    "(3) trusted CLV by MINT anchor book:",
                    split_by(trusted, lambda r: r.anchor_book or "(null)"),
                )

                def line_key(r: Row, market: str = market) -> str:
                    sl = selection_line(r.selection)
                    if sl is None:
                        return "line=None"
                    if market == "totals":
                        return "line=2.5" if sl == 2.5 else f"line={sl}"
                    return f"|line|={abs(sl)}"

                print_split("(3) trusted CLV by line:", split_by(trusted, line_key))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
