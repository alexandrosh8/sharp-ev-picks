"""Pure shared logic for the alias-vetting workflow (no IO side effects beyond
explicit file read/write helpers; NO env, NO DB, NO network in this module).

Pipeline (house doctrine — aliases are per-club, human-vetted, wrong-game-safe):

  1. ``extract_alias_candidates`` replays the probe cascade
     (scripts/research/probe_unmatched_split.py: strict ``match_event`` ->
     OddsPortal slug fallback -> ``match_event_hardened``) over pick fixtures vs
     the ``pinnacle_<sport>`` archive and yields the NAME-FORM alias-candidate
     pairs. The probe's ``_main`` is a module-level asyncio script and not
     importable, so its classification is replicated here SURGICALLY (same
     constants, same relations) and unit-tested.
  2. ``compute_risk_flags`` annotates each pair with the wrong-game risk flags a
     human reviewer must clear (marker conflicts use the resolution module's
     marker tokens via ``distinguishing_markers`` — never re-invented).
  3. ``split_decisions`` / ``build_alias_additions`` / ``apply_additions`` turn a
     human-reviewed CSV back into a seed patch: ONLY ``human_decision=approve``
     rows pass, flagged rows require reviewer_notes, and every alias is guarded
     against marker-crossing and canonical-collision (the CD-Nacional precedent:
     a generic base merging distinct clubs is the cardinal sin).

Nothing here loosens the matcher: the output is a REVIEW artifact + a patch
proposal; the seed file itself is only written by review_aliases --apply, and
every applied batch must still pass app/maintenance/wrong_game_audit.py with
0 new merges before commit.
"""

from __future__ import annotations

import csv
import difflib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.resolution import (
    AliasTable,
    EventCandidate,
    distinguishing_markers,
    jaro_winkler,
    match_event,
    match_event_hardened,
    normalize_name,
    oddsportal_slug_names,
    strip_markers,
)
from app.resolution.matching import _DISAMBIGUATING_TOKENS
from app.resolution.shadow import arcadia_base_sport
from app.resolution.tennis_names import canonical_tennis_name

# --- probe constants, kept in sync with scripts/research/probe_unmatched_split.py
MAX_DAY_DRIFT = 1
_ACCEPT_SECONDS = 6 * 60 * 60  # mirror matching._ACCEPT_MINUTE_DRIFT (6h)
_JW_NEARMISS = 0.84  # mirror matching._JW_REVIEW_FLOOR

WEAK_SIMILARITY_FLOOR = 0.90
_COMMON_TOKEN_MIN_LEN = 4  # ignore trivial short tokens for same_country_common_name

CSV_COLUMNS: tuple[str, ...] = (
    "candidate_id",
    "source_a",
    "raw_name_a",
    "source_b",
    "raw_name_b",
    "sport",
    "league",
    "country",
    "confidence",
    "reason",
    "sample_event_count",
    "example_events",
    "suggested_alias_key",
    "risk_flags",
    "human_decision",
    "reviewer_notes",
)

# Known FALSE pairs visible in probe output / the golden _NOT_ADDED list
# (tests/test_resolution_nameform_aliases.py). Seed examples of the wrong-game
# class — a candidate matching one of these (either order, normalized) is
# flagged known_false_pattern and must never be approved without evidence.
_KNOWN_FALSE_RAW: tuple[tuple[str, str], ...] = (
    ("Western City Rangers", "Western Knights"),
    ("Racing", "Racing Beirut"),
    ("Everton", "Everton Vina del Mar"),
    ("Fenix", "Club Atletico Fenix"),
    ("Jazz Pori", "Jazz"),
    ("Gigantes San Francisco", "Indios de San Francisco de Macoris"),
    ("Bayswater", "Bayswater City"),
    # 2026-07-03 escalation review: Redlands/Redlands United and Playford
    # Patriots/Playford City removed on fixture evidence (see docs/review/
    # alias_candidates_escalated_2026-07-03.csv and the matching _NOT_ADDED
    # amendment) — vetted same-club aliases. Gremio Juventus/Juventus SC STAYS:
    # normalize_name("Juventus SC") -> bare "juventus" collides with the Turin
    # canonical (wrong-game unsafe as a seed alias; needs scoped aliasing).
    ("FC Kharkiv", "Metalist Kharkiv"),
    ("Zaglebie", "Zaglebie Lubin"),
    ("Gremio Juventus", "Juventus SC"),
    ("Brevard SC", "Brevard Fire"),
    ("Zielona Gora", "Lechia Zielona Gora"),
    ("Galanta", "Slovan Galanta"),
)
KNOWN_FALSE_PAIRS: frozenset[frozenset[str]] = frozenset(
    frozenset({normalize_name(a), normalize_name(b)}) for a, b in _KNOWN_FALSE_RAW
)


