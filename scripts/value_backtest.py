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


def bets_for(rows: list[dict], thr: float) -> list[VBet]:
    """One bet per match: the highest-edge selection clearing the threshold."""
    out: list[VBet] = []
    for r in rows:
        ps = [_f(r.get("PSH")), _f(r.get("PSD")), _f(r.get("PSA"))]
        mx = [_f(r.get("MaxH")), _f(r.get("MaxD")), _f(r.get("MaxA"))]
        psc = [_f(r.get("PSCH")), _f(r.get("PSCD")), _f(r.get("PSCA"))]
        mxc = [_f(r.get("MaxCH")), _f(r.get("MaxCD")), _f(r.get("MaxCA"))]
        ftr = r.get("FTR")
        if None in ps or None in mx or ftr not in ("H", "D", "A"):
            continue
        sharp = devig(ps, method=DevigMethod.POWER)  # type: ignore[arg-type]
        close_p = (
            devig(psc, method=DevigMethod.POWER)  # type: ignore[arg-type]
            if None not in psc
            else None
        )
        close_m = (
            devig(mxc, method=DevigMethod.POWER)  # type: ignore[arg-type]
            if None not in mxc
            else None
        )
        best: tuple[float, int] | None = None  # (edge, idx)
        for i in range(3):
            edge = sharp[i] - 1.0 / mx[i]  # type: ignore[operator]
            if edge >= thr and (best is None or edge > best[0]):
                best = (edge, i)
        if best is None:
            continue
        edge, i = best
        sel = ("H", "D", "A")[i]
        out.append(
            VBet(
                won=(ftr == sel),
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
    p.add_argument("--leagues", default="E0,E1,SP1,D1,I1,F1")
    p.add_argument("--train-seasons", default="2122,2223,2324")
    p.add_argument("--test-seasons", default="2425,2526")
    args = p.parse_args()
    leagues = [x.strip() for x in args.leagues.split(",") if x.strip()]
    train_s = [x.strip() for x in args.train_seasons.split(",") if x.strip()]
    test_s = [x.strip() for x in args.test_seasons.split(",") if x.strip()]

    print(f"\nVALUE BACKTEST v2 — leagues {leagues}")
    print(f"TRAIN seasons {train_s} (threshold sweep) | TEST seasons {test_s} (held out)")
    print("One bet per match; CLV vs Pinnacle close AND vs Max-of-books close.\n")

    train_rows = await load(leagues, train_s)
    test_rows = await load(leagues, test_s)
    print(f"train: {len(train_rows)} matches | test: {len(test_rows)} matches\n")

    print("TRAIN sweep (thr=0.000 is the BASELINE null — bet everything):")
    baseline_train = Stats.from_bets(bets_for(train_rows, 0.0))
    print(_fmt(baseline_train, "0.000"))
    sweep: list[tuple[float, Stats]] = []
    for thr in (0.005, 0.010, 0.015, 0.020, 0.030):
        s = Stats.from_bets(bets_for(train_rows, thr))
        sweep.append((thr, s))
        print(_fmt(s, f"{thr:.3f}", baseline_train))

    # choose the train threshold maximizing ROI with a workable sample
    viable = [(t, s) for t, s in sweep if s.n >= 100]
    if not viable:
        print("\nNo viable threshold (n>=100) on train — verdict: NO PROVEN EDGE.")
        return
    best_thr, best_train = max(viable, key=lambda ts: ts[1].roi)
    print(f"\nchosen on TRAIN: thr={best_thr} (ROI {best_train.roi * 100:+.2f}%, n={best_train.n})")

    print("\nHELD-OUT TEST evaluation:")
    baseline_test = Stats.from_bets(bets_for(test_rows, 0.0))
    test = Stats.from_bets(bets_for(test_rows, best_thr))
    print(_fmt(baseline_test, "0.000"))
    print(_fmt(test, f"{best_thr:.3f}", baseline_test))

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
        "books limit winners; ~120 bets/yr across 6 leagues at this threshold."
    )
    print("Manual review required. This system does not place bets.")


if __name__ == "__main__":
    asyncio.run(main())
