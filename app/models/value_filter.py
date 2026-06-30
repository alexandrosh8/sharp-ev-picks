"""Value-filter META-MODEL inference (meta-labeling, verdict ADOPT 2026-06-12).

Doctrine (docs/backtesting/value-findings.md, .claude/memory): the edge is
sharp-vs-soft line shopping; ML winner-prediction from team stats backtested
NEGATIVE and is forbidden as a strategy. This module is the SECONDARY
classifier of a meta-labeling pair (Lopez de Prado): the deterministic value
pipeline generates candidates; this model scores P(candidate beats the
vig-free Max-of-books close) and, when VALUE_ML_FILTER is enabled, demotes
premium candidates scoring below the frozen operating point to the volume
(shadow) tier. It never predicts match outcomes and never places bets.

Held-out evidence (one-shot, seasons 2425+2526 — docs/research/
ml-value-filter.md): META q>=0.725 n=396, ROI +12.0%, incremental CLV vs the
Max close +0.0357 ± 0.0075 (2SE); all four pre-registered adoption criteria
passed. Trained by scripts/ml/train_value_filter.py; artifacts live OUTSIDE
git (data/ml/ is gitignored) and are loaded from a configurable directory.

Dependency policy: importing this module needs core deps only (numpy/stdlib).
lightgbm and pandas are imported lazily inside `ValueFilterModel.load`/
`score`; when they are absent the loader returns None and the pipeline runs
exactly as before — the `ml` extra stays optional.

Shadow candidates (v2, 2026-06-12): a manifest whose verdict is NOT "ADOPT"
(e.g. the v2 trainer's "CANDIDATE..." — seasons 2425+2526 are SPENT, so v2
cannot honestly claim ADOPT before live shadow CLV + the fresh 2627 season)
is refused by default. With `allow_shadow=True` (Settings.
value_ml_manifest_allow_shadow / VALUE_ML_MANIFEST_ALLOW_SHADOW) it loads
with `shadow=True`: scores ANNOTATE picks for live shadow evidence, but the
demotion path (app/pipeline.py) and the composition root (app/scheduler.py)
both refuse to enforce on a shadow model — enforcement always requires a
true ADOPT manifest.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from app.edge.value import CONSENSUS_ANCHOR
from app.probabilities.devig import DevigMethod, devig
from app.schemas.base import Market

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "value_filter_manifest.json"
MODEL_FILENAME = "value_filter_model.txt"

# Categorical model inputs (parity with scripts/ml/train_value_filter.py
# FEATURES_CAT). The manifest carries the full ordered feature list; this set
# only marks which of them must be pandas-categorical at predict time.
CATEGORICAL_FEATURES = frozenset({"league", "market", "selection_type"})

# Disagreement-spread feature methods — parity with
# scripts/ml/build_value_dataset.py SPREAD_METHODS.
_SPREAD_METHODS = (DevigMethod.MULTIPLICATIVE, DevigMethod.SHIN, DevigMethod.POWER)

# The model was trained on football-data.co.uk league codes (18 leagues).
# Live league labels are oddsportal slugs (config) or scraped display names
# (EventDirectory). Best-effort normalization: keys are lowercased with
# non-alphanumerics stripped. UNMAPPED leagues are out of scope -> unscored
# (None), never guessed — the trained vocabulary does not transfer.
_LEAGUE_CODES: dict[str, str] = {
    # oddsportal slugs (app/config.py / register_extra_leagues)
    "englandpremierleague": "E0",
    "englandchampionship": "E1",
    "englandleagueone": "E2",
    "englandleaguetwo": "E3",
    "scotlandpremiership": "SC0",
    "germanybundesliga": "D1",
    "germany2bundesliga": "D2",
    "italyseriea": "I1",
    "italyserieb": "I2",
    "spainlaliga": "SP1",
    "spainlaliga2": "SP2",
    "franceligue1": "F1",
    "franceligue2": "F2",
    "netherlandseredivisie": "N1",
    "belgiumjupilerproleague": "B1",
    "portugalligaportugal": "P1",
    "turkeysuperlig": "T1",
    "greecesuperleague": "G1",
    # oddsportal scraped display names (league_name)
    "premierleague": "E0",
    "championship": "E1",
    "leagueone": "E2",
    "leaguetwo": "E3",
    "premiership": "SC0",
    "bundesliga": "D1",
    "2bundesliga": "D2",
    "seriea": "I1",
    "serieb": "I2",
    "laliga": "SP1",
    "laliga2": "SP2",
    "ligue1": "F1",
    "ligue2": "F2",
    "eredivisie": "N1",
    "jupilerproleague": "B1",
    "ligaportugal": "P1",
    "superlig": "T1",
    "superleague": "G1",
}


def league_code(label: str) -> str | None:
    """football-data league code for a live league label, or None (unscored)."""
    key = "".join(ch for ch in label.lower() if ch.isalnum())
    return _LEAGUE_CODES.get(key)


def _season_end(d: date) -> date:
    """Nominal European season end — June 30 convention, parity with the trainer's
    season-LABEL basis (build_value_dataset._season_end). The season runs Aug-Jun,
    so JULY belongs to the just-ended season (its June 30, this year) — matching how
    the football-data season files label extended/late fixtures. Only Aug-Dec roll
    to next year's June 30 (audit #9: the old <=6 cutoff sign-flipped July)."""
    return date(d.year if d.month <= 7 else d.year + 1, 6, 30)


