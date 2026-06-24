"""NBA total-line CLV / value backtest on free SBR data — honest-anchor edition.

DATA REALITY (inspected before building; see tests/test_nba_backtest.py):
    /tmp/sbr/data/nba_archive_10Y.json — SportsbookReview consensus, 2011-2021,
    13,903 games. Each row has final scores and:
      * home_close_ml / away_close_ml  -> CLOSING moneyline (American). The ONLY
        priced 2-way market in the file — and it is CLOSING-ONLY. There is no
        opening moneyline anywhere, so the football recipe "devig the OPENING
        moneyline, bet it, CLV vs the CLOSING moneyline" is IMPOSSIBLE here.
      * open_over_under / close_over_under -> OPENING and CLOSING total lines.
        These DO move open->close, but they are bare line numbers with NO price.

So the only walk-forward-clean, leakage-free test the data supports is the
TOTAL market as a LINE-VALUE study:

    Signal at the OPENING total line. Fair Over prob = a logistic line->cover
    map fit on TRAIN games ONLY (older seasons). Bet OVER/UNDER at assumed
    -110 juice when |edge| >= threshold, where edge = model_prob - implied(-110).
    Settle on the real final score. CLV = clv_log(fill@-110, devig(closing)),
    where the CLOSING fair Over prob is the SAME train-fit map applied to the
    CLOSING line. Positive CLV = the opening-line bet beat where the close lands.

HONESTY (do not overclaim):
    * The anchor is the SBR consensus / Vegas close, NOT Pinnacle. It is a SOFT
      sharp. Beating it is line value, not proven edge against a sharp book.
    * The -110 price is ASSUMED (the file stores no total price). ROI is only as
      real as that assumption; we also report ROI at a -105 reduced-juice book.
    * The line->prob map is the model; it cannot beat the market in expectation
      if the market is efficient. A null result here is the expected, honest
      outcome and is reported as such (computed verdict, never hardcoded).

Train/test split is a single shot: older seasons fit the map AND sweep the
threshold; the most-recent seasons are held out and evaluated once.

    .venv/bin/python scripts/nba_backtest.py
    .venv/bin/python scripts/nba_backtest.py --data /tmp/sbr/data/nba_archive_10Y.json \
        --train-seasons 2011,2012,2013,2014,2015,2016,2017,2018 \
        --test-seasons 2019,2020,2021

Decision-support only — nothing here places bets.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

from app.backtesting.clv import clv_log
from app.probabilities.devig import DevigMethod, devig

# Assumed total price both sides (SBR stores no total juice). -110 is the US
# standard; -105 is a reduced-juice shop, reported as a sensitivity.
PRICE_STD = -110
PRICE_REDUCED = -105

# Plausible-NBA gates: drop scrape garbage (file has totals like 1955.5 and 0.0,
# spreads like 242.5). A regulation+OT NBA game total is comfortably inside this.
TOTAL_MIN = 150.0
TOTAL_MAX = 280.0
FINAL_MIN = 50  # a single team's points; blowouts and lulls still clear this
FINAL_MAX = 200


def american_to_decimal(odds: object) -> float | None:
    """American -> decimal odds. |odds| < 100 is not real American odds -> None."""
    try:
        a = float(odds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if a >= 100.0:
        return 1.0 + a / 100.0
    if a <= -100.0:
        return 1.0 + 100.0 / (-a)
    return None


def _to_int(x: object) -> int | None:
    try:
        return int(round(float(str(x))))
    except (TypeError, ValueError):
        return None


def total_result(home_final: object, away_final: object, line: float) -> str | None:
    """Settle a total on the real final score. 'over' / 'under' / None (push or
    garbage). Never returns a side it cannot justify."""
    h = _to_int(home_final)
    a = _to_int(away_final)
    if h is None or a is None:
        return None
    if not (FINAL_MIN <= h <= FINAL_MAX and FINAL_MIN <= a <= FINAL_MAX):
        return None
    if not (TOTAL_MIN <= line <= TOTAL_MAX):
        return None
    total = h + a
    if total > line:
        return "over"
    if total < line:
        return "under"
    return None  # exact push


@dataclass(frozen=True)
class LineOverModel:
    """Logistic P(over | total_line) = sigmoid(a + b*line). Fit on TRAIN games
    ONLY. b is expected negative (a higher line is harder to go over)."""

    a: float
    b: float

    @classmethod
    def fit(
        cls,
        samples: list[tuple[float, str]],
        iters: int = 2000,
        lr: float = 0.01,
    ) -> LineOverModel:
        """Gradient-descent logistic on centered line. samples: (line, 'over'/'under')."""
        xs = [ln for ln, _ in samples]
        ys = [1.0 if r == "over" else 0.0 for _, r in samples]
        n = len(xs)
        if n == 0:
            return cls(0.0, 0.0)
        mean = sum(xs) / n
        std = (sum((x - mean) ** 2 for x in xs) / n) ** 0.5 or 1.0
        zs = [(x - mean) / std for x in xs]
        a, b = 0.0, 0.0  # on standardized line
        for _ in range(iters):
            ga = gb = 0.0
            for z, y in zip(zs, ys, strict=True):
                p = 1.0 / (1.0 + math.exp(-(a + b * z)))
                d = p - y
                ga += d
                gb += d * z
            a -= lr * ga / n
            b -= lr * gb / n
        # de-standardize: a + b*z = a + b*(line-mean)/std = (a - b*mean/std) + (b/std)*line
        return cls(a - b * mean / std, b / std)

    def over_prob(self, line: float) -> float:
        p = 1.0 / (1.0 + math.exp(-(self.a + self.b * line)))
        return min(max(p, 1e-6), 1.0 - 1e-6)


@dataclass(frozen=True)
class NBet:
    won: bool
    odds: float  # decimal fill (assumed -110)
    edge: float  # model prob - implied(-110), for the chosen side
    clv: float | None  # clv_log(fill, devig(closing)[chosen side])


@dataclass
class Stats:
    n: int
    hit: float
    roi: float
    roi_reduced: float
    clv: float | None
    clv_se: float | None
    beat_close: float | None
    clv_ci_lo: float | None
    clv_ci_hi: float | None
    roi_ci_lo: float | None
    roi_ci_hi: float | None

    @classmethod
    def from_bets(cls, bets: list[NBet], n_boot: int = 2000, seed: int = 7) -> Stats:
        n = len(bets)
        if n == 0:
            return cls(0, 0.0, 0.0, 0.0, None, None, None, None, None, None, None)
        dec_red = american_to_decimal(PRICE_REDUCED) or 1.0
        profit = sum((b.odds - 1.0) if b.won else -1.0 for b in bets)
        profit_red = sum((dec_red - 1.0) if b.won else -1.0 for b in bets)
        cs = [b.clv for b in bets if b.clv is not None]

        m_clv = se_clv = None
        if cs:
            m_clv = sum(cs) / len(cs)
            se_clv = math.sqrt(sum((c - m_clv) ** 2 for c in cs) / len(cs)) / math.sqrt(len(cs))

        # bootstrap CIs (the skill: no averaging ROI/CLV without a bootstrap CI)
        rng = random.Random(seed)
        roi_samples: list[float] = []
        clv_samples: list[float] = []
        for _ in range(n_boot):
            pick = [bets[rng.randrange(n)] for _ in range(n)]
            roi_samples.append(sum((b.odds - 1.0) if b.won else -1.0 for b in pick) / n)
            pc = [b.clv for b in pick if b.clv is not None]
            if pc:
                clv_samples.append(sum(pc) / len(pc))
        roi_samples.sort()
        clv_samples.sort()

        def pct(xs: list[float], q: float) -> float | None:
            if not xs:
                return None
            return xs[min(len(xs) - 1, max(0, int(q * len(xs))))]

        return cls(
            n=n,
            hit=sum(1 for b in bets if b.won) / n,
            roi=profit / n,
            roi_reduced=profit_red / n,
            clv=m_clv,
            clv_se=se_clv,
            beat_close=(sum(1 for c in cs if c > 0) / len(cs)) if cs else None,
            clv_ci_lo=pct(clv_samples, 0.025),
            clv_ci_hi=pct(clv_samples, 0.975),
            roi_ci_lo=pct(roi_samples, 0.025),
            roi_ci_hi=pct(roi_samples, 0.975),
        )


def bets_for(
    rows: list[dict],
    thr: float,
    model: LineOverModel,
    devig_method: DevigMethod = DevigMethod.MULTIPLICATIVE,
) -> list[NBet]:
    """One total bet per game. DECISION uses ONLY the opening line + train-fit
    model; the closing line feeds the CLV label and nothing else."""
    fill = american_to_decimal(PRICE_STD)
    assert fill is not None
    # devig the assumed -110/-110 total book -> fair implied per side (0.5/0.5).
    implied_over, implied_under = devig((fill, fill), method=devig_method)
    out: list[NBet] = []
    for r in rows:
        try:
            open_line = float(r.get("open_over_under"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if not (TOTAL_MIN <= open_line <= TOTAL_MAX):
            continue  # garbage opening line -> no signal
        res = total_result(r.get("home_final"), r.get("away_final"), open_line)
        if res is None:
            continue  # push or unsettleable -> drop, never settle wrong
        p_over = model.over_prob(open_line)
        edge_over = p_over - implied_over
        edge_under = (1.0 - p_over) - implied_under
        if edge_over >= edge_under:
            side, edge, won_side = "over", edge_over, res == "over"
        else:
            side, edge, won_side = "under", edge_under, res == "under"
        if edge < thr:
            continue
        # CLV label: closing line through the SAME train-fit map (soft anchor).
        clv: float | None = None
        try:
            close_line = float(r.get("close_over_under"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            close_line = float("nan")
        if TOTAL_MIN <= close_line <= TOTAL_MAX:
            close_p_over = model.over_prob(close_line)
            close_fair = close_p_over if side == "over" else (1.0 - close_p_over)
            clv = clv_log(fill, close_fair)
        out.append(NBet(won=won_side, odds=fill, edge=edge, clv=clv))
    return out


def _train_samples(rows: list[dict]) -> list[tuple[float, str]]:
    """(opening line, realized over/under) pairs for fitting — TRAIN only."""
    s: list[tuple[float, str]] = []
    for r in rows:
        try:
            line = float(r.get("open_over_under"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        res = total_result(r.get("home_final"), r.get("away_final"), line)
        if res is not None and TOTAL_MIN <= line <= TOTAL_MAX:
            s.append((line, res))
    return s


def _fmt(stats: Stats, label: str, baseline: Stats | None = None) -> str:
    if stats.n == 0:
        return f"{label:>10} | (no bets)"
    clv = f"{stats.clv:+.4f}+/-{2 * (stats.clv_se or 0):.4f}" if stats.clv is not None else "n/a"
    ci = (
        f" [boot {stats.clv_ci_lo:+.4f},{stats.clv_ci_hi:+.4f}]"
        if stats.clv_ci_lo is not None
        else ""
    )
    inc = ""
    if baseline and baseline.clv is not None and stats.clv is not None:
        inc = f" | incCLV {stats.clv - baseline.clv:+.4f}"
    return (
        f"{label:>10} | n={stats.n:5d} | hit {stats.hit * 100:4.1f}% | "
        f"ROI {stats.roi * 100:+6.2f}% (-105 {stats.roi_reduced * 100:+5.2f}%) | "
        f"CLV {clv}{ci}{inc}"
    )


def load(path: Path) -> list[dict]:
    with path.open() as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON list of games, got {type(data).__name__}")
    return data


def _season_of(r: dict) -> int | None:
    try:
        return int(r.get("season"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--data", default="/tmp/sbr/data/nba_archive_10Y.json")
    p.add_argument("--train-seasons", default="2011,2012,2013,2014,2015,2016,2017,2018")
    p.add_argument("--test-seasons", default="2019,2020,2021")
    args = p.parse_args()

    train_s = {int(x) for x in args.train_seasons.split(",") if x.strip()}
    test_s = {int(x) for x in args.test_seasons.split(",") if x.strip()}

    data = load(Path(args.data))
    train_rows = [r for r in data if _season_of(r) in train_s]
    test_rows = [r for r in data if _season_of(r) in test_s]

    print(f"\nNBA TOTAL-LINE BACKTEST — data {args.data}")
    print(
        f"TRAIN seasons {sorted(train_s)} ({len(train_rows)} games) | "
        f"TEST seasons {sorted(test_s)} ({len(test_rows)} games), held out, one shot"
    )
    print("Bet OPENING total @ assumed -110; settle on real score; CLV vs CLOSING line.")
    print(
        "ANCHOR IS THE SBR/VEGAS CONSENSUS CLOSE — a SOFT sharp, not Pinnacle. "
        "-110 price is ASSUMED.\n"
    )

    # Fit the line->over map on TRAIN games ONLY (no closing info, no test info).
    model = LineOverModel.fit(_train_samples(train_rows))
    print(
        f"line->P(over) map (TRAIN-fit): logit = {model.a:+.4f} {model.b:+.5f}*line "
        f"(b<0 expected: higher line, harder to go over)\n"
    )

    thresholds = (0.0, 0.005, 0.010, 0.015, 0.020, 0.030)
    print("TRAIN sweep (thr=0.000 = BASELINE null, bet every game):")
    base_train = Stats.from_bets(bets_for(train_rows, 0.0, model))
    print(_fmt(base_train, "0.000"))
    sweep: list[tuple[float, Stats]] = []
    for thr in thresholds[1:]:
        s = Stats.from_bets(bets_for(train_rows, thr, model))
        sweep.append((thr, s))
        print(_fmt(s, f"{thr:.3f}", base_train))

    viable = [(t, s) for t, s in sweep if s.n >= 150]
    if not viable:
        print("\nNo viable threshold (n>=150) on train — VERDICT: NO PROVEN EDGE.")
        return
    best_thr, best_train = max(viable, key=lambda x: x[1].roi)
    print(
        f"\nchosen on TRAIN: thr={best_thr:.3f} "
        f"(ROI {best_train.roi * 100:+.2f}%, n={best_train.n})"
    )

    print("\nHELD-OUT TEST (single shot, never tuned on):")
    base_test = Stats.from_bets(bets_for(test_rows, 0.0, model))
    test = Stats.from_bets(bets_for(test_rows, best_thr, model))
    print(_fmt(base_test, "0.000"))
    print(_fmt(test, f"{best_thr:.3f}", base_test))
    if test.beat_close is not None:
        print(f"           share of bets with CLV>0 (beat close): {test.beat_close * 100:.1f}%")

    # Computed verdict (never hardcoded): held-out incremental CLV over the
    # bet-everything baseline must clear 2*SE AND ROI must be positive.
    verdict = "NO PROVEN EDGE on held-out data"
    if (
        test.n >= 50
        and test.clv is not None
        and base_test.clv is not None
        and test.clv_se is not None
    ):
        inc = test.clv - base_test.clv
        if inc - 2 * test.clv_se > 0 and test.roi > 0:
            verdict = (
                f"POSITIVE line value on held-out data: incremental CLV {inc:+.4f} "
                f"(>2SE), ROI {test.roi * 100:+.2f}% vs a SOFT consensus close"
            )
        elif test.roi > 0:
            verdict = (
                f"ROI positive ({test.roi * 100:+.2f}%) but incremental CLV {inc:+.4f} "
                "not conclusively above the bet-everything baseline"
            )
    print(f"\nVERDICT (computed): {verdict}")
    print(
        "\nLIMITATIONS: SBR consensus close is a SOFT anchor (not Pinnacle); the -110 "
        "total price is assumed (file stores no total juice); the line->prob map is the "
        "only model and cannot beat an efficient market in expectation. There is NO "
        "opening moneyline in this data, so a priced open-vs-close ML CLV is impossible."
    )
    print("Manual review required. This system does not place bets.")


if __name__ == "__main__":
    main()
