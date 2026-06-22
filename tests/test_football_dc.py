"""Dixon-Coles adapter over penaltyblog: fit, resolve, predict invariants.

Skips when the `football` extra is not installed (CI default profile).
Synthetic fixture: 4 teams, repeated double round-robins where Alpha is
clearly strongest at home.
"""

import math
from datetime import date, timedelta
from importlib import metadata

import numpy as np
import pytest

pytest.importorskip("penaltyblog")

from app.ingestion.base import EventDirectory, EventTeams  # noqa: E402
from app.ingestion.football_data import MatchRow  # noqa: E402
from app.models.football_dc import DixonColesFootballModel, _normalize  # noqa: E402
from app.schemas.base import Market  # noqa: E402

TEAMS = ["Alpha", "Beta FC", "Gamma City", "Delta Town"]
AS_OF = date(2026, 6, 1)

# Two alternating score tables: Alpha strongest overall, but with enough
# variation (draws, upsets, scorelines) that the MLE stays finite.
SCORES_A = {
    ("Alpha", "Beta FC"): (3, 1),
    ("Alpha", "Gamma City"): (2, 0),
    ("Alpha", "Delta Town"): (3, 1),
    ("Beta FC", "Alpha"): (1, 2),
    ("Beta FC", "Gamma City"): (1, 1),
    ("Beta FC", "Delta Town"): (2, 0),
    ("Gamma City", "Alpha"): (0, 2),
    ("Gamma City", "Beta FC"): (2, 1),
    ("Gamma City", "Delta Town"): (2, 1),
    ("Delta Town", "Alpha"): (0, 2),
    ("Delta Town", "Beta FC"): (1, 1),
    ("Delta Town", "Gamma City"): (1, 2),
}
SCORES_B = {
    ("Alpha", "Beta FC"): (1, 1),
    ("Alpha", "Gamma City"): (2, 1),
    ("Alpha", "Delta Town"): (2, 0),
    ("Beta FC", "Alpha"): (0, 1),
    ("Beta FC", "Gamma City"): (2, 0),
    ("Beta FC", "Delta Town"): (1, 1),
    ("Gamma City", "Alpha"): (1, 1),
    ("Gamma City", "Beta FC"): (0, 1),
    ("Gamma City", "Delta Town"): (3, 1),
    ("Delta Town", "Alpha"): (1, 2),
    ("Delta Town", "Beta FC"): (0, 2),
    ("Delta Town", "Gamma City"): (2, 2),
}
# C adds 0-0 and 1-0 results so the rho low-score correction stays finite.
SCORES_C = {
    ("Alpha", "Beta FC"): (2, 0),
    ("Alpha", "Gamma City"): (1, 0),
    ("Alpha", "Delta Town"): (4, 1),
    ("Beta FC", "Alpha"): (1, 1),
    ("Beta FC", "Gamma City"): (0, 0),
    ("Beta FC", "Delta Town"): (3, 1),
    ("Gamma City", "Alpha"): (1, 2),
    ("Gamma City", "Beta FC"): (1, 0),
    ("Gamma City", "Delta Town"): (0, 0),
    ("Delta Town", "Alpha"): (1, 3),
    ("Delta Town", "Beta FC"): (1, 2),
    ("Delta Town", "Gamma City"): (0, 1),
}


def synthetic_history(rounds: int = 6) -> list[MatchRow]:
    rows: list[MatchRow] = []
    day = AS_OF - timedelta(days=rounds * len(SCORES_A) + 10)
    tables = (SCORES_A, SCORES_B, SCORES_C)
    for r in range(rounds):
        table = tables[r % 3]
        for (home, away), (hg, ag) in table.items():
            rows.append(
                MatchRow(
                    match_date=day,
                    home_team=home,
                    away_team=away,
                    home_goals=hg,
                    away_goals=ag,
                    result="H" if hg > ag else ("A" if ag > hg else "D"),
                    b365_home=None,
                    b365_draw=None,
                    b365_away=None,
                    pinnacle_closing_home=None,
                    pinnacle_closing_draw=None,
                    pinnacle_closing_away=None,
                )
            )
            day += timedelta(days=1)
    return rows


@pytest.fixture(scope="module")
def fitted() -> tuple[DixonColesFootballModel, EventDirectory]:
    directory = EventDirectory()
    model = DixonColesFootballModel(directory, confidence=0.7)
    model.fit(synthetic_history(), AS_OF)
    return model, directory


def test_fit_excludes_same_day_results_no_leak() -> None:
    # audit #8: the live refit must NOT train on a same-day (== as_of) result
    # before pricing later-same-day fixtures. A row dated == AS_OF is excluded,
    # so 49 historical + 1 same-day leaves only 49 usable -> below the >=50 floor.
    hist49 = [r for r in synthetic_history() if r.match_date < AS_OF][:49]
    assert len(hist49) == 49
    same_day = MatchRow(
        match_date=AS_OF,
        home_team="Alpha",
        away_team="Beta FC",
        home_goals=1,
        away_goals=0,
        result="H",
        b365_home=None,
        b365_draw=None,
        b365_away=None,
        pinnacle_closing_home=None,
        pinnacle_closing_draw=None,
        pinnacle_closing_away=None,
    )
    model = DixonColesFootballModel(EventDirectory(), confidence=0.7)
    with pytest.raises(ValueError, match="got 49"):
        model.fit([*hist49, same_day], AS_OF)


