"""AMBIGUOUS-alias REVIEW QUEUE — surface near-miss candidates, never auto-add.

The matching workstream auto-adds only CLEAN single-alias fixes (one side already
base-equal, the other an unambiguous spelling variant). This instrument surfaces
the OTHER near-misses — the ones deliberately LEFT OUT because adding an alias
would risk a wrong-game CLV anchor — as a structured, human-reviewable queue.

It replays the SAME live-equivalent cascade the shadow harness uses (strict
``match_event`` -> OddsPortal slug fallback -> ``match_event_hardened``). For every
pick fixture that STILL does not match but has a plausible same-fixture Pinnacle
counterpart in the +/-6h accept window, it emits one review row per (pick,
candidate) pairing, classified by WHY it is ambiguous:

  wrong-game-marker : the pick and candidate disagree on a women/youth/reserve
                      marker (W / U19 / U21 / U23 / reserve / II). This is a HARD
                      BLOCKER — attaching it would anchor the wrong fixture. Shown,
                      never silently dropped, with the conflicting markers spelled
                      out in the ``blockers`` column.
  bare-ambiguous    : the near side is a BARE token that matches >=2 DISTINCT
                      canonical clubs in the slate, or collides across >=2 leagues
                      (e.g. bare "Vikingur" -> Reykjavik / Gota; "Al Arabi" across
                      Kuwait / Qatar). An alias here would merge distinct clubs.
  distinct-club     : the two base names differ ONLY by a club-DISTINGUISHING token
                      (United / City / B / II / Atletico / ...), so they are most
                      likely DIFFERENT clubs (Western City Rangers <-> Western
                      Knights; Bayswater <-> Bayswater City). Reviewer confirms.
  weak-both-near    : NEITHER side is base-equal; both are only weakly near — the
                      whole fixture is a guess (needs two reviewed aliases).
  near-miss-1side   : one side base-equal, the other a near spelling variant that
                      was NOT auto-added (sub-threshold / token-overlap only) —
                      the lowest-risk band, still vet before adding.

Writes a CSV (default scratchpad) and prints a markdown sample. Adds NOTHING to
the alias table; attaches no close. Read-only.

  uv run python scripts/research/alias_review_queue.py [--csv PATH] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAX_DAY_DRIFT = 1
_ACCEPT_SECONDS = 6 * 60 * 60  # mirror matching._ACCEPT_MINUTE_DRIFT (6h)
_JW_NEARMISS = 0.84  # mirror matching._JW_REVIEW_FLOOR (a plausible same-club hint)
_DEFAULT_CSV = Path("/tmp/claude-1001/-workspace/scratchpad/alias_review_queue.csv")


@dataclass(frozen=True)
class ReviewRow:
    sport: str
    league: str
    kickoff_utc: str
    pick_home: str
    pick_away: str
    cand_home: str
    cand_away: str
    near_side: str  # which side carries the alias gap: home / away / both
    pick_token: str  # the unmatched pick-side name to vet
    candidate_canonical: str  # the Pinnacle-side name it would map to
    reason: str
    blockers: str  # wrong-game discriminators, ; -separated, or ""
    action: str


def _markers_str(markers: frozenset[str]) -> str:
    return "+".join(sorted(markers)) if markers else "none"


async def _main(csv_path: Path, limit: int) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from app.config import get_settings
    from app.database import create_engine, create_session_factory
    from app.resolution import (
        EventCandidate,
        default_aliases,
        distinguishing_markers,
        jaro_winkler,
        match_event,
        match_event_hardened,
        oddsportal_slug_names,
    )
    from app.resolution.matching import normalize_name, strip_markers
    from app.resolution.shadow import arcadia_base_sport
    from app.resolution.tennis_names import canonical_tennis_name
    from app.storage.models import Event, League, Pick, Sport, Team

    try:
        from app.resolution.matching import _DISAMBIGUATING_TOKENS as DISAMB
    except ImportError:  # pragma: no cover - defensive
        DISAMB = frozenset()

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    aliases = default_aliases()
    window = timedelta(days=MAX_DAY_DRIFT + 1)

    def _toks(name: str) -> set[str]:
        return set(normalize_name(name).split())

    def _base(name: str) -> str:
        return aliases.canonical(strip_markers(name))

    def _base_toks(name: str) -> frozenset[str]:
        return frozenset(_base(name).split())

    def _side_relation(a: str, b: str) -> str:
        ba, bb = _base(a), _base(b)
        if not ba or not bb:
            return "unrelated"
        if ba == bb:
            return "same"
        if _toks(a) & _toks(b):
            return "near"
        if jaro_winkler(ba, bb) >= _JW_NEARMISS:
            return "near"
        return "unrelated"

    rows: list[ReviewRow] = []
    reasons: Counter[str] = Counter()

    try:
        async with session_factory() as session:
            home_t, away_t = aliased(Team), aliased(Team)
            pick_rows = (
                await session.execute(
                    select(
                        Pick.id,
                        Sport.key,
                        League.key,
                        home_t.name,
                        away_t.name,
                        Event.starts_at,
                        Event.external_ref,
                    )
                    .select_from(Pick)
                    .join(Event, Pick.event_id == Event.id)
                    .join(Sport, Event.sport_id == Sport.id)
                    .join(League, Event.league_id == League.id)
                    .join(home_t, Event.home_team_id == home_t.id)
                    .join(away_t, Event.away_team_id == away_t.id)
                    .where(Event.starts_at.is_not(None))
                )
            ).all()

            by_ns: dict[str, list] = {}
            for row in pick_rows:
                by_ns.setdefault(f"pinnacle_{arcadia_base_sport(row[1])}", []).append(row)

            for ns, picks in by_ns.items():
                kickoffs = [p[5] for p in picks]
                ah, aw = aliased(Team), aliased(Team)
                al = aliased(League)
                arc_rows = (
                    await session.execute(
                        select(ah.name, aw.name, Event.starts_at, al.key)
                        .join(Sport, Event.sport_id == Sport.id)
                        .join(ah, Event.home_team_id == ah.id)
                        .join(aw, Event.away_team_id == aw.id)
                        .join(al, Event.league_id == al.id, isouter=True)
                        .where(
                            Sport.key == ns,
                            Event.starts_at.is_not(None),
                            Event.starts_at >= min(kickoffs) - window,
                            Event.starts_at <= max(kickoffs) + window,
                        )
                    )
                ).all()
                archive = [
                    EventCandidate(ref=str(i), home=h, away=a, kickoff=ko)
                    for i, (h, a, ko, _l) in enumerate(arc_rows)
                ]
                arc_leagues = {str(i): lg for i, (_h, _a, _ko, lg) in enumerate(arc_rows) if lg}

                # Per-namespace base-name -> {distinct candidate base names} and
                # base-name -> {leagues} indexes, for bare-ambiguous detection.
                base_to_fullbases: dict[str, set[str]] = {}
                base_to_leagues: dict[str, set[str]] = {}
                for _i, (h, a, _ko, lg) in enumerate(arc_rows):
                    for side in (h, a):
                        b = _base(side)
                        if not b:
                            continue
                        base_to_fullbases.setdefault(b, set()).add(b)
                        if lg:
                            base_to_leagues.setdefault(b, set()).add(normalize_name(lg))

                def _bare_ambiguous(
                    near_name: str,
                    _fullbases: dict[str, set[str]] = base_to_fullbases,
                    _leagues: dict[str, set[str]] = base_to_leagues,
                ) -> bool:
                    """True when near_name is a BARE token that is a proper subset
                    of >=2 DISTINCT candidate bases in the slate, OR whose exact
                    base collides across >=2 leagues (cross-league same name)."""
                    nb = _base_toks(near_name)
                    if not nb:
                        return False
                    containing = {full for full in _fullbases if nb < frozenset(full.split())}
                    if len(containing) >= 2:
                        return True
                    base = " ".join(sorted(nb))
                    return len(_leagues.get(base, set())) >= 2

                seen: set[tuple] = set()
                for _pid, sk, lk, home, away, ko, ext in picks:
                    is_tennis = arcadia_base_sport(sk) == "tennis"
                    qh = canonical_tennis_name(home) if is_tennis else home
                    qa = canonical_tennis_name(away) if is_tennis else away
                    in_window = [
                        c
                        for c in archive
                        if abs((c.kickoff.date() - ko.date()).days) <= MAX_DAY_DRIFT
                    ]
                    cands = [
                        EventCandidate(
                            ref=c.ref,
                            home=canonical_tennis_name(c.home) if is_tennis else c.home,
                            away=canonical_tennis_name(c.away) if is_tennis else c.away,
                            kickoff=c.kickoff,
                        )
                        for c in in_window
                    ]
                    # live-equivalent cascade — skip anything that already matches
                    m = match_event(qh, qa, ko, cands, aliases=aliases, max_day_drift=MAX_DAY_DRIFT)
                    if m is None:
                        slug = oddsportal_slug_names(ext)
                        if slug is not None:
                            sh = canonical_tennis_name(slug[0]) if is_tennis else slug[0]
                            sa = canonical_tennis_name(slug[1]) if is_tennis else slug[1]
                            m = match_event(
                                sh, sa, ko, cands, aliases=aliases, max_day_drift=MAX_DAY_DRIFT
                            )
                    if m is None:
                        m = match_event_hardened(
                            qh,
                            qa,
                            ko,
                            cands,
                            aliases=aliases,
                            ordered=not is_tennis,
                            league=lk,
                            candidate_leagues=arc_leagues,
                        )
                    if m is not None:
                        continue  # already resolved by the live cascade

                    # surface every plausible same-fixture counterpart
                    for c in cands:
                        if abs((c.kickoff - ko).total_seconds()) > _ACCEPT_SECONDS:
                            continue
                        orientations = (
                            ((c.home, c.away),)
                            if not is_tennis
                            else ((c.home, c.away), (c.away, c.home))
                        )
                        for ch, ca in orientations:
                            rh = _side_relation(qh, ch)
                            ra = _side_relation(qa, ca)
                            if "unrelated" in {rh, ra}:
                                continue  # different game in this orientation
                            mk_h_pick = distinguishing_markers(qh)
                            mk_h_cand = distinguishing_markers(ch)
                            mk_a_pick = distinguishing_markers(qa)
                            mk_a_cand = distinguishing_markers(ca)
                            blockers: list[str] = []
                            if mk_h_pick != mk_h_cand:
                                blockers.append(
                                    f"home {_markers_str(mk_h_pick)}!={_markers_str(mk_h_cand)}"
                                )
                            if mk_a_pick != mk_a_cand:
                                blockers.append(
                                    f"away {_markers_str(mk_a_pick)}!={_markers_str(mk_a_cand)}"
                                )

                            # which side(s) carry the alias gap (base differs)?
                            gap_home = rh != "same"
                            gap_away = ra != "same"
                            if gap_home and gap_away:
                                near_side = "both"
                                pick_tok, cand_tok = f"{qh}|{qa}", f"{ch}|{ca}"
                            elif gap_home:
                                near_side = "home"
                                pick_tok, cand_tok = qh, ch
                            else:
                                near_side = "away"
                                pick_tok, cand_tok = qa, ca

                            # classify the reason (priority order)
                            if blockers:
                                reason, action = "wrong-game-marker", "BLOCK — wrong fixture"
                            elif (gap_home and _bare_ambiguous(qh)) or (
                                gap_away and _bare_ambiguous(qa)
                            ):
                                reason, action = (
                                    "bare-ambiguous",
                                    "REVIEW — disambiguate by league",
                                )
                            else:
                                diff_tokens: set[str] = set()
                                if gap_home:
                                    diff_tokens |= _base_toks(qh) ^ _base_toks(ch)
                                if gap_away:
                                    diff_tokens |= _base_toks(qa) ^ _base_toks(ca)
                                if diff_tokens and diff_tokens <= DISAMB:
                                    reason, action = (
                                        "distinct-club",
                                        "REVIEW — likely different club",
                                    )
                                elif gap_home and gap_away:
                                    reason, action = (
                                        "weak-both-near",
                                        "REVIEW — both sides unproven",
                                    )
                                else:
                                    reason, action = (
                                        "near-miss-1side",
                                        "REVIEW — vet spelling variant",
                                    )

                            key = (
                                ns,
                                normalize_name(qh),
                                normalize_name(qa),
                                normalize_name(ch),
                                normalize_name(ca),
                            )
                            if key in seen:
                                continue
                            seen.add(key)
                            reasons[reason] += 1
                            rows.append(
                                ReviewRow(
                                    sport=sk,
                                    league=lk or "",
                                    kickoff_utc=ko.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    pick_home=qh,
                                    pick_away=qa,
                                    cand_home=ch,
                                    cand_away=ca,
                                    near_side=near_side,
                                    pick_token=pick_tok,
                                    candidate_canonical=cand_tok,
                                    reason=reason,
                                    blockers="; ".join(blockers),
                                    action=action,
                                )
                            )
    finally:
        await engine.dispose()

    # sort: blockers first, then by reason, sport, league
    reason_order = {
        "wrong-game-marker": 0,
        "bare-ambiguous": 1,
        "distinct-club": 2,
        "weak-both-near": 3,
        "near-miss-1side": 4,
    }
    rows.sort(key=lambda r: (reason_order.get(r.reason, 9), r.sport, r.league, r.pick_home))

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "sport",
                "league",
                "kickoff_utc",
                "pick_home",
                "pick_away",
                "cand_home",
                "cand_away",
                "near_side",
                "pick_token",
                "candidate_canonical",
                "reason",
                "blockers",
                "action",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r.sport,
                    r.league,
                    r.kickoff_utc,
                    r.pick_home,
                    r.pick_away,
                    r.cand_home,
                    r.cand_away,
                    r.near_side,
                    r.pick_token,
                    r.candidate_canonical,
                    r.reason,
                    r.blockers,
                    r.action,
                ]
            )

    print(f"=== ALIAS REVIEW QUEUE — {len(rows)} candidate(s) (NONE auto-added) ===")
    for reason in sorted(reasons, key=lambda k: reason_order.get(k, 9)):
        print(f"  {reason:18s}: {reasons[reason]:4d}")
    print(f"\nCSV written: {csv_path}")

    print(f"\n=== MARKDOWN SAMPLE (first {limit}) ===\n")
    print(
        "| sport | league | kickoff (UTC) | pick | candidate | gap side | reason | "
        "blockers | action |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows[:limit]:
        print(
            f"| {r.sport} | {r.league} | {r.kickoff_utc} | "
            f"{r.pick_home} vs {r.pick_away} | {r.cand_home} vs {r.cand_away} | "
            f"{r.near_side} | {r.reason} | {r.blockers or '—'} | {r.action} |"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=_DEFAULT_CSV)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    asyncio.run(_main(args.csv, args.limit))
