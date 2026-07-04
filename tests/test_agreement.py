"""H6 agreement predicate (app/backtesting/agreement.py) — pure verdict cases,
closed reason vocabulary, fail-closed exclusion semantics, the value_backtest
H6-row parity (H6 stats == frozen stats restricted by the predicate, synthetic
rows only), and live-path isolation. No network, no DB. Places no bets."""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from app.backtesting.agreement import (
    H6_TOLERANCE,
    REASON_AGREE,
    REASON_DELTA_EXCEEDS_TOLERANCE,
    REASON_DIRECTION_CONFLICT,
    REASON_REFERENCE_MISSING,
    REASON_SELECTION_MISSING,
    REASON_VOCABULARY,
    AgreementVerdict,
    agreement_verdict,
)
from app.probabilities.devig import DevigMethod, devig

ANCHOR_1X2 = {"home": 0.50, "draw": 0.28, "away": 0.22}


# --------------------------------------------------------------------------- #
# Verdict cases
# --------------------------------------------------------------------------- #
def test_agree_within_tolerance() -> None:
    v = agreement_verdict(ANCHOR_1X2, {"home": 0.49, "draw": 0.29, "away": 0.22}, "home", 0.02)
    assert v.passes and v.reason == REASON_AGREE
    assert v.delta == pytest.approx(0.01)


def test_tolerance_edge_is_inclusive() -> None:
    """|delta| == tolerance passes (<= semantics, frozen in the module doc)."""
    # exact binary floats so |delta| == tolerance holds without float dust
    v = agreement_verdict({"home": 0.75}, {"home": 0.50}, "home", 0.25)
    assert v.passes and v.reason == REASON_AGREE
    assert v.delta == pytest.approx(0.25)


def test_delta_exceeds_tolerance() -> None:
    v = agreement_verdict(ANCHOR_1X2, {"home": 0.55, "draw": 0.25, "away": 0.20}, "home", 0.02)
    assert not v.passes
    assert v.reason == REASON_DELTA_EXCEEDS_TOLERANCE
    assert v.delta == pytest.approx(-0.05)


def test_direction_conflict() -> None:
    """Anchor favours (>{1/n}), reference unfavours (<1/n): direction conflict."""
    anchor = {"home": 0.40, "draw": 0.32, "away": 0.28}  # home above 1/3
    reference = {"home": 0.28, "draw": 0.36, "away": 0.36}  # home below 1/3
    v = agreement_verdict(anchor, reference, "home", 0.02)
    assert not v.passes
    assert v.reason == REASON_DIRECTION_CONFLICT
    assert v.delta == pytest.approx(0.12)


def test_direction_conflict_never_overrides_agreement() -> None:
    """Direction is a severity sub-label of DISAGREEMENT only — a within-
    tolerance pair straddling 1/n still agrees (pass is tolerance-only)."""
    v = agreement_verdict({"a": 0.505, "b": 0.495}, {"a": 0.495, "b": 0.505}, "a", 0.02)
    assert v.passes and v.reason == REASON_AGREE


def test_reference_missing_none_and_empty() -> None:
    references: tuple[dict[str, float] | None, ...] = (None, {})
    for reference in references:
        v = agreement_verdict(ANCHOR_1X2, reference, "home", 0.02)
        assert v == AgreementVerdict(passes=False, reason=REASON_REFERENCE_MISSING, delta=None)


def test_selection_missing_either_side() -> None:
    v = agreement_verdict(ANCHOR_1X2, {"draw": 0.3}, "home", 0.02)
    assert v == AgreementVerdict(passes=False, reason=REASON_SELECTION_MISSING, delta=None)
    v = agreement_verdict({"draw": 0.3}, ANCHOR_1X2, "home", 0.02)
    assert v.reason == REASON_SELECTION_MISSING


def test_single_entry_mappings_use_reference_outcome_count() -> None:
    """Replay wiring passes a single-entry anchor; n comes from the reference."""
    v = agreement_verdict({"home": 0.40}, {"home": 0.28, "draw": 0.36, "away": 0.36}, "home", 0.02)
    assert v.reason == REASON_DIRECTION_CONFLICT  # 1/n = 1/3 from the reference


def test_negative_or_nan_tolerance_rejected() -> None:
    with pytest.raises(ValueError):
        agreement_verdict(ANCHOR_1X2, ANCHOR_1X2, "home", -0.01)
    with pytest.raises(ValueError):
        agreement_verdict(ANCHOR_1X2, ANCHOR_1X2, "home", float("nan"))


def test_closed_reason_vocabulary() -> None:
    """Every reachable reason is a member of the closed vocabulary."""
    cases = [
        agreement_verdict(ANCHOR_1X2, ANCHOR_1X2, "home", 0.02),
        agreement_verdict(ANCHOR_1X2, None, "home", 0.02),
        agreement_verdict(ANCHOR_1X2, {"draw": 0.3}, "home", 0.02),
        agreement_verdict(ANCHOR_1X2, {"home": 0.60, "draw": 0.2, "away": 0.2}, "home", 0.02),
        agreement_verdict(
            {"home": 0.40, "draw": 0.32, "away": 0.28},
            {"home": 0.28, "draw": 0.36, "away": 0.36},
            "home",
            0.02,
        ),
    ]
    assert {v.reason for v in cases} == set(REASON_VOCABULARY)
    expected = {
        "agree",
        "reference_missing",
        "direction_conflict",
        "delta_exceeds_tolerance",
        "selection_missing",
    }
    assert set(REASON_VOCABULARY) == expected


