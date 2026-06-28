"""Train + ONE-SHOT-evaluate the value-filter META-MODEL (meta-labeling).

Doctrine (docs/backtesting/value-findings.md, .claude/memory): the edge is
sharp-vs-soft line shopping; ML winner-prediction from team stats backtested
NEGATIVE and is forbidden as a strategy. "ML" here means META-MODELING the
validated value signal (Lopez de Prado meta-labeling: a deterministic PRIMARY
rule generates candidates; a SECONDARY classifier learns P(candidate is a
true positive) and filters). PRIMARY = best pre-match price beats
devig(Pinnacle pre-match) fair (candidate pool from
scripts/ml/build_value_dataset.py, edge >= 0.005). SECONDARY = this script.

META-LABEL: clv_max > 0 — the bet beats the vig-free Max-of-books CLOSE, the
strictest reference (it strips the mechanical best-of-N-books premium).
A profit-label variant (y = won) is reported on TRAIN only.

PRE-REGISTERED PROTOCOL — frozen in code BEFORE the holdout is touched:
  Universe   18 baseline leagues (value_backtest split), markets 1x2+ou25,
             maxavg era only, min_odds 1.6 (production v4 floor), devig =
             differential_margin_weighting on BOTH fill and close (ADR-0006).
  Splits     TRAIN seasons 1920-2324; HOLDOUT 2425+2526 touched only under
             --final (consultation #4 of this holdout — declared FINAL).
  CV         Season-blocked expanding walk-forward; fold k fits on seasons
             [0..k-2], calibrates on season k-1 (chronological tail,
             isotonic if n>=1000 else Platt), predicts season k. Matches are
             never split across folds (a match lives in exactly one season);
             the summer break is the embargo.
  Model sel  Pooled out-of-fold LOG-LOSS (Brier reported) — never accuracy.
  Selection  One bet per (match, market): argmax score among rows with
             best_price >= 1.6 and score >= cutoff.
  Operating  Chosen ON TRAIN OOF ONLY: max ROI subject to n >= 300 and
  point      incCLV_max(vs thr=0 null) - 2*bootstrap_SE > 0.
  Adoption   ALL FOUR on holdout: C1 incCLV_max > 2SE (Max-close ref, vs
  gate       thr=0 null); C2 ROI >= thr=0.015 volume baseline ROI; C3
             incCLV_max > per-(league,market)-threshold control (tuned on
             TRAIN only); C4 n >= 300. Bootstrap CIs cluster by MATCH.

Run (train phase only / + one-shot holdout):
    uv run --extra ml python scripts/ml/train_value_filter.py
    uv run --extra ml python scripts/ml/train_value_filter.py --final

Decision-support only — nothing here places bets.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    from sklearn.compose import ColumnTransformer
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, log_loss
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        f"missing ML dependency ({exc.name}); install with: uv sync --extra ml"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- import the dataset builder by path (scripts/ is not a package) --------
_BVD_PATH = Path(__file__).resolve().parent / "build_value_dataset.py"
if "build_value_dataset" in sys.modules:
    bvd: Any = sys.modules["build_value_dataset"]
else:
    _spec = importlib.util.spec_from_file_location("build_value_dataset", _BVD_PATH)
    assert _spec is not None and _spec.loader is not None
    bvd = importlib.util.module_from_spec(_spec)
    sys.modules["build_value_dataset"] = bvd  # register BEFORE exec (dataclasses)
    _spec.loader.exec_module(bvd)

# ---------------------------------------------------------------------------
# Frozen experiment constants (the value_backtest baseline split, exactly)
# ---------------------------------------------------------------------------
LEAGUES_18: tuple[str, ...] = (
    "E0",
    "E1",
    "E2",
    "E3",
    "SC0",
    "D1",
    "D2",
    "I1",
    "I2",
    "SP1",
    "SP2",
    "F1",
    "F2",
    "N1",
    "B1",
    "P1",
    "T1",
    "G1",
)
TRAIN_SEASONS: tuple[str, ...] = ("1920", "2021", "2122", "2223", "2324")
TEST_SEASONS: tuple[str, ...] = ("2425", "2526")
MIN_ODDS = 1.6  # production v4 floor — script default 1.0 does NOT reproduce prod
SEED = 20260612

MATCH_KEY = ["league", "season", "match_date", "home_team", "away_team"]
MM_KEY = [*MATCH_KEY, "market"]

# Features: SIGNAL columns from the dataset SCHEMA, minus `era` (constant in
# the maxavg-only window) and `open_to_signal_drift` (all-null for this
# source), PLUS league/market. league/market are classified ID in SCHEMA
# (join/split keys) but are signal-time-available context, and the methods
# research (R2 d/e, R4) mandates them as model features. assert_feature_
# hygiene() enforces that no LABEL column can ever enter this list.
FEATURES_NUM: tuple[str, ...] = (
    "edge",
    "fair_prob",
    "best_price",
    "pinn_price",
    "overround_pinn",
    "overround_best",
    "devig_spread",
    "book_count",
    "day_of_week",
    "days_to_season_end",
    "is_argmax_edge",
)
FEATURES_CAT: tuple[str, ...] = ("league", "market", "selection_type")
FEATURES: tuple[str, ...] = FEATURES_NUM + FEATURES_CAT
ALLOWED_ID_FEATURES = {"league", "market"}

# Pre-declared grids and criteria (frozen before the holdout is consulted)
Q_GRID: tuple[float, ...] = tuple(round(0.40 + 0.025 * i, 3) for i in range(17))
MIN_TRAIN_BETS = 300  # operating-point floor on the OOF pool
MIN_HOLDOUT_BETS = 300  # adoption gate C4 (anti-overfit rule 5)
CELL_THR_GRID: tuple[float, ...] = (0.005, 0.010, 0.015, 0.020, 0.030, 0.040, 0.050)
CELL_MIN_N = 30
CELL_FALLBACK_THR = 0.015
VOLUME_BASELINE_THR = 0.015
PREMIUM_BASELINE_THR = 0.03
ISOTONIC_MIN_N = 1000  # below this the calibrator falls back to Platt (R4)

LGBM_BASE: dict[str, Any] = {
    "objective": "binary",
    "learning_rate": 0.03,
    "max_depth": 3,  # R3: shallow, heavily regularized
    "num_leaves": 7,
    "reg_lambda": 5.0,
    "subsample": 1.0,
    "colsample_bytree": 1.0,
    "random_state": SEED,
    "n_jobs": 4,
    "verbosity": -1,
}


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str  # "lgbm" | "logreg"
    params: dict[str, Any]


SPECS: tuple[ModelSpec, ...] = (
    ModelSpec("logreg_C0.1", "logreg", {"C": 0.1}),
    ModelSpec("logreg_C1.0", "logreg", {"C": 1.0}),
    ModelSpec("lgbm_mcs200_n200", "lgbm", {"min_child_samples": 200, "n_estimators": 200}),
    ModelSpec("lgbm_mcs200_n400", "lgbm", {"min_child_samples": 200, "n_estimators": 400}),
    ModelSpec("lgbm_mcs500_n200", "lgbm", {"min_child_samples": 500, "n_estimators": 200}),
    ModelSpec("lgbm_mcs500_n400", "lgbm", {"min_child_samples": 500, "n_estimators": 400}),
)


def assert_feature_hygiene() -> None:
    """Build-breaking leakage gate for the TRAINER feature list."""
    bvd.assert_no_label_leak()
    labels = set(bvd.LABEL_COLUMNS)
    leaked = set(FEATURES) & labels
    assert not leaked, f"LABEL columns leaked into trainer features: {leaked}"
    signal = set(bvd.FEATURE_COLUMNS)
    extra = set(FEATURES) - signal - ALLOWED_ID_FEATURES
    assert not extra, f"non-SIGNAL features outside the allowed ID set: {extra}"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _normalize_keys(df: pd.DataFrame) -> pd.DataFrame:
    """String-normalize join keys so parquet-roundtrip and in-memory frames
    cluster identically (match_date date objects -> ISO strings)."""
    for col in MM_KEY:
        df[col] = df[col].astype(str)
    return df


def load_candidates(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df = df[
        (df["era"] == "maxavg")
        & df["league"].isin(LEAGUES_18)
        & df["season"].isin(TRAIN_SEASONS + TEST_SEASONS)
    ].copy()
    df = _normalize_keys(df)
    df = df.sort_values([*MM_KEY, "selection"], kind="mergesort").reset_index(drop=True)
    return df


def load_btb_calib(path: Path, cal_df: pd.DataFrame) -> pd.DataFrame:
    """Load BeatTheBookie consensus rows for CALIBRATION-ONLY augmentation.

    These rows (scripts/ml/build_value_dataset.py --source beatthebookie) carry
    a clv_max label vs the CONSENSUS close. They never enter model fitting,
    model selection, the operating-point sweep, or the holdout — only the final
    calibrator fit, where extra samples cross the isotonic n>=1000 threshold.
    The two sharp-only numeric features (pinn_price, overround_pinn) are
    structurally null here (no Pinnacle); they are imputed with the
    football-data calibration-set medians so the fitted model can score the
    rows. Decision-support only."""
    df = pd.read_parquet(path)
    df = df[df["clv_max"].notna()].copy()
    for col in ("pinn_price", "overround_pinn"):
        df[col] = df[col].fillna(float(cal_df[col].median()))
    return _normalize_keys(df)


def build_full_pool(
    cache_dir: Path, leagues: Sequence[str], seasons: Sequence[str]
) -> pd.DataFrame:
    """ALL selections (min_edge=-10, i.e. no edge floor) from the cached CSVs.

    This is the universe for the thr=0 'bet everything' null and the simple
    threshold baselines — bets_for-parity selection happens in select_bets.
    Reads cache only (no network); the dataset builder must have run first.
    """
    cands: list[Any] = []
    missing: list[str] = []
    for lg in leagues:
        for s in seasons:
            f = cache_dir / f"{s}_{lg}.csv"
            if not f.exists():
                missing.append(f.name)
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
            cands.extend(bvd.candidates_from_rows(lg, s, rows, min_edge=-10.0))
    if missing:
        raise SystemExit(
            f"missing {len(missing)} cached CSVs (e.g. {missing[0]}); "
            "run scripts/ml/build_value_dataset.py first"
        )
    pool = bvd._to_dataframe(cands)
    pool = pool[pool["era"] == "maxavg"].copy()
    pool = _normalize_keys(pool)
    return pool.sort_values([*MM_KEY, "selection"], kind="mergesort").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Selection rule (pre-declared): one bet per (match, market)
# ---------------------------------------------------------------------------
def select_bets(
    df: pd.DataFrame, score_col: str, thr: float, min_odds: float = MIN_ODDS
) -> pd.DataFrame:
    """argmax-score selection per (match, market), subject to the odds floor
    and score >= thr. Parity with scripts/value_backtest.py::bets_for when
    score_col='edge'."""
    d = df[(df["best_price"] >= min_odds) & (df[score_col] >= thr)]
    if d.empty:
        return d
    idx = d.groupby(MM_KEY, sort=False, observed=True)[score_col].idxmax()
    return df.loc[sorted(idx)]


# ---------------------------------------------------------------------------
# Match-clustered bootstrap (charter: never average ROI without a CI)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Boot:
    point: float
    se: float
    lo: float
    hi: float


def _cluster_sums(df: pd.DataFrame, col: str) -> pd.DataFrame:
    d = df[df[col].notna()]
    return d.groupby(MATCH_KEY, sort=True, observed=True)[col].agg(["sum", "count"])


def boot_mean(df: pd.DataFrame, col: str, n_boot: int, rng: np.random.Generator) -> Boot:
    """Match-clustered bootstrap of a per-bet mean (e.g. ROI = mean profit)."""
    a = _cluster_sums(df, col).to_numpy(dtype=float)
    point = a[:, 0].sum() / a[:, 1].sum()
    m = len(a)
    reps = np.empty(n_boot)
    for i in range(n_boot):
        s = a[rng.integers(0, m, m)].sum(axis=0)
        reps[i] = s[0] / s[1]
    return Boot(
        point,
        float(reps.std(ddof=1)),
        float(np.percentile(reps, 2.5)),
        float(np.percentile(reps, 97.5)),
    )


def boot_increment(
    sel: pd.DataFrame, null: pd.DataFrame, col: str, n_boot: int, rng: np.random.Generator
) -> Boot | None:
    """mean(col | selected) - mean(col | null), match-clustered bootstrap.

    Matches are resampled from the UNION of clusters; a 1x2 and an ou25 bet
    on the same match land in the same cluster (correlated outcomes)."""
    a = _cluster_sums(sel, col)
    b = _cluster_sums(null, col)
    if a.empty or b.empty:
        return None
    j = b.join(a, how="outer", lsuffix="_null", rsuffix="_sel").fillna(0.0)
    arr = j[["sum_sel", "count_sel", "sum_null", "count_null"]].to_numpy(dtype=float)
    point = arr[:, 0].sum() / arr[:, 1].sum() - arr[:, 2].sum() / arr[:, 3].sum()
    m = len(arr)
    reps: list[float] = []
    for _ in range(n_boot):
        s = arr[rng.integers(0, m, m)].sum(axis=0)
        if s[1] < 1.0 or s[3] < 1.0:
            continue  # replicate drew no selected (or no null) bets
        reps.append(s[0] / s[1] - s[2] / s[3])
    r = np.asarray(reps)
    if len(r) < max(50, n_boot // 2):
        return Boot(point, float("inf"), float("-inf"), float("inf"))  # unstable CI
    return Boot(
        point, float(r.std(ddof=1)), float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))
    )


# ---------------------------------------------------------------------------
# Per-set statistics
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SetStats:
    label: str
    n: int
    n_matches: int
    hit: float
    roi: float
    roi_boot: Boot | None
    clv_pinn: float | None
    clv_pinn_se: float | None
    clv_max: float | None
    clv_max_se: float | None
    beat_max: float | None
    n_missing_pinn: int
    n_missing_max: int
    inc_pinn: Boot | None
    inc_max: Boot | None


def _mean_se(xs: pd.Series) -> tuple[float | None, float | None]:
    """Analytic mean +/- SE, ddof=0 — parity with value_backtest.Stats."""
    x = xs.dropna().to_numpy(dtype=float)
    if len(x) == 0:
        return None, None
    return float(x.mean()), float(x.std(ddof=0) / np.sqrt(len(x)))


def compute_stats(
    label: str,
    sel: pd.DataFrame,
    null: pd.DataFrame | None,
    n_boot: int,
    rng: np.random.Generator,
) -> SetStats:
    n = len(sel)
    if n == 0:
        return SetStats(label, 0, 0, 0.0, 0.0, None, None, None, None, None, None, 0, 0, None, None)
    cp_m, cp_se = _mean_se(sel["clv_pinn"])
    cm_m, cm_se = _mean_se(sel["clv_max"])
    cm = sel["clv_max"].dropna()
    return SetStats(
        label=label,
        n=n,
        n_matches=int(sel.groupby(MATCH_KEY, observed=True).ngroups),
        hit=float(sel["won"].mean()),
        roi=float(sel["profit_units"].mean()),
        roi_boot=boot_mean(sel, "profit_units", n_boot, rng),
        clv_pinn=cp_m,
        clv_pinn_se=cp_se,
        clv_max=cm_m,
        clv_max_se=cm_se,
        beat_max=float((cm > 0).mean()) if len(cm) else None,
        n_missing_pinn=int(sel["clv_pinn"].isna().sum()),
        n_missing_max=int(sel["clv_max"].isna().sum()),
        inc_pinn=boot_increment(sel, null, "clv_pinn", n_boot, rng) if null is not None else None,
        inc_max=boot_increment(sel, null, "clv_max", n_boot, rng) if null is not None else None,
    )


def fmt_stats(s: SetStats) -> str:
    if s.n == 0:
        return f"{s.label:>14} | (no bets)"
    cp = f"{s.clv_pinn:+.4f}±{2 * (s.clv_pinn_se or 0):.4f}" if s.clv_pinn is not None else "n/a"
    cm = f"{s.clv_max:+.4f}±{2 * (s.clv_max_se or 0):.4f}" if s.clv_max is not None else "n/a"
    roi_ci = f" [{s.roi_boot.lo * 100:+.1f},{s.roi_boot.hi * 100:+.1f}]" if s.roi_boot else ""
    inc = ""
    if s.inc_max is not None:
        inc = f" | incMAX {s.inc_max.point:+.4f}±{2 * s.inc_max.se:.4f}"
    if s.inc_pinn is not None:
        inc += f" incPINN {s.inc_pinn.point:+.4f}"
    return (
        f"{s.label:>14} | n={s.n:5d} ({s.n_matches:5d} matches) | hit {s.hit * 100:4.1f}% | "
        f"ROI {s.roi * 100:+6.2f}%{roi_ci} | CLVpinn {cp} | CLVmax {cm}{inc}"
    )


# ---------------------------------------------------------------------------
# Models + calibration
# ---------------------------------------------------------------------------
def prepare_matrix(
    df: pd.DataFrame, categories: dict[str, list[str]] | None = None
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    x = df[list(FEATURES)].copy()
    x["is_argmax_edge"] = x["is_argmax_edge"].astype("int8")
    if categories is None:  # vocabulary comes from the FIT slice only
        categories = {c: sorted(x[c].astype(str).unique()) for c in FEATURES_CAT}
    for c in FEATURES_CAT:
        x[c] = pd.Categorical(x[c].astype(str), categories=categories[c])
    return x, categories


def fit_model(spec: ModelSpec, x: pd.DataFrame, y: np.ndarray) -> Any:
    if spec.kind == "lgbm":
        params = LGBM_BASE | spec.params
        # We KNOW more edge cannot mean worse expected CLV — encode it.
        mono = [1 if f == "edge" else 0 for f in x.columns]
        clf = lgb.LGBMClassifier(**params, monotone_constraints=mono)
        clf.fit(x, y, categorical_feature=list(FEATURES_CAT))
        return clf
    pre = ColumnTransformer(
        [
            ("cat", OneHotEncoder(handle_unknown="ignore"), list(FEATURES_CAT)),
            ("num", StandardScaler(), list(FEATURES_NUM)),
        ]
    )
    return Pipeline(
        [
            ("pre", pre),
            ("lr", LogisticRegression(C=spec.params["C"], max_iter=2000, random_state=SEED)),
        ]
    ).fit(x, y)


def predict_raw(model: Any, x: pd.DataFrame) -> np.ndarray:
    return np.asarray(model.predict_proba(x)[:, 1], dtype=float)


def fit_calibrator(p_raw: np.ndarray, y: np.ndarray) -> tuple[str, Any]:
    """Calibrate on a held-out chronological tail. Clean-room equivalent of
    CalibratedClassifierCV(cv='prefit', method='isotonic') — that API was
    removed in sklearn 1.8; fitting IsotonicRegression directly on the
    disjoint tail slice is the same math and keeps the artifact pickle-free.
    Isotonic only at n >= 1000 (it overfits below — Niculescu-Mizil &
    Caruana 2005); Platt fallback otherwise."""
    if len(y) >= ISOTONIC_MIN_N:
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso.fit(p_raw, y)
        return "isotonic", iso
    lr = LogisticRegression(C=1e6, max_iter=1000, random_state=SEED)
    lr.fit(p_raw.reshape(-1, 1), y)
    return "platt", lr


def fit_beta_calibration(p_raw: np.ndarray, y: np.ndarray) -> dict[str, float]:
    """Beta calibration (Kull/Silva-Filho/Flach, AISTATS 2017): fit
    sigmoid(a*ln(p) - b*ln(1-p) + c) by logistic regression on features
    [ln(p), -ln(1-p)], dropping a feature whose coefficient turns negative and
    refitting so the map stays monotone (the betacal algorithm) — clean-room, no
    betacal dependency. Returns the JSON-serializable params the sklearn-free
    runtime replay in app.models.value_filter.calibrate consumes (kind='beta')."""
    pr = np.clip(p_raw, 1e-6, 1.0 - 1e-6)
    f_a = np.log(pr)
    f_b = -np.log(1.0 - pr)
    lr = LogisticRegression(C=1e6, max_iter=1000, random_state=SEED)
    lr.fit(np.column_stack([f_a, f_b]), y)
    a, b = float(lr.coef_[0][0]), float(lr.coef_[0][1])
    c = float(lr.intercept_[0])
    if a < 0.0:  # drop ln(p); refit on -ln(1-p) only
        lr.fit(f_b.reshape(-1, 1), y)
        a, b, c = 0.0, float(lr.coef_[0][0]), float(lr.intercept_[0])
    elif b < 0.0:  # drop -ln(1-p); refit on ln(p) only
        lr.fit(f_a.reshape(-1, 1), y)
        a, b, c = float(lr.coef_[0][0]), 0.0, float(lr.intercept_[0])
    return {"kind": "beta", "a": a, "b": b, "c": c}


def apply_calibrator(cal: tuple[str, Any], p_raw: np.ndarray) -> np.ndarray:
    kind, m = cal
    if kind == "isotonic":
        p = np.asarray(m.transform(p_raw), dtype=float)
    elif kind == "beta":  # m is the JSON params dict from fit_beta_calibration
        pr = np.clip(p_raw, 1e-6, 1.0 - 1e-6)
        z = m["a"] * np.log(pr) - m["b"] * np.log(1.0 - pr) + m["c"]
        p = 1.0 / (1.0 + np.exp(-z))
    else:
        p = np.asarray(m.predict_proba(p_raw.reshape(-1, 1))[:, 1], dtype=float)
    return np.clip(p, 1e-6, 1.0 - 1e-6)


def rank_calibrators(
    p_cal: np.ndarray, y_cal: np.ndarray, p_eval: np.ndarray, y_eval: np.ndarray
) -> list[dict[str, float]]:
    """OFFLINE calibrator bake-off: fit isotonic + platt + beta on (p_cal, y_cal)
    and score each on the held-out (p_eval, y_eval) by log-loss + Brier, ranked
    best (lowest) log-loss first. Decision support ONLY — held-out incremental
    CLV (> 2 SE) remains the sole arbiter of swapping the live calibrator
    (ADR-0017); a lower offline log-loss never auto-promotes a method."""
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(p_cal, y_cal)
    platt = LogisticRegression(C=1e6, max_iter=1000, random_state=SEED)
    platt.fit(p_cal.reshape(-1, 1), y_cal)
    candidates: dict[str, tuple[str, Any]] = {
        "isotonic": ("isotonic", iso),
        "platt": ("platt", platt),
        "beta": ("beta", fit_beta_calibration(p_cal, y_cal)),
    }
    rows = [
        {
            "kind": name,
            "log_loss": float(log_loss(y_eval, apply_calibrator(cal, p_eval), labels=[0, 1])),
            "brier": float(brier_score_loss(y_eval, apply_calibrator(cal, p_eval))),
        }
        for name, cal in candidates.items()
    ]
    return sorted(rows, key=lambda r: r["log_loss"])


# ---------------------------------------------------------------------------
# Season-blocked expanding walk-forward
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Fold:
    fit_seasons: tuple[str, ...]
    calib_season: str
    predict_season: str


def make_folds(train_seasons: Sequence[str]) -> list[Fold]:
    """Expanding window over TRAIN seasons only; first two seasons are never
    predicted (they seed the first fit+calibration)."""
    s = sorted(train_seasons)
    return [Fold(tuple(s[: k - 1]), s[k - 1], s[k]) for k in range(2, len(s))]


def oof_calibrated(
    df: pd.DataFrame, spec: ModelSpec, folds: Sequence[Fold], y_col: str
) -> pd.Series:
    """Out-of-fold calibrated P(y=1) for every predict-season row."""
    p = pd.Series(np.nan, index=df.index, dtype=float)
    for fold in folds:
        fit_df = df[df["season"].isin(fold.fit_seasons) & df[y_col].notna()]
        cal_df = df[(df["season"] == fold.calib_season) & df[y_col].notna()]
        prd_df = df[df["season"] == fold.predict_season]
        x_fit, cats = prepare_matrix(fit_df)
        model = fit_model(spec, x_fit, fit_df[y_col].to_numpy(dtype=int))
        cal = fit_calibrator(
            predict_raw(model, prepare_matrix(cal_df, cats)[0]),
            cal_df[y_col].to_numpy(dtype=int),
        )
        p.loc[prd_df.index] = apply_calibrator(
            cal, predict_raw(model, prepare_matrix(prd_df, cats)[0])
        )
    return p


# ---------------------------------------------------------------------------
# Operating point (pre-declared criterion) + per-cell threshold control
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OperatingPoint:
    q: float
    stats: SetStats


def choose_operating_point(
    points: Sequence[OperatingPoint], min_bets: int = MIN_TRAIN_BETS
) -> OperatingPoint | None:
    """PRE-DECLARED (frozen before any holdout look): maximize TRAIN-OOF ROI
    subject to n >= min_bets AND incCLV_max - 2*bootstrap_SE > 0. Returns
    None when nothing qualifies (-> REJECT without consulting the holdout)."""
    ok = [
        p
        for p in points
        if p.stats.n >= min_bets
        and p.stats.inc_max is not None
        and np.isfinite(p.stats.inc_max.se)
        and p.stats.inc_max.point - 2 * p.stats.inc_max.se > 0
    ]
    if not ok:
        return None
    return max(ok, key=lambda p: p.stats.roi)


def tune_cell_thresholds(train_pool: pd.DataFrame) -> dict[tuple[str, str], float]:
    """Simpler-control (gate C3): per-(league, market) edge threshold tuned
    on TRAIN only — max cell ROI over the grid, n >= 30, else fallback."""
    out: dict[tuple[str, str], float] = {}
    for (lg, mkt), cell in train_pool.groupby(["league", "market"], observed=True):
        best: tuple[float, float] | None = None
        for thr in CELL_THR_GRID:
            sel = select_bets(cell, "edge", thr)
            if len(sel) < CELL_MIN_N:
                continue
            roi = float(sel["profit_units"].mean())
            if best is None or roi > best[1]:
                best = (thr, roi)
        out[(str(lg), str(mkt))] = best[0] if best else CELL_FALLBACK_THR
    return out


def select_cell_control(pool: pd.DataFrame, cell_thr: dict[tuple[str, str], float]) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for (lg, mkt), cell in pool.groupby(["league", "market"], observed=True):
        sel = select_bets(cell, "edge", cell_thr.get((str(lg), str(mkt)), CELL_FALLBACK_THR))
        if not sel.empty:
            parts.append(sel)
    return pd.concat(parts).sort_index() if parts else pool.iloc[0:0]


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def reliability_table(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> tuple[list[str], float]:
    df = pd.DataFrame({"p": p, "y": y}).dropna()
    df["bin"] = pd.qcut(df["p"], n_bins, duplicates="drop")
    lines: list[str] = []
    ece = 0.0
    for b, g in df.groupby("bin", observed=True):
        gap = float(g["p"].mean() - g["y"].mean())
        ece += len(g) / len(df) * abs(gap)
        lines.append(
            f"  {str(b):>18} | n={len(g):5d} | mean_pred {g['p'].mean():.4f} | "
            f"frac_pos {g['y'].mean():.4f} | gap {gap:+.4f}"
        )
    return lines, ece


def importance_table(model: Any) -> list[str]:
    if isinstance(model, lgb.LGBMClassifier):
        booster = model.booster_
        gain = booster.feature_importance(importance_type="gain")
        split = booster.feature_importance(importance_type="split")
        names = booster.feature_name()
        order = np.argsort(gain)[::-1]
        total = gain.sum() or 1.0
        return [
            f"  {names[i]:>20} | gain {gain[i]:12.1f} ({gain[i] / total * 100:5.1f}%) | "
            f"splits {split[i]:4d}"
            for i in order
        ]
    # logistic pipeline: standardized coefficients
    lr = model.named_steps["lr"]
    names = model.named_steps["pre"].get_feature_names_out()
    coefs = lr.coef_[0]
    order = np.argsort(np.abs(coefs))[::-1]
    return [f"  {names[i]:>32} | coef {coefs[i]:+.4f}" for i in order[:25]]


def pinnacle_fill_sensitivity(sel: pd.DataFrame) -> str:
    """Execution-realism haircut: settle the SAME picks at the Pinnacle
    pre-match price instead of the Max price (soft books limit winners; the
    Max line assumes shopping every book at snapshot time)."""
    if sel.empty:
        return "  (no bets)"
    profit = np.where(sel["won"], sel["pinn_price"] - 1.0, -1.0)
    with np.errstate(invalid="ignore"):
        clv_max = np.log(sel["pinn_price"].to_numpy() * sel["max_close_fair"].to_numpy())
    clv_ok = clv_max[~np.isnan(clv_max)]
    return (
        f"  fill@Pinnacle instead of Max: ROI {profit.mean() * 100:+6.2f}% | "
        f"CLVmax {clv_ok.mean():+.4f} (n={len(sel)})"
    )


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stats_dict(s: SetStats) -> dict[str, Any]:
    d: dict[str, Any] = {
        "label": s.label,
        "n": s.n,
        "n_matches": s.n_matches,
        "hit": s.hit,
        "roi": s.roi,
        "clv_pinn": s.clv_pinn,
        "clv_pinn_se": s.clv_pinn_se,
        "clv_max": s.clv_max,
        "clv_max_se": s.clv_max_se,
        "beat_max": s.beat_max,
        "n_missing_close_pinn": s.n_missing_pinn,
        "n_missing_close_max": s.n_missing_max,
    }
    if s.roi_boot:
        d["roi_ci95"] = [s.roi_boot.lo, s.roi_boot.hi]
    for name, b in (("inc_clv_pinn", s.inc_pinn), ("inc_clv_max", s.inc_max)):
        if b is not None:
            d[name] = {"point": b.point, "boot_se": b.se, "ci95": [b.lo, b.hi]}
    return d


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, default=REPO_ROOT / "data/ml/value_candidates.parquet")
    ap.add_argument("--cache-dir", type=Path, default=REPO_ROOT / "data/ml/cache")
    ap.add_argument(
        "--pool-cache", type=Path, default=REPO_ROOT / "data/ml/value_pool_full.parquet"
    )
    ap.add_argument("--rebuild-pool", action="store_true")
    ap.add_argument(
        "--final",
        action="store_true",
        help="run the ONE-SHOT holdout evaluation (consultation #4, declared final)",
    )
    ap.add_argument(
        "--btb-calib-parquet",
        type=Path,
        default=None,
        help="opt-in: augment ONLY the final calibrator fit with BeatTheBookie "
        "consensus rows (more samples -> isotonic n>=1000); never enters model "
        "fit, model selection, the operating-point sweep, or the holdout",
    )
    ap.add_argument("--n-boot-train", type=int, default=500)
    ap.add_argument("--n-boot-final", type=int, default=2000)
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "data/ml")
    args = ap.parse_args(argv)

    assert_feature_hygiene()
    rng_train = np.random.default_rng(SEED)
    rng_final = np.random.default_rng(SEED + 1)

    print(f"label: clv_max > 0 (beats vig-free Max-of-books close) | seed {SEED}")
    print(f"features ({len(FEATURES)}): {', '.join(FEATURES)}")

    # ---- data -------------------------------------------------------------
    cand = load_candidates(args.dataset)
    cand["y"] = np.where(cand["clv_max"].notna(), (cand["clv_max"] > 0).astype(float), np.nan)
    cand["y_profit"] = cand["won"].astype(float)
    train_cand = cand[cand["season"].isin(TRAIN_SEASONS)].copy()
    n_unlabeled = int(train_cand["clv_max"].isna().sum())
    print(
        f"\ncandidates: train {len(train_cand)} rows ({n_unlabeled} lack the Max-close "
        f"label and are excluded from fitting); holdout rows untouched until --final"
    )

    if args.pool_cache.exists() and not args.rebuild_pool:
        pool = pd.read_parquet(args.pool_cache)
        pool = _normalize_keys(pool)
        print(f"full pool: {len(pool)} rows (cached {args.pool_cache.name})")
    else:
        pool = build_full_pool(args.cache_dir, LEAGUES_18, TRAIN_SEASONS + TEST_SEASONS)
        args.pool_cache.parent.mkdir(parents=True, exist_ok=True)
        pool.to_parquet(args.pool_cache, engine="pyarrow", index=False)
        print(f"full pool: {len(pool)} rows (rebuilt, no edge floor, min_odds applied later)")
    pool_train = pool[pool["season"].isin(TRAIN_SEASONS)]
    pool_test = pool[pool["season"].isin(TEST_SEASONS)]

    # ---- model selection on season-blocked walk-forward OOF ---------------
    folds = make_folds(TRAIN_SEASONS)
    for f in folds:
        print(f"fold: fit {f.fit_seasons} -> calib {f.calib_season} -> predict {f.predict_season}")
    oof_seasons = [f.predict_season for f in folds]

    print("\nMODEL SELECTION (pooled OOF log-loss; Brier reported; never accuracy):")
    results: list[tuple[ModelSpec, float, float, pd.Series]] = []
    for spec in SPECS:
        p = oof_calibrated(train_cand, spec, folds, "y")
        mask = p.notna() & train_cand["y"].notna()
        ll = float(log_loss(train_cand.loc[mask, "y"], p[mask], labels=[0.0, 1.0]))
        br = float(brier_score_loss(train_cand.loc[mask, "y"], p[mask]))
        results.append((spec, ll, br, p))
        print(f"  {spec.name:>18} | log-loss {ll:.5f} | brier {br:.5f} | n_oof {int(mask.sum())}")
    best_spec, best_ll, best_br, best_p = min(results, key=lambda r: r[1])
    print(f"chosen spec (min OOF log-loss): {best_spec.name}")

    # ---- TRAIN-side operating-point sweep ----------------------------------
    oof_df = train_cand[train_cand["season"].isin(oof_seasons)].copy()
    oof_df["p_cal"] = best_p[oof_df.index]
    pool_oof = pool_train[pool_train["season"].isin(oof_seasons)]
    null_oof = select_bets(pool_oof, "edge", 0.0)
    print(
        f"\nTRAIN OOF sweep (seasons {oof_seasons}, null = thr=0 bet-everything, "
        f"min_odds {MIN_ODDS}, B={args.n_boot_train}):"
    )
    print(fmt_stats(compute_stats("null thr=0", null_oof, None, args.n_boot_train, rng_train)))
    for thr in (VOLUME_BASELINE_THR, PREMIUM_BASELINE_THR):
        s = compute_stats(
            f"edge>={thr}",
            select_bets(pool_oof, "edge", thr),
            null_oof,
            args.n_boot_train,
            rng_train,
        )
        print(fmt_stats(s))
    points: list[OperatingPoint] = []
    for q in Q_GRID:
        st = compute_stats(
            f"q>={q:.3f}",
            select_bets(oof_df, "p_cal", q),
            null_oof,
            args.n_boot_train,
            rng_train,
        )
        points.append(OperatingPoint(q, st))
        print(fmt_stats(st))

    chosen = choose_operating_point(points)
    if chosen is None:
        print(
            "\nNO TRAIN-QUALIFYING OPERATING POINT (criterion: n>=300 and "
            "incCLV_max-2SE>0) -> meta-model REJECTED on train; holdout NOT consulted."
        )
    else:
        inc = chosen.stats.inc_max
        assert inc is not None
        print(
            f"\nFROZEN OPERATING POINT (train-only choice): q*={chosen.q:.3f} "
            f"(train ROI {chosen.stats.roi * 100:+.2f}%, n={chosen.stats.n}, "
            f"incCLV_max {inc.point:+.4f}±{2 * inc.se:.4f})"
        )

    # ---- profit-label variant (TRAIN-ONLY report, never holdout) -----------
    print("\nPROFIT-LABEL VARIANT (y=won; TRAIN-only report, chosen spec):")
    p_profit = oof_calibrated(train_cand, best_spec, folds, "y_profit")
    maskp = p_profit.notna()
    llp = float(log_loss(train_cand.loc[maskp, "y_profit"], p_profit[maskp], labels=[0.0, 1.0]))
    brp = float(brier_score_loss(train_cand.loc[maskp, "y_profit"], p_profit[maskp]))
    print(
        f"  OOF log-loss {llp:.5f} | brier {brp:.5f} (clv-label model: "
        f"{best_ll:.5f} / {best_br:.5f})"
    )
    oof_df["p_profit"] = p_profit[oof_df.index]
    for pq in (0.45, 0.50, 0.55):
        st = compute_stats(
            f"won-q>={pq:.2f}",
            select_bets(oof_df, "p_profit", pq),
            null_oof,
            args.n_boot_train,
            rng_train,
        )
        print(fmt_stats(st))

    # ---- per-cell threshold control (gate C3), tuned on TRAIN only --------
    cell_thr = tune_cell_thresholds(pool_train)
    dist = pd.Series(list(cell_thr.values())).value_counts().sort_index()
    print("\nPER-(LEAGUE,MARKET) THRESHOLD CONTROL (tuned on TRAIN only):")
    print("  threshold distribution over cells: " + ", ".join(f"{t}:{c}" for t, c in dist.items()))

    # ---- final model: fit 1920-2223, calibrate on 2324 (chronological tail)
    tail = TRAIN_SEASONS[-1]
    fit_df = train_cand[train_cand["season"].isin(TRAIN_SEASONS[:-1]) & train_cand["y"].notna()]
    cal_df = train_cand[(train_cand["season"] == tail) & train_cand["y"].notna()]
    x_fit, cats = prepare_matrix(fit_df)
    final_model = fit_model(best_spec, x_fit, fit_df["y"].to_numpy(dtype=int))
    cal_p = predict_raw(final_model, prepare_matrix(cal_df, cats)[0])
    cal_y = cal_df["y"].to_numpy(dtype=int)
    n_btb = 0
    if args.btb_calib_parquet is not None and args.btb_calib_parquet.exists():
        btb = load_btb_calib(args.btb_calib_parquet, cal_df)
        if not btb.empty:
            bp = predict_raw(final_model, prepare_matrix(btb, cats)[0])
            by = (btb["clv_max"] > 0).to_numpy(dtype=int)
            cal_p = np.concatenate([cal_p, bp])
            cal_y = np.concatenate([cal_y, by])
            n_btb = len(btb)
    cal_kind, cal_obj = fit_calibrator(cal_p, cal_y)
    btb_note = f" + {n_btb} BeatTheBookie consensus rows" if n_btb else ""
    print(
        f"\nFINAL MODEL: {best_spec.name}, fit on {TRAIN_SEASONS[:-1]}, "
        f"{cal_kind} calibration on {tail} (n={len(cal_df)}{btb_note}, "
        f"calib_total={len(cal_y)})"
    )

    print("\nFEATURE IMPORTANCES (final model):")
    for line in importance_table(final_model):
        print(line)

    print("\nRELIABILITY (train OOF, calibrated scores, 10 quantile bins):")
    mask = oof_df["p_cal"].notna() & oof_df["y"].notna()
    lines, ece = reliability_table(
        oof_df.loc[mask, "p_cal"].to_numpy(), oof_df.loc[mask, "y"].to_numpy()
    )
    for line in lines:
        print(line)
    print(f"  ECE {ece:.4f}")

    # ---- artifacts ---------------------------------------------------------
    args.out_dir.mkdir(parents=True, exist_ok=True)
    model_path = args.out_dir / "value_filter_model.txt"
    if isinstance(final_model, lgb.LGBMClassifier):
        final_model.booster_.save_model(str(model_path))
    else:
        model_path.write_text(
            json.dumps(
                {
                    "kind": "logreg",
                    "feature_names": list(final_model.named_steps["pre"].get_feature_names_out()),
                    "coef": final_model.named_steps["lr"].coef_[0].tolist(),
                    "intercept": float(final_model.named_steps["lr"].intercept_[0]),
                },
                indent=1,
            )
        )
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
    manifest: dict[str, Any] = {
        "created_utc": datetime.now(UTC).isoformat(),
        "script": "scripts/ml/train_value_filter.py",
        "dataset": str(args.dataset),
        "dataset_sha256": _sha256(args.dataset),
        "leagues": list(LEAGUES_18),
        "train_seasons": list(TRAIN_SEASONS),
        "test_seasons": list(TEST_SEASONS),
        "min_odds": MIN_ODDS,
        "devig": "differential_margin_weighting (ADR-0006: same method fill+close)",
        "label": "clv_max > 0 (vig-free Max-of-books close)",
        "features": list(FEATURES),
        "seed": SEED,
        "model": {
            "name": best_spec.name,
            "kind": best_spec.kind,
            "params": (LGBM_BASE | best_spec.params)
            if best_spec.kind == "lgbm"
            else best_spec.params,
            "fit_seasons": list(TRAIN_SEASONS[:-1]),
            "calib_season": tail,
            "calibration": cal_kind,
            "calib_n": len(cal_df),
        },
        "calibrator": calibrator_json,
        "oof_metrics": {"log_loss": best_ll, "brier": best_br, "ece": ece},
        "oof_model_selection": [
            {"name": s.name, "log_loss": ll, "brier": br} for s, ll, br, _ in results
        ],
        "operating_point": None
        if chosen is None
        else {
            "q": chosen.q,
            "criterion": "max train-OOF ROI s.t. n>=300 and incCLV_max-2SE>0",
            "train_stats": _stats_dict(chosen.stats),
        },
        "cell_control_thresholds": {f"{k[0]}/{k[1]}": v for k, v in sorted(cell_thr.items())},
        "profit_label_variant_oof": {"log_loss": llp, "brier": brp},
        "holdout": None,
        "verdict": None,
    }

    # ---- ONE-SHOT holdout ---------------------------------------------------
    if args.final and chosen is not None:
        print("\n" + "=" * 78)
        print("ONE-SHOT HELD-OUT EVALUATION — seasons 2425+2526")
        print("This is consultation #4 of this holdout (v3, min-odds, v4 came before);")
        print("declared FINAL. ROI point estimates carry degraded credibility; the")
        print("binding gate metric is incremental CLV vs the Max-of-books close.")
        print("=" * 78)
        test_cand = cand[cand["season"].isin(TEST_SEASONS)].copy()
        test_cand["p_cal"] = apply_calibrator(
            (cal_kind, cal_obj), predict_raw(final_model, prepare_matrix(test_cand, cats)[0])
        )
        null_t = select_bets(pool_test, "edge", 0.0)
        rows: dict[str, SetStats] = {}
        rows["null"] = compute_stats("null thr=0", null_t, None, args.n_boot_final, rng_final)
        vol_sel = select_bets(pool_test, "edge", VOLUME_BASELINE_THR)
        prem_sel = select_bets(pool_test, "edge", PREMIUM_BASELINE_THR)
        ctrl_sel = select_cell_control(pool_test, cell_thr)
        meta_sel = select_bets(test_cand, "p_cal", chosen.q)
        rows["volume"] = compute_stats("edge>=0.015", vol_sel, null_t, args.n_boot_final, rng_final)
        rows["premium"] = compute_stats(
            "edge>=0.03", prem_sel, null_t, args.n_boot_final, rng_final
        )
        rows["control"] = compute_stats(
            "cell-control", ctrl_sel, null_t, args.n_boot_final, rng_final
        )
        rows["meta"] = compute_stats(
            f"META q>={chosen.q:.3f}", meta_sel, null_t, args.n_boot_final, rng_final
        )
        for s in rows.values():
            print(fmt_stats(s))
        print("\nexecution-realism sensitivity (price haircut):")
        print(pinnacle_fill_sensitivity(meta_sel))
        print(pinnacle_fill_sensitivity(prem_sel))

        print("\nRELIABILITY (holdout, calibrated scores):")
        mask_t = test_cand["p_cal"].notna() & test_cand["y"].notna()
        lines_t, ece_t = reliability_table(
            test_cand.loc[mask_t, "p_cal"].to_numpy(), test_cand.loc[mask_t, "y"].to_numpy()
        )
        for line in lines_t:
            print(line)
        print(f"  ECE {ece_t:.4f}")

        meta, vol, ctrl = rows["meta"], rows["volume"], rows["control"]
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
        if meta.n < MIN_HOLDOUT_BETS:
            fails.append(f"C4 held-out n={meta.n} < {MIN_HOLDOUT_BETS}")
        verdict = "ADOPT" if not fails else "REJECT"
        print(f"\nVERDICT (computed, held-out): {verdict}")
        for f_ in fails:
            print(f"  FAILED {f_}")
        if verdict == "ADOPT":
            print(
                "  all four pre-registered criteria passed (C1 strict incCLV_max>2SE, "
                "C2 ROI>=volume baseline, C3 beats cell control, C4 n>=300)"
            )
        manifest["holdout"] = {k: _stats_dict(v) for k, v in rows.items()}
        manifest["holdout"]["reliability_ece"] = ece_t
        manifest["verdict"] = verdict
        manifest["verdict_failed_criteria"] = fails
    elif args.final:
        manifest["verdict"] = "REJECT (no train-qualifying operating point; holdout untouched)"
        print("\nVERDICT: REJECT — no train-qualifying operating point; holdout NOT consulted.")

    manifest_path = args.out_dir / "value_filter_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=1))
    print(f"\nartifacts: {model_path.name}, {manifest_path.name} -> {args.out_dir} (gitignored)")
    print("Decision-support only — picks are informational; this system never places bets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
