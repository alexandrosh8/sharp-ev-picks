"""Full live end-to-end run on an IN-SEASON league (default: Brazil Serie A).

Fits penaltyblog Dixon-Coles on real football-data.co.uk history AND scrapes
live OddsPortal odds for the SAME league (names match -> real picks can fire).

    uv run python scripts/run_live.py                 # Brazil Serie A
    uv run python scripts/run_live.py --code ARG --slug argentina-primera-division
    uv run python scripts/run_live.py --min-edge 0.0  # show every priced market

Decision-support only: nothing here places bets.
"""

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from app.config import get_settings, stake_policy
from app.database import create_engine, create_session_factory
from app.edge.gates import GatePolicy
from app.ingestion.base import EventDirectory
from app.ingestion.football_data import fetch_new_league_csv, parse_new_league_csv
from app.ingestion.oddsportal import OddsPortalLoader
from app.models.football_dc import DixonColesFootballModel
from app.notifications.base import Alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import PipelineDeps, run_pick_pipeline
from app.probabilities.devig import DevigMethod
from app.risk.exposure import DailyExposureLedger


class PrintSink:
    name = "stdout"

    async def send(self, alert: Alert) -> bool:
        print("\n" + "=" * 62)
        print(alert.body)
        print("=" * 62)
        return True


class StaticLoader:
    def __init__(self, snapshots: list) -> None:  # noqa: ANN001
        self._snapshots = snapshots

    async def fetch_odds(self, sport_key: str) -> list:  # noqa: ANN001
        return self._snapshots


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", default="BRA", help="football-data new-league code")
    parser.add_argument("--slug", default="brazil-serie-a", help="oddsportal league slug")
    parser.add_argument("--min-edge", type=float, default=None, help="override MIN_EDGE")
    parser.add_argument("--min-ev", type=float, default=None, help="override MIN_EV")
    parser.add_argument("--persist", action="store_true", help="write picks to the DB")
    args = parser.parse_args()

    logging.basicConfig(level="ERROR", format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    directory = EventDirectory()

    async with httpx.AsyncClient() as client:
        print(f"\n[1/4] HISTORICAL DATA — football-data.co.uk new/{args.code}.csv ...")
        text = await fetch_new_league_csv(client, args.code)
        rows = parse_new_league_csv(text)
        if not rows:
            print(f"      no historical matches parsed for {args.code} — aborting")
            return
        print(
            f"      {len(rows)} historical matches ({rows[0].match_date} .. {rows[-1].match_date})"
        )

        print("\n[2/4] MODEL — fitting penaltyblog Dixon-Coles (used directly) ...")
        model = DixonColesFootballModel(
            directory,
            confidence=settings.model_confidence,
            totals_line=settings.football_totals_line,
        )
        await asyncio.to_thread(model.fit, rows, datetime.now(tz=UTC).date())
        print(f"      fitted on {len(model._trained)} teams")

        print(f"\n[3/4] LIVE ODDS — scraping oddsportal '{args.slug}' via OddsHarvester ...")
        loader = OddsPortalLoader(
            directory=directory,
            leagues_by_sport_key={"soccer": ("football", [args.slug])},
            markets=("1x2",),
        )
        snapshots = await loader.fetch_odds("soccer")
        events = {s.event_id for s in snapshots}
        print(f"      {len(snapshots)} live odds snapshots across {len(events)} matches")
        resolved = sum(
            1
            for e in events
            if (t := directory.lookup(e))
            and model.resolve_team(t.home)
            and model.resolve_team(t.away)
        )
        print(f"      {resolved}/{len(events)} matches resolved to trained teams")

        print("\n[4/4] PIPELINE — devig -> edge gates -> fractional Kelly -> alert ...")
        engine = create_engine(settings) if args.persist else None
        session_factory = create_session_factory(engine) if engine is not None else None
        gates = GatePolicy(
            min_edge=args.min_edge if args.min_edge is not None else settings.min_edge,
            min_ev=args.min_ev if args.min_ev is not None else settings.min_ev,
            min_confidence=settings.min_confidence,
            max_odds_age_seconds=1e12,  # scraped odds carry oddsportal's own time
            min_liquidity=0.0,  # the scraper provides no liquidity data — a
            # configured MIN_LIQUIDITY > 0 would silently reject every pick
        )
        deps = PipelineDeps(
            loader=StaticLoader(snapshots),
            model=model,
            dispatcher=AlertDispatcher([PrintSink()], InMemoryIdempotencyStore()),
            gate_policy=gates,
            stake_policy=stake_policy(settings),
            ledger=DailyExposureLedger(max_daily_fraction=settings.max_daily_exposure_percent),
            bankroll=Decimal(str(settings.bankroll_base)),
            devig_method=DevigMethod.POWER,
            sport="soccer",
            league=args.slug,
            directory=directory,
            session_factory=session_factory,
            model_name=model.name,
            model_version=model.version,
        )
        picks = await run_pick_pipeline(deps, "soccer")
        print(
            f"\nRESULT: {len(picks)} +EV pick(s) passed every gate "
            f"(edge>={gates.min_edge}, ev>={gates.min_ev}, conf>={gates.min_confidence})."
        )
        if args.persist:
            print("        picks persisted to DB (query /picks or the picks table)")
        if not picks:
            print("        No gate-passing edge right now — the honest, common case.")
            print("        Re-run with --min-edge 0 --min-ev -1 to see every priced market.")
        if engine is not None:
            await engine.dispose()
        print("\nManual review required. This system does not place bets.")


if __name__ == "__main__":
    asyncio.run(main())
