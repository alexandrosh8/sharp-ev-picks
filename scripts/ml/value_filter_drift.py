"""Value-filter drift check — post-ceiling holdout slice + live-vs-train PSI.

READ-ONLY validation instrument (prints a report; changes NO default, writes
NO artifact, flips NO flag, places NO bet). Two sections:

(a) CEILING-SLICE RE-EVALUATION of the pre-registered holdout criteria.
    Production later capped H2H fills at VALUE_MONEYLINE_MAX_ODDS=4.0
    (2026-06-30), so the live premium population is the odds<4.0 slice of
    the distribution the one-shot holdout evaluated. This re-runs the four
    pre-registered adoption criteria (C1-C4, docs/research/ml-value-filter.md
    §3) on that SLICE of the EXISTING holdout dataset with the FROZEN
    deployed model/calibrator/q* — the slice was always inside the one-shot
    eval, so no new holdout consultation and no leakage; but it IS a
    post-hoc subgroup readout, so the verdict line is advisory ("holds on
    slice"), never a new ADOPT/REJECT. Machinery (selection rule, clustered
    bootstrap, cell control) is imported from scripts/ml/train_value_filter.py
    — never restated.

(b) PSI — Population Stability Index between the TRAIN feature distribution
    (train seasons of the manifest's dataset) and the last --days days of
    LIVE picks in the model's scope (soccer 1x2 / OU2.5 in trained leagues):
    edge, fair_prob, best_price (10 quantile bins frozen on train) plus the
    league mix (categorical PSI over football-data codes; unmapped leagues
    pool into OTHER — honest: live cup/qualifier leagues the model never saw
    show up as drift, they are not silently dropped). Also reports the
    SCORED SHARE (picks carrying value_filter_score) — 0% means the shadow
    scorer is not annotating and live drift evidence is not accruing.
    HONESTY FLOOR: PSI is NULLED below n=50 live rows.

EXPLORATORY — any threshold later frozen from this data must treat this
readout as spent.

  uv run python scripts/ml/value_filter_drift.py [--max-odds 4.0] [--days 30]
  uv run python scripts/ml/value_filter_drift.py --dry-run   # no DB, no artifacts

Decision-support only — this system never places bets.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Honesty floor for the live PSI sample — below this, PSI is nulled, never a
# tiny-sample stability claim (MIN_STRATUM_N doctrine at report scale).
MIN_PSI_N = 50

PSI_BINS = 10
_PSI_EPS = 1e-4

# The production ceiling applies to the MONEYLINE market only
# (VALUE_MONEYLINE_MAX_ODDS, H2H-only by design) — ou25 rows pass unsliced.
CEILING_MARKETS = ("1x2",)

NUMERIC_PSI_FEATURES = ("edge", "fair_prob", "best_price")


# --------------------------------------------------------------------------- #
# Pure helpers (numpy/stdlib) — unit-tested in tests/test_value_filter_drift.py
# --------------------------------------------------------------------------- #
def psi_numeric(expected: Sequence[float], actual: Sequence[float]) -> float:
    """PSI with bin edges = `expected` deciles (frozen on the train side).

    psi = sum((a_i - e_i) * ln(a_i / e_i)) over bins, proportions floored at
    _PSI_EPS so an empty bin contributes finitely instead of inf."""
    exp = np.asarray(expected, dtype=float)
    act = np.asarray(actual, dtype=float)
    if len(exp) == 0 or len(act) == 0:
        raise ValueError("psi_numeric requires non-empty samples")
    edges = np.quantile(exp, np.linspace(0.0, 1.0, PSI_BINS + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    edges = np.unique(edges)  # degenerate (constant) features collapse bins
    e_prop = np.maximum(np.histogram(exp, bins=edges)[0] / len(exp), _PSI_EPS)
    a_prop = np.maximum(np.histogram(act, bins=edges)[0] / len(act), _PSI_EPS)
    return float(np.sum((a_prop - e_prop) * np.log(a_prop / e_prop)))


def psi_categorical(expected: Sequence[str], actual: Sequence[str]) -> float:
    """PSI over the union of category labels, proportions floored at _PSI_EPS."""
    if len(expected) == 0 or len(actual) == 0:
        raise ValueError("psi_categorical requires non-empty samples")
    e_counts, a_counts = Counter(expected), Counter(actual)
    labels = sorted(set(e_counts) | set(a_counts))
    psi = 0.0
    for lbl in labels:
        e = max(e_counts.get(lbl, 0) / len(expected), _PSI_EPS)
        a = max(a_counts.get(lbl, 0) / len(actual), _PSI_EPS)
        psi += (a - e) * math.log(a / e)
    return psi


def psi_with_floor(
    kind: str,
    expected: Sequence[Any],
    actual: Sequence[Any],
    floor: int = MIN_PSI_N,
) -> tuple[float | None, str]:
    """(psi, label) with HONESTY-FLOOR NULLING: below `floor` rows on either
    side the PSI is None and the label says insufficient."""
    if len(expected) < floor or len(actual) < floor:
        return None, f"insufficient (n<{floor})"
    psi = psi_numeric(expected, actual) if kind == "numeric" else psi_categorical(expected, actual)
    return psi, psi_verdict(psi)


def psi_verdict(psi: float) -> str:
    """Standard PSI reading: <0.10 stable, 0.10-0.25 moderate, >=0.25 major."""
    if psi < 0.10:
        return "stable (<0.10)"
    if psi < 0.25:
        return "moderate shift (0.10-0.25)"
    return "MAJOR shift (>=0.25)"


def slice_ceiling(df: Any, max_odds: float, markets: Sequence[str] = CEILING_MARKETS) -> Any:
    """The post-ceiling slice: drop rows in `markets` with best_price >= max_odds
    (production drops H2H fills at/above VALUE_MONEYLINE_MAX_ODDS); other
    markets pass through untouched."""
    keep = ~(df["market"].isin(list(markets)) & (df["best_price"] >= max_odds))
    return df[keep].copy()


def evaluate_criteria(meta: Any, vol: Any, ctrl: Any, min_bets: int) -> list[str]:
    """The four pre-registered adoption criteria, EXACTLY as the trainer's
    holdout block states them (train_value_filter.py main). Returns the list
    of failures (empty = all four hold). Inputs are SetStats-shaped objects
    (.roi, .n, .inc_max with .point/.se)."""
    fails: list[str] = []
    if not (
        meta.inc_max is not None
        and np.isfinite(meta.inc_max.se)
        and meta.inc_max.point - 2 * meta.inc_max.se > 0
    ):
        fails.append("C1 incCLV_max (vs thr=0 null, Max-close ref) not > 2*SE above zero")
    if not meta.roi >= vol.roi:
        fails.append(f"C2 ROI {meta.roi * 100:+.2f}% < volume baseline {vol.roi * 100:+.2f}%")
    if not (
        meta.inc_max is not None
        and ctrl.inc_max is not None
        and meta.inc_max.point > ctrl.inc_max.point
    ):
        fails.append("C3 does not beat the per-(league,market) threshold control on incCLV_max")
    if meta.n < min_bets:
        fails.append(f"C4 n={meta.n} < {min_bets}")
    return fails


# --------------------------------------------------------------------------- #
# (a) ceiling-slice holdout re-evaluation
# --------------------------------------------------------------------------- #
def resolve_dataset(recorded: str, override: Path | None) -> Path:
    """The holdout dataset path: an explicit --dataset wins; else the
    manifest's recorded path; else (manifest written on another machine —
    the recorded absolute path does not exist here) the same basename under
    this repo's data/ml/."""
    if override is not None:
        return override
    recorded_path = Path(recorded)
    if recorded_path.exists():
        return recorded_path
    return REPO_ROOT / "data" / "ml" / recorded_path.name


