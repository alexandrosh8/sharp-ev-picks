"""Settle open picks from a ScoreBook — the IO half of settlement.

Invariants (kestrel-settlement discipline):
- Refuse silent-empty: an empty score book settles NOTHING and logs loudly.
- Atomic per run: result_tracking insert + pick.status flip happen in the
  caller's transaction; the insert is idempotent (uq_result_tracking_pick).
- Never guess: missing scores, ambiguous team matches, and unparseable
  selections leave the pick open (manual settlement via the API still works).
- Settling flips status away from 'alerted', which freezes the pick's CLV
  (app/clv_trueup.py only touches alerted rows).
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.probabilities.devig import DevigMethod
from app.schemas.base import Outcome
from app.settlement.outcomes import pick_pnl, pick_roi, settle_selection
from app.settlement.results import FinalScore, ScoreBook, load_scores
from app.storage.models import Event, ManualBetLog, Pick, ResultTracking, Team

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

# How far back the score book reaches. Anything older than this with no
# score available needs manual settlement anyway.
SCORE_WINDOW = timedelta(days=14)

# Full time + stoppage + a buffer for the results CSVs to update. Scores are
# matched by date anyway; the delay just avoids settling in-play fixtures.
SETTLE_DELAY = timedelta(hours=2)

# Picks on events whose kickoff was NEVER reported (starts_at NULL, "TBD")
# can neither auto-settle (settle_open_picks filters NULL out) nor stop
# revalidating — without a deadline they consume off-window scrape slots
# forever. After this age (from pick creation) the off-window selector stops
# re-pricing them (app/clv_trueup.py) and void_stale_null_kickoff_picks
# below closes them out.
STALE_NULL_KICKOFF_AGE = timedelta(days=14)


async def void_stale_null_kickoff_picks(
    session: AsyncSession,
    now: datetime,
    max_age: timedelta = STALE_NULL_KICKOFF_AGE,
) -> int:
    """Void alerted picks whose event STILL has no kickoff after `max_age`.

    Terminal-state convention (same shape as score settlement): an idempotent
    result_tracking row — outcome 'void', stake returned, pnl 0 — plus the
    status flip to 'settled', which freezes CLV and drops the pick from
    revalidation. /performance already counts 'void' outcomes; no new
    vocabulary. Returns the number of picks voided. Caller owns the
    transaction.
    """
    cutoff = now - max_age
    rows = (
        (
            await session.execute(
                select(Pick)
                .join(Event, Pick.event_id == Event.id)
                .where(
                    Pick.status == "alerted",
                    Event.starts_at.is_(None),
                    Pick.created_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    voided = 0
    for pick in rows:
        stake, odds = await _stake_and_odds(session, pick)
        pnl = pick_pnl(Outcome.VOID, stake, odds)  # stake returned -> 0.00
        inserted = await session.execute(
            pg_insert(ResultTracking)
            .values(
                pick_id=pick.id,
                outcome=str(Outcome.VOID),
                pnl=pnl,
                roi=pick_roi(pnl, stake),
                settled_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_result_tracking_pick")
            .returning(ResultTracking.id)
        )
        if inserted.scalar_one_or_none() is None:
            continue  # already settled by a concurrent/manual path
        pick.status = "settled"
        logger.info(
            "voided pick %d (%s %s): kickoff still unknown %d days after pick "
            "creation — stake treated as returned",
            pick.id,
            pick.market,
            pick.selection,
            max_age.days,
        )
        voided += 1
    if voided:
        await session.flush()
        logger.info("settlement cycle: %d stale TBD picks voided", voided)
    return voided


async def settle_open_picks(
    session: AsyncSession,
    book: ScoreBook,
    now: datetime,
    delay: timedelta = SETTLE_DELAY,
    devig_method: DevigMethod | None = None,
    use_pinnacle_archive: bool = False,
    use_betfair_exchange: bool = False,
) -> int:
    """Settle every alerted pick whose event finished and has a known score.

    Returns the number of picks settled. The caller owns the transaction.

    Closing-line source preference: when `devig_method` is given, every pick
    that settles gets its closing fair/CLV recomputed from our OWN
    odds_snapshots change-only history (finalize_closing_from_snapshots) —
    same devig, same anchoring rules, effective odds both sides. When that
    finds no coverage (event not scraped near kickoff, no anchorable close
    set — the common case until snapshots accumulate), the pick KEEPS the
    close the live/match-page re-scrape revalidation last wrote: the
    fallback. `devig_method=None` skips the snapshot path entirely.
    """
    if len(book) == 0:
        logger.error("settlement: empty score book — refusing to settle (silent-empty guard)")
        return 0
    # Lazy import: app.clv_trueup imports STALE_NULL_KICKOFF_AGE from this
    # module at import time — a top-level import here would be circular.
    from app.clv_trueup import finalize_closing_from_snapshots

    home, away = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(Pick, home.name, away.name, Event.starts_at, Event.external_ref)
            .join(Event, Pick.event_id == Event.id)
            .join(home, Event.home_team_id == home.id)
            .join(away, Event.away_team_id == away.id)
            # NULL starts_at (kickoff unknown) is filtered out here by SQL
            # three-valued logic — correct: never auto-settle a game we
            # cannot prove has finished. Manual settlement stays available.
            .where(Pick.status == "alerted", Event.starts_at <= now - delay)
        )
    ).all()

    settled = 0
    for pick, home_name, away_name, starts_at, external_ref in rows:
        score = book.lookup(home_name, away_name, starts_at)
        if score is None:
            continue  # close_pending — stays open, retried next cycle
        if await _settle_one(
            session, pick, home_name, away_name, score.home_score, score.away_score, now
        ):
            settled += 1
            # Snapshot close AFTER the status flip, same transaction: the
            # pick is now frozen for revalidation, so what we write here is
            # final. A False return keeps the re-scrape close untouched.
            if devig_method is not None:
                await finalize_closing_from_snapshots(
                    session,
                    pick,
                    external_ref,
                    starts_at,
                    devig_method,
                    use_pinnacle_archive=use_pinnacle_archive,
                    use_betfair_exchange=use_betfair_exchange,
                )
    if settled:
        await session.flush()  # status flips visible to the caller's transaction
        logger.info("settlement cycle: %d picks settled", settled)
    return settled


async def settle_event_picks(
    session: AsyncSession,
    event_id: int,
    home_score: int,
    away_score: int,
    now: datetime,
) -> tuple[int, int]:
    """Settle every open pick of one event from a user-entered final score
    (the manual path for leagues without a free results feed).

    Returns (settled, skipped). The caller owns the transaction.
    """
    home, away = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(Pick, home.name, away.name)
            .join(Event, Pick.event_id == Event.id)
            .join(home, Event.home_team_id == home.id)
            .join(away, Event.away_team_id == away.id)
            .where(Pick.status == "alerted", Pick.event_id == event_id)
        )
    ).all()
    settled = skipped = 0
    for pick, home_name, away_name in rows:
        if await _settle_one(session, pick, home_name, away_name, home_score, away_score, now):
            settled += 1
        else:
            skipped += 1
    if settled:
        await session.flush()
    return settled, skipped


async def _settle_one(
    session: AsyncSession,
    pick: Pick,
    home_name: str,
    away_name: str,
    home_score: int,
    away_score: int,
    now: datetime,
) -> bool:
    """Atomic single-pick settlement: result row + status flip. False = skipped."""
    try:
        outcome = settle_selection(
            pick.market, pick.selection, home_name, away_name, home_score, away_score
        )
    except ValueError as exc:
        logger.warning("pick %d not settleable: %s", pick.id, exc)
        return False

    stake, odds = await _stake_and_odds(session, pick)
    pnl = pick_pnl(outcome, stake, odds)
    inserted = await session.execute(
        pg_insert(ResultTracking)
        .values(
            pick_id=pick.id,
            outcome=str(outcome),
            pnl=pnl,
            roi=pick_roi(pnl, stake),
            home_score=home_score,
            away_score=away_score,
            settled_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_result_tracking_pick")
        .returning(ResultTracking.id)
    )
    if inserted.scalar_one_or_none() is None:
        return False  # already settled by a concurrent/manual path
    pick.status = "settled"
    logger.info(
        "settled pick %d: %s %s -> %s (%d-%d)",
        pick.id,
        pick.market,
        pick.selection,
        outcome,
        home_score,
        away_score,
    )
    return True


async def _load_scraped_finals(session: AsyncSession, now: datetime) -> list[FinalScore]:
    """FinalScore rows from EVENTS that carry an OddsPortal-scraped final score,
    for still-open picks whose match has kicked off. Lets leagues with no free
    results feed AUTO-settle (no manual entry) from the score already fetched at
    scrape time. The score is on the pick's OWN event, so the ScoreBook matches
    it exactly by the same team names — no cross-source name risk."""
    home_t, away_t = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(
                home_t.name,
                away_t.name,
                Event.starts_at,
                Event.scraped_home_score,
                Event.scraped_away_score,
            )
            .join(Pick, Pick.event_id == Event.id)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .where(
                Pick.status == "alerted",
                Event.scraped_home_score.is_not(None),
                Event.scraped_away_score.is_not(None),
                Event.starts_at.is_not(None),
                Event.starts_at < now,
            )
            .distinct()
        )
    ).all()
    return [
        FinalScore(
            home_team=h,
            away_team=a,
            match_date=ko.date(),
            home_score=int(hs),
            away_score=int(as_),
        )
        for h, a, ko, hs, as_ in rows
    ]


async def run_settlement_cycle(
    client: httpx.AsyncClient,
    session_factory: "async_sessionmaker",
    slugs: Sequence[str],
    seasons: Sequence[str],
    now: datetime | None = None,
    devig_method: DevigMethod | None = None,
    use_pinnacle_archive: bool = False,
    use_betfair_exchange: bool = False,
) -> int:
    """One scheduler cycle: fetch scores for the configured leagues, settle.

    Refuses to settle when the providers return nothing (a feed outage must
    look like an outage, not like a quiet day).

    `devig_method` prices the snapshot-sourced closing line for the picks
    this cycle settles (see settle_open_picks). None — the scheduler's call —
    resolves to the SAME method the pick pipeline runs with, mirroring how
    app/scheduler.py builds deps.devig_method: live CLV, backtest CLV, and
    the settlement-time snapshot close must all speak one devig.
    """
    now = now or datetime.now(tz=UTC)
    from app.config import get_settings  # composition-root parity, lazy

    settings = get_settings()
    if devig_method is None:
        devig_method = (
            DevigMethod(settings.value_devig)
            if settings.pick_strategy == "value"
            else DevigMethod.POWER
        )
    # Stale-TBD voiding runs FIRST and independently of the score feed: a
    # feed outage must not keep dead picks burning revalidation slots.
    async with session_factory() as session:
        await void_stale_null_kickoff_picks(session, now)
        await session.commit()
    scores = await load_scores(client, slugs, seasons, on_or_after=(now - SCORE_WINDOW).date())
    # ESPN free scores add basketball / NFL / tennis auto-settlement (soccer
    # already uses the football-data CSV feeds above). Read-only SCORES only —
    # ESPN odds are soft and are NEVER used as a close.
    if settings.espn_settle_enabled:
        from app.ingestion.espn_scores import load_espn_scores

        espn_sports = [s.strip() for s in settings.espn_settle_sports.split(",") if s.strip()]
        espn_dates = [now.date() - timedelta(days=i) for i in range(settings.espn_settle_days)]
        scores = [*scores, *await load_espn_scores(client, espn_sports, espn_dates)]
    # Auto-settle from the OddsPortal-scraped final score too (no manual entry):
    # the score was already fetched at scrape time and lives on the pick's own
    # event. Feed/ESPN scores are listed first, so they take precedence in the
    # ScoreBook; scraped scores cover the leagues no free feed reaches.
    feed_count = len(scores)
    if settings.settle_from_scraped_scores:
        async with session_factory() as session:
            scraped = await _load_scraped_finals(session, now)
        scores = [*scores, *scraped]
        if scraped and feed_count == 0:
            logger.warning(
                "settle_results: result feeds returned nothing — settling %d game(s) "
                "from scraped final scores",
                len(scraped),
            )
    if not scores:
        logger.error("settle_results: no scores from any source — nothing settled")
        return 0
    async with session_factory() as session:
        settled = await settle_open_picks(
            session,
            ScoreBook(scores),
            now,
            devig_method=devig_method,
            use_pinnacle_archive=use_pinnacle_archive,
            use_betfair_exchange=use_betfair_exchange,
        )
        await session.commit()
    return settled


async def _stake_and_odds(session: AsyncSession, pick: Pick) -> tuple[Decimal, Decimal]:
    """The user's actual stake/odds when they logged the bet, else the
    recommendation — result_tracking.pnl is 'vs actual or recommended stake'."""
    log = await session.scalar(
        select(ManualBetLog)
        .where(
            ManualBetLog.pick_id == pick.id,
            ManualBetLog.bet_placed.is_(True),
            ManualBetLog.actual_stake.is_not(None),
        )
        .order_by(ManualBetLog.id.desc())
        .limit(1)
    )
    if log is not None and log.actual_stake is not None:
        odds = log.actual_odds if log.actual_odds is not None else pick.decimal_odds
        return log.actual_stake, odds
    return pick.recommended_stake_amount, pick.decimal_odds
