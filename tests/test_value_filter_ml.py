"""Value-filter scoring + pipeline demotion with a REAL LightGBM booster.

importorskip-guarded: skips cleanly when the `ml` extra (lightgbm/pandas) is
not installed — CI and the Docker image stay unaffected. A tiny booster is
trained on synthetic frames in tmp_path; no network, no real data, nothing
written outside tmp_path.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

lgb = pytest.importorskip("lightgbm")
pd = pytest.importorskip("pandas")

from app.edge.gates import GatePolicy  # noqa: E402
from app.ingestion.base import EventDirectory, EventTeams  # noqa: E402
from app.models.base import NullModel  # noqa: E402
from app.models.value_filter import ValueFilterModel  # noqa: E402
from app.notifications.base import Alert  # noqa: E402
from app.notifications.dedupe import InMemoryIdempotencyStore  # noqa: E402
from app.notifications.dispatcher import AlertDispatcher  # noqa: E402
from app.pipeline import PipelineDeps, run_value_pipeline  # noqa: E402
from app.risk.exposure import DailyExposureLedger  # noqa: E402
from app.risk.staking import StakePolicy  # noqa: E402
from app.schemas.base import Market  # noqa: E402
from app.schemas.odds import OddsSnapshotIn  # noqa: E402

NOW = datetime.now(tz=UTC)
SEED = 20260612

# Ordered feature list — parity with the production manifest
# (scripts/ml/train_value_filter.py FEATURES).
FEATURES = [
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
    "league",
    "market",
    "selection_type",
]
CATS = {"league": ["E0", "D1"], "market": ["1x2", "ou25"], "selection_type": ["fav", "dog", "draw"]}


def _train_frame(n: int = 600) -> Any:
    rng = np.random.default_rng(SEED)
    x = pd.DataFrame(
        {
            "edge": rng.uniform(0.0, 0.08, n),
            "fair_prob": rng.uniform(0.2, 0.6, n),
            "best_price": rng.uniform(1.6, 4.0, n),
            "pinn_price": rng.uniform(1.5, 3.8, n),
            "overround_pinn": rng.uniform(0.0, 0.06, n),
            "overround_best": rng.uniform(-0.05, 0.05, n),
            "devig_spread": rng.uniform(0.0, 0.01, n),
            "book_count": rng.integers(3, 16, n),
            "day_of_week": rng.integers(0, 7, n),
            "days_to_season_end": rng.integers(0, 320, n),
            "is_argmax_edge": rng.integers(0, 2, n).astype("int8"),
        }
    )
    for col, levels in CATS.items():
        x[col] = pd.Categorical(rng.choice(levels, n), categories=levels)
    return x[FEATURES]


# A holdout block whose recorded numbers PASS all four pre-registered adoption
# criteria (mirrors the real deployed META manifest: q>=0.725 n=396, ROI +12.0%,
# incCLV_max +0.0357 +/- 0.0075). A genuine ADOPT manifest always carries this;
# load() re-derives the verdict from it, so it must be present to load as ADOPT.
_PASSING_HOLDOUT: dict[str, Any] = {
    "meta": {"n": 396, "roi": 0.120, "inc_clv_max": {"point": 0.0357, "boot_se": 0.0075}},
    "volume": {"n": 379, "roi": 0.025, "inc_clv_max": {"point": 0.0190, "boot_se": 0.0100}},
    "control": {"n": 396, "roi": 0.050, "inc_clv_max": {"point": 0.0100, "boot_se": 0.0050}},
    "reliability_ece": 0.03,
}


def _write_artifacts(
    tmp_path: Path,
    q: float,
    verdict: str = "ADOPT",
    manifest_filename: str = "value_filter_manifest.json",
    model_filename: str = "value_filter_model.txt",
    holdout: dict[str, Any] | None = _PASSING_HOLDOUT,
) -> Path:
    """Train a tiny real booster (learnable rule: y = edge > 0.03) and write
    the model + a manifest with operating point `q` (default: ADOPT verdict
    under the deployed v1 filenames, backed by a passing holdout block).

    Pass `holdout=None` to omit the block or a custom dict to force a failing
    re-verification at load."""
    x = _train_frame()
    y = (x["edge"].to_numpy() > 0.03).astype(int)
    booster = lgb.train(
        {"objective": "binary", "min_data_in_leaf": 20, "verbosity": -1, "seed": SEED},
        lgb.Dataset(x, label=y, categorical_feature=list(CATS)),
        num_boost_round=30,
    )
    booster.save_model(str(tmp_path / model_filename))
    manifest: dict[str, Any] = {
        "created_utc": NOW.isoformat(),
        "verdict": verdict,
        "operating_point": {"q": q},
        "features": FEATURES,
        "min_odds": 1.6,
        "model": {"kind": "lgbm"},
        # identity isotonic map: calibrated == raw (clipped off 0/1)
        "calibrator": {"kind": "isotonic", "x_thresholds": [0.0, 1.0], "y_thresholds": [0.0, 1.0]},
    }
    if holdout is not None:
        manifest["holdout"] = holdout
    (tmp_path / manifest_filename).write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


V2_VERDICT = "CANDIDATE (binding verdict: live shadow CLV + fresh 2627 season)"


def _write_shadow_v2_artifacts(tmp_path: Path, q: float) -> Path:
    """v2-style artifacts: CANDIDATE verdict under the *_v2 filenames."""
    return _write_artifacts(
        tmp_path,
        q,
        verdict=V2_VERDICT,
        manifest_filename="value_filter_manifest_v2.json",
        model_filename="value_filter_model_v2.txt",
    )


def _load_shadow_v2(tmp_path: Path) -> ValueFilterModel | None:
    return ValueFilterModel.load(
        tmp_path,
        manifest_filename="value_filter_manifest_v2.json",
        model_filename="value_filter_model_v2.txt",
        allow_shadow=True,
    )


# ---------------------------------------------------------------------------
# Loader + scorer on the real booster
# ---------------------------------------------------------------------------
def _row(edge: float) -> dict[str, Any]:
    return {
        "edge": edge,
        "fair_prob": 0.40,
        "best_price": 2.9,
        "pinn_price": 2.5,
        "overround_pinn": 0.026,
        "overround_best": -0.03,
        "devig_spread": 0.001,
        "book_count": 3,
        "day_of_week": 5,
        "days_to_season_end": 100,
        "is_argmax_edge": True,
        "league": "E0",
        "market": "1x2",
        "selection_type": "fav",
    }


def test_load_and_score_real_booster(tmp_path: Path) -> None:
    vf = ValueFilterModel.load(_write_artifacts(tmp_path, q=0.725))
    assert vf is not None
    assert vf.threshold == 0.725
    assert vf.min_odds == 1.6
    scores = vf.score([_row(0.06), _row(0.005)])
    assert all(0.0 < s < 1.0 for s in scores)
    # the booster learned y = edge > 0.03: high-edge row must outscore low
    assert scores[0] > scores[1]


def test_unseen_categorical_levels_score_without_error(tmp_path: Path) -> None:
    # SP1 was not in the tiny training vocabulary: LightGBM maps it to NaN
    # internally (stored pandas-categorical) — must not raise.
    vf = ValueFilterModel.load(_write_artifacts(tmp_path, q=0.725))
    assert vf is not None
    row = _row(0.06) | {"league": "SP1"}
    (score,) = vf.score([row])
    assert 0.0 < score < 1.0


def test_empty_batch_scores_to_empty_list(tmp_path: Path) -> None:
    vf = ValueFilterModel.load(_write_artifacts(tmp_path, q=0.725))
    assert vf is not None
    assert vf.score([]) == []


def test_adopt_manifest_loads_with_shadow_false(tmp_path: Path) -> None:
    vf = ValueFilterModel.load(_write_artifacts(tmp_path, q=0.725))
    assert vf is not None
    assert vf.shadow is False


# A holdout block that FAILS C1 (incCLV_max not > 2*SE: 0.001 - 2*0.01 < 0) —
# every other field still passes, so only the recorded-number re-check rejects it.
_FAILING_HOLDOUT_C1: dict[str, Any] = {
    "meta": {"n": 396, "roi": 0.120, "inc_clv_max": {"point": 0.001, "boot_se": 0.010}},
    "volume": {"n": 379, "roi": 0.025, "inc_clv_max": {"point": 0.019, "boot_se": 0.010}},
    "control": {"n": 396, "roi": 0.050, "inc_clv_max": {"point": 0.0005, "boot_se": 0.005}},
    "reliability_ece": 0.03,
}


def test_self_declared_adopt_with_failing_holdout_is_refused(tmp_path: Path) -> None:
    # The verdict string says ADOPT but the recorded holdout numbers fail C1 —
    # the loader must NOT trust the self-declared verdict and refuse (None), so
    # an unverified artifact can never demote live picks.
    path = _write_artifacts(tmp_path, q=0.725, holdout=_FAILING_HOLDOUT_C1)
    assert ValueFilterModel.load(path) is None


def test_self_declared_adopt_without_holdout_block_is_refused(tmp_path: Path) -> None:
    # Fail-closed: an ADOPT manifest carrying NO holdout numbers to verify is
    # treated as not adopted (a genuine ADOPT always records its holdout).
    path = _write_artifacts(tmp_path, q=0.725, holdout=None)
    assert ValueFilterModel.load(path) is None


def test_unconfirmed_adopt_loads_as_shadow_under_allow_shadow(tmp_path: Path) -> None:
    # With allow_shadow, an ADOPT claim whose numbers don't confirm it still
    # loads — but only as an annotation-only SHADOW model, never as enforcing.
    path = _write_artifacts(tmp_path, q=0.725, holdout=_FAILING_HOLDOUT_C1)
    vf = ValueFilterModel.load(path, allow_shadow=True)
    assert vf is not None
    assert vf.shadow is True


def test_adopt_with_holdout_n_below_floor_is_refused(tmp_path: Path) -> None:
    # C4: held-out n below the pre-registered 300-bet floor invalidates ADOPT.
    holdout = {**_PASSING_HOLDOUT, "meta": {**_PASSING_HOLDOUT["meta"], "n": 200}}
    assert ValueFilterModel.load(_write_artifacts(tmp_path, q=0.725, holdout=holdout)) is None


def test_shadow_candidate_refused_without_allow_shadow(tmp_path: Path) -> None:
    # The v2 manifest carries verdict CANDIDATE (2425+2526 are SPENT; its
    # binding verdict is live shadow CLV + the fresh 2627 season) — the
    # default loader must refuse it exactly like any non-ADOPT artifact.
    path = _write_shadow_v2_artifacts(tmp_path, q=0.725)
    assert (
        ValueFilterModel.load(
            path,
            manifest_filename="value_filter_manifest_v2.json",
            model_filename="value_filter_model_v2.txt",
        )
        is None
    )


def test_allow_shadow_loads_shadow_candidate_for_annotation(tmp_path: Path) -> None:
    vf = _load_shadow_v2(_write_shadow_v2_artifacts(tmp_path, q=0.725))
    assert vf is not None
    assert vf.shadow is True
    assert vf.threshold == 0.725
    scores = vf.score([_row(0.06), _row(0.005)])
    assert all(0.0 < s < 1.0 for s in scores)
    assert scores[0] > scores[1]


# ---------------------------------------------------------------------------
# Pipeline integration: annotation always, demotion only when enabled
# ---------------------------------------------------------------------------
class FakeLoader:
    def __init__(self, snapshots: list[OddsSnapshotIn]) -> None:
        self.snapshots = snapshots
        self.last_fetch_matches: dict[str, int] = {}

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]:
        self.last_fetch_matches[sport_key] = len({s.event_id for s in self.snapshots})
        return self.snapshots


class RecordingSink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def _snap(book: str, sel: str, odds: float) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id="evt-ml",
        bookmaker=book,
        market=Market.H2H,
        selection=sel,
        decimal_odds=odds,
        captured_at=NOW - timedelta(seconds=30),
        ingested_at=NOW,
    )


def _snapshots() -> list[OddsSnapshotIn]:
    # Pinnacle tight 3-way; SoftBook generous on Home -> one premium pick.
    return [
        _snap("Pinnacle", "Home FC", 2.50),
        _snap("Pinnacle", "Draw", 3.30),
        _snap("Pinnacle", "Away FC", 3.10),
        _snap("SoftBook", "Home FC", 2.90),
        _snap("SoftBook", "Draw", 3.20),
        _snap("SoftBook", "Away FC", 2.95),
    ]


def _deps(sink: RecordingSink, vf: ValueFilterModel | None, enabled: bool) -> PipelineDeps:
    directory = EventDirectory()
    # league must map to the trained vocabulary (Premier League -> E0)
    directory.register(
        "evt-ml", EventTeams(home="Home FC", away="Away FC", league="Premier League")
    )
    return PipelineDeps(
        loader=FakeLoader(_snapshots()),
        model=NullModel(),
        dispatcher=AlertDispatcher([sink], InMemoryIdempotencyStore()),
        gate_policy=GatePolicy(
            min_edge=0.0,
            min_ev=0.0,
            min_confidence=0.0,
            max_odds_age_seconds=300,
            min_liquidity=0.0,
        ),
        stake_policy=StakePolicy(),
        ledger=DailyExposureLedger(max_daily_fraction=0.05),
        bankroll=Decimal("1000"),
        directory=directory,
        value_min_edge=0.015,
        value_min_odds=1.30,
        value_filter=vf,
        value_ml_filter_enabled=enabled,
    )


async def test_pipeline_annotates_score_without_demotion_when_flag_off(tmp_path: Path) -> None:
    # q=1.0 would demote EVERYTHING if enforcement were on (scores are
    # clipped below 1.0) — with the flag OFF the premium pick must flow
    # untouched, score annotated for the dashboard.
    vf = ValueFilterModel.load(_write_artifacts(tmp_path, q=1.0))
    assert vf is not None
    sink = RecordingSink()
    picks = await run_value_pipeline(_deps(sink, vf, enabled=False), "soccer")
    assert len(picks) == 1
    assert picks[0].tier == "premium"
    assert picks[0].value_filter_score is not None
    assert 0.0 < picks[0].value_filter_score < 1.0
    assert "demoted" not in picks[0].reason_summary
    assert len(sink.sent) == 1  # alerted as always


async def test_pipeline_demotes_sub_threshold_premium_when_enabled(tmp_path: Path) -> None:
    # q=1.0 + enforcement ON: the premium candidate is demoted to the volume
    # (shadow) tier — never alerted; without a DB the shadow pick can gather
    # no CLV evidence, so it is dropped from the returned list entirely.
    vf = ValueFilterModel.load(_write_artifacts(tmp_path, q=1.0))
    assert vf is not None
    sink = RecordingSink()
    picks = await run_value_pipeline(_deps(sink, vf, enabled=True), "soccer")
    assert picks == []
    assert sink.sent == []  # demoted picks must NEVER alert


async def test_pipeline_passes_high_scores_when_enabled(tmp_path: Path) -> None:
    # q close to 0: every scored candidate clears the operating point — the
    # premium pick flows with full behavior (alert + exposure) plus a score.
    vf = ValueFilterModel.load(_write_artifacts(tmp_path, q=1e-6))
    assert vf is not None
    sink = RecordingSink()
    picks = await run_value_pipeline(_deps(sink, vf, enabled=True), "soccer")
    assert len(picks) == 1
    assert picks[0].tier == "premium"
    assert picks[0].value_filter_score is not None
    assert len(sink.sent) == 1


async def test_pipeline_never_demotes_out_of_scope_candidates(tmp_path: Path) -> None:
    # Unmapped league -> the candidate is UNSCORED (None) and must pass
    # through unfiltered even with enforcement on and a vetoing threshold:
    # the model cannot veto markets it has never seen.
    vf = ValueFilterModel.load(_write_artifacts(tmp_path, q=1.0))
    assert vf is not None
    sink = RecordingSink()
    deps = _deps(sink, vf, enabled=True)
    assert deps.directory is not None
    deps.directory.register(
        "evt-ml", EventTeams(home="Home FC", away="Away FC", league="Brazil Serie A")
    )
    picks = await run_value_pipeline(deps, "soccer")
    assert len(picks) == 1
    assert picks[0].tier == "premium"
    assert picks[0].value_filter_score is None
    assert len(sink.sent) == 1


async def test_shadow_manifest_never_demotes_even_when_enforcement_enabled(
    tmp_path: Path,
) -> None:
    # SHADOW-CANDIDATE + VALUE_ML_FILTER force-combined directly on deps
    # (bypassing the composition root, which would already refuse): with
    # q=1.0 every score is sub-threshold, yet the premium pick must flow
    # untouched — annotated for live-shadow evidence, alerted as always.
    # Enforcement requires a true ADOPT manifest; a shadow model may not
    # change behavior under ANY flag combination.
    vf = _load_shadow_v2(_write_shadow_v2_artifacts(tmp_path, q=1.0))
    assert vf is not None
    assert vf.shadow is True
    sink = RecordingSink()
    picks = await run_value_pipeline(_deps(sink, vf, enabled=True), "soccer")
    assert len(picks) == 1
    assert picks[0].tier == "premium"
    assert picks[0].value_filter_score is not None
    assert 0.0 < picks[0].value_filter_score < 1.0
    assert "demoted" not in picks[0].reason_summary
    assert len(sink.sent) == 1  # shadow scoring never suppresses alerts
