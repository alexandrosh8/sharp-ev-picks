"""AH one-shot consumption discipline (scripts/ml/anchor_ah_backtest.py).

Regression tests for the two validator-confirmed findings of 2026-06-12:

  1. the corrected power gate must cancel a guaranteed-underpowered look
     UNCONSUMED — counting selectable matches (not pool rows) and never
     touching a label/outcome column on the cancel path;
  2. the intent marker must be written BEFORE the first label/outcome read,
     so a crash mid-computation still counts as the one look (the original
     read-then-mark ordering would have permitted a second look).

Loads the script by path (scripts/ is not a package); synthetic frames only —
no network, no real dataset, marker redirected to tmp_path per test.
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

# anchor_ah_backtest.py imports pandas at module level; without the `ml`
# extra a bare exec below would abort COLLECTION instead of skipping.
pd = pytest.importorskip("pandas")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml" / "anchor_ah_backtest.py"
_spec = importlib.util.spec_from_file_location("anchor_ah_backtest", _SCRIPT)
assert _spec is not None and _spec.loader is not None
aab: Any = importlib.util.module_from_spec(_spec)
# dataclasses resolves sys.modules[cls.__module__] at class creation — the
# module must be registered BEFORE exec_module (importlib docs pattern).
sys.modules["anchor_ah_backtest"] = aab
_spec.loader.exec_module(aab)

THR = 0.015  # the recorded thr* — any value above the pool floor works here


def _selectable_frame(n_matches: int) -> Any:
    """AH pool WITHOUT label/outcome columns: enough to select bets, nothing
    to settle or CLV-score. The power-gate cancel path must never need more."""
    return pd.DataFrame(
        {
            "mk": [f"m{i}" for i in range(n_matches)],
            "market": ["ah"] * n_matches,
            "best_price": [1.9] * n_matches,
            "edge": [THR + 0.005] * n_matches,
        }
    )


def _labeled_frame(n_matches: int, n_labeled: int) -> Any:
    """Full synthetic AH pool: one selectable row per match, `n_labeled` rows
    carrying a clv_max label (the rest NaN — moved-line rows stay unlabeled)."""
    frame = _selectable_frame(n_matches)
    frame["won"] = [i % 2 == 0 for i in range(n_matches)]
    frame["profit_units"] = [0.9 if i % 2 == 0 else -1.0 for i in range(n_matches)]
    frame["clv_pinn"] = np.nan
    frame["clv_max"] = [0.01 if i < n_labeled else np.nan for i in range(n_matches)]
    return frame


@pytest.fixture
def marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the consumption marker away from the real (already-consumed)
    data/ml/AH_ONESHOT_CONSUMED.json for every test in this file."""
    path = tmp_path / "AH_ONESHOT_CONSUMED.json"
    monkeypatch.setattr(aab, "CONSUMPTION_MARKER", path)
    return path


def test_power_gate_counts_selectable_matches_and_cancels_unconsumed(
    marker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The corrected gate: 99 selectable MATCHES < the 100-labeled floor means
    a PASS is arithmetically impossible — cancel BEFORE any label read (the
    frame has no label columns at all: touching one would raise) and leave
    the domain unconsumed (no marker)."""
    frame = _selectable_frame(aab.AH_TEST_MIN_N_LABELED - 1)
    aab.run_ah_oneshot(frame, THR, np.random.default_rng(aab.SEED))
    out = capsys.readouterr().out
    assert "CANCELLED UNCONSUMED" in out
    assert not marker.exists()


def test_refuses_second_look_when_marker_exists(
    marker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker.write_text('{"status": "completed"}\n', encoding="utf-8")
    aab.run_ah_oneshot(_selectable_frame(200), THR, np.random.default_rng(aab.SEED))
    assert "REFUSED" in capsys.readouterr().out
    assert marker.read_text(encoding="utf-8") == '{"status": "completed"}\n'  # untouched


def test_intent_marker_lands_before_first_label_read(
    marker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash between the first label read and the result write (the validator
    scenario): the intent marker must already exist — the look is committed
    the moment we decide to take it — and the marker must block a second
    attempt. stats_for is the earliest label-touching call, so raising there
    simulates the worst-case crash point."""

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated crash during the one-shot computation")

    monkeypatch.setattr(aab, "stats_for", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        aab.run_ah_oneshot(_selectable_frame(200), THR, np.random.default_rng(aab.SEED))
    assert marker.exists()
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["status"] == "intent"
    assert recorded["thr_star"] == THR
    # the half-written look is spent: a retry must refuse, not recompute
    monkeypatch.undo()
    aab.run_ah_oneshot(_labeled_frame(200, 5), THR, np.random.default_rng(aab.SEED))
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "intent"


def test_completed_oneshot_overwrites_intent_with_results(
    marker: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(aab, "B_BOOT", 50)  # bootstrap depth irrelevant here
    frame = _labeled_frame(150, 5)  # 150 selectable, only 5 labeled
    aab.run_ah_oneshot(frame, THR, np.random.default_rng(aab.SEED))
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["status"] == "completed"
    assert recorded["n_labeled_max"] == 5
    assert recorded["verdict"].startswith("UNDERPOWERED")
    out = capsys.readouterr().out
    assert "intent marker written" in out
    assert "consumption recorded" in out
    # intent precedes computation in the transcript as well
    assert out.index("intent marker written") < out.index("AH ONE-SHOT VERDICT")
