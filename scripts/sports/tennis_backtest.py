"""Held-out VALUE backtest for TENNIS (ATP + WTA) — mirrors value_backtest.py.

Methodology (review-corrected, identical spirit to scripts/value_backtest.py):
  - Fair value      = devig(Pinnacle pre-match) over the 2-way H2H market
                      [PSW, PSL]. Tennis H2H is devig-sound: two
                      mutually-exclusive, full-coverage outcomes.
  - Bet             = the best available price (Max-of-books, [MaxW, MaxL])
                      when it beats the Pinnacle fair price by >= threshold.
  - ONE bet per match (the single highest-edge side). The two sides of an H2H
    are perfectly anti-correlated, so betting both would double-count the same
    match and inflate any i.i.d. confidence interval.
  - Thresholds + devig methods are swept on TRAIN years ONLY; the best train
    combo is evaluated ONCE on held-out TEST years. No in-sample headline.
  - Settlement uses the REAL result (Winner/Loser). Only Comment == "Completed"
    matches settle; Retired / Walkover / Awarded are quarantined (logged count)
    because the staked market would void or resolve ambiguously.
  - The verdict is COMPUTED from held-out numbers, never hardcoded.

CRITICAL DATA TRUTH — tennis-data.co.uk has NO closing columns.
  The football quartet (PSC*/MaxC*) does not exist for tennis: only PSW/PSL
  (Pinnacle pre-match) and MaxW/MaxL (max pre-match) are published. Therefore
  CLV-vs-close and CLV-vs-max-close are UNDEFINED here, and tennis can NEVER
  clear the doctrine gate "incremental CLV vs the closing line > 2 SE". The
  best obtainable verdict is VISIBILITY-ONLY / UNVALIDATED, even if held-out
  ROI is positive. We report ROI with bootstrap CIs clustered by match and say
  so honestly. (Verified absent across ATP+WTA 2021-2024; see data-source memo.)

Bootstrap CIs are CLUSTERED BY MATCH: each match contributes at most one bet,
so the cluster == the bet here, but we keep the clustered resampler so the
helper is correct if the market set is ever widened (e.g. set/games totals).

    uv run python scripts/sports/tennis_backtest.py
    uv run python scripts/sports/tennis_backtest.py --tours atp,wta \\
        --train-years 2019,2020,2021,2022,2023 --test-years 2024,2025,2026

Decision-support only — nothing here places bets.
"""

from __future__ import annotations

import argparse
import logging
import random
from dataclasses import dataclass
from pathlib import Path

from app.probabilities.devig import DevigMethod, devig

logger = logging.getLogger("tennis_backtest")

# --- data location ---------------------------------------------------------
# /data/ is gitignored (see .gitignore). Cache xlsx here so reruns are offline.
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "backtest_cache"

# tennis-data.co.uk URL schemes (verified 2026-06-16):
#   ATP: http://www.tennis-data.co.uk/{y}/{y}.xlsx
#   WTA: http://www.tennis-data.co.uk/{y}w/{y}.xlsx
_TOUR_URL = {
    "atp": "http://www.tennis-data.co.uk/{y}/{y}.xlsx",
    "wta": "http://www.tennis-data.co.uk/{y}w/{y}.xlsx",
}

# Pinnacle (sharp) pre-match and Max-of-books pre-match, winner/loser order.
# NOTE: no closing analogues exist -> clv_* is permanently None.
SHARP_COLS = ("PSW", "PSL")
MAX_COLS = ("MaxW", "MaxL")


def _f(x: object) -> float | None:
    """Parse a decimal-odds cell; reject anything <= 1.0 or non-numeric."""
    try:
        v = float(str(x))
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


@dataclass(frozen=True)
class TennisRow:
    """One match, pre-parsed to the fields the backtest needs.

    `winner_idx` is the outcome index that actually won in [winner, loser]
    order: it is always 0 because the source already labels the winner first.
    We keep it explicit so settlement is auditable and so the settle helper is
    testable on synthetic rows where the winning side may be index 1.
    """

    match_id: str
    sharp: tuple[float | None, float | None]  # (PSW, PSL) decimal odds
    best: tuple[float | None, float | None]  # (MaxW, MaxL) decimal odds
    winner_idx: int  # 0 == first listed side won
    completed: bool


@dataclass(frozen=True)
class TBet:
    match_id: str
    won: bool
    odds: float  # the price actually taken (best-of-books)
    edge: float
    # CLV is structurally None for tennis (no closing line in the source).
    clv_close: float | None = None
    clv_max_close: float | None = None


def settle_side(row: TennisRow, side_idx: int) -> bool:
    """Did the bet on `side_idx` win? Pure; no source coupling."""
    return side_idx == row.winner_idx