@dataclass(frozen=True)
class PickFixture:
    """One pick-side fixture (the feed/OddsPortal side of the funnel)."""

    pick_id: int
    sport_key: str
    league_key: str
    country: str
    home: str
    away: str
    kickoff: datetime  # UTC-aware
    external_ref: str


@dataclass(frozen=True)
class ArchiveEvent:
    """One ``pinnacle_<sport>`` archive event (the counterparty side)."""

    sport_key: str
    home: str
    away: str
    kickoff: datetime  # UTC-aware
    league_key: str | None = None


@dataclass
class AliasCandidate:
    """One aggregated NAME-FORM alias-candidate pair awaiting human vetting."""

    raw_name_a: str  # pick/feed side
    raw_name_b: str  # pinnacle archive side
    sport: str
    league: str
    country: str
    confidence: float
    reason: str
    sample_event_count: int = 1
    example_events: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)

    @property
    def suggested_alias_key(self) -> str:
        """Canonical-entry suggestion: the fuller archive form (house seed style
        keys the full club name, aliases the short feed form)."""
        return self.raw_name_b


def pair_confidence(name_a: str, name_b: str, aliases: AliasTable) -> float:
    """Min-side JW on the alias-canonicalized, marker-stripped base names — the
    same confidence basis the hardened matcher persists (MatchOutcome)."""
    return jaro_winkler(
        aliases.canonical(strip_markers(name_a)), aliases.canonical(strip_markers(name_b))
    )


def _toks(name: str) -> set[str]:
    return set(normalize_name(name).split())


def _side_relation(a: str, b: str, aliases: AliasTable) -> str:
    """Probe-identical side relation: 'same' (base-equal), 'near' (token overlap
    or JW >= review floor on base), else 'unrelated'."""
    base_a = aliases.canonical(strip_markers(a))
    base_b = aliases.canonical(strip_markers(b))
    if not base_a or not base_b:
        return "unrelated"
    if base_a == base_b:
        return "same"
    if _toks(a) & _toks(b):
        return "near"
    if jaro_winkler(base_a, base_b) >= _JW_NEARMISS:
        return "near"
    return "unrelated"


def _classify_counterpart(
    home: str,
    away: str,
    cands: Sequence[EventCandidate],
    kickoff: datetime,
    aliases: AliasTable,
) -> tuple[str, list[tuple[str, str, str]]]:
    """Probe-identical NAME-FORM classification, but returning PER-SIDE pairs.

    Returns ``(label, pairs)`` where each pair is ``(pick_name, cand_name,
    reason)``; reason is ``single_side_nearmiss`` (the other side already
    base-matches — the clean one-alias fix) or ``both_sides_near`` (both sides
    are near-misses; emitted as TWO per-club rows for separate vetting)."""
    pairs: list[tuple[str, str, str]] = []
    found_nameform = False
    for c in cands:
        if abs((c.kickoff - kickoff).total_seconds()) > _ACCEPT_SECONDS:
            continue
        for ch, ca in ((c.home, c.away), (c.away, c.home)):
            if distinguishing_markers(home) != distinguishing_markers(ch) or (
                distinguishing_markers(away) != distinguishing_markers(ca)
            ):
                continue
            rh = _side_relation(home, ch, aliases)
            ra = _side_relation(away, ca, aliases)
            if "unrelated" in {rh, ra}:
                continue
            found_nameform = True
            if rh == "same" and ra == "near":
                pairs.append((away, ca, "single_side_nearmiss"))
            elif ra == "same" and rh == "near":
                pairs.append((home, ch, "single_side_nearmiss"))
            elif rh == "near" and ra == "near":
                pairs.append((home, ch, "both_sides_near"))
                pairs.append((away, ca, "both_sides_near"))
    if found_nameform:
        return "NAME-FORM", pairs
    return "NO-COUNTERPART", []


