"""CAPTURE-COVERAGE gap analysis — which leagues lack a sharp counterpart.

The binding constraint on the cross-source match rate is NOT name-form (alias)
gaps but NO-COUNTERPART (~26.5% of pick fixtures): a pick exists from the
OddsPortal scrape, archive events exist that day, but NONE is a plausible same
fixture — the sharp (Pinnacle) line for that league was simply never captured.

This instrument replays the SAME live-equivalent cascade as
``probe_unmatched_split.py`` and aggregates the per-pick label BY LEAGUE, then
cross-references each pick-league against the Pinnacle archive's own league
inventory to split the gap into:

  NAME-MATCH gap   : the Pinnacle archive DOES carry a league with the same
                     normalized key -> the counterpart exists, the miss is a
                     name/alias problem (addressable by the review queue).
  CAPTURE gap      : the Pinnacle archive carries NO league with that key ->
                     the sharp book is not pricing it in our universe; closing
                     it needs new INGESTION (or there is no sharp line to get).

Each pick-league is tagged with a coarse VALUE tier (major / mid / thin-obscure /
youth-women-reserve) so the gap can be ranked by relevance to the pick universe,
not by raw count. Writes a CSV; prints the ranked tables. Read-only — adds and
mutates nothing.

  uv run python scripts/research/coverage_gap.py [--csv PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAX_DAY_DRIFT = 1
_ACCEPT_SECONDS = 6 * 60 * 60
_JW_NEARMISS = 0.84
_DEFAULT_CSV = Path("/tmp/claude-1001/-workspace/scratchpad/coverage_gap.csv")

# Value-tier heuristics on the league key (case-insensitive).
_YOUTH_WOMEN_RE = re.compile(
    r"\b(women|ladies|fem|frauen|u1[0-9]|u2[0-3]|youth|juvenil|reserve|sub2[0-3])\b|\bw$",
    re.IGNORECASE,
)
_THIN_RE = re.compile(
    r"\b(npl|division 3|division 4|esiliiga|i lyga|ii lyga|primera c|torneo federal|"
    r"reserve league|regionalliga|oberliga|state league|premier league 2|"
    r"queensland|new south wales|nsw|south australia|western australia|victoria|"
    r"northern nsw|capital territory|tasmania|segunda|tercera|liga 3|"
    r"national league|league two|league one|copa|friendl)\b",
    re.IGNORECASE,
)
_MAJOR_RE = re.compile(
    r"\b(premier league|la liga|laliga|serie a|bundesliga|ligue 1|eredivisie|"
    r"primeira liga|champions league|europa league|mls|brasileiro serie a|"
    r"liga mx|nba|euro|copa america|world cup|allsvenskan|eliteserien|"
    r"superligaen|super lig|ekstraklasa|jupiler|pro league)\b",
    re.IGNORECASE,
)


def _value_tier(sport: str, league: str) -> str:
    """Coarse relevance tier for a league key, value-lens for the gap ranking."""
    lk = league.strip()
    if not lk:
        return "thin-obscure"
    if _YOUTH_WOMEN_RE.search(lk):
        return "youth-women-reserve"
    # a major key wins unless it is explicitly a women's/youth variant (above)
    if _MAJOR_RE.search(lk) and not _THIN_RE.search(lk):
        if re.search(r"\b(women|w)\b", lk, re.IGNORECASE):
            return "youth-women-reserve"
        return "major"
    if _THIN_RE.search(lk):
        return "thin-obscure"
    return "mid"


@dataclass
class GapRow:
    sport: str
    league: str
    picks: int
    matched: int
    no_counterpart: int
    name_form: int
    tier: str
    gap_kind: str  # CAPTURE / NAME-MATCH / mixed
    est_gain: int


async def _main(csv_path: Path) -> None:
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

    def _has_nameform(home: str, away: str, cands: list, ko) -> bool:
        for c in cands:
            if abs((c.kickoff - ko).total_seconds()) > _ACCEPT_SECONDS:
                continue
            for ch, ca in ((c.home, c.away), (c.away, c.home)):
                if distinguishing_markers(home) != distinguishing_markers(
                    ch
                ) or distinguishing_markers(away) != distinguishing_markers(ca):
                    continue
                if "unrelated" not in {_side_relation(home, ch), _side_relation(away, ca)}:
                    return True
        return False

    counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    arc_league_keys: dict[str, set[str]] = defaultdict(set)

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
                for _h, _a, _ko, lg in arc_rows:
                    if lg:
                        arc_league_keys[ns].add(normalize_name(lg))

                for _pid, sk, lk, home, away, ko, ext in picks:
                    lk = lk or "(none)"
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
                    bucket = counts[(sk, lk)]
                    if m is not None:
                        bucket["matched"] += 1
                    elif not cands:
                        bucket["coverage-gap"] += 1
                    elif _has_nameform(qh, qa, cands, ko):
                        bucket["name-form"] += 1
                    else:
                        bucket["no-counterpart"] += 1
    finally:
        await engine.dispose()

    rows: list[GapRow] = []
    for (sk, lk), c in counts.items():
        matched = c["matched"]
        nocp = c["no-counterpart"]
        nf = c["name-form"]
        total = sum(c.values())
        if total - matched == 0:
            continue  # fully matched league — no gap
        ns = f"pinnacle_{arcadia_base_sport(sk)}"
        sharp_has_league = normalize_name(lk) in arc_league_keys.get(ns, set())
        if nocp and not sharp_has_league:
            gap_kind = "CAPTURE"
        elif sharp_has_league and (nocp or nf):
            gap_kind = "NAME-MATCH"
        else:
            gap_kind = "mixed"
        rows.append(
            GapRow(
                sport=sk,
                league=lk,
                picks=total,
                matched=matched,
                no_counterpart=nocp,
                name_form=nf,
                tier=_value_tier(sk, lk),
                gap_kind=gap_kind,
                est_gain=nocp + nf,
            )
        )

    tier_rank = {"major": 0, "mid": 1, "thin-obscure": 2, "youth-women-reserve": 3}
    rows.sort(key=lambda r: (tier_rank.get(r.tier, 9), -r.est_gain, r.sport, r.league))

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "sport",
                "league",
                "picks",
                "matched",
                "no_counterpart",
                "name_form",
                "value_tier",
                "gap_kind",
                "est_matched_pick_gain",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r.sport,
                    r.league,
                    r.picks,
                    r.matched,
                    r.no_counterpart,
                    r.name_form,
                    r.tier,
                    r.gap_kind,
                    r.est_gain,
                ]
            )

    tot: Counter[str] = Counter()
    for c in counts.values():
        tot.update(c)
    grand = sum(tot.values())
    print(f"=== PICK-FIXTURE LABELS over {grand} ===")
    for k in ("matched", "coverage-gap", "no-counterpart", "name-form"):
        print(f"  {k:16s}: {tot[k]:5d}  ({100.0 * tot[k] / grand if grand else 0:5.1f}%)")

    by_tier: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    for r in rows:
        by_tier[r.tier] += r.est_gain
        by_kind[r.gap_kind] += r.est_gain
    print("\n=== addressable (no-counterpart + name-form) by VALUE TIER ===")
    for t in sorted(by_tier, key=lambda k: tier_rank.get(k, 9)):
        print(f"  {t:22s}: {by_tier[t]:4d}")
    print("\n=== by GAP KIND ===")
    for k, v in by_kind.most_common():
        print(f"  {k:12s}: {v:4d}")

    print("\n=== RANKED coverage-gap leagues (value tier, then est. gain) ===")
    print(
        "| value tier | sport | league | picks | matched | no-cp | name-form | "
        "gap kind | est. gain |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r.tier} | {r.sport} | {r.league} | {r.picks} | {r.matched} | "
            f"{r.no_counterpart} | {r.name_form} | {r.gap_kind} | {r.est_gain} |"
        )

    print(f"\nCSV written: {csv_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=_DEFAULT_CSV)
    args = parser.parse_args()
    asyncio.run(_main(args.csv))
