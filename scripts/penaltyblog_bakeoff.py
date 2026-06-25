"""Model-agnostic bake-off of penaltyblog's goal models — judged by CLV and
calibration, NEVER by accuracy.

Doctrine (docs/backtesting, .claude/memory, calibration-eval skill): for a
staking system the question is not "how often is the favourite right" but
"are the model's fair probabilities (a) well-CALIBRATED and (b) do bets taken
at the pre-match Pinnacle price beat the Pinnacle CLOSE (positive CLV)".
Accuracy/hit-rate is deliberately absent from the verdict.

What the harness does, for ANY penaltyblog goal-model class:
  1. fit the model on the TRAIN seasons (same leagues/seasons/split as
     scripts/value_backtest.py — 2019/20+ Pinnacle-closing window),
  2. predict held-out 1x2 + over/under 2.5 for every TEST fixture whose teams
     were seen in training (strict no-leak: train rows are strictly prior),
  3. devig the Pinnacle CLOSE (reuse app/probabilities/devig) and compute
     per-pick CLV (reuse app/backtesting/clv.clv_log),
  4. score calibration/reliability (reuse app/backtesting/calibration —
     Brier, log-loss, ECE + reliability bins) over the model fair probs,
  5. report ROI (bet the model's value selections at the Pinnacle pre-match
     price) and n.

The prepped TRAIN/TEST frames are cached to ONE pickle fixture
(data/ml/cache/penaltyblog_bakeoff_frames.pkl) so the per-model runs do NOT
re-fetch football-data.co.uk. Raw CSVs are read from the existing
data/ml/cache/{season}_{league}.csv cache first; only missing files are
fetched gently (paced, tenacity-wrapped, read-only GET).

    uv run python scripts/penaltyblog_bakeoff.py                # all models
    uv run python scripts/penaltyblog_bakeoff.py --models DixonColesGoalModel,PoissonGoalsModel
    uv run python scripts/penaltyblog_bakeoff.py --prep-only    # build fixture, no fit

Decision-support only — nothing here places bets.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import math
import pickle
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

from app.backtesting.calibration import CalibrationObservation, calibration_report
from app.backtesting.clv import clv_log
from app.ingestion.football_data import fetch_season_csv
from app.probabilities.devig import DevigMethod, devig

# Same window value_backtest.py uses: 2019/20+ is the maximal span with full
# Pinnacle pre-match (PSH/P>2.5) AND Pinnacle closing (PSC*/PC*) coverage.
DEFAULT_LEAGUES = ("E0", "E1", "E2", "E3", "SC0", "D1", "D2", "I1", "I2",
                   "SP1", "SP2", "F1", "F2", "N1", "B1", "P1", "T1", "G1")  # fmt: skip
DEFAULT_TRAIN_SEASONS = ("1920", "2021", "2122", "2223", "2324")
DEFAULT_TEST_SEASONS = ("2425", "2526")

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "ml" / "cache"
FIXTURE_PATH = CACHE_DIR / "penaltyblog_bakeoff_frames.pkl"

# Real penaltyblog 1.11.0 goal-model class names (introspected). Bayesian
# models are PyMC-sampled (minutes per fit) — included for completeness but
# OFF by default via DEFAULT_MODELS so a routine run stays fast.
ALL_GOAL_MODELS = (
    "PoissonGoalsModel",
    "DixonColesGoalModel",
    "BivariatePoissonGoalModel",
    "NegativeBinomialGoalModel",
    "WeibullCopulaGoalsModel",
    "ZeroInflatedPoissonGoalsModel",
    "BayesianGoalModel",
    "HierarchicalBayesianGoalModel",
)
# Fast MLE families run by default; the two PyMC Bayesian ones are opt-in.
DEFAULT_MODELS = ALL_GOAL_MODELS[:6]


# --------------------------------------------------------------------------
# Pure helpers (TDD'd in tests/test_penaltyblog_bakeoff.py)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelGrid:
    """A goal model's derived market probabilities for one fixture (the subset
    the bake-off scores). Mirrors penaltyblog's FootballProbabilityGrid."""

    home_win: float
    draw: float
    away_win: float
    over25: float
    under25: float