async def test_predictions_are_coherent_probabilities(
    fitted: tuple[DixonColesFootballModel, EventDirectory],
) -> None:
    model, directory = fitted
    # A mid-table matchup: the score grid stays valid and the markets are
    # genuinely contested (the coherence properties are what we assert).
    directory.register("evt-1", EventTeams(home="Alpha", away="Beta FC"))
    preds = await model.predict("evt-1")
    by_key = {(p.market, p.selection): p.probability for p in preds}

    h2h_sum = (
        by_key[(Market.H2H, "Alpha")]
        + by_key[(Market.H2H, "Draw")]
        + by_key[(Market.H2H, "Beta FC")]
    )
    assert h2h_sum == pytest.approx(1.0, abs=1e-6)
    totals_sum = by_key[(Market.TOTALS, "Over 2.5")] + by_key[(Market.TOTALS, "Under 2.5")]
    assert totals_sum == pytest.approx(1.0, abs=1e-6)
    btts_sum = by_key[(Market.BTTS, "BTTS Yes")] + by_key[(Market.BTTS, "BTTS No")]
    assert btts_sum == pytest.approx(1.0, abs=1e-6)
    # the stronger home side is the favourite over the away side
    assert by_key[(Market.H2H, "Alpha")] > by_key[(Market.H2H, "Beta FC")]
    assert all(0.0 <= p <= 1.0 for p in by_key.values())


async def test_unknown_team_yields_no_predictions(
    fitted: tuple[DixonColesFootballModel, EventDirectory],
) -> None:
    model, directory = fitted
    directory.register("evt-2", EventTeams(home="Alpha", away="Omega Unknowns"))
    assert await model.predict("evt-2") == ()


async def test_unregistered_event_yields_no_predictions(
    fitted: tuple[DixonColesFootballModel, EventDirectory],
) -> None:
    model, _ = fitted
    assert await model.predict("missing-event") == ()


def test_alias_and_containment_resolution(
    fitted: tuple[DixonColesFootballModel, EventDirectory],
) -> None:
    model, _ = fitted
    # exact (case/punctuation-insensitive)
    assert model.resolve_team("ALPHA") == "Alpha"
    # containment: oddsportal-style longer name contains trained tokens
    assert model.resolve_team("Gamma City FC") == "Gamma City"
    # custom alias path
    custom = DixonColesFootballModel(
        EventDirectory(), aliases={_normalize("The Betas"): _normalize("Beta FC")}
    )
    custom._trained = dict(model._trained)
    custom._model = model._model
    assert custom.resolve_team("The Betas") == "Beta FC"
    # honest miss
    assert model.resolve_team("Real Madrid") is None


def test_fit_requires_minimum_history() -> None:
    model = DixonColesFootballModel(EventDirectory())
    with pytest.raises(ValueError):
        model.fit(synthetic_history()[:10], AS_OF)


# --- honest version label -----------------------------------------------


def test_version_label_tracks_installed_penaltyblog() -> None:
    """Registry rows must stay honest across penaltyblog bumps — the label
    derives from the installed distribution, never a hardcoded literal."""
    assert DixonColesFootballModel.version == f"pb-{metadata.version('penaltyblog')}"


# --- totals push-mass guard ----------------------------------------------


@pytest.mark.parametrize("line", [3.0, 2.25])
def test_totals_line_rejects_integer_and_quarter_lines(line: float) -> None:
    """grid.total_goals() silently drops push mass: over+under < 1 on
    integer/quarter lines, corrupting EV. Only half lines are safe."""
    with pytest.raises(ValueError, match="half line"):
        DixonColesFootballModel(EventDirectory(), totals_line=line)


@pytest.mark.parametrize("line", [2.5, 3.5])
def test_totals_line_accepts_half_lines(line: float) -> None:
    model = DixonColesFootballModel(EventDirectory(), totals_line=line)
    assert model._totals_line == line


# --- decay-weights helper equivalence -------------------------------------


def test_dixon_coles_weights_match_manual_exponential_decay() -> None:
    """fit() now uses penaltyblog's documented dixon_coles_weights helper;
    it must equal the manual math.exp(-xi*days) computation it replaced."""
    from penaltyblog.models import dixon_coles_weights

    xi = 0.0018
    dates = [AS_OF - timedelta(days=d) for d in (0, 1, 7, 30, 180, 365)]
    manual = [math.exp(-xi * (AS_OF - d).days) for d in dates]
    helper = dixon_coles_weights(dates, xi, base_date=AS_OF)
    assert np.allclose(helper, manual, rtol=1e-15, atol=0.0)


# --- fit robustness: numerical-gradient retry ------------------------------


