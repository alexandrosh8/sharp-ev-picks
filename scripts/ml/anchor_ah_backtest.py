"""Track A/B backtest — consensus-anchor validation + Asian-handicap one-shot.

Consumes data/ml/value_candidates_v3.parquet (built by
``build_value_dataset.py --anchor-consensus --ah``). Two questions, one
protocol (the established one: ONE bet per (match, market) = argmax edge,
odds floor 1.6 — the deployed premium doctrine — dual CLV references
devig(Pinnacle close) AND devig(Max close), thr=0 pool-null row, bootstrap
CIs clustered by MATCH, seed 20260612, B=2000):

TRACK A (consensus anchor, app/edge/value.py fallback):
    Does selection anchored on the non-Pinnacle soft-book MEDIAN show
    positive incremental CLV vs its own bet-everything null on TRAIN
    seasons (<= 2324) — and how does it compare to Pinnacle-anchored
    selection on the SAME matches? TRAIN-ONLY by construction: the script
    drops 2425+2526 for every Track A computation. There is NO legitimate
    fresh domain for a Track A one-shot: the football-data "new leagues"
    feed (BRA/ARG/...) carries CLOSING odds only (header verified
    2026-06-12: every odds column is PSCH/MaxCH/AvgCH/B365CH-class), so no
    signal-time price exists and the protocol is impossible there for ANY
    anchor. The Track A binding verdict is therefore live shadow CLV
    (picks.anchor_type stratification) + the fresh 2627 season.

TRACK B (Asian handicap — the never-consulted market):
    Train sweep on <= 2324, then THE pre-registered one-shot on 2425+2526.
    LEGITIMATE: no prior backtest, training run, or threshold choice in
    this project ever consulted the AH market in any season (spent-holdout
    ledger covers 1x2+ou25 only). Running --oneshot-ah CONSUMES the AH
    2425+2526 domain permanently — the script writes a consumption marker
    and refuses to run twice.

AH PRE-REGISTRATION (frozen here BEFORE the test slice is ever read):
    selection_rule : one bet per match — argmax edge among half-line AH
                     candidates with best_price >= 1.6 and edge >= thr*
    thr_rule       : thr* = argmax train ROI over THRESHOLDS subject to
                     train n >= 150 and train (incCLV_max - 2*SE) > 0;
                     if no threshold qualifies, the one-shot is CANCELLED
                     unconsumed (no look at 2425+2526)
    pass_rule      : PASS iff test n_labeled >= 100 and test
                     (incCLV_max - 2*SE) > 0 vs the test thr=0 pool null
                     and test ROI > 0; n_labeled < 100 -> UNDERPOWERED
                     (defer to live + 2627); otherwise FAIL
    power_gate     : AMENDMENT 2026-06-12 + EXECUTION RECORD (honest log).
                     A row-count-only power gate was added intending to
                     cancel a guaranteed-underpowered look, but its first
                     implementation compared the TOTAL pool row count (140)
                     to the floor (100) instead of an upper bound on
                     labeled SELECTED bets — the gate passed and the
                     one-shot EXECUTED on 2026-06-12, CONSUMING the AH
                     2425+2526 domain. Outcome (recorded in
                     data/ml/AH_ONESHOT_CONSUMED.json): thr*=0.015, n=27,
                     n_labeled=10, ROI -10.96% [boot CI -48.1%, +28.1%],
                     incCLV_max +0.0330 [CI -0.0019, +0.0687],
                     verdict UNDERPOWERED by the frozen pass_rule. AH does
                     NOT meet the premium bar on this look; the binding AH
                     verdict moves to live shadow CLV + the (still
                     unconsumed) 2627 season ALONE. The gate below is now
                     corrected for the record; the consumption marker is
                     what actually enforces single-look from here on.
    labels         : CLV only on rows whose closing line AHCh equals the
                     pre-match line AHh (a moved close prices a different
                     bet); ROI/hit settle on every row (half lines cannot
                     push)

Run:
    uv run python scripts/ml/anchor_ah_backtest.py                # train-only
    uv run python scripts/ml/anchor_ah_backtest.py --oneshot-ah   # + THE one-shot

Decision-support only — nothing here places bets.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DEFAULT = REPO_ROOT / "data" / "ml" / "value_candidates_v3.parquet"
CONSUMPTION_MARKER = REPO_ROOT / "data" / "ml" / "AH_ONESHOT_CONSUMED.json"

SEED = 20260612
B_BOOT = 2000
MIN_ODDS = 1.6  # deployed premium doctrine (odds >= 1.6)
THRESHOLDS = (0.005, 0.010, 0.015, 0.020, 0.030)
POOL_FLOOR = 0.005  # builder min_edge: the thr=0 "null" is really edge>=0.005
TRAIN_MAXAVG = ("1920", "2021", "2122", "2223", "2324")
TEST_SEASONS = ("2425", "2526")  # AH one-shot ONLY — never read elsewhere

# Frozen pre-registration constants (mirror the docstring; the one-shot
# reads only these so a silent edit cannot reinterpret the criterion).
AH_TRAIN_MIN_N = 150
AH_TEST_MIN_N_LABELED = 100


@dataclass(frozen=True)
class Stats:
    n: int
    n_labeled_max: int
    hit: float
    roi: float
    roi_lo: float
    roi_hi: float
    clv_pinn: float | None
    clv_max: float | None
    inc_clv_pinn: float | None
    inc_clv_max: float | None
    inc_clv_max_se: float | None
    inc_clv_max_lo: float | None
    inc_clv_max_hi: float | None


def _match_key(df: pd.DataFrame) -> pd.Series:
    return (
        df["league"].astype(str)
        + "|"
        + df["season"].astype(str)
        + "|"
        + df["match_date"].astype(str)
        + "|"
        + df["home_team"].astype(str)
        + "|"
        + df["away_team"].astype(str)
    )


def one_bet_per_match_market(pool: pd.DataFrame, thr: float) -> pd.DataFrame:
    """The established selection rule: argmax edge per (match, market) among
    candidates with best_price >= MIN_ODDS and edge >= thr."""
    sel = pool[(pool["best_price"] >= MIN_ODDS) & (pool["edge"] >= thr)]
    if sel.empty:
        return sel
    idx = sel.groupby([sel["mk"], sel["market"]], observed=True)["edge"].idxmax()
    return sel.loc[idx.to_numpy()]


def _boot_means(
    bets: pd.DataFrame, null_bets: pd.DataFrame, rng: np.random.Generator
) -> tuple[tuple[float, float], tuple[float, float, float] | None]:
    """Match-clustered bootstrap: (roi_lo, roi_hi) for the selected bets and
    (se, lo, hi) for incCLV_max = mean(sel clv_max) - mean(null clv_max).
    Resamples MATCHES (rows of one match are correlated, never i.i.d.)."""
    matches = null_bets["mk"].unique()
    m_index = pd.Index(matches)

    def per_match(frame: pd.DataFrame, col: str) -> tuple[np.ndarray, np.ndarray]:
        sub = frame.dropna(subset=[col])
        g = sub.groupby("mk", observed=True)[col].agg(["sum", "count"])
        g = g.reindex(m_index, fill_value=0.0)
        return g["sum"].to_numpy(float), g["count"].to_numpy(float)

    profit_sum, profit_cnt = per_match(bets, "profit_units")
    inc_available = bool(bets["clv_max"].notna().any() and null_bets["clv_max"].notna().any())
    sel_sum = sel_cnt = nul_sum = nul_cnt = np.empty(0)
    if inc_available:
        sel_sum, sel_cnt = per_match(bets, "clv_max")
        nul_sum, nul_cnt = per_match(null_bets, "clv_max")
    rois = np.empty(B_BOOT)
    incs = np.full(B_BOOT, np.nan) if inc_available else None
    m = len(matches)
    for b in range(B_BOOT):
        take = rng.integers(0, m, size=m)
        pc = profit_cnt[take].sum()
        rois[b] = profit_sum[take].sum() / pc if pc else np.nan
        if incs is not None:
            sc, nc = sel_cnt[take].sum(), nul_cnt[take].sum()
            if sc and nc:
                incs[b] = sel_sum[take].sum() / sc - nul_sum[take].sum() / nc
    roi_ci = (float(np.nanpercentile(rois, 2.5)), float(np.nanpercentile(rois, 97.5)))
    inc_stats = None
    if incs is not None and np.isfinite(incs).any():
        inc_stats = (
            float(np.nanstd(incs)),
            float(np.nanpercentile(incs, 2.5)),
            float(np.nanpercentile(incs, 97.5)),
        )
    return roi_ci, inc_stats


def stats_for(bets: pd.DataFrame, null_bets: pd.DataFrame, rng: np.random.Generator) -> Stats:
    n = len(bets)
    if n == 0:
        return Stats(0, 0, 0.0, 0.0, 0.0, 0.0, None, None, None, None, None, None, None)
    roi = float(bets["profit_units"].mean())
    (roi_lo, roi_hi), inc_boot = _boot_means(bets, null_bets, rng)
    clv_pinn = float(bets["clv_pinn"].mean()) if bets["clv_pinn"].notna().any() else None
    clv_max = float(bets["clv_max"].mean()) if bets["clv_max"].notna().any() else None
    null_pinn = float(null_bets["clv_pinn"].mean()) if null_bets["clv_pinn"].notna().any() else None
    null_max = float(null_bets["clv_max"].mean()) if null_bets["clv_max"].notna().any() else None
    inc_p = clv_pinn - null_pinn if clv_pinn is not None and null_pinn is not None else None
    inc_m = clv_max - null_max if clv_max is not None and null_max is not None else None
    se = lo = hi = None
    if inc_boot is not None and inc_m is not None:
        se, lo, hi = inc_boot
    return Stats(
        n=n,
        n_labeled_max=int(bets["clv_max"].notna().sum()),
        hit=float(bets["won"].mean()),
        roi=roi,
        roi_lo=roi_lo,
        roi_hi=roi_hi,
        clv_pinn=clv_pinn,
        clv_max=clv_max,
        inc_clv_pinn=inc_p,
        inc_clv_max=inc_m,
        inc_clv_max_se=se,
        inc_clv_max_lo=lo,
        inc_clv_max_hi=hi,
    )


def _fmt(label: str, s: Stats) -> str:
    if s.n == 0:
        return f"{label:>10} | (no bets)"
    cp = f"{s.clv_pinn:+.4f}" if s.clv_pinn is not None else "   n/a "
    cm = f"{s.clv_max:+.4f}" if s.clv_max is not None else "   n/a "
    inc = (
        f"incCLVmax {s.inc_clv_max:+.4f} [CI {s.inc_clv_max_lo:+.4f},{s.inc_clv_max_hi:+.4f}]"
        if s.inc_clv_max is not None and s.inc_clv_max_lo is not None
        else "incCLVmax n/a"
    )
    incp = f" incCLVpinn {s.inc_clv_pinn:+.4f}" if s.inc_clv_pinn is not None else ""
    return (
        f"{label:>10} | n={s.n:5d} (lblM {s.n_labeled_max:5d}) | hit {s.hit * 100:4.1f}% | "
        f"ROI {s.roi * 100:+6.2f}% [{s.roi_lo * 100:+.1f},{s.roi_hi * 100:+.1f}] | "
        f"CLVp {cp} CLVm {cm} | {inc}{incp}"
    )


def sweep(pool: pd.DataFrame, title: str, rng: np.random.Generator) -> dict[float, Stats]:
    """Threshold sweep with the thr=0 pool-null row first. Returns thr->Stats
    (the null is keyed 0.0; remember POOL_FLOOR: the pool starts at 0.005)."""
    print(f"\n{title}")
    print(
        f"(thr=0.000 row = pool null: every candidate, pool floor {POOL_FLOOR}, "
        f"odds >= {MIN_ODDS}, one bet per match-market; CIs = match-clustered bootstrap)"
    )
    null_bets = one_bet_per_match_market(pool, 0.0)
    out: dict[float, Stats] = {0.0: stats_for(null_bets, null_bets, rng)}
    print(_fmt("0.000", out[0.0]))
    for thr in THRESHOLDS:
        out[thr] = stats_for(one_bet_per_match_market(pool, thr), null_bets, rng)
        print(_fmt(f"{thr:.3f}", out[thr]))
    return out


def paired_anchor_comparison(train: pd.DataFrame, thr: float, rng: np.random.Generator) -> None:
    """Consensus vs Pinnacle selection on the SAME matches (1x2, maxavg).

    For each (match, market) where BOTH anchors selected a bet at `thr`,
    report the paired CLV difference (consensus bet minus pinnacle bet) with
    a bootstrap CI — the direct answer to "is the fallback anchor picking
    comparable bets where both anchors exist?"."""
    maxavg = train[(train["era"] == "maxavg") & (train["market"] == "1x2")]
    cons = one_bet_per_match_market(maxavg[maxavg["anchor_type"] == "consensus"], thr)
    pinn = one_bet_per_match_market(maxavg[maxavg["anchor_type"] == "pinnacle"], thr)
    merged = cons.merge(
        pinn, on=["mk", "market"], suffixes=("_cons", "_pinn"), how="outer", indicator=True
    )
    both = merged[merged["_merge"] == "both"]
    only_cons = int((merged["_merge"] == "left_only").sum())
    only_pinn = int((merged["_merge"] == "right_only").sum())
    print(f"\nTRACK A paired comparison at thr={thr} (1x2, maxavg train):")
    print(f"  both anchors bet: {len(both)} | consensus-only: {only_cons} | pinn-only: {only_pinn}")
    if both.empty:
        return
    same = float((both["selection_cons"] == both["selection_pinn"]).mean())
    print(f"  same selection chosen when both bet: {same:.1%}")
    for col in ("clv_max", "clv_pinn"):
        d = (both[f"{col}_cons"] - both[f"{col}_pinn"]).dropna()
        if d.empty:
            continue
        arr = d.to_numpy(float)
        boots = np.empty(B_BOOT)
        for b in range(B_BOOT):
            take = rng.integers(0, len(arr), size=len(arr))
            boots[b] = arr[take].mean()
        print(
            f"  paired d{col} (cons - pinn): {arr.mean():+.4f} "
            f"[CI {np.percentile(boots, 2.5):+.4f},{np.percentile(boots, 97.5):+.4f}] "
            f"(n={len(arr)})"
        )


