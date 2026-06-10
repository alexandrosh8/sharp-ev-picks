"""CLV math: sign conventions, exact values, stake weighting."""

import math

import pytest

from app.backtesting.clv import ClvRecord, beat_close, clv_log, stake_weighted_clv


def test_clv_zero_when_fill_equals_fair_close() -> None:
    # fill 2.0 vs closing fair prob 0.5 (fair odds 2.0): ln(1.0) = 0
    assert clv_log(2.0, 0.5) == pytest.approx(0.0, abs=1e-12)


def test_clv_positive_when_beating_the_close() -> None:
    # fill 2.2 vs fair close 2.0 -> ln(2.2 * 0.5) = ln(1.1)
    assert clv_log(2.2, 0.5) == pytest.approx(math.log(1.1), abs=1e-12)
    assert beat_close(2.2, 0.5) is True


def test_clv_negative_when_market_moved_against() -> None:
    assert clv_log(1.9, 0.5) < 0
    assert beat_close(1.9, 0.5) is False


@pytest.mark.parametrize("bad_odds", [1.0, 0.9])
def test_invalid_fill_odds_raise(bad_odds: float) -> None:
    with pytest.raises(ValueError):
        clv_log(bad_odds, 0.5)


@pytest.mark.parametrize("bad_prob", [0.0, 1.0, -0.1, 1.5])
def test_invalid_closing_probability_raises(bad_prob: float) -> None:
    with pytest.raises(ValueError):
        clv_log(2.0, bad_prob)


def test_stake_weighted_clv_exact() -> None:
    records = [
        ClvRecord(pick_id="a", stake=100.0, clv=0.10),
        ClvRecord(pick_id="b", stake=300.0, clv=-0.02),
    ]
    # (100*0.10 + 300*-0.02) / 400 = (10 - 6) / 400 = 0.01
    assert stake_weighted_clv(records) == pytest.approx(0.01, abs=1e-12)


def test_zero_total_stake_raises() -> None:
    with pytest.raises(ValueError):
        stake_weighted_clv([ClvRecord(pick_id="a", stake=0.0, clv=0.1)])