def calibrate(calibrator: Mapping[str, Any], p_raw: np.ndarray) -> np.ndarray:
    """Apply the manifest's calibrator — clean-room replay of the trainer's
    apply_calibrator using the serialized parameters only (no sklearn).

    isotonic: piecewise-linear interpolation over (x_thresholds,
    y_thresholds); np.interp clamps to the end values, equivalent to
    IsotonicRegression(out_of_bounds="clip"). platt: sigmoid(coef*p + b).
    beta (Kull/Silva-Filho/Flach, AISTATS 2017): sigmoid(a*ln(p) - b*ln(1-p) + c)
    — the 3-parameter map; p is clipped off {0,1} before the logs.
    """
    kind = calibrator.get("kind")
    if kind == "isotonic":
        x = np.asarray(calibrator["x_thresholds"], dtype=float)
        y = np.asarray(calibrator["y_thresholds"], dtype=float)
        p = np.interp(p_raw, x, y)
    elif kind == "platt":
        z = float(calibrator["coef"]) * p_raw + float(calibrator["intercept"])
        p = 1.0 / (1.0 + np.exp(-z))
    elif kind == "beta":
        pr = np.clip(p_raw, 1e-6, 1.0 - 1e-6)
        z = (
            float(calibrator["a"]) * np.log(pr)
            - float(calibrator["b"]) * np.log(1.0 - pr)
            + float(calibrator["c"])
        )
        p = 1.0 / (1.0 + np.exp(-z))
    else:
        raise ValueError(f"unknown calibrator kind: {kind!r}")
    return np.clip(p, 1e-6, 1.0 - 1e-6)


def _norm_book(s: str) -> str:
    return s.strip().lower()


def _market_key(market: Market, market_detail: str | None, n_selections: int) -> str | None:
    """Trained market key, or None when out of the model's scope.

    The model saw exactly two markets: football 1x2 (3-way h2h) and
    over/under 2.5 goals. Everything else (btts, dnb, handicaps, other
    totals lines, 2-way basketball h2h) is unscored by design.
    """
    if market is Market.H2H and n_selections == 3:
        return "1x2"
    if market is Market.TOTALS and market_detail == "over_under_2_5":
        return "ou25"
    return None


def _selection_type(key: str, selection: str, fair_by_sel: Mapping[str, float]) -> str:
    """fav/draw/dog by fair-prob rank — parity with the dataset builder."""
    fair = fair_by_sel[selection]
    if key == "1x2":
        if selection == "Draw":
            return "draw"
        others = [p for s, p in fair_by_sel.items() if s not in (selection, "Draw")]
        return "fav" if not others or fair >= max(others) else "dog"
    return "fav" if fair >= max(fair_by_sel.values()) else "dog"


