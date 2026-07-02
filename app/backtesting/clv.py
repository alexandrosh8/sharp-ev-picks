"""Closing Line Value math. Pure module.

clv_log = ln(fill_odds × p_close_fair) = ln(fill_odds / fair_closing_odds).
Positive means the pick beat the (vig-free) close. The closing fair
probability MUST come from the same devig method used elsewhere (odds-math
skill rule); this module just does the arithmetic.
"""

import math
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats


def clv_log(fill_decimal_odds: float, closing_fair_probability: float) -> float:
    if fill_decimal_odds <= 1.0:
        raise ValueError(f"fill odds must exceed 1.0, got {fill_decimal_odds}")
    if not 0.0 < closing_fair_probability < 1.0:
        raise ValueError(
            f"closing fair probability must be in (0, 1), got {closing_fair_probability}"
        )
    return math.log(fill_decimal_odds * closing_fair_probability)


def beat_close(fill_decimal_odds: float, closing_fair_probability: float) -> bool:
    return clv_log(fill_decimal_odds, closing_fair_probability) > 0.0


@dataclass(frozen=True)
class ClvRecord:
    pick_id: str
    stake: float
    clv: float  # log CLV


def stake_weighted_clv(records: Sequence[ClvRecord]) -> float:
    total_stake = sum(r.stake for r in records)
    if total_stake <= 0.0:
        raise ValueError("stake-weighted CLV requires positive total stake")
    return sum(r.stake * r.clv for r in records) / total_stake


@dataclass(frozen=True)
class MeanSignificance:
    """One-sample significance of a mean clv_log series vs 0.

    A positive CLV point estimate is meaningless without this: the strategy's
    proof rests on mean clv_log being statistically > 0, not small-sample noise.
    ``significant`` is True iff the two-sided (1-alpha) t-CI excludes 0 on the
    positive side (ci_low > 0) — i.e. CLV is reliably positive at ``alpha``.
    """

    n: int
    mean: float
    std: float  # sample std, ddof=1
    tstat: float  # one-sample t of mean vs 0
    ci_low: float  # t-based (1-alpha) CI on the mean
    ci_high: float
    alpha: float
    significant: bool


def mean_significance(values: Sequence[float], alpha: float = 0.05) -> MeanSignificance | None:
    """Significance of mean(values) vs 0 via a one-sample t-test.

    Returns None for n < 2 (sample variance — hence a t-test — is undefined),
    so an empty or single-observation stratum yields null, never a crash. A
    zero-variance sample collapses the CI to the mean (SE=0) and reports an
    infinite tstat rather than dividing by zero. Pure: numpy/scipy/stdlib only.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    n = len(values)
    if n < 2:
        return None
    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())
    # Detect the degenerate zero-variance sample by EXACT min==max (all values
    # identical): numpy's ddof=1 std returns a ~1e-18 float, not 0.0, for such a
    # sample, which would otherwise mint a spurious ~1e16 tstat.
    if float(arr.min()) == float(arr.max()):
        std = 0.0
        tstat = math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
        ci_low = ci_high = mean
    else:
        std = float(arr.std(ddof=1))
        se = std / math.sqrt(n)
        tstat = mean / se
        crit = float(stats.t.ppf(1.0 - alpha / 2.0, df=n - 1))
        ci_low = mean - crit * se
        ci_high = mean + crit * se
    # A zero-variance sample (all values identical) carries NO dispersion evidence,
    # so it cannot establish significance — even though ci_low==mean>0. Require real
    # variance (std>0) as well as ci_low>0 (tstat stays +-inf for display only).
    significant = std > 0.0 and ci_low > 0.0
    return MeanSignificance(
        n=n,
        mean=round(mean, 8),
        std=round(std, 8),
        tstat=tstat if math.isinf(tstat) else round(tstat, 6),
        ci_low=round(ci_low, 8),
        ci_high=round(ci_high, 8),
        alpha=alpha,
        significant=significant,
    )


def cluster_robust_se(values: Sequence[float], clusters: Sequence[Hashable]) -> float | None:
    """Cluster-robust standard error of mean(values), clustered by ``clusters``.

    Same-cluster observations (e.g. same-match 1X2 + OU picks) are CORRELATED;
    the i.i.d. SE pretends they are independent draws and understates the SE
    that feeds any ">2 SE" verdict. This is the standard sandwich/cluster
    estimator for the mean (a regression on a constant) with the CR1
    small-sample correction G/(G-1):

        e_g = sum_{i in g} (x_i - mean)        (per-cluster residual sum)
        SE  = sqrt( G/(G-1) * sum_g e_g^2 ) / n

    With every observation its own cluster (G == n) this reduces EXACTLY to
    the classic i.i.d. sample SE (ddof=1), so the two columns are directly
    comparable. Returns None when G < 2 (between-cluster variance — hence the
    estimator — is undefined; callers must treat None as not-significant,
    never as a fake-zero SE). Pure: numpy/stdlib only.
    """
    n = len(values)
    if n != len(clusters):
        raise ValueError(f"values ({n}) and clusters ({len(clusters)}) lengths differ")
    if n == 0:
        return None
    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())
    residual_sums: dict[Hashable, float] = {}
    for x, g in zip(arr, clusters, strict=True):
        residual_sums[g] = residual_sums.get(g, 0.0) + (float(x) - mean)
    n_clusters = len(residual_sums)
    if n_clusters < 2:
        return None
    ssq = sum(e * e for e in residual_sums.values())
    return math.sqrt(n_clusters / (n_clusters - 1) * ssq) / n


def wilson_interval(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float] | None:
    """Wilson score (1-alpha) CI for a binomial proportion (e.g. beat-close rate).

    Wilson (not normal-approx Wald) because it stays inside [0, 1] and is honest
    at small n / extreme proportions. Returns None for n == 0 (no proportion to
    bound). Bounds are clamped to [0, 1] and always ordered low <= high. Pure.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"need 0 <= successes <= n, got successes={successes}, n={n}")
    if n == 0:
        return None
    z = float(stats.norm.ppf(1.0 - alpha / 2.0))
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2.0 * n)) / denom
    half = (z * math.sqrt((phat * (1.0 - phat) + z2 / (4.0 * n)) / n)) / denom
    low = max(0.0, center - half)
    high = min(1.0, center + half)
    return (round(low, 8), round(high, 8))
