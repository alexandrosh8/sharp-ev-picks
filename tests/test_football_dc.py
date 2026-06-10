"""Dixon-Coles adapter over penaltyblog: fit, resolve, predict invariants.

Skips when the `football` extra is not installed (CI default profile).
Synthetic fixture: 4 teams, repeated double round-robins where Alpha is
clearly strongest at home.
"""

from datetime import date, timedelta

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