@dataclass(frozen=True)
class ModelPick:
    """One settled, priced selection for one fixture/market."""

    market: str  # "1x2" | "ou25"
    selection: str  # home|draw|away | over|under
    model_prob: float  # model fair P(selection)
    fill_odds: float  # Pinnacle PRE-MATCH decimal price for the selection
    won: bool
    clv_pinn: float | None  # CLV vs devig(Pinnacle close); None if no close


def _f(x: object) -> float | None:
    try:
        v = float(str(x))
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


# market -> (pre-match Pinnacle odds cols, closing Pinnacle odds cols,
#           ordered selection labels, grid attribute per selection)
_MARKET_SPEC: dict[
    str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]
] = {
    "1x2": (
        ("PSH", "PSD", "PSA"),
        ("PSCH", "PSCD", "PSCA"),
        ("home", "draw", "away"),
        ("home_win", "draw", "away_win"),
    ),
    "ou25": (
        ("P>2.5", "P<2.5"),
        ("PC>2.5", "PC<2.5"),
        ("over", "under"),
        ("over25", "under25"),
    ),
}


def _won_1x2(row: dict[str, str], idx: int) -> bool | None:
    ftr = row.get("FTR")
    if ftr not in ("H", "D", "A"):
        return None
    return ftr == ("H", "D", "A")[idx]


def _won_ou25(row: dict[str, str], idx: int) -> bool | None:
    try:
        goals = int(row["FTHG"]) + int(row["FTAG"])
    except (KeyError, TypeError, ValueError):
        return None
    return (goals >= 3) if idx == 0 else (goals <= 2)


_SETTLE = {"1x2": _won_1x2, "ou25": _won_ou25}


def grid_to_picks(
    row: dict[str, str],
    grid: ModelGrid,
    devig_method: DevigMethod,
    markets: Sequence[str] = ("1x2", "ou25"),
) -> list[ModelPick]:
    """Turn one fixture's model grid + a football-data.co.uk row into one
    ModelPick per selection (all selections, so calibration is unconditional
    over the slate). Bets at the Pinnacle PRE-MATCH price; CLV vs the devigged
    Pinnacle CLOSE. A market with any missing/invalid pre-match price or an
    unsettleable outcome is skipped entirely; a missing close only nulls CLV."""
    out: list[ModelPick] = []
    for market in markets:
        pre_cols, close_cols, labels, attrs = _MARKET_SPEC[market]
        settle = _SETTLE[market]
        pre = [_f(row.get(c)) for c in pre_cols]
        if None in pre or settle(row, 0) is None:
            continue
        close = [_f(row.get(c)) for c in close_cols]
        close_fair = (
            devig([c for c in close if c is not None], method=devig_method)
            if None not in close
            else None
        )
        for i, (label, attr) in enumerate(zip(labels, attrs, strict=True)):
            won = settle(row, i)
            if won is None:
                continue
            fill = pre[i]
            assert fill is not None  # guarded by the None-in-pre check above
            out.append(
                ModelPick(
                    market=market,
                    selection=label,
                    model_prob=float(getattr(grid, attr)),
                    fill_odds=fill,
                    won=won,
                    clv_pinn=(clv_log(fill, close_fair[i]) if close_fair is not None else None),
                )
            )
    return out


def roi(picks: Sequence[ModelPick]) -> float:
    """Realized profit per unit staked: +(odds-1) on a win, -1 on a loss."""
    if not picks:
        return 0.0
    profit = sum((p.fill_odds - 1.0) if p.won else -1.0 for p in picks)
    return profit / len(picks)


