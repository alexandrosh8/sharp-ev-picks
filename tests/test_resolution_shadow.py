"""Pure match-rate aggregation for the shadow Pinnacle-archive resolver.

These exercise app.resolution.shadow (numpy/stdlib only — no DB). The DB read
that produces ShadowOutcome rows is covered in tests/test_resolution_db.py.
"""

import pytest

from app.resolution.shadow import (
    GroupRate,
    ShadowOutcome,
    arcadia_base_sport,
    summarize_match_rate,
)


def _o(pid: int, sport: str, league: str | None, candidates: int, matched: bool) -> ShadowOutcome:
    return ShadowOutcome(
        pick_id=pid,
        sport=sport,
        league=league,
        candidates_in_window=candidates,
        matched=matched,
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("soccer", "soccer"),
        ("tennis", "tennis"),
        ("basketball", "basketball"),
        ("american_football", "american_football"),
        ("basketball_nba", "basketball"),  # not a base arcadia sport -> first segment
        ("soccer_epl", "soccer"),
    ],
)
def test_arcadia_base_sport(key: str, expected: str) -> None:
    assert arcadia_base_sport(key) == expected


def test_summarize_empty_has_none_rate() -> None:
    report = summarize_match_rate([])
    assert report.total == 0
    assert report.matched == 0
    assert report.match_rate is None
    assert report.by_sport == ()
    assert report.by_league == ()


def test_summarize_counts_overall_rate_and_diagnostic_buckets() -> None:
    outcomes = [
        _o(1, "soccer", "soccer_epl", candidates=2, matched=True),
        _o(2, "soccer", "soccer_epl", candidates=1, matched=False),  # alias/ambiguity gap
        _o(3, "soccer", "soccer_seri", candidates=0, matched=False),  # coverage gap
        _o(4, "basketball_nba", "basketball_nba", candidates=3, matched=True),
    ]
    r = summarize_match_rate(outcomes)
    assert r.total == 4
    assert r.matched == 2
    assert r.match_rate == 0.5
    assert r.no_archive_candidates == 1  # pick 3: no archive event in window
    assert r.unmatched_with_candidates == 1  # pick 2: archive present but strict-rejected


def test_summarize_groups_by_sport_and_league() -> None:
    outcomes = [
        _o(1, "soccer", "soccer_epl", 2, True),
        _o(2, "soccer", "soccer_epl", 1, False),
        _o(3, "basketball_nba", "basketball_nba", 3, True),
    ]
    r = summarize_match_rate(outcomes)
    by_sport = {g.key: g for g in r.by_sport}
    assert by_sport["soccer"].total == 2
    assert by_sport["soccer"].matched == 1
    assert by_sport["soccer"].match_rate == 0.5
    assert by_sport["basketball_nba"].match_rate == 1.0
    by_league = {g.key: g for g in r.by_league}
    assert by_league["soccer_epl"].total == 2
    assert by_league["soccer_epl"].matched == 1


def test_summarize_omits_null_league_from_league_breakdown() -> None:
    outcomes = [
        _o(1, "tennis", None, 0, False),
        _o(2, "tennis", None, 1, True),
    ]
    r = summarize_match_rate(outcomes)
    assert r.by_league == ()  # null leagues never appear as a league row
    assert {g.key for g in r.by_sport} == {"tennis"}
    assert r.by_sport[0].total == 2


def test_group_rate_zero_total_is_none() -> None:
    assert GroupRate(key="x", total=0, matched=0).match_rate is None


def test_groups_are_sorted_by_key_for_determinism() -> None:
    outcomes = [
        _o(1, "tennis", None, 1, True),
        _o(2, "basketball_nba", None, 1, True),
        _o(3, "soccer", None, 1, True),
    ]
    r = summarize_match_rate(outcomes)
    assert [g.key for g in r.by_sport] == ["basketball_nba", "soccer", "tennis"]


def test_as_dict_shape() -> None:
    outcomes = [
        _o(1, "soccer", "soccer_epl", 2, True),
        _o(2, "soccer", None, 0, False),
    ]
    d = summarize_match_rate(outcomes).as_dict()
    assert d["total"] == 2
    assert d["matched"] == 1
    assert d["match_rate"] == 0.5
    assert d["no_archive_candidates"] == 1
    assert d["unmatched_with_candidates"] == 0
    assert d["by_sport"] == [{"key": "soccer", "total": 2, "matched": 1, "match_rate": 0.5}]
    assert d["by_league"] == [{"key": "soccer_epl", "total": 1, "matched": 1, "match_rate": 1.0}]