def live_features(
    *,
    market: Market,
    market_detail: str | None,
    selection: str,
    prices: Mapping[str, Mapping[str, float]],
    fair_by_sel: Mapping[str, float],
    anchor_book: str,
    league: str,
    kickoff_utc: datetime | None,
    now: datetime,
    min_odds: float,
) -> dict[str, Any] | None:
    """Model features for one live value candidate, or None when out of scope.

    Scope gates (each mirrors a property of the TRAINING distribution; an
    out-of-scope candidate passes through the pipeline UNSCORED — the filter
    must never veto markets the model has not seen):
      - market must map to a trained key (1x2 / ou25);
      - the anchor must be a NAMED sharp book (the trained fair side is
        devig(Pinnacle pre-match)); consensus-median anchors are unscored;
      - the league must map to a trained football-data code;
      - the candidate's best price must clear the manifest's odds floor.

    Feature semantics replay build_value_dataset.candidates_from_rows on the
    live snapshot: per-selection best = max raw price across ALL books (the
    dataset's Max columns include Pinnacle); edge = fair - 1/best (raw — the
    live pipeline's commission-netted edge is the PICK gate, not the model
    input). day_of_week/days_to_season_end use the UTC kickoff date (the
    dataset used UK-local dates; <=1h boundary drift, never a leak).
    """
    key = _market_key(market, market_detail, len(fair_by_sel))
    if key is None:
        return None
    if anchor_book == CONSENSUS_ANCHOR:
        return None
    code = league_code(league)
    if code is None:
        return None
    if selection not in fair_by_sel:
        return None

    selections = list(fair_by_sel)
    anchor_norm = _norm_book(anchor_book)
    anchor_odds: list[float] = []
    best_odds: list[float] = []
    full_market_books: set[str] | None = None
    for sel in selections:
        book_odds = prices.get(sel) or {}
        a = next((o for b, o in book_odds.items() if _norm_book(b) == anchor_norm), None)
        valid = {b: o for b, o in book_odds.items() if o > 1.0}
        if a is None or not valid:
            return None  # anchor or market side missing — cannot price features
        anchor_odds.append(a)
        best_odds.append(max(valid.values()))
        names = {_norm_book(b) for b in valid}
        full_market_books = names if full_market_books is None else full_market_books & names

    i = selections.index(selection)
    if best_odds[i] < min_odds:
        return None

    edges = [fair_by_sel[s] - 1.0 / best_odds[j] for j, s in enumerate(selections)]
    spreads = [devig(anchor_odds, method=m) for m in _SPREAD_METHODS]
    devig_spread = max(abs(a[i] - b[i]) for a in spreads for b in spreads)

    d = (kickoff_utc or now).astimezone(UTC).date()
    return {
        "edge": edges[i],
        "fair_prob": fair_by_sel[selection],
        "best_price": best_odds[i],
        "pinn_price": anchor_odds[i],
        "overround_pinn": sum(1.0 / o for o in anchor_odds) - 1.0,
        "overround_best": sum(1.0 / o for o in best_odds) - 1.0,
        "devig_spread": devig_spread,
        "book_count": len(full_market_books or set()),
        "day_of_week": d.weekday(),
        "days_to_season_end": (_season_end(d) - d).days,
        # SINGLE-winner argmax (first max index) — parity with the trainer
        # (build_value_dataset argmax_i + i==argmax_i). `>= max(edges)` marked
        # EVERY tied selection as the argmax; train marks exactly one (audit #8).
        "is_argmax_edge": i == max(range(len(edges)), key=lambda j: edges[j]),
        "league": code,
        "market": key,
        "selection_type": _selection_type(key, selection, fair_by_sel),
    }


def manifest_operating_point(
    model_dir: Path, manifest_filename: str = MANIFEST_FILENAME
) -> float | None:
    """The manifest's frozen operating point q*, WITHOUT loading the booster
    (no lightgbm needed) — for score-bucket stratification in reports
    (GET /performance live evidence). Accepts ANY verdict: stratifying
    accumulated shadow scores is annotation, never enforcement — demotion
    still goes through ValueFilterModel.load's ADOPT gate. None on any
    failure (missing/unreadable manifest, no operating point)."""
    manifest_path = model_dir / manifest_filename
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("value-filter manifest unavailable for reports: %s", type(exc).__name__)
        return None
    q = (manifest.get("operating_point") or {}).get("q")
    return float(q) if q is not None else None


