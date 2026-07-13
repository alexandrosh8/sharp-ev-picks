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


def test_future_odds_timestamp_rejected() -> None:
    decision = evaluate(candidate(odds_age_seconds=-0.001), POLICY)
    assert decision.accepted is False
    assert "odds_timestamp_in_future" in decision.reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decimal_odds", float("inf")),
        ("model_probability", float("nan")),
        ("fair_probability", float("nan")),
        ("confidence", float("inf")),
        ("odds_age_seconds", float("nan")),
        ("liquidity", float("inf")),
    ],
)
def test_nonfinite_candidate_rejected(field: str, value: float) -> None:
    decision = evaluate(candidate(**{field: value}), POLICY)
    assert decision.accepted is False
    assert "invalid_numeric" in decision.reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decimal_odds", 1.0),
        ("model_probability", 1.01),
        ("fair_probability", -0.01),
        ("confidence", 1.01),
        ("liquidity", -1.0),
    ],
)
def test_out_of_range_candidate_rejected(field: str, value: float) -> None:
    decision = evaluate(candidate(**{field: value}), POLICY)
    assert decision.accepted is False
    assert "invalid_numeric" in decision.reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("min_edge", float("nan")),
        ("min_ev", -0.01),
        ("min_confidence", 1.01),
        ("max_odds_age_seconds", -1.0),
        ("min_liquidity", -1.0),
    ],
)
def test_invalid_gate_policy_rejected_at_construction(field: str, value: float) -> None:
    values = {
        "min_edge": 0.03,
        "min_ev": 0.01,
        "min_confidence": 0.60,
        "max_odds_age_seconds": 300.0,
        "min_liquidity": 0.0,
    }
    values[field] = value
    with pytest.raises(ValueError):
        GatePolicy(
            min_edge=values["min_edge"],
            min_ev=values["min_ev"],
            min_confidence=values["min_confidence"],
            max_odds_age_seconds=values["max_odds_age_seconds"],
            min_liquidity=values["min_liquidity"],
        )


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


# --- exchange-commission netting (audit 2026-07-09) -------------------------- #
# EV/Kelly on GROSS exchange odds overstate the payout: the gate must net the
# commission on winnings exactly like the value strategy (app/edge/value.py
# effective_odds): d_eff = 1 + (d - 1) * (1 - c).

COMMISSIONED = GatePolicy(
    min_edge=0.03,
    min_ev=0.01,
    min_confidence=0.60,
    max_odds_age_seconds=300,
    min_liquidity=0.0,
    commission_by_book=(("betfair exchange", 0.05), ("smarkets", 0.02)),
)


def test_no_commission_book_effective_odds_equal_gross() -> None:
    decision = evaluate(candidate(), COMMISSIONED)
    assert decision.effective_odds == pytest.approx(2.10, abs=1e-12)
    assert decision.ev == pytest.approx(0.155, abs=1e-12)  # unchanged vs gross


def test_exchange_commission_nets_ev_not_edge() -> None:
    # d=2.10 at 5% commission: d_eff = 1 + 1.10*0.95 = 2.045
    # EV = 0.55*1.045 - 0.45 = 0.12475 (gross would be 0.155); edge untouched.
    decision = evaluate(candidate(bookmaker="Betfair Exchange"), COMMISSIONED)
    assert decision.effective_odds == pytest.approx(2.045, abs=1e-12)
    assert decision.ev == pytest.approx(0.12475, abs=1e-12)
    assert decision.edge == pytest.approx(0.05, abs=1e-12)  # commission is payout, not prob


def test_commission_lookup_normalizes_book_name() -> None:
    decision = evaluate(candidate(bookmaker="  BETFAIR Exchange "), COMMISSIONED)
    assert decision.effective_odds == pytest.approx(2.045, abs=1e-12)


def test_commissioned_candidate_rejected_when_net_ev_below_threshold() -> None:
    # d=1.85, p=0.55: gross EV = 0.55*0.85 - 0.45 = 0.0175 >= 0.01 would pass,
    # but net of 5% commission d_eff = 1.8075 -> EV = -0.005875: must reject.
    gross = evaluate(candidate(decimal_odds=1.85), COMMISSIONED)
    assert gross.accepted is True
    net = evaluate(candidate(decimal_odds=1.85, bookmaker="betfair exchange"), COMMISSIONED)
    assert net.accepted is False
    assert "ev_below_threshold" in net.reasons
    assert net.ev == pytest.approx(0.55 * 0.8075 - 0.45, abs=1e-12)
