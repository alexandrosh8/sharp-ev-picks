"""Quarter-line AH bridge: devigged market probs -> DC grid -> AH price/EV.

The reference values come from penaltyblog's own grid (pinned 1.11.0) so a
silent upstream behavior change (e.g. the 1.9.0 sign-convention fix
regressing) breaks these tests.
"""

import math

import pytest

pb_models = pytest.importorskip(
    "penaltyblog.models",
    reason="reference oracle needs the football extra — uv sync --extra football",
)
create_dixon_coles_grid = pb_models.create_dixon_coles_grid

from app.models.ah_bridge import (  # noqa: E402
    MarketGoalExpectancy,
    asian_handicap_price,
    goal_expectancy_from_market,
)

# A known goal model: the bridge must recover (approximately) these from
# the market probabilities the same grid produces.
H_EXP, A_EXP, RHO = 1.6, 1.1, -0.05
GRID = create_dixon_coles_grid(H_EXP, A_EXP, RHO)


def market_probs() -> tuple[float, float, float, float]:
    return (
        GRID.home_win,
        GRID.draw,
        GRID.away_win,
        GRID.total_goals("over", 2.5),
    )


def fitted() -> MarketGoalExpectancy:
    home, draw, away, over25 = market_probs()
    return goal_expectancy_from_market(home, draw, away, over25)


def test_recovers_goal_expectancies_from_market_probs() -> None:
    exp = fitted()
    assert exp.home_exp == pytest.approx(H_EXP, abs=0.05)
    assert exp.away_exp == pytest.approx(A_EXP, abs=0.05)
    assert exp.rho == pytest.approx(RHO, abs=0.10)  # rho is weakly identified


def test_half_line_price_matches_grid_win_probability() -> None:
    exp = fitted()
    price = asian_handicap_price(exp, "home", -0.5, decimal_odds=2.10)
    assert price.push == pytest.approx(0.0, abs=1e-9)
    assert price.win == pytest.approx(GRID.home_win, abs=0.01)
    assert price.win + price.lose == pytest.approx(1.0, abs=1e-9)


def test_quarter_line_is_split_stake_average_of_adjacent_lines() -> None:
    exp = fitted()
    q = asian_handicap_price(exp, "home", -0.25, decimal_odds=2.0)
    flat = asian_handicap_price(exp, "home", 0.0, decimal_odds=2.0)
    half = asian_handicap_price(exp, "home", -0.5, decimal_odds=2.0)
    assert q.win == pytest.approx((flat.win + half.win) / 2, abs=1e-9)
    assert q.push == pytest.approx((flat.push + half.push) / 2, abs=1e-9)
    assert q.lose == pytest.approx((flat.lose + half.lose) / 2, abs=1e-9)


def test_ev_zero_at_fair_odds_positive_above() -> None:
    exp = fitted()
    probe = asian_handicap_price(exp, "home", -0.75, decimal_odds=2.0)
    fair_odds = probe.lose / probe.win + 1.0  # EV = win*(o-1) - lose = 0
    at_fair = asian_handicap_price(exp, "home", -0.75, decimal_odds=fair_odds)
    assert at_fair.ev == pytest.approx(0.0, abs=1e-9)
    above = asian_handicap_price(exp, "home", -0.75, decimal_odds=fair_odds + 0.10)
    assert above.ev > 0.0


def test_sign_convention_negative_line_means_giving_goals() -> None:
    # home -1.5 must be strictly harder to win than home -0.5
    exp = fitted()
    minus_half = asian_handicap_price(exp, "home", -0.5, decimal_odds=2.0)
    minus_three_half = asian_handicap_price(exp, "home", -1.5, decimal_odds=2.0)
    assert minus_three_half.win < minus_half.win
    # and away +1.5 is the complement of home -1.5 (no push on half lines)
    away_plus = asian_handicap_price(exp, "away", 1.5, decimal_odds=2.0)
    assert away_plus.win == pytest.approx(minus_three_half.lose, abs=1e-9)


def test_invalid_market_probs_raise() -> None:
    with pytest.raises(ValueError):
        goal_expectancy_from_market(-0.1, 0.5, 0.6, 0.5)
    with pytest.raises(ValueError):
        goal_expectancy_from_market(0.2, 0.2, 0.2, 0.5)  # 1X2 sums to 0.6
    with pytest.raises(ValueError):
        goal_expectancy_from_market(0.5, 0.3, 0.2, 1.2)  # over25 out of range


def test_invalid_side_raises() -> None:
    exp = fitted()
    with pytest.raises(ValueError):
        asian_handicap_price(exp, "both", -0.5, decimal_odds=2.0)


def test_probabilities_are_a_distribution() -> None:
    exp = fitted()
    for line in (-1.75, -1.25, -1.0, -0.75, 0.25, 1.25):
        p = asian_handicap_price(exp, "home", line, decimal_odds=1.9)
        assert math.isclose(p.win + p.push + p.lose, 1.0, abs_tol=1e-9)
        assert 0.0 <= p.win <= 1.0
        assert 0.0 <= p.push <= 1.0
