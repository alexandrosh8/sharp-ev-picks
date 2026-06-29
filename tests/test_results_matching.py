"""Wrong-game safety for settlement score matching (ScoreBook.lookup).

Settlement sets won/lost/pnl/roi, so its name match must be at least as strict
about distinguishing markers (women/youth/reserve/B) as the CLV wrong-game
matcher. A men's pick must NEVER settle from a women's/youth/reserve score that
merely CONTAINS its base name on the same date, and vice-versa.
"""

from datetime import UTC, date, datetime

from app.settlement.results import FinalScore, ScoreBook

KICK = datetime(2026, 6, 9, 18, 0, tzinfo=UTC)


def _score(home: str, away: str, hs: int = 2, as_: int = 1) -> FinalScore:
    return FinalScore(
        home_team=home,
        away_team=away,
        match_date=date(2026, 6, 9),
        home_score=hs,
        away_score=as_,
    )


# --- wrong-game blocked (was bound by loose containment) --------------------


def test_mens_pick_does_not_settle_from_womens_score() -> None:
    book = ScoreBook([_score("Arsenal Women", "Chelsea Women")])
    assert book.lookup("Arsenal", "Chelsea", KICK) is None


def test_mens_pick_does_not_settle_from_youth_score() -> None:
    book = ScoreBook([_score("Arsenal U21", "Chelsea U21")])
    assert book.lookup("Arsenal", "Chelsea", KICK) is None


def test_mens_pick_does_not_settle_from_reserve_b_score() -> None:
    book = ScoreBook([_score("Arsenal B", "Chelsea B")])
    assert book.lookup("Arsenal", "Chelsea", KICK) is None


def test_womens_pick_does_not_settle_from_mens_score() -> None:
    book = ScoreBook([_score("Arsenal", "Chelsea")])
    assert book.lookup("Arsenal Women", "Chelsea Women", KICK) is None


def test_one_sided_marker_mismatch_blocks() -> None:
    # Only the home side carries a marker mismatch -> still blocked.
    book = ScoreBook([_score("Arsenal Women", "Chelsea")])
    assert book.lookup("Arsenal", "Chelsea", KICK) is None


# --- legitimate same-category matches still settle (no regression) ----------


def test_exact_senior_match_still_settles() -> None:
    book = ScoreBook([_score("Flamengo", "Palmeiras")])
    found = book.lookup("Flamengo", "Palmeiras", KICK)
    assert found is not None
    assert (found.home_score, found.away_score) == (2, 1)


def test_containment_alias_same_category_still_settles() -> None:
    # OddsPortal "Flamengo RJ" vs football-data "Flamengo" — no markers on either,
    # so the senior containment fallback must still bind.
    book = ScoreBook([_score("Flamengo", "Palmeiras")])
    assert book.lookup("Flamengo RJ", "Palmeiras", KICK) is not None


def test_womens_pick_settles_from_womens_score() -> None:
    # Markers agree on both sides -> the women's fixture settles normally, incl.
    # the "W" vs "Women" containment form.
    book = ScoreBook([_score("Arsenal Women", "Chelsea Women")])
    assert book.lookup("Arsenal W", "Chelsea W", KICK) is not None
