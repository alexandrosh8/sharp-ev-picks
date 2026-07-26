"""scripts/sports/nfl_lines_provenance.py — importable + dry-runs on fixtures.

READ-ONLY measurement harness (nflverse consensus vs our Arcadia Pinnacle
close): no network, no DB — fixtures only. The script registers no source.
"""

import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sports" / "nfl_lines_provenance.py"
_spec = importlib.util.spec_from_file_location("nfl_lines_provenance", _SCRIPT)
assert _spec is not None and _spec.loader is not None
prov: Any = importlib.util.module_from_spec(_spec)
sys.modules["nfl_lines_provenance"] = prov  # dataclasses need the module registered
_spec.loader.exec_module(prov)


_GAMES_CSV = (
    "game_id,season,game_type,week,gameday,weekday,gametime,away_team,away_score,"
    "home_team,home_score,result,away_moneyline,home_moneyline,spread_line,total_line\n"
    # 20:15 EDT 2026-09-10 => 00:15 UTC 2026-09-11; home BUF -140 / away KC +120
    "2026_01_KC_BUF,2026,REG,1,2026-09-10,Thursday,20:15,KC,,BUF,,,120,-140,-2.5,48.5\n"
    # no moneylines -> skipped
    "2026_01_SF_SEA,2026,REG,1,2026-09-13,Sunday,16:05,SF,,SEA,,,,,2.5,44.0\n"
    # TBD kickoff -> skipped
    "2026_18_DAL_PHI,2026,REG,18,2027-01-03,Sunday,,DAL,,PHI,,,110,-130,-1.5,45.5\n"
)


def _closes_fixture() -> list[dict[str, object]]:
    # Two captures per side: the LAST pre-kickoff one must win (1.65/2.30).
    return [
        {
            "home": "Buffalo Bills",
            "away": "Kansas City Chiefs",
            "starts_at": "2026-09-11T00:15:00+00:00",
            "selection": "Buffalo Bills",
            "decimal_odds": 1.70,
            "captured_at": "2026-09-10T12:00:00+00:00",
        },
        {
            "home": "Buffalo Bills",
            "away": "Kansas City Chiefs",
            "starts_at": "2026-09-11T00:15:00+00:00",
            "selection": "Buffalo Bills",
            "decimal_odds": 1.65,
            "captured_at": "2026-09-11T00:00:00+00:00",
        },
        {
            "home": "Buffalo Bills",
            "away": "Kansas City Chiefs",
            "starts_at": "2026-09-11T00:15:00+00:00",
            "selection": "Kansas City Chiefs",
            "decimal_odds": 2.30,
            "captured_at": "2026-09-11T00:00:00+00:00",
        },
        # One-legged event (away side never captured) -> must be dropped.
        {
            "home": "Green Bay Packers",
            "away": "Chicago Bears",
            "starts_at": "2026-09-13T17:00:00+00:00",
            "selection": "Green Bay Packers",
            "decimal_odds": 1.50,
            "captured_at": "2026-09-13T16:00:00+00:00",
        },
    ]


@pytest.fixture()
def fixture_paths(tmp_path: Path) -> tuple[Path, Path]:
    games = tmp_path / "games.csv"
    games.write_text(_GAMES_CSV, encoding="utf-8")
    closes = tmp_path / "closes.json"
    closes.write_text(json.dumps(_closes_fixture()), encoding="utf-8")
    return games, closes


def test_devig_and_odds_helpers() -> None:
    assert prov.american_to_decimal(120) == pytest.approx(2.20)
    assert prov.american_to_decimal(-140) == pytest.approx(1.0 + 100 / 140)
    p = prov.devig_two_way(2.0, 2.0)
    assert p == pytest.approx(0.5)
    with pytest.raises(ValueError):
        prov.american_to_decimal(50)
    with pytest.raises(ValueError):
        prov.logit(1.0)


def test_dry_run_on_fixture_reports_median_abs_logit_diff(
    fixture_paths: tuple[Path, Path],
) -> None:
    games_path, closes_path = fixture_paths
    report = prov.run(games_path, closes_path)
    assert report["nflverse_games_with_moneylines"] == 1  # 2 rows skipped
    assert report["arcadia_close_events"] == 1  # one-legged event dropped
    assert report["paired_fixtures"] == 1
    # Hand-computed: nflverse devig(1.0+100/140, 2.20) vs close devig(1.65, 2.30)
    nfl_home = prov.devig_two_way(prov.american_to_decimal(-140), prov.american_to_decimal(120))
    close_home = prov.devig_two_way(1.65, 2.30)
    expected = abs(math.log(nfl_home / (1 - nfl_home)) - math.log(close_home / (1 - close_home)))
    assert report["median_abs_logit_diff"] == pytest.approx(expected)


def test_zero_pairable_fixtures_is_a_clean_report(tmp_path: Path) -> None:
    games = tmp_path / "games.csv"
    games.write_text(_GAMES_CSV, encoding="utf-8")
    closes = tmp_path / "closes.json"
    closes.write_text("null", encoding="utf-8")  # json_agg of empty set
    report = prov.run(games, closes)
    assert report["paired_fixtures"] == 0
    assert "median_abs_logit_diff" not in report


def test_tricode_map_covers_all_32_current_teams_and_is_word_safe() -> None:
    current = {v for k, v in prov.TRICODE_TO_NAME.items() if k not in {"OAK", "SD", "STL", "LA"}}
    assert len(current) == 32
    # The map stays LOCAL to the script — word-like tricodes must never enter
    # the global alias seed (collision surface, not a scrape surface form).
    from app.resolution.matching import AliasTable

    table = AliasTable.from_seed()
    assert table.canonical("NO") != table.canonical("New Orleans Saints")
    assert table.canonical("TEN") != table.canonical("Tennessee Titans")
