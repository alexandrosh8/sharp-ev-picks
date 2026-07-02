"""ESPN scoreboard parsers + loader (free, read-only multi-sport results).

Pure-parser tests run on synthetic JSON matching the live ESPN site API shape
(captured 2026-06-20): team sports nest competitions[0].competitors[]; tennis
nests events[].groupings[].competitions[].competitors[] with per-set linescores.
No network — the fetch test uses httpx.MockTransport.
"""

from datetime import date

import httpx

from app.ingestion.espn_scores import (
    EspnSource,
    fetch_espn_scores,
    load_espn_scores,
    parse_team_scoreboard,
    parse_tennis_scoreboard,
)
from app.settlement.results import FinalScore

# --- synthetic fixtures (mirror the real ESPN shape) ----------------------

_NBA = {
    "events": [
        {
            "date": "2024-01-15T23:00Z",
            "competitions": [
                {
                    "status": {"type": {"name": "STATUS_FINAL", "completed": True}},
                    "competitors": [
                        {
                            "homeAway": "home",
                            "score": "124",
                            "winner": True,
                            "team": {"displayName": "Philadelphia 76ers"},
                        },
                        {
                            "homeAway": "away",
                            "score": "115",
                            "winner": False,
                            "team": {"displayName": "Houston Rockets"},
                        },
                    ],
                }
            ],
        },
        {
            "date": "2024-01-15T23:30Z",
            "competitions": [
                {
                    "status": {"type": {"name": "STATUS_IN_PROGRESS", "completed": False}},
                    "competitors": [
                        {"homeAway": "home", "score": "40", "team": {"displayName": "A"}},
                        {"homeAway": "away", "score": "38", "team": {"displayName": "B"}},
                    ],
                }
            ],
        },
    ]
}

_TENNIS = {
    "events": [
        {
            "date": "2024-01-08T05:00Z",
            "groupings": [
                {
                    "competitions": [
                        {
                            "date": "2024-01-08T06:00Z",
                            "status": {"type": {"name": "STATUS_FINAL", "completed": True}},
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "winner": True,
                                    "athlete": {"displayName": "Anastasia Zakharova"},
                                    "linescores": [{"value": 6.0}, {"value": 6.0}],
                                },
                                {
                                    "homeAway": "away",
                                    "winner": False,
                                    "athlete": {"displayName": "Jana Kolodynska"},
                                    "linescores": [{"value": 1.0}, {"value": 3.0}],
                                },
                            ],
                        },
                        {
                            "date": "2024-01-08T07:00Z",
                            "status": {"type": {"name": "STATUS_SCHEDULED", "completed": False}},
                            "competitors": [
                                {"homeAway": "home", "athlete": {"displayName": "X"}},
                                {"homeAway": "away", "athlete": {"displayName": "Y"}},
                            ],
                        },
                    ]
                }
            ],
        }
    ]
}


def test_parse_team_scoreboard_extracts_final_scores() -> None:
    scores = parse_team_scoreboard(_NBA)
    assert scores == [
        FinalScore("Philadelphia 76ers", "Houston Rockets", date(2024, 1, 15), 124, 115)
    ]  # the in-progress game is excluded


def test_parse_team_scoreboard_empty_when_no_events() -> None:
    assert parse_team_scoreboard({"events": []}) == []
    assert parse_team_scoreboard({}) == []


def test_parse_tennis_scoreboard_derives_set_score() -> None:
    # Winner took both sets (6-1, 6-3) -> set score 2-0; total sets 2.
    scores = parse_tennis_scoreboard(_TENNIS)
    assert scores == [
        FinalScore("Anastasia Zakharova", "Jana Kolodynska", date(2024, 1, 8), 2, 0)
    ]  # the scheduled match is excluded


