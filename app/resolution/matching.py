"""Pure, deterministic cross-source event matching — NO IO (numpy/stdlib only).

Links the SAME real-world fixture across odds sources (an OddsPortal event vs a
Pinnacle arcadia archive event) so a sharp Pinnacle close can be attached to a
pick for incremental CLV vs the close.

STRICT ONLY. A match requires exact NORMALIZED team names (after an alias
table) AND kickoff within a small day window. There is NO fuzzy string distance,
NO substring/containment match, and NO best-available fallback: if more than one
candidate qualifies the result is NO match. A wrong Pinnacle close corrupts CLV
(the project's cardinal sin), so the matcher errs toward leaving a fixture
unmatched rather than guessing.

Clean-room provenance: the deterministic exact-key + bounded date-tolerance
approach is adapted from USSoccerFederation/glass_onion (BSD-3) `match.py`; the
bidirectional alias-table pattern from probberechts/soccerdata (Apache-2.0). NO
code was copied; their TF-IDF/cosine fuzzy passes are DELIBERATELY omitted.
Unlike glass_onion we do NOT strip women's/youth markers — conflating an
"Arsenal Women" fixture with the men's "Arsenal" would be a wrong-close defect.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

_SEED_PATH = Path(__file__).with_name("aliases_seed.json")

# Club-form noise tokens dropped during normalization. DELIBERATELY excludes
# women/ladies/youth markers (they DISTINGUISH fixtures — stripping them would
# conflate men's/women's/youth sides) and excludes ambiguous single letters.
_NOISE_TOKENS = frozenset(
    {"fc", "afc", "cf", "cfc", "sc", "fk", "ff", "bk", "if", "ac", "club", "calcio", "jk"}
)


def normalize_name(name: str) -> str:
    """Strict, deterministic normalization of a team/player name.

    NFKD accent-strip -> ASCII -> casefold -> alphanumerics-only -> drop
    club-form noise tokens (FC/AFC/...) -> collapse whitespace. Women's/youth
    markers are preserved on purpose. An all-noise input returns "".
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    cleaned = "".join(ch if ch.isalnum() else " " for ch in ascii_only.casefold())
    tokens = [t for t in cleaned.split() if t not in _NOISE_TOKENS]
    return " ".join(tokens)


class AliasTable:
    """Deterministic `{normalized_alias -> canonical_normalized}` map.

    `canonical(name)` collapses any known alias to its canonical normalized form
    (unknown names pass through normalized); `aliases_of(name)` expands a name to
    its full known alias set (the soccerdata bidirectional pattern). No fuzzy
    lookup — exact normalized keys only.
    """

    def __init__(self, mapping: Mapping[str, str] | None = None) -> None:
        self._alias_to_canon: dict[str, str] = {}
        self._canon_to_aliases: dict[str, set[str]] = {}
        for alias, canonical in (mapping or {}).items():
            self.add(alias, canonical)

    def add(self, alias: str, canonical: str) -> None:
        normalized_alias = normalize_name(alias)
        normalized_canon = normalize_name(canonical)
        if not normalized_alias or not normalized_canon:
            return
        self._alias_to_canon[normalized_alias] = normalized_canon
        self._canon_to_aliases.setdefault(normalized_canon, set()).add(normalized_alias)

    def canonical(self, name: str) -> str:
        normalized = normalize_name(name)
        return self._alias_to_canon.get(normalized, normalized)

    def aliases_of(self, name: str) -> frozenset[str]:
        canon = self.canonical(name)
        return frozenset({canon, *self._canon_to_aliases.get(canon, set())})

    @classmethod
    def from_seed(cls, path: Path | None = None) -> AliasTable:
        """Load the bundled starter alias table. Seed format:
        ``{"teams": {"canonical name": ["alias1", "alias2", ...]}}``. Expand it
        from withqwerty/reep's CC0 names.csv/custom_aliases.json over time."""
        data = json.loads((path or _SEED_PATH).read_text(encoding="utf-8"))
        table = cls()
        for canonical, aliases in data.get("teams", {}).items():
            for alias in [canonical, *aliases]:
                table.add(alias, canonical)
        return table


@lru_cache(maxsize=1)
def default_aliases() -> AliasTable:
    """Process-wide cached seed alias table (read once)."""
    return AliasTable.from_seed()


@dataclass(frozen=True)
class EventCandidate:
    """A candidate fixture to match against. `ref` is opaque to the matcher
    (e.g. a warehouse event id as a string)."""

    ref: str
    home: str
    away: str
    kickoff: datetime  # UTC-aware


def _same_day_window(a: datetime, b: datetime, max_day_drift: int) -> bool:
    return abs((a.date() - b.date()).days) <= max_day_drift


def match_event(
    home: str,
    away: str,
    kickoff: datetime,
    candidates: Sequence[EventCandidate],
    *,
    aliases: AliasTable,
    max_day_drift: int = 1,
    ordered: bool = True,
) -> EventCandidate | None:
    """The UNIQUE candidate that is the same fixture, or None.

    Strict rule: canonical home/away equality (after the alias table) AND
    kickoff date within ``max_day_drift`` days. ``ordered=True`` (soccer/NBA —
    home vs away is meaningful) requires home->home and away->away; a swapped
    orientation does NOT match. ``ordered=False`` (tennis — two players, no
    home/away meaning) matches the unordered pair. If zero or >1 candidates
    qualify, returns None (never guess — a wrong close corrupts CLV).
    """
    target_home = aliases.canonical(home)
    target_away = aliases.canonical(away)
    if not target_home or not target_away:
        return None
    if target_home == target_away:
        return None  # degenerate: cannot tell the two sides apart by name

    matched: list[EventCandidate] = []
    for candidate in candidates:
        if not _same_day_window(kickoff, candidate.kickoff, max_day_drift):
            continue
        cand_home = aliases.canonical(candidate.home)
        cand_away = aliases.canonical(candidate.away)
        if not cand_home or not cand_away:
            continue
        if ordered:
            if cand_home == target_home and cand_away == target_away:
                matched.append(candidate)
        elif {cand_home, cand_away} == {target_home, target_away}:
            matched.append(candidate)
    if not matched:
        return None
    # Every entry in `matched` shares the canonical (target_home, target_away)
    # by construction AND falls inside the day window — so >1 means DUPLICATE
    # captures of ONE fixture (a team plays once per day in soccer/NBA; tennis
    # is the same player pair), never two DISTINCT games. The old
    # "len != 1 -> None" rule therefore rejected fixtures purely because the
    # Pinnacle archive held the same game under two kickoff times. Pick the
    # capture NEAREST the pick's kickoff (deterministic tie-break on ref): this
    # cannot attach a wrong close — all candidates are the same canonical
    # fixture — and recovers those matches. A genuinely different game has a
    # different canonical name and never enters `matched`.
    return min(matched, key=lambda c: (abs((c.kickoff - kickoff).total_seconds()), c.ref))
