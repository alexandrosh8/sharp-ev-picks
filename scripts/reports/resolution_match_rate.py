"""Shadow Pinnacle-archive match-rate report (ADR-0014).

What fraction of our picks could attach a sharp Pinnacle close via the STRICT
cross-source matcher? Run this BEFORE flipping CLV_USE_PINNACLE_ARCHIVE=true — a
low overall rate is DIAGNOSED, not guessed:

  no_archive_candidates     -> COVERAGE gap: the pinnacle_<sport> archive has no
                               event in the kickoff window (enable ARCADIA_ENABLED,
                               capture longer);
  unmatched_with_candidates -> ALIAS gap: archive events exist in-window but the
                               strict matcher rejected them (extend the alias
                               table in app/resolution/aliases_seed.json).

    uv run python scripts/reports/resolution_match_rate.py [--days N] [--json]

Read-only over our own warehouse. Nothing here places a bet, attaches a close,
or writes anything — it only reports whether the matcher WOULD resolve. The pure
aggregation lives in app/resolution/shadow.py (unit-tested); the DB read and
Settings access happen only in main() — this script's composition root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.resolution.shadow import MatchRateReport, summarize_match_rate  # noqa: E402


async def _load(days: int | None) -> MatchRateReport:
    """DB read (composition root): shadow-resolve picks with a known kickoff."""
    from app.config import get_settings
    from app.database import create_engine, create_session_factory
    from app.storage.repositories import shadow_match_rate_outcomes

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    since = datetime.now(tz=UTC) - timedelta(days=days) if days is not None else None
    try:
        async with session_factory() as session:
            outcomes = await shadow_match_rate_outcomes(session, since=since)
    finally:
        await engine.dispose()
    return summarize_match_rate(outcomes)


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _print_human(report: MatchRateReport) -> None:
    print(f"picks evaluated : {report.total}")
    print(f"strict-matched  : {report.matched}  ({_pct(report.match_rate)})")
    print(f"  no archive event in window (coverage gap) : {report.no_archive_candidates}")
    print(f"  in-window but unmatched (alias gap)       : {report.unmatched_with_candidates}")
    if report.by_sport:
        print("\nby sport:")
        for g in report.by_sport:
            print(f"  {g.key:<22} {g.matched:>5}/{g.total:<5}  {_pct(g.match_rate)}")
    if report.by_league:
        print("\nby league:")
        for g in report.by_league:
            print(f"  {g.key:<26} {g.matched:>5}/{g.total:<5}  {_pct(g.match_rate)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow Pinnacle-archive match-rate report.")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="only picks whose kickoff is within the last N days",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args()
    report = asyncio.run(_load(args.days))
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
