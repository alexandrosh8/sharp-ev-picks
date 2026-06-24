"""Reserve-marker recognition for the wrong-game CLV defect (Rank 1.5 part A).

`distinguishing_markers` must flag a TRAILING standalone reserve token — bare
"2"/"3" (MLS Next Pro "Los Angeles FC 2"), "B" (European "Real Madrid B" =
Castilla), plus the existing roman "II"/"III" — as a `reserve` marker, so the
hardened matcher's one-sided marker veto REFUSES to anchor a RESERVE side onto
its SENIOR team's Pinnacle/Betfair close.

PRECISION FIRST. A missed reserve marker = the wrong-game sin (a reserve gets
the senior team's close = fake CLV). A FALSE reserve marker only costs recall (a
REJECT). So these tests err toward CATCHING reserves, while still pinning the
hard false-positive guard: a trailing club-history number that is part of the
SENIOR name ("Schalke 04", "Bayer 04 Leverkusen") must NOT be flagged.

`match_event` (the live anchor loader's matcher) is unchanged — this only tightens
the SHADOW-path `match_event_hardened` via the shared `distinguishing_markers`.
"""

from datetime import UTC, datetime

from app.resolution.matching import (
    AliasTable,
    EventCandidate,
    default_aliases,
    distinguishing_markers,
    match_event_hardened,
)

KO = datetime(2026, 6, 20, 18, 0, tzinfo=UTC)


def _cand(ref: str, home: str, away: str) -> EventCandidate:
    return EventCandidate(ref=ref, home=home, away=away, kickoff=KO)


# --- pure distinguishing_markers: trailing reserve tokens are CAUGHT ---------
def test_trailing_bare_2_is_a_reserve_marker() -> None:
    # MLS Next Pro reserve sides: "<senior> 2". The "FC" noise token is stripped
    # in normalization, leaving "los angeles 2" — the bare trailing "2" must read
    # as reserve (else it anchors onto senior "Los Angeles FC").
    assert distinguishing_markers("Los Angeles FC 2") == frozenset({"reserve"})
    assert distinguishing_markers("Minnesota United 2") == frozenset({"reserve"})
    assert distinguishing_markers("Columbus Crew 2") == frozenset({"reserve"})


def test_trailing_bare_3_is_a_reserve_marker() -> None:
    assert distinguishing_markers("PSV 3") == frozenset({"reserve"})


def test_trailing_b_is_a_reserve_marker() -> None:
    # European reserve sides carry a trailing standalone "B": Real Madrid B =
    # Castilla, Barcelona B, Bayern Munich B.
    assert distinguishing_markers("Real Madrid B") == frozenset({"reserve"})
    assert distinguishing_markers("Barcelona B") == frozenset({"reserve"})


def test_trailing_roman_iii_is_a_reserve_marker() -> None:
    # "III" joins the existing "II" reserve token (third team).
    assert distinguishing_markers("Borussia Dortmund III") == frozenset({"reserve"})
    # existing "II" still flags (regression guard).
    assert distinguishing_markers("Borussia Dortmund II") == frozenset({"reserve"})


# --- pure distinguishing_markers: SENIOR-name numbers are NOT misfired -------
def test_club_history_year_suffix_is_not_a_reserve_marker() -> None:
    # "Schalke 04" / "Bayer 04" end in a club-FOUNDING-YEAR number, NOT a reserve
    # ordinal. Only the exact reserve tokens {2,3,b,ii,iii} count — "04" never.
    assert distinguishing_markers("Schalke 04") == frozenset()
    assert distinguishing_markers("Bayer 04") == frozenset()


def test_mid_name_number_is_not_a_reserve_marker() -> None:
    # A reserve number is TRAILING. "Bayer 04 Leverkusen" carries "04" mid-name
    # and must never be flagged (the senior first team).
    assert distinguishing_markers("Bayer 04 Leverkusen") == frozenset()
    assert distinguishing_markers("FC Schalke 04 Gelsenkirchen") == frozenset()


