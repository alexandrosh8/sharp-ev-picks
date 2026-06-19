"""Shadow Betfair-Exchange close-coverage report (ADR-0015).

How many of our picks could attach a captured Betfair Exchange BACK close via the
EXACT external_ref match ("betfair:"+pick_ref)? Run this BEFORE flipping
CLV_USE_BETFAIR_EXCHANGE=true — unlike the Pinnacle matcher there is no alias /
fuzz ambiguity to diagnose, so coverage is a pure presence/absence count:

  with_event -> a "betfair:"-namespaced event exists for the fixture (the page
                was captured at least once);
  with_close -> of those, the event carries a USABLE BACK close inside the
                kickoff window (the same SNAPSHOT_CLOSE_MAX_GAP gate the
                consumption path applies) — what CLV_USE_BETFAIR_EXCHANGE can
                actually attach.

    uv run python scripts/reports/betfair_exchange_coverage.py [--days N] [--json]

Read-only over our own warehouse. Nothing here places a bet, attaches a close, or
writes anything — it only reports whether a Betfair close WOULD be attachable. The
pure aggregation lives in app/resolution/shadow.py (unit-tested); the DB read and
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

from app.resolution.shadow import BetfairCoverageReport, summarize_betfair_coverage  # noqa: E402


async def _load(days: int | None) -> BetfairCoverageReport:
    """DB read (composition root): shadow Betfair coverage over known-kickoff picks."""
    from app.config import get_settings
    from app.database import create_engine, create_session_factory
    from app.storage.repositories import betfair_exchange_coverage_outcomes

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    since = datetime.now(tz=UTC) - timedelta(days=days) if days is not None else None
    try:
        async with session_factory() as session:
            outcomes = await betfair_exchange_coverage_outcomes(session, since=since)
    finally:
        await engine.dispose()
    return summarize_betfair_coverage(outcomes)


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _print_human(report: BetfairCoverageReport) -> None:
    print(f"picks evaluated      : {report.total}")
    print(f"with betfair event   : {report.with_event}")
    print(f"with usable close    : {report.with_close}  ({_pct(report.close_rate)})")
    # Per-sport with_event vs with_close keeps a 0 HONEST: a sport with
    # with_event 0 was never captured (structural — capture off/unwired for it),
    # NOT "no Betfair-liquid match in the slate" (which shows as with_event > 0,
    # with_close 0). The note under each bucket spells out which 0 it is.
    event_by_sport = {g.key: g for g in report.event_by_sport}
    if report.by_sport:
        print("\nby sport (with_event -> usable close):")
        for g in report.by_sport:
            ev = event_by_sport.get(g.key)
            ev_count = ev.matched if ev is not None else 0
            note = ""
            if g.matched == 0:
                note = (
                    "  <- no betfair page captured for this sport (structural 0)"
                    if ev_count == 0
                    else "  <- pages captured, none usable this window (thin slate)"
                )
            print(
                f"  {g.key:<22} event {ev_count:>4}/{g.total:<5}  "
                f"close {g.matched:>4}/{g.total:<5}  {_pct(g.match_rate)}{note}"
            )
    if report.by_league:
        print("\nby league (with usable close):")
        for g in report.by_league:
            print(f"  {g.key:<26} {g.matched:>5}/{g.total:<5}  {_pct(g.match_rate)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Shadow Betfair-Exchange close-coverage report (read-only)."
    )
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