# --------------------------------------------------------------------------
# Data prep (cached, paced, read-only GET) — reuses the existing cache layout
# --------------------------------------------------------------------------
async def _load_csv_rows(
    client: httpx.AsyncClient, league: str, season: str
) -> list[dict[str, str]] | None:
    """Cache-first read of one football-data.co.uk season file. Returns None if
    the file is unavailable (so a rate-limit/404 quarantines, never crashes)."""
    cache = CACHE_DIR / f"{season}_{league}.csv"
    if cache.exists():
        text = cache.read_text(encoding="utf-8", errors="replace")
        return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    text = ""
    for attempt in range(4):
        try:
            text = await fetch_season_csv(client, league, season)
            break
        except httpx.HTTPStatusError as exc:  # never log URL/exc text (may carry keys)
            if exc.response.status_code == 404:
                print(f"  quarantine {league} {season}: HTTP 404")
                return None
            print(f"  retry {league} {season}: HTTP {exc.response.status_code} ({attempt + 1}/4)")
            await asyncio.sleep(1.5)
        except httpx.HTTPError as exc:
            print(f"  retry {league} {season}: {type(exc).__name__} ({attempt + 1}/4)")
            await asyncio.sleep(1.5)
    if not text:
        print(f"  quarantine {league} {season}: fetch failed after 4 attempts")
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    await asyncio.sleep(0.3)  # pacing — respect football-data.co.uk
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


@dataclass(frozen=True)
class FitRow:
    """Minimal fit record for a penaltyblog goal model (one historical match)."""

    home_team: str
    away_team: str
    home_goals: int
    away_goals: int


@dataclass
class PrepFrames:
    """Prepped, fixture-cached train/test data. `test_rows` keep the raw CSV
    dicts so per-model runs re-settle + re-CLV against Pinnacle close."""

    leagues: tuple[str, ...]
    train_seasons: tuple[str, ...]
    test_seasons: tuple[str, ...]
    fit_rows: list[FitRow]  # train matches to fit on
    test_rows: list[dict[str, str]]  # held-out raw rows (HomeTeam/AwayTeam + odds)
    cached_files: int  # how many season files were served from disk
    fetched_files: int  # how many had to be fetched
    quarantined: int  # how many were unavailable (rate-limit / 404)


async def prepare_frames(
    leagues: Sequence[str],
    train_seasons: Sequence[str],
    test_seasons: Sequence[str],
) -> PrepFrames:
    """Build (and the caller persists) the train fit-rows + held-out test rows."""
    cached = fetched = quarantined = 0
    fit_rows: list[FitRow] = []
    test_rows: list[dict[str, str]] = []
    async with httpx.AsyncClient() as client:
        for season in (*train_seasons, *test_seasons):
            is_train = season in train_seasons
            for lg in leagues:
                on_disk = (CACHE_DIR / f"{season}_{lg}.csv").exists()
                rows = await _load_csv_rows(client, lg, season)
                if rows is None:
                    quarantined += 1
                    continue
                cached += int(on_disk)
                fetched += int(not on_disk)
                for r in rows:
                    if not r.get("HomeTeam") or not r.get("AwayTeam"):
                        continue
                    if is_train:
                        try:
                            fit_rows.append(
                                FitRow(
                                    home_team=r["HomeTeam"].strip(),
                                    away_team=r["AwayTeam"].strip(),
                                    home_goals=int(r["FTHG"]),
                                    away_goals=int(r["FTAG"]),
                                )
                            )
                        except (KeyError, ValueError):
                            continue
                    else:
                        test_rows.append(r)
    return PrepFrames(
        leagues=tuple(leagues),
        train_seasons=tuple(train_seasons),
        test_seasons=tuple(test_seasons),
        fit_rows=fit_rows,
        test_rows=test_rows,
        cached_files=cached,
        fetched_files=fetched,
        quarantined=quarantined,
    )


def _frames_to_dict(frames: PrepFrames) -> dict[str, object]:
    """Flatten to primitives (tuples/lists/dicts) so the on-disk fixture is
    import-path-INDEPENDENT: pickling the dataclass directly records it under
    the writing module (`__main__` from the CLI), which then fails to load via
    `import scripts.penaltyblog_bakeoff`. Plain containers round-trip anywhere."""
    return {
        "leagues": list(frames.leagues),
        "train_seasons": list(frames.train_seasons),
        "test_seasons": list(frames.test_seasons),
        "fit_rows": [
            (r.home_team, r.away_team, r.home_goals, r.away_goals) for r in frames.fit_rows
        ],
        "test_rows": frames.test_rows,
        "cached_files": frames.cached_files,
        "fetched_files": frames.fetched_files,
        "quarantined": frames.quarantined,
    }