# Pre-registered holdout adoption criteria, frozen in
# scripts/ml/train_value_filter.py BEFORE the one-shot holdout was consulted:
# C1 strict incremental CLV vs the Max-of-books close > 2*SE above zero, C2 ROI
# >= the volume baseline, C3 beats the per-(league,market) threshold control on
# incremental CLV, C4 held-out n >= this floor. The number must match the
# trainer (MIN_HOLDOUT_BETS) — it is the C4 gate.
_MIN_HOLDOUT_BETS = 300


def _adopt_confirmed_by_holdout(manifest: Mapping[str, Any]) -> tuple[bool, str]:
    """Re-derive the ADOPT verdict from the manifest's OWN recorded holdout
    stats, so a self-declared ``verdict == "ADOPT"`` is trusted only when the
    numbers behind it still pass every pre-registered criterion.

    Returns ``(True, "")`` when all four criteria pass, else ``(False, reason)``.
    Fail-closed: a missing or malformed holdout block — whose numbers cannot be
    checked — is NOT confirmed, because an unverifiable ADOPT string must never
    be allowed to demote live picks. The genuine trainer always writes this
    block (manifest["holdout"]) in the same step that stamps verdict=ADOPT, so
    a real adopted artifact is never false-rejected.
    """
    holdout = manifest.get("holdout")
    if not isinstance(holdout, Mapping):
        return False, "no holdout block recorded to verify the ADOPT claim"
    try:
        meta = holdout["meta"]
        meta_inc = meta["inc_clv_max"]
        ctrl_inc = holdout["control"]["inc_clv_max"]
        meta_point = float(meta_inc["point"])
        meta_se = float(meta_inc["boot_se"])
        meta_roi = float(meta["roi"])
        meta_n = int(meta["n"])
        vol_roi = float(holdout["volume"]["roi"])
        ctrl_point = float(ctrl_inc["point"])
    except (KeyError, TypeError, ValueError):
        return False, "holdout block is missing the recorded adoption metrics"
    fails: list[str] = []
    if not (math.isfinite(meta_se) and meta_point - 2.0 * meta_se > 0.0):
        fails.append("C1 incCLV_max not > 2*SE above zero")
    if not meta_roi >= vol_roi:
        fails.append("C2 ROI below the volume baseline")
    if not meta_point > ctrl_point:
        fails.append("C3 does not beat the (league,market) cell control")
    if meta_n < _MIN_HOLDOUT_BETS:
        fails.append(f"C4 held-out n={meta_n} < {_MIN_HOLDOUT_BETS}")
    return (not fails), "; ".join(fails)


