"""Train + evaluate the value-filter META-MODEL **v3** (drop book_count only).

Round A9 scope — ONE change vs the deployed v2 CANDIDATE: remove `book_count`
from the feature set. v2's final model gave book_count ZERO gain and ZERO
splits (value_filter_manifest_v2.json importances); v3 measures the retrain
without it under the EXACT v2 protocol. The 37 dataset-v2 features (rolling
form, xG, devig deltas, odds_band) were a REAL NULL in v2 (manifest
feature_lift_v1_to_v2_logloss = -0.00261) and are NOT retried here — no
V2-feature arm exists in this script by construction.

SPENT-HOLDOUT DISCIPLINE (binding, .claude/memory/decisions.md) — identical
to v2, enforced by reusing v2's loader verbatim:
  Seasons 2425+2526 are SPENT and are filtered out at load time
  (t2.load_candidates_v2); they never reach any computation. All numbers in
  this run are train-OOF on the season-blocked expanding walk-forward within
  seasons <= 2324 — the SAME folds v2 used.
  The fresh never-consulted division pool (EC/SC1/SC2/SC3) was a
  pre-registered ONE-SHOT and was CONSUMED by the v2 run (2026-06-12): this
  script never scores it. The binding verdict for v3 is live shadow CLV +
  the fresh 2627 season, exactly as for v2.

PROTOCOL v3 — frozen before any result is computed; every reused component
is imported from scripts/ml/train_value_filter_v2.py (no re-implementation):
  Universe   identical rows: data/ml/value_candidates_v2.parquet, sha256
             asserted equal to the v2 manifest pin.
  Features   V3 = V1 minus book_count (13: 10 numeric + 3 categorical).
  Folds      tvf.make_folds — fit 1920 -> calib 2021 -> predict 2122; etc.
  ES proto   v2 option A unchanged: cap 2000, early_stopping(100, min_delta
             1e-4) on the CALIBRATION season (double duty with isotonic).
  Arms       1. v1_grounding          exact v1 winner, V1 features — must
                                      reproduce OOF log-loss 0.651751143.
             2. v2_selected_grounding v2's winning draw-81 params on V1
                                      features — must reproduce the v2
                                      manifest OOF log-loss 0.649683881.
             3. v3_lgbm               the SAME seeded sweep sequence
                                      (SWEEP_SEED, 100 draws — draw 81 is
                                      asserted identical to the v2 winner's
                                      params) on V3 features (headline).
             NO XGBoost arm: the v2 challenger ran on the banned V2 feature
             set and was refused by the pre-registered rule; re-running it
             is outside the A9 scope (book_count drop only).
  Selection  argmin pooled OOF log-loss within the v3 sweep (the feature set
             is FIXED by scope — the measured quantity is the v3-vs-v2 delta
             on identical folds, reported either way).
  Operating  v1 criterion unchanged: q on tvf.Q_GRID maximizing TRAIN-OOF
  point      ROI s.t. n >= 300 and incCLV_max - 2*bootstrap_SE > 0.
  Final fit  fit 1920-2223, early-stop + isotonic on 2324. Artifacts ONLY to
             *_v3 paths — deployed v1 and candidate v2 files are never
             touched. Manifest verdict is hard-coded "CANDIDATE" — this
             script can never emit ADOPT, so the live loader refuses it
             without VALUE_ML_MANIFEST_ALLOW_SHADOW, by construction.

Run:
    uv run --extra ml python scripts/ml/train_value_filter_v3.py
    uv run --extra ml python scripts/ml/train_value_filter_v3.py --quick

Checkpoint/resume (--ckpt-dir): pure compute plumbing for interruptible
environments — per-draw OOF metrics are appended to a JSONL as they finish
and reloaded on restart (params asserted identical to the seeded draw before
reuse; the selected draw is re-fit when its OOF vector is not in memory, and
its checkpointed log-loss is asserted reproduced). No seed, fold, draw
sequence, feature, or selection-rule change.

Decision-support only — nothing here places bets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from sklearn.metrics import log_loss  # noqa: F401  (env guard; used via t2)
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        f"missing ML dependency ({exc.name}); install with: uv sync --extra ml"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent


# Load the v2 trainer by path (scripts/ is not a package); it loads v1 (tvf)
# and the dataset builder (bvd) itself. ALL protocol machinery comes from it.
def _import_by_path(name: str, path: Path) -> Any:
    import importlib.util

    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


t2: Any = _import_by_path("train_value_filter_v2", _HERE / "train_value_filter_v2.py")
tvf: Any = sys.modules["train_value_filter"]

# ---------------------------------------------------------------------------
# Frozen v3 constants
# ---------------------------------------------------------------------------
SEED: int = int(t2.SEED)  # 20260612
SWEEP_SEED: int = int(t2.SWEEP_SEED)  # SEED + 100 — identical draw sequence
TRAIN_SEASONS: tuple[str, ...] = tuple(t2.TRAIN_SEASONS)
SPENT_SEASONS: tuple[str, ...] = tuple(t2.SPENT_SEASONS)
MM_KEY: list[str] = list(t2.MM_KEY)
DROPPED_FEATURE = "book_count"

# v2 manifest pins (data/ml/value_filter_manifest_v2.json, 2026-06-12) —
# grounding targets and the identical-row-set assertion.
V1_GROUNDING_LL: float = float(t2.V1_GROUNDING_LL)  # 0.651751143188923
V2_SELECTED_LL = 0.649683880634154
V2_SELECTED_BRIER = 0.22873386948618857
V2_SELECTED_ECE = 0.02165444893058038
V2_SELECTED_NAME = "lgbm_v1feat_sweep_draw81"
V2_SELECTED_DRAW = 81
V2_DATASET_SHA256 = "b5d5f701fa8a2a98b968c79b1466e5e9d49843fb108a2a91d4a1520033963b4b"
V2_SELECTED_PARAMS: dict[str, Any] = {
    "learning_rate": 0.03,
    "max_depth": 3,
    "num_leaves": 7,
    "min_child_samples": 100,
    "reg_lambda": 20.841311843135255,
    "reg_alpha": 0.1,
    "min_split_gain": 0.0,
    "colsample_bytree": 0.9569475559357388,
    "subsample": 0.780532720332403,
    "subsample_freq": 1,
    "max_bin": 127,
    "path_smooth": 1.0,
    "extra_trees": True,
    "monotone_penalty": 0.0,
}

FS_V1: Any = t2.FS_V1
FS_V3: Any = t2.FeatureSet(
    "v3",
    tuple(f for f in t2.V1_FEATURES_NUM if f != DROPPED_FEATURE),
    tuple(t2.V1_FEATURES_CAT),
)


def assert_feature_hygiene_v3() -> None:
    """Build-breaking gates: v2 hygiene + the v3 set is EXACTLY v1 minus book_count."""
    t2.assert_feature_hygiene_v2()
    assert DROPPED_FEATURE not in FS_V3.all
    assert set(FS_V3.all) == set(FS_V1.all) - {DROPPED_FEATURE}
    assert len(FS_V3.all) == len(FS_V1.all) - 1
    assert "edge" in FS_V3.num
    banned = set(t2.V2_NEW_FEATURES) & set(FS_V3.all)
    assert not banned, f"REAL-NULL v2 features must not be retried: {banned}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Checkpoint/resume plumbing (metrics only — never touches the protocol)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CkptResult:
    """Metrics-only stand-in for t2.OofResult reloaded from a checkpoint.

    p_cal is None by construction: any consumer needing the OOF probability
    vector (operating point) must re-fit, which the sweep loop does for the
    selected draw only.
    """

    log_loss: float
    brier: float
    ece: float
    calib_ece: float
    best_iters: tuple[int | None, ...]
    calib_kinds: tuple[str, ...]
    p_cal: None = None


def _res_dict(r: Any) -> dict[str, Any]:
    return {
        "log_loss": r.log_loss,
        "brier": r.brier,
        "ece": r.ece,
        "calib_ece": r.calib_ece,
        "best_iters": list(r.best_iters),
        "calib_kinds": list(r.calib_kinds),
    }


def _res_from_dict(d: Mapping[str, Any]) -> CkptResult:
    return CkptResult(
        log_loss=float(d["log_loss"]),
        brier=float(d["brier"]),
        ece=float(d["ece"]),
        calib_ece=float(d["calib_ece"]),
        best_iters=tuple(d["best_iters"]),
        calib_kinds=tuple(d["calib_kinds"]),
    )


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0915
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dataset", type=Path, default=REPO_ROOT / "data/ml/value_candidates_v2.parquet"
    )
    ap.add_argument(
        "--pool-cache", type=Path, default=REPO_ROOT / "data/ml/value_pool_full.parquet"
    )
    ap.add_argument("--n-draws-lgbm", type=int, default=100)
    ap.add_argument("--n-boot-train", type=int, default=500)
    ap.add_argument("--quick", action="store_true", help="smoke run: 4 draws, 100 bootstrap")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "data/ml")
    ap.add_argument("--ckpt-dir", type=Path, default=None, help="resume checkpoints (JSONL)")
    ap.add_argument("--mirror-dir", type=Path, default=None, help="also copy artifacts here")
    args = ap.parse_args(argv)
    if args.quick:
        args.n_draws_lgbm, args.n_boot_train = 4, 100

    ground_ckpt: Path | None = None
    sweep_ckpt: Path | None = None
    if args.ckpt_dir is not None:
        args.ckpt_dir.mkdir(parents=True, exist_ok=True)
        ground_ckpt = args.ckpt_dir / "v3_ground_ckpt.json"
        sweep_ckpt = args.ckpt_dir / "v3_sweep_ckpt.jsonl"
    ground_saved: dict[str, Any] = {}
    if ground_ckpt is not None and ground_ckpt.exists():
        ground_saved = json.loads(ground_ckpt.read_text())

    def grounded(key: str, compute: Callable[[], Any]) -> Any:
        if key in ground_saved:
            print("  (reloaded from checkpoint)")
            return _res_from_dict(ground_saved[key])
        r = compute()
        ground_saved[key] = _res_dict(r)
        if ground_ckpt is not None:
            ground_ckpt.write_text(json.dumps(ground_saved))
        return r

    assert_feature_hygiene_v3()
    rng_train = np.random.default_rng(SEED)

    print(f"value-filter v3 trainer | seed {SEED} | sweep seed {SWEEP_SEED}")
    print("label: clv_max > 0 | selection: pooled OOF log-loss (never accuracy)")
    print(f"V3 features ({len(FS_V3.all)}) = V1 minus {DROPPED_FEATURE}: {', '.join(FS_V3.all)}")
    print(
        "SPENT-HOLDOUT DISCIPLINE: 2425+2526 filtered at load (v2 loader reused); "
        "fresh EC/SC1/SC2/SC3 one-shot CONSUMED by v2 — never scored here. "
        "Binding verdict = live shadow CLV + fresh 2627 season."
    )

    dataset_sha = _sha256(args.dataset)
    assert dataset_sha == V2_DATASET_SHA256, (
        f"dataset drift: {dataset_sha} != v2 manifest pin — v3 requires the identical row set"
    )

    # ---- data (identical loader: spent seasons dropped, fresh slice unused) --
    train_cand, _fresh_never_consulted = t2.load_candidates_v2(args.dataset)
    print(
        f"\ntrain candidates: {len(train_cand)} rows "
        f"({int(train_cand['y'].notna().sum())} labeled) | fresh slice: NOT consulted"
    )
    folds = tvf.make_folds(TRAIN_SEASONS)
    for f in folds:
        print(f"fold: fit {f.fit_seasons} -> calib {f.calib_season} -> predict {f.predict_season}")

    fd_v1 = t2.make_fold_data(train_cand, folds, FS_V1)
    fd_v3 = t2.make_fold_data(train_cand, folds, FS_V3)

    # ---- arm 1: v1 grounding (harness parity) --------------------------------
    print("\nARM v1_grounding (exact v1 winner, v1 features, fixed 200 rounds):")
    res_ground = grounded(
        "v1_grounding", lambda: t2.run_oof(train_cand, fd_v1, t2.fit_v1_grounding)
    )
    drift1 = abs(res_ground.log_loss - V1_GROUNDING_LL)
    print(
        f"  log-loss {res_ground.log_loss:.6f} | manifest {V1_GROUNDING_LL:.6f} | "
        f"drift {drift1:.2e} {'OK' if drift1 < 1e-6 else 'MISMATCH — harness not at parity'}"
    )

    # ---- arm 2: v2 selected grounding (fold+ES parity at the v2 winner) ------
    print("\nARM v2_selected_grounding (v2 winner draw-81 params, V1 features, ES):")
    res_v2sel = grounded(
        "v2_selected_grounding",
        lambda: t2.run_oof(
            train_cand, fd_v1, lambda fd: t2.fit_lgbm_es(V2_SELECTED_PARAMS, fd, FS_V1)
        ),
    )
    drift2 = abs(res_v2sel.log_loss - V2_SELECTED_LL)
    print(
        f"  log-loss {res_v2sel.log_loss:.6f} | v2 manifest {V2_SELECTED_LL:.6f} | "
        f"drift {drift2:.2e} {'OK' if drift2 < 1e-6 else 'MISMATCH — harness not at parity'}"
    )

    # ---- arm 3: v3 sweep (identical seeded draw sequence, V3 features) -------
    rng_sweep = np.random.default_rng(SWEEP_SEED)
    lgbm_draws = [t2.sample_lgbm_params(rng_sweep) for _ in range(args.n_draws_lgbm)]
    if args.n_draws_lgbm > V2_SELECTED_DRAW:
        assert lgbm_draws[V2_SELECTED_DRAW] == V2_SELECTED_PARAMS, (
            "sweep draw sequence drifted: draw 81 no longer matches the v2 winner"
        )
    print(f"\nARM v3_lgbm (sweep, {len(lgbm_draws)} draws, V3 features):")
    done: dict[int, dict[str, Any]] = {}
    if sweep_ckpt is not None and sweep_ckpt.exists():
        for line in sweep_ckpt.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                done[int(row["draw"])] = row
        if done:
            print(f"  checkpoint: {len(done)} draws already computed — resuming")
    sweep_v3: list[Any] = []
    t0 = time.monotonic()
    for i, params in enumerate(lgbm_draws):
        row = done.get(i)
        if row is not None and row["params"] == params:
            sweep_v3.append(t2.Draw(i, dict(params), _res_from_dict(row)))
        else:
            res = t2.run_oof(train_cand, fd_v3, lambda fd, p=params: t2.fit_lgbm_es(p, fd, FS_V3))
            sweep_v3.append(t2.Draw(i, dict(params), res))
            if sweep_ckpt is not None:
                with sweep_ckpt.open("a") as fh:  # O_APPEND: line-atomic on POSIX
                    fh.write(json.dumps({"draw": i, "params": params, **_res_dict(res)}) + "\n")
        if (i + 1) % 10 == 0 or i + 1 == len(lgbm_draws):
            best = min(sweep_v3, key=lambda d: d.result.log_loss)
            print(
                f"  [lgbm/v3] draw {i + 1:3d}/{len(lgbm_draws)} | "
                f"elapsed {time.monotonic() - t0:6.1f}s | "
                f"best ll {best.result.log_loss:.5f} (draw {best.index})",
                flush=True,
            )
    best_v3 = min(sweep_v3, key=lambda d: d.result.log_loss)
    if best_v3.result.p_cal is None:  # reloaded from checkpoint — refit for the OOF vector
        pcal_ckpt = args.ckpt_dir / f"v3_pcal_draw{best_v3.index}.json" if args.ckpt_dir else None
        if pcal_ckpt is not None and pcal_ckpt.exists():
            print(f"  reloading OOF p_cal for selected draw {best_v3.index} from checkpoint")
            saved = json.loads(pcal_ckpt.read_text())
            assert abs(saved["log_loss"] - best_v3.result.log_loss) < 1e-9
            p_cal = pd.Series(np.nan, index=train_cand.index, dtype=float)
            p_cal.iloc[[int(k) for k in saved["pos"]]] = saved["vals"]
            best_v3 = t2.Draw(
                best_v3.index,
                dict(best_v3.params),
                t2.OofResult(
                    p_cal=p_cal,
                    log_loss=best_v3.result.log_loss,
                    brier=best_v3.result.brier,
                    ece=best_v3.result.ece,
                    calib_ece=best_v3.result.calib_ece,
                    best_iters=best_v3.result.best_iters,
                    calib_kinds=best_v3.result.calib_kinds,
                ),
            )
        else:
            print(
                f"  re-running OOF for selected draw {best_v3.index} (checkpoint is metrics-only)"
            )
            res = t2.run_oof(
                train_cand, fd_v3, lambda fd, p=best_v3.params: t2.fit_lgbm_es(p, fd, FS_V3)
            )
            assert abs(res.log_loss - best_v3.result.log_loss) < 1e-9, (
                "checkpointed metrics not reproduced on refit — discard the checkpoint and rerun"
            )
            if pcal_ckpt is not None:
                mask = res.p_cal.notna()
                pos = np.flatnonzero(mask.to_numpy())
                pcal_ckpt.write_text(
                    json.dumps(
                        {
                            "log_loss": res.log_loss,
                            "pos": [int(p) for p in pos],
                            "vals": [float(v) for v in res.p_cal.to_numpy()[pos]],
                        }
                    )
                )
            best_v3 = t2.Draw(best_v3.index, dict(best_v3.params), res)

    # ---- sweep log artifact ---------------------------------------------------
    args.out_dir.mkdir(parents=True, exist_ok=True)
    sweep_path = args.out_dir / "value_filter_v3_sweep.csv"
    pd.DataFrame(t2.sweep_rows("v3_lgbm", sweep_v3)).to_csv(sweep_path, index=False)

    # ---- arm comparison (pre-registered currency: pooled OOF log-loss) --------
    print("\nOOF ARM COMPARISON (pooled over the SAME folds v2 used; calibrated):")
    print(
        f"  {'arm':>22} | {'log-loss':>9} | {'brier':>8} | {'ece':>7} | "
        f"{'calib-ece':>9} | best_iters"
    )
    for name, r in (
        ("v1_grounding", res_ground),
        ("v2_selected_grounding", res_v2sel),
        ("v3_lgbm", best_v3.result),
    ):
        print(
            f"  {name:>22} | {r.log_loss:9.5f} | {r.brier:8.5f} | {r.ece:7.4f} | "
            f"{r.calib_ece:9.4f} | {list(r.best_iters)}"
        )

    delta_ll = best_v3.result.log_loss - V2_SELECTED_LL
    print(f"\nSELECTED: lgbm draw {best_v3.index} on v3 features")
    print(f"  params: {json.dumps(best_v3.params, sort_keys=True)}")
    print(
        f"\nBOOK_COUNT-DROP DELTA: v3 best {best_v3.result.log_loss:.5f} vs v2 selected "
        f"{V2_SELECTED_LL:.5f} -> delta {delta_ll:+.5f} "
        f"({'v3 WORSE — book_count mattered after all' if delta_ll > 0 else 'no OOF cost'})"
    )

    # ---- operating point on OOF of the selected arm (v1 criterion) ------------
    oof_seasons = [f.predict_season for f in folds]
    oof_df = train_cand[train_cand["season"].isin(oof_seasons)].copy()
    oof_df["p_cal"] = best_v3.result.p_cal[oof_df.index]
    if not args.pool_cache.exists():
        raise SystemExit(f"pool cache missing: {args.pool_cache} (run the v1 trainer once)")
    pool = pd.read_parquet(args.pool_cache)
    for col in MM_KEY:
        pool[col] = pool[col].astype(str)
    pool_oof = pool[pool["season"].isin(oof_seasons)]
    assert not set(pool_oof["season"]) & set(SPENT_SEASONS)
    null_oof = tvf.select_bets(pool_oof, "edge", 0.0)
    print(f"\nTRAIN OOF operating-point sweep (null=thr0, B={args.n_boot_train}):")
    print(
        tvf.fmt_stats(tvf.compute_stats("null thr=0", null_oof, None, args.n_boot_train, rng_train))
    )
    points: list[Any] = []
    for q in tvf.Q_GRID:
        st = tvf.compute_stats(
            f"q>={q:.3f}",
            tvf.select_bets(oof_df, "p_cal", q),
            null_oof,
            args.n_boot_train,
            rng_train,
        )
        points.append(tvf.OperatingPoint(q, st))
        print(tvf.fmt_stats(st))
    chosen = tvf.choose_operating_point(points)
    if chosen is None:
        print("\nNO TRAIN-QUALIFYING OPERATING POINT (n>=300 and incCLV_max-2SE>0).")
    else:
        inc = chosen.stats.inc_max
        assert inc is not None
        print(
            f"\nFROZEN OPERATING POINT q*={chosen.q:.3f} "
            f"(train ROI {chosen.stats.roi * 100:+.2f}%, n={chosen.stats.n}, "
            f"incCLV_max {inc.point:+.4f}±{2 * inc.se:.4f})"
        )

    # ---- final model: fit 1920-2223, early-stop + isotonic on 2324 ------------
    tail = TRAIN_SEASONS[-1]
    fit_df = train_cand[train_cand["season"].isin(TRAIN_SEASONS[:-1]) & train_cand["y"].notna()]
    cal_df = train_cand[(train_cand["season"] == tail) & train_cand["y"].notna()]
    x_fit, cats = t2.prepare_matrix_v2(fit_df, FS_V3)
    x_cal_final = t2.prepare_matrix_v2(cal_df, FS_V3, cats)[0]
    final_fd = t2.FoldData(
        fold=tvf.Fold(tuple(TRAIN_SEASONS[:-1]), tail, tail),
        x_fit=x_fit,
        y_fit=fit_df["y"].to_numpy(dtype=int),
        x_cal=x_cal_final,
        y_cal=cal_df["y"].to_numpy(dtype=int),
        x_prd=x_cal_final,
        prd_index=cal_df.index,
        cats=cats,
    )
    final_model = t2.fit_lgbm_es(best_v3.params, final_fd, FS_V3)
    final_best_iter = t2.best_iteration_of(final_model)
    p_raw_cal = t2.predict_raw(final_model, final_fd.x_cal)
    cal_kind, cal_obj = tvf.fit_calibrator(p_raw_cal, final_fd.y_cal)
    _, final_cal_ece = tvf.reliability_table(
        tvf.apply_calibrator((cal_kind, cal_obj), p_raw_cal), final_fd.y_cal.astype(float)
    )
    print(
        f"\nFINAL v3 MODEL: lgbm, fit {TRAIN_SEASONS[:-1]}, early-stop+{cal_kind} "
        f"on {tail} (n={len(cal_df)}, best_iteration {final_best_iter}, "
        f"calib-season ECE {final_cal_ece:.4f} — mildly optimistic, see v2 protocol caveat)"
    )
    print("\nFEATURE IMPORTANCES (final v3 model):")
    importances = t2.print_importances(final_model, list(final_fd.x_fit.columns))
    assert all(r["feature"] != DROPPED_FEATURE for r in importances)

    # ---- artifacts (v3 paths ONLY — never v1/v2 filenames) --------------------
    model_path = args.out_dir / "value_filter_model_v3.txt"
    final_model.booster_.save_model(
        str(model_path), num_iteration=final_best_iter or t2.N_ESTIMATORS_CAP
    )
    assert "_v3" in model_path.name

    calibrator_json: dict[str, Any]
    if cal_kind == "isotonic":
        calibrator_json = {
            "kind": "isotonic",
            "x_thresholds": cal_obj.X_thresholds_.tolist(),
            "y_thresholds": cal_obj.y_thresholds_.tolist(),
        }
    else:
        calibrator_json = {
            "kind": "platt",
            "coef": float(cal_obj.coef_[0][0]),
            "intercept": float(cal_obj.intercept_[0]),
        }

    def _arm_dict(
        name: str, r: Any, params: dict[str, Any] | None, draw: int | None
    ) -> dict[str, Any]:
        return {
            "arm": name,
            "log_loss": r.log_loss,
            "brier": r.brier,
            "ece": r.ece,
            "calib_ece": r.calib_ece,
            "best_iters": list(r.best_iters),
            "calib_kinds": list(r.calib_kinds),
            "params": params,
            "draw": draw,
        }

    manifest: dict[str, Any] = {
        "manifest_version": 3,
        "created_utc": datetime.now(UTC).isoformat(),
        "script": "scripts/ml/train_value_filter_v3.py",
        "dataset": str(args.dataset),
        "dataset_sha256": dataset_sha,
        "leagues": list(t2.LEAGUES_18),
        "train_seasons": list(TRAIN_SEASONS),
        "spent_seasons_never_loaded": list(SPENT_SEASONS),
        "min_odds": float(t2.MIN_ODDS),
        "devig": "differential_margin_weighting (ADR-0006: same method fill+close)",
        "label": "clv_max > 0 (vig-free Max-of-books close)",
        "features": list(FS_V3.all),
        "features_cat": list(FS_V3.cat),
        "dropped_vs_v2": [DROPPED_FEATURE],
        "banned_features_not_retried": list(t2.V2_NEW_FEATURES),
        "seed": SEED,
        "sweep_seed": SWEEP_SEED,
        "sweep": {
            "n_draws_lgbm": len(lgbm_draws),
            "es_protocol": (
                "cap 2000 rounds; early_stopping(rounds=100, min_delta=1e-4) on the "
                "CALIBRATION season (double duty with isotonic — v2 option A, unchanged)"
            ),
            "monotone": "edge:+1, method=advanced",
            "log": str(sweep_path),
        },
        "model": {
            "name": f"lgbm_v3feat_sweep_draw{best_v3.index}",
            "kind": "lgbm",
            "params": best_v3.params,
            "fit_seasons": list(TRAIN_SEASONS[:-1]),
            "calib_season": tail,
            "calibration": cal_kind,
            "calib_n": int(len(cal_df)),
            "best_iteration": final_best_iter,
            "calib_season_ece": final_cal_ece,
        },
        "calibrator": calibrator_json,
        "oof_metrics": {
            "log_loss": best_v3.result.log_loss,
            "brier": best_v3.result.brier,
            "ece": best_v3.result.ece,
            "n_oof": int((best_v3.result.p_cal.notna() & train_cand["y"].notna()).sum()),
        },
        "oof_arms": [
            _arm_dict("v1_grounding", res_ground, None, None),
            _arm_dict("v2_selected_grounding", res_v2sel, dict(V2_SELECTED_PARAMS), None),
            _arm_dict("v3_lgbm", best_v3.result, best_v3.params, best_v3.index),
        ],
        "v2_reference": {
            "model": V2_SELECTED_NAME,
            "log_loss": V2_SELECTED_LL,
            "brier": V2_SELECTED_BRIER,
            "ece": V2_SELECTED_ECE,
            "manifest": "data/ml/value_filter_manifest_v2.json",
        },
        "delta_oof_logloss_v3_minus_v2": delta_ll,
        "grounding_drift": {"v1": drift1, "v2_selected": drift2},
        "operating_point": None
        if chosen is None
        else {
            "q": chosen.q,
            "criterion": "max train-OOF ROI s.t. n>=300 and incCLV_max-2SE>0",
            "train_stats": tvf._stats_dict(chosen.stats),
        },
        "importances": importances,
        "fresh_one_shot": {
            "consulted": False,
            "note": (
                "the pre-registered EC/SC1/SC2/SC3 one-shot was CONSUMED by the v2 run "
                "(2026-06-12) and is never re-consulted; spent seasons 2425/2526 were "
                "filtered at load and touched by nothing in this run"
            ),
        },
        # NEVER "ADOPT" from this script — the live loader refuses non-ADOPT
        # manifests unless VALUE_ML_MANIFEST_ALLOW_SHADOW is set, by construction.
        "verdict": "CANDIDATE (binding verdict: live shadow CLV + fresh 2627 season)",
    }

    manifest_path = args.out_dir / "value_filter_manifest_v3.json"
    assert manifest_path.name not in ("value_filter_manifest.json", "value_filter_manifest_v2.json")
    manifest_path.write_text(json.dumps(manifest, indent=1))
    print(
        f"\nartifacts: {model_path.name}, {manifest_path.name}, {sweep_path.name} -> {args.out_dir}"
    )
    if args.mirror_dir is not None:
        args.mirror_dir.mkdir(parents=True, exist_ok=True)
        for p in (model_path, manifest_path, sweep_path):
            shutil.copy2(p, args.mirror_dir / p.name)
        print(f"mirrored artifacts -> {args.mirror_dir}")
    print("Decision-support only — picks are informational; this system never places bets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
