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
import re
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


# Fixture-distinguishing markers. A women's / youth / reserve side is a DIFFERENT
# fixture from the men's / senior one. `normalize_name` deliberately KEEPS these
# tokens, but the OddsPortal URL slug DROPS them — so the slug-fallback matcher
# must refuse a slug match that loses a marker the display name carries, else a
# women's/youth pick attaches the men's/senior Pinnacle close (a wrong-game CLV
# defect). Bare digits / single non-"w" letters are NOT markers (false-positive
# risk: "Boca Juniors", "Bayer 04" are senior sides).
_WOMEN_MARKERS = frozenset(
    {
        "w",
        "women",
        "womens",
        "ladies",
        "fem",
        "femenino",
        "femenina",
        "feminin",
        "feminine",
        "feminino",
        "frauen",
        "damen",
        "dames",
        "kvinner",
        "kvinnor",
    }
)
_YOUTH_WORD_MARKERS = frozenset({"youth", "juvenil", "juvenis", "jugend"})
# Position-INDEPENDENT reserve tokens (roman ordinals + the explicit words). "ii"
# and "iii" name the 2nd/3rd team and never appear as a senior club's word.
_RESERVE_MARKERS = frozenset({"ii", "iii", "reserve", "reserves"})
# Position-DEPENDENT reserve tokens: a bare "2"/"3" (MLS Next Pro "Los Angeles
# FC 2", "Minnesota United 2") or a standalone "b" (European "Real Madrid B" =
# Castilla, "Barcelona B") marks a RESERVE side — but ONLY as the LAST token.
# Anchored to the trailing position so a club-FOUNDING-YEAR ("Schalke 04",
# "Bayer 04 Leverkusen") or a leading/mid "B" is never misfired. ERR TOWARD
# catching: a false reserve only costs recall (a REJECT); a MISSED one anchors a
# reserve onto the senior team's Pinnacle line — the wrong-game CLV defect.
_TRAILING_RESERVE_TOKENS = frozenset({"2", "3", "b"})
_YOUTH_AGE = re.compile(r"^(?:u|sub)(?:1[0-9]|2[0-3])$")  # u14..u23 / sub14..sub23


def distinguishing_markers(name: str) -> frozenset[str]:
    """Return the {'women','youth','reserve'} markers a name carries.

    Operates on the normalized token set. Used by the slug-fallback guard and the
    hardened matcher's veto to refuse a match that would conflate a women's/
    youth/reserve fixture with the men's/senior one. "junior(s)" is intentionally
    NOT a youth marker (it is part of senior club names like Boca/Argentinos
    Juniors); youth is detected via the age pattern (u20/sub20) and unambiguous
    words only. Reserve is detected from the position-independent roman/word
    tokens AND a TRAILING bare "2"/"3"/"B" (the ordinal-suffix reserve naming).
    """
    out: set[str] = set()
    tokens = normalize_name(name).split()
    for tok in tokens:
        if tok in _WOMEN_MARKERS:
            out.add("women")
        elif tok in _YOUTH_WORD_MARKERS or _YOUTH_AGE.match(tok):
            out.add("youth")
        elif tok in _RESERVE_MARKERS:
            out.add("reserve")
    # Trailing reserve ordinal: only the LAST token, and only when there is a real
    # club name in front of it (a bare "2"/"B" alone is not a reserve side).
    if len(tokens) >= 2 and tokens[-1] in _TRAILING_RESERVE_TOKENS:
        out.add("reserve")
    return frozenset(out)


# Known senior clubs whose name carries a marker-LIKE token that is NOT a
# women/youth/reserve marker (it is part of the SENIOR club's proper name). The
# marker veto consults this whitelist FIRST so "Boca Juniors"/"Young Boys"/
# "Argentinos Juniors" are never vetoed as youth sides. Keyed by normalized name.
_KNOWN_CLUB_WHITELIST: frozenset[str] = frozenset(
    {
        normalize_name(n)
        for n in (
            "Boca Juniors",
            "Argentinos Juniors",
            "Young Boys",
            "Defensa y Justicia",
            "Newells Old Boys",
            "Junior",  # Atletico Junior (Barranquilla)
        )
    }
)