@dataclass(frozen=True)
class ValueFilterModel:
    """Loaded meta-model: LightGBM booster + manifest calibrator/threshold."""

    booster: Any  # lightgbm.Booster (typed Any — lightgbm is optional)
    features: tuple[str, ...]
    calibrator: dict[str, Any]
    threshold: float  # frozen operating point q* (manifest, train-only choice)
    min_odds: float  # training odds floor — candidates below it are unscored
    manifest_created_utc: str
    # True when the manifest verdict is NOT "ADOPT" and loading was allowed
    # via allow_shadow: ANNOTATION-ONLY. Every enforcement (demotion) call
    # site MUST check this — a shadow candidate never changes pick behavior.
    shadow: bool = False

    @classmethod
    def load(
        cls,
        model_dir: Path,
        *,
        manifest_filename: str = MANIFEST_FILENAME,
        model_filename: str = MODEL_FILENAME,
        allow_shadow: bool = False,
    ) -> ValueFilterModel | None:
        """Load artifacts from `model_dir`, or None (logged) when unavailable.

        Refuses anything but a manifest whose ONE-SHOT held-out verdict is
        ADOPT with a frozen operating point: an unvalidated artifact must
        never be able to demote live picks. The single exception is
        `allow_shadow=True` (VALUE_ML_MANIFEST_ALLOW_SHADOW): a non-ADOPT
        manifest then loads with `shadow=True` for annotation-only scoring —
        demotion call sites refuse shadow models, so enforcement still
        requires a true ADOPT manifest. All failure modes are non-fatal —
        the value pipeline simply runs unfiltered, as it always has.
        """
        manifest_path = model_dir / manifest_filename
        model_path = model_dir / model_filename
        if not manifest_path.exists() or not model_path.exists():
            logger.info("value-filter artifacts not found in %s; scoring disabled", model_dir)
            return None
        try:
            manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("value-filter manifest unreadable: %s", type(exc).__name__)
            return None
        verdict = manifest.get("verdict")
        is_adopt = verdict == "ADOPT"
        if is_adopt:
            # The verdict string is self-declared; re-derive it from the
            # manifest's own recorded holdout numbers so a hand-edited or stale
            # "ADOPT" whose evidence does not back it cannot demote live picks.
            confirmed, why = _adopt_confirmed_by_holdout(manifest)
            if not confirmed:
                is_adopt = False
                logger.warning(
                    "value-filter manifest declares verdict ADOPT but its own "
                    "recorded holdout numbers do not confirm it (%s); treating as "
                    "NOT ADOPT — a self-declared verdict cannot mint authority to "
                    "demote live picks without the evidence behind it",
                    why,
                )
        if not is_adopt:
            if not allow_shadow:
                logger.warning(
                    "value-filter manifest verdict is %r (not ADOPT); scoring disabled",
                    verdict,
                )
                return None
            logger.warning(
                "value-filter manifest %s verdict is %r — loading as SHADOW-CANDIDATE "
                "(VALUE_ML_MANIFEST_ALLOW_SHADOW): scores annotate picks only; "
                "demotion still requires a true ADOPT manifest",
                manifest_filename,
                verdict,
            )
        threshold = (manifest.get("operating_point") or {}).get("q")
        features = manifest.get("features")
        calibrator = manifest.get("calibrator")
        kind = (manifest.get("model") or {}).get("kind")
        if threshold is None or not features or not calibrator:
            logger.warning("value-filter manifest incomplete; scoring disabled")
            return None
        if kind != "lgbm":
            logger.warning("value-filter model kind %r unsupported; scoring disabled", kind)
            return None
        try:
            lgb = importlib.import_module("lightgbm")
        except ImportError:
            logger.warning(
                "value-filter artifacts present but lightgbm is not installed "
                "(uv sync --extra ml); scoring disabled"
            )
            return None
        try:
            booster = lgb.Booster(model_file=str(model_path))
        except Exception as exc:  # lightgbm raises bare Exception on bad files
            logger.warning("value-filter model unreadable: %s", type(exc).__name__)
            return None
        loaded = cls(
            booster=booster,
            features=tuple(features),
            calibrator=dict(calibrator),
            threshold=float(threshold),
            min_odds=float(manifest.get("min_odds", 1.6)),
            manifest_created_utc=str(manifest.get("created_utc", "")),
            shadow=not is_adopt,
        )
        logger.info(
            "value-filter meta-model loaded (manifest %s, q*=%.3f, %d features, mode=%s)",
            loaded.manifest_created_utc,
            loaded.threshold,
            len(loaded.features),
            "SHADOW/annotation-only" if loaded.shadow else "ADOPT",
        )
        return loaded

    def score(self, rows: Sequence[Mapping[str, Any]]) -> list[float]:
        """Calibrated P(candidate beats the vig-free Max close) per row.

        Rows come from `live_features`. Categorical levels unseen at training
        become NaN inside LightGBM (the booster file carries the training
        pandas-categorical vocabulary) and follow the learned default branch.
        """
        if not rows:
            return []
        pd = importlib.import_module("pandas")  # lightgbm's hard dependency chain
        x = pd.DataFrame([{f: r[f] for f in self.features} for r in rows])
        if "is_argmax_edge" in x.columns:
            x["is_argmax_edge"] = x["is_argmax_edge"].astype("int8")
        for col in CATEGORICAL_FEATURES & set(x.columns):
            x[col] = x[col].astype("category")
        raw = np.asarray(self.booster.predict(x), dtype=float)
        return [float(p) for p in calibrate(self.calibrator, raw)]
