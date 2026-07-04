"""GENERATED regression skeleton for the 2026-07-04 alias batch (review before
moving into tests/). Locks BOTH directions of the wrong-game-safety contract:
the vetted pair now strict-matches, and the alias NEVER crosses a
women/youth/reserve marker. Apply the seed patch FIRST, then run the
wrong-game audit (0 new merges) and
`uv run pytest tests/test_alias_batch_2026_07_04.py -q`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.resolution import EventCandidate, default_aliases, match_event, match_event_hardened

# (feed form, pinnacle/canonical form, real opponent from the observed fixture)
_BATCH: list[tuple[str, str, str]] = [
    ("Drogheda", "Drogheda United", "Shelbourne"),
    ("HJK", "HJK Helsinki", "Mariehamn"),
    ("Manly Utd", "Manly United", "St George Saints"),
    ("Neman", "Neman Grodno", "Dnepr Mogilev"),
    # Monitor round same day (AC-0093/0094): 2 exact co-occurrences each.
    ("Hornsby S.", "Hornsby Spiders", "Hills Hornets"),
    ("Maitland M.", "Maitland Mustangs", "Central Coast Crusaders"),
    # Monitor round 2 (AC-0095): 2 exact co-occurrences.
    ("Def. de Cambaceres", "Defensores de Cambaceres", "Argentino de Rosario"),
    # Addressable-gap batch (alias_candidates_addressable_2026-07-04.csv):
    # NBL1 city-name -> full-nickname (women's sides carry W, marker-vetoed).
    ("Southern District", "Southern Districts Spartans", "Ipswich Force"),
    ("Ballarat", "Ballarat Miners", "Geelong"),
    ("Dandenong", "Dandenong Rangers", "Frankston"),
    ("Diamond Valley", "Diamond Valley Eagles", "Casey Cavaliers"),
    ("Eltham", "Eltham Wildcats", "Keilor Thunder"),
    ("Mt Gambier", "MT Gambier Pioneers", "Kilsyth"),
    # pinnacle carries two spellings — both unify at ONE canonical (AC-0019).
    ("Mount Gambier Pioneers", "MT Gambier Pioneers", "Nunawading"),
    ("Nunawading", "Nunawading Spectres", "Ringwood"),
    ("Waverley", "Waverley Falcons", "Knox"),
    ("Kalamunda Eastern Suns", "Eastern Suns", "Geraldton Buccaneers"),
    # Soccer per-club abbreviations/short forms (namespace-scanned, unique).
    ("Akranes", "IA Akranes", "KR Reykjavik"),
    ("Nyiregyhaza", "Nyiregyhaza Spartacus", "Brno"),
    ("Univ. Craiova", "Universitatea Craiova", "Sabah Baku"),
    ("Valeriodoce", "Valeriodoce EC", "Aymores"),
    ("Dianella White Eagle", "Dianella White Eagles", "Sorrento"),
    # Merged into the EXISTING canonical entry (split-canonical trap avoided).
    ("St. Patricks", "St Patrick's Athletic F.C.", "Sligo Rovers"),
    ("Laferrere", "Deportivo Laferrere", "Defensores Unidos"),
    ("America MG", "America Mineiro", "Criciuma"),
    ("Novorizontino", "Gremio Novorizontino", "Ponte Preta"),
    ("San Martin Mendoza", "San Martin de Mendoza", "Huracan Las Heras"),
    # First-team mapping also recovers 'Sporting Jax 2' vs '... II' (equal
    # reserve markers; matcher aliases marker-STRIPPED bases).
    ("Sporting Jax", "Sporting Club Jacksonville", "Loudoun"),
    ("Springvale", "Springvale White Eagles", "Eastern Lions"),
    ("Naftan", "Naftan Novopolotsk", "Baranovici"),
    # Tennis compound-surname truncations (same trailing initial, unique
    # surname on tour; Pinnacle keeps the LAST surname only).
    ("budkovkjaer n", "kjaer n", "virtanen o"),
    ("davidovichfokina a", "fokina a", "cerundolo j m"),
    ("deminaur a", "minaur a", "burruchaga r a"),
    ("martintiffon p", "tiffon p", "bailly g a"),
    ("vandezandschulp b", "zandschulp b", "kovacevic a"),
    ("bouzasmaneiro j", "maneiro j", "potapova a"),
    ("sorribestormo s", "tormo s", "pegula j"),
]

_KO = datetime(2026, 7, 2, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(("feed", "pinnacle", "opponent"), _BATCH)
def test_alias_fixes_the_match(feed: str, pinnacle: str, opponent: str) -> None:
    aliases = default_aliases()
    assert aliases.canonical(feed) == aliases.canonical(pinnacle)
    cand = EventCandidate(ref="x", home=pinnacle, away=opponent, kickoff=_KO)
    assert match_event(feed, opponent, _KO, [cand], aliases=aliases) is cand


@pytest.mark.parametrize("marker", ["Women", "W", "U19", "U20", "II", "B", "Reserves"])
@pytest.mark.parametrize(("feed", "pinnacle", "opponent"), _BATCH)
def test_alias_never_crosses_a_marker(feed: str, pinnacle: str, opponent: str, marker: str) -> None:
    aliases = default_aliases()
    cand = EventCandidate(ref="x", home=f"{pinnacle} {marker}", away=opponent, kickoff=_KO)
    assert match_event_hardened(feed, opponent, _KO, [cand], aliases=aliases, ordered=True) is None
