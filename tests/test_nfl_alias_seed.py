"""NFL 32-team alias block (2026-07-26 season prep) — seed validity, reviewed
short forms resolve, and the collision exclusions STAY excluded.

The block was imported through the sanctioned collision-checked pattern
(scripts/research/import_alias_datasets.build_import); these tests freeze the
review outcome so a later batch cannot silently loosen it.
"""

import json

from app.resolution.matching import _SEED_PATH, AliasTable, distinguishing_markers, normalize_name

_NFL_CANONICALS = [
    "Arizona Cardinals",
    "Atlanta Falcons",
    "Baltimore Ravens",
    "Buffalo Bills",
    "Carolina Panthers",
    "Chicago Bears",
    "Cincinnati Bengals",
    "Cleveland Browns",
    "Dallas Cowboys",
    "Denver Broncos",
    "Detroit Lions",
    "Green Bay Packers",
    "Houston Texans",
    "Indianapolis Colts",
    "Jacksonville Jaguars",
    "Kansas City Chiefs",
    "Las Vegas Raiders",
    "Los Angeles Chargers",
    "Los Angeles Rams",
    "Miami Dolphins",
    "Minnesota Vikings",
    "New England Patriots",
    "New Orleans Saints",
    "New York Giants",
    "New York Jets",
    "Philadelphia Eagles",
    "Pittsburgh Steelers",
    "San Francisco 49ers",
    "Seattle Seahawks",
    "Tampa Bay Buccaneers",
    "Tennessee Titans",
    "Washington Commanders",
]


def test_seed_json_is_valid_and_collision_free() -> None:
    data = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    teams = data["teams"]
    assert isinstance(teams, dict)
    # No two DISTINCT canonicals collapse to one normalized form (the
    # alias-collision trap — the project-wide invariant).
    canon_norms = [normalize_name(c) for c in teams]
    assert len(canon_norms) == len(set(canon_norms))
    # Every NFL surface form maps to exactly ONE canonical: no NFL alias may
    # shadow (or be shadowed by) any other seed entry's surface. (The global
    # seed predates this block and carries a few duplicate soccer surfaces
    # resolved at AliasTable build time — the NFL block must add none.)
    surface_owner: dict[str, list[str]] = {}
    for canonical, aliases in teams.items():
        for surface in [canonical, *aliases]:
            s_norm = normalize_name(surface)
            if s_norm:
                owners = surface_owner.setdefault(s_norm, [])
                if canonical not in owners:
                    owners.append(canonical)
    for canonical in _NFL_CANONICALS:
        for surface in [canonical, *teams[canonical]]:
            assert surface_owner[normalize_name(surface)] == [canonical], surface


def test_all_32_nfl_canonicals_are_seeded_marker_free() -> None:
    teams = json.loads(_SEED_PATH.read_text(encoding="utf-8"))["teams"]
    for canonical in _NFL_CANONICALS:
        assert canonical in teams, canonical
        assert distinguishing_markers(canonical) == set(), canonical
        for alias in teams[canonical]:
            assert distinguishing_markers(alias) == set(), alias


def test_reviewed_short_forms_resolve_to_pinnacle_long_forms() -> None:
    table = AliasTable.from_seed()
    pairs = [
        ("Chiefs", "Kansas City Chiefs"),
        ("Kansas City", "Kansas City Chiefs"),
        ("49ers", "San Francisco 49ers"),
        ("San Francisco", "San Francisco 49ers"),
        ("NY Giants", "New York Giants"),
        ("NY Jets", "New York Jets"),
        ("LA Rams", "Los Angeles Rams"),
        ("LA Chargers", "Los Angeles Chargers"),
        ("Packers", "Green Bay Packers"),
        ("Commanders", "Washington Commanders"),
        ("Tampa", "Tampa Bay Buccaneers"),
    ]
    for short, canonical in pairs:
        assert table.canonical(short) == table.canonical(canonical), (short, canonical)


def test_collision_exclusions_stay_excluded() -> None:
    """The review dropped these on purpose — they must never resolve to NFL."""
    table = AliasTable.from_seed()
    # "New England" belongs to soccer's New England FC (FC-noise strips to the
    # same normalized form) — NOT the Patriots.
    assert table.canonical("New England") != table.canonical("New England Patriots")
    # NBA-owned bare city forms stay with their NBA canonicals.
    assert table.canonical("Dallas") == table.canonical("Dallas Mavericks")
    assert table.canonical("Dallas") != table.canonical("Dallas Cowboys")
    assert table.canonical("Denver") == table.canonical("Denver Nuggets")
    assert table.canonical("Denver") != table.canonical("Denver Broncos")
    # Ambiguous two-team cities resolve to NEITHER NFL side.
    for bare in ("New York", "Los Angeles"):
        for canonical in (
            "New York Giants",
            "New York Jets",
            "Los Angeles Rams",
            "Los Angeles Chargers",
        ):
            assert table.canonical(bare) != table.canonical(canonical), (bare, canonical)
    # nflverse tricodes are deliberately NOT seeded (word-like collision
    # surface; the tricode bridge lives in scripts/sports/nfl_lines_provenance.py).
    for tricode, canonical in (("KC", "Kansas City Chiefs"), ("NO", "New Orleans Saints")):
        assert table.canonical(tricode) != table.canonical(canonical), tricode
