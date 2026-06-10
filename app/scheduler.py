"""APScheduler job registry (ADR-0007: AsyncIOScheduler, single process).

Jobs are registered through `build_scheduler` so a future move to another
orchestrator touches only this module. The poll job only runs when an odds
source is configured; with the NullModel it produces no picks (fail-safe).
"""

import logging
from decimal import Decimal

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from redis.asyncio import Redis

from app.config import Settings, gate_policy, stake_policy
from app.ingestion.odds_api import OddsApiClient
from app.models.base import NullModel
from app.notifications.dedupe import RedisIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.notifications.telegram import TelegramSink
from app.notifications.webhook import WebhookSink
from app.pipeline import PipelineDeps, run_pick_pipeline
from app.risk.exposure import DailyExposureLedger

logger = logging.getLogger(__name__)

DEFAULT_SPORT_KEYS = ("soccer_epl", "basketball_nba")
POLL_INTERVAL_SECONDS = 300  # credit-frugal default; tighten near kickoff later


def build_scheduler(
    settings: Settings,
    http_client: httpx.AsyncClient,
    redis: Redis,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    keys = settings.odds_api_keys()
    if keys:
        deps = PipelineDeps(
            loader=OddsApiClient(api_keys=keys, client=http_client),
            model=NullModel(),  # replaced by trained engines in phases 3/5
            dispatcher=AlertDispatcher(
                sinks=[
                    TelegramSink(
                        settings.telegram_bot_token, settings.telegram_chat_id, http_client
                    ),
                    WebhookSink(settings.webhook_url, http_client),
                ],
                store=RedisIdempotencyStore(redis),
            ),
            gate_policy=gate_policy(settings),
            stake_policy=stake_policy(settings),
            ledger=DailyExposureLedger(max_daily_fraction=settings.max_daily_exposure_percent),
            bankroll=Decimal(str(settings.bankroll_base)),
        )

        async def poll_odds() -> None:
            for sport_key in DEFAULT_SPORT_KEYS:
                try:
                    await run_pick_pipeline(deps, sport_key)
                except Exception as exc:
                    logger.error("poll_odds failed for %s: %s", sport_key, type(exc).__name__)

        scheduler.add_job(
            poll_odds,
            IntervalTrigger(seconds=POLL_INTERVAL_SECONDS),
            id="poll_odds",
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.warning("no odds API keys configured; poll_odds job not scheduled")

    async def settle_results() -> None:
        # Roadmap phase 4/6: load results, settle picks, fill CLV columns.
        logger.info("settle_results: settlement engine arrives in roadmap phase 4")

    async def snapshot_bankroll() -> None:
        # Roadmap phase 6: persist bankroll_snapshots from manual entries.
        logger.info("snapshot_bankroll: bankroll tracking arrives in roadmap phase 6")

    scheduler.add_job(settle_results, CronTrigger(minute=15), id="settle_results")
    scheduler.add_job(snapshot_bankroll, CronTrigger(hour=0, minute=30), id="snapshot_bankroll")
    return scheduler
