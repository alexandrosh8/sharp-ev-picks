"""Unit tests for the anchor-calibration diagnostic (pure module).

Pure math — no DB/IO. Verifies log-loss / Brier golden values, the n-weighted
ECE/MCE, the reliability binning, the insufficient-n honesty gate (estimates
nulled below MIN_CALIBRATION_N), and the perfect- vs biased-calibration cases.
"""

import math

import pytest

from app.backtesting.calibration import (
    MIN_CALIBRATION_N,
    BetBandObservation,
    CalibrationObservation,
    bet_band_reliability,
    calibration_report,
)


def _obs(pairs: list[tuple[float, bool]]) -> list[CalibrationObservation]:
    return [CalibrationObservation(fair_prob=p, won=w) for p, w in pairs]


def _repeat(pairs: list[tuple[float, bool]], times: int) -> list[CalibrationObservation]:
    out: list[CalibrationObservation] = []
    for _ in range(times):
        out.extend(_obs(pairs))
    return out


def test_insufficient_below_min_nulls_every_estimate() -> None:
    rep = calibration_report(_obs([(0.6, True)] * (MIN_CALIBRATION_N - 1)))
    assert rep.insufficient is True
    assert rep.n == MIN_CALIBRATION_N - 1
    assert rep.log_loss is None
    assert rep.ignorance is None
    assert rep.brier is None
    assert rep.ece is None
    assert rep.mce is None
    assert rep.base_rate is None
    assert rep.mean_pred is None
    assert rep.bins == ()


def test_empty_input_is_insufficient_not_a_crash() -> None:
    rep = calibration_report([])
    assert rep.n == 0
    assert rep.insufficient is True
    assert rep.log_loss is None


def test_log_loss_and_brier_golden_values() -> None:
    # 100 obs all at p=0.5, half won half lost -> log_loss = -ln(0.5) = ln 2,
    # brier = 0.25, base_rate = mean_pred = 0.5. min_n lowered so it scores.
    rep = calibration_report(_obs([(0.5, True), (0.5, False)] * 50), min_n=10)
    assert rep.insufficient is False
    assert rep.n == 100
    assert rep.log_loss is not None and math.isclose(rep.log_loss, math.log(2.0), rel_tol=1e-12)
    # ignorance = log_loss / ln(2) = 1.0 bit at p=0.5
    assert rep.ignorance is not None and math.isclose(rep.ignorance, 1.0, rel_tol=1e-12)
    assert rep.brier is not None and math.isclose(rep.brier, 0.25, rel_tol=1e-12)
    assert rep.base_rate is not None and math.isclose(rep.base_rate, 0.5, rel_tol=1e-12)
    assert rep.mean_pred is not None and math.isclose(rep.mean_pred, 0.5, rel_tol=1e-12)


def test_perfectly_calibrated_set_has_zero_ece() -> None:
    # Each probability band's observed frequency exactly equals its predicted
    # value -> ECE and MCE are 0. Bands: 0.2 (1/5 win), 0.5 (1/2), 0.8 (4/5).
    block = (
        [(0.2, True)]
        + [(0.2, False)] * 4  # bin 0.2: 1/5 = 0.2
        + [(0.5, True), (0.5, False)]  # bin 0.5: 1/2 = 0.5
        + [(0.8, True)] * 4
        + [(0.8, False)]  # bin 0.8: 4/5 = 0.8
    )
    rep = calibration_report(_repeat(block, 10), min_n=10)  # 12*10 = 120 obs
    assert rep.insufficient is False
    assert rep.ece is not None and math.isclose(rep.ece, 0.0, abs_tol=1e-12)
    assert rep.mce is not None and math.isclose(rep.mce, 0.0, abs_tol=1e-12)


def test_badly_calibrated_set_has_large_ece() -> None:
    # Anchor always says 0.8 but the bets win only 30% of the time -> the 0.8
    # bin's gap is 0.5, and since all mass is in one bin, ECE == MCE == 0.5.
    block = [(0.8, True)] * 3 + [(0.8, False)] * 7  # 3/10 = 0.3 observed
    rep = calibration_report(_repeat(block, 8), min_n=10)  # 80 obs
    assert rep.insufficient is False
    assert rep.ece is not None and math.isclose(rep.ece, 0.5, abs_tol=1e-9)
    assert rep.mce is not None and math.isclose(rep.mce, 0.5, abs_tol=1e-9)
    assert rep.base_rate is not None and math.isclose(rep.base_rate, 0.3, rel_tol=1e-9)


def test_bins_partition_all_observations_and_carry_their_n() -> None:
    obs = _obs([(0.05, False), (0.55, True), (0.95, True)] * 30)  # 90 obs
    rep = calibration_report(obs, n_bins=10, min_n=10)
    # every observation is counted exactly once across the bins
    assert sum(b.n for b in rep.bins) == rep.n == 90
    assert len(rep.bins) == 10
    # empty bins expose None estimates (not 0.0 noise)
    for b in rep.bins:
        if b.n == 0:
            assert b.mean_pred is None and b.observed is None
        else:
            assert b.mean_pred is not None and b.observed is not None


