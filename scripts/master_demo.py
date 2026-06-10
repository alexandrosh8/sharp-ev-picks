"""Master-app end-to-end demo: the proven engines bound together.

    uv run python scripts/master_demo.py                # full run (network)
    uv run python scripts/master_demo.py --skip-scrape  # model-only (faster)
    uv run python scripts/master_demo.py --league brazil-serie-a

Flow:
  1. download football-data.co.uk history (free) and fit penaltyblog's
     Dixon-Coles directly (ADR-0011),
  2. price a real fixture from the fitted model (1X2 / totals / BTTS),
  3. scrape FREE live (pre-match) odds from oddsportal.com via OddsHarvester,
  4. run the full pick pipeline: devig -> edge gates -> fractional Kelly ->
     alert rendering (printed, not sent).

Decision-support only: nothing here (or anywhere) places bets.
"""

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from app.config import gate_policy, get_settings, stake_policy
from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal import OddsPortalLoader
from app.models.football_dc import DixonColesFootballModel
from app.notifications.base import Alert, build_pick_alert
from app.notifications.dedupe import InMemoryIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.pipeline import PipelineDeps, run_pick_pipeline
from app.risk.exposure import DailyExposureLedger
from app.scheduler import fetch_football_history


class PrintSink:
    name = "stdout"

    async def send(self, alert: Alert) -> bool:
        print("\n--- PICK ALERT ---------------------------------------------")
        print(alert.body)
        print("------------------------------------------------------------")
        return True


class StaticLoader:
    """Replays already-scraped snapshots into the pipeline (scrape once)."""

    def __init__(self, snapshots: list) -> None:  # noqa: ANN001 - demo helper
        self._snapshots = snapshots

    async def fetch_odds(self, sport_key: str) -> list:  # noqa: ANN001
        return self._snapshots


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", default="england-premier-league", help="oddsportal slug")
    parser.add_argument("--skip-scrape", action="store_true", help="skip the live scrape step")
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    directory = EventDirectory()

    async with httpx.AsyncClient() as client:
        # 1. Fit Dixon-Coles on free football-data history (penaltyblog direct)
        print(
            f"\n[1/4] fetching football-data history "
            f"({settings.footballdata_league_codes} / {settings.footballdata_seasons})..."
        )
        rows = await fetch_football_history(settings, client)
        print(f"      {len(rows)} historical matches")
        model = DixonColesFootballModel(
            directory,
            confidence=settings.model_confidence,
            totals_line=settings.football_totals_line,
        )
        await asyncio.to_thread(model.fit, rows, datetime.now(tz=UTC).date())
        print("      Dixon-Coles fitted (penaltyblog, used directly)")

        # 2. Price a real fixture from the trained pool
        sample = rows[-1]
        grid_home, grid_away = sample.home_team, sample.away_team
        from app.ingestion.base import EventTeams

        directory.register("demo-fixture", EventTeams(home=grid_home, away=grid_away))
        preds = await model.predict("demo-fixture")
        print(f"\n[2/4] model prices for {grid_home} vs {grid_away}:")
        for p in preds:
            print(f"      {p.market:>8} | {p.selection:<14} -> {p.probability:.3f}")

        # 3. Scrape FREE live (pre-match) odds from oddsportal via OddsHarvester
        snapshots = []
        if args.skip_scrape:
            print("\n[3/4] scrape skipped (--skip-scrape)")
        else:
            print(f"\n[3/4] scraping oddsportal ({args.league}) via OddsHarvester...")
            loader = OddsPortalLoader(
                directory=directory,
                leagues_by_sport_key={"soccer": ("football", [args.league])},
            )
            try:
                snapshots = await loader.fetch_odds("soccer")
            except Exception as exc:
                print(f"      scrape failed: {type(exc).__name__}: {exc}")
            print(f"      {len(snapshots)} odds snapshots scraped")
            for snap in snapshots[:6]:
                print(
                    f"      {snap.bookmaker:<14} {snap.market:>7} "
                    f"{snap.selection:<22} @ {snap.decimal_odds}"
                )

        # 4. Full pipeline: devig -> gates -> Kelly -> alerts (printed)
        print("\n[4/4] running pick pipeline (devig -> gates -> Kelly -> alert)...")
        deps = PipelineDeps(
            loader=StaticLoader(snapshots),
            model=model,
            dispatcher=AlertDispatcher([PrintSink()], InMemoryIdempotencyStore()),
            gate_policy=gate_policy(settings),
            stake_policy=stake_policy(settings),
            ledger=DailyExposureLedger(max_daily_fraction=settings.max_daily_exposure_percent),
            bankroll=Decimal(str(settings.bankroll_base)),
            league=args.league,
        )
        picks = await run_pick_pipeline(deps, "soccer")
        print(f"\nRESULT: {len(picks)} +EV pick(s) passed every gate.")
        if not picks and snapshots:
            print("        (no gate-passing edge right now — that is the honest, common case)")
        if not snapshots:
            print("        (no live matches scraped — off-season league or scrape skipped)")
        print("\nManual review required. This system does not place bets.")
        # demonstrate the alert rendering even when no live pick fired
        if not picks and preds:
            print("\nSample alert rendering (model-priced demo fixture, synthetic odds):")
            from app.schemas.base import Market
            from app.schemas.picks import PickOut, StakeBreakdownOut

            sample_pick = PickOut(
                pick_id="demo",
                sport="soccer",
                league=args.league,
                event=f"{grid_home} vs {grid_away}",
                event_id="demo-fixture",
                market=Market.H2H,
                selection=grid_home,
                bookmaker="demo-book",
                decimal_odds=2.10,
                model_probability=preds[0].probability,
                fair_probability=0.50,
                edge=preds[0].probability - 0.50,
                ev=preds[0].probability * 1.10 - (1 - preds[0].probability),
                confidence=settings.model_confidence,
                recommended_stake_fraction=0.02,
                recommended_stake_amount=Decimal("20.00"),
                stake_breakdown=StakeBreakdownOut(
                    raw_kelly=0.1, fractional=0.025, capped=True, final=0.02
                ),
                odds_age_seconds=0.0,
                liquidity=None,
                reason_summary="demo rendering only",
                created_at=datetime.now(tz=UTC),
            )
            print(build_pick_alert(sample_pick).body)


if __name__ == "__main__":
    asyncio.run(main())
