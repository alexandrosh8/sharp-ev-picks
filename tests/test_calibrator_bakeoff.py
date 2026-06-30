"""Beta calibration + offline calibrator bake-off in the value-filter trainer.

Loads scripts/ml/train_value_filter.py by path (scripts/ is not a package);
importorskip-guarded (skips cleanly without the `ml` extra). Asserts the
trainer's beta FIT and the sklearn-free runtime replay
(app.models.value_filter.calibrate) agree — the clean-room contract that already
holds for isotonic/platt — and that the bake-off ranks isotonic/platt/beta by
held-out log-loss. Decision support only: held-out CLV remains the sole arbiter
of swapping the live calibrator (ADR-0017).
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("sklearn")

from app.models.value_filter import calibrate  # noqa: E402

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml" / "train_value_filter.py"
_spec = importlib.util.spec_from_file_location("train_value_filter", _SCRIPT)
assert _spec is not None and _spec.loader is not None
tvf: Any = importlib.util.module_from_spec(_spec)
sys.modules["train_value_filter"] = tvf
_spec.loader.exec_module(tvf)


def _miscalibrated(n: int = 4000) -> tuple[np.ndarray, np.ndarray]:
    # Overconfident raw scores vs truth -> a calibrator has something to fix.
    rng = np.random.default_rng(7)
    p_true = rng.uniform(0.05, 0.95, size=n)
    y = (rng.uniform(size=n) < p_true).astype(int)
    p_raw = np.clip(p_true**1.6, 1e-4, 1.0 - 1e-4)
    return p_raw, y


def test_beta_fit_matches_runtime_replay() -> None:
    # The trainer's beta fit produces JSON params the sklearn-free runtime replay
    # reproduces exactly — the same clean-room guarantee isotonic/platt have.
    p_raw, y = _miscalibrated()
    params = tvf.fit_beta_calibration(p_raw, y)
    assert params["kind"] == "beta"
    assert params["a"] >= 0.0 and params["b"] >= 0.0  # monotone map (betacal guard)
    grid = np.array([0.05, 0.2, 0.5, 0.8, 0.95])
    trainer = tvf.apply_calibrator(("beta", params), grid)
    runtime = calibrate(params, grid)
    assert trainer == pytest.approx(runtime, abs=1e-9)


def test_bakeoff_ranks_isotonic_platt_beta_by_log_loss() -> None:
    p_raw, y = _miscalibrated()
    cut = len(y) // 2
    rows = tvf.rank_calibrators(p_raw[:cut], y[:cut], p_raw[cut:], y[cut:])
    assert {r["kind"] for r in rows} == {"isotonic", "platt", "beta"}
    lls = [r["log_loss"] for r in rows]
    assert lls == sorted(lls)  # ranked best (lowest) log-loss first
    # the winner must beat the raw distorted score (the bake-off found a real fix)
    from sklearn.metrics import log_loss as _ll

    raw_ll = float(_ll(y[cut:], np.clip(p_raw[cut:], 1e-6, 1.0 - 1e-6), labels=[0, 1]))
    assert min(lls) <= raw_ll + 1e-9
