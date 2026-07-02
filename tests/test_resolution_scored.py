"""Scored hardened matcher (MatchOutcome) + review-band tap — observability only.

Proves the R1 design contract: ``match_event_hardened_scored`` returns the SAME
accept/reject decision as the historical ``match_event_hardened`` (now a thin
wrapper) plus confidence provenance; the optional ``review_out`` tap records the
silently-discarded borderline bands WITHOUT changing any decision; categorical
wrong-game vetoes (disambiguating tokens / markers) are never "reviewable".
Pure unit tests — no DB, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.resolution.matching import (
    _JW_ACCEPT,
    _JW_REVIEW_FLOOR,
    AliasTable,
    EventCandidate,
    MatchReviewCandidate,
    jaro_winkler,
    match_event_hardened,
    match_event_hardened_scored,
)

KO = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
ALIASES = AliasTable()  # empty: deterministic, no seed-file dependence


def cand(ref: str, home: str, away: str, kickoff: datetime = KO) -> EventCandidate:
    return EventCandidate(ref=ref, home=home, away=away, kickoff=kickoff)


def test_exact_canonical_match_scores_confidence_one() -> None:
    c = cand("e1", "Borussia Dortmund", "Bayern Munchen")
    outcome = match_event_hardened_scored(
        "Borussia Dortmund", "Bayern Munchen", KO, [c], aliases=ALIASES
    )
    assert outcome is not None
    assert outcome.candidate is c
    assert outcome.confidence == 1.0
    assert outcome.method == "exact_canonical"


def test_fuzzy_accept_scores_min_side_jw_and_jw_two_tier() -> None:
    # 'borussia dortmund' vs 'borussia dortmond': JW 0.9765 >= 0.92, ts 94.1 >= 90
    # -> fuzzy ACCEPT tier; away side exact (JW 1.0) -> min-side JW is the home JW.
    c = cand("e1", "Borussia Dortmond", "Bayern Munchen")
    outcome = match_event_hardened_scored(
        "Borussia Dortmund", "Bayern Munchen", KO, [c], aliases=ALIASES
    )
    assert outcome is not None
    assert outcome.method == "jw_two_tier"
    expected = jaro_winkler("borussia dortmund", "borussia dortmond")
    assert _JW_ACCEPT <= expected < 1.0  # sanity: genuinely the fuzzy band
    assert outcome.confidence == expected
    assert 0.0 <= outcome.confidence <= 1.0


def test_wrapper_returns_same_candidate_as_scored_variant() -> None:
    # match_event_hardened is now a thin wrapper: identical decisions, bare candidate.
    cases: list[list[EventCandidate]] = [
        [cand("e1", "Borussia Dortmund", "Bayern Munchen")],  # exact accept
        [cand("e2", "Borussia Dortmond", "Bayern Munchen")],  # fuzzy accept
        [cand("e3", "Atletico Madeira", "Slavia Sofia")],  # reject
    ]
    for candidates in cases:
        scored = match_event_hardened_scored(
            "Borussia Dortmund", "Bayern Munchen", KO, candidates, aliases=ALIASES
        )
        bare = match_event_hardened(
            "Borussia Dortmund", "Bayern Munchen", KO, candidates, aliases=ALIASES
        )
        if scored is None:
            assert bare is None
        else:
            assert bare is scored.candidate


def test_review_band_reject_stays_rejected_and_lands_in_tap() -> None:
    # 'atletico mineiro' vs 'atletico madeira': JW 0.9096 in [0.84, 0.92) — the
    # documented REVIEW band. The match must still FAIL exactly as before; the
    # tap records it (reason jw_below_accept) for the human review queue.
    jw = jaro_winkler("atletico mineiro", "atletico madeira")
    assert _JW_REVIEW_FLOOR <= jw < _JW_ACCEPT  # sanity: genuinely in-band
    c = cand("e-band", "Atletico Madeira", "Bayern Munchen")
    taps: list[MatchReviewCandidate] = []
    outcome = match_event_hardened_scored(
        "Atletico Mineiro", "Bayern Munchen", KO, [c], aliases=ALIASES, review_out=taps
    )
    assert outcome is None  # STILL rejected — the tap is never a gate
    assert len(taps) == 1
    tap = taps[0]
    assert tap.candidate is c
    assert tap.reason == "jw_below_accept"
    assert tap.confidence == jw  # min-side JW (away side is exact, JW 1.0)
    assert tap.evidence["query_base_home"] == "atletico mineiro"
    assert tap.evidence["candidate_base_home"] == "atletico madeira"
    assert tap.evidence["kickoff_delta_seconds"] == 0.0


def test_token_sort_below_accept_band_is_tapped() -> None:
    # 'feyenoord' vs 'feyenrood': JW 0.9778 >= 0.92 but token_sort 88.9 < 90 —
    # the second silent band. Rejected, tapped with its own reason.
    c = cand("e-ts", "Feyenrood", "Bayern Munchen")
    taps: list[MatchReviewCandidate] = []
    outcome = match_event_hardened_scored(
        "Feyenoord", "Bayern Munchen", KO, [c], aliases=ALIASES, review_out=taps
    )
    assert outcome is None
    assert [t.reason for t in taps] == ["token_sort_below_accept"]


def test_wrong_game_disambiguating_pair_rejected_and_never_reviewable() -> None:
    # Man Utd vs Man City: the categorical disambiguating-token veto. Rejected
    # AND kept OUT of the review tap — recovery of that class must go through
    # reviewed per-club aliases, never a queue entry that invites a threshold drop.
    c = cand("e-city", "Manchester City", "Bayern Munchen")
    taps: list[MatchReviewCandidate] = []
    outcome = match_event_hardened_scored(
        "Manchester United", "Bayern Munchen", KO, [c], aliases=ALIASES, review_out=taps
    )
    assert outcome is None
    assert taps == []  # categorical veto: decided, not borderline


def test_women_marker_conflict_rejected_and_never_reviewable() -> None:
    # A one-sided women's marker is a categorical wrong-fixture veto (never a
    # borderline near-miss): rejected with an EMPTY tap.
    c = cand("e-w", "Arsenal Women", "Chelsea")
    taps: list[MatchReviewCandidate] = []
    outcome = match_event_hardened_scored(
        "Arsenal", "Chelsea", KO, [c], aliases=ALIASES, review_out=taps
    )
    assert outcome is None
    assert taps == []


def test_ambiguity_reject_taps_both_candidates() -> None:
    # Two DISTINCT candidates, both passing the two-tier accept, summed scores
    # 1.9765 vs 1.9714 (delta 0.005 < margin 0.04) -> ambiguity REJECT
    # (unchanged), and BOTH are tapped as ambiguity_margin.
    c1 = cand("e-a1", "Borussia Dortmond", "Bayern Munchen")
    c2 = cand("e-a2", "Borussia Dortmund", "Bayern Munchem")
    taps: list[MatchReviewCandidate] = []
    outcome = match_event_hardened_scored(
        "Borussia Dortmund", "Bayern Munchen", KO, [c1, c2], aliases=ALIASES, review_out=taps
    )
    assert outcome is None
    assert {t.reason for t in taps} == {"ambiguity_margin"}
    assert {t.candidate.ref for t in taps} == {"e-a1", "e-a2"}


def test_kickoff_drift_reject_is_tapped() -> None:
    # Name-accepted candidate OUTSIDE the tight accept window (fetch window is
    # wide): rejected as before, tapped as kickoff_drift.
    far = KO + timedelta(hours=20)
    c = cand("e-drift", "Borussia Dortmund", "Bayern Munchen", kickoff=far)
    taps: list[MatchReviewCandidate] = []
    outcome = match_event_hardened_scored(
        "Borussia Dortmund",
        "Bayern Munchen",
        KO,
        [c],
        aliases=ALIASES,
        max_minute_drift=2 * 24 * 60,  # wide fetch window (the resolver's shape)
        review_out=taps,
    )
    assert outcome is None
    assert [t.reason for t in taps] == ["kickoff_drift"]
    assert taps[0].evidence["kickoff_delta_seconds"] == 20 * 3600.0


def test_review_out_none_changes_nothing() -> None:
    # The tap default (None) is byte-identical behavior: same rejects, no crash.
    c = cand("e-band", "Atletico Madeira", "Bayern Munchen")
    assert (
        match_event_hardened_scored("Atletico Mineiro", "Bayern Munchen", KO, [c], aliases=ALIASES)
        is None
    )
