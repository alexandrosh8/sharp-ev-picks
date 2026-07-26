"""Wheatcroft GAP shots/corners OU2.5 shadow screen — pure-math tests.

Covers: blended-stat handling, Poisson tail math, rating monotonicity,
probability bounds, shadow-signal veto semantics (DISABLED by default —
operator shadow-first mandate), and a walk-forward eval smoke.
"""

import math
import random

from app.models.football_shots import (
    GapRatings,
    MatchStats,
    ShotsPolicy,
    blended_stat,
    evaluate_walkforward_ou25,
    poisson_p_over_25,
    shots_totals_signal,
)


def _match(
    home: str,
    away: str,
    hg: int,
    ag: int,
    hst: int | None,
    ast: int | None,
    hs: int | None = None,
    a_s: int | None = None,
    hc: int | None = None,
    ac: int | None = None,
) -> MatchStats:
    return MatchStats(
        home_team=home,
        away_team=away,
        home_goals=hg,
        away_goals=ag,
        home_shots=hs,
        away_shots=a_s,
        home_shots_on_target=hst,
        away_shots_on_target=ast,
        home_corners=hc,
        away_corners=ac,
    )


def _round_robin(
    teams: list[str], sot: dict[str, int], goals: dict[str, int], rounds: int = 8
) -> list[MatchStats]:
    out: list[MatchStats] = []
    for _ in range(rounds):
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                out.append(_match(h, a, goals[h], goals[a], sot[h], sot[a]))
    return out


# ---------------------------------------------------------------- blended stat


def test_blended_stat_requires_sot() -> None:
    policy = ShotsPolicy()
    assert blended_stat(None, 12, 5, policy) is None


def test_blended_stat_weights_components() -> None:
    policy = ShotsPolicy(weight_sot=1.0, weight_shots=0.2, weight_corners=0.1)
    got = blended_stat(5, 10, 4, policy)
    assert got is not None
    assert math.isclose(got, 5 + 2.0 + 0.4)


def test_blended_stat_tolerates_missing_secondary_components() -> None:
    policy = ShotsPolicy(weight_sot=1.0, weight_shots=0.2, weight_corners=0.1)
    assert blended_stat(5, None, None, policy) == 5.0


# ---------------------------------------------------------------- poisson tail


def test_poisson_p_over_25_matches_closed_form() -> None:
    lam = 2.0
    expected = 1.0 - math.exp(-lam) * (1.0 + lam + lam * lam / 2.0)
    assert math.isclose(poisson_p_over_25(lam), expected, rel_tol=1e-12)


def test_poisson_p_over_25_monotone_in_lambda() -> None:
    ps = [poisson_p_over_25(lam) for lam in (0.5, 1.5, 2.5, 3.5, 5.0)]
    assert all(0.0 < p < 1.0 for p in ps)
    assert ps == sorted(ps)


# ---------------------------------------------------------------- gap ratings


def test_ratings_insufficient_data_returns_none() -> None:
    policy = ShotsPolicy(min_team_matches=5)
    ratings = GapRatings(policy)
    assert ratings.p_over_25("A", "B") is None


def test_high_shot_teams_lean_more_over_than_low_shot_teams() -> None:
    policy = ShotsPolicy(min_team_matches=3)
    ratings = GapRatings(policy)
    sot = {"HI1": 9, "HI2": 8, "LO1": 2, "LO2": 1}
    goals = {"HI1": 3, "HI2": 2, "LO1": 0, "LO2": 1}
    for m in _round_robin(list(sot), sot, goals):
        ratings.update(m)
    p_hi = ratings.p_over_25("HI1", "HI2")
    p_lo = ratings.p_over_25("LO1", "LO2")
    assert p_hi is not None
    assert p_lo is not None
    assert 0.0 < p_lo < p_hi < 1.0


def test_probabilities_always_in_open_unit_interval() -> None:
    policy = ShotsPolicy(min_team_matches=2)
    ratings = GapRatings(policy)
    rng = random.Random(7)
    teams = [f"T{i}" for i in range(6)]
    for _ in range(300):
        h, a = rng.sample(teams, 2)
        ratings.update(
            _match(
                h,
                a,
                rng.randint(0, 6),
                rng.randint(0, 6),
                rng.randint(0, 15),
                rng.randint(0, 15),
                rng.randint(0, 30),
                rng.randint(0, 30),
                rng.randint(0, 15),
                rng.randint(0, 15),
            )
        )
    for h in teams:
        for a in teams:
            if h == a:
                continue
            p = ratings.p_over_25(h, a)
            assert p is not None
            assert 0.0 < p < 1.0


