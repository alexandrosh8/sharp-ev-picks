"""Vig-stripping: multiplicative, additive, power, and Shin (1993) methods.

Pure module (numpy/scipy/stdlib only — NO logging/env/IO side effects; audit
2026-07-09 removed the per-market logger calls). Method selection per market
type is a config policy (ADR-0006) — callers pass the enum, nothing is
hardcoded here. Pathological solver inputs fall back to multiplicative
normalization instead of raising mid-pipeline; the fallback condition is
returned AS DATA (a :class:`DevigFallbackReason`, see
:func:`devig_with_diagnostics`) so the IO layer decides whether/how to log.
Invalid odds always raise at the boundary.
"""

from collections.abc import Sequence
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import brentq
from scipy.stats import norm

_FloatArray = NDArray[np.float64]


class DevigMethod(StrEnum):
    MULTIPLICATIVE = "multiplicative"
    ADDITIVE = "additive"
    POWER = "power"
    SHIN = "shin"
    # Inverse-normal (probit) constant shift — best on SYMMETRIC markets
    # (totals / Asian handicap / balanced props), where both sides price close.
    PROBIT = "probit"
    # Buchdahl "Wisdom of the Crowds" family (parity-tested vs penaltyblog):
    ODDS_RATIO = "odds_ratio"
    LOGARITHMIC = "logarithmic"
    DIFFERENTIAL_MARGIN = "differential_margin_weighting"


class DevigFallbackReason(StrEnum):
    """Why a fallback-capable method fell back to multiplicative normalization.

    Pure DATA in place of the old in-module log lines: the IO-layer call site
    decides how to surface it (reasons in :data:`EXPECTED_FALLBACKS` follow the
    debug doctrine; everything else is anomalous and warrants a warning)."""

    ADDITIVE_NON_POSITIVE = "additive_non_positive"
    POWER_SOLVE_FAILED = "power_solve_failed"
    LOGARITHMIC_SOLVE_FAILED = "logarithmic_solve_failed"
    PROBIT_SOLVE_FAILED = "probit_solve_failed"
    DIFFERENTIAL_MARGIN_NON_POSITIVE = "differential_margin_non_positive"
    DIFFERENTIAL_MARGIN_OUT_OF_RANGE = "differential_margin_out_of_range"
    SHIN_UNDERROUND = "shin_underround"
    SHIN_DEGENERATE = "shin_degenerate"
    SHIN_SOLVE_FAILED = "shin_solve_failed"


# Documented-EXPECTED fallbacks — the method simply does not apply to the price
# vector: Shin on the routinely-underround Max-of-books composites (a 46k-match
# backtest once emitted 154k warning lines before this was demoted), Buchdahl's
# differential margin on fat-margin longshots, and the degenerate 2-outcome
# Shin denominator. IO callers should log these at DEBUG, the rest at WARNING.
EXPECTED_FALLBACKS: frozenset[DevigFallbackReason] = frozenset(
    {
        DevigFallbackReason.SHIN_UNDERROUND,
        DevigFallbackReason.SHIN_DEGENERATE,
        DevigFallbackReason.DIFFERENTIAL_MARGIN_NON_POSITIVE,
        DevigFallbackReason.DIFFERENTIAL_MARGIN_OUT_OF_RANGE,
    }
)

_Fallback = DevigFallbackReason | None


def devig(
    odds: Sequence[float],
    method: DevigMethod = DevigMethod.MULTIPLICATIVE,
) -> tuple[float, ...]:
    """Convert decimal odds for one market into vig-free probabilities.

    Returns probabilities in input order, summing to 1.0.
    """
    return _devig_with_fallback(odds, method)[0]


def devig_with_provenance(
    odds: Sequence[float],
    method: DevigMethod = DevigMethod.MULTIPLICATIVE,
) -> tuple[tuple[float, ...], bool]:
    """Like :func:`devig`, but also reports whether `method` FELL BACK to
    multiplicative normalization on degenerate input (an underround book or a
    solver failure). The probabilities are byte-identical to :func:`devig`;
    only the flag is added.

    The flag exists so a caller can record that the configured method did not
    actually apply for a given price vector. The CLV true-up uses it to drop
    rows where the mint and close fairs were devigged ASYMMETRICALLY (one side
    fell back, the other did not) from the trusted sharp-CLV subset — such a CLV
    is a devig-method artifact, not a genuine line move (P2-2)."""
    probs, reason = _devig_with_fallback(odds, method)
    return probs, reason is not None


def devig_with_diagnostics(
    odds: Sequence[float],
    method: DevigMethod = DevigMethod.MULTIPLICATIVE,
) -> tuple[tuple[float, ...], DevigFallbackReason | None]:
    """Like :func:`devig`, but also returns WHY the configured method fell back
    to multiplicative (None = it applied). Probabilities are byte-identical to
    :func:`devig`. This is the log-free replacement for the old in-module
    warnings: IO callers log reasons in :data:`EXPECTED_FALLBACKS` at debug and
    everything else at warning."""
    return _devig_with_fallback(odds, method)


