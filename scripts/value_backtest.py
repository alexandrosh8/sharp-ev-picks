"""Backtest the sharp-vs-soft VALUE strategy — review-corrected methodology.

Fair value = devig(Pinnacle pre-match). Bet the best available price (Max
across books) when it beats Pinnacle fair by >= threshold. No goals model.

Corrections from the 2026-06-10 deep review:
- ONE bet per match (highest-edge selection) — multiple mutually-exclusive
  bets per match are correlated and inflate the i.i.d. confidence interval.
- CLV is reported against TWO references: devig(Pinnacle close) AND
  devig(Max-of-books close). The second strips the mechanical
  "best-of-N-books premium" — beating it is the stricter test of selection
  skill. The thr=0 row is the explicit BASELINE null (bet everything).
- Thresholds are swept on TRAIN seasons only; the best train threshold is
  then evaluated once on held-out TEST seasons. No in-sample headline.
- The verdict is COMPUTED from the held-out numbers, never hardcoded.

    uv run python scripts/value_backtest.py
    uv run python scripts/value_backtest.py --train-seasons 2122,2223,2324 --test-seasons 2425,2526

Decision-support only — nothing here places bets.
"""

import argparse
import asyncio
import csv
import io
import math
from dataclasses import dataclass

import httpx

from app.backtesting.clv import clv_log
from app.ingestion.football_data import fetch_season_csv
from app.probabilities.devig import DevigMethod, devig


def _f(x: object) -> float | None:
    try:
        v = float(str(x))
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


@dataclass(frozen=True)
class VBet:
    won: bool
    odds: float
    edge: float
    clv_pinn: float | None  # vs devig(Pinnacle close)
    clv_max: float | None  # vs devig(Max-of-books close) — stricter


@dataclass
class Stats:
    n: int
    hit: float
    roi: float
    clv_pinn: float | None
    clv_pinn_se: float | None
    clv_max: float | None
    clv_max_se: float | None
    beat_pinn: float | None

    @classmethod
    def from_bets(cls, bets: list[VBet]) -> "Stats":
        n = len(bets)
        if n == 0:
            return cls(0, 0.0, 0.0, None, None, None, None, None)
        profit = sum((b.odds - 1.0) if b.won else -1.0 for b in bets)
        cp = [b.clv_pinn for b in bets if b.clv_pinn is not None]
        cm = [b.clv_max for b in bets if b.clv_max is not None]

        def mean_se(xs: list[float]) -> tuple[float | None, float | None]:
            if not xs:
                return None, None
            m = sum(xs) / len(xs)
            se = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs)) / math.sqrt(len(xs))
            return m, se

        mp, sp = mean_se(cp)
        mm, sm = mean_se(cm)
        return cls(
            n=n,
            hit=sum(1 for b in bets if b.won) / n,
            roi=profit / n,
            clv_pinn=mp,
            clv_pinn_se=sp,
            clv_max=mm,
            clv_max_se=sm,
            beat_pinn=(sum(1 for c in cp if c > 0) / len(cp)) if cp else None,
        )


# Market definitions: (pre-match Pinnacle cols, pre-match Max cols, closing
# Pinnacle cols, closing Max cols, settle fn over the CSV row per outcome idx)
def _won_1x2(r: dict, i: int) -> bool | None:
    ftr = r.get("FTR")
    if ftr not in ("H", "D", "A"):
        return None
    return ftr == ("H", "D", "A")[i]


def _won_ou25(r: dict, i: int) -> bool | None:
    try:
        goals = int(r["FTHG"]) + int(r["FTAG"])
    except (KeyError, TypeError, ValueError):
        return None
    return (goals >= 3) if i == 0 else (goals <= 2)


MARKETS = {
    "1x2": (
        ("PSH", "PSD", "PSA"),
        ("MaxH", "MaxD", "MaxA"),
        ("PSCH", "PSCD", "PSCA"),
        ("MaxCH", "MaxCD", "MaxCA"),
        _won_1x2,
    ),
    "ou25": (
        ("P>2.5", "P<2.5"),
        ("Max>2.5", "Max<2.5"),
        ("PC>2.5", "PC<2.5"),
        ("MaxC>2.5", "MaxC<2.5"),
        _won_ou25,
    ),
}