def _frames_from_dict(d: dict[str, object]) -> PrepFrames:
    raw_fit = cast("list[tuple[str, str, int, int]]", d["fit_rows"])
    fit_rows = [FitRow(h, a, hg, ag) for h, a, hg, ag in raw_fit]
    return PrepFrames(
        leagues=tuple(cast("Sequence[str]", d["leagues"])),
        train_seasons=tuple(cast("Sequence[str]", d["train_seasons"])),
        test_seasons=tuple(cast("Sequence[str]", d["test_seasons"])),
        fit_rows=fit_rows,
        test_rows=cast("list[dict[str, str]]", d["test_rows"]),
        cached_files=cast("int", d["cached_files"]),
        fetched_files=cast("int", d["fetched_files"]),
        quarantined=cast("int", d["quarantined"]),
    )


def _load_matching_fixture(
    leagues: Sequence[str],
    train_seasons: Sequence[str],
    test_seasons: Sequence[str],
) -> PrepFrames | None:
    """Return the on-disk fixture iff it matches the requested split, else None."""
    if not FIXTURE_PATH.exists():
        return None
    with FIXTURE_PATH.open("rb") as fh:
        payload: dict[str, object] = pickle.load(fh)  # noqa: S301 — our own fixture
    frames = _frames_from_dict(payload)
    matches = (
        frames.leagues == tuple(leagues)
        and frames.train_seasons == tuple(train_seasons)
        and frames.test_seasons == tuple(test_seasons)
    )
    return frames if matches else None


def _persist_fixture(frames: PrepFrames) -> None:
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FIXTURE_PATH.open("wb") as fh:
        pickle.dump(_frames_to_dict(frames), fh)


async def build_frames_fixture(
    leagues: Sequence[str],
    train_seasons: Sequence[str],
    test_seasons: Sequence[str],
    *,
    rebuild: bool = False,
) -> PrepFrames:
    """Async fixture access: reuse the matching on-disk fixture, else prep and
    persist it. Safe to call from inside a running event loop (the CLI path)."""
    if not rebuild:
        cached = _load_matching_fixture(leagues, train_seasons, test_seasons)
        if cached is not None:
            return cached
    frames = await prepare_frames(leagues, train_seasons, test_seasons)
    _persist_fixture(frames)
    return frames


def load_or_build_frames(
    leagues: Sequence[str],
    train_seasons: Sequence[str],
    test_seasons: Sequence[str],
    *,
    rebuild: bool = False,
) -> PrepFrames:
    """Sync fixture access for per-model backtests run OUTSIDE an event loop.
    Reuses the matching on-disk fixture, else preps + persists it. Must not be
    called from a running loop — use build_frames_fixture there instead."""
    if not rebuild:
        cached = _load_matching_fixture(leagues, train_seasons, test_seasons)
        if cached is not None:
            return cached
    frames = asyncio.run(prepare_frames(leagues, train_seasons, test_seasons))
    _persist_fixture(frames)
    return frames


# --------------------------------------------------------------------------
# Model fit + held-out prediction (model-agnostic)
# --------------------------------------------------------------------------
def _normalize(name: str) -> str:
    return name.strip().lower()


