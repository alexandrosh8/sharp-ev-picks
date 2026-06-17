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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import aliased

from app.backtesting.clv import clv_log
from app.edge.value import effective_odds
from app.ingestion.base import OddsLoader
from app.pipeline import event_fair_probs, group_market_prices
from app.probabilities.devig import DevigMethod
from app.resolution.shadow import arcadia_base_sport
from app.schemas.odds import OddsSnapshotIn
from app.settlement.engine import STALE_NULL_KICKOFF_AGE
from app.storage.models import Event, Pick, Sport, Team
from app.storage.repositories import closing_odds_from_snapshots

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
                # STARTED events are excluded: once a game kicks off the
                # scraper follows OddsPortal's in-play pages, and in-play
                # prices must neither overwrite the last pre-kickoff
                # observation (the de-facto close this loop maintains) nor
                # pose as a live "still worth betting?" verdict. NULL
                # kickoff = cannot prove the game started -> keep re-pricing
                # (same rule as the off-window selector below).
                .where(
                    Pick.status == "alerted",
                    or_(Event.starts_at.is_(None), Event.starts_at > now),
                )
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


def order_offwindow_refs(
    rows: Sequence[tuple[str, str, str, str, datetime | None]],
) -> list[str]:
    """Scrape order over (external_ref, market, selection, tier,
    revalidation_attempted_at) open-pick rows: PREMIUM-bearing events first
    — the alerted tier's CLV true-up cadence must not be diluted by the ~6x
    larger volume shadow tier competing for the same link cap — then the
    attempts round-robin inside each band (never-attempted events lead,
    then stalest attempt: whoever waited longest goes next cycle)."""
    has_premium: dict[str, bool] = {}
    attempts: dict[str, list[datetime]] = {}
    for ref, _market, _selection, tier, attempted in rows:
        has_premium[ref] = has_premium.get(ref, False) or tier == "premium"
        stamps = attempts.setdefault(ref, [])
        if attempted is not None:
            stamps.append(attempted)

    def sort_key(ref: str) -> tuple[bool, bool, datetime]:
        # min over non-NULL stamps (SQL MIN semantics, matching the previous
        # query): an event is "never attempted" only when NO pick has one.
        stamps = attempts[ref]
        return (
            not has_premium[ref],
            bool(stamps),
            min(stamps) if stamps else datetime.min.replace(tzinfo=UTC),
        )

    return sorted(has_premium, key=sort_key)


def _selection_line(selection: str) -> float | None:
    """The line embedded in a stored selection string — 'Over 2.5' -> 2.5,
    'Alpha FC -1.5' -> -1.5, 'Draw (+1)' -> 1.0. None = no parseable line."""
    parts = selection.rsplit(maxsplit=1)
    if not parts:
        return None
    token = parts[-1].strip("()")
    try:
        return float(token)
    except ValueError:
        return None


def _key_line(line: float, signed: bool = True) -> str:
    text = f"{line:+g}" if signed else f"{line:g}"
    return text.replace(".", "_")


def _pick_market_keys(sport_key: str, market: str, selection: str) -> tuple[str, ...] | None:
    """OddsHarvester market key(s) a stored pick needs for re-pricing; None
    = unmappable (the caller falls back to the loader's full configured
    list). Spread selections carry the line from THEIR team's perspective
    while provider keys are home-relative — both signs are returned and the
    loader's config intersection drops whichever does not exist."""
    basketball = sport_key == "basketball"
    if market == "h2h":
        return ("home_away",) if basketball else ("1x2",)
    if market in ("btts", "dnb"):
        return (market,)
    if market == "double_chance":
        return ("double_chance", "1x2")  # DC fair is DERIVED from the 1X2 anchor
    if market == "totals":
        line = _selection_line(selection)
        if line is None or line <= 0:
            return None
        frag = _key_line(line, signed=False)
        return (f"over_under_games_{frag}",) if basketball else (f"over_under_{frag}",)
    if market == "spreads":
        line = _selection_line(selection)
        if line is None or line == 0:
            return None
        lines = sorted({line, -line})
        if basketball:
            return tuple(f"asian_handicap_games_{_key_line(ln)}_games" for ln in lines)
        if line == int(line):  # integer line = 3-way European handicap
            return tuple(f"european_handicap_{_key_line(ln)}" for ln in lines)
        return tuple(f"asian_handicap_{_key_line(ln)}" for ln in lines)
    return None