def strip_markers(name: str) -> str:
    """Normalized name with women/youth/reserve marker tokens removed — the BASE
    club/team name used for fuzzy comparison. A whitelisted senior club is
    returned unchanged (its marker-like token is part of its real name), so its
    base name is never accidentally truncated."""
    norm = normalize_name(name)
    if norm in _KNOWN_CLUB_WHITELIST:
        return norm
    tokens = norm.split()
    # Drop a TRAILING bare reserve ordinal ("2"/"3"/"b") so the base club is what
    # remains ("real madrid b" -> "real madrid") — mirrors distinguishing_markers
    # so a reserve and its senior side share a base name (the marker veto, not the
    # base, is what keeps them apart).
    if len(tokens) >= 2 and tokens[-1] in _TRAILING_RESERVE_TOKENS:
        tokens = tokens[:-1]
    kept: list[str] = []
    for tok in tokens:
        if tok in _WOMEN_MARKERS or tok in _YOUTH_WORD_MARKERS or tok in _RESERVE_MARKERS:
            continue
        if _YOUTH_AGE.match(tok):
            continue
        kept.append(tok)
    return " ".join(kept) if kept else norm


def _jaro(s1: str, s2: str) -> float:
    """Jaro similarity in [0, 1] (stdlib-only; matches rapidfuzz.Jaro semantics).
    Two empty strings are identical (1.0); one empty is 0.0."""
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0
    match_distance = max(0, max(len1, len2) // 2 - 1)
    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    for i in range(len1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    # Count transpositions (half the out-of-order matched chars).
    k = 0
    transpositions = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    t = transpositions / 2.0
    m = float(matches)
    return (m / len1 + m / len2 + (m - t) / m) / 3.0


def jaro_winkler(s1: str, s2: str, *, prefix_weight: float = 0.1) -> float:
    """Jaro-Winkler similarity in [0, 1] (stdlib-only; rapidfuzz semantics with
    the standard 0.1 prefix weight, capped at a 4-char common prefix). Cohen/
    Ravikumar/Fienberg (KDD-2003) found JW best for short proper-noun matching;
    chosen on the accept path over token_set_ratio/WRatio (subset-trap merges)."""
    jaro = _jaro(s1, s2)
    prefix = 0
    for c1, c2 in zip(s1, s2, strict=False):
        if c1 != c2 or prefix == 4:
            break
        prefix += 1
    return jaro + prefix * prefix_weight * (1.0 - jaro)


def _ratio(s1: str, s2: str) -> float:
    """Indel (normalized Levenshtein) similarity ratio in [0, 100] — the
    rapidfuzz.fuzz.ratio basis, via difflib's longest-matching-block ratio
    scaled to 100."""
    if not s1 and not s2:
        return 100.0
    if not s1 or not s2:
        return 0.0
    from difflib import SequenceMatcher

    return SequenceMatcher(None, s1, s2).ratio() * 100.0


def token_sort_ratio(s1: str, s2: str) -> float:
    """rapidfuzz.fuzz.token_sort_ratio: sort whitespace tokens of each string,
    then take the indel ratio. Order-insensitive ("real madrid" == "madrid
    real" -> 100). EXCLUDED from the design: token_SET_ratio / WRatio /
    partial_ratio (the subset-100 trap that merges Man Utd / Man City)."""
    a = " ".join(sorted(s1.split()))
    b = " ".join(sorted(s2.split()))
    return _ratio(a, b)


# Disambiguating tokens: when two BASE names differ ONLY by one of these tokens
# (one has it, the other does not, or they carry DIFFERENT ones), they are
# DIFFERENT clubs and must never fuzzy-merge — the Man Utd/Man City, Real
# Madrid/Real Sociedad, Inter/AC class of false merge. The fuzzy accept path is
# vetoed whenever the symmetric token difference of the two base names is a
# NON-EMPTY subset of these tokens.
_DISAMBIGUATING_TOKENS: frozenset[str] = frozenset(
    {
        "united",
        "city",
        "town",
        "rovers",
        "athletic",
        "atletico",
        "wanderers",
        "albion",
        "county",
        "sociedad",
        "real",
        "inter",
        "internazionale",
        "sporting",
        "racing",
        "dynamo",
        "dinamo",
        "lokomotiv",
        "spartak",
        "zenit",
        "central",
        "nacional",
        "junior",
        "juniors",
        "north",
        "south",
        "east",
        "west",
        "b",
        "ii",
    }
)


_ODDSPORTAL_ID = re.compile(r"-[A-Za-z0-9]{8}$")


def oddsportal_slug_names(external_ref: str) -> tuple[str, str] | None:
    """Parse (home, away) from an OddsPortal match-URL external_ref, or None for a
    non-OddsPortal ref.

    The URL slug (``.../h2h/los-angeles-sparks-<id>/new-york-liberty-<id>/``) is
    often a CLEANER match key than the scraped display name: it drops the
    women-league "W" suffix the display carries and is consistently
    lowercased/hyphenated. The matcher tries it as a FALLBACK query when the
    display name fails — a wrong close still cannot result, since match_event
    requires a UNIQUE home+away+day candidate. Strips the trailing 8-char
    per-team OddsPortal id from each slug.
    """
    if "oddsportal.com/" not in external_ref:
        return None
    path = external_ref.split("oddsportal.com/", 1)[1].split("#", 1)[0]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    home = _ODDSPORTAL_ID.sub("", parts[-2]).replace("-", " ").strip()
    away = _ODDSPORTAL_ID.sub("", parts[-1]).replace("-", " ").strip()
    if not home or not away:
        return None
    return home, away


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


# --- precision-hardened SHADOW-path matcher ---------------------------------
# Two-tier accept thresholds (research brief 2026-06-23, DoorDash two-tier;
# NCES FCSM 2018 JW>=0.85 -> 985TP/1FP). ACCEPT requires BOTH a high Jaro-Winkler
# AND a high token-sort ratio on the marker-stripped BASE names; the REVIEW band
# is deliberately NOT auto-accepted (left for a later odds-vector confirm).
_JW_ACCEPT = 0.92
_TOKEN_SORT_ACCEPT = 90.0
_JW_REVIEW_FLOOR = 0.84
# Two surviving candidates whose summed (JW, token-sort) scores are within this
# margin are ambiguous -> REJECT (cannot tell which is the real fixture).
_AMBIGUITY_MARGIN = 0.04

# Tight kickoff ACCEPT bound (minutes), INDEPENDENT of the candidate-fetch window.
# A candidate-fetch window (``max_minute_drift``) is deliberately WIDE so ambiguity
# detection can see every same-teams leg of a series; acceptance is gated SEPARATELY
# at this tighter bound. Sized from the live cross-source kickoff-drift distribution
# (2026-06-24): for exact-name matches 94% drift <=90min, ~99% <=3h, and ZERO
# legitimate matches in the 24h-48h band — that band is two-leg / rematch series.
# 6h gives generous headroom for the date-only (00:00) archive row vs a real
# wall-clock pick and any timezone/rounding noise, while a same-teams fixture two
# days apart (the Gigantes/Cangrejeros BSN rematch, home/away swapped, 48h earlier)
# is decisively EXCLUDED. The research mandate (match-rate-lift-2026-06-23) is
# kickoff as a TIGHT predicate (+/-90min auto-accept, +/-3h review); 6h is the
# safe outer accept bound, never the +/-2-DAY the go-live flip wrongly passed in.
_ACCEPT_MINUTE_DRIFT = 360

# Max kickoff spread between two captures of the SAME game to treat them as
# DUPLICATE captures (collapse to nearest) rather than two DISTINCT same-teams
# games (reject as ambiguous). Sized from the live archive (2026-06-24): the
# largest observed same-date duplicate-capture spread is 255min (4.25h, AU semi-pro
# soccer kickoff revisions); 6h covers it with headroom. Two same-teams candidates
# BOTH inside the +/-6h accept window yet MORE than 6h apart from each other cannot
# both be the pick's game (a true doubleheader / back-to-back leg) -> REJECT.
_DUPLICATE_CAPTURE_SECONDS = _ACCEPT_MINUTE_DRIFT * 60


def _markers_conflict(home: str, away: str, cand_home: str, cand_away: str) -> bool:
    """True when the pick and candidate disagree on a women/youth/reserve marker
    on EITHER side (one present, the other absent, or different) — the
    categorical negative-rule veto. Present-vs-absent IS a conflict."""
    return distinguishing_markers(home) != distinguishing_markers(
        cand_home
    ) or distinguishing_markers(away) != distinguishing_markers(cand_away)


def _base_name_ok(a: str, b: str) -> bool:
    """Two-tier fuzzy accept on marker-stripped base names: exact canonical-base
    equality, OR (JW>=0.92 AND token_sort>=90) with NO disambiguating-token-only
    difference. Returns False in the REVIEW band and on a disambiguating diff."""
    if not a or not b:
        return False
    if a == b:
        return True
    # Disambiguating-token veto: if the two base names differ ONLY by tokens that
    # distinguish clubs (United/City/Sociedad/...), they are different clubs.
    diff = set(a.split()) ^ set(b.split())
    if diff and diff <= _DISAMBIGUATING_TOKENS:
        return False
    jw = jaro_winkler(a, b)
    if jw < _JW_REVIEW_FLOOR:
        return False
    return jw >= _JW_ACCEPT and token_sort_ratio(a, b) >= _TOKEN_SORT_ACCEPT


def _leagues_agree(league: str | None, cand_league: str | None) -> bool:
    """League block: agree when EITHER side's league is unknown (never reject on
    absent metadata) OR the two canonicalize equal. A KNOWN disagreement blocks."""
    if not league or not cand_league:
        return True
    return normalize_name(league) == normalize_name(cand_league)


def _pair_score(a1: str, a2: str, b1: str, b2: str) -> float:
    """Summed JW over the two oriented base-name pairs — the ambiguity tie-break
    signal (higher = a closer overall name match)."""
    return jaro_winkler(a1, b1) + jaro_winkler(a2, b2)


def match_event_hardened(
    home: str,
    away: str,
    kickoff: datetime,
    candidates: Sequence[EventCandidate],
    *,
    aliases: AliasTable,
    ordered: bool = True,
    league: str | None = None,
    candidate_leagues: Mapping[str, str] | None = None,
    max_minute_drift: int = 360,
    max_accept_minute_drift: int = _ACCEPT_MINUTE_DRIFT,
    allow_orientation_flip: bool = False,
) -> EventCandidate | None:
    """Precision-hardened cross-source match — the SHADOW-path lift over the
    strict ``match_event``. ``match_event`` is intentionally left UNCHANGED so the
    live anchor loader keeps its exact-only behaviour; this function adds the
    research-validated recall levers, each paired with an independent precision
    guard (Fellegi-Sunter ANDing):

    STAGE 0  BLOCK  — per-candidate (canonical) league agreement + a UTC-minute
                      kickoff window (tighter than the strict calendar day).
    STAGE 1  VETO   — categorical marker negative-rule: a one-sided women/youth/
                      reserve marker REJECTS (known-club whitelist consulted
                      first, in ``strip_markers``).
    STAGE 4/5 NAME  — exact canonical OR two-tier Jaro-Winkler + token-sort on
                      marker-stripped base names, with a disambiguating-token
                      blocklist; the REVIEW band (0.84<=JW<0.92) is NOT accepted.
    ORIENTATION     — a home/away swap (ordered events) is accepted ONLY when
                      league AND the kickoff window independently agree AND
                      ``allow_orientation_flip`` is set — never on name alone.
    AMBIGUITY       — if the top-two survivors are within the score margin, or
                      both orientations of one candidate qualify, REJECT.
    ACCEPT WINDOW   — ``max_minute_drift`` is only the candidate-FETCH window (wide
                      on purpose, so ambiguity detection sees every same-teams leg
                      of a series). Acceptance is gated SEPARATELY at the tighter
                      ``max_accept_minute_drift`` (``_ACCEPT_MINUTE_DRIFT``, 6h):
                      the chosen best must be within it OR the match is REJECTED.
                      Same-teams legs split ACROSS that bound (a rematch / two-leg
                      / doubleheader) are DISTINCT games and are NEVER silently
                      collapsed to the nearer kickoff — they REJECT as ambiguous.

    Returns the unique best candidate, or None (never guess — a wrong close is
    fake CLV, the cardinal sin).
    """
    leagues = candidate_leagues or {}
    target_home = aliases.canonical(home)
    target_away = aliases.canonical(away)
    if not target_home or not target_away or target_home == target_away:
        return None
    # Base name = alias-canonicalized AND marker-stripped. Canonicalizing first
    # preserves the strict exact-alias tier ("Man Utd"->"manchester united") so
    # the fuzzy band only ever fires when the alias table did not already resolve
    # the pair; marker-stripping isolates the BASE club for the JW comparison.
    base_home = aliases.canonical(strip_markers(home))
    base_away = aliases.canonical(strip_markers(away))

    scored: list[tuple[float, EventCandidate]] = []
    for cand in candidates:
        # STAGE 0a: UTC-minute kickoff window (not calendar day).
        if abs((cand.kickoff - kickoff).total_seconds()) > max_minute_drift * 60:
            continue
        # STAGE 0b: league block (known disagreement only).
        cand_league = leagues.get(cand.ref)
        same_league = _leagues_agree(league, cand_league)
        if not same_league:
            continue
        if aliases.canonical(cand.home) == aliases.canonical(cand.away):
            continue
        cand_base_home = aliases.canonical(strip_markers(cand.home))
        cand_base_away = aliases.canonical(strip_markers(cand.away))

        # Forward orientation.
        forward = (
            not _markers_conflict(home, away, cand.home, cand.away)
            and _base_name_ok(base_home, cand_base_home)
            and _base_name_ok(base_away, cand_base_away)
        )
        if forward:
            scored.append((_pair_score(base_home, base_away, cand_base_home, cand_base_away), cand))
            continue

        # Orientation flip: ONLY for unordered (tennis) by default, or for ordered
        # events when explicitly allowed AND league is KNOWN-agreeing (not merely
        # absent) — a swapped name coincidence is not a confirmed fixture.
        flip_ok = (not ordered) or (
            allow_orientation_flip and bool(league) and bool(cand_league) and same_league
        )
        if flip_ok:
            swapped = (
                not _markers_conflict(home, away, cand.away, cand.home)
                and _base_name_ok(base_home, cand_base_away)
                and _base_name_ok(base_away, cand_base_home)
            )
            if swapped:
                scored.append(
                    (_pair_score(base_home, base_away, cand_base_away, cand_base_home), cand)
                )

    if not scored:
        return None
    # ACCEPT-window gate (independent of the wide candidate-FETCH window): a
    # candidate is only ELIGIBLE to be accepted when its kickoff is within the tight
    # accept bound of the pick. Candidates gathered only for ambiguity context (a
    # same-teams series leg two days away) are filtered out HERE — they can never be
    # the close, but a same-teams leg INSIDE the bound competing with them is still
    # judged on its own. A same-teams fixture beyond the bound is a DIFFERENT game
    # (rematch / two-leg / doubleheader — Gigantes/Cangrejeros BSN, home/away
    # swapped, 48h earlier); attaching its close would be fake CLV, the cardinal sin.
    accept_seconds = max_accept_minute_drift * 60
    eligible = [
        (s, c) for (s, c) in scored if abs((c.kickoff - kickoff).total_seconds()) <= accept_seconds
    ]
    if not eligible:
        return None

    def _same_fixture(x: EventCandidate, y: EventCandidate) -> bool:
        return aliases.canonical(x.home) == aliases.canonical(y.home) and aliases.canonical(
            x.away
        ) == aliases.canonical(y.away)

    # Best (already score/kickoff/ref sorted) among the eligible in-bound candidates.
    eligible.sort(key=lambda s: (-s[0], abs((s[1].kickoff - kickoff).total_seconds()), s[1].ref))
    best_score, best = eligible[0]
    if len(eligible) > 1:
        runner_score, runner = eligible[1]
        # Two DISTINCT candidates within the ambiguity margin -> cannot tell which
        # is the real fixture. (Duplicate captures of ONE fixture share canonical
        # teams and are NOT ambiguous — they collapse to the nearest-kickoff one.)
        if not _same_fixture(runner, best) and best_score - runner_score < _AMBIGUITY_MARGIN:
            return None
        # Two DISTINCT same-teams games BOTH inside the tight accept bound (a true
        # doubleheader / two legs <6h apart) cannot be told apart by name OR by a
        # day-window — REJECT. Only candidates that are within the same-game kickoff
        # tolerance of the best are treated as duplicate captures and collapsed;
        # a same-teams runner inside the accept window but a distinct-game distance
        # from the best (a few hours, not minutes) is ambiguous, not a duplicate.
        for _runner_score, other in eligible[1:]:
            if other is best:
                continue
            if _same_fixture(other, best) and (
                abs((other.kickoff - best.kickoff).total_seconds()) > _DUPLICATE_CAPTURE_SECONDS
            ):
                return None
    return best