def devig_fell_back(
    odds: Sequence[float],
    method: DevigMethod = DevigMethod.MULTIPLICATIVE,
) -> bool:
    """True when devigging `odds` with `method` falls back to multiplicative
    (degenerate/underround book or solver failure). MULTIPLICATIVE never falls
    back (it IS the base), so it is always False for that method."""
    return _devig_with_fallback(odds, method)[1] is not None


def _devig_with_fallback(
    odds: Sequence[float],
    method: DevigMethod,
) -> tuple[tuple[float, ...], _Fallback]:
    """Shared core: validate, dispatch, normalize, and report fallback.

    Each fallback-capable helper returns ``(probabilities, reason)``; the
    fallback path always returns the multiplicative result, so a non-None
    ``reason`` marks "the configured method did not apply to this price
    vector" (and says why)."""
    if len(odds) < 2:
        raise ValueError("a market needs at least two outcomes")
    arr = np.asarray(odds, dtype=np.float64)
    if np.any(arr <= 1.0):
        raise ValueError(f"all decimal odds must exceed 1.0, got {list(odds)}")

    q = 1.0 / arr
    reason: _Fallback = None
    if method is DevigMethod.MULTIPLICATIVE:
        p = _multiplicative(q)  # the base normalization — never a "fallback"
    elif method is DevigMethod.ADDITIVE:
        p, reason = _additive(q)
    elif method is DevigMethod.POWER:
        p, reason = _power(q)
    elif method is DevigMethod.SHIN:
        p, reason = _shin(q)
    elif method is DevigMethod.PROBIT:
        p, reason = _probit(q)
    elif method is DevigMethod.ODDS_RATIO:
        # ODDS_RATIO is a constant logit shift == LOGARITHMIC; route through the
        # logarithmic solver (robust bracket + underround branch) so the two can
        # never diverge on extreme-overround books (audit #2).
        p, reason = _logarithmic(q)
    elif method is DevigMethod.LOGARITHMIC:
        p, reason = _logarithmic(q)
    elif method is DevigMethod.DIFFERENTIAL_MARGIN:
        p, reason = _differential_margin(arr, q)
    else:  # pragma: no cover - enum exhausts
        raise ValueError(f"unknown devig method: {method}")

    p = p / p.sum()  # absorb float dust so the contract sum==1.0 holds
    return tuple(float(x) for x in p), reason


def _multiplicative(q: _FloatArray) -> _FloatArray:
    return q / q.sum()


def _additive(q: _FloatArray) -> tuple[_FloatArray, _Fallback]:
    margin_share = (q.sum() - 1.0) / q.size
    p = q - margin_share
    if np.any(p <= 0.0):
        # additive devig produced a non-positive probability
        return _multiplicative(q), DevigFallbackReason.ADDITIVE_NON_POSITIVE
    return p, None


def _power(q: _FloatArray) -> tuple[_FloatArray, _Fallback]:
    booksum = q.sum()
    if abs(booksum - 1.0) < 1e-12:
        return q.copy(), None

    def f(k: float) -> float:
        return float(np.power(q, k).sum() - 1.0)

    lo = 1e-3  # q**~0 -> ones, sum = n > 1, f > 0
    hi = 1.0
    try:
        while f(hi) > 0.0:
            hi *= 2.0
            if hi > 1024.0:
                raise RuntimeError("power devig bracket not found")
        k = float(brentq(f, lo, hi, xtol=1e-12))
        return np.power(q, k), None
    except (RuntimeError, ValueError):
        return _multiplicative(q), DevigFallbackReason.POWER_SOLVE_FAILED


def _logarithmic(q: _FloatArray) -> tuple[_FloatArray, _Fallback]:
    """Additive shift in logit space: logit(p_i) = logit(q_i) - c, with c
    solved so probabilities sum to 1 (penaltyblog's 'logarithmic')."""
    if abs(q.sum() - 1.0) < 1e-12:
        return q.copy(), None
    safe = np.clip(q, 1e-15, 1.0 - 1e-15)
    log_odds = np.log(safe / (1.0 - safe))

    def f(c: float) -> float:
        return float((1.0 / (1.0 + np.exp(-(log_odds - c)))).sum() - 1.0)

    try:
        try:
            c = float(brentq(f, 0.0, 20.0, xtol=1e-12))
        except ValueError:  # underround books need a negative shift
            c = float(brentq(f, -20.0, 20.0, xtol=1e-12))
        return 1.0 / (1.0 + np.exp(-(log_odds - c))), None
    except ValueError:
        return _multiplicative(q), DevigFallbackReason.LOGARITHMIC_SOLVE_FAILED