def _load_trainer() -> Any:
    """Import scripts/ml/train_value_filter.py by path (scripts/ is not a
    package; heavy deps — lightgbm/sklearn — load only on this path)."""
    if "train_value_filter" in sys.modules:
        return sys.modules["train_value_filter"]
    path = Path(__file__).resolve().parent / "train_value_filter.py"
    spec = importlib.util.spec_from_file_location("train_value_filter", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["train_value_filter"] = mod
    spec.loader.exec_module(mod)
    return mod


def run_slice_reeval(args: argparse.Namespace) -> dict[str, Any]:
    import lightgbm as lgb
    import pandas as pd

    from app.models.value_filter import CATEGORICAL_FEATURES, calibrate

    tvf = _load_trainer()
    manifest = json.loads(Path(args.manifest).read_text())
    if manifest.get("operating_point") is None:
        raise SystemExit("manifest has no operating point — nothing to re-evaluate")
    q_star = float(manifest["operating_point"]["q"])
    features = list(manifest["features"])
    test_seasons = list(manifest["test_seasons"])
    cell_thr = {
        (k.split("/")[0], k.split("/")[1]): v
        for k, v in manifest["cell_control_thresholds"].items()
    }

    cand = tvf.load_candidates(resolve_dataset(manifest["dataset"], args.dataset))
    test_cand = cand[cand["season"].isin(test_seasons)].copy()

    # Score with the FROZEN deployed booster + manifest calibrator (the
    # booster file carries the training pandas-categorical vocabulary).
    booster = lgb.Booster(model_file=str(args.model))
    x = test_cand[features].copy()
    if "is_argmax_edge" in x.columns:
        x["is_argmax_edge"] = x["is_argmax_edge"].astype("int8")
    for col in CATEGORICAL_FEATURES & set(x.columns):
        x[col] = x[col].astype("category")
    test_cand["p_cal"] = calibrate(
        manifest["calibrator"], np.asarray(booster.predict(x), dtype=float)
    )

    pool = pd.read_parquet(args.pool_cache)
    pool = tvf._normalize_keys(pool)
    pool_test = pool[pool["season"].isin(test_seasons)]

    results: dict[str, dict[str, Any]] = {}
    for label, sliced in (
        ("full holdout (reference)", False),
        (f"odds<{args.max_odds:g} slice ({'/'.join(CEILING_MARKETS)} only)", True),
    ):
        tc = slice_ceiling(test_cand, args.max_odds) if sliced else test_cand
        pt = slice_ceiling(pool_test, args.max_odds) if sliced else pool_test
        rng = np.random.default_rng(tvf.SEED + 1)  # frozen seed, both passes
        null_t = tvf.select_bets(pt, "edge", 0.0)
        stats = {
            "null": tvf.compute_stats("null thr=0", null_t, None, args.n_boot, rng),
            "volume": tvf.compute_stats(
                "edge>=0.015",
                tvf.select_bets(pt, "edge", tvf.VOLUME_BASELINE_THR),
                null_t,
                args.n_boot,
                rng,
            ),
            "control": tvf.compute_stats(
                "cell-control", tvf.select_cell_control(pt, cell_thr), null_t, args.n_boot, rng
            ),
            "meta": tvf.compute_stats(
                f"META q>={q_star:.3f}",
                tvf.select_bets(tc, "p_cal", q_star),
                null_t,
                args.n_boot,
                rng,
            ),
        }
        fails = evaluate_criteria(
            stats["meta"], stats["volume"], stats["control"], tvf.MIN_HOLDOUT_BETS
        )
        results[label] = {"stats": stats, "fails": fails}
    return {"q_star": q_star, "results": results}


def render_slice(section: dict[str, Any], fmt_stats: Any) -> str:
    lines = [
        "=" * 78,
        f"(a) CEILING-SLICE RE-EVALUATION — frozen q*={section['q_star']:.3f}; advisory",
        "    subgroup readout of the SPENT holdout (no new consultation, no new",
        "    ADOPT/REJECT; the binding gate metric remains incCLV_max).",
        "=" * 78,
    ]
    for label, res in section["results"].items():
        lines.append(f"\n--- {label} ---")
        for s in res["stats"].values():
            lines.append(fmt_stats(s))
        if res["fails"]:
            lines.append("  pre-registered criteria on this population: DO NOT ALL HOLD")
            lines.extend(f"    FAILED {f}" for f in res["fails"])
        else:
            lines.append(
                "  pre-registered criteria on this population: ALL FOUR HOLD "
                "(C1 incCLV_max>2SE, C2 ROI>=volume, C3 beats cell control, C4 n floor)"
            )
    slice_res = [r for label, r in section["results"].items() if "slice" in label][-1]
    verdict = (
        "HOLDS on the post-ceiling slice"
        if not slice_res["fails"]
        else ("DOES NOT fully hold on the post-ceiling slice")
    )
    lines.append(f"\nSLICE VERDICT (advisory): q*=0.725 {verdict}.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# (b) PSI train vs live
# --------------------------------------------------------------------------- #
async def fetch_live_rows(days: int) -> tuple[list[dict[str, Any]], int, int]:
    """Live in-scope picks (soccer 1x2 / OU2.5) for the PSI window.

    Returns (rows, n_scope, n_scored). READ-ONLY SELECTs; credentials never
    printed (database_url doctrine)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from scripts.research.sport_quality_report import _fetch_all, database_url

    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as conn:
            rows = await _fetch_all(
                conn,
                """
                SELECT p.edge, p.fair_probability, p.decimal_odds, l.name,
                       p.value_filter_score IS NOT NULL
                FROM picks p
                JOIN events e ON e.id = p.event_id
                JOIN sports s ON s.id = e.sport_id
                JOIN leagues l ON l.id = e.league_id
                WHERE s.key = 'soccer'
                  AND p.created_at >= :cutoff
                  AND (p.market = 'h2h'
                       OR (p.market = 'totals'
                           AND p.market_detail IN ('over_under_2_5', 'totals_2_5')))
                """,
                {"cutoff": datetime.now(UTC) - timedelta(days=days)},
            )
    finally:
        await engine.dispose()
    out = [
        {
            "edge": float(r[0]),
            "fair_prob": float(r[1]),
            "best_price": float(r[2]),
            "league_name": r[3],
        }
        for r in rows
    ]
    return out, len(rows), sum(1 for r in rows if r[4])


def compute_psi_section(
    train_df: Any, live_rows: list[dict[str, Any]], n_scope: int, n_scored: int
) -> dict[str, Any]:
    """Assemble the PSI table (pure given the frames — testable)."""
    from app.models.value_filter import league_code

    live_leagues = [league_code(r["league_name"]) or "OTHER" for r in live_rows]
    train_leagues = [str(v) for v in train_df["league"].tolist()]
    features: dict[str, dict[str, Any]] = {}
    for feat in NUMERIC_PSI_FEATURES:
        psi, label = psi_with_floor(
            "numeric",
            train_df[feat].dropna().tolist(),
            [r[feat] for r in live_rows],
        )
        features[feat] = {"psi": psi, "label": label}
    psi_l, label_l = psi_with_floor("categorical", train_leagues, live_leagues)
    features["league_mix"] = {"psi": psi_l, "label": label_l}
    return {
        "n_train": len(train_df),
        "n_live_scope": n_scope,
        "n_scored": n_scored,
        "scored_share": (n_scored / n_scope) if n_scope else None,
        "live_league_top": Counter(live_leagues).most_common(8),
        "features": features,
    }


def render_psi(section: dict[str, Any], days: int) -> str:
    lines = [
        "",
        "=" * 78,
        f"(b) PSI — train distribution vs last {days}d live in-scope picks "
        f"(honesty floor n>={MIN_PSI_N})",
        "=" * 78,
        f"train n={section['n_train']} | live in-scope n={section['n_live_scope']} | "
        f"scored n={section['n_scored']}",
    ]
    share = section["scored_share"]
    if share is not None:
        lines.append(
            f"SCORED SHARE: {share * 100:.1f}%"
            + (
                " — value_filter_score is NOT being annotated on live picks; "
                "shadow drift evidence is NOT accruing (investigate the scorer wiring)."
                if share == 0.0
                else ""
            )
        )
    lines.append(f"live league mix (mapped code or OTHER): {section['live_league_top']}")
    lines.append(f"\n{'feature':<14}{'PSI':>10}  reading")
    for feat, cell in section["features"].items():
        psi = f"{cell['psi']:.4f}" if cell["psi"] is not None else "--"
        lines.append(f"{feat:<14}{psi:>10}  {cell['label']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Dry run — pure-math self-check, no DB, no artifacts
# --------------------------------------------------------------------------- #
def run_dry_run() -> int:
    rng = np.random.default_rng(20260726)
    same = [float(v) for v in rng.normal(0.0, 1.0, 2000)]
    shifted = [v + 1.0 for v in same]
    psi_same = psi_numeric(same, same)
    psi_shift = psi_numeric(same, shifted)
    assert psi_same < 1e-9, psi_same
    assert psi_shift > 0.25, psi_shift
    psi_cat = psi_categorical(["a"] * 50 + ["b"] * 50, ["a"] * 90 + ["b"] * 10)
    nulled, label = psi_with_floor("numeric", same, same[:10])
    assert nulled is None and "insufficient" in label
    print("DRY RUN (no DB, no artifacts) — PSI self-check on known inputs:")
    print(f"  psi(x, x)            = {psi_same:.6f} (identical -> ~0)")
    print(f"  psi(x, x+1)          = {psi_shift:.4f} ({psi_verdict(psi_shift)})")
    print(f"  psi cat 50/50->90/10 = {psi_cat:.4f} (analytic 0.8789)")
    print(f"  floor nulling        = ({nulled}, '{label}')")
    print("Decision-support only — this system never places bets.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    default_ml = REPO_ROOT / "data" / "ml"
    ap.add_argument("--manifest", type=Path, default=default_ml / "value_filter_manifest.json")
    ap.add_argument("--model", type=Path, default=default_ml / "value_filter_model.txt")
    ap.add_argument("--dataset", type=Path, default=None, help="default: the manifest's dataset")
    ap.add_argument("--pool-cache", type=Path, default=default_ml / "value_pool_full.parquet")
    ap.add_argument("--max-odds", type=float, default=4.0)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--skip-db", action="store_true", help="section (a) only")
    ap.add_argument(
        "--dry-run", action="store_true", help="no DB, no artifacts: PSI/criteria self-check"
    )
    args = ap.parse_args(argv)

    if args.dry_run:
        return run_dry_run()

    import asyncio

    import pandas as pd

    section_a = run_slice_reeval(args)
    print(render_slice(section_a, sys.modules["train_value_filter"].fmt_stats))

    if not args.skip_db:
        manifest = json.loads(Path(args.manifest).read_text())
        train_df = pd.read_parquet(resolve_dataset(manifest["dataset"], args.dataset))
        train_df = train_df[train_df["season"].isin(manifest["train_seasons"])]
        live_rows, n_scope, n_scored = asyncio.run(fetch_live_rows(args.days))
        print(render_psi(compute_psi_section(train_df, live_rows, n_scope, n_scored), args.days))

    print(
        "\nREPORT ONLY — no defaults changed; q* remains frozen at "
        f"{section_a['q_star']:.3f} pending operator review."
    )
    print("Decision-support only — this system never places bets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
