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


def _soccer_final(status_type: dict) -> dict:
    return {
        "events": [
            {
                "date": "2026-07-10T18:00Z",
                "competitions": [
                    {
                        "status": {"type": {"completed": True, **status_type}},
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "2",
                                "team": {"displayName": "Shamrock Rovers"},
                            },
                            {
                                "homeAway": "away",
                                "score": "1",
                                "team": {"displayName": "Vikingur Reykjavik"},
                            },
                        ],
                    }
                ],
            }
        ]
    }


def test_soccer_ninety_minute_only_skips_extra_time_and_shootout_finals() -> None:
    """1X2/totals settle on the 90-minute result. An ESPN soccer 'score' is
    ET-inclusive, so an AET/pens final must NOT be graded from it — the pick
    stays open for another source or manual entry (same doctrine as the
    martj42 ninety_minute_only gate and the OddsPortal ET/pens marker veto)."""
    for status_type in (
        {"name": "STATUS_FINAL_AET", "detail": "FT-ET"},
        {"name": "STATUS_FINAL_PEN", "detail": "FT-Pens"},
        {"name": "STATUS_FULL_TIME", "detail": "Full Time (AET)"},
        {"name": "STATUS_ABANDONED", "detail": "Abandoned"},
    ):
        assert parse_team_scoreboard(_soccer_final(status_type), ninety_minute_only=True) == []


def test_soccer_ninety_minute_only_keeps_normal_full_time() -> None:
    scores = parse_team_scoreboard(
        _soccer_final({"name": "STATUS_FULL_TIME", "detail": "FT"}), ninety_minute_only=True
    )
    assert scores == [FinalScore("Shamrock Rovers", "Vikingur Reykjavik", date(2026, 7, 10), 2, 1)]
    # generic STATUS_FINAL without ET/pens markers also passes (some feeds use it)
    assert parse_team_scoreboard(
        _soccer_final({"name": "STATUS_FINAL", "detail": "FT"}), ninety_minute_only=True
    ) == [FinalScore("Shamrock Rovers", "Vikingur Reykjavik", date(2026, 7, 10), 2, 1)]


def test_team_scoreboard_default_keeps_overtime_finals() -> None:
    """Basketball/NFL markets INCLUDE overtime — the guard must be soccer-only."""
    ot = _soccer_final({"name": "STATUS_FINAL", "detail": "Final/OT"})
    assert parse_team_scoreboard(ot) == [
        FinalScore("Shamrock Rovers", "Vikingur Reykjavik", date(2026, 7, 10), 2, 1)
    ]