def test_fail_closed_exclusion_semantics_documented_and_distinct() -> None:
    """reference_missing is an EXCLUSION for the H6 row (never a pass, never a
    counted fail): passes must be False and its reason distinct from the
    disagreement reasons so callers can report the exclusion count."""
    v = agreement_verdict(ANCHOR_1X2, None, "home", 0.02)
    assert v.passes is False
    assert v.reason not in (REASON_DELTA_EXCEEDS_TOLERANCE, REASON_DIRECTION_CONFLICT)
    import app.backtesting.agreement as agreement_mod

    assert "EXCLUDED" in (agreement_mod.__doc__ or "")


# --------------------------------------------------------------------------- #
# value_backtest H6 row: math == frozen row restricted by the predicate
# --------------------------------------------------------------------------- #
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


vb: Any = _load(_SCRIPTS / "value_backtest.py", "value_backtest_h6")


def _row(
    ps: tuple[float, float, float],
    mx: tuple[float, float, float],
    ftr: str,
    avg: tuple[float, float, float] | None,
) -> dict:
    r = {
        "PSH": str(ps[0]),
        "PSD": str(ps[1]),
        "PSA": str(ps[2]),
        "MaxH": str(mx[0]),
        "MaxD": str(mx[1]),
        "MaxA": str(mx[2]),
        "FTR": ftr,
    }
    if avg is not None:
        r |= {"AvgH": str(avg[0]), "AvgD": str(avg[1]), "AvgA": str(avg[2])}
    return r


def test_h6_row_math_equals_frozen_row_restricted_by_predicate(capsys: Any) -> None:
    rows = [
        # agrees: Avg == PS -> zero delta
        _row((2.0, 3.4, 4.0), (2.2, 3.5, 4.1), "H", (2.0, 3.4, 4.0)),
        # disagrees: consensus far from the sharp anchor
        _row((2.0, 3.4, 4.0), (2.2, 3.5, 4.1), "A", (2.6, 3.4, 2.9)),
        # reference missing: no Avg*, no soft-book columns -> EXCLUDED
        _row((2.0, 3.4, 4.0), (2.2, 3.5, 4.1), "D", None),
        # agrees again (different fixture, away side value)
        _row((3.0, 3.3, 2.4), (3.4, 3.4, 2.5), "H", (3.05, 3.3, 2.42)),
    ]
    thr = 0.01
    frozen_bets = vb.bets_for(rows, thr, DevigMethod.POWER, ("1x2",), 1.0, 1000.0)
    assert len(frozen_bets) == 4  # every synthetic row mints exactly one bet
    baseline = vb.Stats.from_bets(vb.bets_for(rows, 0.0, DevigMethod.POWER, ("1x2",), 1.0, 1000.0))
    retained, fail_reasons, excluded = vb._h6_agreement_row(rows, "1x2", frozen_bets, baseline)
    out = capsys.readouterr().out
    assert "H6 agreement-gate variant (pre-registered)" in out
    assert "SIGNED 2026-07-04" in out  # tolerance provenance rides the printout

    # Independent replication of the predicate over the SAME frozen bets.
    names = ("home", "draw", "away")
    expected_retained = []
    for b in frozen_bets:
        r = rows[int(b.cluster[1:])]
        sharp = devig([float(r["PSH"]), float(r["PSD"]), float(r["PSA"])], method=DevigMethod.POWER)
        idx = next(
            i
            for i in range(3)
            if float(r[("MaxH", "MaxD", "MaxA")[i]]) == b.odds and sharp[i] - 1.0 / b.odds == b.edge
        )
        ref_odds = vb._h6_reference_odds(r, "1x2")
        reference = (
            dict(zip(names, devig(ref_odds, method=DevigMethod.POWER), strict=True))
            if ref_odds
            else None
        )
        v = agreement_verdict(
            dict(zip(names, sharp, strict=True)), reference, names[idx], H6_TOLERANCE
        )
        if v.passes:
            expected_retained.append(b)
    assert retained == expected_retained
    assert len(retained) == 2
    assert sum(fail_reasons.values()) == 1
    assert excluded == {"reference_missing": 1}  # fail-closed: reported, not failed

    # H6 stats are EXACTLY the frozen-row Stats math restricted by the predicate.
    h6_stats = vb.Stats.from_bets(retained, with_roi_ci=True)
    ref_stats = vb.Stats.from_bets(expected_retained, with_roi_ci=True)
    assert h6_stats == ref_stats


def test_h6_reference_falls_back_to_named_soft_book_mean() -> None:
    """Without Avg*, the reference is the per-outcome mean of _SOFT_1X2_BOOKS."""
    r = _row((2.0, 3.4, 4.0), (2.2, 3.5, 4.1), "H", None)
    r |= {"B365H": "2.0", "B365D": "3.4", "B365A": "4.0"}
    r |= {"WHH": "2.2", "WHD": "3.6", "WHA": "4.2"}
    assert vb._h6_reference_odds(r, "1x2") == pytest.approx([2.1, 3.5, 4.1])


def test_h6_reference_missing_when_no_consensus_columns() -> None:
    assert vb._h6_reference_odds(_row((2.0, 3.4, 4.0), (2.2, 3.5, 4.1), "H", None), "1x2") is None


# --------------------------------------------------------------------------- #
# Live-path isolation: the agreement gate is offline-only, off by default
# --------------------------------------------------------------------------- #
def test_agreement_module_not_imported_by_live_pick_minting() -> None:
    """app/pipeline.py and app/edge/value.py must NOT import
    app.backtesting.agreement — the H6 gate is an offline evaluation row only
    (no live selection change, no staking change)."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    for live in ("pipeline.py", "edge/value.py", "scheduler.py"):
        text = (app_dir / live).read_text(encoding="utf-8")
        assert "backtesting.agreement" not in text, f"{live} must not import the H6 gate"
        assert "agreement_verdict" not in text, f"{live} must not call the H6 gate"
