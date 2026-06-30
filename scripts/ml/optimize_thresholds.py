"""Interpretable per-(league-tier x market) threshold CONTROL — no ML.

This is the simpler control any value meta-model MUST beat (adoption gate
criterion 3, docs/backtesting/value-findings.md protocol): a lookup table of
edge thresholds tuned per (league-tier, market) cell on TRAIN seasons only,
evaluated ONE-SHOT on the held-out seasons. If a meta-model cannot beat this
table on held-out incCLV_max at comparable n, its complexity is rejected.

League tiers (documented mapping over the 18-league frozen universe):
  top5      = E0, D1, I1, SP1, F1            (the "Big Five" top divisions —
                                              deepest, sharpest markets)
  secondary = SC0, N1, B1, P1, T1, G1        (other first divisions)
  lower     = E1, E2, E3, D2, I2, SP2, F2    (second tier and below)

PRE-REGISTERED PROTOCOL (frozen before the holdout is touched):
  - TRAIN seasons 1920,2021,2122,2223,2324; HELD-OUT TEST 2425,2526
    (the frozen split of scripts/value_backtest.py).
  - Candidate pool: data/ml/value_candidates.parquet (edge >= 0.005,
    devig = differential_margin_weighting on BOTH fill and close sides,
    ADR-0006). One bet per (match, market) = argmax-edge selection subject to
    edge >= cell threshold AND best_price >= 1.6 (production v4 parity).
  - Per-cell grid: THRESHOLD_GRID below. Eligibility: a threshold is eligible
    for a cell only if it yields >= 150 TRAIN bets in that cell; a cell with
    no eligible threshold falls back to the global 0.03 (production premium).
  - Per-cell objective: maximize TRAIN mean clv_max (CLV vs the Max-of-books
    close — the stricter reference and the adoption-gate currency; CLV first,
    ROI second per doctrine: ROI at n<500 is noise-dominated). Ties break on
    ROI, then larger n.
  - Held-out evaluation is ONE SHOT: pooled n/hit/ROI/CLVpinn/CLVmax,
    incremental CLV vs the thr=0 bet-everything null (same devig, same odds
    floor), with match-clustered bootstrap CIs for ROI and both incCLVs.
    Per-cell/league/market/odds-band tables are DESCRIPTIVE only.
  - The verdict is COMPUTED from held-out numbers (never hardcoded) and can
    print NO PROVEN EDGE.

The thr=0 null cannot be derived from the parquet (pool floor 0.005), so it
is computed from the cached football-data CSVs through the canonical
generator scripts/value_backtest.py::bets_for — reuse, not reimplementation.
A parity check asserts the parquet and the CSVs agree on the global-0.03 row
before any held-out number is reported.

Run (CSVs are cache-first under data/ml/cache/ — read-only GETs):

    uv run python scripts/ml/optimize_thresholds.py

Decision-support only — nothing here places bets.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.util
import json
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from app.probabilities.devig import DevigMethod

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(module_name: str, relative: str) -> Any:
    """Load a scripts/ module by path (scripts/ is not a package)."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod  # register BEFORE exec (dataclass pattern)
    spec.loader.exec_module(mod)
    return mod


vb: Any = _load_script("value_backtest", "scripts/value_backtest.py")
bvd: Any = _load_script("build_value_dataset", "scripts/ml/build_value_dataset.py")

# ---------------------------------------------------------------------------
# Frozen configuration (parity with scripts/value_backtest.py + value-findings)
# ---------------------------------------------------------------------------
TRAIN_SEASONS: tuple[str, ...] = ("1920", "2021", "2122", "2223", "2324")
TEST_SEASONS: tuple[str, ...] = ("2425", "2526")
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
MARKETS: tuple[str, ...] = ("1x2", "ou25")
MIN_ODDS_DEFAULT = 1.6  # production v4 odds floor — required for baseline parity
CANONICAL_DEVIG = DevigMethod.DIFFERENTIAL_MARGIN  # production v4; ADR-0006

