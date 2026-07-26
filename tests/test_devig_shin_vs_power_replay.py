"""Pure helpers of scripts/research/devig_shin_vs_power_replay.py — paired
fair recompute via the REAL app devig, sign-flip logic, paired-delta math,
honesty-floor nulling, and the --dry-run path. NO DB, no network. Places no
bets and changes no devig default."""

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "research" / "devig_shin_vs_power_replay.py"
)


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rep: Any = _load(_SCRIPT, "devig_shin_vs_power_replay_t")


# --------------------------------------------------------------------------- #
# fair_pair — the real app devig under both methods, fail-closed on fallback
# --------------------------------------------------------------------------- #
def test_fair_pair_returns_both_methods() -> None:
    pair = rep.fair_pair([2.0, 3.5, 3.6], 0)
    assert pair is not None
    p_power, p_shin = pair
    assert 0.0 < p_power < 1.0
    assert 0.0 < p_shin < 1.0
    # both are genuine devigs of the favourite side of an overround book
    assert p_power < 1 / 2.0  # devig strips vig, prob below raw implied
    assert p_shin != pytest.approx(p_power)  # methods genuinely differ


def test_fair_pair_none_on_underround_fallback() -> None:
    # An underround (arb) vector makes Shin fall back to multiplicative ->
    # not a method comparison -> None, never a fake pair.
    assert rep.fair_pair([3.5, 3.5, 3.5], 0) is None


def test_fair_pair_none_on_bad_vector() -> None:
    assert rep.fair_pair([1.0, 0.0], 0) is None


# --------------------------------------------------------------------------- #
# Sign-flip at the premium gate
# --------------------------------------------------------------------------- #
def test_edge_sign_flip() -> None:
    # p*odds-1: 0.50*2.10-1 = 5.0% >= 3%; 0.49*2.10-1 = 2.9% < 3% -> flip
    assert rep.edge_sign_flip(0.50, 0.49, 2.10, gate=0.03) is True
    # both above the gate -> no flip
    assert rep.edge_sign_flip(0.50, 0.495, 2.10, gate=0.03) is False
    # both below -> no flip
    assert rep.edge_sign_flip(0.40, 0.41, 2.10, gate=0.03) is False


# --------------------------------------------------------------------------- #
# Paired CLV delta
# --------------------------------------------------------------------------- #
def test_paired_clv_delta_is_log_ratio() -> None:
    # ln(o*ps) - ln(o*pp) = ln(ps/pp) — fill odds cancel exactly
    d = rep.paired_clv_delta(0.50, 0.52, 2.10)
    assert d == pytest.approx(math.log(0.52 / 0.50))
    assert rep.paired_clv_delta(0.5, 0.5, 3.0) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Honesty-floor nulling in the cell summary
# --------------------------------------------------------------------------- #
def test_cell_summary_nulls_below_floor() -> None:
    n = rep.MIN_STRATUM_N - 1
    out = rep.cell_summary([True] * n, [0.01] * n)
    assert out["flip_rate"] is None
    assert out["delta_mean"] is None
    assert "insufficient" in out["flip_label"]
    assert "insufficient" in out["delta_label"]


def test_cell_summary_reports_at_floor() -> None:
    n = rep.MIN_STRATUM_N
    flips = [i % 4 == 0 for i in range(n)]
    deltas = [0.01 + 0.001 * (i % 3) for i in range(n)]
    out = rep.cell_summary(flips, deltas)
    assert out["flip_rate"] == pytest.approx(sum(flips) / n)
    assert out["delta_mean"] == pytest.approx(sum(deltas) / n)
    assert out["delta_2se"] > 0
    assert out["flip_label"] == "ok"
    assert out["delta_label"] == "ok"


def test_summarize_cells_union_of_keys() -> None:
    flips = {("soccer", "h2h", "pinnacle"): [True] * 60}
    deltas = {("soccer", "1x2", "betfair exchange"): [0.01] * 60}
    cells = rep.summarize_cells(flips, deltas)
    assert len(cells) == 2
    keys = {(c["sport"], c["market"], c["anchor_book"]) for c in cells}
    assert ("soccer", "h2h", "pinnacle") in keys
    assert ("soccer", "1x2", "betfair exchange") in keys


# --------------------------------------------------------------------------- #
# --dry-run path (no DB)
# --------------------------------------------------------------------------- #
def test_dry_run_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert rep.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "SHIN-vs-POWER" in out
    assert "DRY RUN" in out
    assert "insufficient" in out  # the thin synthetic cell stays nulled
    assert "never places bets" in out