def _stub_pb_fit(monkeypatch: pytest.MonkeyPatch, fail_times: int) -> list[dict[str, object]]:
    """Replace penaltyblog's DixonColesGoalModel with a stub whose fit()
    fails `fail_times` times, recording every call's options/gradient flag."""
    import penaltyblog.models as pb_models

    calls: list[dict[str, object]] = []

    class StubModel:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def fit(self, minimizer_options: dict | None = None, use_gradient: bool = True) -> None:
            calls.append({"minimizer_options": minimizer_options, "use_gradient": use_gradient})
            if len(calls) <= fail_times:
                raise ValueError("Optimization failed with message: stub failure")

    monkeypatch.setattr(pb_models, "DixonColesGoalModel", StubModel)
    return calls


def test_fit_retries_once_with_numerical_gradient(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_pb_fit(monkeypatch, fail_times=1)
    model = DixonColesFootballModel(EventDirectory())
    model.fit(synthetic_history(), AS_OF)  # must NOT raise: retry rescues it
    assert model.fitted
    assert len(calls) == 2
    assert calls[0]["use_gradient"] is True
    assert calls[1]["use_gradient"] is False
    # docs-example tolerances on BOTH attempts
    expected = {"maxiter": 3000, "gtol": 1e-8, "ftol": 1e-9}
    assert calls[0]["minimizer_options"] == expected
    assert calls[1]["minimizer_options"] == expected


def test_fit_failing_twice_preserves_no_model_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pb_fit(monkeypatch, fail_times=2)
    model = DixonColesFootballModel(EventDirectory())
    with pytest.raises(ValueError, match="Optimization failed"):
        model.fit(synthetic_history(), AS_OF)
    assert not model.fitted  # unchanged failure mode: no model this cycle
    assert len(calls) == 2  # exactly one retry, never more


# --- batch prediction (predict_many fast-path) -----------------------------


def test_predict_matches_matches_per_fixture_output(
    fitted: tuple[DixonColesFootballModel, EventDirectory],
) -> None:
    """Batch results are identical to predict_match per fixture, and an
    unresolvable team skips ONLY its own fixture (empty tuple)."""
    model, _ = fitted
    fixtures = [
        ("Alpha", "Beta FC"),
        ("Gamma City", "Omega Unknowns"),  # unresolvable away side
        ("Delta Town", "Gamma City"),
    ]
    batch = model.predict_matches(fixtures)
    assert len(batch) == 3
    assert batch[0] == model.predict_match("Alpha", "Beta FC")
    assert batch[1] == ()
    assert batch[2] == model.predict_match("Delta Town", "Gamma City")


def test_predict_matches_respects_per_fixture_neutral_flags(
    fitted: tuple[DixonColesFootballModel, EventDirectory],
) -> None:
    model, _ = fitted
    (home_adv,) = model.predict_matches([("Alpha", "Beta FC")], neutral=[False])
    (neutral,) = model.predict_matches([("Alpha", "Beta FC")], neutral=[True])
    assert neutral == model.predict_match("Alpha", "Beta FC", neutral=True)
    by_key = lambda preds: {(p.market, p.selection): p.probability for p in preds}  # noqa: E731
    # removing home advantage must lower the home side's win probability
    assert by_key(neutral)[(Market.H2H, "Alpha")] < by_key(home_adv)[(Market.H2H, "Alpha")]


def test_predict_matches_falls_back_per_fixture_when_batch_fails(
    fitted: tuple[DixonColesFootballModel, EventDirectory],
) -> None:
    """predict_many is all-or-nothing: one invalid grid raises for the whole
    batch. The fallback reprices per fixture so a single bad matchup yields
    () while the rest of the slate survives — current behavior, preserved."""
    model, _ = fitted

    class FlakyBatch:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def predict_many(self, *args: object, **kwargs: object) -> None:
            raise ValueError("goal_matrix contains negative probabilities")

        def predict(self, home: str, away: str, neutral_venue: bool = False) -> object:
            if home == "Gamma City":
                raise ValueError("goal_matrix contains negative probabilities")
            return self._inner.predict(home, away, neutral_venue=neutral_venue)  # type: ignore[attr-defined]

    shadow = DixonColesFootballModel(EventDirectory(), confidence=0.7)  # match fixture
    shadow._trained = dict(model._trained)
    shadow._model = FlakyBatch(model._model)
    batch = shadow.predict_matches([("Alpha", "Beta FC"), ("Gamma City", "Delta Town")])
    assert batch[0] == model.predict_match("Alpha", "Beta FC")
    assert batch[1] == ()  # the bad matchup skips itself, not the slate


def test_predict_matches_requires_parallel_neutral(
    fitted: tuple[DixonColesFootballModel, EventDirectory],
) -> None:
    model, _ = fitted
    with pytest.raises(ValueError, match="parallel"):
        model.predict_matches([("Alpha", "Beta FC")], neutral=[True, False])


def test_predict_matches_unfitted_returns_empty_per_fixture() -> None:
    model = DixonColesFootballModel(EventDirectory())
    assert model.predict_matches([("Alpha", "Beta FC"), ("X", "Y")]) == ((), ())