def offwindow_market_keys(
    sport_key: str, picks: Sequence[tuple[str, str]]
) -> tuple[str, ...] | None:
    """Provider market keys the off-window re-scrape needs: ONLY the
    submarkets the capped links' open picks actually reference — every key
    costs one browser tab per match page, and the full configured list is
    18-21 tabs. None = no picks or at least one is unmappable: the loader
    then scrapes its full configured list (never worse coverage)."""
    keys: set[str] = set()
    for market, selection in picks:
        mapped = _pick_market_keys(sport_key, market, selection)
        if mapped is None:
            return None
        keys.update(mapped)
    return tuple(sorted(keys)) if keys else None


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
        pick_rows = (
            await session.execute(
                select(
                    Event.external_ref,
                    Pick.market,
                    Pick.selection,
                    Pick.tier,
                    Pick.revalidation_attempted_at,
                )
                .join(Event, Pick.event_id == Event.id)
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
            )
        ).all()
    # Premium-bearing events first, then the attempts round-robin (a dead
    # link that never re-prices would keep revalidated_at NULL forever,
    # sort first every cycle, and starve the queue — hence attempts).
    refs = order_offwindow_refs([tuple(row) for row in pick_rows])
    pairs_by_ref: dict[str, list[tuple[str, str]]] = {}
    for ref, market, selection, _tier, _attempted in pick_rows:
        pairs_by_ref.setdefault(ref, []).append((market, selection))
    segment_for = getattr(loader, "sport_segment", None)
    segment = segment_for(sport_key) if callable(segment_for) else None
    links = select_offwindow_links(refs, segment, covered_event_ids)
    if not links:
        return 0
    # Trimmed market set: scrape only the tabs the capped links' open picks
    # reference (None = full configured list when any pick is unmappable).
    needed = offwindow_market_keys(
        sport_key, [pair for link in links for pair in pairs_by_ref.get(link, [])]
    )
    snapshots = await fetch(sport_key, links, markets=needed)
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


# --- closing-line capture from our OWN odds_snapshots ------------------------
# Scrape-coverage gate for the snapshot close: the EVENT's last pre-kickoff
# snapshot must be at most this old at kickoff. This guards events that FELL
# OUT of the scrape (dropped from listings days before kickoff) — it does NOT
# judge slow-moving prices: change-only persistence means an individual book's
# last row may be days old and still be that book's true close (the price
# simply never moved while the event kept being scraped), so the gate reads
# the event-wide last-capture clock, never per-row age.
SNAPSHOT_CLOSE_MAX_GAP = timedelta(hours=4)


