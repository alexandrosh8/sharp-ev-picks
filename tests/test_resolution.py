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
    distinguishing_markers,
    marker_safe_slug_names,
    match_event,
    match_event_hardened,
    normalize_name,
    oddschecker_slug_names,
    oddsportal_slug_names,
    slug_names,
)


def test_oddsportal_slug_names_are_a_cleaner_match_key() -> None:
    # The OddsPortal match-URL slug drops the women-league "W" suffix the scraped
    # display name carries, and is consistently lowercased -> a cleaner FALLBACK
    # query than the display name. Strip the 8-char per-team OddsPortal id.
    ref = "https://www.oddsportal.com/basketball/h2h/los-angeles-sparks-Ia6UdBZF/new-york-liberty-h4iAv3Jl/#UVVsCGdR"
    assert oddsportal_slug_names(ref) == ("los angeles sparks", "new york liberty")
    # multi-word slug + accents already URL-stripped
    ref2 = "https://www.oddsportal.com/basketball/h2h/cangrejeros-de-santurce-Yi6Va0d4/indios-de-mayaguez-Y3WmYnZn/#x"
    assert oddsportal_slug_names(ref2) == ("cangrejeros de santurce", "indios de mayaguez")


def test_oddsportal_slug_names_none_for_non_oddsportal_refs() -> None:
    assert oddsportal_slug_names("1631993947") is None  # Pinnacle numeric ref
    assert oddsportal_slug_names("betfair:abc-123") is None
    assert oddsportal_slug_names("") is None


def test_marker_safe_slug_names_refuses_marker_losing_slugs() -> None:
    # The slug drops the women "W" suffix the display names carry: matching on
    # it would pseudo-merge the women's fixture onto the men's/senior archive
    # event (the wrong-game class the close-attach guard already refuses).
    # marker_safe_slug_names must return None, not the marker-less slug.
    ref = "https://www.oddsportal.com/basketball/h2h/los-angeles-sparks-Ia6UdBZF/new-york-liberty-h4iAv3Jl/#UVVsCGdR"
    assert marker_safe_slug_names(ref, "Los Angeles Sparks W", "New York Liberty W") is None
    # One-sided marker loss is enough to refuse.
    assert marker_safe_slug_names(ref, "Los Angeles Sparks W", "New York Liberty") is None


def test_marker_safe_slug_names_rejects_marker_shifted_between_sides() -> None:
    ref = "https://www.oddsportal.com/basketball/h2h/sparks-Ia6UdBZF/new-york-liberty-w-h4iAv3Jl/"
    assert marker_safe_slug_names(ref, "Sparks W", "New York Liberty") is None


def test_oddschecker_slug_names_parse_v_and_at_orientations() -> None:
    # ISSUE 1 fix: OddsChecker refs carry the matchup in a path segment
    # ("home-v-away" for soccer/tennis; "away-at-home" for US sports), unlike
    # OddsPortal's per-team-id URL. This was previously INERT (the slug tier
    # only fired on "oddsportal.com/" refs), starving Pinnacle-close recall.
    assert oddschecker_slug_names(
        "oddschecker:football/english/premier-league/arsenal-v-coventry/winner"
    ) == ("arsenal", "coventry")
    # matchup can be the last segment (no trailing /winner)
    assert oddschecker_slug_names(
        "oddschecker:tennis/wimbledon/ashlyn-krueger-v-marta-kostyuk"
    ) == ("ashlyn krueger", "marta kostyuk")
    # "-at-" is US "away at home" -> orientation flips to (home, away)
    assert oddschecker_slug_names(
        "oddschecker:american-football/nfl/carolina-panthers-at-arizona-cardinals/winner"
    ) == ("arizona cardinals", "carolina panthers")


def test_oddschecker_slug_names_none_for_numeric_and_foreign_refs() -> None:
    # Numeric subevent ids carry NO team names -> None (no regression; the slug
    # tier simply doesn't fire, same as a non-URL OddsPortal ref).
    assert oddschecker_slug_names("oddschecker:101657668") is None
    assert oddschecker_slug_names("oddschecker:football/some-league/standings") is None
    assert oddschecker_slug_names("1631993947") is None
    assert oddschecker_slug_names("https://www.oddsportal.com/x/h2h/a-b/c-d/") is None


def test_slug_names_dispatches_both_providers_oddsportal_byte_identical() -> None:
    # Dispatcher must be byte-identical to oddsportal_slug_names on OddsPortal
    # refs (differential-fuzz decision-identity) AND newly parse OddsChecker.
    op = "https://www.oddsportal.com/basketball/h2h/los-angeles-sparks-Ia6UdBZF/new-york-liberty-h4iAv3Jl/#x"
    assert slug_names(op) == oddsportal_slug_names(op)
    assert slug_names(op) == ("los angeles sparks", "new york liberty")
    assert slug_names("oddschecker:football/english/premier-league/arsenal-v-coventry/winner") == (
        "arsenal",
        "coventry",
    )
    assert slug_names("oddschecker:101657668") is None
    assert slug_names("1631993947") is None