def _probit(q: _FloatArray) -> tuple[_FloatArray, _Fallback]:
    """Probit (inverse-normal) constant shift: p_i = Phi(Phi^-1(q_i) - c), with c
    solved so probabilities sum to 1. The Gaussian analogue of ``_logarithmic``;
    preferred on SYMMETRIC markets (totals / Asian handicap) where both sides are
    priced close. Underround/degenerate input falls back to multiplicative."""
    if abs(q.sum() - 1.0) < 1e-12:
        return q.copy(), None
    safe = np.clip(q, 1e-15, 1.0 - 1e-15)
    z = norm.ppf(safe)

    def f(c: float) -> float:
        return float(norm.cdf(z - c).sum() - 1.0)

    try:
        try:
            c = float(brentq(f, 0.0, 20.0, xtol=1e-12))
        except ValueError:  # underround books need a negative shift
            c = float(brentq(f, -20.0, 20.0, xtol=1e-12))
        return np.asarray(norm.cdf(z - c), dtype=np.float64), None
    except ValueError:
        return _multiplicative(q), DevigFallbackReason.PROBIT_SOLVE_FAILED


def _differential_margin(odds: _FloatArray, q: _FloatArray) -> tuple[_FloatArray, _Fallback]:
    """Buchdahl's differential margin weighting.

    Despite the name, p_i = (n - margin*odds_i)/(n*odds_i) = 1/odds_i - margin/n:
    the odds term CANCELS, so the margin is removed EQUALLY in probability space —
    i.e. this method coincides with the additive method (and matches penaltyblog's
    DIFFERENTIAL_MARGIN_WEIGHTING, which likewise equals its additive). Output is
    correct (sums to 1, order-preserving); the historical 'distributed
    proportionally to the odds' description was wrong (audit #4)."""
    margin = q.sum() - 1.0
    n = float(odds.size)
    denom = n - margin * odds
    # Longshot odds with a fat margin push the denominator non-positive: the
    # method simply doesn't apply there and the multiplicative fallback is
    # the design (same doctrine as Shin's underround fallback) — an EXPECTED
    # reason, not a warning per affected market per cycle.
    if np.any(denom <= 0.0):
        return _multiplicative(q), DevigFallbackReason.DIFFERENTIAL_MARGIN_NON_POSITIVE
    p = denom / (n * odds)
    if np.any(p <= 0.0) or np.any(p >= 1.0):
        return _multiplicative(q), DevigFallbackReason.DIFFERENTIAL_MARGIN_OUT_OF_RANGE
    return p, None


def _shin(q: _FloatArray) -> tuple[_FloatArray, _Fallback]:
    """Shin (1993) insider-trading model.

    p_i = (sqrt(z^2 + 4(1-z) q_i^2 / Q) - z) / (2(1-z)), with z (insider
    fraction) solved so probabilities sum to 1. Only meaningful on overround
    books; underround input falls back to multiplicative.
    """
    booksum = q.sum()
    if booksum <= 1.0 + 1e-12:
        if abs(booksum - 1.0) < 1e-12:
            return q.copy(), None
        # Documented-expected path (Max-of-books composites are routinely
        # underround) — an EXPECTED reason: a 46k-match backtest emitted
        # 154k of these lines when it was a warning.
        return _multiplicative(q), DevigFallbackReason.SHIN_UNDERROUND

    def probs_for(z: float) -> _FloatArray:
        inner = z * z + 4.0 * (1.0 - z) * (q * q) / booksum
        return (np.sqrt(np.maximum(inner, 0.0)) - z) / (2.0 * (1.0 - z))

    # Exact 2-outcome closed form (Jullien & Salanie 1994) — no solver, no bracket
    # failure: z = ((B-1)(D^2 - B)) / (B(D^2 - 1)), B=booksum, D=q_0 - q_1.
    if q.size == 2:
        d = float(q[0] - q[1])
        denom = booksum * (d * d - 1.0)
        if abs(denom) <= 1e-15:
            return _multiplicative(q), DevigFallbackReason.SHIN_DEGENERATE
        z2 = ((booksum - 1.0) * (d * d - booksum)) / denom
        return probs_for(min(max(z2, 0.0), 0.999)), None

    def f(z: float) -> float:
        return float(probs_for(z).sum() - 1.0)

    try:
        # f(0) = sqrt(booksum) - 1 > 0 for overround books; f decreases in z.
        z_hi = 0.999
        if f(z_hi) > 0.0:
            raise RuntimeError("shin devig bracket not found")
        z = float(brentq(f, 0.0, z_hi, xtol=1e-12))
        return probs_for(z), None
    except (RuntimeError, ValueError):
        return _multiplicative(q), DevigFallbackReason.SHIN_SOLVE_FAILED
