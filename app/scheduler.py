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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from redis.asyncio import Redis

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, exposure_ledger, gate_policy, stake_policy, value_policy
from app.ingestion.base import EventDirectory, EventTeams, OddsLoader
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
from app.models.value_filter import ValueFilterModel
from app.notifications.dedupe import RedisIdempotencyStore
from app.notifications.dispatcher import AlertDispatcher
from app.notifications.telegram import TelegramSink
from app.notifications.webhook import WebhookSink
from app.pipeline import PipelineDeps, run_pick_pipeline, run_value_pipeline
from app.probabilities.devig import DevigMethod
from app.risk.exposure import DailyExposureLedger

logger = logging.getLogger(__name__)


#: Interval-poll CAPTURE jobs whose max-instances skip is BY DESIGN (the interval
#: is a ceiling; an overrunning cycle coalesces the next slot). CRON jobs
#: (settle_results / refit / snapshot / upstream_watch) are NOT here — a skip
#: there is a real signal and stays a WARNING.
_CONTINUOUS_POLL_JOBS = (
    "poll_odds",
    "capture_pinnacle_arcadia",
    "capture_betfair_exchange",
    "capture_finished_scores",
)


class _PollSkipNoiseFilter(logging.Filter):
    """Downgrade apscheduler's max-instances skip warning for the interval-poll
    capture jobs (poll_odds, capture_pinnacle_arcadia, capture_betfair_exchange).

    The short interval + max_instances=1 + coalesce is the documented
    continuous-polling design: while a long capture cycle runs, every interval
    slot is skipped by design. Downgraded to INFO (not dropped) — the skip is the
    only scheduler-side evidence of a HUNG cycle. Skips of any OTHER (cron) job
    remain warnings.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "maximum number of running instances reached" in msg and any(
            job in msg for job in _CONTINUOUS_POLL_JOBS
        ):
            record.levelno = logging.INFO
            record.levelname = "INFO"
            # The logger's level gate already passed at WARNING before filters
            # ran; without re-checking, the downgraded line would still emit
            # under LOG_LEVEL=WARNING. Honour the effective level for the NEW
            # severity: emit at INFO, suppress at WARNING+.
            return logging.getLogger(record.name).isEnabledFor(record.levelno)
        return True


_POLL_SKIP_FILTER = _PollSkipNoiseFilter()


def _dispatcher(
    settings: Settings, http_client: httpx.AsyncClient, redis: Redis
) -> AlertDispatcher:
    return AlertDispatcher(
        sinks=[
            TelegramSink(
                settings.telegram_bot_token.get_secret_value(),
                settings.telegram_chat_id,
                http_client,
            ),
            WebhookSink(settings.webhook_url.get_secret_value(), http_client),
        ],
        # TTL governs how long an UNCHANGED market state stays quiet (a price
        # move mints a new dedupe key and alerts immediately) — default 7d so
        # still-open same-odds picks do not re-alert daily.
        store=RedisIdempotencyStore(redis, ttl_seconds=settings.alert_dedupe_ttl_seconds),
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


async def seed_exposure_ledger(
    ledger: DailyExposureLedger,
    session_factory: "async_sessionmaker",
) -> None:
    """Preload today's already-recommended exposure from persisted picks.

    The ledger is in-memory and rebuilt on every process start; without this
    a mid-day restart resets used(today) to ~0 (re-detections reserve then
    release as DB duplicates) and DOUBLES the day's recommendable exposure.
    ALL statuses count — each pick consumed budget when it was recommended
    today, whatever happened to it since. PREMIUM tier only: volume picks
    never reserve exposure (their stake fields are informational), so
    counting them would shrink the cap premium picks are entitled to. An
    upgraded volume pick gets created_at bumped to its upgrade moment
    (repositories.persist_pick), which is exactly when it DID reserve.
    """
    from sqlalchemy import func, select

    from app.storage.models import Pick

    now = datetime.now(tz=UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    async with session_factory() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(Pick.recommended_stake_fraction), 0)).where(
                Pick.created_at >= day_start,
                Pick.created_at < day_end,
                Pick.tier == "premium",
            )
        )
    used = float(total or 0)
    ledger.preload(now.date(), used)
    if used > 0.0:
        logger.info(
            "exposure ledger seeded for %s: %.4f of bankroll already recommended today",
            now.date().isoformat(),
            used,
        )


def build_scheduler(
    settings: Settings,
    http_client: httpx.AsyncClient,
    redis: Redis,
    session_factory: "async_sessionmaker | None" = None,
    ledger: DailyExposureLedger | None = None,
    arcadia_http_client: httpx.AsyncClient | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    loader: OddsLoader | None = None
    model: ProbabilityModel = NullModel()
    sport_keys: tuple[str, ...] = ()
    league_label = ""
    directory: EventDirectory | None = None
    # Sport keys that scrape for the AVAILABLE GAMES view only (no picks/
    # alerts) — populated by the oddsportal branch when tennis is enabled.
    visibility_only_sports: frozenset[str] = frozenset()
    # Unvalidated sports promoted to EXPERIMENTAL picks (volume/shadow tier only,
    # never alerted) when ENABLE_UNVALIDATED_PICKS is on; else they stay
    # visibility-only. Passed to PipelineDeps.experimental_sports.
    experimental_sports: frozenset[str] = frozenset()

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
            # Batch 3 DEMOTION (audit 2026-06-26): basketball has NOT cleared the
            # held-out > 2 SE per-sport CLV gate, so by default it is EXPERIMENTAL —
            # still scraped/minted/persisted/CLV-tracked/auto-settled AND shown, but
            # every pick is forced to the volume/shadow tier (never alerted, zero
            # exposure). Flip NBA_EXPERIMENTAL=false only after a deliberate,
            # ADR-logged promotion. Unlike tennis/NFL, basketball still MINTS picks
            # either way — the knob only governs whether they can alert.
            if settings.nba_experimental:
                experimental_sports = experimental_sports | frozenset({"basketball"})
        # Tennis is VISIBILITY-ONLY / UNVALIDATED (held-out CLV undefined — no
        # closing source; app/config.py). Enabled only when leagues are set
        # (OFF by default); it scrapes for the AVAILABLE GAMES view but mints
        # NO picks/alerts — enforced by visibility_only_sports below.
        tennis_leagues = _csv(settings.oddsportal_tennis_leagues)
        if tennis_leagues:
            config["tennis"] = ("tennis", tennis_leagues)
            markets_by["tennis"] = tuple(_csv(settings.oddsportal_tennis_markets))
            sport_keys = (*sport_keys, "tennis")
            if settings.enable_unvalidated_picks:
                experimental_sports = experimental_sports | frozenset({"tennis"})
            else:
                visibility_only_sports = visibility_only_sports | frozenset({"tennis"})
        # American football / NFL is ALSO VISIBILITY-ONLY / UNVALIDATED (its
        # forward Pinnacle-close archive is only now being captured via arcadia
        # sport-id 15; held-out CLV cannot be evaluated until it accrues —
        # app/config.py). Enabled when leagues are set (default "nfl"); scrapes
        # for AVAILABLE GAMES but mints NO picks/alerts (visibility_only_sports
        # below + the warehouse _VALIDATED_SPORT_PREFIXES). Upstream sport string
        # is "american-football"; slugs nfl/ncaa.
        nfl_leagues = _csv(settings.oddsportal_nfl_leagues)
        if nfl_leagues:
            config["american_football"] = ("american-football", nfl_leagues)
            markets_by["american_football"] = tuple(_csv(settings.oddsportal_nfl_markets))
            sport_keys = (*sport_keys, "american_football")
            if settings.enable_unvalidated_picks:
                experimental_sports = experimental_sports | frozenset({"american_football"})
            else:
                visibility_only_sports = visibility_only_sports | frozenset({"american_football"})
        loader = OddsPortalLoader(
            directory=directory,
            leagues_by_sport_key=config,
            markets_by_sport_key=markets_by,
            # dated scrapes (today..today+N UTC) — the actionable slate only
            days_ahead=settings.oddsportal_days_ahead,
            concurrency_tasks=settings.oddsportal_concurrency,
            request_delay=settings.oddsportal_request_delay,
            locale=settings.oddsportal_locale,
            proxy_pool=settings.scraper_proxies(),
            # Raise OddsHarvester's hardcoded 15s match-page goto timeout so
            # fewer heavy OddsPortal pages time out (read-only; see oddsportal.py).
            nav_timeout_ms=settings.scrape_nav_timeout_ms,
            # HARD per-scrape-pass watchdog: a hung Over/Under extraction can
            # never make poll_odds run forever (and so starve settle_results) —
            # each scrape pass is bounded and cancelled on timeout (prod fix).
            cycle_timeout_seconds=settings.scrape_cycle_timeout_seconds,
            # SELECTABLE curl_cffi JSON-feed per-match odds (OFF by default). When
            # on, the listing runs markets=[] (no per-match Playwright odds) and a
            # per-match JSON failure is a scrape gap — NO Playwright odds fallback
            # (operator 2026-06-23). See config.py.
            use_json_feed=settings.oddsportal_use_json_feed,
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
            loader = OddsApiClient(
                api_keys=keys, client=http_client, regions=settings.odds_api_regions
            )
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
        # Value-filter meta-model (v1 verdict ADOPT — held-out evidence cited
        # in app/config.py; the v2 retrain is a SHADOW-CANDIDATE loadable
        # only via VALUE_ML_MANIFEST_ALLOW_SHADOW, annotation-only). Loading
        # is best-effort: missing artifacts or ML deps leave value_filter=
        # None and the pipeline runs unfiltered. When the ENFORCEMENT flag
        # is on but nothing loaded, fail LOUDLY at composition time — a
        # silently absent filter must not masquerade as an active one.
        value_filter = (
            ValueFilterModel.load(
                Path(settings.value_ml_model_dir),
                manifest_filename=settings.value_ml_manifest_filename,
                model_filename=settings.value_ml_model_filename,
                allow_shadow=settings.value_ml_manifest_allow_shadow,
            )
            if use_value
            else None
        )
        ml_filter_enforced = settings.value_ml_filter
        if ml_filter_enforced and value_filter is None:
            logger.error(
                "VALUE_ML_FILTER=true but no value-filter model loaded from %s "
                "(missing artifacts or ML deps?) — picks run UNFILTERED",
                settings.value_ml_model_dir,
            )
        if ml_filter_enforced and value_filter is not None and value_filter.shadow:
            # Enforcement requires a true ADOPT manifest. A SHADOW-CANDIDATE
            # (verdict != ADOPT, loaded via VALUE_ML_MANIFEST_ALLOW_SHADOW)
            # may only annotate — demotion on unproven evidence is forbidden
            # by the spent-holdout discipline (.claude/memory/decisions.md).
            logger.error(
                "VALUE_ML_FILTER=true but manifest %s is a SHADOW-CANDIDATE "
                "(verdict != ADOPT) — enforcement refused; running ANNOTATION-ONLY",
                settings.value_ml_manifest_filename,
            )
            ml_filter_enforced = False
        # OPTIONAL: anchor live picks on the free Betfair/Pinnacle sharp prices
        # (VALUE_SHARP_ANCHOR_FROM_ARCHIVES) instead of the soft-book consensus —
        # merging the captured archives into the anchor set at pick time. Default
        # OFF; needs the archives populated (BETFAIR_EXCHANGE_ENABLED/ARCADIA).
        sharp_anchor_loader = None
        if (
            use_value
            and settings.value_sharp_anchor_from_archives
            and session_factory is not None
            and directory is not None
        ):
            from app.clv_trueup import build_sharp_anchor_loader

            sharp_anchor_loader = build_sharp_anchor_loader(
                session_factory,
                directory,
                use_betfair=settings.betfair_exchange_enabled,
                use_pinnacle=settings.arcadia_enabled,
                # EVENT-WIDE freshness: keep an event's sharp anchor unless it
                # FELL OUT of capture (no row in 4h). Generous so a STEADY liquid
                # price (change-only persistence -> old row, still current) is NOT
                # dropped; only a stale/abandoned event is (review 2026-06-21).
                max_age_seconds=14400.0,
            )
            logger.info(
                "LIVE sharp-anchor injection ENABLED (betfair=%s pinnacle=%s) — picks "
                "anchor on the free sharp price where available, not consensus",
                settings.betfair_exchange_enabled,
                settings.arcadia_enabled,
            )
        deps = PipelineDeps(
            loader=loader,
            model=model,
            dispatcher=_dispatcher(settings, http_client, redis),
            gate_policy=gate_policy(settings),
            stake_policy=stake_policy(settings),
            # seeded at the composition root (app/main.py lifespan) so a
            # restart cannot forget today's already-recommended exposure
            ledger=ledger if ledger is not None else exposure_ledger(settings),
            bankroll=Decimal(str(settings.bankroll_base)),
            league=league_label,
            directory=directory,
            session_factory=session_factory,
            model_name="value-sharp-vs-soft" if use_value else model.name,
            model_version="v3" if use_value else model.version,
            devig_method=value_devig if use_value else DevigMethod.POWER,
            value_min_edge=settings.value_min_edge,
            # volume (shadow) tier floor; == value_min_edge disables it
            value_volume_min_edge=settings.value_volume_min_edge,
            value_min_odds=settings.value_min_odds,
            # optional per-market/odds-band/book-count refinements — the
            # default (all env knobs empty) is the all-empty no-op policy
            value_policy=value_policy(settings),
            value_filter=value_filter,
            value_ml_filter_enabled=ml_filter_enforced,
            # tennis/NFL: visibility-only by default, or EXPERIMENTAL (volume/
            # shadow picks, never alerted) when ENABLE_UNVALIDATED_PICKS is on.
            visibility_only_sports=visibility_only_sports,
            experimental_sports=experimental_sports,
            sharp_anchor_loader=sharp_anchor_loader,
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
        # A cycle outlasting the interval is the design (continuous polling),
        # so the per-slot skip warnings are noise; addFilter is idempotent
        # for the same instance.
        logging.getLogger("apscheduler.scheduler").addFilter(_POLL_SKIP_FILTER)

        # CLV true-up + current-odds revalidation now run INSIDE every poll
        # cycle on the cycle's own snapshots (app/pipeline.py) — a separate
        # 30-min fetch job would just double the scraping load.

    async def capture_finished_scores_job() -> None:
        # DEDICATED finished-score scrape (cactusbets.cloud prod fix, 2026-06-22).
        # Runs on its OWN light interval (RESULTS_SCRAPE_INTERVAL_SECONDS),
        # SEPARATE from the heavy odds-polling pass, so finished scores land —
        # and picks settle — promptly even when a full odds cycle is slow. The
        # old design welded this scrape onto the hourly settle_results cron; one
        # slow league there starved every later run (max_instances=1) and scores
        # never committed. Each finished link is now scraped + committed
        # individually under a per-link timeout, with a per-cycle time budget, so
        # a single hung VPS proxy request cannot stall the pass. OddsPortalLoader
        # only (others no-op via the fetch_match_odds duck-type). Per-sport error
        # isolation: one sport failing never blocks the rest.
        if (
            session_factory is None
            or not settings.settle_from_scraped_scores
            or loader is None
            or directory is None
        ):
            return
        from app.clv_trueup import capture_finished_scores

        for sport_key in sport_keys:
            try:
                await capture_finished_scores(
                    loader,
                    session_factory,
                    directory,
                    sport_key,
                    window=timedelta(days=settings.results_scrape_window_days),
                    per_link_timeout=settings.results_scrape_link_timeout_seconds,
                    time_budget=settings.results_scrape_cycle_budget_seconds,
                )
            except Exception as exc:
                logger.error("results scrape failed for %s: %s", sport_key, type(exc).__name__)

    async def settle_results() -> None:
        # Phase 4: free results sources -> outcome mapping -> result_tracking.
        # Leagues without a free results feed (nba, euroleague) settle
        # manually via POST /events/{id}/result on the dashboard.
        #
        # The finished-score SCRAPE now runs in its OWN job
        # (capture_finished_scores_job) on a lighter, more-frequent interval —
        # this job only CONSUMES the Event.scraped_* scores it already committed
        # (run_settlement_cycle reads them from the DB independently). Decoupling
        # the two means a slow scrape can never starve settlement, and settlement
        # never blocks the scrape.
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
                use_pinnacle_archive=settings.clv_use_pinnacle_archive,
                use_betfair_exchange=settings.clv_use_betfair_exchange,
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

    async def run_self_audit_job() -> None:
        # Runtime self-audit: cheap READ-ONLY DB anomaly checks (awaiting-result
        # backlog, stale odds) that WARN/ERROR so the health monitor catches
        # operational problems proactively. Never raises (self_audit_job guards).
        if session_factory is None:
            return
        from app.maintenance.self_audit import self_audit_job

        await self_audit_job(session_factory)

    scheduler.add_job(
        settle_results,
        # Short interval (was hourly CronTrigger(minute=15)): settle_results only
        # reads scraped scores from the DB and settles — cheap, no scrape — so a
        # freshly-captured FINAL score settles within ~1 cycle instead of up to
        # an hour. Paired with the 60s finished-score capture, a result lands
        # within ~1-2 min of FT.
        IntervalTrigger(seconds=settings.settle_interval_seconds),
        id="settle_results",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,  # run on Mac wake, don't skip
    )
    if session_factory is not None:
        scheduler.add_job(
            run_self_audit_job,
            IntervalTrigger(seconds=settings.self_audit_interval_seconds),
            id="self_audit",
            max_instances=1,
            coalesce=True,
        )
    # DEDICATED finished-score scrape on its OWN light interval, decoupled from
    # both the heavy odds poll and the hourly settle cron (cactusbets.cloud prod
    # fix). Only registered when the source can actually scrape match pages
    # (oddsportal loader + directory + DB) and the feature is on; otherwise the
    # job would be a no-op every interval. max_instances=1 + coalesce: an
    # overrunning scrape coalesces the next slot (its per-cycle budget bounds the
    # run). A DateTrigger run at startup clears any existing backlog promptly.
    if (
        session_factory is not None
        and settings.settle_from_scraped_scores
        and loader is not None
        and directory is not None
    ):
        scheduler.add_job(
            capture_finished_scores_job,
            IntervalTrigger(seconds=settings.results_scrape_interval_seconds),
            id="capture_finished_scores",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,  # run on Mac wake, don't skip
        )
        scheduler.add_job(
            capture_finished_scores_job, DateTrigger(), id="capture_finished_scores_initial"
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

    # Independent read-only Pinnacle sharp-line ARCHIVE capture (ADR-0013).
    # Runs ALONGSIDE the active odds_source (never replaces it, mints no picks):
    # persists period-0 moneyline closes under the isolated `pinnacle_<sport>`
    # namespace so closing_odds_from_snapshots can reconstruct a true sharp
    # close. OFF unless ARCADIA_ENABLED=true and a DB session factory exists.
    if settings.arcadia_enabled and session_factory is not None:
        from app.ingestion.pinnacle_arcadia import (
            PinnacleArcadiaCapture,
            PinnacleArcadiaClient,
            discover_arcadia_config,
        )

        arcadia_client = PinnacleArcadiaClient(
            arcadia_http_client or http_client,
            base_url=settings.arcadia_base_url,
            guest_key=settings.arcadia_guest_key.get_secret_value(),
        )
        arcadia_capture = PinnacleArcadiaCapture(
            client=arcadia_client,
            session_factory=session_factory,
            sports=tuple(_csv(settings.arcadia_sports)),
            horizon=timedelta(hours=settings.arcadia_horizon_hours),
        )
        # Best-effort PUBLIC key/base discovery, opt-in (default OFF -> this
        # whole block is skipped and the path is byte-identical). Done once,
        # lazily, on the first job run (build_scheduler is sync; discovery is
        # async). On ANY failure discover_arcadia_config returns None and we
        # keep the configured key + base URL — never aborts the capture cycle.
        discover_done = {"ran": False}

        async def _maybe_discover_arcadia_config() -> None:
            if not settings.arcadia_discover_config or discover_done["ran"]:
                return
            discover_done["ran"] = True
            config = await discover_arcadia_config(arcadia_http_client or http_client)
            if config is not None:
                arcadia_client.apply_config(config)

        async def capture_pinnacle_arcadia() -> None:
            try:
                await _maybe_discover_arcadia_config()
                await arcadia_capture.capture_once()
            except Exception as exc:
                logger.error("pinnacle arcadia capture failed: %s", type(exc).__name__)

        scheduler.add_job(
            capture_pinnacle_arcadia,
            IntervalTrigger(seconds=settings.arcadia_poll_interval_seconds),
            id="capture_pinnacle_arcadia",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,  # run on Mac wake, don't skip
        )
        logger.info(
            "pinnacle arcadia sharp-line archive ENABLED (read-only) for sports=%s",
            settings.arcadia_sports,
        )

    # Independent read-only Betfair Exchange BACK-odds ARCHIVE capture
    # (ADR-0015). Like arcadia, it runs ALONGSIDE the active odds_source, mints
    # NO picks/alerts, and persists into the isolated `betfair_<sport>`
    # namespace (bookmaker="Betfair Exchange"). OFF unless
    # BETFAIR_EXCHANGE_ENABLED=true, the source is oddsportal, and a DB session
    # factory exists.
    #
    # TARGETS — DB-SOURCED, BOUNDED, ROTATING (CPU-aware, prod fix 2026-06-23).
    # Previously _betfair_targets re-read loader.last_fetch_event_ids[sport],
    # which the OddsPortal scrape populates ONLY when a full multi-league scrape
    # COMPLETES. On the CPU-bound box one slow scrape held poll_odds's single
    # slot (poll_odds_completions=0 / skips=12 in an hour), so that map stayed
    # empty -> the reader got NO targets -> captured nothing, even a £270k-liquid
    # major. select_betfair_targets reads the WAREHOUSE instead (recent upcoming
    # events with odds that haven't kicked off), so the capture is DECOUPLED from
    # full-scrape completion. It is its own scheduler job (its own browser/slot),
    # so this only changes where its URLs come from. BOUNDED to
    # BETFAIR_EXCHANGE_MAX_TARGETS_PER_CYCLE per cycle and ROTATING
    # (never-captured first, then stalest Betfair capture), so the reader NEVER
    # opens all ~91 pages at once and the slate is swept over cycles.
    #
    # Per-sport: BETFAIR_EXCHANGE_SPORTS is a csv ("soccer", or
    # "soccer,basketball"); each sport's targets are its canonical DB events
    # (Sport.key == the sport key), so a betfair sport only sees fixtures once
    # its OddsPortal scrape has priced them (e.g. basketball needs
    # ODDSPORTAL_BASKETBALL_LEAGUES). The reader reads 3 BACK cells for soccer
    # (1X2) and 2 for basketball (moneyline); each keeps its own betfair_<sport>
    # namespace + error isolation.
    if (
        settings.betfair_exchange_enabled
        and settings.odds_source == "oddsportal"
        and directory is not None
        and session_factory is not None
        and isinstance(loader, OddsPortalLoader)
    ):
        from app.ingestion.betfair_exchange import (
            BetfairExchangeCapture,
            BetfairExchangeReader,
            MatchTarget,
        )
        from app.storage.repositories import select_betfair_targets

        bfx_session_factory = session_factory
        bfx_target_limit = settings.betfair_exchange_max_targets_per_cycle
        bfx_target_window = timedelta(hours=settings.betfair_exchange_target_window_hours)

        async def _betfair_targets(sport: str) -> list[MatchTarget]:
            # DB-sourced (decoupled from full-scrape completion): bounded +
            # rotating canonical events for THIS sport. The event external_ref IS
            # the OddsPortal match-link URL, and team/league context comes from
            # the same warehouse rows the main scrape wrote.
            rows = await select_betfair_targets(
                bfx_session_factory,
                sport=sport,
                window=bfx_target_window,
                limit=bfx_target_limit,
            )
            return [
                MatchTarget(
                    event_id=row.external_ref,
                    url=row.external_ref,
                    teams=EventTeams(
                        home=row.home,
                        away=row.away,
                        league=row.league,
                        starts_at=row.starts_at,
                    ),
                )
                for row in rows
            ]

        betfair_capture = BetfairExchangeCapture(
            reader=BetfairExchangeReader(
                min_liquidity=settings.betfair_exchange_min_liquidity,
                proxy_pool=settings.scraper_proxies(),
                locale=settings.oddsportal_locale,
            ),
            session_factory=session_factory,
            targets_fn=_betfair_targets,
            sports=tuple(_csv(settings.betfair_exchange_sports)),
        )

        async def capture_betfair_exchange() -> None:
            try:
                await betfair_capture.capture_once()
            except Exception as exc:
                logger.error("betfair exchange capture failed: %s", type(exc).__name__)

        scheduler.add_job(
            capture_betfair_exchange,
            IntervalTrigger(seconds=settings.betfair_exchange_poll_interval_seconds),
            id="capture_betfair_exchange",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,  # run on Mac wake, don't skip
        )
        logger.info(
            "betfair exchange BACK-odds archive ENABLED (read-only) for sports=%s",
            settings.betfair_exchange_sports,
        )

    return scheduler


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]
