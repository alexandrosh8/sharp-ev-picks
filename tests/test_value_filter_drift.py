"""Pure helpers of scripts/ml/value_filter_drift.py — PSI math on known
inputs, honesty-floor nulling, ceiling slice, pre-registered criteria replay,
and the --dry-run path. NO DB, no network, no ML artifacts. Places no bets."""

import importlib.util
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml" / "value_filter_drift.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


vfd: Any = _load(_SCRIPT, "value_filter_drift_t")


# --------------------------------------------------------------------------- #
# PSI math on known inputs
# --------------------------------------------------------------------------- #
def test_psi_numeric_identical_is_zero() -> None:
    x = list(np.random.default_rng(7).normal(0, 1, 500))
    assert vfd.psi_numeric(x, x) == pytest.approx(0.0, abs=1e-12)


def test_psi_numeric_shift_detected() -> None:
    rng = np.random.default_rng(7)
    x = list(rng.normal(0, 1, 2000))
    assert vfd.psi_numeric(x, [v + 1.0 for v in x]) > 0.25


def test_psi_numeric_constant_feature_does_not_crash() -> None:
    # Degenerate deciles collapse to a single bin -> PSI 0, not a crash.
    assert vfd.psi_numeric([1.0] * 100, [1.0] * 100) == pytest.approx(0.0, abs=1e-12)


def test_psi_numeric_empty_raises() -> None:
    with pytest.raises(ValueError):
        vfd.psi_numeric([], [1.0])


def test_psi_categorical_known_value() -> None:
    # 50/50 -> 90/10: (0.9-0.5)ln(0.9/0.5) + (0.1-0.5)ln(0.1/0.5) = 0.87889
    expected = ["a"] * 50 + ["b"] * 50
    actual = ["a"] * 90 + ["b"] * 10
    analytic = 0.4 * math.log(0.9 / 0.5) + (-0.4) * math.log(0.1 / 0.5)
    assert vfd.psi_categorical(expected, actual) == pytest.approx(analytic, abs=1e-9)


def test_psi_categorical_unseen_label_is_finite() -> None:
    # A live-only label must contribute finitely (eps floor), never inf.
    psi = vfd.psi_categorical(["a"] * 100, ["z"] * 100)
    assert math.isfinite(psi)
    assert psi > 0.25


# --------------------------------------------------------------------------- #
# Honesty-floor nulling
# --------------------------------------------------------------------------- #
def test_psi_with_floor_nulls_small_samples() -> None:
    big = list(range(100))
    small = list(range(vfd.MIN_PSI_N - 1))
    psi, label = vfd.psi_with_floor("numeric", big, small)
    assert psi is None
    assert "insufficient" in label
    # floor met on both sides -> real value + verdict label
    psi2, label2 = vfd.psi_with_floor("numeric", big, big)
    assert psi2 == pytest.approx(0.0, abs=1e-12)
    assert label2 == vfd.psi_verdict(0.0)


def test_psi_verdict_bands() -> None:
    assert vfd.psi_verdict(0.05) == "stable (<0.10)"
    assert "moderate" in vfd.psi_verdict(0.15)
    assert "MAJOR" in vfd.psi_verdict(0.30)


# --------------------------------------------------------------------------- #
# Ceiling slice
# --------------------------------------------------------------------------- #
def test_slice_ceiling_drops_only_ceiling_market_rows() -> None:
    df = pd.DataFrame(
        {
            "market": ["1x2", "1x2", "ou25", "ou25"],
            "best_price": [4.5, 3.9, 4.5, 2.0],
        }
    )
    out = vfd.slice_ceiling(df, 4.0)
    # the 1x2 row at 4.5 is dropped; ou25 rows pass regardless of price
    assert len(out) == 3
    assert not ((out["market"] == "1x2") & (out["best_price"] >= 4.0)).any()
    assert (out["market"] == "ou25").sum() == 2


# --------------------------------------------------------------------------- #
# Pre-registered criteria replay (C1-C4) on stand-in stats objects
# --------------------------------------------------------------------------- #
@dataclass
class _Inc:
    point: float
    se: float


@dataclass
class _Stats:
    n: int
    roi: float
    inc_max: _Inc | None


def test_evaluate_criteria_all_pass() -> None:
    meta = _Stats(n=400, roi=0.12, inc_max=_Inc(0.035, 0.004))
    vol = _Stats(n=360, roi=0.05, inc_max=_Inc(0.014, 0.006))
    ctrl = _Stats(n=700, roi=0.01, inc_max=_Inc(0.008, 0.003))
    assert vfd.evaluate_criteria(meta, vol, ctrl, 300) == []


def test_evaluate_criteria_each_gate_trips() -> None:
    vol = _Stats(n=360, roi=0.05, inc_max=_Inc(0.014, 0.006))
    ctrl = _Stats(n=700, roi=0.01, inc_max=_Inc(0.008, 0.003))
    # C1: incCLV lower bound not above zero
    fails = vfd.evaluate_criteria(_Stats(400, 0.12, _Inc(0.005, 0.004)), vol, ctrl, 300)
    assert any(f.startswith("C1") for f in fails)
    # C1 also trips on an unstable (infinite-SE) bootstrap
    fails = vfd.evaluate_criteria(_Stats(400, 0.12, _Inc(0.05, float("inf"))), vol, ctrl, 300)
    assert any(f.startswith("C1") for f in fails)
    # C2: ROI below the volume baseline
    fails = vfd.evaluate_criteria(_Stats(400, 0.01, _Inc(0.035, 0.004)), vol, ctrl, 300)
    assert any(f.startswith("C2") for f in fails)
    # C3: does not beat the cell control
    fails = vfd.evaluate_criteria(_Stats(400, 0.12, _Inc(0.007, 0.001)), vol, ctrl, 300)
    assert any(f.startswith("C3") for f in fails)
    # C4: n floor
    fails = vfd.evaluate_criteria(_Stats(299, 0.12, _Inc(0.035, 0.004)), vol, ctrl, 300)
    assert any(f.startswith("C4") for f in fails)


# --------------------------------------------------------------------------- #
# --dry-run path (no DB, no artifacts)
# --------------------------------------------------------------------------- #
def test_dry_run_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert vfd.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "never places bets" in out
