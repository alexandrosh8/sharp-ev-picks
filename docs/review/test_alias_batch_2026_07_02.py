"""GENERATED regression skeleton for the 2026-07-02 alias batch (review before
moving into tests/). Locks BOTH directions of the wrong-game-safety contract:
the vetted pair now strict-matches, and the alias NEVER crosses a
women/youth/reserve marker. Apply the seed patch FIRST, then run the
wrong-game audit (0 new merges) and
`uv run pytest tests/test_alias_batch_2026_07_02.py -q`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.resolution import EventCandidate, default_aliases, match_event, match_event_hardened

# (feed form, pinnacle/canonical form, real opponent from the observed fixture)
_BATCH: list[tuple[str, str, str]] = [
    ('Bay Hawks', "Hawke's Bay Hawks", 'Taranaki Airs'),
    ('Tauranga Whai', 'Whai', 'Franklin Bulls'),
    ('Frankston', 'Frankston Blues', 'Ringwood'),
    ('Kilsyth', 'Kilsyth Cobras', 'Ringwood'),
    ('Knox', 'Knox Raiders', 'Frankston'),
    ('Ringwood', 'Ringwood Hawks', 'Kilsyth'),
    ('COD Meknes', 'CODM Meknes', 'FAR Rabat'),
    ('Dcheira', 'Olympique Dcheira', 'Olympique de Safi'),
    ('IR Tanger', 'Ittihad Tanger', 'Union Touarga'),
    ('Olympique de Safi', 'Olympic Safi', 'Dcheira'),
    ('Brno', 'Zbrojovka Brno', 'Tiszakecske'),
    ('FC Arges', 'Arges Pitesti', 'Vllaznia'),
    ('Grosuplje', 'Brinje Grosuplje', 'Din. Zagreb'),
    ('Petrolul', 'Petrolul Ploiesti', 'Chojniczanka'),
    ('Vllaznia', 'Vllaznia Shkoder', 'FC Arges'),
    ('A. Italiano', 'Audax Italiano', 'Palestino'),
    ('U. Catolica', 'Universidad Catolica', 'Everton'),
    ('U. De Chile', 'Universidad de Chile', 'Union La Calera'),
    ('U. De Concepcion', 'Universidad de Concepcion', 'Nublense'),
    ('Kopavogur', 'HK Kopavogur', 'Grotta'),
    ('Shanghai Second', 'Shanghai Segenda', 'Shanghai Port'),
    ('Dodoma Jiji', 'Dodoma', 'Azam'),
    ('Broadmeadow', 'Broadmeadow Magic', 'Weston Bears'),
    ('Weston Bears', 'Weston Workers', 'Broadmeadow'),
    ('Para', 'Para Hills Knights', 'NE Metrostars'),
    ('Al Riyadi Abbasiyah', 'Al Riyadi Abassiya', 'Racing'),
    ('Al Sahel', 'Shabab Al Sahel', 'Jwayya'),
    ('Jwayya', 'Jwaya', 'Al Sahel'),
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
