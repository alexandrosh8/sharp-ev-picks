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

from app.settlement.outcomes import pick_pnl, pick_roi, settle_selection
from app.settlement.results import ScoreBook, load_scores
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


async def settle_open_picks(
    session: AsyncSession,
    book: ScoreBook,
    now: datetime,
    delay: timedelta = SETTLE_DELAY,
) -> int:
    """Settle every alerted pick whose event finished and has a known score.

    Returns the number of picks settled. The caller owns the transaction.
    """
    if len(book) == 0:
        logger.error("settlement: empty score book — refusing to settle (silent-empty guard)")
        return 0

    home, away = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(Pick, home.name, away.name, Event.starts_at)
            .join(Event, Pick.event_id == Event.id)
            .join(home, Event.home_team_id == home.id)
            .join(away, Event.away_team_id == away.id)
            .where(Pick.status == "alerted", Event.starts_at <= now - delay)
        )
    ).all()

    settled = 0
    for pick, home_name, away_name, starts_at in rows:
        score = book.lookup(home_name, away_name, starts_at)
        if score is None:
            continue  # close_pending — stays open, retried next cycle
        if await _settle_one(
            session, pick, home_name, away_name, score.home_score, score.away_score, now
        ):
            settled += 1
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


async def run_settlement_cycle(
    client: httpx.AsyncClient,
    session_factory: "async_sessionmaker",
    slugs: Sequence[str],
    seasons: Sequence[str],
    now: datetime | None = None,
) -> int:
    """One scheduler cycle: fetch scores for the configured leagues, settle.

    Refuses to settle when the providers return nothing (a feed outage must
    look like an outage, not like a quiet day).
    """
    now = now or datetime.now(tz=UTC)
    scores = await load_scores(client, slugs, seasons, on_or_after=(now - SCORE_WINDOW).date())
    if not scores:
        logger.error("settle_results: results providers returned no scores — nothing settled")
        return 0
    async with session_factory() as session:
        settled = await settle_open_picks(session, ScoreBook(scores), now)
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