def test_parse_tennis_scoreboard_three_set_match() -> None:
    data = {
        "events": [
            {
                "date": "2024-02-01T00:00Z",
                "groupings": [
                    {
                        "competitions": [
                            {
                                "date": "2024-02-01T12:00Z",
                                "status": {"type": {"name": "STATUS_FINAL", "completed": True}},
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "winner": False,
                                        "athlete": {"displayName": "Player A"},
                                        "linescores": [
                                            {"value": 6.0},
                                            {"value": 4.0},
                                            {"value": 3.0},
                                        ],
                                    },
                                    {
                                        "homeAway": "away",
                                        "winner": True,
                                        "athlete": {"displayName": "Player B"},
                                        "linescores": [
                                            {"value": 4.0},
                                            {"value": 6.0},
                                            {"value": 6.0},
                                        ],
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    # A won set 1; B won sets 2 and 3 -> 1-2; total sets 3.
    assert parse_tennis_scoreboard(data) == [
        FinalScore("Player A", "Player B", date(2024, 2, 1), 1, 2)
    ]


def _tennis_comp(
    home_lines: list[float],
    away_lines: list[float],
    status: dict | None = None,
    home: str = "Player A",
    away: str = "Player B",
) -> dict:
    return {
        "events": [
            {
                "date": "2024-02-01T00:00Z",
                "groupings": [
                    {
                        "competitions": [
                            {
                                "date": "2024-02-01T12:00Z",
                                "status": status
                                or {"type": {"name": "STATUS_FINAL", "completed": True}},
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "athlete": {"displayName": home},
                                        "linescores": [{"value": v} for v in home_lines],
                                    },
                                    {
                                        "homeAway": "away",
                                        "athlete": {"displayName": away},
                                        "linescores": [{"value": v} for v in away_lines],
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_parse_tennis_skips_retired_and_walkover_matches() -> None:
    # A retirement/walkover is NOT a normal completed match: the set score is
    # meaningless for over_under_sets and often for match_winner too. ESPN
    # still flags these completed=True, so the detail text must gate them out
    # (the pick stays pending and ages into the existing void path).
    retired = {"type": {"name": "STATUS_RETIRED", "completed": True, "detail": "Ret."}}
    assert parse_tennis_scoreboard(_tennis_comp([6.0, 3.0], [4.0, 1.0], status=retired)) == []
    walkover = {"type": {"name": "STATUS_FINAL", "completed": True, "shortDetail": "W/O"}}
    assert parse_tennis_scoreboard(_tennis_comp([], [], status=walkover)) == []
    defaulted = {"type": {"name": "STATUS_FINAL", "completed": True, "detail": "Defaulted"}}
    assert parse_tennis_scoreboard(_tennis_comp([6.0, 2.0], [2.0, 0.0], status=defaulted)) == []


def test_parse_tennis_does_not_count_leading_partial_set() -> None:
    # Mid-set 3-1 lead when play stopped: NOT a won set, and 1 complete set is
    # not a complete best-of-3 -> emit nothing (never settle a partial match).
    assert parse_tennis_scoreboard(_tennis_comp([6.0, 3.0], [4.0, 1.0])) == []


def test_parse_tennis_requires_complete_best_of_pattern() -> None:
    # One completed set only (6-4) with completed=True -> no FinalScore.
    assert parse_tennis_scoreboard(_tennis_comp([6.0], [4.0])) == []


def test_parse_tennis_settles_normal_and_tiebreak_finals() -> None:
    assert parse_tennis_scoreboard(_tennis_comp([6.0, 6.0], [4.0, 4.0])) == [
        FinalScore("Player A", "Player B", date(2024, 2, 1), 2, 0)
    ]
    assert parse_tennis_scoreboard(_tennis_comp([7.0, 7.0], [6.0, 6.0])) == [
        FinalScore("Player A", "Player B", date(2024, 2, 1), 2, 0)
    ]
    # third-set match tiebreak to 10 counts as a won set
    assert parse_tennis_scoreboard(_tennis_comp([4.0, 6.0, 10.0], [6.0, 4.0, 8.0])) == [
        FinalScore("Player A", "Player B", date(2024, 2, 1), 2, 1)
    ]


async def test_fetch_espn_scores_uses_dated_endpoint_and_parses() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_NBA)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        scores = await fetch_espn_scores(
            client, EspnSource(sport="basketball", league="nba"), [date(2024, 1, 15)]
        )
    assert scores == [
        FinalScore("Philadelphia 76ers", "Houston Rockets", date(2024, 1, 15), 124, 115)
    ]
    assert seen == [
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20240115"
    ]


async def test_load_espn_scores_queries_each_configured_sports_sources() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if "basketball/nba" in request.url.path:
            return httpx.Response(200, json=_NBA)
        return httpx.Response(200, json={"events": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        scores = await load_espn_scores(client, ["basketball"], [date(2024, 1, 15)])
    assert (
        FinalScore("Philadelphia 76ers", "Houston Rockets", date(2024, 1, 15), 124, 115) in scores
    )
    assert any("basketball/nba" in p for p in seen)
    # an unknown sport key contributes nothing and makes no request
    seen.clear()
    async with httpx.AsyncClient(transport=transport) as client:
        assert await load_espn_scores(client, ["curling"], [date(2024, 1, 15)]) == []
    assert seen == []