TIER_OF_LEAGUE: dict[str, str] = {
    # top5: the "Big Five" European top divisions
    "E0": "top5",
    "D1": "top5",
    "I1": "top5",
    "SP1": "top5",
    "F1": "top5",
    # secondary: other first divisions
    "SC0": "secondary",
    "N1": "secondary",
    "B1": "secondary",
    "P1": "secondary",
    "T1": "secondary",
    "G1": "secondary",
    # lower: second tier and below
    "E1": "lower",
    "E2": "lower",
    "E3": "lower",
    "D2": "lower",
    "I2": "lower",
    "SP2": "lower",
    "F2": "lower",
}
TIERS: tuple[str, ...] = ("top5", "secondary", "lower")

# Grid floor 0.005 = the parquet pool floor (lower thresholds are not
# representable); 0.03 = the production premium threshold; the 0.04 row
# exists only so the min-n guard can be seen binding.
THRESHOLD_GRID: tuple[float, ...] = (0.005, 0.0075, 0.010, 0.015, 0.020, 0.025, 0.030, 0.040)
MIN_CELL_N = 150  # minimum TRAIN bets for a (cell, threshold) to be eligible
FALLBACK_THR = 0.03  # global production premium threshold (v4)
VOLUME_REF_THR = 0.015  # global volume-tier reference, reported for context

MATCH_KEYS: tuple[str, ...] = ("league", "season", "match_date", "home_team", "away_team")
ODDS_BAND_EDGES: tuple[float, ...] = (1.6, 2.0, 3.0, 5.0, float("inf"))

Cell = tuple[str, str]  # (tier, market)
Policy = dict[Cell, float]

OUT_JSON_DEFAULT = REPO_ROOT / "data" / "ml" / "threshold_control_report.json"


# ---------------------------------------------------------------------------
# Tiering + bet selection (pure DataFrame logic — unit-tested synthetically)
# ---------------------------------------------------------------------------
def league_tier(league: str) -> str:
    try:
        return TIER_OF_LEAGUE[league]
    except KeyError:
        raise ValueError(f"league {league!r} has no tier mapping (18-league universe)") from None