def test_marker_safe_slug_names_refuses_oddschecker_marker_loss() -> None:
    # The marker veto must protect OddsChecker refs too: a women's fixture whose
    # slug drops the "W" must never attach the marker-less senior close.
    ref = "oddschecker:basketball/wnba/las-vegas-aces-v-new-york-liberty/winner"
    assert marker_safe_slug_names(ref, "Las Vegas Aces W", "New York Liberty W") is None
    # A marker-preserving OddsChecker slug is returned normally.
    assert marker_safe_slug_names(ref, "Las Vegas Aces", "New York Liberty") == (
        "las vegas aces",
        "new york liberty",
    )


def test_marker_safe_slug_names_passes_marker_free_and_marker_retaining_slugs() -> None:
    # Marker-free display names: the slug is a cleaner key, hand it through.
    ref = "https://www.oddsportal.com/basketball/h2h/cangrejeros-de-santurce-Yi6Va0d4/indios-de-mayaguez-Y3WmYnZn/#x"
    assert marker_safe_slug_names(ref, "Cangrejeros de Santurce", "Indios de Mayaguez") == (
        "cangrejeros de santurce",
        "indios de mayaguez",
    )
    # Slug RETAINS the marker the display carries: safe, hand it through.
    ref_w = "https://www.oddsportal.com/basketball/h2h/sparks-w-Ia6UdBZF/liberty-w-h4iAv3Jl/#z"
    assert marker_safe_slug_names(ref_w, "Sparks W", "Liberty W") == ("sparks w", "liberty w")
    # Non-OddsPortal refs stay None regardless of markers.
    assert marker_safe_slug_names("1631993947", "Arsenal", "Chelsea") is None


def test_distinguishing_markers_flag_women_youth_reserve() -> None:
    # Women/youth/reserve markers DISTINGUISH a fixture. The slug-fallback guard
    # uses these to REFUSE matching a women's/youth pick onto the men's/senior
    # game when the URL slug has dropped the marker (the wrong-game CLV defect).
    assert distinguishing_markers("Lanus W") == frozenset({"women"})
    assert distinguishing_markers("Arsenal Women") == frozenset({"women"})
    assert distinguishing_markers("Boca Juniors U20") == frozenset({"youth"})
    assert distinguishing_markers("Brasiliense Sub20") == frozenset({"youth"})
    assert distinguishing_markers("Spartak Reserves") == frozenset({"reserve"})
    # plain senior/men names carry NO marker
    assert distinguishing_markers("Lanus") == frozenset()
    assert distinguishing_markers("Manchester United") == frozenset()
    # a bare digit / single letter is NOT a marker (too many false positives)
    assert distinguishing_markers("Bayer 04 Leverkusen") == frozenset()


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


def test_kickoff_drift_within_absolute_window_matches() -> None:
    m = match_event(
        "Alpha",
        "Beta",
        KO,
        [_cand("1", "Alpha", "Beta", KO + timedelta(hours=6))],
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


def test_repeated_fixture_with_different_kickoffs_is_ambiguous() -> None:
    # Exact team names are not proof of one event: doubleheaders and tournament
    # rematches can repeat within hours.  Divergent kickoffs fail closed.
    pick_ko = datetime(2026, 6, 20, 12, 10, tzinfo=UTC)
    cands = [
        _cand("early", "Alpha", "Beta", datetime(2026, 6, 20, 10, 20, tzinfo=UTC)),
        _cand("exact", "Alpha", "Beta", datetime(2026, 6, 20, 12, 10, tzinfo=UTC)),
    ]
    assert match_event("Alpha", "Beta", pick_ko, cands, aliases=AliasTable()) is None


def test_exact_fixture_outside_absolute_kickoff_window_does_not_match() -> None:
    candidate = _cand("later", "Alpha", "Beta", KO + timedelta(hours=7))
    assert match_event("Alpha", "Beta", KO, [candidate], aliases=AliasTable()) is None


def test_distinct_refs_with_identical_kickoff_are_ambiguous() -> None:
    # Equal published kickoffs are not a source identity: doubleheaders and
    # tournament legs can share them, so distinct refs must fail closed.
    cands = [_cand("b", "Alpha", "Beta"), _cand("a", "Alpha", "Beta")]
    assert match_event("Alpha", "Beta", KO, cands, aliases=AliasTable()) is None


def test_repeated_copy_of_same_ref_can_collapse() -> None:
    cands = [_cand("same", "Alpha", "Beta"), _cand("same", "Alpha", "Beta")]
    match = match_event("Alpha", "Beta", KO, cands, aliases=AliasTable())
    assert match is not None and match.ref == "same"


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
        # 2026-06-19 CLV-readiness audit: high-confidence soccer same-fixture gaps.
        ("Turkey", "Turkiye"),  # exonym vs endonym (country)
        ("Czech Republic", "Czechia"),  # long-form vs short-form (country)
        ("San Martin T.", "San Martin de San Juan"),  # surname-abbrev vs full club name
        ("San Martin S.J.", "San Martin de San Juan"),  # S.J. abbrev for the same club
        ("Colon Santa Fe", "Colon de Santa Fe"),  # missing "de" connector
    ]
    for oddsportal_name, pinnacle_name in pairs:
        assert table.canonical(oddsportal_name) == table.canonical(pinnacle_name), (
            oddsportal_name,
            pinnacle_name,
        )


