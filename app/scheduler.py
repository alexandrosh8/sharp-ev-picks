"""APScheduler job registry (ADR-0007: AsyncIOScheduler, single process).

The master app binds the proven engines together here (ADR-0011/0012):
- odds_source="oddsportal" (default, free): OddsHarvester scrapes
  oddsportal.com -> OddsPortalLoader; penaltyblog Dixon-Coles prices the
  events (refit daily from football-data.co.uk history).
- odds_source="odds_api": The Odds API client; models plug in per phase.

Jobs are registered through `build_scheduler` so a future move to another
orchestrator touches only this module.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from redis.asyncio import Redis

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, gate_policy, stake_policy
from app.ingestion.base import EventDirectory, OddsLoader
from app.ingestion.football_data import (
    MatchRow,
    fetch_new_league_csv,
    fetch_season_csv,
    parse_new_league_csv,
    parse_season_csv,
)
from app.ingestion.odds_api import OddsApiClient
from app.ingestion.oddsportal import OddsPortalLoader
from app.models.base import NullModel, ProbabilityModel
from app.models.football_dc import DixonColesFootballModel
from app.notifications.dedupe import RedisIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.notifications.telegram import TelegramSink
from app.notifications.webhook import WebhookSink
from app.pipeline import PipelineDeps, run_pick_pipeline, run_value_pipeline
from app.probabilities.devig import DevigMethod
from app.risk.exposure import DailyExposureLedger

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 300  # fallback; settings.poll_interval_seconds wins


def _dispatcher(
    settings: Settings, http_client: httpx.AsyncClient, redis: Redis
) -> AlertDispatcher:
    return AlertDispatcher(
        sinks=[
            TelegramSink(settings.telegram_bot_token, settings.telegram_chat_id, http_client),
            WebhookSink(settings.webhook_url, http_client),
        ],
        store=RedisIdempotencyStore(redis),
    )


async def fetch_football_history(
    settings: Settings, http_client: httpx.AsyncClient
) -> list[MatchRow]:
    """Download configured football-data.co.uk history (free, Pinnacle close).

    Uses the "new leagues" all-seasons CSV when FOOTBALLDATA_NEW_LEAGUE_CODE is
    set (in-season non-European leagues), else the European mmz4281 seasons.
    """
    rows: list[MatchRow] = []
    new_code = settings.footballdata_new_league_code.strip()
    if new_code:
        try:
            text = await fetch_new_league_csv(http_client, new_code)
            rows.extend(parse_new_league_csv(text))
        except httpx.HTTPError as exc:
            logger.error("football-data new/%s fetch failed: %s", new_code, type(exc).__name__)
        return rows
    for code in _csv(settings.footballdata_league_codes):
        for season in _csv(settings.footballdata_seasons):
            try:
                text = await fetch_season_csv(http_client, code, season)
                rows.extend(parse_season_csv(text))
            except httpx.HTTPError as exc:
                logger.error(
                    "football-data fetch failed for %s/%s: %s", code, season, type(exc).__name__
                )
    return rows


def build_scheduler(
    settings: Settings,
    http_client: httpx.AsyncClient,
    redis: Redis,
    session_factory: "async_sessionmaker | None" = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    loader: OddsLoader | None = None
    model: ProbabilityModel = NullModel()
    sport_keys: tuple[str, ...] = ()
    league_label = ""
    directory: EventDirectory | None = None

    if settings.odds_source == "oddsportal":
        directory = EventDirectory()
        leagues = _csv(settings.oddsportal_football_leagues)
        config: dict[str, tuple[str, list[str]]] = {"soccer": ("football", leagues)}
        markets_by: dict[str, Sequence[str]] = {
            "soccer": tuple(_csv(settings.oddsportal_football_markets)),
        }
        sport_keys = ("soccer",)
        bb_leagues = _csv(settings.oddsportal_basketball_leagues)
        if bb_leagues:
            config["basketball"] = ("basketball", bb_leagues)
            markets_by["basketball"] = tuple(_csv(settings.oddsportal_basketball_markets))
            sport_keys = ("soccer", "basketball")
        loader = OddsPortalLoader(
            directory=directory,
            leagues_by_sport_key=config,
            markets_by_sport_key=markets_by,
            # dated scrapes (today..today+N UTC) — the actionable slate only
            days_ahead=settings.oddsportal_days_ahead,
            concurrency_tasks=settings.oddsportal_concurrency,
            request_delay=settings.oddsportal_request_delay,
            locale=settings.oddsportal_locale,
        )
        league_label = settings.oddsportal_football_leagues

        # The Dixon-Coles goals model only runs for pick_strategy="model"
        # (negative backtested CLV — screens only). The validated "value"
        # strategy needs no model and no daily refit.
        if settings.pick_strategy == "model":
            dc_model = DixonColesFootballModel(
                directory,
                confidence=settings.model_confidence,
                totals_line=settings.football_totals_line,
            )
            model = dc_model

            async def refit_football_model() -> None:
                rows = await fetch_football_history(settings, http_client)
                if len(rows) < 50:
                    logger.error("refit skipped: only %d historical matches fetched", len(rows))
                    return
                await asyncio.to_thread(dc_model.fit, rows, datetime.now(tz=UTC).date())

            scheduler.add_job(
                refit_football_model,
                DateTrigger(),  # once at startup
                id="refit_football_model_initial",
            )
            scheduler.add_job(
                refit_football_model,
                CronTrigger(hour=4, minute=10),
                id="refit_football_model",
            )
    elif settings.odds_source == "odds_api":
        keys = settings.odds_api_keys()
        if keys:
            loader = OddsApiClient(api_keys=keys, client=http_client)
            sport_keys = ("soccer_epl", "basketball_nba")
        else:
            logger.warning("odds_source=odds_api but no keys configured; polling disabled")
    else:
        logger.error("unknown odds_source %r; polling disabled", settings.odds_source)

    if loader is not None:
        use_value = settings.pick_strategy == "value"
        # v3 backtest chose the devig on TRAIN only (docs/backtesting/
        # value-findings.md); the same method prices the closing fair in the
        # CLV true-up so live CLV is comparable to the backtest.
        value_devig = DevigMethod(settings.value_devig)
        deps = PipelineDeps(
            loader=loader,
            model=model,
            dispatcher=_dispatcher(settings, http_client, redis),
            gate_policy=gate_policy(settings),
            stake_policy=stake_policy(settings),
            ledger=DailyExposureLedger(max_daily_fraction=settings.max_daily_exposure_percent),
            bankroll=Decimal(str(settings.bankroll_base)),
            league=league_label,
            directory=directory,
            session_factory=session_factory,
            model_name="value-sharp-vs-soft" if use_value else model.name,
            model_version="v3" if use_value else model.version,
            devig_method=value_devig if use_value else DevigMethod.POWER,
            value_min_edge=settings.value_min_edge,
            value_min_odds=settings.value_min_odds,
        )
        pipeline_fn = run_value_pipeline if use_value else run_pick_pipeline

        async def poll_odds() -> None:
            for sport_key in sport_keys:
                try:
                    await pipeline_fn(deps, sport_key)
                except Exception as exc:
                    logger.error("poll_odds failed for %s: %s", sport_key, type(exc).__name__)

        scheduler.add_job(
            poll_odds,
            IntervalTrigger(seconds=settings.poll_interval_seconds),
            id="poll_odds",
            max_instances=1,
            coalesce=True,
            # Laptop sleep makes jobs "miss" their slot; run them on wake
            # (coalesced to one run) instead of skipping to the next slot.
            misfire_grace_time=None,
        )

        # CLV true-up + current-odds revalidation now run INSIDE every poll
        # cycle on the cycle's own snapshots (app/pipeline.py) — a separate
        # 30-min fetch job would just double the scraping load.

    async def settle_results() -> None:
        # Phase 4: free results sources -> outcome mapping -> result_tracking.
        # Leagues without a free results feed (nba, euroleague) settle
        # manually via POST /events/{id}/result on the dashboard.
        if session_factory is None:
            logger.info("settle_results: no DB session factory; skipping")
            return
        from app.settlement.engine import run_settlement_cycle

        try:
            await run_settlement_cycle(
                http_client,
                session_factory,
                slugs=_csv(settings.oddsportal_football_leagues),
                seasons=_csv(settings.footballdata_seasons),
            )
        except Exception as exc:
            logger.error("settle_results failed: %s", type(exc).__name__)

    async def snapshot_bankroll() -> None:
        # Roadmap phase 6: persist bankroll_snapshots from manual entries.
        logger.info("snapshot_bankroll: bankroll tracking arrives in roadmap phase 6")

    upstream_dispatcher = _dispatcher(settings, http_client, redis)

    async def upstream_watch() -> None:
        # Daily PyPI release check for the bound engines (penaltyblog,
        # oddsharvester). Notifies once per release (Redis dedupe) and
        # surfaces on /health + dashboard; NEVER auto-installs — upgrades
        # go through scripts/upgrade_deps.sh (full test gate).
        from app.maintenance.upstream_watch import check_upstream, update_alert

        try:
            for notice in await check_upstream(http_client):
                await upstream_dispatcher.dispatch(update_alert(notice))
        except Exception as exc:
            logger.error("upstream watch failed: %s", type(exc).__name__)

    scheduler.add_job(
        settle_results,
        CronTrigger(minute=15),
        id="settle_results",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,  # run on Mac wake, don't skip
    )
    scheduler.add_job(snapshot_bankroll, CronTrigger(hour=0, minute=30), id="snapshot_bankroll")
    scheduler.add_job(
        upstream_watch,
        CronTrigger(hour=8, minute=5),
        id="upstream_watch",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,  # run on Mac wake, don't skip
    )
    scheduler.add_job(upstream_watch, DateTrigger(), id="upstream_watch_initial")
    return scheduler


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]