async def test_fetch_espn_scores_applies_ninety_minute_gate_to_soccer() -> None:
    aet = _soccer_final({"name": "STATUS_FINAL_AET", "detail": "FT-ET"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=aet)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        soccer = await fetch_espn_scores(
            client, EspnSource(sport="soccer", league="uefa.champions_qual"), [date(2026, 7, 10)]
        )
        basketball = await fetch_espn_scores(
            client, EspnSource(sport="basketball", league="nba"), [date(2026, 7, 10)]
        )
    assert soccer == []  # AET final withheld from 90-minute settlement
    assert len(basketball) == 1  # non-soccer team sports keep OT-inclusive finals


def test_wnba_source_tags_women_marker_so_w_suffixed_picks_settle() -> None:
    """OddsChecker labels WNBA teams '<team> W' (women); ESPN's WNBA feed —
    already a women's-only league — returns bare names, so the settlement
    marker veto rejected every WNBA match. WNBA has no men's counterpart, so
    the ESPN side is tagged with the women marker to agree. Regression for 23
    WNBA picks stuck open despite an exact ESPN score."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from app.settlement.results import ScoreBook

    data = {
        "events": [
            {
                "date": "2026-07-04T23:00Z",
                "competitions": [
                    {
                        "status": {"type": {"name": "STATUS_FINAL", "completed": True}},
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "98",
                                "team": {"displayName": "Las Vegas Aces"},
                            },
                            {
                                "homeAway": "away",
                                "score": "90",
                                "team": {"displayName": "Chicago Sky"},
                            },
                        ],
                    }
                ],
            }
        ]
    }
    scores = parse_team_scoreboard(data, women_marker=True)
    assert scores[0].home_team == "Las Vegas Aces W"
    assert scores[0].away_team == "Chicago Sky W"
    book = ScoreBook(scores)
    assert (
        book.lookup("Las Vegas Aces W", "Chicago Sky W", _dt(2026, 7, 4, 23, 0, tzinfo=_UTC))
        is not None
    )
    # WNBA source is registered women=True so fetch tags it.
    from app.ingestion.espn_scores import SPORT_ESPN_SOURCES

    wnba = [s for s in SPORT_ESPN_SOURCES["basketball"] if s.league == "wnba"]
    assert wnba and wnba[0].women is True
    assert all(
        not getattr(s, "women", False)
        for s in SPORT_ESPN_SOURCES["basketball"]
        if s.league != "wnba"
    )


def test_soccer_espn_sources_cover_qualifiers() -> None:
    """Soccer must be registered with the UEFA-qualifier + World Cup + secondary
    slugs — the leagues football-data does NOT cover, which dominate the settlement
    backlog. A regression here silently reopens the 15-day-void gap for obscure
    soccer (2026-07-14). All soccer feeds are team-kind (not tennis)."""
    from app.ingestion.espn_scores import SPORT_ESPN_SOURCES

    soccer = SPORT_ESPN_SOURCES.get("soccer", ())
    leagues = {s.league for s in soccer}
    assert {
        "uefa.champions_qual",
        "uefa.europa_qual",
        "uefa.europa.conf_qual",
        "fifa.world",
        "bra.2",
    } <= leagues
    assert all(s.sport == "soccer" and s.kind == "team" for s in soccer)


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
    winner: str | None = None,  # "home" | "away" | None (no winner flag at all)
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
                                        **({"winner": winner == "home"} if winner else {}),
                                        "athlete": {"displayName": home},
                                        "linescores": [{"value": v} for v in home_lines],
                                    },
                                    {
                                        "homeAway": "away",
                                        **({"winner": winner == "away"} if winner else {}),
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


_RETIRED = {"type": {"name": "STATUS_RETIRED", "completed": True, "detail": "Ret."}}
_WALKOVER = {"type": {"name": "STATUS_FINAL", "completed": True, "shortDetail": "W/O"}}
_DEFAULTED = {"type": {"name": "STATUS_FINAL", "completed": True, "detail": "Defaulted"}}


def test_parse_tennis_retirement_after_one_set_grades_advancing_player() -> None:
    # TENNIS_SETTLEMENT_CONVENTION "pinnacle_one_set": retirement after >=1
    # completed set emits completion="retired" + the ESPN winner flag's side —
    # the settler grades h2h to the advancing player and voids other markets.
    got = parse_tennis_scoreboard(
        _tennis_comp([6.0, 3.0], [4.0, 1.0], status=_RETIRED, winner="home")
    )
    assert got == [
        FinalScore(
            "Player A", "Player B", date(2024, 2, 1), 1, 0, completion="retired", winner_side="home"
        )
    ]
    # Default (disqualification) after a completed set follows the same rule.
    got = parse_tennis_scoreboard(
        _tennis_comp([2.0, 6.0], [6.0, 3.0], status=_DEFAULTED, winner="away")
    )
    assert got == [
        FinalScore(
            "Player A", "Player B", date(2024, 2, 1), 1, 1, completion="retired", winner_side="away"
        )
    ]


def test_parse_tennis_walkover_and_early_abandonment_void() -> None:
    # Walkover (no play) and retirement BEFORE one completed set -> VOID all
    # markets. A walkover must NEVER grade as a win for anyone.
    assert parse_tennis_scoreboard(_tennis_comp([], [], status=_WALKOVER, winner="home")) == [
        FinalScore("Player A", "Player B", date(2024, 2, 1), 0, 0, completion="void")
    ]
    # Mid-first-set retirement: 3-1 games is not a completed set -> void too.
    assert parse_tennis_scoreboard(_tennis_comp([3.0], [1.0], status=_RETIRED, winner="home")) == [
        FinalScore("Player A", "Player B", date(2024, 2, 1), 0, 0, completion="void")
    ]


def test_parse_tennis_retirement_without_winner_flag_left_unsettled() -> None:
    # >=1 completed set but NO ESPN winner flag: the advancing player cannot
    # be affirmatively determined -> emit nothing (never guess); the pick
    # stays open for manual settlement.
    assert parse_tennis_scoreboard(_tennis_comp([6.0, 3.0], [4.0, 1.0], status=_RETIRED)) == []


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