def select_bet(
    row: TennisRow,
    thr: float,
    devig_method: DevigMethod,
    min_odds: float = 1.0,
) -> TBet | None:
    """ONE bet per match: the highest-edge side whose edge >= threshold.

    edge_i = fair_prob_i - 1/best_price_i, where fair_prob = devig(sharp).
    Returns None when the match is non-completed, has missing prices, or no
    side clears the threshold / odds floor.
    """
    if not row.completed:
        return None
    if None in row.sharp or None in row.best:
        return None
    sharp = (row.sharp[0], row.sharp[1])
    best_prices = (row.best[0], row.best[1])
    fair = devig([sharp[0], sharp[1]], method=devig_method)  # type: ignore[list-item]
    best: tuple[float, int] | None = None  # (edge, side_idx)
    for i in range(2):
        price = best_prices[i]
        if price is None or price < min_odds:
            continue  # odds floor (no short prices)
        edge = fair[i] - 1.0 / price
        if edge >= thr and (best is None or edge > best[0]):
            best = (edge, i)
    if best is None:
        return None
    edge, i = best
    return TBet(
        match_id=row.match_id,
        won=settle_side(row, i),
        odds=best_prices[i],  # type: ignore[arg-type]
        edge=edge,
        clv_close=None,  # no closing line exists for tennis
        clv_max_close=None,
    )


def bets_for(
    rows: list[TennisRow],
    thr: float,
    devig_method: DevigMethod,
    min_odds: float = 1.0,
) -> list[TBet]:
    out: list[TBet] = []
    for r in rows:
        b = select_bet(r, thr, devig_method, min_odds)
        if b is not None:
            out.append(b)
    return out


# --- metrics ---------------------------------------------------------------
@dataclass(frozen=True)
class Stats:
    n: int
    hit: float
    roi: float
    roi_lo: float  # bootstrap 2.5th pct, clustered by match
    roi_hi: float  # bootstrap 97.5th pct, clustered by match
    clv_close: float | None  # always None for tennis (no close)
    clv_close_se: float | None
    inc_clv_se: float | None  # bootstrap SE of incremental CLV vs close


def _profit(bets: list[TBet]) -> float:
    return sum((b.odds - 1.0) if b.won else -1.0 for b in bets)