def add_tier(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with a `tier` column; unknown leagues are a hard error."""
    unknown = sorted(set(df["league"]) - set(TIER_OF_LEAGUE))
    if unknown:
        raise ValueError(f"leagues without tier mapping: {unknown}")
    out = df.copy()
    out["tier"] = out["league"].map(TIER_OF_LEAGUE)
    return out


def uniform_policy(thr: float) -> Policy:
    return {(tier, market): thr for tier in TIERS for market in MARKETS}


def select_bets(
    df: pd.DataFrame, thresholds: Mapping[Cell, float], min_odds: float
) -> pd.DataFrame:
    """One bet per (match, market): argmax-edge selection subject to
    edge >= threshold[(tier, market)] AND best_price >= min_odds.

    Filter-then-argmax order matches scripts/value_backtest.py::bets_for —
    if the top-edge selection fails the odds floor, the next-best eligible
    selection is bet, not skipped.
    """
    d = df if "tier" in df.columns else add_tier(df)
    cells = set(zip(d["tier"], d["market"], strict=True))
    missing = sorted(cells - set(thresholds))
    if missing:
        raise ValueError(f"policy has no threshold for cells: {missing}")
    thr = pd.Series(
        [thresholds[(t, m)] for t, m in zip(d["tier"], d["market"], strict=True)],
        index=d.index,
        dtype=float,
    )
    d = d[(d["best_price"] >= min_odds) & (d["edge"] >= thr)]
    if d.empty:
        return d
    idx = d.groupby([*MATCH_KEYS, "market"], sort=False)["edge"].idxmax()
    return d.loc[idx]


# ---------------------------------------------------------------------------
# Per-set statistics — math parity with scripts/value_backtest.py::Stats
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Summary:
    n: int
    hit: float
    roi: float
    clv_pinn: float | None
    clv_pinn_se: float | None
    clv_max: float | None
    clv_max_se: float | None
    beat_pinn: float | None
    n_missing_clv_pinn: int
    n_missing_clv_max: int


def _mean_se(xs: np.ndarray) -> tuple[float | None, float | None]:
    """Sample-std (ddof=1) analytic SE — exact parity with Stats.from_bets. The
    SE feeds the >2SE adoption gate, so it must use the UNBIASED sample variance,
    not the population std (which is too small and makes the gate too easy on a
    small sample). n<2 has no defined sample SE -> None (callers treat None as
    not-significant), never a fake-zero SE that mints spurious significance."""
    n = int(xs.size)
    if n == 0:
        return None, None
    m = float(xs.mean())
    if n < 2:
        return m, None
    se = float(xs.std(ddof=1) / math.sqrt(n))
    return m, se


def summarize(bets: pd.DataFrame) -> Summary:
    n = len(bets)
    if n == 0:
        return Summary(0, 0.0, 0.0, None, None, None, None, None, 0, 0)
    cp = bets["clv_pinn"].dropna().to_numpy(dtype=float)
    cm = bets["clv_max"].dropna().to_numpy(dtype=float)
    mp, sp = _mean_se(cp)
    mm, sm = _mean_se(cm)
    return Summary(
        n=n,
        hit=float(bets["won"].mean()),
        roi=float(bets["profit_units"].mean()),
        clv_pinn=mp,
        clv_pinn_se=sp,
        clv_max=mm,
        clv_max_se=sm,
        beat_pinn=float((cp > 0).mean()) if cp.size else None,
        n_missing_clv_pinn=n - cp.size,
        n_missing_clv_max=n - cm.size,
    )


# ---------------------------------------------------------------------------
# TRAIN-only per-cell grid search
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CellChoice:
    tier: str
    market: str
    threshold: float
    source: str  # "optimized" | "fallback"
    train: Summary  # train stats at the chosen threshold


def optimize_cells(
    train: pd.DataFrame,
    grid: tuple[float, ...] = THRESHOLD_GRID,
    min_n: int = MIN_CELL_N,
    fallback_thr: float = FALLBACK_THR,
    min_odds: float = MIN_ODDS_DEFAULT,
) -> tuple[Policy, list[CellChoice], list[tuple[str, str, float, Summary]]]:
    """Grid-search the edge threshold per (tier, market) cell on TRAIN only.

    Eligibility: >= min_n train bets at that threshold in that cell AND a
    computable clv_max mean. Objective (pre-registered): max mean clv_max,
    tie-break ROI, then n. No eligible threshold -> global fallback.
    """
    d = add_tier(train)
    policy: Policy = {}
    choices: list[CellChoice] = []
    audit: list[tuple[str, str, float, Summary]] = []
    for tier in TIERS:
        for market in MARKETS:
            cell_df = d[(d["tier"] == tier) & (d["market"] == market)]
            eligible: list[tuple[float, Summary]] = []
            fallback_summary = Summary(0, 0.0, 0.0, None, None, None, None, None, 0, 0)
            for thr in grid:
                s = summarize(select_bets(cell_df, {(tier, market): thr}, min_odds))
                audit.append((tier, market, thr, s))
                if s.n >= min_n and s.clv_max is not None:
                    eligible.append((thr, s))
                if thr == fallback_thr:
                    fallback_summary = s
            if eligible:
                thr, s = max(eligible, key=lambda ts: (ts[1].clv_max, ts[1].roi, ts[1].n))
                choices.append(CellChoice(tier, market, thr, "optimized", s))
                policy[(tier, market)] = thr
            else:
                choices.append(CellChoice(tier, market, fallback_thr, "fallback", fallback_summary))
                policy[(tier, market)] = fallback_thr
    return policy, choices, audit


# ---------------------------------------------------------------------------
# thr=0 null + parity check — from cached CSVs via the canonical generator
# ---------------------------------------------------------------------------
NULL_COLUMNS: tuple[str, ...] = (
    *MATCH_KEYS,
    "market",
    "best_price",
    "edge",
    "won",
    "profit_units",
    "clv_pinn",
    "clv_max",
)


def null_rows_for_match_rows(
    league: str, season: str, rows: list[dict[str, str]], min_odds: float
) -> list[dict[str, object]]:
    """thr=0 bet-everything bets WITH match identity, via bets_for per row.

    bets_for is called one row at a time purely to retain the (match, market)
    cluster key for the bootstrap — selection logic stays the canonical one.
    """
    out: list[dict[str, object]] = []
    for r in rows:
        home = (r.get("HomeTeam") or "").strip()
        away = (r.get("AwayTeam") or "").strip()
        d = bvd._parse_match_date(r.get("Date"))
        if not home or d is None:
            continue
        for market in MARKETS:
            for b in vb.bets_for([r], 0.0, CANONICAL_DEVIG, (market,), min_odds):
                out.append(
                    {
                        "league": league,
                        "season": season,
                        "match_date": d,
                        "home_team": home,
                        "away_team": away,
                        "market": market,
                        "best_price": b.odds,
                        "edge": b.edge,
                        "won": b.won,
                        "profit_units": (b.odds - 1.0) if b.won else -1.0,
                        "clv_pinn": b.clv_pinn,
                        "clv_max": b.clv_max,
                    }
                )
    return out


async def _load_cached_rows(
    leagues: tuple[str, ...], seasons: tuple[str, ...], cache_dir: Path
) -> dict[tuple[str, str], list[dict[str, str]]]:
    out: dict[tuple[str, str], list[dict[str, str]]] = {}
    async with httpx.AsyncClient() as client:
        for league in leagues:
            for season in seasons:
                rows = await bvd._load_csv(client, cache_dir, league, season)
                if rows is not None:
                    out[(league, season)] = rows
    return out


def build_null_bets(
    csv_rows: dict[tuple[str, str], list[dict[str, str]]], min_odds: float
) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for (league, season), rows in csv_rows.items():
        records.extend(null_rows_for_match_rows(league, season, rows, min_odds))
    return pd.DataFrame(records, columns=list(NULL_COLUMNS))


def parity_check(
    test_pool: pd.DataFrame,
    csv_rows: dict[tuple[str, str], list[dict[str, str]]],
    min_odds: float,
) -> None:
    """The parquet pool and the cached CSVs must agree on the global-0.03 row.

    Catches snapshot drift between data/ml/value_candidates.parquet and
    data/ml/cache/ before any held-out number is reported. Hard failure —
    never silently swallowed.
    """
    from_parquet = summarize(select_bets(test_pool, uniform_policy(FALLBACK_THR), min_odds))
    all_rows = [r for rows in csv_rows.values() for r in rows]
    from_csv = vb.Stats.from_bets(
        vb.bets_for(all_rows, FALLBACK_THR, CANONICAL_DEVIG, MARKETS, min_odds)
    )
    if from_parquet.n != from_csv.n or abs(from_parquet.roi - from_csv.roi) > 1e-6:
        raise SystemExit(
            "PARITY FAILURE: parquet pool vs cached CSVs disagree on the global-0.03 row "
            f"(parquet n={from_parquet.n} roi={from_parquet.roi:+.4f}; "
            f"csv n={from_csv.n} roi={from_csv.roi:+.4f}). Rebuild the dataset with "
            "scripts/ml/build_value_dataset.py before trusting any number here."
        )


# ---------------------------------------------------------------------------
# Match-clustered bootstrap (charter: never average ROI without a CI)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BootCI:
    point: float
    lo: float
    hi: float
    se: float


_AGG_COLS = ("nbet", "profit", "clvp_sum", "clvp_n", "clvm_sum", "clvm_n")


def _aggregate_by_match(bets: pd.DataFrame) -> pd.DataFrame:
    g = pd.DataFrame(
        {
            "nbet": 1.0,
            "profit": bets["profit_units"].astype(float),
            "clvp_sum": bets["clv_pinn"].fillna(0.0).astype(float),
            "clvp_n": bets["clv_pinn"].notna().astype(float),
            "clvm_sum": bets["clv_max"].fillna(0.0).astype(float),
            "clvm_n": bets["clv_max"].notna().astype(float),
        },
        index=bets.index,
    )
    for k in MATCH_KEYS:
        g[k] = bets[k]
    return g.groupby(list(MATCH_KEYS), sort=False)[list(_AGG_COLS)].sum()


def _metrics_from_sums(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
    """a = policy per-cluster sums, b = null per-cluster sums (len-6 each)."""
    nan = float("nan")
    roi = a[1] / a[0] if a[0] > 0 else nan
    clvp = a[2] / a[3] if a[3] > 0 else nan
    clvm = a[4] / a[5] if a[5] > 0 else nan
    null_clvp = b[2] / b[3] if b[3] > 0 else nan
    null_clvm = b[4] / b[5] if b[5] > 0 else nan
    return {
        "roi": roi,
        "clv_pinn": clvp,
        "clv_max": clvm,
        "inc_clv_pinn": clvp - null_clvp,
        "inc_clv_max": clvm - null_clvm,
    }


def bootstrap_cis(
    policy_bets: pd.DataFrame,
    null_bets: pd.DataFrame,
    n_boot: int = 2000,
    seed: int = 20260612,
) -> dict[str, BootCI]:
    """95% percentile CIs, resampling CLUSTERED BY MATCH (a 1x2 and an ou25
    bet on the same match are correlated and must move together)."""
    a_tbl = _aggregate_by_match(policy_bets)
    b_tbl = _aggregate_by_match(null_bets)
    index = a_tbl.index.union(b_tbl.index)
    a_mat = a_tbl.reindex(index).fillna(0.0).to_numpy(dtype=float)
    b_mat = b_tbl.reindex(index).fillna(0.0).to_numpy(dtype=float)
    m = len(index)
    if m == 0:
        raise ValueError("bootstrap needs at least one match cluster")
    points = _metrics_from_sums(a_mat.sum(axis=0), b_mat.sum(axis=0))
    rng = np.random.default_rng(seed)
    draws: dict[str, list[float]] = {k: [] for k in points}
    for _ in range(n_boot):
        idx = rng.integers(0, m, m)
        sample = _metrics_from_sums(a_mat[idx].sum(axis=0), b_mat[idx].sum(axis=0))
        for k, v in sample.items():
            draws[k].append(v)
    out: dict[str, BootCI] = {}
    for k, point in points.items():
        arr = np.asarray(draws[k], dtype=float)
        out[k] = BootCI(
            point=float(point),
            lo=float(np.nanpercentile(arr, 2.5)),
            hi=float(np.nanpercentile(arr, 97.5)),
            se=float(np.nanstd(arr)),
        )
    return out


# ---------------------------------------------------------------------------
# Execution-realism sensitivity: re-price the SAME selected bets at Pinnacle
# ---------------------------------------------------------------------------
def haircut_at_pinnacle(bets: pd.DataFrame) -> pd.DataFrame:
    """Price-haircut row: identical selections, filled at the Pinnacle
    pre-match price instead of the Max line (no line shopping at all).
    CLV recomputed as ln(fill * close_fair) per app/backtesting/clv.py."""
    out = bets.copy()
    fill = out["pinn_price"].astype(float)
    out["profit_units"] = np.where(out["won"], fill - 1.0, -1.0)
    out["clv_pinn"] = np.log(fill * out["pinn_close_fair"].astype(float))
    out["clv_max"] = np.log(fill * out["max_close_fair"].astype(float))
    out["best_price"] = fill
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _fmt_row(label: str, s: Summary, null: Summary | None = None) -> str:
    """Row format parity with scripts/value_backtest.py::_fmt (+ incCLVmax)."""
    if s.n == 0:
        return f"{label:>24} | (no bets)"
    cp = f"{s.clv_pinn:+.4f}+/-{2 * (s.clv_pinn_se or 0):.4f}" if s.clv_pinn is not None else "n/a"
    cm = f"{s.clv_max:+.4f}+/-{2 * (s.clv_max_se or 0):.4f}" if s.clv_max is not None else "n/a"
    inc = ""
    if null is not None and null.clv_pinn is not None and s.clv_pinn is not None:
        inc = f" | incCLVp {s.clv_pinn - null.clv_pinn:+.4f}"
        if null.clv_max is not None and s.clv_max is not None:
            inc += f" incCLVm {s.clv_max - null.clv_max:+.4f}"
    return (
        f"{label:>24} | n={s.n:5d} | hit {s.hit * 100:4.1f}% | ROI {s.roi * 100:+6.2f}% | "
        f"CLVpinn {cp} | CLVmax {cm}{inc}"
    )


def compute_verdict(s: Summary, null: Summary, cis: dict[str, BootCI]) -> str:
    """Same computed-verdict logic as scripts/value_backtest.py, extended with
    the strict adoption-gate criterion (incCLV vs the Max-of-books close
    > 2*bootstrap-SE above zero). Never hardcoded; can say NO PROVEN EDGE."""
    if s.n < 50 or s.clv_pinn is None or null.clv_pinn is None or s.clv_pinn_se is None:
        return "NO PROVEN EDGE on held-out data (insufficient sample or missing CLV)"
    inc_p = s.clv_pinn - null.clv_pinn
    strict = cis["inc_clv_max"]
    strict_pass = strict.point - 2 * strict.se > 0
    if inc_p - 2 * s.clv_pinn_se > 0 and s.roi > 0:
        tail = (
            "and clears the STRICT gate (incCLV vs Max-of-books close > 2SE)"
            if strict_pass
            else "but FAILS the strict gate (incCLV vs Max-of-books close not > 2SE — "
            "selection skill beyond the best-price premium is unproven)"
        )
        return (
            f"POSITIVE selection skill on held-out data: incremental CLVpinn {inc_p:+.4f} "
            f"(>2SE), ROI {s.roi * 100:+.2f}% — {tail}"
        )
    if s.roi > 0:
        return (
            f"ROI positive ({s.roi * 100:+.2f}%) but incremental CLVpinn {inc_p:+.4f} "
            "not conclusively above the bet-everything baseline"
        )
    return "NO PROVEN EDGE on held-out data"


def _ci_str(ci: BootCI) -> str:
    return f"{ci.point:+.4f} [95% CI {ci.lo:+.4f}, {ci.hi:+.4f}; boot SE {ci.se:.4f}]"


def _breakdown(bets: pd.DataFrame, by: str, title: str) -> None:
    print(f"\n  {title} (descriptive only — no adoption decision reads these):")
    if bets.empty:
        print("    (no bets)")
        return
    for key, grp in bets.groupby(by, observed=True, sort=True):
        print(_fmt_row(f"  {key}", summarize(grp)))


def _policy_json(policy: Policy) -> dict[str, float]:
    return {f"{tier}/{market}": thr for (tier, market), thr in sorted(policy.items())}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dataset", type=Path, default=REPO_ROOT / "data" / "ml" / "value_candidates.parquet"
    )
    p.add_argument("--cache-dir", type=Path, default=REPO_ROOT / "data" / "ml" / "cache")
    p.add_argument("--min-odds", type=float, default=MIN_ODDS_DEFAULT)
    p.add_argument("--n-boot", type=int, default=2000)
    p.add_argument("--seed", type=int, default=20260612)
    p.add_argument("--out-json", type=Path, default=OUT_JSON_DEFAULT)
    args = p.parse_args(argv)
    min_odds = float(args.min_odds)
    if min_odds != MIN_ODDS_DEFAULT:
        print(f"WARNING: --min-odds {min_odds} differs from production 1.6 — no baseline parity.")

    df = pd.read_parquet(args.dataset)
    df = df[df["league"].isin(LEAGUES_18) & df["market"].isin(MARKETS)]
    train = df[df["season"].isin(TRAIN_SEASONS)].copy()
    test = df[df["season"].isin(TEST_SEASONS)].copy()
    for name, part in (("train", train), ("test", test)):
        eras = set(part["era"].unique())
        if eras != {"maxavg"}:
            raise SystemExit(f"DATA-QUALITY FAILURE: {name} pool has unexpected eras {eras}")

    print("\nTHRESHOLD CONTROL — per-(league-tier x market) edge thresholds, no ML")
    print(
        f"pool: {args.dataset.name} | {len(LEAGUES_18)} leagues | "
        f"markets {MARKETS} | min_odds {min_odds}"
    )
    print(
        f"TRAIN {list(TRAIN_SEASONS)} ({len(train)} candidates) | "
        f"HELD-OUT {list(TEST_SEASONS)} ({len(test)} candidates)"
    )
    print(f"devig {CANONICAL_DEVIG.value} on fill AND close sides (ADR-0006)")
    print(
        "tiers: top5={E0,D1,I1,SP1,F1} secondary={SC0,N1,B1,P1,T1,G1} lower={E1,E2,E3,D2,I2,SP2,F2}"
    )
    print(
        f"pre-registered: grid {THRESHOLD_GRID}; eligibility n_train>={MIN_CELL_N} per cell "
        f"else fallback {FALLBACK_THR}; objective max train mean CLVmax, ties ROI then n"
    )
    print(
        "HOLDOUT CONSULTATION #4 (after v3, min-odds re-run, v4) — declared FINAL for "
        "the threshold-control policy. ROI point estimates are degraded by prior "
        "consultations; CLV with CIs is the planning number."
    )

    print("\nTRAIN per-cell grid (eligible = n>=150):")
    policy, choices, audit = optimize_cells(
        train, THRESHOLD_GRID, MIN_CELL_N, FALLBACK_THR, min_odds
    )
    chosen_optimized = {(c.tier, c.market): c.threshold for c in choices if c.source == "optimized"}
    for tier, market, thr, s in audit:
        mark = " <- chosen" if chosen_optimized.get((tier, market)) == thr else ""
        flag = "" if s.n >= MIN_CELL_N else " (ineligible)"
        print(_fmt_row(f"{tier}/{market}/{thr:.4f}", s) + flag + mark)
    print("\nchosen policy (TRAIN-only decisions):")
    for c in choices:
        cm = "n/a" if c.train.clv_max is None else f"{c.train.clv_max:+.4f}"
        print(
            f"  {c.tier:>9}/{c.market:<4} thr={c.threshold:.4f} [{c.source}] "
            f"train n={c.train.n} ROI {c.train.roi * 100:+.2f}% CLVmax {cm}"
        )

    # ---------------------------------------------------------------- held-out
    print("\nHELD-OUT TEST evaluation (single shot, never tuned on):")
    csv_rows = asyncio.run(_load_cached_rows(LEAGUES_18, TEST_SEASONS, args.cache_dir))
    parity_check(test, csv_rows, min_odds)
    print("parity check passed: parquet pool == cached CSVs on the global-0.03 row")

    null_bets = build_null_bets(csv_rows, min_odds)
    null_s = summarize(null_bets)
    composite = select_bets(test, policy, min_odds)
    composite_s = summarize(composite)
    premium = select_bets(test, uniform_policy(FALLBACK_THR), min_odds)
    premium_s = summarize(premium)
    volume = select_bets(test, uniform_policy(VOLUME_REF_THR), min_odds)
    volume_s = summarize(volume)
    haircut = haircut_at_pinnacle(composite) if not composite.empty else composite
    haircut_s = summarize(haircut)

    print(_fmt_row("null thr=0 (all bets)", null_s))
    print(_fmt_row("COMPOSITE per-cell", composite_s, null_s))
    print(_fmt_row(f"global {FALLBACK_THR} premium", premium_s, null_s))
    print(_fmt_row(f"global {VOLUME_REF_THR} volume", volume_s, null_s))
    print(_fmt_row("composite @Pinn fill", haircut_s, null_s))
    print(
        "  (volume reference uses the canonical devig at min_odds "
        f"{min_odds}; the historical v2 volume row was power/1x2-only — context only.\n"
        "   composite @Pinn fill = same picks priced at Pinnacle pre-match: the "
        "no-line-shopping execution haircut.)"
    )
    if composite_s.n_missing_clv_pinn or composite_s.n_missing_clv_max:
        print(
            f"  composite bets missing close refs: clv_pinn {composite_s.n_missing_clv_pinn}, "
            f"clv_max {composite_s.n_missing_clv_max} (kept for ROI, excluded from CLV means)"
        )
    print(
        f"  null bets missing close refs: clv_pinn {null_s.n_missing_clv_pinn}, "
        f"clv_max {null_s.n_missing_clv_max} of n={null_s.n}"
    )

    print(f"\nmatch-clustered bootstrap ({args.n_boot} draws, seed {args.seed}):")
    cis = bootstrap_cis(composite, null_bets, args.n_boot, args.seed)
    for k in ("roi", "clv_pinn", "clv_max", "inc_clv_pinn", "inc_clv_max"):
        print(f"  composite {k:>13}: {_ci_str(cis[k])}")
    premium_cis = bootstrap_cis(premium, null_bets, args.n_boot, args.seed)
    volume_cis = bootstrap_cis(volume, null_bets, args.n_boot, args.seed)
    for label, ref in (("premium", premium_cis), ("volume", volume_cis)):
        print(
            f"  {label:>9} reference: ROI {_ci_str(ref['roi'])} | "
            f"incCLVmax {_ci_str(ref['inc_clv_max'])}"
        )

    comp_band = add_tier(composite)
    comp_band["odds_band"] = pd.cut(
        comp_band["best_price"], bins=list(ODDS_BAND_EDGES), right=False
    ).astype(str)
    comp_band["cell"] = comp_band["tier"] + "/" + comp_band["market"]
    _breakdown(comp_band, "cell", "held-out composite per cell")
    _breakdown(comp_band, "market", "held-out composite per market")
    _breakdown(comp_band, "league", "held-out composite per league")
    _breakdown(comp_band, "odds_band", "held-out composite per odds band")

    verdict = compute_verdict(composite_s, null_s, cis)
    strict = cis["inc_clv_max"]
    strict_pass = strict.point - 2 * strict.se > 0
    print("\nSTRICT GATE incCLVmax (vs Max-of-books close, match-clustered bootstrap):")
    print(
        f"  {_ci_str(strict)} -> "
        + ("PASS (point - 2*SE > 0)" if strict_pass else "FAIL (point - 2*SE <= 0)")
    )
    print(f"\nVERDICT (computed): {verdict}")
    print(
        "\nCaveats: Max line assumes line-shopping every book at snapshot time; soft "
        "books limit winners; the @Pinn-fill row above is the no-shopping haircut."
    )
    print("Manual review required. This system does not place bets.")

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "config": {
            "dataset": str(args.dataset),
            "train_seasons": list(TRAIN_SEASONS),
            "test_seasons": list(TEST_SEASONS),
            "leagues": list(LEAGUES_18),
            "markets": list(MARKETS),
            "min_odds": min_odds,
            "devig": CANONICAL_DEVIG.value,
            "grid": list(THRESHOLD_GRID),
            "min_cell_n": MIN_CELL_N,
            "fallback_thr": FALLBACK_THR,
            "objective": "max train mean clv_max; ties ROI then n",
            "n_boot": args.n_boot,
            "seed": args.seed,
            "holdout_consultation": "FINAL (#4 after v3, min-odds re-run, v4)",
        },
        "policy": _policy_json(policy),
        "cell_choices": [
            {
                "tier": c.tier,
                "market": c.market,
                "threshold": c.threshold,
                "source": c.source,
                "train": dataclasses.asdict(c.train),
            }
            for c in choices
        ],
        "held_out": {
            "null": dataclasses.asdict(null_s),
            "composite": dataclasses.asdict(composite_s),
            "premium_ref": dataclasses.asdict(premium_s),
            "volume_ref": dataclasses.asdict(volume_s),
            "haircut_pinn_fill": dataclasses.asdict(haircut_s),
            "composite_bootstrap": {k: dataclasses.asdict(v) for k, v in cis.items()},
            "premium_bootstrap": {k: dataclasses.asdict(v) for k, v in premium_cis.items()},
            "volume_bootstrap": {k: dataclasses.asdict(v) for k, v in volume_cis.items()},
            "verdict": verdict,
            "strict_gate_pass": bool(strict_pass),
        },
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote control artifact -> {args.out_json}")
    print("Decision-support only — nothing here places bets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
