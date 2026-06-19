"""Pure cross-source matcher — strictness + cardinal-sin guards (no IO, no DB).

A wrong Pinnacle close corrupts CLV, so these tests pin the STRICT behavior:
exact normalized names (+ alias table) and a small kickoff window, with NO
fuzzy/containment match and NO best-available fallback (ambiguous -> no match).
"""

from datetime import UTC, datetime, timedelta

from app.resolution.matching import (
    AliasTable,
    EventCandidate,
    default_aliases,
    match_event,
    normalize_name,
)

KO = datetime(2026, 6, 20, 18, 0, tzinfo=UTC)


def _cand(ref: str, home: str, away: str, kickoff: datetime = KO) -> EventCandidate:
    return EventCandidate(ref=ref, home=home, away=away, kickoff=kickoff)


# --- normalization ---------------------------------------------------------
def test_normalize_strips_accents_and_club_noise() -> None:
    assert normalize_name("Atlético Madrid CF") == "atletico madrid"
    assert normalize_name("FC Bayern München") == "bayern munchen"
    assert normalize_name("Manchester United") == "manchester united"


def test_normalize_strips_jk_club_suffix() -> None:
    # "JK" (Jimnastik/Jalgpalli Kulübü) is a club-form suffix like FC/SC — the
    # Pinnacle-vs-OddsPortal "Besiktas JK" / "Besiktas" mismatch the probe found.
    assert normalize_name("Besiktas JK") == normalize_name("Besiktas")


def test_normalize_preserves_women_marker() -> None:
    assert "women" in normalize_name("Arsenal Women").split()
    assert normalize_name("Arsenal Women") != normalize_name("Arsenal")


def test_normalize_all_noise_is_empty() -> None:
    assert normalize_name("FC") == ""
    assert normalize_name("   ") == ""


# --- alias table -----------------------------------------------------------
def test_alias_collapses_known_alias_to_canonical() -> None:
    t = default_aliases()
    assert t.canonical("Man Utd") == t.canonical("Manchester United")
    assert t.canonical("Bayern") == normalize_name("Bayern Munich")
    assert t.canonical("PSG") == normalize_name("Paris Saint-Germain")


def test_alias_unknown_passes_through_normalized() -> None:
    assert default_aliases().canonical("Some Random FC") == "some random"


def test_alias_men_and_women_stay_distinct() -> None:
    t = default_aliases()
    assert t.canonical("Wolves Women") != t.canonical("Wolves")


def test_aliases_of_is_bidirectional() -> None:
    t = default_aliases()
    spurs = t.aliases_of("Spurs")
    assert normalize_name("Tottenham Hotspur") in spurs
    assert "tottenham" in spurs


# --- strict matching -------------------------------------------------------
def test_match_exact_same_fixture() -> None:
    m = match_event(
        "Alpha FC", "Beta United", KO, [_cand("1", "Alpha FC", "Beta United")], aliases=AliasTable()
    )
    assert m is not None and m.ref == "1"


def test_match_via_alias_table() -> None:
    m = match_event(
        "Man Utd",
        "Man City",
        KO,
        [_cand("1", "Manchester United", "Manchester City")],
        aliases=default_aliases(),
    )
    assert m is not None and m.ref == "1"


def test_no_match_for_different_teams() -> None:
    assert (
        match_event("Alpha", "Beta", KO, [_cand("1", "Gamma", "Delta")], aliases=AliasTable())
        is None
    )


def test_kickoff_drift_within_window_matches() -> None:
    m = match_event(
        "Alpha",
        "Beta",
        KO,
        [_cand("1", "Alpha", "Beta", KO + timedelta(days=1))],
        aliases=AliasTable(),
    )
    assert m is not None


def test_kickoff_drift_beyond_window_no_match() -> None:
    far = [_cand("1", "Alpha", "Beta", KO + timedelta(days=3))]
    assert match_event("Alpha", "Beta", KO, far, aliases=AliasTable(), max_day_drift=1) is None


# --- the cardinal-sin guards ----------------------------------------------
def test_home_away_swap_does_not_match_when_ordered() -> None:
    # soccer/NBA: home vs away is meaningful; a swapped orientation is NOT the
    # same bettable fixture.
    assert (
        match_event(
            "Alpha", "Beta", KO, [_cand("1", "Beta", "Alpha")], aliases=AliasTable(), ordered=True
        )
        is None
    )


def test_unordered_pair_matches_swap_for_tennis() -> None:
    # tennis: two players, no home/away meaning -> the swapped pair is the SAME match.
    m = match_event(
        "Medvedev",
        "Sinner",
        KO,
        [_cand("1", "Sinner", "Medvedev")],
        aliases=AliasTable(),
        ordered=False,
    )
    assert m is not None and m.ref == "1"