def choose_ah_threshold(train_sweep: dict[float, Stats]) -> float | None:
    """Frozen thr_rule (docstring): max train ROI s.t. n >= AH_TRAIN_MIN_N and
    incCLV_max - 2*SE > 0. None = no qualifier -> one-shot CANCELLED."""
    viable = [
        (thr, s)
        for thr, s in train_sweep.items()
        if thr > 0.0
        and s.n >= AH_TRAIN_MIN_N
        and s.inc_clv_max is not None
        and s.inc_clv_max_se is not None
        and s.inc_clv_max - 2 * s.inc_clv_max_se > 0
    ]
    if not viable:
        return None
    return max(viable, key=lambda t: t[1].roi)[0]


def run_ah_oneshot(test_ah: pd.DataFrame, thr: float, rng: np.random.Generator) -> None:
    """THE pre-registered single look at AH 2425+2526. Consumes the domain."""
    if CONSUMPTION_MARKER.exists():
        print("\nREFUSED: AH one-shot already consumed (data/ml/AH_ONESHOT_CONSUMED.json).")
        print("The AH 2425+2526 domain is spent; the next legitimate look is season 2627.")
        return
    # power_gate (see docstring EXECUTION RECORD: the original version of
    # this check compared the total row count to the floor, passed at 140,
    # and the one-shot ran — this corrected bound arrives after the fact and
    # is kept so any future re-registration cannot repeat the mistake).
    # Upper bound on labeled SELECTED bets at thr: rows with edge >= thr,
    # odds >= MIN_ODDS, one per match — itself bounded by distinct matches.
    selectable = test_ah[(test_ah["best_price"] >= MIN_ODDS) & (test_ah["edge"] >= thr)]
    if selectable["mk"].nunique() < AH_TEST_MIN_N_LABELED:
        print(
            f"\nAH ONE-SHOT CANCELLED UNCONSUMED (power gate): at thr*={thr} at most "
            f"{selectable['mk'].nunique()} bets are selectable < {AH_TEST_MIN_N_LABELED} "
            "labeled bets required for a PASS — a passing verdict is arithmetically "
            "impossible, so the look is preserved (no label/outcome read)."
        )
        return
    print("\n" + "=" * 78)
    print("AH ONE-SHOT — pre-registered single look at seasons 2425+2526 (CONSUMING)")
    print(
        f"frozen criterion: thr*={thr} (train-chosen), pass iff n_labeled >= "
        f"{AH_TEST_MIN_N_LABELED} and incCLV_max - 2SE > 0 and ROI > 0"
    )
    print("=" * 78)
    # INTENT marker FIRST — before any label/outcome column is read. The
    # original ordering computed on the test slice and only then wrote the
    # marker, so a crash in between would have permitted a second look
    # (validator finding, 2026-06-12). The look is consumed the moment we
    # commit to it; results are appended to the same file afterwards.
    CONSUMPTION_MARKER.parent.mkdir(parents=True, exist_ok=True)
    CONSUMPTION_MARKER.write_text(
        json.dumps(
            {
                "consumed_at": datetime.now(tz=UTC).isoformat(),
                "domain": "AH market, seasons 2425+2526, half-line rows",
                "thr_star": thr,
                "status": "intent",
                "note": (
                    "intent marker written BEFORE the first label/outcome read; "
                    "if results are absent the look still counts as consumed"
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"intent marker written (look committed) -> {CONSUMPTION_MARKER}")
    null_bets = one_bet_per_match_market(test_ah, 0.0)
    null_stats = stats_for(null_bets, null_bets, rng)
    test_stats = stats_for(one_bet_per_match_market(test_ah, thr), null_bets, rng)
    print(_fmt("null", null_stats))
    print(_fmt(f"{thr:.3f}", test_stats))
    verdict = "FAIL — AH does not meet the premium bar on the fresh slice"
    if test_stats.n_labeled_max < AH_TEST_MIN_N_LABELED:
        verdict = (
            f"UNDERPOWERED — n_labeled {test_stats.n_labeled_max} < {AH_TEST_MIN_N_LABELED}; "
            "defer the AH verdict to live shadow CLV + season 2627"
        )
    elif (
        test_stats.inc_clv_max is not None
        and test_stats.inc_clv_max_se is not None
        and test_stats.inc_clv_max - 2 * test_stats.inc_clv_max_se > 0
        and test_stats.roi > 0
    ):
        verdict = (
            f"PASS — incCLV_max {test_stats.inc_clv_max:+.4f} (-2SE "
            f"{test_stats.inc_clv_max - 2 * test_stats.inc_clv_max_se:+.4f} > 0), "
            f"ROI {test_stats.roi * 100:+.2f}%"
        )
    print(f"\nAH ONE-SHOT VERDICT (computed): {verdict}")
    CONSUMPTION_MARKER.write_text(
        json.dumps(
            {
                "consumed_at": datetime.now(tz=UTC).isoformat(),
                "domain": "AH market, seasons 2425+2526, half-line rows",
                "thr_star": thr,
                "status": "completed",
                "n": test_stats.n,
                "n_labeled_max": test_stats.n_labeled_max,
                "roi": test_stats.roi,
                "inc_clv_max": test_stats.inc_clv_max,
                "inc_clv_max_se": test_stats.inc_clv_max_se,
                "verdict": verdict,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"consumption recorded -> {CONSUMPTION_MARKER}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=DATASET_DEFAULT)
    p.add_argument(
        "--oneshot-ah",
        action="store_true",
        help="run THE pre-registered AH one-shot on 2425+2526 (consumes the domain)",
    )
    args = p.parse_args(argv)
    rng = np.random.default_rng(SEED)

    df = pd.read_parquet(args.dataset)
    df["mk"] = _match_key(df)
    is_test = df["season"].isin(TEST_SEASONS)
    train = df[~is_test]
    assert not set(train["season"].unique()) & set(TEST_SEASONS)
    # the ONLY slice of 2425+2526 this script may ever touch: AH rows, and
    # only inside the pre-registered one-shot path below.
    test_ah = df[is_test & (df["market"] == "ah")].copy() if args.oneshot_ah else None
    del df, is_test

    print(f"dataset: {args.dataset.name} | train rows {len(train)} (seasons <= 2324 only)")

    # ---- TRACK A: consensus anchor, train-only ----------------------------
    cons_max = train[
        (train["anchor_type"] == "consensus")
        & (train["era"] == "maxavg")
        & (train["market"] == "1x2")
    ]
    pinn_max = train[
        (train["anchor_type"] == "pinnacle")
        & (train["era"] == "maxavg")
        & (train["market"] == "1x2")
    ]
    sweep(cons_max, "TRACK A — CONSENSUS-anchored 1x2, maxavg TRAIN (1920-2324)", rng)
    sweep(pinn_max, "TRACK A reference — PINNACLE-anchored 1x2, same matches/protocol", rng)
    cons_bb = train[
        (train["anchor_type"] == "consensus")
        & (train["era"] == "betbrain")
        & (train["market"] == "1x2")
    ]
    if not cons_bb.empty:
        sweep(
            cons_bb,
            "TRACK A supplementary — CONSENSUS 1x2, betbrain era (clv_pinn only, no Max close)",
            rng,
        )
    for thr in (0.015, 0.030):
        paired_anchor_comparison(train, thr, rng)
    print(
        "\nTRACK A fresh-domain note: football-data 'new leagues' (BRA/ARG/...) carry"
        "\nCLOSING odds only (verified 2026-06-12) — no signal-time price, protocol"
        "\nimpossible, NO one-shot available. Binding verdict: live anchor_type-"
        "\nstratified CLV + season 2627."
    )

    # ---- TRACK B: Asian handicap ------------------------------------------
    train_ah = train[train["market"] == "ah"]
    ah_sweep = sweep(train_ah, "TRACK B — ASIAN HANDICAP (half-line), TRAIN (1920-2324)", rng)
    thr_star = choose_ah_threshold(ah_sweep)
    if thr_star is None:
        print(
            "\nAH thr_rule: NO qualifying threshold on train "
            f"(need n >= {AH_TRAIN_MIN_N} and incCLV_max - 2SE > 0) — "
            "one-shot CANCELLED, the AH 2425+2526 domain stays UNCONSUMED."
        )
        return 0
    print(f"\nAH thr_rule chose thr* = {thr_star} (frozen rule in choose_ah_threshold)")
    if args.oneshot_ah:
        assert test_ah is not None
        run_ah_oneshot(test_ah, thr_star, rng)
    else:
        print("(--oneshot-ah not given: 2425+2526 AH rows were never read this run)")
    print("\nDecision-support only — nothing here places bets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