def extract_alias_candidates(
    picks: Sequence[PickFixture],
    archive: Sequence[ArchiveEvent],
    *,
    aliases: AliasTable,
) -> list[AliasCandidate]:
    """Replay the probe cascade over pick fixtures vs the archive and return the
    aggregated, deduplicated NAME-FORM alias-candidate pairs (risk flags NOT yet
    attached — see ``attach_risk_flags``). Pure: rows in, candidates out."""
    by_ns: dict[str, list[ArchiveEvent]] = {}
    for ev in archive:
        by_ns.setdefault(ev.sport_key, []).append(ev)

    agg: dict[tuple[str, str], AliasCandidate] = {}
    for pick in picks:
        ns = f"pinnacle_{arcadia_base_sport(pick.sport_key)}"
        is_tennis = arcadia_base_sport(pick.sport_key) == "tennis"
        qh = canonical_tennis_name(pick.home) if is_tennis else pick.home
        qa = canonical_tennis_name(pick.away) if is_tennis else pick.away
        in_window = [
            ev
            for ev in by_ns.get(ns, [])
            if abs((ev.kickoff.date() - pick.kickoff.date()).days) <= MAX_DAY_DRIFT
        ]
        cands = [
            EventCandidate(
                ref=str(i),
                home=canonical_tennis_name(ev.home) if is_tennis else ev.home,
                away=canonical_tennis_name(ev.away) if is_tennis else ev.away,
                kickoff=ev.kickoff,
            )
            for i, ev in enumerate(in_window)
        ]
        cand_leagues = {str(i): ev.league_key for i, ev in enumerate(in_window) if ev.league_key}
        # live-equivalent cascade (probe-identical)
        matched = match_event(
            qh, qa, pick.kickoff, cands, aliases=aliases, max_day_drift=MAX_DAY_DRIFT
        )
        if matched is None:
            slug = oddsportal_slug_names(pick.external_ref)
            if slug is not None:
                sh = canonical_tennis_name(slug[0]) if is_tennis else slug[0]
                sa = canonical_tennis_name(slug[1]) if is_tennis else slug[1]
                # Production slug marker-loss guard (app/storage/repositories.py):
                # the OddsPortal slug DROPS women/youth/reserve markers, so use it
                # only when it RETAINS every marker the display name carries —
                # else the marker-less slug strict-matches the men's/senior event
                # (a pseudo-merge the live matcher categorically refuses).
                display_markers = distinguishing_markers(pick.home) | distinguishing_markers(
                    pick.away
                )
                slug_markers = distinguishing_markers(sh) | distinguishing_markers(sa)
                if display_markers <= slug_markers:
                    matched = match_event(
                        sh, sa, pick.kickoff, cands, aliases=aliases, max_day_drift=MAX_DAY_DRIFT
                    )
        if matched is None:
            matched = match_event_hardened(
                qh,
                qa,
                pick.kickoff,
                cands,
                aliases=aliases,
                ordered=not is_tennis,
                league=pick.league_key,
                candidate_leagues=cand_leagues,
            )
        if matched is not None or not cands:
            continue  # MATCHED or COVERAGE-GAP — nothing alias-addressable
        label, side_pairs = _classify_counterpart(qh, qa, cands, pick.kickoff, aliases)
        if label != "NAME-FORM":
            continue
        example = f"{pick.home} vs {pick.away} @ {pick.kickoff.isoformat()}"
        for pick_name, cand_name, reason in side_pairs:
            if normalize_name(pick_name) == normalize_name(cand_name):
                continue  # already identical post-normalization: not an alias gap
            key = (normalize_name(pick_name), normalize_name(cand_name))
            existing = agg.get(key)
            if existing is None:
                agg[key] = AliasCandidate(
                    raw_name_a=pick_name,
                    raw_name_b=cand_name,
                    sport=pick.sport_key,
                    league=pick.league_key,
                    country=pick.country,
                    confidence=round(pair_confidence(pick_name, cand_name, aliases), 4),
                    reason=reason,
                    sample_event_count=1,
                    example_events=[example],
                )
            else:
                existing.sample_event_count += 1
                if example not in existing.example_events and len(existing.example_events) < 2:
                    existing.example_events.append(example)
    return sorted(agg.values(), key=lambda c: (c.sport, c.league, c.raw_name_a, c.raw_name_b))