def bets_for(
    rows: list[dict],
    thr: float,
    devig_method: DevigMethod = DevigMethod.POWER,
    markets: tuple[str, ...] = ("1x2",),
    min_odds: float = 1.0,
    max_odds: float = 1000.0,
) -> list[VBet]:
    """One bet per (match, market): the highest-edge selection >= threshold."""
    out: list[VBet] = []
    for r in rows:
        for market in markets:
            ps_c, mx_c, psc_c, mxc_c, won_fn = MARKETS[market]
            ps = [_f(r.get(c)) for c in ps_c]
            mx = [_f(r.get(c)) for c in mx_c]
            psc = [_f(r.get(c)) for c in psc_c]
            mxc = [_f(r.get(c)) for c in mxc_c]
            if None in ps or None in mx or won_fn(r, 0) is None:
                continue
            sharp = devig(ps, method=devig_method)  # type: ignore[arg-type]
            close_p = devig(psc, method=devig_method) if None not in psc else None  # type: ignore[arg-type]
            close_m = devig(mxc, method=devig_method) if None not in mxc else None  # type: ignore[arg-type]
            best: tuple[float, int] | None = None  # (edge, idx)
            for i in range(len(ps)):
                if mx[i] < min_odds or mx[i] > max_odds:  # type: ignore[operator]
                    continue  # odds band: floor (no short prices) + ceiling (no longshots)
                edge = sharp[i] - 1.0 / mx[i]  # type: ignore[operator]
                if edge >= thr and (best is None or edge > best[0]):
                    best = (edge, i)
            if best is None:
                continue
            edge, i = best
            won = won_fn(r, i)
            if won is None:
                continue
            out.append(
                VBet(
                    won=won,
                    odds=mx[i],  # type: ignore[arg-type]
                    edge=edge,
                    clv_pinn=clv_log(mx[i], close_p[i]) if close_p else None,  # type: ignore[arg-type]
                    clv_max=clv_log(mx[i], close_m[i]) if close_m else None,  # type: ignore[arg-type]
                )
            )
    return out


async def load(leagues: list[str], seasons: list[str]) -> list[dict]:
    rows: list[dict] = []
    async with httpx.AsyncClient() as client:
        for lg in leagues:
            for s in seasons:
                for _ in range(4):
                    try:
                        txt = await fetch_season_csv(client, lg, s)
                        rows.extend(csv.DictReader(io.StringIO(txt)))
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(1.5)
                await asyncio.sleep(0.3)
    return rows