def _grid_from_pb(pb_grid: object) -> ModelGrid | None:
    """Adapt a penaltyblog FootballProbabilityGrid to our ModelGrid (the 5
    market probs the bake-off scores). Returns None on an invalid grid."""
    try:
        return ModelGrid(
            home_win=float(pb_grid.home_win),  # type: ignore[attr-defined]
            draw=float(pb_grid.draw),  # type: ignore[attr-defined]
            away_win=float(pb_grid.away_win),  # type: ignore[attr-defined]
            over25=float(pb_grid.total_goals("over", 2.5)),  # type: ignore[attr-defined]
            under25=float(pb_grid.total_goals("under", 2.5)),  # type: ignore[attr-defined]
        )
    except (ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class BakeoffResult:
    model: str
    fitted: bool
    n: int  # scored selections (calibration n)
    n_value_bets: int  # bets where model_prob > pinnacle pre-match fair (CLV/ROI base)
    clv_pinn: float | None
    clv_pinn_se: float | None
    beat_close_rate: float | None  # fraction of value bets with CLV > 0
    roi: float | None
    brier: float | None
    log_loss: float | None
    ece: float | None
    fit_seconds: float
    note: str


def _value_anchor_fair(row: dict[str, str], devig_method: DevigMethod) -> dict[str, float]:
    """devig(Pinnacle pre-match) fair prob per selection — the value yardstick:
    a 'value bet' is a selection the model prices HIGHER than the sharp
    pre-match line (so its fill price is, by the model, +EV)."""
    fair: dict[str, float] = {}
    for market, (pre_cols, _close, labels, _attrs) in _MARKET_SPEC.items():
        pre = [_f(row.get(c)) for c in pre_cols]
        if None in pre:
            continue
        probs = devig([p for p in pre if p is not None], method=devig_method)
        for label, p in zip(labels, probs, strict=True):
            fair[f"{market}:{label}"] = p
    return fair


def run_model(
    model_name: str,
    frames: PrepFrames,
    devig_method: DevigMethod,
    *,
    value_threshold: float = 0.0,
) -> BakeoffResult:
    """Fit one penaltyblog goal model on the train frames and score it on the
    held-out test rows by CLV + calibration + ROI (NOT accuracy)."""
    from penaltyblog import models as pb_models

    model_cls = getattr(pb_models, model_name)
    fit = frames.fit_rows
    trained_teams = {_normalize(r.home_team) for r in fit} | {_normalize(r.away_team) for r in fit}

    t0 = time.perf_counter()
    model = model_cls(
        goals_home=[r.home_goals for r in fit],
        goals_away=[r.away_goals for r in fit],
        teams_home=[r.home_team for r in fit],
        teams_away=[r.away_team for r in fit],
    )
    try:
        model.fit()
    except (ValueError, RuntimeError) as exc:
        return BakeoffResult(
            model=model_name, fitted=False, n=0, n_value_bets=0, clv_pinn=None,
            clv_pinn_se=None, beat_close_rate=None, roi=None, brier=None,
            log_loss=None, ece=None, fit_seconds=time.perf_counter() - t0,
            note=f"fit failed: {type(exc).__name__}",
        )  # fmt: skip
    fit_seconds = time.perf_counter() - t0

    all_picks: list[ModelPick] = []
    value_picks: list[ModelPick] = []
    skipped_unseen = 0
    for row in frames.test_rows:
        home, away = row.get("HomeTeam", "").strip(), row.get("AwayTeam", "").strip()
        # strict no-leak is structural (train seasons < test seasons); also skip
        # fixtures with a team never seen in training (model can't price them).
        if _normalize(home) not in trained_teams or _normalize(away) not in trained_teams:
            skipped_unseen += 1
            continue
        try:
            pb_grid = model.predict(home, away)
        except (ValueError, KeyError):
            continue
        grid = _grid_from_pb(pb_grid)
        if grid is None:
            continue
        picks = grid_to_picks(row, grid, devig_method)
        all_picks.extend(picks)
        anchor = _value_anchor_fair(row, devig_method)
        for p in picks:
            ref = anchor.get(f"{p.market}:{p.selection}")
            if ref is not None and p.model_prob - ref > value_threshold:
                value_picks.append(p)

    report = calibration_report(
        [CalibrationObservation(fair_prob=p.model_prob, won=p.won) for p in all_picks]
    )
    clvs = [p.clv_pinn for p in value_picks if p.clv_pinn is not None]
    clv_mean = sum(clvs) / len(clvs) if clvs else None
    clv_se = (
        math.sqrt(sum((c - clv_mean) ** 2 for c in clvs) / len(clvs)) / math.sqrt(len(clvs))
        if clvs and clv_mean is not None
        else None
    )
    return BakeoffResult(
        model=model_name,
        fitted=True,
        n=len(all_picks),
        n_value_bets=len(value_picks),
        clv_pinn=clv_mean,
        clv_pinn_se=clv_se,
        beat_close_rate=(sum(1 for c in clvs if c > 0) / len(clvs)) if clvs else None,
        roi=roi(value_picks) if value_picks else None,
        brier=report.brier,
        log_loss=report.log_loss,
        ece=report.ece,
        fit_seconds=fit_seconds,
        note=f"skipped {skipped_unseen} test fixtures with an unseen team",
    )


def _fmt(r: BakeoffResult) -> str:
    if not r.fitted:
        return f"{r.model:<30} | {r.note}"
    clv = f"{r.clv_pinn:+.4f}+/-{2 * (r.clv_pinn_se or 0):.4f}" if r.clv_pinn is not None else "n/a"

    def s(x: float | None, f: str) -> str:
        return format(x, f) if x is not None else "n/a"

    beat = s(r.beat_close_rate, "5.1%") if r.beat_close_rate is not None else "n/a"
    return (
        f"{r.model:<30} | n={r.n:5d} vb={r.n_value_bets:5d} | "
        f"CLVpinn {clv} | beat% {beat:>6} | "
        f"ROI {s(r.roi, '+6.2%')} | Brier {s(r.brier, '.4f')} | "
        f"logloss {s(r.log_loss, '.4f')} | ECE {s(r.ece, '.4f')} | {r.fit_seconds:5.1f}s"
    )


async def _amain() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--leagues", default=",".join(DEFAULT_LEAGUES))
    p.add_argument("--train-seasons", default=",".join(DEFAULT_TRAIN_SEASONS))
    p.add_argument("--test-seasons", default=",".join(DEFAULT_TEST_SEASONS))
    p.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help=f"comma-sep subset of {ALL_GOAL_MODELS}",
    )
    p.add_argument("--devig", default=DevigMethod.POWER.value)
    p.add_argument("--value-threshold", type=float, default=0.0)
    p.add_argument("--rebuild-fixture", action="store_true")
    p.add_argument("--prep-only", action="store_true", help="build the fixture and exit")
    args = p.parse_args()

    leagues = tuple(x.strip() for x in args.leagues.split(",") if x.strip())
    train_s = tuple(x.strip() for x in args.train_seasons.split(",") if x.strip())
    test_s = tuple(x.strip() for x in args.test_seasons.split(",") if x.strip())
    models = [x.strip() for x in args.models.split(",") if x.strip()]
    devig_method = DevigMethod(args.devig)

    print(f"\npenaltyblog GOAL-MODEL BAKE-OFF — {len(leagues)} leagues")
    print(f"TRAIN {train_s} | TEST {test_s} (held out) | devig {devig_method.value}")
    print("Judged by CLV (vs Pinnacle close) + calibration (Brier/logloss/ECE) — NOT accuracy.\n")

    frames = await build_frames_fixture(leagues, train_s, test_s, rebuild=args.rebuild_fixture)
    print(
        f"data: {frames.cached_files} cached, {frames.fetched_files} fetched, "
        f"{frames.quarantined} unavailable | "
        f"train fits {len(frames.fit_rows)} matches | test {len(frames.test_rows)} rows\n"
    )
    if not frames.fit_rows or not frames.test_rows:
        print("INSUFFICIENT DATA (rate-limited / no cache) — no bake-off run.")
        return
    if args.prep_only:
        print(f"fixture written: {FIXTURE_PATH}")
        return

    for name in models:
        result = run_model(name, frames, devig_method, value_threshold=args.value_threshold)
        print(_fmt(result))
    print("\nManual review required. This system does not place bets.")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