def build_token_league_map(candidates: Iterable[AliasCandidate]) -> dict[str, set[str]]:
    """token -> set of distinct league keys the token appears in, over BOTH names
    of every candidate — the basis for the same_country_common_name flag."""
    out: dict[str, set[str]] = {}
    for cand in candidates:
        for name in (cand.raw_name_a, cand.raw_name_b):
            for tok in _toks(name):
                if len(tok) >= _COMMON_TOKEN_MIN_LEN:
                    out.setdefault(tok, set()).add(cand.league)
    return out


def compute_risk_flags(
    name_a: str,
    name_b: str,
    confidence: float,
    token_leagues: Mapping[str, set[str]] | None = None,
) -> list[str]:
    """Wrong-game risk flags for one candidate pair (pipe-joined in the CSV).

    Marker conflicts come from the resolution module's own marker tokens
    (``distinguishing_markers`` — women/youth/reserve), never re-invented here.
    """
    flags: list[str] = []
    marker_diff = distinguishing_markers(name_a) ^ distinguishing_markers(name_b)
    if "women" in marker_diff:
        flags.append("women_men_conflict")
    if "youth" in marker_diff:
        flags.append("youth_senior_conflict")
    if "reserve" in marker_diff:
        flags.append("reserve_b_team_conflict")
    norm_a, norm_b = normalize_name(name_a), normalize_name(name_b)
    if norm_a != norm_b and sorted(norm_a.split()) == sorted(norm_b.split()):
        flags.append("token_order_only")
    if confidence < WEAK_SIMILARITY_FLOOR:
        flags.append("weak_similarity")
    if token_leagues:
        for tok in sorted(_toks(name_a) | _toks(name_b)):
            if len(tok) >= _COMMON_TOKEN_MIN_LEN and len(token_leagues.get(tok, set())) > 1:
                flags.append("same_country_common_name")
                break
    toks_a, toks_b = _toks(name_a), _toks(name_b)
    if toks_a and toks_b and (toks_a < toks_b or toks_b < toks_a):
        flags.append("city_club_ambiguity")
    # Known-false: the literal seed pairs, OR the United/City class — two base
    # names differing ONLY by disambiguating tokens are DISTINCT clubs.
    base_a, base_b = strip_markers(name_a), strip_markers(name_b)
    diff = set(base_a.split()) ^ set(base_b.split())
    if frozenset({norm_a, norm_b}) in KNOWN_FALSE_PAIRS or (
        base_a != base_b and diff and diff <= _DISAMBIGUATING_TOKENS
    ):
        flags.append("known_false_pattern")
    return flags