def test_reep_imported_aliases_resolve_to_canonical() -> None:
    """A sample of the withqwerty/reep (CC0) club aliases imported by
    scripts/research/import_reep_soccer_aliases.py must canonicalize onto their
    seed canonical. Each is an EXACT (normalized) alias of a club we scrape — the
    import is bounded to scraped teams and the matcher stays exact-only."""
    table = AliasTable.from_seed()
    pairs = [
        ("Athletic Bilbao", "Athletic Club"),  # common name vs official name
        ("HJK Helsinki", "Helsingin Jalkapalloklubi"),  # acronym vs full Finnish name
        ("Athlone Town", "Athlone Town F.C."),  # club suffix
        ("Coritiba", "Coritiba F.C."),  # short vs F.C. form
        ("CABJ", "Boca Juniors"),  # acronym
        ("CA San Martín (San Juan)", "San Martin de San Juan"),  # reep accent variant
    ]
    for alias, canonical in pairs:
        assert table.canonical(alias) == table.canonical(canonical), (alias, canonical)


def test_reep_import_did_not_introduce_fuzzy_pairing() -> None:
    """The reep import is DATA-only: it must not have made the matcher fuzzy.
    A name that merely SHARES tokens with a seeded club but is a DIFFERENT club
    must NOT collapse onto it — only exact normalized aliases resolve. (Note:
    matcher equality is by NORMALIZED form; bare club-noise like "Athletic Club"
    -> "athletic" is the existing normalizer, not a reep alias — so we test on
    distinguishing tokens that survive normalization.)"""
    table = AliasTable.from_seed()
    # "Athletic Bilbao" is a seeded alias of Athletic Club; an UNSEEDED, clearly
    # different club sharing the token "Bilbao" must NOT snap onto it.
    assert table.canonical("Bilbao Athletic") != table.canonical("Athletic Bilbao")
    # An unseeded near-miss with an extra distinguishing token passes through
    # normalized, never substring-snapping onto the seeded club.
    assert table.canonical("Boca Juniors Reserve") == normalize_name("Boca Juniors Reserve")
    assert table.canonical("Boca Juniors Reserve") != table.canonical("Boca Juniors")
    # No alias resolves to two different canonicals (the import logs+skips such
    # collisions): every alias key maps to exactly one canonical by construction.
    table_alias_to_canon = table._alias_to_canon  # noqa: SLF001 (invariant check)
    assert len(table_alias_to_canon) == len(set(table_alias_to_canon))


def test_basketball_seed_bridges_real_pinnacle_vs_oddsportal_names() -> None:
    # Data-driven (2026-06-21 live DB): Pinnacle prices WNBA/club basketball
    # WITHOUT OddsPortal's "W" women-league suffix or sponsor/city tail, so the
    # sharp anchor failed to attach. WNBA names are gender-unique (no NBA team
    # shares them) -> bridging the "W" form cannot conflate men's/women's.
    table = AliasTable.from_seed()

    # WNBA "W" suffix (in-season) — Pinnacle name vs OddsPortal "<team> W"
    sparks = _cand("w1", "Los Angeles Sparks W", "Minnesota Lynx W")
    assert (
        match_event("Los Angeles Sparks", "Minnesota Lynx", KO, [sparks], aliases=table) is sparks
    )
    sky = _cand("w2", "Chicago Sky W", "New York Liberty W")
    assert match_event("Chicago Sky", "New York Liberty", KO, [sky], aliases=table) is sky

    # Sponsor / city tail
    legia = _cand("c1", "Legia Warszawa", "Zielona Gora")
    assert match_event("Legia", "Zielona Gora", KO, [legia], aliases=table) is legia
    ginebra = _cand("c2", "Barangay Ginebra San Miguel", "Fubon Braves")
    assert (
        match_event("Barangay Ginebra", "Taipei Fubon Braves", KO, [ginebra], aliases=table)
        is ginebra
    )


