"""Vig-stripping: multiplicative, additive, power, and Shin (1993) methods.

Pure module (numpy/scipy/stdlib only). Method selection per market type is a
config policy (ADR-0006) — callers pass the enum, nothing is hardcoded here.
Pathological solver inputs fall back to multiplicative normalization (logged)
instead of raising mid-pipeline; invalid odds always raise at the boundary.
"""

import logging
from collections.abc import Sequence
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import brentq

logger = logging.getLogger(__name__)

_FloatArray = NDArray[np.float64]


class DevigMethod(StrEnum):
    MULTIPLICATIVE = "multiplicative"
    ADDITIVE = "additive"
    POWER = "power"
    SHIN = "shin"


def devig(
    odds: Sequence[float],
    method: DevigMethod = DevigMethod.MULTIPLICATIVE,
) -> tuple[float, ...]:
    """Convert decimal odds for one market into vig-free probabilities.

    Returns probabilities in input order, summing to 1.0.
    """
    if len(odds) < 2:
        raise ValueError("a market needs at least two outcomes")
    arr = np.asarray(odds, dtype=np.float64)
    if np.any(arr <= 1.0):
        raise ValueError(f"all decimal odds must exceed 1.0, got {list(odds)}")

    q = 1.0 / arr
    if method is DevigMethod.MULTIPLICATIVE:
        p = _multiplicative(q)
    elif method is DevigMethod.ADDITIVE:
        p = _additive(q)
    elif method is DevigMethod.POWER:
        p = _power(q)
    elif method is DevigMethod.SHIN:
        p = _shin(q)
    else:  # pragma: no cover - enum exhausts
        raise ValueError(f"unknown devig method: {method}")

    p = p / p.sum()  # absorb float dust so the contract sum==1.0 holds
    return tuple(float(x) for x in p)


def _multiplicative(q: _FloatArray) -> _FloatArray:
    return q / q.sum()


def _additive(q: _FloatArray) -> _FloatArray:
    margin_share = (q.sum() - 1.0) / q.size
    p = q - margin_share
    if np.any(p <= 0.0):
        logger.warning("additive devig produced non-positive probability; falling back")
        return _multiplicative(q)
    return p


def _power(q: _FloatArray) -> _FloatArray:
    booksum = q.sum()
    if abs(booksum - 1.0) < 1e-12:
        return q.copy()

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
        return np.power(q, k)
    except (RuntimeError, ValueError):
        logger.warning("power devig solve failed (booksum=%.6f); falling back", booksum)
        return _multiplicative(q)


def _shin(q: _FloatArray) -> _FloatArray:
    """Shin (1993) insider-trading model.

    p_i = (sqrt(z^2 + 4(1-z) q_i^2 / Q) - z) / (2(1-z)), with z (insider
    fraction) solved so probabilities sum to 1. Only meaningful on overround
    books; underround input falls back to multiplicative.
    """
    booksum = q.sum()
    if booksum <= 1.0 + 1e-12:
        if abs(booksum - 1.0) < 1e-12:
            return q.copy()
        logger.warning("shin devig on underround book (booksum=%.6f); falling back", booksum)
        return _multiplicative(q)

    def probs_for(z: float) -> _FloatArray:
        inner = z * z + 4.0 * (1.0 - z) * (q * q) / booksum
        return (np.sqrt(inner) - z) / (2.0 * (1.0 - z))

    def f(z: float) -> float:
        return float(probs_for(z).sum() - 1.0)

    try:
        # f(0) = sqrt(booksum) - 1 > 0 for overround books; f decreases in z.
        z_hi = 0.999
        if f(z_hi) > 0.0:
            raise RuntimeError("shin devig bracket not found")
        z = float(brentq(f, 0.0, z_hi, xtol=1e-12))
        return probs_for(z)
    except (RuntimeError, ValueError):
        logger.warning("shin devig solve failed (booksum=%.6f); falling back", booksum)
        return _multiplicative(q)
