"""OddsPortal adapter: OddsHarvester match dicts -> snapshots + directory.

Uses an injected fake scrape_fn — no oddsharvester import, no network.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal import OddsPortalLoader
from app.schemas.base import Market

MATCH = {
    "home_team": "Alpha FC",
    "away_team": "Beta United",
    "match_date": "2026-06-11",
    "league_name": "Testland League",
    "match_link": "https://www.oddsportal.com/football/testland/alpha-beta/",
    "scraped_date": "2026-06-10T12:00:00Z",
    "1x2_market": [
        {
            "1": "2.10",
            "X": "3.40",
            "2": "3.60",
            "bookmaker_name": "BookieOne",
            "period": "FullTime",
        },
        {
            "1": "2.05",
            "X": "3.45",
            "2": "3.70",
            "bookmaker_name": "BookieTwo",
            "period": "FullTime",
        },
    ],
    "over_under_2_5_market": [
        {
            "odds_over": "1.95",
            "odds_under": "1.95",
            "bookmaker_name": "BookieOne",
            "period": "FullTime",
        },
    ],
}


def make_loader(directory: EventDirectory, matches: list[dict[str, Any]]) -> OddsPortalLoader:
    async def fake_scrape(**kwargs: Any) -> Any:
        fake_scrape.calls.append(kwargs)  # type: ignore[attr-defined]
        return SimpleNamespace(success=matches, failed=[], partial=[])

    fake_scrape.calls = []  # type: ignore[attr-defined]
    return OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key={"soccer": ("football", ["testland-league"])},
        scrape_fn=fake_scrape,
    )


async def test_match_converts_to_snapshots_and_registers_teams() -> None:
    directory = EventDirectory()
    loader = make_loader(directory, [MATCH])
    snapshots = await loader.fetch_odds("soccer")

    # 2 bookmakers x 3 1x2 selections + 1 bookmaker x 2 totals = 8
    assert len(snapshots) == 8
    assert {s.market for s in snapshots} == {Market.H2H, Market.TOTALS}
    h2h_selections = {s.selection for s in snapshots if s.market is Market.H2H}
    assert h2h_selections == {"Alpha FC", "Draw", "Beta United"}
    totals = [s for s in snapshots if s.market is Market.TOTALS]
    assert {s.selection for s in totals} == {"Over 2.5", "Under 2.5"}
    assert all(s.decimal_odds > 1.0 for s in snapshots)

    teams = directory.lookup(str(MATCH["match_link"]))
    assert teams is not None
    assert teams.home == "Alpha FC"
    assert teams.away == "Beta United"


async def test_unknown_sport_key_returns_empty() -> None:
    loader = make_loader(EventDirectory(), [MATCH])
    assert await loader.fetch_odds("basketball_nba") == []


async def test_malformed_entries_are_skipped() -> None:
    bad = dict(MATCH)
    bad["1x2_market"] = [
        {"1": "not-a-number", "X": "", "2": None, "bookmaker_name": "BadBook"},
        "garbage-entry",
    ]
    bad["over_under_2_5_market"] = []
    loader = make_loader(EventDirectory(), [bad])
    snapshots = await loader.fetch_odds("soccer")
    assert snapshots == []


async def test_match_without_teams_is_skipped() -> None:
    loader = make_loader(EventDirectory(), [{"home_team": "", "away_team": "X"}])
    assert await loader.fetch_odds("soccer") == []


def test_unsupported_market_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        OddsPortalLoader(
            directory=EventDirectory(),
            leagues_by_sport_key={},
            markets=("1x2", "correct_score_9_9"),
        )
