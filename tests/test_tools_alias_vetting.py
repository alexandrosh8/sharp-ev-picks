"""Unit tests for the alias-vetting workflow (tools/alias_vetting.py).

Pure — synthetic fixtures only, no network, no DB. Covers the three contract
surfaces the task mandates: risk-flag computation, the approve-filter (flagged
approvals REQUIRE reviewer notes), and seed-patch generation with the
wrong-game guards (marker-crossing + collision refusals — CD-Nacional class).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.resolution import AliasTable, normalize_name
from tools.alias_vetting import (
    AliasCandidate,
    ArchiveEvent,
    PickFixture,
    apply_additions,
    attach_risk_flags,
    build_alias_additions,
    build_token_league_map,
    candidates_to_rows,
    compute_risk_flags,
    extract_alias_candidates,
    load_review_csv,
    parse_ts,
    render_rejected_suggestions,
    render_seed,
    render_test_skeleton,
    split_decisions,
    unified_seed_diff,
    write_review_csv,
)

KO = datetime(2026, 7, 2, 18, 0, tzinfo=UTC)


def _table(mapping: dict[str, str] | None = None) -> AliasTable:
    return AliasTable(mapping or {})


# --- risk flags ---------------------------------------------------------------


def test_flag_women_men_conflict() -> None:
    flags = compute_risk_flags("Arsenal Women", "Arsenal", 0.95)
    assert "women_men_conflict" in flags


def test_flag_youth_senior_conflict() -> None:
    flags = compute_risk_flags("Ajax U19", "Ajax", 0.95)
    assert "youth_senior_conflict" in flags


def test_flag_reserve_b_team_conflict() -> None:
    flags = compute_risk_flags("Real Madrid B", "Real Madrid", 0.95)
    assert "reserve_b_team_conflict" in flags


def test_flag_token_order_only() -> None:
    flags = compute_risk_flags("Madrid Real Sociedad", "Real Sociedad Madrid", 0.95)
    assert "token_order_only" in flags


def test_flag_weak_similarity_below_090() -> None:
    assert "weak_similarity" in compute_risk_flags("Alpha", "Beta", 0.89)
    assert "weak_similarity" not in compute_risk_flags("Alpha", "Alphas", 0.95)


def test_flag_city_club_ambiguity_strict_token_subset() -> None:
    flags = compute_risk_flags("Kimberley", "Kimberley Mar del Plata", 0.9)
    assert "city_club_ambiguity" in flags
    # equal token sets are NOT a subset conflict
    assert "city_club_ambiguity" not in compute_risk_flags("Gjovik Lyn", "Gjovik-Lyn", 0.99)


def test_flag_known_false_pattern_seed_pair_both_orders() -> None:
    a, b = "Western City Rangers", "Western Knights"
    assert "known_false_pattern" in compute_risk_flags(a, b, 0.9)
    assert "known_false_pattern" in compute_risk_flags(b, a, 0.9)


def test_flag_known_false_pattern_disambiguating_token_class() -> None:
    # the United/City class: base names differing ONLY by disambiguating tokens
    flags = compute_risk_flags("Bayswater City", "Bayswater United", 0.95)
    assert "known_false_pattern" in flags


def test_flag_same_country_common_name_uses_league_token_map() -> None:
    cands = [
        AliasCandidate("Nacional", "Nacional Madeira", "soccer", "pt_liga", "PT", 0.9, "x"),
        AliasCandidate("Nacional Asuncion", "Nacional", "soccer", "py_primera", "PY", 0.9, "x"),
    ]
    token_leagues = build_token_league_map(cands)
    flags = compute_risk_flags("Nacional", "Nacional Madeira", 0.95, token_leagues)
    assert "same_country_common_name" in flags
    # a single-league token does not fire
    single = build_token_league_map(cands[:1])
    assert "same_country_common_name" not in compute_risk_flags(
        "Nacional", "Nacional Madeira", 0.95, single
    )


def test_clean_pair_has_no_flags() -> None:
    # distinct multi-token names sharing tokens but not subset/marker/known-false
    flags = compute_risk_flags("Heidelberg Utd", "Heidelberg Unted", 0.97)
    assert flags == []


# --- extraction (probe-cascade replication) ------------------------------------


def _pick(home: str, away: str, ext: str = "ref-1") -> PickFixture:
    return PickFixture(
        pick_id=1,
        sport_key="soccer",
        league_key="au_npl",
        country="Australia",
        home=home,
        away=away,
        kickoff=KO,
        external_ref=ext,
    )


def test_extract_skips_matched_and_coverage_gap() -> None:
    aliases = _table()
    matched_pick = _pick("Alpha United", "Beta City")
    archive = [ArchiveEvent("pinnacle_soccer", "Alpha United", "Beta City", KO, "au_npl")]
    assert extract_alias_candidates([matched_pick], archive, aliases=aliases) == []
    # coverage gap: no archive events at all -> no candidate rows
    assert extract_alias_candidates([matched_pick], [], aliases=aliases) == []


def test_extract_emits_single_side_nearmiss_pair() -> None:
    # "West Torrens" vs "West Torrens Birkalla": JW 0.914 sits in the REVIEW band
    # (<0.92) so the hardened cascade REJECTS it — the alias-addressable slice.
    aliases = _table()
    pick = _pick("Alpha United", "West Torrens")
    archive = [
        ArchiveEvent("pinnacle_soccer", "Alpha United", "West Torrens Birkalla", KO, "au_npl")
    ]
    cands = extract_alias_candidates([pick], archive, aliases=aliases)
    assert len(cands) == 1
    cand = cands[0]
    assert cand.raw_name_a == "West Torrens"
    assert cand.raw_name_b == "West Torrens Birkalla"
    assert cand.reason == "single_side_nearmiss"
    assert cand.sample_event_count == 1
    assert cand.example_events == [f"Alpha United vs West Torrens @ {KO.isoformat()}"]
    assert cand.suggested_alias_key == "West Torrens Birkalla"


def test_extract_aggregates_duplicate_pairs_and_caps_examples() -> None:
    aliases = _table()
    # opponents must be fully distinct (no shared tokens, no trailing digit —
    # a trailing bare "2"/"3" is a RESERVE marker) so each pick pairs only with
    # its own counterpart fixture.
    rivals = ["Eagle Harbour", "Falcon Ridge", "Hawk Valley"]
    picks = [
        PickFixture(i, "soccer", "au_npl", "AU", rival, "West Torrens", KO, f"r{i}")
        for i, rival in enumerate(rivals, start=1)
    ]
    archive = [
        ArchiveEvent("pinnacle_soccer", rival, "West Torrens Birkalla", KO, "au_npl")
        for rival in rivals
    ]
    cands = extract_alias_candidates(picks, archive, aliases=aliases)
    assert len(cands) == 1
    assert cands[0].sample_event_count == 3
    assert len(cands[0].example_events) == 2  # capped at 2


def test_extract_never_pairs_across_a_marker() -> None:
    aliases = _table()
    pick = _pick("Alpha United", "West Torrens")
    archive = [
        ArchiveEvent("pinnacle_soccer", "Alpha United", "West Torrens Birkalla Women", KO, "au_npl")
    ]
    assert extract_alias_candidates([pick], archive, aliases=aliases) == []


# A women's pick whose OddsPortal URL slug DROPS the "W" marker. Production's
# live cascade (app/storage/repositories.py) refuses a slug that loses a marker
# the display name carries (display_markers <= slug_markers); the harness
# cascade must mirror that guard or the marker-less slug strict-matches the
# MEN'S archive event — a women->men pseudo-merge production provably blocks.
_SLUG_LOSES_W_REF = (
    "https://www.oddsportal.com/football/australia/npl-w/"
    "alpha-united-a1b2c3d4/west-torrens-e5f6a7b8/"
)


def _womens_pick(ext: str) -> PickFixture:
    return PickFixture(
        pick_id=1,
        sport_key="soccer",
        league_key="au_npl_w",
        country="Australia",
        home="Alpha United W",
        away="West Torrens W",
        kickoff=KO,
        external_ref=ext,
    )


def test_slug_marker_loss_never_pseudo_matches_mens_event() -> None:
    # Only the men's event exists: the marker-crossing candidate is categorically
    # NOT alias-addressable (production's marker veto) — no pairs, ever.
    aliases = _table()
    mens_only = [ArchiveEvent("pinnacle_soccer", "Alpha United", "West Torrens", KO, "au_npl")]
    assert (
        extract_alias_candidates([_womens_pick(_SLUG_LOSES_W_REF)], mens_only, aliases=aliases)
        == []
    )


def test_slug_marker_loss_does_not_swallow_genuine_nameform_pair() -> None:
    # BUG (2026-07-04): without production's slug marker-loss guard, the
    # marker-less slug strict-matched the MEN'S decoy event, recording the
    # fixture MATCHED and swallowing the genuine women's NAME-FORM candidate.
    # With the guard, the slug is refused and the women's near-miss pair
    # ("West Torrens W" -> "West Torrens Birkalla W") is emitted, identical to
    # the no-slug control below.
    aliases = _table()
    archive = [
        ArchiveEvent("pinnacle_soccer", "Alpha United", "West Torrens", KO, "au_npl"),
        ArchiveEvent(
            "pinnacle_soccer", "Alpha United W", "West Torrens Birkalla W", KO, "au_npl_w"
        ),
    ]
    with_slug = extract_alias_candidates(
        [_womens_pick(_SLUG_LOSES_W_REF)], archive, aliases=aliases
    )
    control = extract_alias_candidates([_womens_pick("opaque-ref")], archive, aliases=aliases)
    pairs = {(c.raw_name_a, c.raw_name_b, c.reason) for c in with_slug}
    assert ("West Torrens W", "West Torrens Birkalla W", "single_side_nearmiss") in pairs
    # decision-identity with the no-slug control: the marker-losing slug must
    # change NOTHING about what the classifier emits
    assert pairs == {(c.raw_name_a, c.raw_name_b, c.reason) for c in control}


def test_slug_retaining_markers_still_matches() -> None:
    # The guard refuses only marker-LOSING slugs: a marker-free pick with a
    # marker-free slug (display_markers <= slug_markers trivially) still uses
    # the slug fallback to strict-match its counterpart.
    aliases = _table()
    pick = PickFixture(
        pick_id=2,
        sport_key="soccer",
        league_key="au_npl",
        country="Australia",
        home="Alpha Utd.",  # display form that strict-fails vs the archive
        away="West Torrens",
        kickoff=KO,
        external_ref=(
            "https://www.oddsportal.com/football/australia/npl/"
            "alpha-united-a1b2c3d4/west-torrens-e5f6a7b8/"
        ),
    )
    archive = [ArchiveEvent("pinnacle_soccer", "Alpha United", "West Torrens", KO, "au_npl")]
    # slug matches -> fixture is MATCHED -> nothing alias-addressable emitted
    assert extract_alias_candidates([pick], archive, aliases=aliases) == []


def test_candidates_to_rows_review_shape_decision_blank() -> None:
    cand = AliasCandidate("A Utd.", "A United", "soccer", "lg", "AU", 0.97, "single_side_nearmiss")
    attach_risk_flags([cand])
    rows = candidates_to_rows([cand])
    assert rows[0]["candidate_id"] == "AC-0001"
    assert rows[0]["source_b"] == "pinnacle_soccer"
    assert rows[0]["human_decision"] == ""
    assert rows[0]["reviewer_notes"] == ""


# --- approve filter -------------------------------------------------------------


def _row(cid: str, decision: str, flags: str = "", notes: str = "") -> dict[str, str]:
    return {
        "candidate_id": cid,
        "source_a": "oddsportal",
        "raw_name_a": "Broadbeach Utd.",
        "source_b": "pinnacle_soccer",
        "raw_name_b": "Broadbeach United",
        "sport": "soccer",
        "league": "au_npl",
        "country": "Australia",
        "confidence": "0.9700",
        "reason": "single_side_nearmiss",
        "sample_event_count": "1",
        "example_events": f"Alpha United vs Broadbeach Utd. @ {KO.isoformat()}",
        "suggested_alias_key": "Broadbeach United",
        "risk_flags": flags,
        "human_decision": decision,
        "reviewer_notes": notes,
    }


def test_split_decisions_only_approve_passes() -> None:
    rows = [_row("AC-0001", ""), _row("AC-0002", "reject"), _row("AC-0003", "approve")]
    split = split_decisions(rows)
    assert [r["candidate_id"] for r in split.approved] == ["AC-0003"]
    assert len(split.skipped) == 2
    assert split.rejected_missing_notes == []


def test_split_decisions_flagged_approve_requires_notes() -> None:
    no_notes = _row("AC-0001", "approve", flags="city_club_ambiguity")
    with_notes = _row("AC-0002", "approve", flags="city_club_ambiguity", notes="verified same club")
    split = split_decisions([no_notes, with_notes])
    assert [r["candidate_id"] for r in split.rejected_missing_notes] == ["AC-0001"]
    assert [r["candidate_id"] for r in split.approved] == ["AC-0002"]


def test_csv_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_review_csv([_row("AC-0001", "approve")], path)
    loaded = load_review_csv(path)
    assert loaded[0]["candidate_id"] == "AC-0001"
    assert loaded[0]["human_decision"] == "approve"


# --- patch generation + wrong-game guards ---------------------------------------


def test_build_additions_happy_path() -> None:
    additions, errors = build_alias_additions([_row("AC-0001", "approve")], _table())
    assert errors == []
    assert len(additions) == 1
    assert additions[0].canonical == "Broadbeach United"
    assert additions[0].alias == "Broadbeach Utd."


def test_build_additions_refuses_marker_crossing() -> None:
    row = _row("AC-0001", "approve")
    row["raw_name_a"] = "Broadbeach United Women"
    additions, errors = build_alias_additions([row], _table())
    assert additions == []
    assert any("MARKER-CROSSING" in e for e in errors)


def test_build_additions_refuses_collision_with_existing_club() -> None:
    # seed already maps "Broadbeach Utd." to a DIFFERENT club
    seed_table = _table({"Broadbeach Utd.": "Some Other Club"})
    additions, errors = build_alias_additions([_row("AC-0001", "approve")], seed_table)
    assert additions == []
    assert any("COLLISION" in e for e in errors)


def test_build_additions_refuses_batch_conflict() -> None:
    row_a = _row("AC-0001", "approve")
    row_b = _row("AC-0002", "approve")
    row_b["suggested_alias_key"] = "Broadbeach City"
    row_b["raw_name_b"] = "Broadbeach City"
    additions, errors = build_alias_additions([row_a, row_b], _table())
    # AC-0001's alias lands; AC-0002 tries to claim the SAME alias for another
    # canonical -> refused (its raw_name_b equals its canonical, so no addition).
    assert [a.candidate_id for a in additions] == ["AC-0001"]
    assert any("BATCH CONFLICT" in e for e in errors)


def test_apply_additions_appends_and_creates_entries() -> None:
    seed: dict[str, object] = {"_comment": "c", "teams": {"Existing Club": ["EC"]}}
    additions, errors = build_alias_additions([_row("AC-0001", "approve")], _table())
    assert errors == []
    new_data = apply_additions(seed, additions)
    teams = new_data["teams"]
    assert isinstance(teams, dict)
    assert teams["Existing Club"] == ["EC"]  # untouched
    assert teams["Broadbeach United"] == ["Broadbeach Utd."]
    # input NOT mutated
    original_teams = seed["teams"]
    assert isinstance(original_teams, dict)
    assert "Broadbeach United" not in original_teams
    # merged table actually canonicalizes the pair together
    merged = AliasTable()
    for canon, alias_list in teams.items():
        for alias in [canon, *alias_list]:
            merged.add(alias, canon)
    assert merged.canonical("Broadbeach Utd.") == merged.canonical("Broadbeach United")
    assert merged.canonical("Broadbeach Utd.") == normalize_name("Broadbeach United")


def test_unified_diff_and_render_are_patch_shaped() -> None:
    seed: dict[str, object] = {"teams": {"Existing Club": ["EC"]}}
    additions, _ = build_alias_additions([_row("AC-0001", "approve")], _table())
    old_text = render_seed(seed)
    new_text = render_seed(apply_additions(seed, additions))
    diff = unified_seed_diff(old_text, new_text)
    assert diff.startswith("--- a/app/resolution/aliases_seed.json")
    assert '+      "Broadbeach Utd."' in diff
    assert json.loads(new_text)["teams"]["Broadbeach United"] == ["Broadbeach Utd."]


def test_render_test_skeleton_compiles_and_carries_pairs() -> None:
    src = render_test_skeleton([_row("AC-0001", "approve")], "2026-07-02")
    compile(src, "test_alias_batch_2026_07_02.py", "exec")  # valid python
    assert "('Broadbeach Utd.', 'Broadbeach United', 'Alpha United')" in src
    assert "test_alias_fixes_the_match" in src
    assert "test_alias_never_crosses_a_marker" in src


def test_render_rejected_suggestions_is_fully_commented() -> None:
    row = _row("AC-0009", "reject", flags="known_false_pattern", notes="distinct clubs")
    block = render_rejected_suggestions([row])
    assert all(line.startswith("#") for line in block.strip().splitlines())
    assert "'Broadbeach Utd.'" in block


def test_parse_ts_accepts_psql_and_iso_forms() -> None:
    assert parse_ts("2026-07-02 18:00:00+00") == KO
    assert parse_ts("2026-07-02T18:00:00+00:00") == KO
    naive = parse_ts("2026-07-02 18:00:00")
    assert naive.tzinfo is UTC