def test_update_without_shots_data_is_a_noop_for_shots_mode() -> None:
    policy = ShotsPolicy(min_team_matches=1)
    ratings = GapRatings(policy)
    ratings.update(_match("A", "B", 2, 1, None, None))
    assert ratings.p_over_25("A", "B") is None  # nothing learned


def test_goals_mode_learns_from_goals_only() -> None:
    policy = ShotsPolicy(min_team_matches=2, min_league_matches=5)
    baseline = GapRatings(policy, mode="goals")
    for m in _round_robin(["A", "B"], {"A": 0, "B": 0}, {"A": 3, "B": 2}, rounds=6):
        baseline.update(m)  # no shots data at all
    p = baseline.p_over_25("A", "B")
    assert p is not None
    assert p > 0.5  # 5-goal matches lean over


# ---------------------------------------------------------------- shadow signal


def test_signal_veto_disabled_by_default_even_on_disagreement() -> None:
    policy = ShotsPolicy(min_team_matches=3, min_league_matches=10)  # veto default: False
    ratings = GapRatings(policy)
    sot = {"HI1": 9, "HI2": 8}
    goals = {"HI1": 3, "HI2": 2}
    for m in _round_robin(list(sot), sot, goals):
        ratings.update(m)
    sig = shots_totals_signal(ratings, "HI1", "HI2", pick_side="under", policy=policy)
    assert sig.lean == "over"
    assert sig.veto is False  # shadow-first: tag only, never veto by default


def test_signal_veto_fires_only_when_enabled_and_disagreeing() -> None:
    policy = ShotsPolicy(min_team_matches=3, min_league_matches=10, veto_enabled=True)
    ratings = GapRatings(policy)
    sot = {"HI1": 9, "HI2": 8}
    goals = {"HI1": 3, "HI2": 2}
    for m in _round_robin(list(sot), sot, goals):
        ratings.update(m)
    veto_sig = shots_totals_signal(ratings, "HI1", "HI2", pick_side="under", policy=policy)
    agree_sig = shots_totals_signal(ratings, "HI1", "HI2", pick_side="over", policy=policy)
    assert veto_sig.veto is True
    assert agree_sig.veto is False


def test_signal_insufficient_data_never_vetoes() -> None:
    policy = ShotsPolicy(min_team_matches=5, veto_enabled=True)
    ratings = GapRatings(policy)
    sig = shots_totals_signal(ratings, "X", "Y", pick_side="over", policy=policy)
    assert sig.p_over25 is None
    assert sig.lean is None
    assert sig.veto is False
    assert sig.reason == "insufficient_data"


# ---------------------------------------------------------------- walk-forward


def test_walkforward_smoke_on_synthetic_history() -> None:
    rng = random.Random(42)
    teams = [f"T{i}" for i in range(8)]
    strength = {t: rng.uniform(0.6, 2.2) for t in teams}
    matches: list[MatchStats] = []
    for _ in range(60):
        for h in teams:
            a = rng.choice([t for t in teams if t != h])
            lam_h = strength[h] * 1.2
            lam_a = strength[a]
            hg = min(8, int(rng.expovariate(1.0 / max(lam_h, 0.1))))
            ag = min(8, int(rng.expovariate(1.0 / max(lam_a, 0.1))))
            hst = max(0, int(lam_h * 3 + rng.gauss(0, 1.5)))
            ast = max(0, int(lam_a * 3 + rng.gauss(0, 1.5)))
            matches.append(_match(h, a, hg, ag, hst, ast, hst * 3, ast * 3, hst, ast))
    result = evaluate_walkforward_ou25(matches, ShotsPolicy(min_team_matches=4), warmup=120)
    assert result.n_evaluated > 100
    assert math.isfinite(result.shots_log_loss)
    assert math.isfinite(result.baseline_log_loss)
    assert result.shots_log_loss > 0.0
    assert result.shots_brier > 0.0
    # sanity: neither model should be wildly worse than coin-flip entropy x2
    assert result.shots_log_loss < 2.0 * math.log(2.0)