def test_duplicate_captures_match_nearest_kickoff() -> None:
    # Two archive captures of the SAME fixture (identical canonical teams) at
    # DIFFERENT kickoff times within the window are duplicates of one game (a
    # team plays once per day), NOT two distinct fixtures. The matcher picks the
    # capture nearest the pick's kickoff rather than rejecting — a wrong close
    # cannot result because both candidates ARE the same canonical fixture.
    pick_ko = datetime(2026, 6, 20, 12, 10, tzinfo=UTC)
    cands = [
        _cand("early", "Alpha", "Beta", datetime(2026, 6, 20, 10, 20, tzinfo=UTC)),
        _cand("exact", "Alpha", "Beta", datetime(2026, 6, 20, 12, 10, tzinfo=UTC)),
    ]
    m = match_event("Alpha", "Beta", pick_ko, cands, aliases=AliasTable())
    assert m is not None and m.ref == "exact"


def test_duplicate_captures_identical_kickoff_match_deterministically() -> None:
    # Exact duplicates (same teams + same kickoff) collapse to a single match,
    # chosen deterministically (lowest ref) — still the same fixture's close.
    cands = [_cand("b", "Alpha", "Beta"), _cand("a", "Alpha", "Beta")]
    m = match_event("Alpha", "Beta", KO, cands, aliases=AliasTable())
    assert m is not None and m.ref == "a"


def test_women_fixture_never_matches_mens() -> None:
    # "Wolves Women" vs "Arsenal Women" MUST NOT match the men's "Wolves"/"Arsenal".
    cands = [_cand("1", "Wolves", "Arsenal")]
    assert (
        match_event("Wolves Women", "Arsenal Women", KO, cands, aliases=default_aliases()) is None
    )


def test_empty_normalized_name_never_matches() -> None:
    # "FC" normalizes to "" -> cannot be a key -> no match (no false positive).
    assert match_event("FC", "Beta", KO, [_cand("1", "FC", "Beta")], aliases=AliasTable()) is None


def test_one_real_match_among_decoys_is_unique() -> None:
    cands = [
        _cand("decoy1", "Gamma", "Delta"),
        _cand("real", "Alpha", "Beta"),
        _cand("decoy2", "Alpha", "Zeta"),
    ]
    m = match_event("Alpha", "Beta", KO, cands, aliases=AliasTable())
    assert m is not None and m.ref == "real"


def test_unordered_same_name_pair_is_degenerate_none() -> None:
    # even unordered (tennis), a pair that canonicalizes to one name cannot be
    # oriented -> None (otherwise the re-key would mis-attribute a price).
    cands = [_cand("1", "Player One", "Player One")]
    assert (
        match_event("Player One", "Player One", KO, cands, aliases=AliasTable(), ordered=False)
        is None
    )


def test_seed_alias_canonicals_do_not_collide() -> None:
    # No two DISTINCT canonical seed entries may collapse to the same
    # normalize_name — a noise-token collision would conflate two real clubs.
    import json

    from app.resolution.matching import _SEED_PATH

    data = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    canonicals = [normalize_name(name) for name in data["teams"]]
    assert len(canonicals) == len(set(canonicals))


def test_seed_aliases_resolve_cross_source_name_variants() -> None:
    """Real OddsPortal-vs-Pinnacle fixture name variants surfaced by the shadow
    match-rate harness (scripts/reports/resolution_match_rate.py) must
    canonicalize equal — else a true fixture goes unmatched and its sharp close
    is lost. Each pair is a VERIFIED same fixture (same opponent + kickoff)."""
    table = AliasTable.from_seed()
    pairs = [
        ("Bosnia & Herzegovina", "Bosnia and Herzegovina"),  # & vs and
        ("Maghreb Fez", "Maghreb Fes"),  # transliteration
        ("Difaa El Jadidi", "Difaa El Jadida"),  # transliteration
        ("Landvetter", "Landvetter IS"),  # club suffix
        ("FC Gareji Sagarejo", "Gareji"),  # long vs short name
        ("AS Monaco", "Monaco"),  # AS prefix (basketball LNB)
        # 2026-06-18 shadow match-rate audit: each verified same fixture
        # (same opponent + identical kickoff) from the live archive.
        ("D.R. Congo", "DR Congo"),  # punctuation split (D.R. vs DR)
        ("UMF Grindavik", "Grindavik"),  # club prefix (Icelandic UMF)
        ("Kolkheti 1913", "Kolkheti 1913 Poti"),  # city suffix
        ("Odishi 1919", "Odishi 1919 Zugdidi"),  # city suffix
        ("Franke", "IK Franke"),  # club prefix (Swedish IK)
        ("Bulleen", "Bulleen Lions"),  # nickname suffix
        ("Macae", "Macae Esporte RJ"),  # short vs full name
        ("San German", "Atleticos de San German"),  # club prefix (BSN basketball)
    ]
    for oddsportal_name, pinnacle_name in pairs:
        assert table.canonical(oddsportal_name) == table.canonical(pinnacle_name), (
            oddsportal_name,
            pinnacle_name,
        )