def test_leading_or_mid_b_is_not_a_reserve_marker() -> None:
    # The reserve "B" is TRAILING and standalone. A "B" that is not the last
    # token is part of the real name, not a reserve ordinal.
    assert "reserve" not in distinguishing_markers("B 1909")
    assert "reserve" not in distinguishing_markers("B Team Hollywood United")


def test_existing_markers_still_flag() -> None:
    # Regression: the women/youth/reserve-word markers are unaffected.
    assert distinguishing_markers("Arsenal Women") == frozenset({"women"})
    assert distinguishing_markers("Boca Juniors U20") == frozenset({"youth"})
    assert distinguishing_markers("Spartak Reserves") == frozenset({"reserve"})


# --- end-to-end: a reserve must NOT match the senior side (the defect) -------
def test_mls_next_pro_reserve_does_not_anchor_to_senior() -> None:
    # Pick is the reserve "Los Angeles FC 2"; the only candidate is the SENIOR
    # "Los Angeles FC". One-sided reserve marker -> the categorical veto REJECTS.
    cands = [_cand("1", "Los Angeles FC", "Minnesota United")]
    assert (
        match_event_hardened(
            "Los Angeles FC 2", "Minnesota United 2", KO, cands, aliases=AliasTable()
        )
        is None
    )


def test_real_madrid_b_does_not_anchor_to_senior() -> None:
    # "Real Madrid B" (Castilla) must not anchor onto the senior "Real Madrid"
    # Pinnacle line.
    cands = [_cand("1", "Real Madrid", "Barcelona")]
    assert (
        match_event_hardened("Real Madrid B", "Barcelona B", KO, cands, aliases=AliasTable())
        is None
    )


def test_both_reserve_sides_still_match() -> None:
    # When BOTH the pick and candidate are the reserve fixture, markers AGREE on
    # both sides -> no veto -> the reserve-vs-reserve close attaches correctly.
    cands = [_cand("1", "Los Angeles FC 2", "Minnesota United 2")]
    m = match_event_hardened(
        "Los Angeles FC 2", "Minnesota United 2", KO, cands, aliases=AliasTable()
    )
    assert m is not None and m.ref == "1"


def test_senior_with_year_suffix_still_matches_itself() -> None:
    # The false-positive guard end-to-end: a senior side whose name ends in a
    # founding year ("Schalke 04") is NOT vetoed against itself.
    cands = [_cand("1", "Schalke 04", "Bayer 04 Leverkusen")]
    m = match_event_hardened("Schalke 04", "Bayer 04 Leverkusen", KO, cands, aliases=AliasTable())
    assert m is not None and m.ref == "1"


def test_trailing_b_false_positive_costs_recall_not_a_wrong_merge() -> None:
    # ACCEPTED TRADE-OFF (precision-first). "Union B." is really a SENIOR alias
    # (B.=Berlin), but the trailing-"b" heuristic conservatively flags it reserve.
    # The marker veto runs on RAW names (it MUST — applying it post-alias would let
    # a real reserve "Real Madrid B"->Castilla agree with senior "Real Madrid" and
    # merge, the wrong-game sin). So the senior "Union B." vs canonical-senior
    # candidate is REJECTED: a RECALL loss (no close attached), NOT a wrong-game
    # merge. Per the directive, a false reserve marker is the safe direction to err.
    al = default_aliases()
    cands = [_cand("1", "1. FC Union Berlin", "Werder Bremen")]
    assert match_event_hardened("Union B.", "Werder Bremen", KO, cands, aliases=al) is None
    # ...but the SAME raw surface on BOTH sides (the common archive case) agrees on
    # the marker and still resolves — the recall loss is only the asymmetric case.
    cands_sym = [_cand("1", "Union B.", "Werder Bremen")]
    m = match_event_hardened("Union B.", "Werder Bremen", KO, cands_sym, aliases=al)
    assert m is not None and m.ref == "1"
