"""Property tests for the NFL spread-conditioned margin PMF (SHADOW-only).

Pure math — no network, no DB. The frozen table is committed data
(app/probabilities/nfl_margin_table.json, fit by scripts/sports/nfl_margin_fit.py
from FREE nflverse history); a synthetic table exercises the math independently
of the committed fit.
"""

import json
import math
from pathlib import Path

import pytest

from app.probabilities.nfl_margin import NflMarginModel, load_default_model

_TABLE_PATH = Path("app/probabilities/nfl_margin_table.json")


@pytest.fixture(scope="module")
def model() -> NflMarginModel:
    return load_default_model()


def _synthetic_model() -> NflMarginModel:
    # Flat weights except a hand-planted key spike at +/-3.
    weights = {m: 1.0 for m in range(-30, 31)}
    weights[3] = 2.5
    weights[-3] = 2.5
    return NflMarginModel(sigma=13.0, weights=weights, support_min=-30, support_max=30)


# --- frozen table -----------------------------------------------------------


def test_frozen_table_is_valid_committed_data() -> None:
    table = json.loads(_TABLE_PATH.read_text(encoding="utf-8"))
    assert table["n_games"] >= 1000
    assert 10.0 < table["sigma"] < 17.0  # NFL scoring-era sanity band
    support = range(int(table["support_min"]), int(table["support_max"]) + 1)
    assert set(table["weights"]) == {str(m) for m in support}
    assert all(v > 0.0 for v in table["weights"].values())


def test_frozen_table_carries_the_key_numbers(model: NflMarginModel) -> None:
    # The empirical NFL facts the mixture exists for: 3 and 7 are spikes
    # relative to their non-key neighbors; ties (margin 0) are near-extinct.
    for sign in (1, -1):
        assert model.weights[3 * sign] > 1.5
        assert model.weights[3 * sign] > model.weights[2 * sign]
        assert model.weights[3 * sign] > model.weights[4 * sign]
        assert model.weights[7 * sign] > model.weights[8 * sign]
    assert model.weights[0] < 0.5


# --- PMF properties ---------------------------------------------------------


@pytest.mark.parametrize("mu", [-21.0, -7.0, -2.5, 0.0, 0.09, 3.0, 13.5, 27.0])
def test_pmf_sums_to_one_and_is_nonnegative(model: NflMarginModel, mu: float) -> None:
    pmf = model.margin_pmf(mu)
    assert abs(math.fsum(pmf.values()) - 1.0) < 1e-9
    assert all(p >= 0.0 for p in pmf.values())
    assert set(pmf) == set(range(model.support_min, model.support_max + 1))


def test_pmf_mass_tracks_mu(model: NflMarginModel) -> None:
    # A home-favored mu puts more mass on home-positive margins, and vice versa.
    fav = model.margin_pmf(7.0)
    dog = model.margin_pmf(-7.0)
    assert math.fsum(p for m, p in fav.items() if m > 0) > 0.5
    assert math.fsum(p for m, p in dog.items() if m > 0) < 0.5


def test_pmf_key_spike_at_three(model: NflMarginModel) -> None:
    # With mu near the key number the spike must survive discretization.
    pmf = model.margin_pmf(2.5)
    assert pmf[3] > pmf[2]
    assert pmf[3] > pmf[4]


def test_degenerate_mu_outside_support_raises() -> None:
    m = _synthetic_model()
    with pytest.raises(ValueError, match="probability mass"):
        m.margin_pmf(500.0)


# --- cover prob / line monotonicity ----------------------------------------


def test_cover_prob_is_monotonic_in_line(model: NflMarginModel) -> None:
    # More points handed to the home side => cover prob strictly rises.
    for mu in (-6.5, 0.0, 4.5):
        ladder = [x + 0.5 for x in range(-15, 15)]
        probs = [model.home_cover_prob(mu, line) for line in ladder]
        assert all(b > a for a, b in zip(probs, probs[1:], strict=False))
        assert all(0.0 < p < 1.0 for p in probs)


@pytest.mark.parametrize("bad_line", [3.0, 0.0, -7.0, 2.25, -1.75])
def test_non_half_lines_are_rejected(model: NflMarginModel, bad_line: float) -> None:
    with pytest.raises(ValueError, match="half-line"):
        model.home_cover_prob(0.0, bad_line)
    with pytest.raises(ValueError, match="half-line"):
        model.implied_mu(bad_line, 0.5)


# --- anchor calibration (fair-prob-at-line) ---------------------------------


def test_implied_mu_round_trips(model: NflMarginModel) -> None:
    for mu, line in [(2.3, -2.5), (-8.0, 6.5), (0.0, 0.5), (12.75, -13.5)]:
        p = model.home_cover_prob(mu, line)
        assert model.implied_mu(line, p) == pytest.approx(mu, abs=1e-4)


def test_fair_prob_at_anchor_line_reproduces_the_anchor(model: NflMarginModel) -> None:
    assert model.fair_prob_at_line(-2.5, 0.55, -2.5) == pytest.approx(0.55, abs=1e-8)


def test_fair_prob_at_line_monotonic_in_target(model: NflMarginModel) -> None:
    targets = [x + 0.5 for x in range(-10, 10)]
    fair = [model.fair_prob_at_line(-3.5, 0.48, t) for t in targets]
    assert all(b > a for a, b in zip(fair, fair[1:], strict=False))


def test_fair_prob_crossing_the_key_number_moves_more(model: NflMarginModel) -> None:
    # Buying from -3.5 to -2.5 crosses the 3 spike: worth more than -4.5 to -3.5.
    p = 0.50
    across_key = model.fair_prob_at_line(-3.5, p, -2.5) - p
    off_key = p - model.fair_prob_at_line(-3.5, p, -4.5)
    assert across_key > off_key


def test_implied_mu_rejects_degenerate_probs(model: NflMarginModel) -> None:
    for bad in (0.0, 1.0, -0.1, 1.7):
        with pytest.raises(ValueError):
            model.implied_mu(-2.5, bad)


def test_constructor_rejects_bad_tables() -> None:
    with pytest.raises(ValueError, match="sigma"):
        NflMarginModel(sigma=0.0, weights={0: 1.0}, support_min=-1, support_max=1)
    with pytest.raises(ValueError, match="weight"):
        NflMarginModel(sigma=13.0, weights={0: -1.0}, support_min=-1, support_max=1)
    with pytest.raises(ValueError, match="support"):
        NflMarginModel(sigma=13.0, weights={}, support_min=5, support_max=5)