def _fmt(stats: Stats, label: str, baseline: Stats | None = None) -> str:
    if stats.n == 0:
        return f"{label:>9} | (no bets)"
    cp = (
        f"{stats.clv_pinn:+.4f}+/-{2 * (stats.clv_pinn_se or 0):.4f}"
        if stats.clv_pinn is not None
        else "n/a"
    )
    cm = (
        f"{stats.clv_max:+.4f}+/-{2 * (stats.clv_max_se or 0):.4f}"
        if stats.clv_max is not None
        else "n/a"
    )
    inc = ""
    if baseline and baseline.clv_pinn is not None and stats.clv_pinn is not None:
        inc = f" | incCLV {stats.clv_pinn - baseline.clv_pinn:+.4f}"
    return (
        f"{label:>9} | n={stats.n:5d} | hit {stats.hit * 100:4.1f}% | "
        f"ROI {stats.roi * 100:+6.2f}% | CLVpinn {cp} | CLVmax {cm}{inc}"
    )


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--leagues", default="E0,E1,E2,E3,SC0,D1,D2,I1,I2,SP1,SP2,F1,F2,N1,B1,P1,T1,G1")
    # 2019/20+ is the maximal window with full PSH+Max+PSC+MaxC coverage
    # (Max/Avg columns replaced BetBrain in 2019/20; Pinnacle closing too).
    p.add_argument("--train-seasons", default="1920,2021,2122,2223,2324")
    p.add_argument("--test-seasons", default="2425,2526")
    p.add_argument("--markets", default="1x2,ou25")
    p.add_argument("--min-odds", type=float, default=1.0, help="odds floor for candidate picks")
    p.add_argument(
        "--max-odds", type=float, default=1000.0, help="odds ceiling — kill longshots (P2)"
    )
    args = p.parse_args()
    leagues = [x.strip() for x in args.leagues.split(",") if x.strip()]
    train_s = [x.strip() for x in args.train_seasons.split(",") if x.strip()]
    test_s = [x.strip() for x in args.test_seasons.split(",") if x.strip()]
    markets = tuple(x.strip() for x in args.markets.split(",") if x.strip())
    min_odds = args.min_odds
    max_odds = args.max_odds

    print(f"\nVALUE BACKTEST — {len(leagues)} leagues, markets {markets}, min_odds {min_odds}")
    if min_odds < 1.6:
        print("NOTE: production v4 config was selected WITH --min-odds 1.6 (odds floor);")
        print("      at the default floor the sweep may pick a different (equivalent) devig.")
    print(f"TRAIN {train_s} (devig x threshold sweep) | TEST {test_s} (held out, one shot)")
    print("One bet per (match, market); CLV vs Pinnacle close AND Max-of-books close.\n")

    train_rows = await load(leagues, train_s)
    test_rows = await load(leagues, test_s)
    print(f"train: {len(train_rows)} matches | test: {len(test_rows)} matches\n")

    devig_methods = (
        DevigMethod.POWER,
        DevigMethod.SHIN,
        DevigMethod.MULTIPLICATIVE,
        DevigMethod.ODDS_RATIO,
        DevigMethod.LOGARITHMIC,
        DevigMethod.DIFFERENTIAL_MARGIN,
    )
    thresholds = (0.005, 0.010, 0.015, 0.020, 0.030)

    print("TRAIN sweep (thr=0.000 rows are the BASELINE null — bet everything):")
    sweep: list[tuple[DevigMethod, float, Stats]] = []
    baselines: dict[DevigMethod, Stats] = {}
    for dm in devig_methods:
        baselines[dm] = Stats.from_bets(bets_for(train_rows, 0.0, dm, markets, min_odds, max_odds))
        print(_fmt(baselines[dm], f"{dm.value[:5]}/0.000"))
        for thr in thresholds:
            s = Stats.from_bets(bets_for(train_rows, thr, dm, markets, min_odds, max_odds))
            sweep.append((dm, thr, s))
            print(_fmt(s, f"{dm.value[:5]}/{thr:.3f}", baselines[dm]))

    # choose the (devig, threshold) maximizing train ROI with a workable sample
    viable = [(d, t, s) for d, t, s in sweep if s.n >= 150]
    if not viable:
        print("\nNo viable combo (n>=150) on train — verdict: NO PROVEN EDGE.")
        return
    best_dm, best_thr, best_train = max(viable, key=lambda x: x[2].roi)
    print(
        f"\nchosen on TRAIN: devig={best_dm.value} thr={best_thr} "
        f"(ROI {best_train.roi * 100:+.2f}%, n={best_train.n})"
    )

    print("\nHELD-OUT TEST evaluation (single shot, never tuned on):")
    baseline_test = Stats.from_bets(bets_for(test_rows, 0.0, best_dm, markets, min_odds, max_odds))
    test = Stats.from_bets(bets_for(test_rows, best_thr, best_dm, markets, min_odds, max_odds))
    print(_fmt(baseline_test, "0.000"))
    print(_fmt(test, f"{best_thr:.3f}", baseline_test))
    for market in markets:
        m_stats = Stats.from_bets(
            bets_for(test_rows, best_thr, best_dm, (market,), min_odds, max_odds)
        )
        print(_fmt(m_stats, f"  {market}", baseline_test))

    # computed verdict (never hardcoded): selection skill on held-out data =
    # incremental CLV-vs-Pinnacle over the baseline, with 2*SE separation,
    # plus the stricter CLV-vs-Max sign check.
    verdict = "NO PROVEN EDGE on held-out data"
    if (
        test.n >= 50
        and test.clv_pinn is not None
        and baseline_test.clv_pinn is not None
        and test.clv_pinn_se is not None
    ):
        inc = test.clv_pinn - baseline_test.clv_pinn
        if inc - 2 * test.clv_pinn_se > 0 and test.roi > 0:
            if (test.clv_max or 0) > 0:
                strict = "and beats even the Max-of-books close"
            else:
                strict = (
                    "but does NOT beat the Max-of-books close "
                    "(edge is mostly the best-price premium)"
                )
            verdict = (
                f"POSITIVE selection skill on held-out data: incremental CLV {inc:+.4f} "
                f"(>2SE), ROI {test.roi * 100:+.2f}% — {strict}"
            )
        elif test.roi > 0:
            verdict = (
                f"ROI positive ({test.roi * 100:+.2f}%) but incremental CLV {inc:+.4f} "
                "not conclusively above the bet-everything baseline"
            )
    print(f"\nVERDICT (computed): {verdict}")
    print(
        "\nCaveats: Max line assumes line-shopping every book at snapshot time; soft "
        f"books limit winners; high thresholds are selective ({len(leagues)} leagues swept)."
    )
    print("Manual review required. This system does not place bets.")


if __name__ == "__main__":
    asyncio.run(main())
