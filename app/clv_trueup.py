"""Live revalidation of open picks — CLV true-up + "is it still worth it?".

Every poll cycle re-prices OPEN picks from the freshest multi-book odds:

- clv_log = ln(pick_odds x closing_fair_prob); rows are overwritten each
  run, so the LAST observation before the market disappears (kickoff) is
  what remains — the de-facto close. Settled picks are frozen.
- current_odds/current_edge: the pick's price at its own bookmaker right
  now (best book as fallback) and its edge vs the fresh fair probability —
  the dashboard's "still value / edge gone" verdict. Stale alerts must
  never read as live opportunities.

The poll pipeline calls revalidate_open_picks with the snapshots it just
scraped — no second scrape. Track stake-weighted CLV: a strategy version is
only trusted while it stays positive (docs/backtesting/value-findings.md).
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.backtesting.clv import clv_log
from app.ingestion.base import OddsLoader
from app.pipeline import event_fair_probs, group_market_prices
from app.probabilities.devig import DevigMethod
from app.schemas.odds import OddsSnapshotIn
from app.storage.models import Event, Pick

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)


async def revalidate_open_picks(
    session_factory: "async_sessionmaker",
    snapshots: Sequence[OddsSnapshotIn],
    devig_method: DevigMethod,
) -> int:
    """Refresh closing-fair/CLV and current-odds/edge on open picks from
    already-scraped snapshots. Returns rows updated."""
    if not snapshots:
        return 0
    # Same devig + same fair rules as the pick pipeline (event_fair_probs),
    # so live CLV is comparable to the backtest's CLV columns. Keyed by
    # SELECTION: line-bearing selections ("Over 215.5", "Alpha FC -1.5")
    # disambiguate submarkets that share one Market enum value.
    grouped = group_market_prices(snapshots)
    fair_by_key: dict[tuple[str, str, str], float] = {}
    for (event_id, market, _detail), (_book, fair) in event_fair_probs(
        grouped, devig_method
    ).items():
        for sel, p in fair.items():
            fair_by_key[(event_id, str(market), sel)] = p
    prices_by_key: dict[tuple[str, str, str], dict[str, float]] = {}
    for (event_id, market, _detail), (prices, _captured) in grouped.items():
        for sel, books in prices.items():
            prices_by_key[(event_id, str(market), sel)] = books

    if not fair_by_key:
        return 0
    now = datetime.now(tz=UTC)
    updated = 0
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Pick, Event.external_ref)
                .join(Event, Pick.event_id == Event.id)
                .where(Pick.status == "alerted")
            )
        ).all()
        for pick, external_ref in rows:
            key = (external_ref, pick.market, pick.selection)
            closing_fair = fair_by_key.get(key)
            if closing_fair is None or not 0.0 < closing_fair < 1.0:
                continue
            clv = clv_log(float(pick.decimal_odds), closing_fair)
            pick.closing_fair_probability = Decimal(f"{closing_fair:.6f}")
            pick.clv_log = Decimal(f"{clv:.6f}")
            pick.beat_close = clv > 0
            books = prices_by_key.get(key) or {}
            # The pick's own book is the actionable price; if it dropped the
            # market, the best remaining price is what a bettor could take.
            current = books.get(pick.bookmaker) or (max(books.values()) if books else None)
            if current is not None and current > 1.0:
                pick.current_odds = Decimal(f"{current:.4f}")
                pick.current_edge = Decimal(f"{closing_fair - 1.0 / current:.6f}")
            pick.revalidated_at = now
            updated += 1
        await session.commit()
    if updated:
        logger.info("revalidation refreshed %d open picks", updated)
    return updated


# One match page per link per cycle; cap keeps a pathological backlog of
# far-future open picks from dominating cycle time (oldest picks wait a
# cycle — they are re-priced round-robin as earlier ones settle/expire).
OFFWINDOW_LINK_CAP = 25


async def revalidate_offwindow_picks(
    loader: OddsLoader,
    session_factory: "async_sessionmaker",
    sport_key: str,
    covered_event_ids: set[str],
    devig_method: DevigMethod = DevigMethod.SHIN,
) -> int:
    """Re-price open picks whose games were NOT in this cycle's scrape
    (taken weeks ahead of kickoff): scrape their match pages directly.

    Requires the loader to support fetch_match_odds (OddsPortalLoader);
    other loaders silently skip. Returns rows updated.
    """
    fetch = getattr(loader, "fetch_match_odds", None)
    if fetch is None:
        return 0
    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        refs = (
            (
                await session.execute(
                    select(Event.external_ref)
                    .join(Pick, Pick.event_id == Event.id)
                    .where(Pick.status == "alerted", Event.starts_at > now)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    links = [ref for ref in refs if ref.startswith("http") and ref not in covered_event_ids][
        :OFFWINDOW_LINK_CAP
    ]
    if not links:
        return 0
    snapshots = await fetch(sport_key, links)
    # Same devig as the live pipeline (passed from the composition root) so
    # off-window re-pricing stays comparable to in-window CLV numbers.
    return await revalidate_open_picks(session_factory, snapshots, devig_method)


async def true_up_clv(
    loader: OddsLoader,
    session_factory: "async_sessionmaker",
    sport_keys: Sequence[str],
    devig_method: DevigMethod = DevigMethod.SHIN,
) -> int:
    """Standalone fetch + revalidate (used when no fresh snapshots exist)."""
    updated = 0
    for sport_key in sport_keys:
        try:
            snapshots = await loader.fetch_odds(sport_key)
        except Exception as exc:
            logger.error("clv true-up fetch failed for %s: %s", sport_key, type(exc).__name__)
            continue
        updated += await revalidate_open_picks(session_factory, snapshots, devig_method)
    return updated
