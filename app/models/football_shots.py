"""Wheatcroft GAP-style attacking-performance ratings from shots/corners.

Generalized Attacking Performance (GAP) ratings (Wheatcroft 2020/21,
"Forecasting football matches by predicting match statistics"): each team
carries venue-specific attacking/defensive ratings over an observable
attacking statistic — here a weighted blend of shots on target (primary),
total shots, and corners — updated online with a learning-rate rule. The
predicted blended attacking output of both teams is converted to an
expected total-goals rate via an online league conversion ratio and mapped
through a Poisson tail to a P(over 2.5 goals) lean.

SHADOW-ONLY (operator mandate 2026-07-04): this module is an
annotation/veto/visibility screen for soccer totals candidates. It must
NEVER price picks, act as a fair-price source, or alert on its own.
Walk-forward eval 2026-07-26 (scripts/research/shots_ou25_walkforward.py,
E0/D1/SP1/I1/F1 2018-2025, n=10,378): the shots screen beat the goals-only
baseline OOS in all 5 leagues (pooled log-loss 0.6798 vs 0.6870, Brier
0.2434 vs 0.2465). `ShotsPolicy.veto_enabled` still defaults to False —
shadow-first mandate: tag-only until forward trusted-CLV evidence; flipping
the veto on is an operator decision.

Pure math: stdlib only. No env/DB/HTTP/log side effects. Policy enters as a
frozen dataclass from the composition root. NO closing odds anywhere here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

_LEAN_OVER = "over"
_LEAN_UNDER = "under"

# clamp bounds for the Poisson total-goals rate: a rating blow-up must never
# emit a degenerate 0/1 probability
_MIN_TOTAL_LAMBDA = 0.2
_MAX_TOTAL_LAMBDA = 8.0
_PROB_EPS = 1e-6


@dataclass(frozen=True)
class ShotsPolicy:
    """Frozen policy inputs for the GAP shots screen (composition root)."""

    # blend weights — shots on target is the primary Wheatcroft signal
    weight_sot: float = 1.0
    weight_shots: float = 0.2
    weight_corners: float = 0.1
    # GAP online update rates: direct venue and cross-venue
    learning_rate: float = 0.1
    cross_rate: float = 0.5  # cross-venue update = learning_rate * cross_rate
    # a team needs this many observed matches before it can be priced
    min_team_matches: int = 6
    # minimum league-wide updates before the goals-conversion ratio is trusted
    min_league_matches: int = 20
    # shadow-signal behaviour: lean declared only beyond this margin from 0.5
    veto_margin: float = 0.10
    # SHADOW-FIRST: veto disabled by default DESPITE beating the goals-only
    # baseline OOS (2026-07-26 walk-forward, all 5 major leagues) — operator
    # mandate: tag-only until forward trusted evidence; flipping this on is
    # an operator decision.
    veto_enabled: bool = False


@dataclass(frozen=True)
class MatchStats:
    """One finished match's attacking stats — the module's only input row."""

    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    home_shots: int | None = None
    away_shots: int | None = None
    home_shots_on_target: int | None = None
    away_shots_on_target: int | None = None
    home_corners: int | None = None
    away_corners: int | None = None


@dataclass(frozen=True)
class ShotsTotalsSignal:
    """Shadow annotation for one soccer totals candidate (line 2.5)."""

    p_over25: float | None
    lean: str | None  # "over" | "under" | None
    veto: bool
    reason: str


@dataclass(frozen=True)
class OU25EvalResult:
    """Walk-forward OOS comparison: shots screen vs goals-only baseline."""

    n_evaluated: int
    shots_log_loss: float
    shots_brier: float  # == RPS for a binary outcome
    baseline_log_loss: float
    baseline_brier: float

    @property
    def beats_baseline(self) -> bool:
        return self.n_evaluated > 0 and self.shots_log_loss < self.baseline_log_loss


def blended_stat(
    sot: int | None, shots: int | None, corners: int | None, policy: ShotsPolicy
) -> float | None:
    """Weighted attacking-output blend. Shots on target is required (primary
    signal); missing secondary components contribute nothing rather than
    invalidating the row."""
    if sot is None:
        return None
    total = policy.weight_sot * float(sot)
    if shots is not None:
        total += policy.weight_shots * float(shots)
    if corners is not None:
        total += policy.weight_corners * float(corners)
    return total


def poisson_p_over_25(lam: float) -> float:
    """P(N >= 3) for N ~ Poisson(lam), clamped away from 0/1."""
    lam = min(max(lam, _MIN_TOTAL_LAMBDA), _MAX_TOTAL_LAMBDA)
    p_le_2 = math.exp(-lam) * (1.0 + lam + lam * lam / 2.0)
    return min(max(1.0 - p_le_2, _PROB_EPS), 1.0 - _PROB_EPS)


@dataclass
class _TeamRating:
    att_home: float
    att_away: float
    def_home: float  # stat conceded when playing at home
    def_away: float  # stat conceded when playing away
    n_matches: int = 0


class GapRatings:
    """Online GAP ratings over the blended shots stat (or goals, for the
    baseline). `update()` only ever consumes finished-match rows strictly in
    the past — predict-then-update ordering is the caller's walk-forward
    guarantee (see evaluate_walkforward_ou25)."""

    def __init__(self, policy: ShotsPolicy, mode: Literal["shots", "goals"] = "shots") -> None:
        self._policy = policy
        self._mode: Literal["shots", "goals"] = mode
        self._teams: dict[str, _TeamRating] = {}
        # running league sums for cold-start priors and goals conversion
        self._sum_home_stat = 0.0
        self._sum_away_stat = 0.0
        self._sum_goals = 0.0
        self._n_matches = 0

    # ------------------------------------------------------------- internals

    def _stat_pair(self, match: MatchStats) -> tuple[float, float] | None:
        if self._mode == "goals":
            return float(match.home_goals), float(match.away_goals)
        home = blended_stat(
            match.home_shots_on_target, match.home_shots, match.home_corners, self._policy
        )
        away = blended_stat(
            match.away_shots_on_target, match.away_shots, match.away_corners, self._policy
        )
        if home is None or away is None:
            return None
        return home, away

    def _mean_home(self) -> float:
        return self._sum_home_stat / self._n_matches if self._n_matches else 0.0

    def _mean_away(self) -> float:
        return self._sum_away_stat / self._n_matches if self._n_matches else 0.0

    def _get_or_seed(self, team: str) -> _TeamRating:
        rating = self._teams.get(team)
        if rating is None:
            # cold-start prior: league-average attacking/conceding output
            rating = _TeamRating(
                att_home=self._mean_home(),
                att_away=self._mean_away(),
                def_home=self._mean_away(),  # concedes at home what visitors score
                def_away=self._mean_home(),
            )
            self._teams[team] = rating
        return rating

    # ------------------------------------------------------------ public api

    def update(self, match: MatchStats) -> None:
        """Consume one finished match. Rows without usable stats (shots mode
        with missing shots-on-target) are skipped — never imputed."""
        pair = self._stat_pair(match)
        if pair is None:
            return
        home_stat, away_stat = pair

        home = self._get_or_seed(match.home_team)
        away = self._get_or_seed(match.away_team)

        pred_home = 0.5 * (home.att_home + away.def_away)
        pred_away = 0.5 * (away.att_away + home.def_home)

        lr = self._policy.learning_rate
        xr = lr * self._policy.cross_rate
        err_home = home_stat - pred_home
        err_away = away_stat - pred_away

        home.att_home += lr * err_home
        away.def_away += lr * err_home
        away.att_away += lr * err_away
        home.def_home += lr * err_away
        # cross-venue updates keep the other-venue ratings from starving
        home.att_away += xr * err_home
        away.def_home += xr * err_home
        away.att_home += xr * err_away
        home.def_away += xr * err_away

        home.n_matches += 1
        away.n_matches += 1
        self._sum_home_stat += home_stat
        self._sum_away_stat += away_stat
        self._sum_goals += float(match.home_goals + match.away_goals)
        self._n_matches += 1

    def predict_stat_pair(self, home_team: str, away_team: str) -> tuple[float, float] | None:
        """Expected (home, away) blended attacking output; None if either
        team is under-observed or the league prior is still cold."""
        if self._n_matches < self._policy.min_league_matches:
            return None
        home = self._teams.get(home_team)
        away = self._teams.get(away_team)
        if home is None or away is None:
            return None
        if (
            home.n_matches < self._policy.min_team_matches
            or away.n_matches < self._policy.min_team_matches
        ):
            return None
        pred_home = max(0.0, 0.5 * (home.att_home + away.def_away))
        pred_away = max(0.0, 0.5 * (away.att_away + home.def_home))
        return pred_home, pred_away

    def expected_total_goals(self, home_team: str, away_team: str) -> float | None:
        pair = self.predict_stat_pair(home_team, away_team)
        if pair is None:
            return None
        stat_sum = self._sum_home_stat + self._sum_away_stat
        if stat_sum <= 0.0:
            return None
        conversion = self._sum_goals / stat_sum  # online goals-per-stat-unit
        lam = conversion * (pair[0] + pair[1])
        return min(max(lam, _MIN_TOTAL_LAMBDA), _MAX_TOTAL_LAMBDA)

    def p_over_25(self, home_team: str, away_team: str) -> float | None:
        lam = self.expected_total_goals(home_team, away_team)
        if lam is None:
            return None
        return poisson_p_over_25(lam)


def shots_totals_signal(
    ratings: GapRatings,
    home_team: str,
    away_team: str,
    pick_side: str,
    policy: ShotsPolicy,
) -> ShotsTotalsSignal:
    """SHADOW screen for one soccer totals (2.5) candidate.

    Never a fair source: output is a tag (`lean`) plus an optional demotion
    veto that only fires when `policy.veto_enabled` is True AND the screen
    leans beyond `veto_margin` against the pick side. Insufficient data
    always yields a no-op signal.
    """
    p_over = ratings.p_over_25(home_team, away_team)
    if p_over is None:
        return ShotsTotalsSignal(p_over25=None, lean=None, veto=False, reason="insufficient_data")
    if p_over >= 0.5 + policy.veto_margin:
        lean: str | None = _LEAN_OVER
    elif p_over <= 0.5 - policy.veto_margin:
        lean = _LEAN_UNDER
    else:
        lean = None
    if lean is None:
        return ShotsTotalsSignal(p_over25=p_over, lean=None, veto=False, reason="no_lean")
    if lean == pick_side:
        return ShotsTotalsSignal(p_over25=p_over, lean=lean, veto=False, reason="agrees")
    veto = bool(policy.veto_enabled)
    reason = "veto_disagrees" if veto else "shadow_tag_disagrees"
    return ShotsTotalsSignal(p_over25=p_over, lean=lean, veto=veto, reason=reason)


@dataclass
class _LossAccumulator:
    log_loss_sum: float = 0.0
    brier_sum: float = 0.0
    n: int = 0

    def add(self, p: float, outcome: int) -> None:
        p = min(max(p, _PROB_EPS), 1.0 - _PROB_EPS)
        self.log_loss_sum += -(outcome * math.log(p) + (1 - outcome) * math.log(1.0 - p))
        self.brier_sum += (p - outcome) ** 2
        self.n += 1

    def mean_log_loss(self) -> float:
        return self.log_loss_sum / self.n if self.n else math.nan

    def mean_brier(self) -> float:
        return self.brier_sum / self.n if self.n else math.nan


def evaluate_walkforward_ou25(
    matches: Sequence[MatchStats],
    policy: ShotsPolicy,
    warmup: int = 200,
) -> OU25EvalResult:
    """Leakage-free walk-forward OU2.5 eval: for each match (chronological
    order is the caller's contract) predict FIRST with both the shots screen
    and the goals-only baseline, score against the realized total, THEN
    update both models. A match is scored only when both models can price it
    (apples-to-apples subset)."""
    shots_model = GapRatings(policy, mode="shots")
    baseline = GapRatings(policy, mode="goals")
    shots_acc = _LossAccumulator()
    base_acc = _LossAccumulator()

    for i, m in enumerate(matches):
        if i >= warmup:
            p_shots = shots_model.p_over_25(m.home_team, m.away_team)
            p_base = baseline.p_over_25(m.home_team, m.away_team)
            if p_shots is not None and p_base is not None:
                outcome = 1 if (m.home_goals + m.away_goals) >= 3 else 0
                shots_acc.add(p_shots, outcome)
                base_acc.add(p_base, outcome)
        # update strictly after prediction — never before
        shots_model.update(m)
        baseline.update(m)

    return OU25EvalResult(
        n_evaluated=shots_acc.n,
        shots_log_loss=shots_acc.mean_log_loss(),
        shots_brier=shots_acc.mean_brier(),
        baseline_log_loss=base_acc.mean_log_loss(),
        baseline_brier=base_acc.mean_brier(),
    )
