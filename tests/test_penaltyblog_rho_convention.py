"""Pin penaltyblog's TWO opposite Dixon-Coles tau conventions (v1.11.0).

penaltyblog ships both parameterizations of the low-score correction:

- PAPER (Dixon & Coles 1997): tau(0,1) = 1 + rho*lambda_home,
  tau(1,0) = 1 + rho*lambda_away — used by the compiled model kernel
  ``compute_dixon_coles_probabilities`` (the fitted ``DixonColes`` model)
  and by ``goal_expectancy`` (basic).
- TRANSPOSED: tau(0,1) = 1 + rho*lambda_away, tau(1,0) = 1 + rho*lambda_home
  — used by ``goal_expectancy_extended`` and ``create_dixon_coles_grid``.

The two rho parameterizations are NOT interchangeable unless
lambda_home == lambda_away. app/models/ah_bridge.py pairs
goal_expectancy_extended -> create_dixon_coles_grid (both transposed), so it
is internally consistent as-is. These tests exist so a future penaltyblog
bump that flips EITHER convention breaks loudly instead of silently
mis-pricing the 1-0/0-1 cells (which moves AH +/-0.5 and +/-1.0 prices).

Never mix rho across the {DixonColes model, goal_expectancy} and
{goal_expectancy_extended, create_dixon_coles_grid} families — see
.claude/memory/pitfalls.md ("Dixon-Coles rho conventions").
"""

import numpy as np
import pytest
from scipy.stats import poisson

pb_models = pytest.importorskip(
    "penaltyblog.models",
    reason="pins installed penaltyblog tau conventions — uv sync --extra football",
)
create_dixon_coles_grid = pb_models.create_dixon_coles_grid

from penaltyblog.models.goal_expectancy import goal_expectancy_extended  # noqa: E402

# Deliberately asymmetric lambdas: the two conventions coincide iff lh == la.
LH, LA, RHO = 1.6, 0.9, -0.08
MAX_GOALS = 15


def _base_grid() -> np.ndarray:
    goals = np.arange(MAX_GOALS + 1)
    return np.outer(poisson.pmf(goals, LH), poisson.pmf(goals, LA))


def _apply_tau(
    grid: np.ndarray, tau_01: float, tau_10: float, normalize: bool = True
) -> np.ndarray:
    """Apply the four low-score tau factors, optionally normalizing.

    create_dixon_coles_grid constructs its FootballProbabilityGrid with
    normalize=True, so cells compared against it must be normalized the same
    way. Convention-vs-convention structure is compared RAW: the two
    conventions share tau(0,0) and tau(1,1) but their total masses differ,
    so normalization shifts even the agreeing cells.
    """
    out = grid.copy()
    out[0, 0] *= 1.0 - RHO * LH * LA
    out[1, 1] *= 1.0 - RHO
    out[0, 1] *= tau_01
    out[1, 0] *= tau_10
    return out / out.sum() if normalize else out


def test_create_dixon_coles_grid_uses_transposed_tau() -> None:
    """grid[0,1] carries 1 + rho*lambda_AWAY and grid[1,0] carries
    1 + rho*lambda_HOME (the transposed convention) — exact on all four
    low-score cells for asymmetric lambdas."""
    got = np.asarray(create_dixon_coles_grid(LH, LA, RHO, max_goals=MAX_GOALS).grid)
    expected = _apply_tau(_base_grid(), tau_01=1.0 + RHO * LA, tau_10=1.0 + RHO * LH)
    np.testing.assert_allclose(got[:2, :2], expected[:2, :2], rtol=1e-12)


def test_paper_convention_differs_on_low_score_cells() -> None:
    """The SAME rho under the paper convention prices (0,1)/(1,0) differently,
    by a materially large margin — the parameterizations are not
    interchangeable. Anyone feeding the fitted DixonColes model's rho (paper
    kernel) into create_dixon_coles_grid would hit exactly this gap."""
    # Structure, compared RAW (normalization constants differ between the
    # conventions, shifting even the agreeing cells): the conventions share
    # tau(0,0)/tau(1,1) and differ ONLY on the off-diagonal.
    paper_raw = _apply_tau(
        _base_grid(), tau_01=1.0 + RHO * LH, tau_10=1.0 + RHO * LA, normalize=False
    )
    transposed_raw = _apply_tau(
        _base_grid(), tau_01=1.0 + RHO * LA, tau_10=1.0 + RHO * LH, normalize=False
    )
    assert np.isclose(paper_raw[0, 0], transposed_raw[0, 0], rtol=1e-12)
    assert np.isclose(paper_raw[1, 1], transposed_raw[1, 1], rtol=1e-12)
    assert abs(paper_raw[0, 1] - transposed_raw[0, 1]) > 1e-3
    assert abs(paper_raw[1, 0] - transposed_raw[1, 0]) > 1e-3

    # And the library grid does NOT match a paper-convention grid on the
    # off-diagonal cells (same rho, normalized identically):
    got = np.asarray(create_dixon_coles_grid(LH, LA, RHO, max_goals=MAX_GOALS).grid)
    paper = _apply_tau(_base_grid(), tau_01=1.0 + RHO * LH, tau_10=1.0 + RHO * LA)
    assert not np.isclose(got[0, 1], paper[0, 1])
    assert not np.isclose(got[1, 0], paper[1, 0])
    # The gap is AH-pricing material, not float noise:
    assert abs(got[0, 1] - paper[0, 1]) > 1e-3
    assert abs(got[1, 0] - paper[1, 0]) > 1e-3


def test_extended_expectancy_roundtrips_grid_convention() -> None:
    """goal_expectancy_extended inverts create_dixon_coles_grid's own markets
    back to the SAME (mu_h, mu_a, rho) — proof the pair shares one tau
    convention, which is the consistency app/models/ah_bridge.py relies on.
    If either side flips convention upstream, the recovered rho diverges for
    asymmetric lambdas and this test fails."""
    grid = create_dixon_coles_grid(LH, LA, RHO, max_goals=MAX_GOALS)
    under, _push, over = grid.totals(2.5)  # (under, push, over) in 1.11.0
    res = goal_expectancy_extended(
        grid.home_win, grid.draw, grid.away_win, over, under, remove_overround=False
    )
    assert res["success"]
    assert res["home_exp"] == pytest.approx(LH, abs=5e-4)
    assert res["away_exp"] == pytest.approx(LA, abs=5e-4)
    assert res["implied_rho"] == pytest.approx(RHO, abs=5e-4)
