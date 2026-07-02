"""WP3 backtest-honesty locks (audit 2026-07-01, patch 13).

Two defects made the "+22.4% ROI / CLV +0.107 > 2SE" headline indefensible:

1. FILL UNIVERSE — the backtest fills at football-data's gross Max across ALL
   books (exchanges included, gross of commission) while live fills at the best
   soft book, or an exchange net of commission. ``--fill-universe soft`` now
   fills at the best NAMED soft book only (sharp Pinnacle excluded; Betfair
   Exchange prices enter only NET of commission).
2. I.I.D. SEs — same-match 1X2+OU picks are correlated; treating them as
   independent understates the SE feeding the >2SE verdict. ``cluster_robust_se``
   (app/backtesting/clv.py, pure numpy/stdlib) provides the by-match
   cluster-robust SE; Stats carries it alongside the old i.i.d. SE.

No network, synthetic rows only. This system places no bets.
"""

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

import pytest

from app.backtesting.clv import cluster_robust_se
from app.probabilities.devig import DevigMethod, devig

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(path: Path, name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vb: Any = _load(_SCRIPTS / "value_backtest.py", "value_backtest")


# ---------------------------------------------------------------------------
# cluster_robust_se — pure math (hand-computed fixtures)
# ---------------------------------------------------------------------------
def test_cluster_robust_se_hand_computed() -> None:
    # values [1,2,3,4], clusters {a: [1,2], b: [3,4]}; mean = 2.5
    # e_a = (1-2.5)+(2-2.5) = -2 ; e_b = (3-2.5)+(4-2.5) = +2 ; sum e^2 = 8
    # SE = sqrt(G/(G-1) * sum e_g^2) / n = sqrt(2/1 * 8) / 4 = 1.0
    se = cluster_robust_se([1.0, 2.0, 3.0, 4.0], ["a", "a", "b", "b"])
    assert se == pytest.approx(1.0)


def test_cluster_robust_se_singletons_reduce_to_iid_ddof1() -> None:
    # With every observation its own cluster the sandwich collapses to the
    # classic i.i.d. sample SE (ddof=1) — the property that makes the two
    # columns comparable in the report.
    xs = [0.05, -0.02, 0.01]
    m = sum(xs) / 3
    iid = math.sqrt(sum((x - m) ** 2 for x in xs) / 2) / math.sqrt(3)
    se = cluster_robust_se(xs, ["a", "b", "c"])
    assert se == pytest.approx(iid)


def test_cluster_robust_se_within_cluster_correlation_inflates_se() -> None:
    # Perfectly duplicated same-cluster observations: the i.i.d. SE pretends
    # n=4 independent draws; the clustered SE knows there are only 2.
    xs = [1.0, 1.0, 3.0, 3.0]
    m = sum(xs) / 4
    iid = math.sqrt(sum((x - m) ** 2 for x in xs) / 3) / math.sqrt(4)
    se = cluster_robust_se(xs, ["a", "a", "b", "b"])
    assert se is not None
    assert se == pytest.approx(1.0)  # e_a=-2, e_b=+2 -> sqrt(2*8)/4
    assert se > iid


def test_cluster_robust_se_degenerate_inputs() -> None:
    assert cluster_robust_se([], []) is None  # nothing to estimate
    assert cluster_robust_se([1.0, 2.0], ["a", "a"]) is None  # G<2: undefined
    with pytest.raises(ValueError):
        cluster_robust_se([1.0, 2.0], ["a"])  # length mismatch


# ---------------------------------------------------------------------------
# Stats — cluster-robust SEs alongside the old i.i.d. SEs
# ---------------------------------------------------------------------------
def test_stats_carries_cluster_se_next_to_iid_se() -> None:
    bets = [
        vb.VBet(won=True, odds=2.0, edge=0.03, clv_pinn=0.05, clv_max=None, cluster="m1"),
        vb.VBet(won=False, odds=2.0, edge=0.03, clv_pinn=0.03, clv_max=None, cluster="m1"),
        vb.VBet(won=True, odds=2.0, edge=0.03, clv_pinn=0.01, clv_max=None, cluster="m2"),
    ]
    s = vb.Stats.from_bets(bets)
    # mean 0.03; e_m1 = 0.02, e_m2 = -0.02; SE = sqrt(2 * 0.0008)/3 = 0.04/3
    assert s.clv_pinn == pytest.approx(0.03)
    assert s.clv_pinn_se_cl == pytest.approx(0.04 / 3)
    assert s.clv_pinn_se is not None  # old i.i.d. SE stays visible
    assert s.clv_pinn_se_cl > s.clv_pinn_se  # correlation acknowledged
    # ROI SE is clustered too (pnl per cluster)
    assert s.roi_se_cl is not None


def test_stats_without_cluster_ids_degrades_to_iid() -> None:
    # Back-compat: VBet.cluster defaults to None -> every bet is its own
    # cluster and the clustered SE equals the i.i.d. one (never fake-smaller).
    bets = [
        vb.VBet(won=True, odds=2.0, edge=0.02, clv_pinn=x, clv_max=None)
        for x in (0.05, -0.02, 0.01)
    ]
    s = vb.Stats.from_bets(bets)
    assert s.clv_pinn_se_cl == pytest.approx(s.clv_pinn_se)


# ---------------------------------------------------------------------------
# bets_for — fill universes
# ---------------------------------------------------------------------------
def _row_1x2(**extra: str) -> dict[str, str]:
    """Home value bet: sharp fair ~0.50, best prices above 2.0."""
    row = {
        "FTR": "H",
        "FTHG": "2",
        "FTAG": "1",
        "PSH": "1.95",
        "PSD": "3.60",
        "PSA": "4.30",
        "MaxH": "2.25",
        "MaxD": "3.80",
        "MaxA": "4.60",
    }
    row.update(extra)
    return row


def test_fill_universe_default_max_is_unchanged() -> None:
    bets = vb.bets_for([_row_1x2()], 0.0, DevigMethod.POWER, ("1x2",), 1.0)
    assert len(bets) == 1
    assert bets[0].odds == pytest.approx(2.25)  # gross Max — old behavior


def test_fill_universe_soft_uses_best_named_soft_book() -> None:
    row = _row_1x2(
        B365H="2.10",
        B365D="3.40",
        B365A="4.00",
        BWH="2.05",
        BWD="3.30",
        BWA="3.90",
    )
    bets = vb.bets_for([row], 0.0, DevigMethod.POWER, ("1x2",), 1.0, fill_universe="soft")
    assert len(bets) == 1
    b = bets[0]
    assert b.odds == pytest.approx(2.10)  # best soft, NOT the 2.25 gross Max
    # the decision edge is computed against the soft fill, not the Max
    fair = devig([1.95, 3.60, 4.30], method=DevigMethod.POWER)
    assert b.edge == pytest.approx(fair[0] - 1.0 / 2.10)


def test_fill_universe_soft_nets_exchange_commission() -> None:
    # BFE 2.30 gross nets to 1 + 1.30*0.95 = 2.235 at 5% commission — it may
    # beat the soft 2.10 only AFTER netting; the gross 2.30 is never used.
    row = _row_1x2(B365H="2.10", B365D="3.40", B365A="4.00", BFEH="2.30")
    bets = vb.bets_for([row], 0.0, DevigMethod.POWER, ("1x2",), 1.0, fill_universe="soft")
    assert len(bets) == 1
    assert bets[0].odds == pytest.approx(2.235)


def test_fill_universe_soft_excludes_sharp_and_gross_max() -> None:
    # A row with ONLY the sharp anchor and the composite Max has no takeable
    # soft price -> no bet in the soft universe (never fall back to PS/Max).
    bets = vb.bets_for([_row_1x2()], 0.0, DevigMethod.POWER, ("1x2",), 1.0, fill_universe="soft")
    assert bets == []


def test_bets_share_cluster_per_match_across_markets() -> None:
    row = _row_1x2(**{"P>2.5": "1.90", "P<2.5": "1.90", "Max>2.5": "2.10", "Max<2.5": "2.05"})
    row2 = _row_1x2()
    bets = vb.bets_for([row, row2], 0.0, DevigMethod.POWER, ("1x2", "ou25"), 1.0)
    clusters = [b.cluster for b in bets]
    assert len(bets) == 3  # row: 1x2 + ou25; row2: 1x2
    assert clusters[0] is not None
    assert clusters[0] == clusters[1]  # same match -> same cluster
    assert clusters[2] != clusters[0]  # different match -> different cluster
