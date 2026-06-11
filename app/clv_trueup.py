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
from urllib.parse import urlparse

from sqlalchemy import and_, func, or_, select, update

from app.backtesting.clv import clv_log
from app.edge.value import effective_odds
from app.ingestion.base import OddsLoader
from app.pipeline import event_fair_probs, group_market_prices
from app.probabilities.devig import DevigMethod
from app.schemas.odds import OddsSnapshotIn
from app.settlement.engine import STALE_NULL_KICKOFF_AGE
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
    already-scraped snapshots. Returns rows updated.

    CLV netting convention: BOTH sides of clv_log are commission-netted.
    The closing fair probability comes from anchors devigged on EFFECTIVE
    prices (app/edge/value.py nets exchange commission before devig), so the
    fill side must be the EFFECTIVE fill odds too — feeding the raw exchange
    price would inflate CLV by ~ln(1/(1-c)) on every exchange pick. Picks at
    commission-free books are unaffected (effective == raw).
    """
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
            # EFFECTIVE fill vs net-anchored close — see docstring convention.
            fill_eff = effective_odds(pick.bookmaker, float(pick.decimal_odds))
            clv = clv_log(fill_eff, closing_fair)
            pick.closing_fair_probability = Decimal(f"{closing_fair:.6f}")
            pick.clv_log = Decimal(f"{clv:.6f}")
            pick.beat_close = clv > 0
            books = prices_by_key.get(key) or {}
            # The pick's own book is the actionable price; if it dropped the
            # market, the best remaining price is what a bettor could take —
            # "best" by EFFECTIVE odds, so selection agrees with the
            # effective-odds valuation below (and with pick-time selection in
            # app/edge/value.py).
            if pick.bookmaker in books:
                current_book, current = pick.bookmaker, books[pick.bookmaker]
            elif books:
                current_book, current = max(
                    books.items(), key=lambda kv: effective_odds(kv[0], kv[1])
                )
            else:
                current_book, current = None, None
            if current_book is not None and current is not None and current > 1.0:
                pick.current_odds = Decimal(f"{current:.4f}")
                # Edge on the EFFECTIVE (commission-netted) price — pick-time
                # edges are netted too, so "still value" verdicts compare
                # like with like at exchanges.
                current_eff = effective_odds(current_book, current)
                pick.current_edge = Decimal(f"{closing_fair - 1.0 / current_eff:.6f}")
            # Success-only stamp (dashboard "verified" badge) — plus the
            # attempt clock, since a successful re-price is also an attempt.
            pick.revalidated_at = now
            pick.revalidation_attempted_at = now
            updated += 1
        await session.commit()
    if updated:
        logger.info("revalidation refreshed %d open picks", updated)
    return updated


# One match page per link per cycle; cap keeps a pathological backlog of
# far-future open picks from dominating cycle time. The query orders by
# stalest-ATTEMPT-first (never-attempted picks lead), so the cap is a true
# round-robin: whoever waited longest goes next cycle. Ordering on attempts
# (not successes) is what keeps dead links — postponed pages, dropped
# markets, scrape gaps — rotating to the back instead of pinning the front
# and starving healthy picks.
OFFWINDOW_LINK_CAP = 25

# fetch_match_odds drives a headless browser to these URLs — only OddsPortal
# match pages may ever be fetched. A poisoned/garbage Event.external_ref must
# not steer the scraper to an arbitrary host (SSRF).
ALLOWED_MATCH_HOSTS = frozenset({"oddsportal.com", "www.oddsportal.com"})


def _is_allowed_match_url(ref: str) -> bool:
    # Parser-differential hardening: browsers (WHATWG URL) treat '\' as '/'
    # and tolerate embedded whitespace; urllib (RFC 3986) does not. A ref
    # like "https://www.oddsportal.com\@evil.com/x" parses HERE as host
    # oddsportal.com while Chromium would navigate to evil.com — reject the
    # raw string BEFORE parsing.
    if "\\" in ref or any(ch.isspace() for ch in ref):
        return False
    try:
        parsed = urlparse(ref)
        port = parsed.port  # property raises ValueError on garbage ports
    except ValueError:
        return False
    if parsed.scheme != "https":  # oddsportal is https-only; no downgrades
        return False
    if parsed.username is not None or parsed.password is not None:
        return False  # userinfo@host is a classic allowlist bypass shape
    host = (parsed.hostname or "").casefold()
    return host in ALLOWED_MATCH_HOSTS and port in (None, 443)


def select_offwindow_links(
    refs: Sequence[str],
    sport_segment: str | None,
    covered_event_ids: set[str],
    cap: int = OFFWINDOW_LINK_CAP,
) -> list[str]:
    """Order-preserving choice of match links to scrape: oddsportal-host refs
    for THIS sport that the cycle didn't already cover, capped AFTER
    filtering — wrong-host, wrong-sport, or already-covered rows must never
    burn cap slots."""
    links = [ref for ref in refs if _is_allowed_match_url(ref) and ref not in covered_event_ids]
    if sport_segment:
        links = [ref for ref in links if f"/{sport_segment}/" in ref]
    return links[:cap]


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
                    # NULL starts_at = kickoff unknown ("TBD"): we cannot
                    # prove the game started, so keep re-pricing — without
                    # the IS NULL arm, SQL's "NULL > now" (unknown) silently
                    # drops TBD picks from revalidation forever. But only
                    # for STALE_NULL_KICKOFF_AGE from pick creation: a
                    # kickoff that never materialises must not burn scrape
                    # slots indefinitely — the settlement cycle voids those
                    # picks (void_stale_null_kickoff_picks).
                    .where(
                        Pick.status == "alerted",
                        or_(
                            and_(
                                Event.starts_at.is_(None),
                                Pick.created_at > now - STALE_NULL_KICKOFF_AGE,
                            ),
                            Event.starts_at > now,
                        ),
                    )
                    .group_by(Event.external_ref)
                    # Round-robin on ATTEMPTS, not successes: a dead link that
                    # never re-prices would keep revalidated_at NULL forever,
                    # sort first every cycle, and starve the queue.
                    .order_by(func.min(Pick.revalidation_attempted_at).asc().nulls_first())
                )
            )
            .scalars()
            .all()
        )
    segment_for = getattr(loader, "sport_segment", None)
    segment = segment_for(sport_key) if callable(segment_for) else None
    links = select_offwindow_links(refs, segment, covered_event_ids)
    if not links:
        return 0
    snapshots = await fetch(sport_key, links)
    # Every fetched match page counts as an ATTEMPT for its open picks —
    # priced or not (wholesale-empty fetch, postponed page, per-market gap) —
    # so dead links rotate to the back of the round-robin above instead of
    # re-burning cap slots every cycle. revalidated_at stays success-only
    # (set in revalidate_open_picks): it backs the dashboard "verified"
    # badge and must never be stamped by a failed attempt.
    attempted_at = datetime.now(tz=UTC)
    async with session_factory() as session:
        await session.execute(
            update(Pick)
            .where(
                Pick.status == "alerted",
                Pick.event_id.in_(select(Event.id).where(Event.external_ref.in_(links))),
            )
            .values(revalidation_attempted_at=attempted_at)
        )
        await session.commit()
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
