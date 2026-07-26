"""Pure helpers of scripts/research/mint_timing_study.py — bucketing, the
hours_to_kickoff fallback, honesty-floor nulling, t-CI math, ARM/HOLD
recommendation, and the --dry-run path. NO DB, no network. Places no bets."""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research" / "mint_timing_study.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mts: Any = _load(_SCRIPT, "mint_timing_study_t")

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Bucketing + fallback
# --------------------------------------------------------------------------- #
def test_bucket_hours_edges() -> None:
    assert mts.bucket_hours(-0.01) == "post-kickoff"
    assert mts.bucket_hours(0.0) == "0-2h"
    assert mts.bucket_hours(1.99) == "0-2h"
    assert mts.bucket_hours(2.0) == "2-6h"
    assert mts.bucket_hours(6.0) == "6-12h"
    assert mts.bucket_hours(12.0) == "12-24h"
    assert mts.bucket_hours(24.0) == "24-48h"
    assert mts.bucket_hours(48.0) == "48h+"
    assert mts.bucket_hours(500.0) == "48h+"


def test_effective_hours_prefers_stored_column() -> None:
    # stored column wins even when created_at/starts_at disagree
    assert mts.effective_hours(7.5, T0, T0 + timedelta(hours=99)) == 7.5


def test_effective_hours_falls_back_to_timestamps() -> None:
    assert mts.effective_hours(None, T0, T0 + timedelta(hours=36)) == pytest.approx(36.0)
    # post-kickoff mints go negative, never clamped
    assert mts.effective_hours(None, T0, T0 - timedelta(minutes=30)) == pytest.approx(-0.5)


def test_effective_hours_none_when_underivable() -> None:
    assert mts.effective_hours(None, T0, None) is None
    assert mts.effective_hours(None, None, None) is None


# --------------------------------------------------------------------------- #
# Honesty-floor nulling + t-CI
# --------------------------------------------------------------------------- #
def test_bucket_stats_nulls_below_floor() -> None:
    st = mts.bucket_stats([0.01] * (mts.MIN_STRATUM_N - 1))
    assert st["n"] == mts.MIN_STRATUM_N - 1
    assert st["mean"] is None
    assert st["se"] is None
    assert st["ci95"] is None
    assert "insufficient" in st["label"]


def test_bucket_stats_at_floor_reports() -> None:
    vals = [0.01 + 0.001 * (i % 3) for i in range(mts.MIN_STRATUM_N)]
    st = mts.bucket_stats(vals)
    assert st["label"] == "ok"
    assert st["mean"] == pytest.approx(sum(vals) / len(vals))
    lo, hi = st["ci95"]
    assert lo < st["mean"] < hi


def test_t_ci95_matches_scipy() -> None:
    from scipy.stats import t as t_dist

    lo, hi = mts.t_ci95(0.0, 1.0, 10)
    half = float(t_dist.ppf(0.975, 9))
    assert lo == pytest.approx(-half)
    assert hi == pytest.approx(half)
    # t half-width exceeds the normal 1.96 at small n (honest wider CI)
    assert hi > 1.96


# --------------------------------------------------------------------------- #
# ARM/HOLD recommendation
# --------------------------------------------------------------------------- #
def _rows(n: int, hours: float, clv: float, jitter: float = 1e-4) -> list[tuple[float, float]]:
    return [(hours, clv + jitter * (i % 5)) for i in range(n)]


def test_arm_at_smallest_significantly_negative_threshold() -> None:
    # >24h slice (80 rows) clearly negative; the >12h slice is diluted by a
    # big positive 12-24h block -> not significant -> ARM lands at 24, the
    # SMALLEST threshold whose dropped slice is significantly negative.
    rows = _rows(80, 30.0, -0.05) + _rows(200, 18.0, +0.05) + _rows(60, 1.0, +0.01)
    rec = mts.arm_recommendation(rows)
    assert rec["armed_at"] == 24.0
    assert "ARM candidate" in rec["verdict"]
    assert "24" in rec["verdict"]


def test_arm_at_12_when_whole_tail_is_negative() -> None:
    # every row past 12h is negative -> the smallest qualifying threshold wins
    rows = _rows(80, 30.0, -0.05) + _rows(60, 1.0, +0.01)
    rec = mts.arm_recommendation(rows)
    assert rec["armed_at"] == 12.0


def test_hold_when_slice_below_floor() -> None:
    # negative slice but n<50 -> honesty floor forces HOLD
    rows = _rows(mts.MIN_STRATUM_N - 1, 30.0, -0.05) + _rows(60, 1.0, +0.01)
    rec = mts.arm_recommendation(rows)
    assert rec["armed_at"] is None
    assert rec["verdict"] == "HOLD"


def test_hold_when_ci_straddles_zero() -> None:
    # large n but mean ~0 with wide spread -> CI straddles 0 -> HOLD
    rows = [(30.0, 0.5 if i % 2 else -0.5) for i in range(200)]
    rec = mts.arm_recommendation(rows)
    assert rec["armed_at"] is None
    assert rec["verdict"] == "HOLD"


# --------------------------------------------------------------------------- #
# Report assembly + --dry-run path
# --------------------------------------------------------------------------- #
def test_build_report_buckets_and_floor() -> None:
    rows = [mts.TimingRow("soccer", "h2h", 30.0, -0.05) for _ in range(60)] + [
        mts.TimingRow("soccer", "h2h", 1.0, 0.01) for _ in range(10)
    ]
    report = mts.build_report(rows, {"untrusted_clv": 3})
    assert report["n_trusted"] == 70
    assert report["skips"] == {"untrusted_clv": 3}
    by_bucket = {b["bucket"]: b for b in report["buckets"]}
    assert by_bucket["24-48h"]["label"] == "ok"
    assert by_bucket["24-48h"]["n"] == 60
    # 0-2h has n=10 < 50 -> nulled
    assert by_bucket["0-2h"]["mean"] is None
    assert "insufficient" in by_bucket["0-2h"]["label"]


def test_dry_run_exits_zero_and_arms(capsys: pytest.CaptureFixture[str]) -> None:
    assert mts.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "MINT-TIMING STUDY" in out
    assert "ARM candidate: VALUE_PREMIUM_MAX_HOURS_TO_KICKOFF=24" in out
    assert "insufficient" in out  # tennis thin stratum stays nulled
    assert "never places bets" in out
