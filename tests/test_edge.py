"""Edge/EV gate logic: every gate trips its named reason; boundaries exact.

edge = p_model - p_fair (devigged); EV = p_model*(d-1) - (1-p_model).
"""

import pytest

from app.edge.gates import GatePolicy, PickCandidate, evaluate

POLICY = GatePolicy(
    min_edge=0.03,
    min_ev=0.01,
    min_confidence=0.60,
    max_odds_age_seconds=300,
    min_liquidity=0.0,
)


def candidate(**overrides: object) -> PickCandidate:
    base: dict[str, object] = {
        "event_id": "evt-1",
        "market": "h2h",
        "selection": "home",
        "decimal_odds": 2.10,
        "model_probability": 0.55,
        "fair_probability": 0.50,
        "confidence": 0.75,
        "odds_age_seconds": 60.0,
        "liquidity": 0.0,
    }
    base.update(overrides)
    return PickCandidate(**base)  # type: ignore[arg-type]


def test_all_gates_pass() -> None:
    # edge = 0.05; EV = 0.55*1.10 - 0.45 = 0.155
    decision = evaluate(candidate(), POLICY)
    assert decision.accepted is True
    assert decision.reasons == ()
    assert decision.edge == pytest.approx(0.05, abs=1e-12)
    assert decision.ev == pytest.approx(0.155, abs=1e-12)


def test_edge_below_threshold_trips() -> None:
    decision = evaluate(candidate(model_probability=0.51), POLICY)
    assert decision.accepted is False
    assert "edge_below_threshold" in decision.reasons


def test_ev_below_threshold_trips() -> None:
    # d=1.83, p=0.55: EV = 0.55*0.83 - 0.45 = 0.0065 < 0.01; edge 0.05 still ok
    decision = evaluate(candidate(decimal_odds=1.83), POLICY)
    assert decision.accepted is False
    assert "ev_below_threshold" in decision.reasons
    assert "edge_below_threshold" not in decision.reasons


def test_zero_ev_rejected_even_with_zero_min_ev() -> None:
    # EV must be strictly positive: p=0.5 @ d=2.0 -> EV = 0 exactly.
    policy = GatePolicy(
        min_edge=0.0,
        min_ev=0.0,
        min_confidence=0.0,
        max_odds_age_seconds=300,
        min_liquidity=0.0,
    )
    decision = evaluate(
        candidate(model_probability=0.5, fair_probability=0.45, decimal_odds=2.0),
        policy,
    )
    assert decision.accepted is False
    assert "ev_below_threshold" in decision.reasons


def test_confidence_below_threshold_trips() -> None:
    decision = evaluate(candidate(confidence=0.59), POLICY)
    assert decision.accepted is False
    assert "confidence_below_threshold" in decision.reasons


def test_odds_age_boundary() -> None:
    assert evaluate(candidate(odds_age_seconds=299.0), POLICY).accepted is True
    assert evaluate(candidate(odds_age_seconds=300.0), POLICY).accepted is True
    stale = evaluate(candidate(odds_age_seconds=301.0), POLICY)
    assert stale.accepted is False
    assert "odds_too_stale" in stale.reasons


def test_insufficient_liquidity_trips() -> None:
    policy = GatePolicy(
        min_edge=0.03,
        min_ev=0.01,
        min_confidence=0.60,
        max_odds_age_seconds=300,
        min_liquidity=100.0,
    )
    decision = evaluate(candidate(liquidity=50.0), policy)
    assert decision.accepted is False
    assert "insufficient_liquidity" in decision.reasons


def test_multiple_failures_report_all_reasons() -> None:
    decision = evaluate(
        candidate(model_probability=0.51, confidence=0.10, odds_age_seconds=500.0),
        POLICY,
    )
    assert decision.accepted is False
    assert set(decision.reasons) >= {
        "edge_below_threshold",
        "confidence_below_threshold",
        "odds_too_stale",
    }
