"""Classify UNMATCHED cross-source fixtures: COVERAGE-GAP vs NAME-FORM (read-only).

STEP 1 instrument for the alias-coverage push. For every pick fixture with a
known kickoff it replays the SAME live-equivalent cascade the shadow harness uses
(strict ``match_event`` -> OddsPortal slug fallback -> ``match_event_hardened``)
and, for the ones that STILL do not match, splits them into:

  COVERAGE-GAP   : zero pinnacle_<sport> archive events in the +/-1-day window
                   (no counterparty fixture exists in our event universe at all —
                   UNFIXABLE by aliases).
  NO-COUNTERPART : archive events exist that day, but NONE is a plausible same
                   fixture (different games — also effectively unfixable here:
                   the counterparty simply was not captured).
  NAME-FORM      : a plausible same-fixture counterparty DOES exist (markers
                   agree, kickoff within the tight accept window, and at least one
                   side already base-matches while the other is a near-miss, OR
                   both sides token-overlap) — the ALIAS-FIXABLE addressable slice.

For each NAME-FORM case it prints the NON-matching side's (pick_name, cand_name)
pair — the exact alias candidate to vet. Writes nothing; attaches no close.

  uv run python scripts/research/probe_unmatched_split.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAX_DAY_DRIFT = 1
_ACCEPT_SECONDS = 6 * 60 * 60  # mirror matching._ACCEPT_MINUTE_DRIFT (6h)
_JW_NEARMISS = 0.84  # mirror matching._JW_REVIEW_FLOOR (a plausible same-club hint)


async def _main() -> None:
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

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    aliases = default_aliases()
    window = timedelta(days=MAX_DAY_DRIFT + 1)

    def _toks(name: str) -> set[str]:
        return set(normalize_name(name).split())

    def _base(name: str) -> str:
        return aliases.canonical(strip_markers(name))

    def _side_relation(a: str, b: str) -> str:
        """How related are two team names? 'same' (base-equal), 'near' (token
        overlap or JW>=floor on base — a plausible alias gap), or 'unrelated'."""
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

    def _classify_counterpart(
        home: str, away: str, cands: list[EventCandidate], kickoff: datetime
    ) -> tuple[str, tuple[str, str] | None]:
        """Return (label, alias_candidate). label in {NAME-FORM, NO-COUNTERPART}.
        alias_candidate is the (pick_side_name, cand_side_name) on the side that is
        a near-miss when exactly one side matches, else None."""
        best: tuple[str, str] | None = None
        found_nameform = False
        for c in cands:
            if abs((c.kickoff - kickoff).total_seconds()) > _ACCEPT_SECONDS:
                continue
            for ch, ca, _swapped in ((c.home, c.away, False), (c.away, c.home, True)):
                # markers must agree on both sides for this orientation
                if distinguishing_markers(home) != distinguishing_markers(
                    ch
                ) or distinguishing_markers(away) != distinguishing_markers(ca):
                    continue
                rh = _side_relation(home, ch)
                ra = _side_relation(away, ca)
                rels = {rh, ra}
                if "unrelated" in rels:
                    continue
                # both sides at least 'near' -> plausible same fixture (NAME-FORM)
                found_nameform = True
                # exactly one side already base-equal, the other a near miss ->
                # that other side is the clean single-alias fix.
                if rh == "same" and ra == "near":
                    best = (away, ca)
                elif ra == "same" and rh == "near":
                    best = (home, ch)
                elif best is None and rh == "near" and ra == "near":
                    best = (f"{home}|{away}", f"{ch}|{ca}")
        if found_nameform:
            return "NAME-FORM", best
        return "NO-COUNTERPART", None

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

            labels: Counter[str] = Counter()
            labels_by_sport: dict[str, Counter[str]] = {}
            alias_candidates: list[tuple[str, str, str, str]] = []  # sport,league,pick,cand
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
                    # live-equivalent cascade
                    m = match_event(qh, qa, ko, cands, aliases=aliases, max_day_drift=MAX_DAY_DRIFT)
                    if m is None:
                        slug = oddsportal_slug_names(ext)
                        if slug is not None:
                            sh = canonical_tennis_name(slug[0]) if is_tennis else slug[0]
                            sa = canonical_tennis_name(slug[1]) if is_tennis else slug[1]
                            # Production slug marker-loss guard (repositories.py):
                            # use the slug only when it RETAINS every marker the
                            # display name carries — a marker-less slug would
                            # strict-match the men's/senior event (pseudo-merge).
                            display_markers = distinguishing_markers(home) | distinguishing_markers(
                                away
                            )
                            slug_markers = distinguishing_markers(sh) | distinguishing_markers(sa)
                            if display_markers <= slug_markers:
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
                    sport_ctr = labels_by_sport.setdefault(sk, Counter())
                    if m is not None:
                        labels["MATCHED"] += 1
                        sport_ctr["MATCHED"] += 1
                        continue
                    if not cands:
                        labels["COVERAGE-GAP"] += 1
                        sport_ctr["COVERAGE-GAP"] += 1
                        continue
                    label, alias_cand = _classify_counterpart(qh, qa, cands, ko)
                    labels[label] += 1
                    sport_ctr[label] += 1
                    if label == "NAME-FORM" and alias_cand is not None:
                        alias_candidates.append((sk, lk or "", alias_cand[0], alias_cand[1]))

            total = sum(labels.values())
            print(f"\n=== UNMATCHED-SPLIT over {total} pick-fixtures ===")
            for k in ("MATCHED", "COVERAGE-GAP", "NO-COUNTERPART", "NAME-FORM"):
                n = labels.get(k, 0)
                pct = 100.0 * n / total if total else 0.0
                print(f"  {k:16s}: {n:6d}  ({pct:5.1f}%)")
            unmatched = total - labels.get("MATCHED", 0)
            addressable = labels.get("NAME-FORM", 0)
            print(
                f"\n  unmatched={unmatched}  addressable-by-alias(NAME-FORM)={addressable}"
                f"  ({100.0 * addressable / unmatched if unmatched else 0:.1f}% of unmatched)"
            )

            print("\n=== per-sport ===")
            for sk in sorted(labels_by_sport):
                c = labels_by_sport[sk]
                print(
                    f"  {sk:22s} matched={c.get('MATCHED', 0):5d} "
                    f"covgap={c.get('COVERAGE-GAP', 0):4d} "
                    f"nocp={c.get('NO-COUNTERPART', 0):4d} "
                    f"nameform={c.get('NAME-FORM', 0):4d}"
                )

            n_cand = len(alias_candidates)
            print(f"\n=== NAME-FORM alias candidates ({n_cand}) — vet before adding ===")
            seen: set[tuple[str, str]] = set()
            for sk, lk, pick_side, cand_side in sorted(alias_candidates):
                key = (normalize_name(pick_side), normalize_name(cand_side))
                if key in seen:
                    continue
                seen.add(key)
                print(f"  [{sk} / {lk}]  PICK {pick_side!r}  <->  PINN {cand_side!r}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