async def finalize_closing_from_snapshots(
    session: "AsyncSession",
    pick: Pick,
    external_ref: str,
    kickoff: datetime | None,
    devig_method: DevigMethod,
    max_gap: timedelta = SNAPSHOT_CLOSE_MAX_GAP,
    *,
    use_pinnacle_archive: bool = False,
) -> bool:
    """Recompute the pick's closing fair/CLV from our own odds_snapshots
    history instead of trusting the last pre-kickoff re-scrape write.

    Returns True when the snapshot close was applied. False = NO coverage
    (kickoff unknown, event not scraped near kickoff, no anchorable close
    book set, selection unpriced at close): the pick keeps whatever the
    live/match-page re-scrape revalidation path last wrote — that overwrite
    IS the fallback close, so this function must never blank existing fields.

    Consistency rules — identical to the live pick path BY CONSTRUCTION:
    - SAME devig method the pick used (the pipeline's deps.devig_method);
    - SAME anchoring/min-book rules: the close set runs through
      event_fair_probs/anchor_fair_probs — a named sharp book pricing the
      full market, else a >= MIN_CONSENSUS_BOOKS median consensus; anything
      thinner yields no fair and falls back rather than anchoring on garbage;
    - EFFECTIVE odds on BOTH sides (netting convention, see
      revalidate_open_picks): anchor prices are commission-netted before
      devig (close side) and the fill is netted here (fill side).

    Provenance: `closing_odds` — never populated by the re-scrape path — is
    written ONLY here (close-row price at the pick's own book; best remaining
    book by effective odds when it dropped the market). `closing_odds IS NOT
    NULL` therefore marks a snapshot-sourced close; an INFO log line says the
    same at write time.
    """
    if kickoff is None:
        return False  # kickoff unknown -> "close" is undefined
    snaps, last_capture = await closing_odds_from_snapshots(
        session, pick.event_id, external_ref, kickoff
    )
    if last_capture is None or kickoff - last_capture > max_gap:
        logger.info(
            "pick %d: no snapshot-close coverage (event not scraped within %s "
            "of kickoff) — keeping revalidation close",
            pick.id,
            max_gap,
        )
        return False
    if use_pinnacle_archive:
        # Inject the matched Pinnacle ARCHIVE close (strict cross-source match)
        # so a real sharp close anchors the fair (value.SHARP_BOOKS[0]=="pinnacle")
        # for incremental CLV. No match -> [] -> behaviour unchanged.
        extra = await _pinnacle_archive_close(session, pick, external_ref, kickoff)
        if extra:
            snaps = [*snaps, *extra]
            logger.info(
                "pick %d: injected %d Pinnacle archive close rows (strict match)",
                pick.id,
                len(extra),
            )
    grouped = group_market_prices(snaps)
    fair_by_key: dict[tuple[str, str], float] = {}
    for (_event, market, _detail), (_anchor, fair_by_sel) in event_fair_probs(
        grouped, devig_method
    ).items():
        for sel, p in fair_by_sel.items():
            fair_by_key[(str(market), sel)] = p
    fair = fair_by_key.get((pick.market, pick.selection))
    if fair is None or not 0.0 < fair < 1.0:
        logger.info(
            "pick %d: snapshot close has no anchored fair for its market/selection "
            "— keeping revalidation close",
            pick.id,
        )
        return False
    # EFFECTIVE fill vs net-anchored close — same symmetry as the live path.
    fill_eff = effective_odds(pick.bookmaker, float(pick.decimal_odds))
    clv = clv_log(fill_eff, fair)
    books: dict[str, float] = {}
    for (_event, market, _detail), (prices, _captured) in grouped.items():
        if str(market) == pick.market and pick.selection in prices:
            books = prices[pick.selection]
            break
    if pick.bookmaker in books:
        close_odds = books[pick.bookmaker]
    elif books:
        _, close_odds = max(books.items(), key=lambda kv: effective_odds(kv[0], kv[1]))
    else:  # unreachable when fair exists (the anchor priced the selection)
        close_odds = None
    pick.closing_fair_probability = Decimal(f"{fair:.6f}")
    pick.clv_log = Decimal(f"{clv:.6f}")
    pick.beat_close = clv > 0
    if close_odds is not None and close_odds > 1.0:
        pick.closing_odds = Decimal(f"{close_odds:.4f}")
    logger.info(
        "pick %d: closing line from odds_snapshots (clv_log=%.4f, %d close books)",
        pick.id,
        clv,
        len(books),
    )
    return True


async def _pinnacle_archive_close(
    session: "AsyncSession",
    pick: Pick,
    external_ref: str,
    kickoff: datetime,
) -> list[OddsSnapshotIn]:
    """The matched Pinnacle ARCHIVE event's close snapshots for this pick, or []
    (strict cross-source match; see repositories.resolve_pinnacle_close_snaps).
    Looks up the pick event's sport + team names, derives the `pinnacle_<sport>`
    namespace, and delegates the strict match."""
    from app.storage.repositories import resolve_pinnacle_close_snaps

    home_t, away_t = aliased(Team), aliased(Team)
    info = (
        await session.execute(
            select(Sport.key, home_t.name, away_t.name)
            .select_from(Event)
            .join(Sport, Event.sport_id == Sport.id)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .where(Event.id == pick.event_id)
        )
    ).first()
    if info is None:
        return []
    sport_key, home, away = info
    base = arcadia_base_sport(sport_key)
    return await resolve_pinnacle_close_snaps(
        session,
        pinnacle_sport_key=f"pinnacle_{base}",
        pick_external_ref=external_ref,
        home=home,
        away=away,
        kickoff=kickoff,
    )


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
