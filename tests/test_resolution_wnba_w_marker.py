"""H6: fail-closed WNBA ``W``-suffix recovery.

The Pinnacle basketball archive conflates NBA/WNBA in one namespace and the
live anchor path has no comparable cross-source league label. Therefore a marker
exception cannot rely on ``league=None`` or blanket ``W`` stripping. Recovery is
allowed only when BOTH oriented participants have:

* an explicit canonical<->``W`` alias in the reviewed local seed; and
* membership in the explicit all-women WNBA roster metadata.

The generic women/men, youth, and reserve marker vetoes remain absolute.
"""

from datetime import UTC, datetime

import pytest

from app.resolution.matching import (
    AliasTable,
    EventCandidate,
    _markers_conflict,
    default_aliases,
    distinguishing_markers,
    match_event_hardened,
)

KO = datetime(2026, 6, 29, 23, 0, tzinfo=UTC)


def _match(
    qh: str,
    qa: str,
    ch: str,
    ca: str,
    *,
    league: str | None = None,
    aliases: AliasTable | None = None,
) -> bool:
    """True iff the hardened matcher anchors the candidate to the query."""
    cand = [EventCandidate(ref="1", home=ch, away=ca, kickoff=KO)]
    cl = {"1": league} if league is not None else None
    matched = match_event_hardened(
        qh,
        qa,
        KO,
        cand,
        aliases=aliases or default_aliases(),
        ordered=True,
        league=league,
        candidate_leagues=cl,
    )
    return matched is not None


# --- ABSOLUTE CONSTRAINT: men's X never matches women's X W -------------------
def test_mens_team_never_matches_womens_team_unknown_league() -> None:
    # No league context (the live anchor path): an unconfirmed one-sided "W"
    # MUST veto so a men's pick never anchors onto a women's close (or vice versa).
    assert _markers_conflict(
        "Los Angeles Lakers", "Boston Celtics", "Los Angeles Lakers W", "Boston Celtics W"
    )
    assert not _match(
        "Los Angeles Lakers", "Boston Celtics", "Los Angeles Lakers W", "Boston Celtics W"
    )


def test_mens_team_never_matches_womens_team_mixed_basketball_namespace() -> None:
    # A generic basketball label does not establish an all-women competition.
    assert not _match(
        "Phoenix Suns",
        "Dallas Mavericks",
        "Phoenix Suns W",
        "Dallas Mavericks W",
        league="basketball",
    )


def test_one_sided_w_difference_vetoes_when_only_one_side_carries_it() -> None:
    assert _markers_conflict("Some Club", "Other Club", "Some Club W", "Other Club")
    assert not _match("Some Club", "Other Club", "Some Club W", "Other Club")


# --- youth / reserve vetoes are UNCHANGED ------------------------------------
def test_youth_marker_veto_still_fires() -> None:
    assert _markers_conflict("Argentina U19", "Brazil U19", "Argentina", "Brazil")
    assert "youth" in distinguishing_markers("Argentina U19")


def test_reserve_marker_veto_still_fires() -> None:
    assert _markers_conflict("Real Madrid B", "Barcelona B", "Real Madrid", "Barcelona")
    assert "reserve" in distinguishing_markers("Real Madrid B")
    assert _markers_conflict("Bayern II", "Dortmund II", "Bayern", "Dortmund")


# --- scoped WNBA recovery -----------------------------------------------------
@pytest.mark.parametrize(
    ("query_home", "query_away", "candidate_home", "candidate_away"),
    [
        ("Las Vegas Aces", "New York Liberty", "Las Vegas Aces W", "New York Liberty W"),
        ("Las Vegas Aces W", "New York Liberty W", "Las Vegas Aces", "New York Liberty"),
        ("Las Vegas Aces", "New York Liberty", "Las Vegas Aces W", "New York Liberty"),
        ("Portland Fire", "Toronto Tempo", "Portland Fire W", "Toronto Tempo W"),
    ],
)
def test_bilaterally_confirmed_wnba_w_difference_matches(
    query_home: str,
    query_away: str,
    candidate_home: str,
    candidate_away: str,
) -> None:
    # The raw marker detector remains strict; only the full fixture-level matcher
    # may admit this exact, bilaterally confirmed WNBA case.
    assert _markers_conflict(query_home, query_away, candidate_home, candidate_away)
    assert _match(query_home, query_away, candidate_home, candidate_away)


def test_wnba_exception_requires_both_fixture_sides_to_be_confirmed() -> None:
    assert not _match(
        "Las Vegas Aces",
        "Los Angeles Lakers",
        "Las Vegas Aces W",
        "Los Angeles Lakers",
    )


def test_wnba_roster_membership_without_curated_aliases_is_insufficient() -> None:
    # Marker stripping alone would make both bases equal. An empty alias table
    # proves the explicit canonical<->W seed relationship is also mandatory.
    assert not _match(
        "Las Vegas Aces",
        "New York Liberty",
        "Las Vegas Aces W",
        "New York Liberty W",
        aliases=AliasTable(),
    )


def test_wnba_exception_rejects_fuzzy_or_non_w_marker_forms() -> None:
    assert not _match(
        "Las Vegas Aces",
        "New York Liberty",
        "Las Vegas Ace W",
        "New York Liberty W",
    )
    assert not _match(
        "Las Vegas Aces",
        "New York Liberty",
        "Las Vegas Aces Women",
        "New York Liberty Women",
    )


def test_wnba_exception_never_suppresses_youth_or_reserve_conflicts() -> None:
    assert not _match(
        "Las Vegas Aces U19",
        "New York Liberty U19",
        "Las Vegas Aces W",
        "New York Liberty W",
    )
    assert not _match(
        "Las Vegas Aces B",
        "New York Liberty B",
        "Las Vegas Aces W",
        "New York Liberty W",
    )
