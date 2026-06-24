"""Precision-hardened cross-source matcher — `match_event_hardened` (no IO, no DB).

The hardened matcher is the SHADOW-path lift over the strict `match_event`: it
adds (STAGE0) a league + UTC-minute kickoff block, (STAGE1) a categorical
marker-veto with a known-club whitelist, (STAGE4/5) a two-tier rapidfuzz-free
Jaro-Winkler + token-sort accept band with a disambiguating-token blocklist and
a top-two-margin reject, and an orientation-flip that needs time+league to
independently agree. A wrong-game match = fake CLV (the cardinal sin), so every
recall lever is paired with an independent precision guard. `match_event` is
left UNCHANGED (the live anchor loader depends on it) — these tests pin the new
function only.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.resolution.matching import (
    _ACCEPT_MINUTE_DRIFT,
    AliasTable,
    EventCandidate,
    default_aliases,
    jaro_winkler,
    match_event_hardened,
    strip_markers,
    token_sort_ratio,
)

KO = datetime(2026, 6, 20, 18, 0, tzinfo=UTC)


def _cand(
    ref: str,
    home: str,
    away: str,
    kickoff: datetime = KO,
) -> EventCandidate:
    return EventCandidate(ref=ref, home=home, away=away, kickoff=kickoff)


# --- pure string-similarity kernels (replicate rapidfuzz semantics, stdlib) ---
def test_jaro_winkler_identical_is_one() -> None:
    assert jaro_winkler("manchester united", "manchester united") == pytest.approx(1.0)


def test_jaro_winkler_prefix_bonus_rewards_shared_start() -> None:
    # JW gives a prefix bonus -> a shared leading run scores higher than the
    # underlying Jaro. "manchester united" vs "manchester utd" share a long prefix.
    jw = jaro_winkler("manchester united", "manchester utd")
    assert 0.90 < jw < 1.0


def test_jaro_winkler_disjoint_names_score_low() -> None:
    # Two clearly different clubs must NOT score in the accept band.
    assert jaro_winkler("internazionale", "ac milan") < 0.84


def test_jaro_winkler_is_in_unit_interval() -> None:
    for a, b in [("", ""), ("a", ""), ("", "b"), ("abc", "xyz"), ("alpha", "alphb")]:
        assert 0.0 <= jaro_winkler(a, b) <= 1.0


def test_token_sort_ratio_is_order_insensitive() -> None:
    # token_sort_ratio sorts tokens before comparing -> word-order variants match.
    assert token_sort_ratio("real madrid", "madrid real") == pytest.approx(100.0)


def test_token_sort_ratio_penalizes_disambiguating_token() -> None:
    # "manchester united" vs "manchester city" share a token but differ on the
    # disambiguating one -> token_sort_ratio is well below the 90 accept floor.
    assert token_sort_ratio("manchester united", "manchester city") < 90.0


# --- STAGE 1: marker veto ----------------------------------------------------
def test_one_sided_women_marker_is_rejected() -> None:
    # Pick carries the women marker, candidate does not (the men's side) -> the
    # categorical marker veto REJECTS even though the base names match.
    cands = [_cand("1", "Wolves", "Arsenal")]
    assert (
        match_event_hardened("Wolves Women", "Arsenal Women", KO, cands, aliases=default_aliases())
        is None
    )


def test_one_sided_youth_marker_is_rejected() -> None:
    cands = [_cand("1", "Boca Juniors", "River Plate")]
    assert (
        match_event_hardened("Boca Juniors U20", "River Plate U20", KO, cands, aliases=AliasTable())
        is None
    )


def test_one_sided_reserve_marker_is_rejected() -> None:
    cands = [_cand("1", "Spartak", "Zenit")]
    assert (
        match_event_hardened("Spartak Reserves", "Zenit Reserves", KO, cands, aliases=AliasTable())
        is None
    )


def test_both_sides_share_marker_is_allowed() -> None:
    # Women's pick vs women's candidate -> markers AGREE, base names match -> match.
    cands = [_cand("1", "Arsenal Women", "Chelsea Women")]
    m = match_event_hardened("Arsenal Women", "Chelsea Women", KO, cands, aliases=AliasTable())
    assert m is not None and m.ref == "1"


def test_whitelisted_club_token_is_not_a_marker() -> None:
    # A whitelisted senior club whose name contains a marker-LIKE token (Boca
    # Juniors -> "juniors", Young Boys -> "young") must NOT be vetoed as youth.
    assert strip_markers("Boca Juniors") == strip_markers("Boca Juniors")
    cands = [_cand("1", "Young Boys", "Boca Juniors")]
    m = match_event_hardened("Young Boys", "Boca Juniors", KO, cands, aliases=AliasTable())
    assert m is not None and m.ref == "1"


# --- STAGE 0: league + UTC kickoff-window block ------------------------------
def test_league_disagreement_blocks_match() -> None:
    # Same names, but the two events are in DIFFERENT canonical leagues -> a
    # name coincidence across competitions must NOT merge.
    cands = [_cand("1", "Alpha", "Beta")]
    assert (
        match_event_hardened(
            "Alpha",
            "Beta",
            KO,
            cands,
            aliases=AliasTable(),
            league="england-premier-league",
            candidate_leagues={"1": "spain-laliga"},
        )
        is None
    )


def test_league_agreement_allows_match() -> None:
    cands = [_cand("1", "Alpha", "Beta")]
    m = match_event_hardened(
        "Alpha",
        "Beta",
        KO,
        cands,
        aliases=AliasTable(),
        league="england-premier-league",
        candidate_leagues={"1": "england-premier-league"},
    )
    assert m is not None and m.ref == "1"


def test_missing_league_does_not_block() -> None:
    # League is an OPTIONAL precision lever — when either side's league is
    # unknown the block is skipped (we never reject on absent metadata).
    cands = [_cand("1", "Alpha", "Beta")]
    m = match_event_hardened(
        "Alpha", "Beta", KO, cands, aliases=AliasTable(), league=None, candidate_leagues=None
    )
    assert m is not None and m.ref == "1"


def test_kickoff_outside_minute_window_no_match() -> None:
    # The hardened block uses a UTC TIME window (minutes), tighter than the
    # strict calendar-day window: a candidate 600 min away is rejected.
    far = [_cand("1", "Alpha", "Beta", KO + timedelta(minutes=600))]
    assert (
        match_event_hardened("Alpha", "Beta", KO, far, aliases=AliasTable(), max_minute_drift=240)
        is None
    )


def test_kickoff_inside_minute_window_matches() -> None:
    near = [_cand("1", "Alpha", "Beta", KO + timedelta(minutes=30))]
    m = match_event_hardened("Alpha", "Beta", KO, near, aliases=AliasTable(), max_minute_drift=240)
    assert m is not None and m.ref == "1"


# --- STAGE 4/5: two-tier fuzzy accept ---------------------------------------
def test_two_tier_accepts_high_jw_and_token_sort() -> None:
    # "Manchester Utd" is a near-exact spelling variant (no alias) that clears
    # BOTH JW>=0.92 AND token_sort>=90.
    cands = [_cand("1", "Manchester Utd", "Liverpool")]
    m = match_event_hardened("Manchester United", "Liverpool", KO, cands, aliases=AliasTable())
    assert m is not None and m.ref == "1"


def test_two_tier_rejects_review_band() -> None:
    # "Atletico Madrid" vs "Athletico Paranaense": share a long prefix (review
    # band) but are different clubs -> must not merge.
    cands = [_cand("1", "Athletico Paranaense", "Flamengo")]
    assert (
        match_event_hardened("Atletico Madrid", "Flamengo", KO, cands, aliases=AliasTable()) is None
    )


def test_disambiguating_token_blocklist_rejects_city_vs_united() -> None:
    # The single biggest false-merge risk: Man Utd vs Man City share a high base
    # similarity but differ on a disambiguating token -> blocklist REJECTS.
    cands = [_cand("1", "Manchester City", "Liverpool")]
    assert (
        match_event_hardened("Manchester United", "Liverpool", KO, cands, aliases=AliasTable())
        is None
    )


def test_two_candidates_within_margin_reject() -> None:
    # If the top-two candidates both clear the bar and are within the ambiguity
    # margin, the matcher REJECTS (cannot tell which is the real fixture).
    cands = [
        _cand("1", "Manchester Utd", "Liverpool"),
        _cand("2", "Manchester Unitd", "Liverpool"),
    ]
    assert (
        match_event_hardened("Manchester United", "Liverpool", KO, cands, aliases=AliasTable())
        is None
    )


# --- same-teams REMATCH / two-leg series: tight accept threshold -------------
# A wide candidate-fetch window (so ambiguity detection sees BOTH legs) must NOT
# translate into a wide ACCEPT window: a fixture between the SAME teams two days
# apart is a DIFFERENT game (home/away swapped second leg, BSN Puerto Rico /
# tennis / cup two-leg). It must NEVER be accepted as the pick's close — that is
# fake CLV, the cardinal sin (live wrong-game audit, Gigantes/Cangrejeros).

# The drift band where a same-teams fixture is a different game, not noise.
_TWO_DAYS = 2 * 24 * 60


def test_same_teams_two_days_apart_is_rejected_not_matched() -> None:
    # The live defect: pick @ today, only same-teams candidate is the leg two days
    # earlier (forward orientation matched it via the slug). The wide fetch window
    # would admit it, but it is beyond the tight accept bound -> REJECT, never the
    # wrong leg.
    far_leg = _cand("21", "Alpha", "Beta", KO - timedelta(days=2))
    m = match_event_hardened(
        "Alpha",
        "Beta",
        KO,
        [far_leg],
        aliases=AliasTable(),
        max_minute_drift=_TWO_DAYS,  # wide fetch window (the caller's bound)
    )
    assert m is None


def test_same_teams_picks_closest_leg_within_tight_bound() -> None:
    # Both legs of the series fall in the WIDE fetch window; the correct same-day
    # leg is within the tight accept bound, the rematch two days away is not. The
    # matcher must pick the close leg, never collapse to the far one.
    close_leg = _cand("23", "Alpha", "Beta", KO)
    far_leg = _cand("21", "Alpha", "Beta", KO - timedelta(days=2))
    m = match_event_hardened(
        "Alpha",
        "Beta",
        KO,
        [far_leg, close_leg],
        aliases=AliasTable(),
        max_minute_drift=_TWO_DAYS,
    )
    assert m is not None and m.ref == "23"


def test_two_distinct_same_teams_games_in_accept_band_reject() -> None:
    # Two DISTINCT same-teams games BOTH inside the tight accept band (a true
    # doubleheader / two legs <6h apart) cannot be told apart by name -> REJECT.
    # They must NOT silently collapse to the nearer kickoff.
    leg_a = _cand("a", "Alpha", "Beta", KO - timedelta(hours=5))
    leg_b = _cand("b", "Alpha", "Beta", KO + timedelta(hours=5))
    m = match_event_hardened(
        "Alpha",
        "Beta",
        KO,
        [leg_a, leg_b],
        aliases=AliasTable(),
        max_minute_drift=_TWO_DAYS,
    )
    assert m is None


def test_same_day_timezone_noise_still_matches() -> None:
    # RECALL guard: a few hours of cross-source timezone/rounding noise on the SAME
    # game (the date-only 00:00 archive row vs a real wall-clock pick) must STILL
    # match. A 3-hour drift is within the tight accept bound.
    near = _cand("1", "Alpha", "Beta", KO + timedelta(hours=3))
    m = match_event_hardened(
        "Alpha", "Beta", KO, [near], aliases=AliasTable(), max_minute_drift=_TWO_DAYS
    )
    assert m is not None and m.ref == "1"


def test_duplicate_same_game_captures_still_collapse_to_nearest() -> None:
    # Two captures of the ONE same game, both inside the tight accept bound (a
    # duplicate archive row a few minutes apart) are NOT distinct games -> they
    # still collapse to the nearest-kickoff capture (recall preserved).
    dup_a = _cand("a", "Alpha", "Beta", KO + timedelta(minutes=5))
    dup_b = _cand("b", "Alpha", "Beta", KO + timedelta(minutes=40))
    m = match_event_hardened(
        "Alpha",
        "Beta",
        KO,
        [dup_a, dup_b],
        aliases=AliasTable(),
        max_minute_drift=_TWO_DAYS,
    )
    assert m is not None and m.ref == "a"


@pytest.mark.parametrize("hours_apart", [6.1, 12, 24, 48, 72])
def test_property_same_teams_beyond_accept_bound_never_matches(hours_apart: float) -> None:
    # PROPERTY: same teams + kickoff strictly beyond the accept bound => a
    # DIFFERENT game => the matcher must REJECT or pick a closer leg, NEVER attach
    # the far leg. With only the far leg present, the result is always None.
    assert _ACCEPT_MINUTE_DRIFT < 6.1 * 60  # the bound is tighter than the band tested
    far = _cand("far", "Alpha", "Beta", KO + timedelta(hours=hours_apart))
    m = match_event_hardened(
        "Alpha", "Beta", KO, [far], aliases=AliasTable(), max_minute_drift=_TWO_DAYS
    )
    assert m is None


# --- orientation flip --------------------------------------------------------
def test_orientation_flip_accepted_with_time_and_league() -> None:
    # ordered (soccer): a home/away SWAP is accepted ONLY when kickoff-in-window
    # AND league independently agree.
    cands = [_cand("1", "Beta", "Alpha")]
    m = match_event_hardened(
        "Alpha",
        "Beta",
        KO,
        cands,
        aliases=AliasTable(),
        ordered=True,
        league="L1",
        candidate_leagues={"1": "L1"},
        allow_orientation_flip=True,
    )
    assert m is not None and m.ref == "1"


def test_orientation_flip_rejected_without_league() -> None:
    # The same swap with NO league agreement available must NOT flip on name
    # alone — a swapped name coincidence is not a confirmed fixture.
    cands = [_cand("1", "Beta", "Alpha")]
    assert (
        match_event_hardened(
            "Alpha",
            "Beta",
            KO,
            cands,
            aliases=AliasTable(),
            ordered=True,
            league=None,
            candidate_leagues=None,
            allow_orientation_flip=True,
        )
        is None
    )


def test_orientation_flip_off_by_default() -> None:
    cands = [_cand("1", "Beta", "Alpha")]
    assert (
        match_event_hardened(
            "Alpha",
            "Beta",
            KO,
            cands,
            aliases=AliasTable(),
            ordered=True,
            league="L1",
            candidate_leagues={"1": "L1"},
        )
        is None
    )


# --- inherited cardinal-sin guards (must still hold) -------------------------
def test_degenerate_same_name_pair_is_none() -> None:
    cands = [_cand("1", "Alpha", "Alpha")]
    assert match_event_hardened("Alpha", "Alpha", KO, cands, aliases=AliasTable()) is None


def test_exact_alias_path_still_matches() -> None:
    # The exact-alias accept tier (tier 1) is preserved: Man Utd -> canonical.
    cands = [_cand("1", "Manchester United", "Manchester City")]
    m = match_event_hardened("Man Utd", "Man City", KO, cands, aliases=default_aliases())
    assert m is not None and m.ref == "1"


def test_no_match_for_unrelated_teams() -> None:
    assert (
        match_event_hardened(
            "Alpha", "Beta", KO, [_cand("1", "Gamma", "Delta")], aliases=AliasTable()
        )
        is None
    )


# --- data-quality alarm: men's-league name carrying a women marker -----------
def _mens_league_women_marker_alarm(league: str, name: str) -> bool:
    """A name in a known MEN'S-ONLY league must not carry a women marker. True
    flags a data-quality ALARM (quarantine the row) rather than letting it pass
    silently into the matcher — a mislabelled feed could otherwise marker-veto a
    real men's fixture or, worse, conflate sides."""
    from app.resolution.matching import distinguishing_markers

    mens_only = {"nba", "premier-league", "laliga", "serie-a", "bundesliga"}
    return league.lower() in mens_only and "women" in distinguishing_markers(name)


def test_mens_league_with_women_marker_raises_data_alarm() -> None:
    # An NBA (men's-only) feed row carrying a stray "W" is a DATA ALARM.
    assert _mens_league_women_marker_alarm("nba", "Los Angeles Lakers W") is True
    # The clean men's name in the same league is NOT an alarm.
    assert _mens_league_women_marker_alarm("nba", "Los Angeles Lakers") is False
    # A women's league legitimately carrying the marker is NOT an alarm (it is
    # the correct, expected label there).
    assert _mens_league_women_marker_alarm("wnba", "Los Angeles Sparks W") is False


def test_strip_markers_preserves_whitelisted_club_base() -> None:
    # Property: a whitelisted senior club's base name survives strip_markers
    # intact — its marker-like token is part of the real name, never stripped.
    from app.resolution.matching import normalize_name, strip_markers

    assert strip_markers("Boca Juniors") == normalize_name("Boca Juniors")
    assert strip_markers("Young Boys") == normalize_name("Young Boys")
    # A genuine youth side IS stripped to its base.
    assert strip_markers("Spartak U20") == normalize_name("Spartak")
    assert strip_markers("Arsenal Women") == normalize_name("Arsenal")