def test_prob_one_lands_in_last_bin_no_overflow() -> None:
    # p == 1.0 must bucket into the final bin, not raise an index error.
    rep = calibration_report(_obs([(1.0, True), (0.0, False)] * 40))  # 80 obs
    assert rep.insufficient is False
    assert rep.bins[-1].n == 40  # all the 1.0s
    assert rep.bins[0].n == 40  # all the 0.0s
    assert rep.log_loss is not None and math.isfinite(rep.log_loss)  # clamping


def test_n_bins_must_be_positive() -> None:
    with pytest.raises(ValueError, match="n_bins"):
        calibration_report(_obs([(0.5, True)] * 60), n_bins=0)


# --- P1-1: claimed-fair reliability monitor (bet-band scoped) -----------------


def _bet(pairs: list[tuple[float, bool, float]]) -> list[BetBandObservation]:
    return [
        BetBandObservation(claimed_fair=p, won=w, fill_odds=o) for p, w, o in pairs
    ]


def test_bet_band_reliability_zero_ece_when_claims_match_outcomes() -> None:
    # A well-calibrated, in-band book: claimed 0.5 wins ~half, claimed 0.8 wins
    # ~80%. All fill odds sit inside the [p10, p90] band -> none excluded, and
    # ECE ~ 0 because each claim band's observed frequency matches the claim.
    block = (
        [(0.5, True, 2.00), (0.5, False, 2.00)]  # claim 0.5 -> 1/2 observed
        + [(0.8, True, 1.50)] * 4
        + [(0.8, False, 1.50)]  # claim 0.8 -> 4/5 observed
    )
    obs = _bet(block * 20)  # 7 entries/block * 20 = 140 in-band observations
    rep = bet_band_reliability(obs, min_n=10)
    assert rep["insufficient"] is False
    # only two odds values (1.50, 2.00) -> p10..p90 spans both -> none excluded
    assert rep["n_in_band"] == 140
    assert rep["ece"] is not None and math.isclose(rep["ece"], 0.0, abs_tol=1e-9)


def test_bet_band_reliability_overconfident_fixture_shows_high_ece() -> None:
    # Deliberately OVERCONFIDENT: every bet claims a 0.80 fair, but the bets win
    # only 30% of the time. The 0.8 bin's gap is 0.5 and all mass is in one bin
    # -> ECE == 0.5. This is exactly the drift the monitor must surface.
    block = [(0.80, True, 2.00)] * 3 + [(0.80, False, 2.00)] * 7  # 3/10 observed
    rep = bet_band_reliability(_bet(block * 8), min_n=10)  # 80 obs
    assert rep["insufficient"] is False
    assert rep["ece"] is not None and math.isclose(rep["ece"], 0.5, abs_tol=1e-9)
    assert rep["base_rate"] is not None and math.isclose(rep["base_rate"], 0.3, rel_tol=1e-9)
    assert rep["mean_pred"] is not None and math.isclose(rep["mean_pred"], 0.8, rel_tol=1e-9)


def test_bet_band_reliability_scopes_to_actually_bet_odds_band() -> None:
    # The band is the population's own [p10, p90] fill-odds range: extreme
    # longshots/heavy-favourites outside it are EXCLUDED, so they cannot define
    # or distort the in-band calibration verdict.
    core = [(0.5, True, 2.00), (0.5, False, 2.00)] * 30  # 60 bets clustered at 2.00
    tails = [(0.99, True, 1.01), (0.02, False, 50.0)]  # 2 extreme-odds outliers
    rep = bet_band_reliability(_bet(core + tails), min_n=10)
    # the two tail bets fall outside [p10, p90] of {1.01, 2.00*60, 50.0}
    assert rep["n_total"] == 62
    assert rep["n_in_band"] == 60
    assert rep["band_lo_odds"] is not None and rep["band_lo_odds"] >= 1.5
    assert rep["band_hi_odds"] is not None and rep["band_hi_odds"] <= 2.5


def test_bet_band_reliability_insufficient_below_min_n() -> None:
    rep = bet_band_reliability(_bet([(0.6, True, 2.0)] * 5), min_n=50)
    assert rep["insufficient"] is True
    assert rep["ece"] is None  # nulled at source by the honesty gate
    assert rep["n_total"] == 5


def test_bet_band_reliability_empty_is_insufficient_not_a_crash() -> None:
    rep = bet_band_reliability([])
    assert rep["n_total"] == 0
    assert rep["n_in_band"] == 0
    assert rep["band_lo_odds"] is None
    assert rep["insufficient"] is True


def test_bet_band_reliability_rejects_bad_band_quantiles() -> None:
    with pytest.raises(ValueError, match="band_lo_q"):
        bet_band_reliability(_bet([(0.5, True, 2.0)] * 60), band_lo_q=0.9, band_hi_q=0.1)