def attach_risk_flags(candidates: Sequence[AliasCandidate]) -> None:
    """Compute + attach risk flags in place (needs the whole batch for the
    cross-league common-name flag)."""
    token_leagues = build_token_league_map(candidates)
    for cand in candidates:
        cand.risk_flags = compute_risk_flags(
            cand.raw_name_a, cand.raw_name_b, cand.confidence, token_leagues
        )


def candidates_to_rows(candidates: Sequence[AliasCandidate]) -> list[dict[str, str]]:
    """Serialize candidates into review-CSV row dicts (human_decision blank)."""
    rows: list[dict[str, str]] = []
    for i, cand in enumerate(candidates, start=1):
        rows.append(
            {
                "candidate_id": f"AC-{i:04d}",
                "source_a": "oddsportal",
                "raw_name_a": cand.raw_name_a,
                "source_b": f"pinnacle_{arcadia_base_sport(cand.sport)}",
                "raw_name_b": cand.raw_name_b,
                "sport": cand.sport,
                "league": cand.league,
                "country": cand.country,
                "confidence": f"{cand.confidence:.4f}",
                "reason": cand.reason,
                "sample_event_count": str(cand.sample_event_count),
                "example_events": "; ".join(cand.example_events),
                "suggested_alias_key": cand.suggested_alias_key,
                "risk_flags": "|".join(cand.risk_flags),
                "human_decision": "",
                "reviewer_notes": "",
            }
        )
    return rows


