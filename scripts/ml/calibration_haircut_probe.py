"""Tail-bias haircut probe — would recalibrating the devigged fair_prob help?

The honest, reproducible answer to the standing "calibrate the devigged fair
prob (tail-bias haircut)" question. It runs the pure, unit-tested walk-forward
recalibration-gain detector (app/backtesting/calibration.walk_forward_beta_gain)
over the maximal value dataset and prints a verdict: does ANY leakage-free,
fit-on-past beta recalibration of fair_prob beat the identity out-of-sample?

Finding (2026-06-24, see docs/research/calibration-haircut-decision-2026-06-24.md):
NO. On 210k unbiased full-pool selections the devigged Pinnacle fair prob is
already calibrated (mean_pred == base_rate to 5 dp); the fitted beta converges
to the identity (slope ~1, intercept ~0) and the pooled out-of-sample log-loss
gain is ~+0.002% — noise. A haircut would degrade log-loss and demote genuine
+EV picks. This script is the standing re-check: rerun it as live data
accumulates; if the verdict ever flips to WARRANTED, revisit the haircut.

Read-only analysis of the backtest dataset. Nothing here places bets. The math
is the pure, unit-tested module; the parquet read happens only in main() — this
script's composition root. Run (parquet engine lives in the `ml` extra):

    uv run --extra ml python scripts/ml/calibration_haircut_probe.py
    uv run --extra ml python scripts/ml/calibration_haircut_probe.py --bet-set --market 1x2
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.backtesting.calibration import (  # noqa: E402
    CalibrationObservation,
    walk_forward_beta_gain,
)

_FULL_POOL = "data/ml/value_pool_full.parquet"  # every selection — unbiased
_BET_SET = "data/ml/value_candidates_v3.parquet"  # argmax-edge picks — selection-biased


def _load_periods(path: Path, *, market: str | None, bet_set: bool):
    import pandas as pd  # local: pandas/pyarrow arrive via the `ml` extra

    df = pd.read_parquet(path)
    if bet_set and "is_argmax_edge" in df.columns:
        df = df[df["is_argmax_edge"] == True]  # noqa: E712 (pandas mask, not `is`)
    if market:
        df = df[df["market"] == market]
    df = df.dropna(subset=["fair_prob", "won", "season"]).copy()
    df = df[(df["fair_prob"] > 0.0) & (df["fair_prob"] < 1.0)]

    periods: list[tuple[str, list[CalibrationObservation]]] = []
    for season in sorted(df["season"].unique()):
        sub = df[df["season"] == season]
        obs = [
            CalibrationObservation(fair_prob=float(p), won=bool(w))
            for p, w in zip(sub["fair_prob"], sub["won"], strict=True)
        ]
        periods.append((str(season), obs))
    return periods


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=None, help="parquet path (default: full pool)")
    ap.add_argument(
        "--bet-set", action="store_true", help="argmax-edge bet set instead of full pool"
    )
    ap.add_argument("--market", default=None, help="filter to one market (e.g. 1x2, ou25)")
    ap.add_argument("--min-train", type=int, default=2000)
    ap.add_argument("--min-test", type=int, default=200)
    ap.add_argument("--min-warrant-rel-pct", type=float, default=0.5)
    args = ap.parse_args()

    path = args.data or Path(_BET_SET if args.bet_set else _FULL_POOL)
    if not path.exists():
        print(f"dataset not found: {path}", file=sys.stderr)
        return 2

    periods = _load_periods(path, market=args.market, bet_set=args.bet_set)
    report = walk_forward_beta_gain(
        periods,
        min_train=args.min_train,
        min_test=args.min_test,
        min_warrant_rel_pct=args.min_warrant_rel_pct,
    )

    scope = "BET SET (argmax-edge)" if args.bet_set else "FULL POOL (unbiased)"
    mkt = args.market or "all"
    print(f"\nWalk-forward recalibration-gain probe — {scope}, market={mkt}, {path.name}")
    print("=" * 78)
    if report.insufficient:
        print(
            f"INSUFFICIENT: no eligible walk-forward fold "
            f"(need >= {args.min_train} train / {args.min_test} test). n_total={report.n_total}"
        )
        return 0

    print(
        f"{'period':>8} {'n':>8} {'slope_a':>9} {'intcpt_b':>9} "
        f"{'identity_ll':>12} {'recal_ll':>10} {'oos_gain':>10}"
    )
    for f in report.folds:
        print(
            f"{str(f.period):>8} {f.n:>8} {f.slope:>9.4f} {f.intercept:>9.4f} "
            f"{f.identity_log_loss:>12.6f} {f.recal_log_loss:>10.6f} "
            f"{f.oos_log_loss_gain:>+10.6f}"
        )
    print("-" * 78)
    print(
        f"POOLED ({report.n_folds} folds, n={report.n_total}): "
        f"identity_ll={report.pooled_identity_log_loss:.6f} "
        f"recal_ll={report.pooled_recal_log_loss:.6f}"
    )
    print(
        f"  out-of-sample log-loss gain = {report.pooled_oos_gain:+.6f} "
        f"({report.pooled_rel_gain_pct:+.4f}%)"
    )
    verdict = (
        "WARRANTED — revisit the haircut"
        if report.warrants_recalibration
        else ("NOT WARRANTED — fair_prob already calibrated; no haircut")
    )
    print(f"  VERDICT: recalibration {verdict}")
    print(f"  (threshold: pooled OOS gain >= {args.min_warrant_rel_pct}% AND every fold positive)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