def bootstrap_roi_ci(
    bets: list[TBet],
    n_boot: int = 2000,
    seed: int = 12345,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile bootstrap CI for ROI, RESAMPLING CLUSTERS (matches).

    Each match contributes its own bet(s); we resample matches with
    replacement so correlation within a match (none here, one bet/match, but
    correct in general) is preserved.
    """
    if not bets:
        return (0.0, 0.0)
    # cluster bets by match_id
    by_match: dict[str, list[TBet]] = {}
    for b in bets:
        by_match.setdefault(b.match_id, []).append(b)
    clusters = list(by_match.values())
    rng = random.Random(seed)
    k = len(clusters)
    rois: list[float] = []
    for _ in range(n_boot):
        picked: list[TBet] = []
        for _ in range(k):
            picked.extend(clusters[rng.randrange(k)])
        if picked:
            rois.append(_profit(picked) / len(picked))
    rois.sort()
    lo = rois[int((alpha / 2) * len(rois))]
    hi = rois[min(len(rois) - 1, int((1 - alpha / 2) * len(rois)))]
    return (lo, hi)


def stats_from_bets(bets: list[TBet], n_boot: int = 2000, seed: int = 12345) -> Stats:
    n = len(bets)
    if n == 0:
        return Stats(0, 0.0, 0.0, 0.0, 0.0, None, None, None)
    roi = _profit(bets) / n
    lo, hi = bootstrap_roi_ci(bets, n_boot=n_boot, seed=seed)
    # CLV is structurally undefined for tennis -> None, SE None.
    return Stats(
        n=n,
        hit=sum(1 for b in bets if b.won) / n,
        roi=roi,
        roi_lo=lo,
        roi_hi=hi,
        clv_close=None,
        clv_close_se=None,
        inc_clv_se=None,
    )


# --- verdict ---------------------------------------------------------------
def compute_verdict(
    test: Stats,
    baseline: Stats,
    has_closing_line: bool,
    min_n: int = 150,
) -> str:
    """Compute the per-sport verdict string from held-out numbers.

    Doctrine gate (non-negotiable): a sport earns LIVE ALERTS only if the
    held-out incremental CLV vs the closing line exceeds 2 SE AND ROI >= 0.
    Tennis has NO closing line, so it can structurally only reach
    visibility-only (or reject on negative/thin data).
    """
    if test.n < min_n:
        return f"reject (held-out n={test.n} < {min_n}; too thin to judge)"

    if not has_closing_line:
        # No closing line -> the CLV-vs-close gate is unevaluable; cannot
        # promote to alerts under doctrine no matter how good ROI looks.
        roi_pos = test.roi > 0 and test.roi_lo > 0  # ROI CI strictly above 0
        ev_note = (
            f"held-out ROI {test.roi * 100:+.2f}% "
            f"[{test.roi_lo * 100:+.2f}%, {test.roi_hi * 100:+.2f}%]"
        )
        if roi_pos:
            return (
                f"visibility-only (EV-positive on held-out data: {ev_note}, but "
                "NO closing line exists in the source -> incremental CLV vs close "
                "is UNDEFINED, so the >2 SE CLV gate cannot be cleared; "
                "UNVALIDATED for live alerts)"
            )
        return (
            f"visibility-only / UNVALIDATED (no closing line -> CLV gate "
            f"unevaluable; {ev_note} not conclusively > 0)"
        )

    # has_closing_line branch (kept for parity with football; unused by tennis):
    if (
        test.clv_close is not None
        and baseline.clv_close is not None
        and test.inc_clv_se is not None
    ):
        inc = test.clv_close - baseline.clv_close
        if inc - 2 * test.inc_clv_se > 0 and test.roi > 0:
            return f"alerts (incremental CLV {inc:+.4f} > 2 SE, ROI {test.roi * 100:+.2f}%)"
    return "visibility-only (closing line present but CLV gate not cleared)"


# --- I/O orchestration (network-touching, not imported by tests) -----------
def _load_year(tour: str, year: int) -> list[TennisRow]:  # pragma: no cover
    """Load one tour-year from the gitignored cache, downloading if absent."""
    import pandas as pd

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{tour}_{year}.xlsx"
    if not path.exists():
        import httpx

        url = _TOUR_URL[tour].format(y=year)
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        # log only the type + tour-year, never the URL (logging-hygiene rule)
        logger.info("downloaded %s %d (%d bytes)", tour, year, len(resp.content))

    df = pd.read_excel(path)
    rows: list[TennisRow] = []
    quarantined = 0
    for idx, r in df.iterrows():
        comment = str(r.get("Comment", "")).strip()
        completed = comment == "Completed"
        if not completed:
            quarantined += 1
        sharp = (_f(r.get(SHARP_COLS[0])), _f(r.get(SHARP_COLS[1])))
        best = (_f(r.get(MAX_COLS[0])), _f(r.get(MAX_COLS[1])))
        rows.append(
            TennisRow(
                match_id=f"{tour}-{year}-{idx}",
                sharp=sharp,
                best=best,
                winner_idx=0,  # source lists the winner first
                completed=completed,
            )
        )
    logger.info(
        "%s %d: %d matches (%d non-completed quarantined)",
        tour,
        year,
        len(rows),
        quarantined,
    )
    return rows


def _load(tours: list[str], years: list[int]) -> list[TennisRow]:  # pragma: no cover
    out: list[TennisRow] = []
    for tour in tours:
        for y in years:
            try:
                out.extend(_load_year(tour, y))
            except Exception as exc:  # noqa: BLE001 - log type only, never URL/body
                logger.warning("skip %s %d: %s", tour, y, type(exc).__name__)
    return out


def _fmt(stats: Stats, label: str) -> str:  # pragma: no cover - presentation
    if stats.n == 0:
        return f"{label:>14} | (no bets)"
    return (
        f"{label:>14} | n={stats.n:5d} | hit {stats.hit * 100:4.1f}% | "
        f"ROI {stats.roi * 100:+6.2f}% "
        f"[{stats.roi_lo * 100:+6.2f}%, {stats.roi_hi * 100:+6.2f}%] | "
        f"CLVvsClose {'n/a (no close)' if stats.clv_close is None else f'{stats.clv_close:+.4f}'}"
    )


def main() -> None:  # pragma: no cover - CLI orchestration
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tours", default="atp,wta")
    p.add_argument("--train-years", default="2019,2020,2021,2022,2023")
    p.add_argument("--test-years", default="2024,2025,2026")
    p.add_argument("--min-odds", type=float, default=1.0, help="odds floor for picks")
    p.add_argument("--n-boot", type=int, default=2000)
    p.add_argument(
        "--train-floor",
        type=int,
        default=1000,
        help="minimum TRAIN bet count for a combo to be eligible as the "
        "operating point. Guarantees an evaluable held-out fold and stops the "
        "sweep selecting a degenerate high-threshold bucket. Selection then "
        "maximizes the TRAIN ROI lower CI bound among eligible combos.",
    )
    args = p.parse_args()

    tours = [t.strip().lower() for t in args.tours.split(",") if t.strip()]
    train_years = [int(y) for y in args.train_years.split(",") if y.strip()]
    test_years = [int(y) for y in args.test_years.split(",") if y.strip()]

    print("\nTENNIS VALUE BACKTEST — sharp(Pinnacle) vs best-of-books, pre-match only")
    print(f"tours={tours} | TRAIN {train_years} (sweep) | TEST {test_years} (held out, one shot)")
    print("ONE bet per match; settle on real Winner/Loser (Completed only).")
    print("NOTE: tennis-data.co.uk has NO closing line -> CLV vs close is UNDEFINED;")
    print("      the doctrine >2 SE CLV gate CANNOT be cleared (visibility-only ceiling).\n")

    devig_methods = (
        DevigMethod.POWER,
        DevigMethod.SHIN,
        DevigMethod.MULTIPLICATIVE,
        DevigMethod.ODDS_RATIO,
        DevigMethod.LOGARITHMIC,
    )
    thresholds = (0.0, 0.01, 0.02, 0.03, 0.05)

    for tour in tours:
        print(f"\n{'=' * 78}\nTOUR: {tour.upper()}")
        train_rows = _load([tour], train_years)
        test_rows = _load([tour], test_years)
        print(f"train matches: {len(train_rows)} | test matches: {len(test_rows)}\n")
        if not train_rows or not test_rows:
            print(f"  {tour}: no usable data — verdict: reject (NO DATA)")
            continue

        print("TRAIN sweep (thr=0.000 row is the BASELINE null — bet everything):")
        sweep: list[tuple[DevigMethod, float, Stats]] = []
        for dm in devig_methods:
            for thr in thresholds:
                s = stats_from_bets(
                    bets_for(train_rows, thr, dm, args.min_odds), n_boot=args.n_boot
                )
                sweep.append((dm, thr, s))
                print(_fmt(s, f"{dm.value[:5]}/{thr:.3f}"))

        # OPERATING-POINT SELECTION (pre-registered; reads TRAIN only).
        #
        # Maximizing point ROI on TRAIN is a trap: ROI grows monotonically with
        # the threshold because fewer, higher-edge bets survive — so it always
        # selects the sparsest bucket, which then collapses the held-out fold
        # below the verdict floor (empirically train thr=0.05 -> ~45-73 test
        # bets). Two corrections, both honest:
        #   1. require a TRAIN-n floor so the held-out fold is guaranteed
        #      evaluable (train ~= 1.75x test overall here, so the default
        #      floor of 1000 train bets keeps test n in the high hundreds);
        #   2. among those, maximize the TRAIN ROI lower confidence bound
        #      (roi_lo), NOT the point ROI. The CI-lo penalizes thin, noisy
        #      buckets (wide CI -> low lo) and rewards thresholds that are both
        #      profitable AND well-sampled — i.e. the ones that survive
        #      out-of-sample. This selects on TRAIN bootstrap CIs; the held-out
        #      fold is still touched exactly once, below.
        viable = [(d, t, s) for d, t, s in sweep if s.n >= args.train_floor and t > 0.0]
        if not viable:
            print(
                f"\n  {tour}: no thr>0 combo clears the train-n floor "
                f"({args.train_floor}); too thin to choose an evaluable point — "
                "verdict: reject"
            )
            continue
        best_dm, best_thr, best_train = max(viable, key=lambda x: x[2].roi_lo)
        print(
            f"\nchosen on TRAIN (max ROI CI-lo, train n >= {args.train_floor}): "
            f"devig={best_dm.value} thr={best_thr} "
            f"(ROI {best_train.roi * 100:+.2f}% [CI-lo {best_train.roi_lo * 100:+.2f}%], "
            f"train n={best_train.n})"
        )

        print("\nHELD-OUT TEST (single shot, never tuned on):")
        baseline_test = stats_from_bets(
            bets_for(test_rows, 0.0, best_dm, args.min_odds), n_boot=args.n_boot
        )
        test = stats_from_bets(
            bets_for(test_rows, best_thr, best_dm, args.min_odds), n_boot=args.n_boot
        )
        print(_fmt(baseline_test, "0.000 (null)"))
        print(_fmt(test, f"{best_thr:.3f}"))

        verdict = compute_verdict(test, baseline_test, has_closing_line=False)
        print(f"\nVERDICT ({tour}, computed): {verdict}")

    print(
        "\nCaveats: Max line assumes shopping every book at pre-match snapshot; soft books "
        "limit winners. No closing line => no CLV gate => tennis stays visibility-only "
        "regardless of ROI. This system does not place bets."
    )


if __name__ == "__main__":
    main()
