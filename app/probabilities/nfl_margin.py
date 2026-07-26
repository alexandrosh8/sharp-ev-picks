"""Empirical spread-conditioned NFL margin PMF (key-number-weighted normal
mixture, nfelo-style) — SHADOW ANNOTATION ONLY.

Pure module (stdlib math only — NO env/DB/HTTP/log side effects; the frozen
fit table is a committed data file loaded lazily from this package, exactly
like app/resolution/aliases_seed.json). It exposes fair-prob-at-line given an
anchor devig at ANOTHER line: calibrate the latent expected home margin ``mu``
to a devigged cover probability at the anchor half-line, then read the fair
cover probability at any other half-line off the same PMF.

DOCTRINE (shadow-first mandate): this model may TAG or DEMOTE — it must NEVER
be a live premium fair-price source and must never alert on its own. NFL stays
visibility-only until forward trusted-CLV clears; nothing here is wired into
the pick pipeline.

Model:  P(margin = m | mu) ∝ w_m * [Phi((m+0.5-mu)/sigma) - Phi((m-0.5-mu)/sigma)]
over integer margins m in [support_min, support_max]. ``w_m`` are the frozen
key-number weights (3/7/6/10/14 spikes, near-zero ties) fit from FREE
nflverse/nfldata history by scripts/sports/nfl_margin_fit.py.

Line convention: ``line`` is the HOME handicap (points ADDED to the home
margin) — home covers iff margin + line > 0. Only half-lines are accepted
(integer/quarter lines push and are loader-rejected project-wide).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_FROZEN_TABLE_PATH = Path(__file__).with_name("nfl_margin_table.json")

# mu search bracket for calibration — wider than any real NFL expectation.
_MU_LO, _MU_HI = -80.0, 80.0
_BISECT_TOL = 1e-10
_BISECT_MAX_ITER = 200


def _phi(z: float) -> float:
    """Standard normal CDF via math.erf (stdlib — no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _require_half_line(line: float) -> None:
    doubled = line * 2.0
    if abs(doubled - round(doubled)) > 1e-9 or round(doubled) % 2 == 0:
        raise ValueError(f"line={line} is not a half-line (integer/quarter lines push)")


@dataclass(frozen=True)
class NflMarginModel:
    """Frozen spread-conditioned margin PMF. Immutable pure-math value object."""

    sigma: float
    weights: Mapping[int, float]  # signed integer margin -> key-number weight
    support_min: int
    support_max: int

    def __post_init__(self) -> None:
        if self.sigma <= 0.0:
            raise ValueError("sigma must be positive")
        if self.support_min >= self.support_max:
            raise ValueError("empty margin support")
        for m in range(self.support_min, self.support_max + 1):
            if self.weights.get(m, 1.0) <= 0.0:
                raise ValueError(f"non-positive weight at margin {m}")

    @classmethod
    def from_frozen_table(cls, path: Path | None = None) -> NflMarginModel:
        """Load the committed fit table (scripts/sports/nfl_margin_fit.py)."""
        table = json.loads((path or _FROZEN_TABLE_PATH).read_text(encoding="utf-8"))
        return cls(
            sigma=float(table["sigma"]),
            weights={int(k): float(v) for k, v in table["weights"].items()},
            support_min=int(table["support_min"]),
            support_max=int(table["support_max"]),
        )

    def margin_pmf(self, mu: float) -> dict[int, float]:
        """PMF over integer home margins given expected home margin ``mu``.

        Key-number-weighted discretized normal, renormalized to sum to 1.
        """
        raw: dict[int, float] = {}
        for m in range(self.support_min, self.support_max + 1):
            cell = _phi((m + 0.5 - mu) / self.sigma) - _phi((m - 0.5 - mu) / self.sigma)
            raw[m] = self.weights.get(m, 1.0) * cell
        total = math.fsum(raw.values())
        if total <= 0.0:  # mu far outside support — degenerate, refuse to guess
            raise ValueError(f"mu={mu} leaves no probability mass on the margin support")
        return {m: p / total for m, p in raw.items()}

    def home_cover_prob(self, mu: float, line: float) -> float:
        """P(home margin + line > 0) at a HALF-line (no push mass exists)."""
        _require_half_line(line)
        pmf = self.margin_pmf(mu)
        threshold = -line  # covers iff m > -line; half-line => never equal
        return math.fsum(p for m, p in pmf.items() if m > threshold)

    def implied_mu(self, line: float, home_cover_prob: float) -> float:
        """Invert the PMF: the ``mu`` whose cover prob at ``line`` matches.

        Cover probability is strictly increasing in mu (every PMF cell is
        positive), so bisection is exact and safe.
        """
        _require_half_line(line)
        if not 0.0 < home_cover_prob < 1.0:
            raise ValueError("home_cover_prob must be strictly inside (0, 1)")
        lo, hi = _MU_LO, _MU_HI
        p_lo = self.home_cover_prob(lo, line)
        p_hi = self.home_cover_prob(hi, line)
        if not p_lo < home_cover_prob < p_hi:
            raise ValueError(
                f"home_cover_prob={home_cover_prob} unreachable at line={line} "
                "within the model's mu bracket"
            )
        for _ in range(_BISECT_MAX_ITER):
            mid = 0.5 * (lo + hi)
            p_mid = self.home_cover_prob(mid, line)
            if abs(p_mid - home_cover_prob) < _BISECT_TOL:
                return mid
            if p_mid < home_cover_prob:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    def fair_prob_at_line(
        self, anchor_line: float, anchor_home_prob: float, target_line: float
    ) -> float:
        """Fair home-cover probability at ``target_line`` given a devigged
        anchor probability at ``anchor_line`` (both HOME-handicap half-lines).

        The anchor devig (e.g. a sharp book's spread pair at -2.5) calibrates
        the latent expected margin; the PMF then prices any other half-line of
        the ladder. SHADOW annotation only — never a live premium fair price.
        """
        mu = self.implied_mu(anchor_line, anchor_home_prob)
        return self.home_cover_prob(mu, target_line)


def load_default_model() -> NflMarginModel:
    """The committed frozen-table model (convenience composition-root hook)."""
    return NflMarginModel.from_frozen_table()
