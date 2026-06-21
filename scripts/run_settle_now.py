"""One-shot: capture finished picks' scores from their match pages, then settle.

Mirrors the scheduler's settle_results job (capture_finished_scores ->
run_settlement_cycle) so results + ROI can be populated on demand instead of
waiting for the hourly cron. Read-only market data; records outcomes only.
Run: uv run python scripts/run_settle_now.py
"""

import asyncio

import httpx

from app.clv_trueup import capture_finished_scores
from app.config import get_settings
from app.database import create_engine, create_session_factory
from app.ingestion.base import EventDirectory
from app.ingestion.oddsportal import OddsPortalLoader
from app.settlement.engine import run_settlement_cycle


def _csv(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


async def main() -> None:
    s = get_settings()
    session_factory = create_session_factory(create_engine(s))
    directory = EventDirectory()
    config = {"soccer": ("football", _csv(s.oddsportal_football_leagues))}
    markets_by = {"soccer": tuple(_csv(s.oddsportal_football_markets))}
    if _csv(s.oddsportal_basketball_leagues):
        config["basketball"] = ("basketball", _csv(s.oddsportal_basketball_leagues))
        markets_by["basketball"] = tuple(_csv(s.oddsportal_basketball_markets))
    loader = OddsPortalLoader(
        directory=directory,
        leagues_by_sport_key=config,
        markets_by_sport_key=markets_by,
        days_ahead=s.oddsportal_days_ahead,
        concurrency_tasks=s.oddsportal_concurrency,
        request_delay=s.oddsportal_request_delay,
        locale=s.oddsportal_locale,
        proxy_pool=s.scraper_proxies(),
    )
    for sk in config:
        try:
            n = await capture_finished_scores(loader, session_factory, directory, sk)
            print(f"capture_finished_scores[{sk}] -> {n} score(s) written", flush=True)
        except Exception as exc:  # noqa: BLE001 - report + continue
            print(f"capture_finished_scores[{sk}] FAILED: {type(exc).__name__}", flush=True)
    async with httpx.AsyncClient(timeout=30) as client:
        settled = await run_settlement_cycle(
            client,
            session_factory,
            slugs=_csv(s.oddsportal_football_leagues),
            seasons=_csv(s.footballdata_seasons),
            use_pinnacle_archive=s.clv_use_pinnacle_archive,
            use_betfair_exchange=s.clv_use_betfair_exchange,
        )
    print(f"run_settlement_cycle -> settled {settled} pick(s)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
