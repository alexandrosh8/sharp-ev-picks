"""Synthetic tests for the per-(league-tier x market) threshold control.

Loads scripts/ml/optimize_thresholds.py by path (scripts/ is not a package)
and exercises the pure DataFrame logic on synthetic frames — no network, no
parquet, no files written. Locks the two protocol-critical behaviors:
per-cell selection is TRAIN-only, and the min-n guard falls back to the
global 0.03.
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

# importorskip-then-bind: a bare `import pandas` would abort COLLECTION in a
# no-extras env (optimize_thresholds.py imports pandas at module level too).
pd = pytest.importorskip("pandas")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml" / "optimize_thresholds.py"
_spec = importlib.util.spec_from_file_location("optimize_thresholds", _SCRIPT)
assert _spec is not None and _spec.loader is not None
ot: Any = importlib.util.module_from_spec(_spec)
sys.modules["optimize_thresholds"] = ot
_spec.loader.exec_module(ot)


def _cand(
    *,
    league: str = "E0",
    season: str = "2122",
    match: str = "m1",
    market: str = "1x2",
    selection: str = "H",
    edge: float = 0.02,
    best_price: float = 2.0,
    won: bool = True,
    clv_pinn: float | None = 0.01,
    clv_max: float | None = 0.005,
    pinn_price: float = 1.9,
    pinn_close_fair: float = 0.5,
    max_close_fair: float = 0.5,
) -> dict[str, object]:
    """One synthetic candidate row in the value_candidates.parquet shape."""
    return {
        "league": league,
        "season": season,
        "match_date": date(2021, 9, 4),
        "home_team": f"home_{match}",
        "away_team": f"away_{match}",
        "market": market,
        "selection": selection,
        "edge": edge,
        "best_price": best_price,
        "pinn_price": pinn_price,
        "won": won,
        "profit_units": (best_price - 1.0) if won else -1.0,
        "clv_pinn": clv_pinn,
        "clv_max": clv_max,
        "pinn_close_fair": pinn_close_fair,
        "max_close_fair": max_close_fair,
    }


def _frame(rows: list[dict[str, object]]) -> Any:  # pd.DataFrame (pd bound via importorskip)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Tier mapping
# ---------------------------------------------------------------------------
def test_tier_mapping_covers_the_18_league_universe() -> None:
    assert set(ot.TIER_OF_LEAGUE) == set(ot.LEAGUES_18)
    assert {ot.league_tier(lg) for lg in ot.LEAGUES_18} == set(ot.TIERS)
    assert {lg for lg, t in ot.TIER_OF_LEAGUE.items() if t == "top5"} == {
        "E0",
        "D1",
        "I1",
        "SP1",
        "F1",
    }
    assert {lg for lg, t in ot.TIER_OF_LEAGUE.items() if t == "secondary"} == {
        "SC0",
        "N1",
        "B1",
        "P1",
        "T1",
        "G1",
    }


def test_unknown_league_is_a_hard_error_not_a_silent_default() -> None:
    with pytest.raises(ValueError, match="EC"):
        ot.league_tier("EC")
    with pytest.raises(ValueError, match="SC1"):
        ot.add_tier(_frame([_cand(league="SC1")]))


# ---------------------------------------------------------------------------
# Bet selection: one per (match, market), argmax edge, odds floor
# ---------------------------------------------------------------------------
def test_select_bets_one_per_match_market_argmax_with_odds_floor() -> None:
    rows = [
        # 1x2: top edge fails the 1.6 floor -> next-best eligible is bet
        _cand(match="m1", selection="H", edge=0.05, best_price=1.50),
        _cand(match="m1", selection="D", edge=0.03, best_price=3.00),
        _cand(match="m1", selection="A", edge=0.025, best_price=4.00),
        # same match, other market: independent bet
        _cand(match="m1", market="ou25", selection="over", edge=0.04, best_price=2.10),
        # below threshold -> no bet for this match
        _cand(match="m2", selection="H", edge=0.01, best_price=2.50),
    ]
    bets = ot.select_bets(_frame(rows), ot.uniform_policy(0.02), min_odds=1.6)
    assert len(bets) == 2  # one per (match, market)
    one_x_two = bets[bets["market"] == "1x2"]
    assert list(one_x_two["selection"]) == ["D"]  # argmax AFTER the odds floor
    assert set(bets["market"]) == {"1x2", "ou25"}


def test_select_bets_applies_the_cell_specific_threshold() -> None:
    rows = [
        _cand(league="E0", match="m1", edge=0.018),  # top5 cell
        _cand(league="E2", match="m2", edge=0.018),  # lower cell
    ]
    policy = ot.uniform_policy(0.005)
    policy[("top5", "1x2")] = 0.02  # only the top5 cell tightens
    bets = ot.select_bets(_frame(rows), policy, min_odds=1.6)
    assert list(bets["league"]) == ["E2"]


def test_select_bets_rejects_a_policy_with_missing_cells() -> None:
    with pytest.raises(ValueError, match="no threshold"):
        ot.select_bets(_frame([_cand()]), {("lower", "ou25"): 0.01}, min_odds=1.6)


# ---------------------------------------------------------------------------
# Per-cell grid search: TRAIN-only + min-n fallback
# ---------------------------------------------------------------------------
def _train_cell_frame() -> Any:  # pd.DataFrame (pd bound via importorskip)
    """top5/1x2 cell where thr=0.02 is train-optimal on mean clv_max:
    10 high-edge bets at clv_max +0.10 vs 6 low-edge bets at clv_max -0.50."""
    rows = [
        _cand(match=f"hi{i}", edge=0.021, clv_max=0.10, clv_pinn=0.10, won=True) for i in range(10)
    ]
    rows += [
        _cand(match=f"lo{i}", edge=0.006, clv_max=-0.50, clv_pinn=-0.50, won=False)
        for i in range(6)
    ]
    return _frame(rows)


def test_per_cell_threshold_is_chosen_on_train_only() -> None:
    train = _train_cell_frame()
    # held-out data engineered so 0.005 would win if it leaked into selection
    test = _frame(
        [_cand(match=f"t{i}", edge=0.006, clv_max=0.9, clv_pinn=0.9) for i in range(50)]
        + [_cand(match=f"u{i}", edge=0.021, clv_max=-0.9, clv_pinn=-0.9) for i in range(50)]
    )
    policy, choices, _ = ot.optimize_cells(
        train, grid=(0.005, 0.02), min_n=5, fallback_thr=0.03, min_odds=1.6
    )
    # optimize_cells never sees `test`; the choice is the TRAIN optimum
    assert policy[("top5", "1x2")] == 0.02
    choice = next(c for c in choices if (c.tier, c.market) == ("top5", "1x2"))
    assert choice.source == "optimized"
    assert choice.train.n == 10
    # the engineered held-out frame would have preferred 0.005 — prove it
    s_low = ot.summarize(ot.select_bets(test, {("top5", "1x2"): 0.005}, 1.6))
    s_high = ot.summarize(ot.select_bets(test, {("top5", "1x2"): 0.02}, 1.6))
    assert s_low.clv_max is not None and s_high.clv_max is not None
    assert s_low.clv_max > s_high.clv_max  # leak would have flipped the choice


def test_min_n_guard_falls_back_to_the_global_premium_threshold() -> None:
    # 3 bets per threshold in the cell — never reaches min_n=150
    train = _frame([_cand(match=f"m{i}", edge=0.05) for i in range(3)])
    policy, choices, _ = ot.optimize_cells(
        train, grid=(0.005, 0.02), min_n=150, fallback_thr=0.03, min_odds=1.6
    )
    choice = next(c for c in choices if (c.tier, c.market) == ("top5", "1x2"))
    assert choice.source == "fallback"
    assert choice.threshold == 0.03
    assert policy[("top5", "1x2")] == 0.03
    # cells with zero candidates fall back too (the other five cells here)
    assert all(policy[cell] == 0.03 for cell in policy if cell != ("top5", "1x2"))


def test_ineligible_threshold_never_wins_despite_better_clv() -> None:
    rows = [_cand(match=f"a{i}", edge=0.006, clv_max=0.01, clv_pinn=0.01) for i in range(6)]
    rows += [_cand(match=f"b{i}", edge=0.021, clv_max=5.0, clv_pinn=5.0) for i in range(2)]
    policy, choices, _ = ot.optimize_cells(
        _frame(rows), grid=(0.005, 0.02), min_n=5, fallback_thr=0.03, min_odds=1.6
    )
    assert policy[("top5", "1x2")] == 0.005  # 0.02 has n=2 < 5 -> ineligible
    choice = next(c for c in choices if (c.tier, c.market) == ("top5", "1x2"))
    assert choice.source == "optimized"


# ---------------------------------------------------------------------------
# Statistics parity + bootstrap
# ---------------------------------------------------------------------------
def test_summarize_matches_value_backtest_stats_math() -> None:
    vb = sys.modules["value_backtest"]
    bets = [
        ("w", 2.4, 0.05, 0.04),
        ("l", 3.0, -0.02, None),  # missing clv_max kept for ROI
        ("w", 1.8, 0.01, 0.00501),
    ]
    vbets = [
        vb.VBet(won=(w == "w"), odds=o, edge=0.02, clv_pinn=cp, clv_max=cm) for w, o, cp, cm in bets
    ]
    expected = vb.Stats.from_bets(vbets)
    got = ot.summarize(
        _frame(
            [
                _cand(match=f"m{i}", won=(w == "w"), best_price=o, clv_pinn=cp, clv_max=cm)
                for i, (w, o, cp, cm) in enumerate(bets)
            ]
        )
    )
    assert got.n == expected.n
    assert got.hit == pytest.approx(expected.hit)
    assert got.roi == pytest.approx(expected.roi)
    assert got.clv_pinn == pytest.approx(expected.clv_pinn)
    assert got.clv_pinn_se == pytest.approx(expected.clv_pinn_se)
    assert got.clv_max == pytest.approx(expected.clv_max)
    assert got.clv_max_se == pytest.approx(expected.clv_max_se)
    assert got.beat_pinn == pytest.approx(expected.beat_pinn)
    assert got.n_missing_clv_max == 1


def test_clv_se_uses_sample_std_ddof1_not_population() -> None:
    # The SE feeding the >2SE adoption gate must be the UNBIASED sample SE
    # (ddof=1), not the population SE (ddof=0) which is too small.
    import math as _m

    vb = sys.modules["value_backtest"]
    xs = [0.05, -0.02, 0.01]
    s = vb.Stats.from_bets(
        [vb.VBet(won=True, odds=2.0, edge=0.02, clv_pinn=x, clv_max=None) for x in xs]
    )
    m = sum(xs) / 3
    sample_se = _m.sqrt(sum((x - m) ** 2 for x in xs) / 2) / _m.sqrt(3)  # ddof=1
    pop_se = _m.sqrt(sum((x - m) ** 2 for x in xs) / 3) / _m.sqrt(3)  # ddof=0
    assert s.clv_pinn_se == pytest.approx(sample_se)
    assert s.clv_pinn_se > pop_se  # strictly larger than the old population SE


def test_single_observation_clv_se_is_none_not_fake_zero() -> None:
    # n<2 has no defined sample variance: SE must be None (not a fake-zero that
    # mints spurious >2SE significance from one observation).
    vb = sys.modules["value_backtest"]
    s = vb.Stats.from_bets([vb.VBet(won=True, odds=2.0, edge=0.02, clv_pinn=0.03, clv_max=None)])
    assert s.clv_pinn == pytest.approx(0.03)
    assert s.clv_pinn_se is None


def test_bootstrap_is_deterministic_and_zero_for_identical_sets() -> None:
    bets = _frame(
        [
            _cand(match=f"m{i}", clv_pinn=0.01 * i, clv_max=0.02 * i, won=i % 2 == 0)
            for i in range(8)
        ]
    )
    a = ot.bootstrap_cis(bets, bets, n_boot=200, seed=42)
    b = ot.bootstrap_cis(bets, bets, n_boot=200, seed=42)
    assert a == b  # same seed -> identical draws
    # policy == null -> incremental CLV is exactly zero, CI collapses to zero
    assert a["inc_clv_pinn"].point == pytest.approx(0.0)
    assert a["inc_clv_max"].point == pytest.approx(0.0)
    assert a["inc_clv_max"].lo == pytest.approx(0.0)
    assert a["inc_clv_max"].hi == pytest.approx(0.0)
    assert a["roi"].lo <= a["roi"].point <= a["roi"].hi


def test_bootstrap_single_cluster_collapses_to_the_point_estimate() -> None:
    # both bets share one match -> one cluster -> every resample is identical
    bets = _frame(
        [
            _cand(match="m1", market="1x2", clv_pinn=0.05, clv_max=0.03, won=True),
            _cand(match="m1", market="ou25", clv_pinn=-0.01, clv_max=-0.02, won=False),
        ]
    )
    cis = ot.bootstrap_cis(bets, bets, n_boot=50, seed=7)
    assert cis["roi"].lo == pytest.approx(cis["roi"].point)
    assert cis["roi"].hi == pytest.approx(cis["roi"].point)
    assert cis["roi"].se == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Null generator reuses the canonical bets_for; haircut re-pricing
# ---------------------------------------------------------------------------
def _csv_row_maxavg() -> dict[str, str]:
    return {
        "Div": "E0",
        "Date": "16/08/2024",
        "HomeTeam": "Alpha",
        "AwayTeam": "Beta",
        "FTHG": "2",
        "FTAG": "1",
        "FTR": "H",
        "PSH": "2.00",
        "PSD": "3.50",
        "PSA": "4.00",
        "MaxH": "2.20",
        "MaxD": "3.55",
        "MaxA": "4.05",
        "PSCH": "1.90",
        "PSCD": "3.60",
        "PSCA": "4.40",
        "MaxCH": "2.00",
        "MaxCD": "3.70",
        "MaxCA": "4.50",
        "P>2.5": "1.90",
        "P<2.5": "2.00",
        "Max>2.5": "2.30",
        "Max<2.5": "2.02",
        "PC>2.5": "1.85",
        "PC<2.5": "2.05",
        "MaxC>2.5": "1.95",
        "MaxC<2.5": "2.10",
    }


def test_null_rows_carry_match_identity_and_match_canonical_bets_for() -> None:
    vb = sys.modules["value_backtest"]
    row = _csv_row_maxavg()
    null = ot.null_rows_for_match_rows("E0", "2425", [row], min_odds=1.6)
    direct = vb.bets_for([row], 0.0, ot.CANONICAL_DEVIG, ot.MARKETS, 1.6)
    assert len(null) == len(direct) > 0
    for rec in null:
        assert rec["league"] == "E0"
        assert rec["match_date"] == date(2024, 8, 16)
        assert rec["home_team"] == "Alpha" and rec["away_team"] == "Beta"
        assert rec["market"] in ot.MARKETS
    assert sorted(r["best_price"] for r in null) == sorted(b.odds for b in direct)
    assert sorted(r["clv_pinn"] for r in null) == sorted(b.clv_pinn for b in direct)


def test_haircut_reprices_the_same_selections_at_pinnacle() -> None:
    import math

    bets = _frame(
        [
            _cand(
                match="m1",
                won=True,
                best_price=2.2,
                pinn_price=2.0,
                pinn_close_fair=0.5,
                max_close_fair=0.48,
            )
        ]
    )
    cut = ot.haircut_at_pinnacle(bets)
    assert len(cut) == len(bets)
    assert cut.iloc[0]["profit_units"] == pytest.approx(1.0)  # 2.0 - 1, not 2.2 - 1
    assert cut.iloc[0]["clv_pinn"] == pytest.approx(math.log(2.0 * 0.5))
    assert cut.iloc[0]["clv_max"] == pytest.approx(math.log(2.0 * 0.48))


def test_verdict_can_say_no_proven_edge() -> None:
    tiny = ot.summarize(_frame([_cand()]))  # n=1 < 50
    null = ot.summarize(_frame([_cand(match="m2")]))
    cis = ot.bootstrap_cis(_frame([_cand()]), _frame([_cand(match="m2")]), n_boot=10, seed=1)
    assert "NO PROVEN EDGE" in ot.compute_verdict(tiny, null, cis)
