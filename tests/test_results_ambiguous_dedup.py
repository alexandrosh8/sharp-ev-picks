"""Ambiguous-score dedupe + warning rate-limit (verified audit, 2026-08-06).

The 30s settlement cycle re-warned 'ambiguous score match ... leaving open'
for the same stuck fixture every cycle (~180k lines/48h), and the "ambiguity"
was near-always N identical duplicate rows from the results feed (WTA). Two
fixes under test:

1. Candidates whose score/outcome payload is IDENTICAL are not ambiguous —
   grading is the same whichever row is "the" game, so lookup settles.
   Genuinely conflicting payloads still refuse (fail-closed unchanged).
2. The residual warning fires once per (fixture, UTC day), not per cycle,
   mirroring the engine's unsettleable-warning dedup (c780c1a).
"""

import logging
from datetime import UTC, date, datetime

from app.settlement.results import (
    _AMBIGUOUS_WARNED,
    FinalScore,
    ScoreBook,
    normalize_team,
    reset_ambiguous_warning_state,
)

KICKOFF = datetime(2026, 8, 5, 18, 0, tzinfo=UTC)
D0 = KICKOFF.date()


def _lookup(book: ScoreBook, home: str = "Ostapenko J.", away: str = "Siegemund L."):  # type: ignore[no-untyped-def]
    return book.lookup(home, away, KICKOFF)


def _dup_rows(n: int = 3) -> list[FinalScore]:
    """N byte-identical duplicate feed rows (the observed WTA storm shape)."""
    return [FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 2, 0) for _ in range(n)]


def test_identical_duplicate_candidates_settle(caplog) -> None:  # type: ignore[no-untyped-def]
    reset_ambiguous_warning_state()
    book = ScoreBook(_dup_rows())
    with caplog.at_level(logging.WARNING, logger="app.settlement.results"):
        found = _lookup(book)
    assert found is not None
    assert (found.home_score, found.away_score) == (2, 0)
    assert not [r for r in caplog.records if "ambiguous" in r.getMessage()]


def test_same_payload_different_spellings_settle() -> None:
    # Duplicate rows that differ only in provider name-spelling still carry
    # one payload -> grading identical -> settle.
    reset_ambiguous_warning_state()
    book = ScoreBook(
        [
            FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 2, 0),
            FinalScore("Ostapenko Jelena", "Siegemund Laura", D0, 2, 0),
        ]
    )
    found = _lookup(book)
    assert found is not None
    assert (found.home_score, found.away_score) == (2, 0)


def test_conflicting_payloads_stay_open_with_one_warning(caplog) -> None:  # type: ignore[no-untyped-def]
    reset_ambiguous_warning_state()
    book = ScoreBook(
        [
            FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 2, 0),
            FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 0, 2),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="app.settlement.results"):
        # Three settlement cycles re-scanning the same stuck pick.
        assert _lookup(book) is None
        assert _lookup(book) is None
        assert _lookup(book) is None
    warned = [r for r in caplog.records if "ambiguous score match" in r.getMessage()]
    assert len(warned) == 1


def test_conflicting_completion_stays_open() -> None:
    # Same numeric score but different completion (full vs retired) is a
    # REAL grading conflict (tennis convention) -> fail closed.
    reset_ambiguous_warning_state()
    book = ScoreBook(
        [
            FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 2, 0),
            FinalScore(
                "Jelena Ostapenko",
                "Laura Siegemund",
                D0,
                2,
                0,
                completion="retired",
                winner_side="home",
            ),
        ]
    )
    assert _lookup(book) is None


def test_warning_dedup_is_per_fixture(caplog) -> None:  # type: ignore[no-untyped-def]
    reset_ambiguous_warning_state()
    conflict_a = [
        FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 2, 0),
        FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 0, 2),
    ]
    conflict_b = [
        FinalScore("Santos FC", "Palmeiras SP", D0, 1, 1),
        FinalScore("Santos Laguna", "Palmeiras SP", D0, 3, 0),
    ]
    book = ScoreBook(conflict_a + conflict_b)
    with caplog.at_level(logging.WARNING, logger="app.settlement.results"):
        assert _lookup(book) is None
        assert _lookup(book) is None
        assert book.lookup("Santos", "Palmeiras", KICKOFF) is None
        assert book.lookup("Santos", "Palmeiras", KICKOFF) is None
    warned = [r for r in caplog.records if "ambiguous score match" in r.getMessage()]
    assert len(warned) == 2  # one per fixture, not per lookup


def test_warning_re_emitted_on_new_utc_day(caplog) -> None:  # type: ignore[no-untyped-def]
    reset_ambiguous_warning_state()
    book = ScoreBook(
        [
            FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 2, 0),
            FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 0, 2),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="app.settlement.results"):
        assert _lookup(book) is None
        # Simulate the UTC day rolling over: the stored stamp goes stale.
        key = (normalize_team("Ostapenko J."), normalize_team("Siegemund L."), D0)
        assert key in _AMBIGUOUS_WARNED
        _AMBIGUOUS_WARNED[key] = date(2026, 8, 5)
        assert _lookup(book) is None
    warned = [r for r in caplog.records if "ambiguous score match" in r.getMessage()]
    assert len(warned) == 2


def test_reset_ambiguous_warning_state(caplog) -> None:  # type: ignore[no-untyped-def]
    reset_ambiguous_warning_state()
    book = ScoreBook(
        [
            FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 2, 0),
            FinalScore("Jelena Ostapenko", "Laura Siegemund", D0, 0, 2),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="app.settlement.results"):
        assert _lookup(book) is None
        reset_ambiguous_warning_state()
        assert _lookup(book) is None
    warned = [r for r in caplog.records if "ambiguous score match" in r.getMessage()]
    assert len(warned) == 2
