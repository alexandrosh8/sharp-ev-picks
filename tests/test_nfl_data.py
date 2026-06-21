"""NFL read-only data spine (nflreadpy / nflverse). Pure-parser tests — no
network, no nflreadpy dependency required (the loader is injected). SCREEN/
visibility data only: NFL picks stay un-staked until held-out CLV clears
(research 2026-06-21). Lines are nflverse CONSENSUS, never a sharp anchor.
"""

from datetime import UTC, datetime

from app.ingestion.nfl_data import NflGame, parse_nfl_games


def test_parse_nfl_games_maps_utc_scores_and_consensus_lines() -> None:
    rows = [
        {
            "game_id": "2026_01_KC_BUF",
            "season": 2026,
            "week": 1,
            "gameday": "2026-09-10",
            "gametime": "20:15",  # ET wall-clock (EDT in September -> UTC-4)
            "home_team": "BUF",
            "away_team": "KC",
            "home_score": 24,
            "away_score": 27,
            "spread_line": -2.5,  # home perspective (consensus)
            "total_line": 48.5,
            "home_moneyline": 120,
            "away_moneyline": -140,
        }
    ]
    games = parse_nfl_games(rows)
    assert len(games) == 1
    g = games[0]
    assert isinstance(g, NflGame)
    assert g.game_id == "2026_01_KC_BUF"
    # 20:15 EDT on 2026-09-10 == 00:15 UTC on 2026-09-11
    assert g.kickoff_utc == datetime(2026, 9, 11, 0, 15, tzinfo=UTC)
    assert g.home_team == "BUF" and g.away_team == "KC"
    assert g.home_score == 24 and g.away_score == 27
    assert g.spread_line == -2.5 and g.total_line == 48.5
    assert g.home_moneyline == 120 and g.away_moneyline == -140
    assert g.is_final is True


def test_parse_nfl_games_unplayed_has_no_score() -> None:
    rows = [
        {
            "game_id": "2026_05_NE_NYJ",
            "season": 2026,
            "week": 5,
            "gameday": "2026-10-05",
            "gametime": "13:00",
            "home_team": "NYJ",
            "away_team": "NE",
            "home_score": None,
            "away_score": None,
            "spread_line": 3.0,
            "total_line": 41.0,
            "home_moneyline": 145,
            "away_moneyline": -170,
        }
    ]
    g = parse_nfl_games(rows)[0]
    assert g.home_score is None and g.away_score is None
    assert g.is_final is False


def test_parse_nfl_games_skips_rows_without_kickoff() -> None:
    # nflverse rows for far-future / TBD games may lack gametime — skip them
    # rather than invent a kickoff (UTC discipline: no naive guesses).
    rows = [
        {
            "game_id": "tbd",
            "season": 2026,
            "week": 18,
            "gameday": "2027-01-03",
            "gametime": None,
            "home_team": "SEA",
            "away_team": "SF",
            "home_score": None,
            "away_score": None,
            "spread_line": None,
            "total_line": None,
            "home_moneyline": None,
            "away_moneyline": None,
        },
    ]
    assert parse_nfl_games(rows) == []