def write_review_csv(rows: Sequence[Mapping[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def load_review_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


@dataclass(frozen=True)
class DecisionSplit:
    """Reviewed-CSV triage: only ``approved`` rows may become aliases."""

    approved: list[dict[str, str]]
    rejected_missing_notes: list[dict[str, str]]  # approve + risk flags, NO notes -> hard reject
    skipped: list[dict[str, str]]  # blank / reject / anything not 'approve'


def split_decisions(rows: Sequence[Mapping[str, str]]) -> DecisionSplit:
    """ONLY human_decision=approve passes; an approved row carrying ANY risk flag
    additionally requires non-empty reviewer_notes (the human must say WHY the
    flag is safe) — else it is hard-rejected, never silently accepted."""
    approved: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for row in rows:
        decision = (row.get("human_decision") or "").strip().lower()
        if decision != "approve":
            skipped.append(dict(row))
            continue
        has_flags = bool((row.get("risk_flags") or "").strip())
        has_notes = bool((row.get("reviewer_notes") or "").strip())
        if has_flags and not has_notes:
            rejected.append(dict(row))
        else:
            approved.append(dict(row))
    return DecisionSplit(approved=approved, rejected_missing_notes=rejected, skipped=skipped)


@dataclass(frozen=True)
class AliasAddition:
    canonical: str
    alias: str
    candidate_id: str


def build_alias_additions(
    approved: Sequence[Mapping[str, str]], seed_table: AliasTable
) -> tuple[list[AliasAddition], list[str]]:
    """Turn approved rows into (canonical, alias) additions with the wrong-game
    guards: marker-crossing aliases and canonical-collisions are ERRORS, never
    silently added (the CD-Nacional precedent)."""
    additions: list[AliasAddition] = []
    errors: list[str] = []
    batch_alias_to_canon: dict[str, str] = {}
    for row in approved:
        cid = row.get("candidate_id", "?")
        canonical = (row.get("suggested_alias_key") or "").strip() or row.get("raw_name_b", "")
        canonical = canonical.strip()
        if not canonical or not normalize_name(canonical):
            errors.append(f"{cid}: empty/all-noise canonical key — cannot add")
            continue
        canon_norm = seed_table.canonical(canonical)
        for name in (row.get("raw_name_a", ""), row.get("raw_name_b", "")):
            alias = name.strip()
            alias_norm = normalize_name(alias)
            if not alias_norm or alias_norm == normalize_name(canonical):
                continue
            if distinguishing_markers(alias) != distinguishing_markers(canonical):
                errors.append(
                    f"{cid}: MARKER-CROSSING alias {alias!r} -> {canonical!r} "
                    "(women/youth/reserve marker mismatch) — refused"
                )
                continue
            # Collision guard: the alias name is already KNOWN to the seed table
            # (as an alias or a canonical) and resolves to a DIFFERENT club.
            existing_canon = seed_table.canonical(alias)
            known = existing_canon != alias_norm or len(seed_table.aliases_of(alias)) > 1
            if known and existing_canon != canon_norm:
                errors.append(
                    f"{cid}: COLLISION — alias {alias!r} already resolves to "
                    f"{existing_canon!r}, not {canon_norm!r} — refused"
                )
                continue
            prior = batch_alias_to_canon.get(alias_norm)
            if prior is not None and prior != canon_norm:
                errors.append(
                    f"{cid}: BATCH CONFLICT — alias {alias!r} claimed for both "
                    f"{prior!r} and {canon_norm!r} — refused"
                )
                continue
            batch_alias_to_canon[alias_norm] = canon_norm
            additions.append(AliasAddition(canonical=canonical, alias=alias, candidate_id=cid))
    return additions, errors


def apply_additions(
    seed_data: Mapping[str, object], additions: Sequence[AliasAddition]
) -> dict[str, object]:
    """Return a NEW seed dict with the additions merged (existing canonical
    entries get the alias appended; unknown canonicals become new entries at the
    end, matching the seed's append-at-end convention). Never mutates input."""
    teams_obj = seed_data.get("teams", {})
    if not isinstance(teams_obj, dict):
        raise ValueError("seed data has no 'teams' mapping")
    teams: dict[str, list[str]] = {str(k): list(v) for k, v in teams_obj.items()}
    norm_to_key = {normalize_name(k): k for k in teams}
    for add in additions:
        key = norm_to_key.get(normalize_name(add.canonical))
        if key is None:
            teams[add.canonical] = []
            key = add.canonical
            norm_to_key[normalize_name(key)] = key
        existing_norms = {normalize_name(n) for n in (key, *teams[key])}
        if normalize_name(add.alias) not in existing_norms:
            teams[key].append(add.alias)
    out: dict[str, object] = {k: v for k, v in seed_data.items() if k != "teams"}
    out["teams"] = teams
    return out


def render_seed(seed_data: Mapping[str, object]) -> str:
    """The seed file's exact on-disk rendering (verified round-trip-identical
    with the current aliases_seed.json)."""
    return json.dumps(seed_data, indent=2, ensure_ascii=False) + "\n"


def unified_seed_diff(old_text: str, new_text: str) -> str:
    """git-apply-able unified diff a/... b/... against the seed path."""
    rel = "app/resolution/aliases_seed.json"
    lines = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    return "".join(lines)


def _opponent_from_example(example: str, name_a: str, name_b: str) -> str:
    """Best-effort opponent extraction from a 'Home vs Away @ kickoff' example
    (the side that is NOT the aliased club); falls back to a synthetic rival."""
    fixture = example.split(" @ ", 1)[0]
    if " vs " in fixture:
        home, away = fixture.split(" vs ", 1)
        aliased_norms = {normalize_name(name_a), normalize_name(name_b)}
        for side in (home, away):
            if normalize_name(side) not in aliased_norms:
                return side.strip()
    return "Opponent United"


def render_test_skeleton(approved: Sequence[Mapping[str, str]], date_str: str) -> str:
    """A per-batch regression-test skeleton in the golden-test style
    (tests/test_resolution_nameform_aliases.py): one fixes-the-match test + one
    no-wrong-merge (marker) test per alias. Valid python; move it into tests/
    AFTER the seed patch is applied (it fails against the unpatched seed)."""
    pair_lines: list[str] = []
    for row in approved:
        feed = row.get("raw_name_a", "")
        pinn = row.get("suggested_alias_key") or row.get("raw_name_b", "")
        example = (row.get("example_events") or "").split("; ")[0]
        opponent = _opponent_from_example(example, feed, pinn)
        pair_lines.append(f"    ({feed!r}, {pinn!r}, {opponent!r}),")
    pairs_block = "\n".join(pair_lines) if pair_lines else "    # (no approved pairs)"
    module_date = date_str.replace("-", "_")
    return f'''"""GENERATED regression skeleton for the {date_str} alias batch (review before
moving into tests/). Locks BOTH directions of the wrong-game-safety contract:
the vetted pair now strict-matches, and the alias NEVER crosses a
women/youth/reserve marker. Apply the seed patch FIRST, then run the
wrong-game audit (0 new merges) and
`uv run pytest tests/test_alias_batch_{module_date}.py -q`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.resolution import EventCandidate, default_aliases, match_event, match_event_hardened

# (feed form, pinnacle/canonical form, real opponent from the observed fixture)
_BATCH: list[tuple[str, str, str]] = [
{pairs_block}
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
    cand = EventCandidate(ref="x", home=f"{{pinnacle}} {{marker}}", away=opponent, kickoff=_KO)
    assert match_event_hardened(feed, opponent, _KO, [cand], aliases=aliases, ordered=True) is None
'''


def render_rejected_suggestions(rows: Sequence[Mapping[str, str]]) -> str:
    """Commented negative-alias suggestion block for the pairs a human REJECTED —
    for consideration as _NOT_ADDED golden entries (never auto-applied)."""
    lines = [
        "# Rejected alias candidates — NEGATIVE-alias suggestions (human consideration",
        "# only; add the vetted ones to the _NOT_ADDED list in",
        "# tests/test_resolution_nameform_aliases.py so they stay distinct forever):",
        "# _NOT_ADDED_CANDIDATES = [",
    ]
    for row in rows:
        a = row.get("raw_name_a", "")
        b = row.get("raw_name_b", "")
        flags = row.get("risk_flags", "")
        notes = (row.get("reviewer_notes") or "").strip()
        comment = f"{flags}" + (f" — {notes}" if notes else "")
        lines.append(f"#     ({a!r}, {b!r}),  # {comment}")
    lines.append("# ]")
    return "\n".join(lines) + "\n"


# --- offline row parsing (psql COPY ... CSV extracts) -------------------------


def parse_ts(value: str) -> datetime:
    """ISO-8601 (or 'YYYY-MM-DD HH:MM:SS+00') -> UTC-aware datetime. A naive
    input is a bug upstream (UTC-everywhere rule) but is pinned to UTC here
    rather than dropped, so an operator extract never silently loses rows."""
    ts = datetime.fromisoformat(value.strip())
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def picks_from_csv(path: Path) -> list[PickFixture]:
    """Columns: pick_id,sport_key,league_key,country,home,away,starts_at,external_ref."""
    out: list[PickFixture] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or row[0] == "pick_id":
                continue
            out.append(
                PickFixture(
                    pick_id=int(row[0]),
                    sport_key=row[1],
                    league_key=row[2],
                    country=row[3],
                    home=row[4],
                    away=row[5],
                    kickoff=parse_ts(row[6]),
                    external_ref=row[7],
                )
            )
    return out


def archive_from_csv(path: Path) -> list[ArchiveEvent]:
    """Columns: sport_key,home,away,starts_at,league_key."""
    out: list[ArchiveEvent] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or row[0] == "sport_key":
                continue
            out.append(
                ArchiveEvent(
                    sport_key=row[0],
                    home=row[1],
                    away=row[2],
                    kickoff=parse_ts(row[3]),
                    league_key=row[4] or None,
                )
            )
    return out


def kickoff_window(picks: Sequence[PickFixture]) -> tuple[datetime, datetime]:
    """The archive fetch window the probe uses: [min-2d, max+2d] around picks."""
    window = timedelta(days=MAX_DAY_DRIFT + 1)
    kickoffs = [p.kickoff for p in picks]
    return min(kickoffs) - window, max(kickoffs) + window
