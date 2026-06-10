"""Quarter-line Asian handicap pricing bridge (penaltyblog >= 1.9 grid).

Converts a sharp book's DEVIGGED 1X2 + Over/Under 2.5 probabilities into
implied goal expectancies + Dixon-Coles rho (`goal_expectancy_extended`),
builds a probability grid (`create_dixon_coles_grid`), and prices any AH
line — integer, half, and quarter (split stakes) — via
`FootballProbabilityGrid.asian_handicap_probs`.

Why this exists: simple 2-way devig of a quarter-line book gives price-
implied probabilities but NOT the bet's EV — quarter lines settle as two
half-stakes (full/half win, push, half/full loss), so EV needs the
win/push/lose decomposition only a goal grid provides. This was the
researched blocker for quarter-line AH markets (decision log 2026-06-10);
penaltyblog 1.9.0+ unblocked it (verified against 1.11.0, 2026-06-10).

Sign convention (penaltyblog, post-1.9.0 fix): `line` is the handicap the
chosen side RECEIVES — negative = giving goals ("Alpha FC -1.25" ->
side="home", line=-1.25). Locked by tests/test_ah_bridge.py.

Inputs must already be devigged (we pass remove_overround=False); the
value pipeline's fair probabilities qualify. penaltyblog import keeps this
in app/models (same boundary as football_dc.py), not app/probabilities.
"""

from dataclasses import dataclass

from penaltyblog.models import create_dixon_coles_grid, goal_expectancy_extended

_PROB_TOL = 0.02  # devigged 1X2 must sum to 1 within this


@dataclass(frozen=True)
class MarketGoalExpectancy:
    """Goal model implied by a (devigged) 1X2 + O/U 2.5 market."""

    home_exp: float
    away_exp: float
    rho: float
    fit_error: float


@dataclass(frozen=True)
class AsianHandicapPrice:
    """Stake-weighted settlement probabilities and EV at given odds.

    For quarter lines, win/push/lose are the half-stake-weighted fractions
    (e.g. -0.25 = 50% at 0.0 + 50% at -0.5), so EV stays linear:
    EV per unit stake = win*(odds-1) - lose.
    """

    win: float
    push: float
    lose: float
    ev: float


def goal_expectancy_from_market(
    p_home: float,
    p_draw: float,
    p_away: float,
    p_over25: float,
    p_under25: float | None = None,
    max_goals: int = 15,
) -> MarketGoalExpectancy:
    """Implied (home_exp, away_exp, rho) from devigged market probabilities.

    Raises ValueError on non-probabilities, a 1X2 that does not sum to ~1,
    or an optimizer failure — callers must skip the market, never guess.
    """
    probs = [p_home, p_draw, p_away, p_over25]
    if p_under25 is None:
        p_under25 = 1.0 - p_over25
    probs.append(p_under25)
    if any(not 0.0 < p < 1.0 for p in probs):
        raise ValueError(f"probabilities out of (0,1): {probs}")
    total_1x2 = p_home + p_draw + p_away
    if abs(total_1x2 - 1.0) > _PROB_TOL:
        raise ValueError(f"1X2 probabilities sum to {total_1x2:.4f}, expected ~1 (devigged)")

    result = goal_expectancy_extended(
        home=p_home,
        draw=p_draw,
        away=p_away,
        over25=p_over25,
        under25=p_under25,
        max_goals=max_goals,
        remove_overround=False,  # inputs are already devigged
    )
    if not result.get("success", False):
        raise ValueError(f"goal expectancy fit failed: {result.get('message', 'no message')}")
    return MarketGoalExpectancy(
        home_exp=float(result["home_exp"]),
        away_exp=float(result["away_exp"]),
        rho=float(result["implied_rho"]),
        fit_error=float(result["error"]),
    )


def asian_handicap_price(
    exp: MarketGoalExpectancy,
    side: str,
    line: float,
    decimal_odds: float,
    max_goals: int = 15,
) -> AsianHandicapPrice:
    """Price an AH selection (any line) from an implied goal model.

    `side` is "home" or "away"; `line` is the handicap that side receives
    (negative = giving goals). EV is per unit stake at `decimal_odds`.
    """
    if side not in ("home", "away"):
        raise ValueError(f"side must be 'home' or 'away', got {side!r}")
    if decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must exceed 1.0, got {decimal_odds}")
    grid = create_dixon_coles_grid(exp.home_exp, exp.away_exp, exp.rho, max_goals=max_goals)
    probs = grid.asian_handicap_probs(side, line)
    win, push, lose = float(probs["win"]), float(probs["push"]), float(probs["lose"])
    return AsianHandicapPrice(
        win=win,
        push=push,
        lose=lose,
        ev=win * (decimal_odds - 1.0) - lose,
    )
