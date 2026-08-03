"""Golden tests for the fixture-confirmed NAME-FORM alias additions (2026-06-29).

The alias-coverage push (scripts/research/probe_unmatched_split.py) found ~75
unmatched cross-source fixtures whose counterparty DID exist but differed only by
name FORM (sponsor tail / club-type prefix / abbreviation / connector). The
vetted, UNAMBIGUOUS same-club pairs were added to the seed via
scripts/research/exotic_slate_aliases.py + import_alias_datasets.py.

This module locks in BOTH directions of the wrong-game-safety contract:

  POSITIVE  — every auto-added feed<->Pinnacle pair canonicalizes to the SAME
              club (so the strict matcher now links the fixture).
  NEGATIVE  — the new aliases NEVER cause a match across a women/youth/reserve
              marker or between two DISTINCT clubs, and the deliberately-OMITTED
              ambiguous pairs were NOT auto-added.

All pairs are real strings from the live instrument run (instrument.txt).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.resolution import (
    AliasTable,
    EventCandidate,
    default_aliases,
    match_event,
    match_event_hardened,
)

# Feed (OddsPortal display) -> Pinnacle-archive form. Both must canonicalize to
# the SAME normalized club after the alias merge (the strict-tier match).
_AUTO_ADDED: list[tuple[str, str]] = [
    ("Cangrejeros", "Cangrejeros de Santurce"),
    ("Pelita Jaya", "Pelita Jaya Jakarta"),
    ("Taranaki Airs", "Taranaki Mountainairs"),
    ("Berkane", "RS Berkane"),
    ("KAC Marrakech", "Kawkab Marrakech"),
    ("D. Puerto Montt", "Deportes Puerto Montt"),
    ("Puerto Montt", "Deportes Puerto Montt"),
    ("U. Espanola", "Union Espanola"),
    ("Binacional", "Deportivo Binacional"),
    ("Njardvik", "UMF Njardvik"),
    ("Arsenal Sarandi", "Arsenal de Sarandi"),
    ("Heidelberg Utd", "Heidelberg United"),
    ("Broadbeach Utd.", "Broadbeach United"),
    ("Cumberland Utd.", "Cumberland United"),
    ("West Torrens", "West Torrens Birkalla"),
    ("Gjoevik-Lyn", "SK Gjovik-Lyn"),
    ("Flint City", "Flint City Bucks"),
    ("Hudson Valley", "Hudson Valley Hammers"),
    ("Defensores de Vilelas", "Defensores de Puerto Vilelas"),
    ("Kimberley", "Kimberley Mar del Plata"),
    ("Sunnersta", "Sunnersta AIF"),
    ("Grobina", "Grobinas SC/LFS"),
    # 2026-07-01 Pinnacle-anchoring push (live-slate near-misses; unambiguous
    # single-club name-forms only — city/full-name suffix, one club worldwide).
    ("Xamax", "Neuchatel Xamax"),
    ("Flora", "Flora Tallinn"),
    ("Jihlava", "Vysocina Jihlava"),
    ("Helmond", "Helmond Sport"),
    ("Macva", "Macva Sabac"),
    ("Sutjeska", "Sutjeska Niksic"),
    ("Haukar", "Haukar Hafnarfjordur"),
    ("Atmosfera", "Atmosfera Mazeikiai"),
    ("Asane", "Asane Fotball"),
    ("Csikszereda", "Csikszereda M. Ciuc"),
    ("Limache", "Deportes Limache"),
]

# Deliberately NOT auto-added (ambiguous / wrong-game-unsafe) — these MUST stay
# DISTINCT after the merge (see the review/omit list in the run report).
_NOT_ADDED: list[tuple[str, str]] = [
    ("Racing", "Racing Beirut"),  # bare "Racing" — many distinct Racing clubs
    ("Everton", "Everton Vina del Mar"),  # collides with English Everton
    ("Fenix", "Club Atletico Fenix"),  # bare "Fenix" ambiguous
    ("Jazz Pori", "Jazz"),  # cross-sport Jazz (Utah Jazz) collision
    ("Gigantes San Francisco", "Indios de San Francisco de Macoris"),  # distinct clubs
    ("Bayswater", "Bayswater City"),  # bare + disambiguating "City"
    # 2026-07-03 escalation review: two pairs REMOVED from this list on
    # fixture evidence (operator-delegated review, docs/review/
    # alias_candidates_escalated_2026-07-03.csv) — they are now vetted aliases:
    #   Redlands/Redlands United (3 aligned QPL fixtures; one club),
    #   Playford Patriots/Playford City (3 aligned NPL-SA fixtures; club is
    #     "Playford City Patriots SC" — the entry was a stale rename guard).
    # 2026-07-01 push: deliberately EXCLUDED near-misses (distinct clubs / bare
    # ambiguous) — must stay distinct even though they shared a kickoff + a token.
    ("FC Kharkiv", "Metalist Kharkiv"),  # distinct Kharkiv clubs
    ("Zaglebie", "Zaglebie Lubin"),  # bare "Zaglebie" — Lubin vs Sosnowiec
    # 2026-07-03: Gremio Juventus/Juventus SC STAYS here despite 2 aligned
    # Catarinense-2 fixtures — normalize_name("Juventus SC") strips the "sc"
    # club-form token to bare "juventus", colliding with the "Juventus FC"
    # (Turin) canonical; a seed alias would anchor Gremio picks to Turin
    # closes. Needs league/country-scoped aliasing (same class as Racing).
    (
        "Gremio Juventus",
        "Juventus SC",
    ),  # "Juventus SC" normalizes to bare "juventus" = Turin collision
    ("Brevard SC", "Brevard Fire"),  # distinct Brevard clubs
    ("Zielona Gora", "Lechia Zielona Gora"),  # bare city name — ambiguous
    ("Galanta", "Slovan Galanta"),  # bare "Galanta" — disambiguating prefix
    # 2026-08-03 queue batch (docs/review/alias_candidates_queue_2026-08-03.csv)
    # — reviewed and REJECTED; must stay distinct forever:
    ("Atletico FC", "Atletico Madrid"),  # removed poison: bare 'atletico' unowned
    ("St Albans", "St. Albans City FC"),  # removed poison: bare form unowned (AUS/ENG clubs)
    ("Paysandu PA", "Paysandú F.C."),  # contested base: Paysandu SC (Belém) vs Paysandú FC (UY)
    ("Athletic Club MG", "Athletic Club"),  # bare 'athletic' = Athletic Bilbao's listed form
    ("Operario", "Operario Ferroviario"),  # bare 'operario' shared by -PR/-MS/-VG
    ("Americano Bacabal", "Americano"),  # bare 'americano' shared (Americano-RJ)
    ("Santa Cruz", "Santa Cruz PE"),  # bare shared (Santa Cruz de Natal-RN)
    ("Portuguesa", "Portuguesa SP"),  # bare shared (Portuguesa-RJ/Santista/Venezuela)
    ("Universidad Catolica", "Universidad Catolica del Ecuador"),  # bare = the Chilean club
    ("Rangers", "CSD Rangers"),  # bare 'rangers' = Rangers FC (Glasgow)
    ("Perth Azzurri", "Perth"),  # bare 'perth' contested (Glory/SC/RedStar)
    ("Sol de America", "Sol de America de Formosa"),  # bare = the Paraguayan club
    ("San Martin de San Juan", "San Martin de Tucuman"),  # distinct clubs, same prefix
    ("Cairns Marlins", "Cairns Dolphins"),  # men's vs women's NBL1 teams
    ("Canberra Gunners", "Canberra Nationals"),  # men's vs women's NBL1 teams
    ("Defensores Unidos", "Defensores de Cambaceres"),  # distinct clubs (Zárate/Ensenada)
    ("Argentino de Quilmes", "Argentinos Juniors"),  # distinct clubs
    ("Ferro", "Ferro Carril Oeste"),  # bare 'ferro' contested (BA vs General Pico)
    ("Neftchi", "Neftchi Baku"),  # bare shared (Neftchi Fergana)
    ("Europa", "CE Europa"),  # bare shared (Europa FC / Europa Point, Gibraltar)
    ("Shkendija", "Shkendija 79"),  # bare shared across Balkan Shkendija clubs
    ("Virtus", "SS Virtus"),  # bare shared (Italian Virtus clubs, cross-sport Bologna)
    ("Mornar", "Mornar Bar"),  # cross-sport clash (KK Mornar, ABA)
    ("Peninsula", "Peninsula Power"),  # bare contested (Peninsula Strikers, NPL VIC)
    ("Hurstville", "Hurstville Zagreb"),  # bare contested (Hurstville City Minotaurs)
    ("Shenzhen", "Shenzhen 2028"),  # bare contested (Shenzhen Peng City / Juniors)
]


@pytest.fixture(scope="module")
def aliases() -> AliasTable:
    return default_aliases()


@pytest.mark.parametrize(("feed", "pinnacle"), _AUTO_ADDED)
def test_nameform_alias_canonicalizes_same_club(
    aliases: AliasTable, feed: str, pinnacle: str
) -> None:
    """Each vetted pair resolves to ONE canonical club (the strict-tier link)."""
    assert aliases.canonical(feed) == aliases.canonical(pinnacle)
    assert aliases.canonical(feed)  # non-empty


@pytest.mark.parametrize(("feed", "pinnacle"), _AUTO_ADDED)
def test_nameform_alias_strict_match_event(aliases: AliasTable, feed: str, pinnacle: str) -> None:
    """The strict exact-on-alias matcher now links the fixture (shared opponent)."""
    ko = datetime(2026, 6, 29, 18, 0, tzinfo=UTC)
    cand = EventCandidate(ref="x", home=pinnacle, away="Opponent United", kickoff=ko)
    matched = match_event(feed, "Opponent United", ko, [cand], aliases=aliases)
    assert matched is cand


@pytest.mark.parametrize(("a", "b"), _NOT_ADDED)
def test_ambiguous_pairs_were_not_auto_added(aliases: AliasTable, a: str, b: str) -> None:
    """The OMITTED/REVIEW pairs stay DISTINCT — no ambiguous auto-merge."""
    assert aliases.canonical(a) != aliases.canonical(b)


@pytest.mark.parametrize(
    "marker_suffix",
    ["Women", "W", "Femenino", "U19", "U20", "Youth", "II", "B", "Reserves"],
)
@pytest.mark.parametrize(("feed", "pinnacle"), _AUTO_ADDED)
def test_nameform_alias_never_crosses_a_marker(
    aliases: AliasTable, feed: str, pinnacle: str, marker_suffix: str
) -> None:
    """A new alias must NEVER link a senior side to a women/youth/reserve side of
    the SAME club. The marker-bearing variant is a DIFFERENT fixture."""
    ko = datetime(2026, 6, 29, 18, 0, tzinfo=UTC)
    # pick = senior feed name; candidate = SAME club + a distinguishing marker.
    cand = EventCandidate(ref="x", home=f"{pinnacle} {marker_suffix}", away="Opponent", kickoff=ko)
    assert match_event_hardened(feed, "Opponent", ko, [cand], aliases=aliases, ordered=True) is None


def test_nameform_alias_never_merges_distinct_clubs(aliases: AliasTable) -> None:
    """Distinct clubs sharing a token / region must not merge via the new aliases."""
    ko = datetime(2026, 6, 29, 18, 0, tzinfo=UTC)
    distinct_pairs = [
        ("RS Berkane", "FUS Rabat"),  # two distinct Botola clubs
        ("Flint City Bucks", "Hudson Valley Hammers"),  # two USL2 clubs
        ("Everton", "Everton de Vina del Mar"),  # English vs Chilean Everton
        ("West Torrens Birkalla", "Western Knights"),  # distinct Australian clubs
        ("Heidelberg United", "Heidelberg Women"),  # marker
    ]
    for home, cand_home in distinct_pairs:
        cand = EventCandidate(ref="x", home=cand_home, away="Common Rival", kickoff=ko)
        assert (
            match_event_hardened(home, "Common Rival", ko, [cand], aliases=aliases, ordered=True)
            is None
        )
