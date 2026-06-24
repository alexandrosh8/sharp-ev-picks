"""List the fixtures the HARDENED shadow matcher still misses (read-only).

`probe_arcadia_match.py` replays the STRICT `match_event`; the shadow REPORT,
however, runs `match_event_hardened` (the JW-fuzzy + league/marker/ambiguity
path) — so the near-spelling cases the strict probe shows as "alias gaps" are
mostly ALREADY matched in the headline rate. This script replays the SAME
hardened path `repositories.shadow_match_rate_outcomes` uses and prints ONLY the
fixtures the hardened matcher STILL rejects — the true target set for new
aliases. Writes nothing; attaches no close.

  uv run python scripts/research/probe_hardened_misses.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAX_DAY_DRIFT = 1


async def _main() -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from app.config import get_settings
    from app.database import create_engine, create_session_factory
    from app.resolution import (
        EventCandidate,
        default_aliases,
        match_event,
        match_event_hardened,
        oddsportal_slug_names,
    )
    from app.resolution.matching import normalize_name
    from app.resolution.shadow import arcadia_base_sport
    from app.storage.models import Event, League, Pick, Sport, Team

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    aliases = default_aliases()
    window = timedelta(days=MAX_DAY_DRIFT + 1)

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

            misses: list[tuple] = []
            matched = 0
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
                    in_window = [
                        c
                        for c in archive
                        if abs((c.kickoff.date() - ko.date()).days) <= MAX_DAY_DRIFT
                    ]
                    m = match_event(
                        home, away, ko, in_window, aliases=aliases, max_day_drift=MAX_DAY_DRIFT
                    )
                    if m is None:
                        slug = oddsportal_slug_names(ext)
                        if slug is not None:
                            m = match_event(
                                slug[0],
                                slug[1],
                                ko,
                                in_window,
                                aliases=aliases,
                                max_day_drift=MAX_DAY_DRIFT,
                            )
                    if m is None:
                        m = match_event_hardened(
                            home,
                            away,
                            ko,
                            in_window,
                            aliases=aliases,
                            ordered=sk != "tennis",
                            league=lk,
                            candidate_leagues=arc_leagues,
                        )
                    if m is not None:
                        matched += 1
                    elif in_window:
                        misses.append((sk, lk, home, away, ko, in_window))

            print(
                f"\nHARDENED-path: matched={matched}  still-missing-with-candidates={len(misses)}\n"
            )

            def _tok(name: str) -> set[str]:
                return set(normalize_name(name).split())

            print("=== HARDENED-path MISSES with a plausible same-fixture candidate ===")
            for sk, lk, home, away, ko, cands in misses:
                pt = _tok(home) | _tok(away)
                suspects = [c for c in cands if (_tok(c.home) | _tok(c.away)) & pt]
                if not suspects:
                    continue
                print(f"\n[{sk} / {lk}]  {ko:%Y-%m-%d %H:%M}")
                print(f"  ODDSPORTAL : {home!r} vs {away!r}")
                for c in suspects[:4]:
                    dd = (c.kickoff.date() - ko.date()).days
                    print(f"  PINNACLE   : {c.home!r} vs {c.away!r}  (day {dd:+d})")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