# --- ambiguity margin vs duplicate captures (hardened matcher) ---------------
def test_duplicate_capture_does_not_shield_ambiguity_margin() -> None:
    # Audit 2026-07-09: the margin guard compared the best only against
    # eligible[1]. When that slot holds a duplicate capture of the best fixture
    # (same canonical teams, minutes apart — the Pinnacle-archive shape the
    # duplicate-collapse exists for), a DISTINCT rival at eligible[2] within
    # _AMBIGUITY_MARGIN was never examined, so the accept/reject decision
    # flipped on the mere presence of a duplicate. The margin must be measured
    # against the first DISTINCT fixture wherever it ranks -> REJECT here.
    cands = [
        _cand("1", "Manchester United", "Liverpool"),
        _cand("2", "Manchester United", "Liverpool", KO + timedelta(minutes=5)),
        _cand("3", "Manchester Unitd", "Liverpool", KO + timedelta(minutes=10)),
    ]
    assert (
        match_event_hardened("Manchester United", "Liverpool", KO, cands, aliases=AliasTable())
        is None
    )
    # Same triple without the duplicate already rejects (the pre-fix behavior
    # the shield was flipping): the decision must be identical either way.
    assert (
        match_event_hardened(
            "Manchester United", "Liverpool", KO, [cands[0], cands[2]], aliases=AliasTable()
        )
        is None
    )


def test_duplicate_capture_without_distinct_rival_still_collapses() -> None:
    # RECALL guard: duplicate captures of ONE fixture (no distinct rival in the
    # set) are NOT ambiguous — they still collapse to the nearest-kickoff one.
    cands = [
        _cand("1", "Manchester United", "Liverpool"),
        _cand("2", "Manchester United", "Liverpool", KO + timedelta(minutes=5)),
    ]
    m = match_event_hardened("Manchester United", "Liverpool", KO, cands, aliases=AliasTable())
    assert m is not None and m.ref == "1"


# --- alias-conflict quarantine ------------------------------------------------
def test_alias_claimed_by_two_canonicals_is_quarantined() -> None:
    # An alias string claimed by two DIFFERENT canonicals is a conflict: silent
    # last-write-wins could hang a name on the wrong club (wrong-game risk).
    # The alias must resolve to NEITHER claimant (identity passthrough only)
    # and the conflict must be observable.
    table = AliasTable()
    table.add("Sporting X", "Alpha Town")
    table.add("Sporting X", "Beta Town")
    assert table.canonical("Sporting X") == normalize_name("Sporting X")
    assert table.conflicts == {"sporting x": frozenset({"alpha town", "beta town"})}
    # The stale reverse entry is dropped too: neither claimant expands to it.
    assert "sporting x" not in table.aliases_of("Alpha Town")
    assert "sporting x" not in table.aliases_of("Beta Town")


def test_alias_readd_to_same_canonical_is_not_a_conflict() -> None:
    table = AliasTable()
    table.add("Man Utd", "Manchester United")
    table.add("Man Utd", "Manchester United")
    assert table.canonical("Man Utd") == normalize_name("Manchester United")
    assert table.conflicts == {}


def test_conflicted_alias_never_resolves_again() -> None:
    # A later re-claim (even repeating an original claimant) must NOT resurrect
    # a quarantined alias — it stays identity and the claim is recorded.
    table = AliasTable()
    table.add("Sporting X", "Club Alpha")
    table.add("Sporting X", "Club Beta")
    table.add("Sporting X", "Club Alpha")
    assert table.canonical("Sporting X") == normalize_name("Sporting X")
    assert "sporting x" in table.conflicts


def test_seed_alias_conflicts_are_exactly_the_known_one() -> None:
    # Tripwire: any NEW cross-canonical alias claim added to the seed fails
    # here. The known conflict (the América Futebol Clube / America Mineiro
    # fork) is quarantined: the alias resolves to its own normalized form,
    # never to either claimant, instead of the old nondeterministic
    # last-write-wins. (The Drogheda United F.C. / Drogheda United fork was
    # MERGED into one group in the 2026-08-03 alias batch — same club.)
    table = AliasTable.from_seed()
    assert set(table.conflicts) == {"america mineiro"}
    assert table.canonical("America Mineiro") == "america mineiro"
    assert table.canonical("Drogheda United") == "drogheda united"
    assert table.canonical("Drogheda United F.C.") == "drogheda united"
