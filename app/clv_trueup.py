"""Live CLV true-up — the discipline that proves (or disproves) real edge.

Every poll, refresh the fair closing probability for OPEN picks from the
freshest multi-book odds: clv_log = ln(pick_odds x closing_fair_prob).
Rows are overwritten on each run, so the LAST observation before the market
disappears (kickoff) is what remains — the de-facto close. Once a pick is
settled (status != 'alerted'), its CLV is frozen.

Track this number: a model/strategy version is only trusted while its live
stake-weighted CLV stays positive (docs/backtesting/value-findings.md).
"""

import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.backtesting.clv import clv_log
from app.edge.value import anchor_fair_probs
from app.ingestion.base import OddsLoader
from app.pipeline import group_market_prices
from app.probabilities.devig import DevigMethod
from app.storage.models import Event, Pick

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)


async def true_up_clv(
    loader: OddsLoader,
    session_factory: "async_sessionmaker",
    sport_keys: Sequence[str],
    devig_method: DevigMethod = DevigMethod.SHIN,
) -> int:
    """Refresh closing-fair/CLV fields on open picks. Returns rows updated."""
    updated = 0
    for sport_key in sport_keys:
        try:
            snapshots = await loader.fetch_odds(sport_key)
        except Exception as exc:
            logger.error("clv true-up fetch failed for %s: %s", sport_key, type(exc).__name__)
            continue
        if not snapshots:
            continue
        # (external event ref, market str) -> fair probs from the freshest book panel
        fair_by_market: dict[tuple[str, str], dict[str, float]] = {}
        for (event_id, market), (prices, _) in group_market_prices(snapshots).items():
            # Same devig as the pick strategy, so live CLV is comparable to
            # the backtest's CLV columns.
            anchored = anchor_fair_probs(prices, devig_method=devig_method)
            if anchored is not None:
                fair_by_market[(event_id, str(market))] = anchored[1]

        if not fair_by_market:
            continue
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Pick, Event.external_ref)
                    .join(Event, Pick.event_id == Event.id)
                    .where(Pick.status == "alerted")
                )
            ).all()
            for pick, external_ref in rows:
                fair = fair_by_market.get((external_ref, pick.market))
                if fair is None:
                    continue
                closing_fair = fair.get(pick.selection)
                if closing_fair is None or not 0.0 < closing_fair < 1.0:
                    continue
                clv = clv_log(float(pick.decimal_odds), closing_fair)
                pick.closing_fair_probability = Decimal(f"{closing_fair:.6f}")
                pick.clv_log = Decimal(f"{clv:.6f}")
                pick.beat_close = clv > 0
                updated += 1
            await session.commit()
    if updated:
        logger.info("clv true-up refreshed %d open picks", updated)
    return updated
