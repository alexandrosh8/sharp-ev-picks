"""Export NAME-FORM alias candidates to a human-review CSV (STRICTLY read-only).

Replays the probe cascade (scripts/research/probe_unmatched_split.py) over pick
fixtures vs the ``pinnacle_<sport>`` archive and writes
``docs/review/alias_candidates_<YYYY-MM-DD>.csv`` with computed wrong-game risk
flags. ``human_decision`` is ALWAYS left blank — a human vets every pair
(house doctrine: per-club aliases only; see tools/review_aliases.py for the
approve->patch step).

Modes (all SELECT-only; this tool never writes to the database):
  DB mode      : uv run python -m tools.export_alias_candidates [--dsn URL]
                 (default DSN = app settings; use --dsn inside the prod
                 container, e.g. postgresql+asyncpg://...@postgres:5432/betting_ai)
  Offline mode : --picks-csv X.csv --archive-csv Y.csv (psql COPY extracts, for
                 sandboxes that cannot reach the DB port). Extraction SQL is in
                 the two _SQL_* constants below — run each via
                 docker exec betting-ai-postgres-1 psql -U betting_ai -d betting_ai
                 with "COPY (...) TO STDOUT WITH CSV".
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from tools.alias_vetting import (
    ArchiveEvent,
    PickFixture,
    archive_from_csv,
    attach_risk_flags,
    candidates_to_rows,
    extract_alias_candidates,
    kickoff_window,
    picks_from_csv,
    write_review_csv,
)

_SQL_PICKS = """
SELECT p.id, s.key, l.key, l.country, ht.name, at.name, e.starts_at, e.external_ref
FROM picks p
JOIN events e ON p.event_id = e.id
JOIN sports s ON e.sport_id = s.id
JOIN leagues l ON e.league_id = l.id
JOIN teams ht ON e.home_team_id = ht.id
JOIN teams at ON e.away_team_id = at.id
WHERE e.starts_at IS NOT NULL
"""

_SQL_ARCHIVE = """
SELECT s.key, ht.name, at.name, e.starts_at, l.key
FROM events e
JOIN sports s ON e.sport_id = s.id
JOIN teams ht ON e.home_team_id = ht.id
JOIN teams at ON e.away_team_id = at.id
LEFT JOIN leagues l ON e.league_id = l.id
WHERE s.key LIKE 'pinnacle%' AND e.starts_at IS NOT NULL
"""


async def _fetch_rows(dsn: str | None) -> tuple[list[PickFixture], list[ArchiveEvent]]:
    """SELECT-only fetch of pick fixtures + pinnacle archive events via the
    app's async engine (probe-identical query surface + league country)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import aliased

    from app.database import create_session_factory
    from app.storage.models import Event, League, Pick, Sport, Team

    if dsn is None:
        from app.config import get_settings
        from app.database import create_engine

        engine = create_engine(get_settings())
    else:
        engine = create_async_engine(dsn, pool_pre_ping=True)
    session_factory = create_session_factory(engine)
    picks: list[PickFixture] = []
    archive: list[ArchiveEvent] = []
    try:
        async with session_factory() as session:
            home_t, away_t = aliased(Team), aliased(Team)
            pick_rows = (
                await session.execute(
                    select(
                        Pick.id,
                        Sport.key,
                        League.key,
                        League.country,
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
            picks = [
                PickFixture(
                    pick_id=pid,
                    sport_key=sk,
                    league_key=lk,
                    country=country or "",
                    home=home,
                    away=away,
                    kickoff=ko if ko.tzinfo else ko.replace(tzinfo=UTC),
                    external_ref=ext,
                )
                for pid, sk, lk, country, home, away, ko, ext in pick_rows
            ]
            if picks:
                lo, hi = kickoff_window(picks)
                arc_home, arc_away = aliased(Team), aliased(Team)
                arc_rows = (
                    await session.execute(
                        select(Sport.key, arc_home.name, arc_away.name, Event.starts_at, League.key)
                        .select_from(Event)
                        .join(Sport, Event.sport_id == Sport.id)
                        .join(arc_home, Event.home_team_id == arc_home.id)
                        .join(arc_away, Event.away_team_id == arc_away.id)
                        .join(League, Event.league_id == League.id, isouter=True)
                        .where(
                            Sport.key.like("pinnacle%"),
                            Event.starts_at.is_not(None),
                            Event.starts_at >= lo,
                            Event.starts_at <= hi,
                        )
                    )
                ).all()
                archive = [
                    ArchiveEvent(
                        sport_key=sk,
                        home=home,
                        away=away,
                        kickoff=ko if ko.tzinfo else ko.replace(tzinfo=UTC),
                        league_key=lk,
                    )
                    for sk, home, away, ko, lk in arc_rows
                ]
    finally:
        await engine.dispose()
    return picks, archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export alias candidates to a review CSV")
    parser.add_argument("--dsn", default=None, help="async SQLAlchemy DSN override (SELECT-only)")
    parser.add_argument("--picks-csv", type=Path, default=None, help="offline picks extract")
    parser.add_argument("--archive-csv", type=Path, default=None, help="offline archive extract")
    parser.add_argument("--out", type=Path, default=None, help="output CSV path")
    args = parser.parse_args(argv)

    if (args.picks_csv is None) != (args.archive_csv is None):
        parser.error("--picks-csv and --archive-csv must be given together")
    if args.picks_csv is not None and args.archive_csv is not None:
        picks = picks_from_csv(args.picks_csv)
        archive = archive_from_csv(args.archive_csv)
    else:
        picks, archive = asyncio.run(_fetch_rows(args.dsn))

    from app.resolution import default_aliases

    candidates = extract_alias_candidates(picks, archive, aliases=default_aliases())
    attach_risk_flags(candidates)
    rows = candidates_to_rows(candidates)

    today = datetime.now(tz=UTC).date().isoformat()
    out: Path = args.out or Path("docs/review") / f"alias_candidates_{today}.csv"
    write_review_csv(rows, out)

    flag_counts: dict[str, int] = {}
    for cand in candidates:
        for flag in cand.risk_flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    print(f"picks={len(picks)} archive_events={len(archive)} candidates={len(rows)} -> {out}")
    for flag, n in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
        print(f"  flag {flag:28s} {n}")
    unflagged = sum(1 for c in candidates if not c.risk_flags)
    print(f"  (unflagged: {unflagged})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
