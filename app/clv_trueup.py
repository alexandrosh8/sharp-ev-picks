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

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import aliased

from app.backtesting.clv import clv_log
from app.edge.value import (
    SHARP_BOOKS,
    anchor_type_for,
    close_is_independent_of_fill,
    effective_odds,
)
from app.ingestion.base import EventDirectory, OddsLoader
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


def _best_soft_book(books: dict[str, float]) -> tuple[str | None, float | None]:
    """Best SOFT book by EFFECTIVE odds — sharp/anchor books (Pinnacle/Betfair/…)
    excluded. The re-price fallback (when a pick's own book dropped the selection)
    must never land on a sharp book: you cannot bet it, and its price beating its
    own fair is not "still value" (audit #3). Mirrors value._best_other_book's
    SHARP_BOOKS exclusion. Returns (None, None) when no soft book quotes it."""
    sharp = {b.strip().lower() for b in SHARP_BOOKS}
    soft = {b: o for b, o in books.items() if b.strip().lower() not in sharp}
    if not soft:
        return None, None
    book = max(soft, key=lambda b: effective_odds(b, soft[b]))
    return book, soft[book]


def _is_implausible_final(sport_key: str, home_score: int, away_score: int) -> bool:
    """True if a captured "final" score is physically impossible for the sport and
    must be REJECTED (retry next cycle) rather than recorded + used to settle a pick
    (audit 2026-06-26). Basketball cannot end tied (no regulation/OT tie) and a real
    final totals well over 100 points; a tied/low capture (e.g. 24-24) is a
    mis-scrape, not a result. Other sports carry no plausibility constraint here."""
    if "basketball" in (sport_key or "").lower():
        return home_score == away_score or (home_score + away_score) < 100
    return False


def _consistent_current_edge(pick: Pick, fair: float) -> Decimal | None:
    """current_edge kept consistent with a freshly-stamped fair (audit 2026-06-26):
    recompute against the pick's last-known current price so the dashboard Edge /
    "no value now" / valueGone can never contradict the Fair/EV/"ok >=" floor (all of
    which reason against the fresh fair). None when no current price exists — a stale
    edge beside a refreshed fair is exactly the bug this prevents. Invariant after any
    fair refresh: fair - current_edge == 1/effective_odds(current price)."""
    if pick.current_odds is None:
        return None
    eff = effective_odds(pick.current_bookmaker or pick.bookmaker, float(pick.current_odds))
    return Decimal(f"{fair - 1.0 / eff:.6f}")


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
    anchor_by_key: dict[tuple[str, str, str], str] = {}
    for (event_id, market, _detail), (book, fair) in event_fair_probs(
        grouped, devig_method
    ).items():
        for sel, p in fair.items():
            fair_by_key[(event_id, str(market), sel)] = p
            anchor_by_key[(event_id, str(market), sel)] = book
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
            close_anchor = anchor_by_key.get(key)
            if close_anchor:
                # CLOSE-side provenance: the anchor that priced this re-scrape
                # close. This path never writes closing_odds, so the close is a
                # poll-time FALLBACK (not a snapshot close); honest CLV trusts it
                # only if finalize_closing_from_snapshots later overwrites with a
                # sharp anchor AND closing_odds (the snapshot-close marker).
                pick.closing_anchor_type = anchor_type_for(close_anchor)
                # Stamp independence here too (audit #4): this path previously left
                # close_independent_of_fill NULL, which the trusted-CLV gate admitted as
                # "not circular". Record it so a re-scrape close can never leak in as
                # trusted (same source as the pick, or fill self-priced => not independent).
                pick.close_independent_of_fill = close_is_independent_of_fill(
                    close_anchor,
                    pick.bookmaker,
                    pick_anchor_type=pick.anchor_type or "",
                    close_anchor_type=pick.closing_anchor_type or "",
                    # CLV-3: prefer BOOK identity — two different sharp books (e.g.
                    # Smarkets pick vs Betfair close) are independent even though
                    # both collapse to anchor_type 'sharp'.
                    pick_anchor_book=pick.anchor_book or "",
                )
            books = prices_by_key.get(key) or {}
            # The pick's own book is the actionable price; if it dropped the
            # market, the best remaining price is what a bettor could take —
            # "best" by EFFECTIVE odds, so selection agrees with the
            # effective-odds valuation below (and with pick-time selection in
            # app/edge/value.py).
            current_book: str | None
            current: float | None
            if pick.bookmaker in books:
                current_book, current = pick.bookmaker, books[pick.bookmaker]
            else:
                # Pick's own (soft) book dropped the selection -> best remaining
                # SOFT book, never a sharp/anchor (audit #3): re-pricing onto
                # Pinnacle/Betfair would fake a "still value" verdict.
                current_book, current = _best_soft_book(books)
            if current_book is not None and current is not None and current > 1.0:
                pick.current_odds = Decimal(f"{current:.4f}")
                # Record which book this price came from — normally the pick's
                # own bookmaker, but the fallback branch above can pick the best
                # remaining book when the original dropped the selection. The
                # dashboard uses this to label "now at <book>" honestly.
                pick.current_bookmaker = current_book
                # Edge on the EFFECTIVE (commission-netted) price — pick-time
                # edges are netted too, so "still value" verdicts compare
                # like with like at exchanges.
                current_eff = effective_odds(current_book, current)
                pick.current_edge = Decimal(f"{closing_fair - 1.0 / current_eff:.6f}")
            else:
                # No usable live price THIS cycle: keep current_edge consistent with the
                # FRESH fair (recompute against the last-known price) or null it — never
                # leave a stale edge beside a refreshed fair, which made the dashboard
                # Edge/valueGone contradict the Fair/EV/"ok >=" floor (audit 2026-06-26).
                pick.current_edge = _consistent_current_edge(pick, closing_fair)
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


#: Terminal Event.status once a final score is captured. Event.status was
#: previously only ever the 'scheduled' server-default (no code transitioned it),
#: so finished, settled games stayed 'scheduled' forever (Issue 2, 2026-06-24).
#: The finished-gated score capture is the one place that KNOWS a game is over, so
#: it owns the transition. Lifecycle-only (no logic reads it), so this is safe.
_FINISHED_STATUS = "finished"
#: How far back to re-scrape finished, still-open picks for their final score.
#: Wide enough that a slow VPS which missed a score for several days still
#: recovers it — the old 3-day window stranded older picks on "awaiting result"
#: forever (cactusbets.cloud: Australian-league soccer 9 days past kickoff, no
#: results feed). The per-cycle limit/budget still bound the backlog.
#: Overridable via RESULTS_SCRAPE_WINDOW_DAYS (the scheduler injects it).
RESULTS_SCRAPE_WINDOW = timedelta(days=14)
#: Cap finished-score scrapes per cycle so one backlog can't drive 100s of
#: browser pages at once; the un-scored remainder drains over the next cycles.
RESULTS_SCRAPE_MAX_PER_CYCLE = 40
#: Minimum age PAST KICKOFF before a scraped score is trusted as FINAL. OddsPortal
#: shows a LIVE running score in-play and OddsHarvester exposes no finished flag,
#: so without this floor an in-play partial score could be recorded as the result
#: (corrupting outcome + ROI). Sport-specific + generous: a still-running match is
#: never captured (it settles on a later cycle once truly final).
_FINISHED_FLOOR = {
    "soccer": timedelta(hours=3, minutes=30),
    "basketball": timedelta(hours=3, minutes=30),
    "american_football": timedelta(hours=4, minutes=30),
    "tennis": timedelta(hours=6),
}
_FINISHED_FLOOR_DEFAULT = timedelta(hours=4)
#: SOFT candidate floor. With OddsPortal's explicit finished-status now the real
#: safeguard (a Finished score is captured; an in-play False is REJECTED in
#: _scrape_one_finished_score), the SQL select only needs a short minimum so a
#: finished game becomes a candidate within minutes of FT. The full sport floor
#: above is retained as the fallback for status-MISSING (obscure-league) rows.
#: A normal soccer match is ~105 min, so 100 min never makes an obviously
#: mid-match game a candidate — and even if it did, finished=False rejects it.
_RESULTS_SOFT_FLOOR = timedelta(minutes=100)


def _finished_floor(sport_key: str) -> timedelta:
    return _FINISHED_FLOOR.get(sport_key, _FINISHED_FLOOR_DEFAULT)


#: Default per-LINK scrape timeout (seconds) for the finished-score pass. One
#: hung proxy request on a VPS must not stall the whole pass: each match page is
#: fetched under its own asyncio.wait_for, and a timeout drops just that link
#: (retried next cycle). Generous — OddsPortal match pages are heavy — but
#: finite. None at the call boundary = no bound (the default for in-process
#: callers/tests that pass nothing); the scheduler injects a concrete value.
RESULTS_SCRAPE_LINK_TIMEOUT_SECONDS = 90.0
#: Default per-CYCLE wall-clock budget (seconds). Even with per-link timeouts a
#: long backlog of slow pages could outlast the job's useful window; when the
#: budget is spent the pass STOPS CLEANLY (already-committed scores are durable,
#: the remainder drains next cycle). None = no budget.
RESULTS_SCRAPE_CYCLE_BUDGET_SECONDS = 600.0


async def _scrape_one_finished_score(
    fetch: "Callable[..., Awaitable[object]]",
    session_factory: "async_sessionmaker",
    directory: EventDirectory,
    sport_key: str,
    ref: str,
    past_full_floor: bool,
    per_link_timeout: float | None,
) -> int:
    """Scrape ONE finished match page and commit its score in its own session.

    Returns 1 when a final score was written, else 0. Isolated by design: the
    fetch runs under a per-link timeout (a hung/slow proxy request drops just
    THIS link) and the guarded UPDATE (scraped_home_score IS NULL) keeps the
    write finished-gated + never-clobbering. Raises nothing the caller must
    handle for control flow — failures here are caught and logged by the caller
    so one bad link never blocks the already-committed scores of the others.
    """
    # prefiltered=True: the caller already routed `ref` to this sport by the DB
    # sport on the open pick, so scrape the stored URL AS-IS (no URL sport-segment
    # filter) — robust to an OddsPortal per-game-type URL change.
    # score_only=True: scrape NO markets — only the match header (which carries
    # the finished score). The full market list would re-run the slow/hang-prone
    # Over/Under extraction, so the per-link timeout fired BEFORE the score was
    # read (only 1 of ~24 scores landed on cactusbets.cloud). Score-only keeps the
    # finished-score scrape cheap and reliable; the score reaches us via `directory`.
    coro = fetch(sport_key, [ref], prefiltered=True, score_only=True)
    if per_link_timeout is not None:
        await asyncio.wait_for(coro, timeout=per_link_timeout)
    else:
        await coro
    teams = directory.lookup(ref)
    if teams is None or teams.home_score is None or teams.away_score is None:
        return 0
    # Sport-aware plausibility guard (audit 2026-06-26): a basketball "final" that is
    # tied or totals < 100 points is a mis-scrape (e.g. 24-24), not a result — reject
    # it so the link retries next cycle instead of recording garbage that permanently
    # settles a pick on an impossible score.
    if _is_implausible_final(sport_key, teams.home_score, teams.away_score):
        logger.warning(
            "rejected implausible %s final for %s: %d-%d (retry next cycle)",
            sport_key,
            ref,
            teams.home_score,
            teams.away_score,
        )
        return 0
    # Explicit finished-status gate — the REAL safeguard (the SQL soft-floor only
    # widens candidates). True = page reports Finished -> capture now. False =
    # in-play/scheduled (a live partial) -> REJECT, never recorded as final.
    # None = source gave no status -> require the conservative full sport floor.
    if teams.finished is False:
        return 0
    if teams.finished is None and not past_full_floor:
        return 0
    async with session_factory() as session:
        # 1) Write the final score (guarded: never clobber a recorded score) AND
        #    transition the event to its terminal status in the SAME statement, so
        #    a freshly-captured finished game is both scored and marked 'finished'.
        res = await session.execute(
            update(Event)
            .where(
                Event.external_ref == ref,
                Event.scraped_home_score.is_(None),  # never clobber a recorded score
            )
            .values(
                scraped_home_score=teams.home_score,
                scraped_away_score=teams.away_score,
                status=_FINISHED_STATUS,
            )
        )
        # 2) HEAL the status of an already-scored event still stuck at 'scheduled'
        #    (the pre-fix backlog: Event.status was never transitioned, so finished,
        #    settled games kept the 'scheduled' default). Idempotent + finished-gated
        #    (we only reach here when the page reports finished / past the floor), and
        #    it never touches the score, so it can't corrupt settlement. This is the
        #    score-write's no-op sibling for rows whose score landed before the fix.
        await session.execute(
            update(Event)
            .where(
                Event.external_ref == ref,
                Event.status != _FINISHED_STATUS,
            )
            .values(status=_FINISHED_STATUS)
        )
        # Commit PER LINK: an already-scraped finished score must survive a
        # later link hanging/raising or the cycle's time budget running out.
        await session.commit()
    # The return is the SCORE-write signal (1 when a new final score landed), kept
    # distinct from the status heal so the caller's per-cycle "written" tally still
    # counts newly-captured scores, not status backfills.
    return res.rowcount or 0


async def capture_finished_scores(
    loader: OddsLoader,
    session_factory: "async_sessionmaker",
    directory: EventDirectory,
    sport_key: str,
    now: datetime | None = None,
    window: timedelta | None = None,
    limit: int | None = None,
    *,
    per_link_timeout: float | None = None,
    time_budget: float | None = None,
) -> int:
    """Re-scrape FINISHED, still-open picks' OddsPortal match pages to capture
    their final SCORE (Event.scraped_*), so leagues with no free results feed
    auto-settle (settle_from_scraped_scores) with NO manual entry. The odds
    revalidation path stops at kickoff, so finished games are never otherwise
    re-fetched — this is the missing post-kickoff score pass.

    Unlike revalidate_offwindow_picks this NEVER re-prices (post-match odds are
    stale): it only reads the score the scrape registered in `directory` and
    writes it via a guarded UPDATE (scraped_home_score IS NULL — never clobbers
    an existing score). Requires fetch_match_odds (OddsPortalLoader); other
    loaders skip. Returns events whose score was written.

    Resilience (cactusbets.cloud prod fix, 2026-06-22): each finished link is
    scraped + COMMITTED INDIVIDUALLY under a ``per_link_timeout`` (so one hung
    VPS proxy request can't stall the pass), and a ``time_budget`` ends the
    cycle cleanly once spent. Both default ``None`` (no bound) for in-process
    callers/tests; the scheduler injects concrete values from Settings so prod
    works with no manual config. A per-link failure is logged (type + status
    only — never the URL) and the link is simply retried next cycle.
    """
    fetch = getattr(loader, "fetch_match_odds", None)
    if fetch is None:
        return 0
    now = now or datetime.now(tz=UTC)
    window = window or RESULTS_SCRAPE_WINDOW  # wider = clear an older backlog
    limit = limit or RESULTS_SCRAPE_MAX_PER_CYCLE
    async with session_factory() as session:
        links = (
            await session.execute(
                select(
                    Event.external_ref,
                    (Event.starts_at < now - _finished_floor(sport_key)).label("past_full_floor"),
                )
                .join(Pick, Pick.event_id == Event.id)
                .join(Sport, Sport.id == Event.sport_id)
                .where(
                    # Route by the DB sport (authoritative) — NOT by parsing the
                    # match URL. The URL is reused exactly as it was scraped
                    # (external_ref), so an OddsPortal per-game-type URL change can
                    # never misroute or silently drop a finished score: the stored
                    # URL is re-fetched as-is (fetch ... prefiltered=True below).
                    Sport.key == sport_key,
                    Pick.status == "alerted",
                    Event.starts_at.is_not(None),
                    # SOFT floor: surface finished games within minutes of FT.
                    # The per-link finished-status gate is the real safeguard
                    # (in-play partial rejected; status-missing below the full
                    # sport floor deferred — see _scrape_one_finished_score).
                    Event.starts_at < now - _RESULTS_SOFT_FLOOR,
                    Event.starts_at > now - window,
                    Event.scraped_home_score.is_(None),
                )
                .distinct()
                .limit(limit)
            )
        ).all()
    if not links:
        return 0
    written = 0
    timed_out = 0
    deadline = time.monotonic() + time_budget if time_budget is not None else None
    for ref, past_full_floor in links:
        if deadline is not None and time.monotonic() >= deadline:
            # Budget spent: stop CLEANLY. Everything committed so far is durable;
            # the un-scraped remainder simply drains over the next cycles.
            logger.info(
                "results scrape %s: per-cycle time budget (%.0fs) reached after %d "
                "score(s) — remaining links retried next cycle",
                sport_key,
                time_budget,
                written,
            )
            break
        try:
            written += await _scrape_one_finished_score(
                fetch,
                session_factory,
                directory,
                sport_key,
                ref,
                bool(past_full_floor),
                per_link_timeout,
            )
        except TimeoutError:
            # asyncio.wait_for raises the builtin TimeoutError (3.11+). A single
            # hung/slow proxy request — drop just this link; the already-committed
            # scores of earlier links are untouched.
            timed_out += 1
            logger.warning(
                "results scrape %s: a match-page fetch timed out (>%ss) — "
                "skipping that link this cycle",
                sport_key,
                per_link_timeout,
            )
        except Exception as exc:  # one bad link must never block the others
            logger.error(
                "results scrape %s: a match-page fetch failed: %s — skipping that link",
                sport_key,
                type(exc).__name__,
            )
    if written or timed_out:
        logger.info(
            "results scrape %s: captured %d final score(s) for settlement%s",
            sport_key,
            written,
            f" ({timed_out} link(s) timed out)" if timed_out else "",
        )
    return written


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
    use_betfair_exchange: bool = False,
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
    soft_fresh = last_capture is not None and kickoff - last_capture <= max_gap
    # clv-2: resolve the SHARP-ARCHIVE (Pinnacle/Betfair) close BEFORE the coverage
    # verdict. An event that fell OUT of the soft OddsPortal scrape can still have a
    # FRESH matched sharp close — the close we most want to anchor on. Coverage is
    # satisfied if EITHER the soft scrape is fresh OR a fresh sharp close exists; we
    # return False only when NEITHER is fresh.
    sharp_snaps: list[OddsSnapshotIn] = []
    if use_pinnacle_archive:
        # Matched Pinnacle ARCHIVE close (strict cross-source match) so a real sharp
        # close anchors the fair (value.SHARP_BOOKS[0]=="pinnacle"). No match -> [].
        sharp_snaps.extend(await _pinnacle_archive_close(session, pick, external_ref, kickoff))
    if use_betfair_exchange:
        # Captured Betfair Exchange BACK close (EXACT-match: betfair ref is
        # deterministically "betfair:"+ref). No betfair event / no close -> [].
        # Both flags may be on: event_fair_probs prefers Pinnacle (SHARP_BOOKS[0])
        # over Betfair (index 2), so Pinnacle wins when both price the market.
        sharp_snaps.extend(await _betfair_exchange_close(session, pick, external_ref, kickoff))
    sharp_last = max(
        (s.captured_at for s in sharp_snaps if s.captured_at is not None), default=None
    )
    sharp_fresh = sharp_last is not None and kickoff - sharp_last <= max_gap
    if not soft_fresh and not sharp_fresh:
        logger.info(
            "pick %d: no snapshot-close coverage (neither soft scrape nor sharp "
            "archive fresh within %s of kickoff) — keeping revalidation close",
            pick.id,
            max_gap,
        )
        return False
    if not soft_fresh:
        # The soft scrape is stale (event dropped from coverage); a fresh sharp close
        # must NOT be anchored against a stale soft book set — use the sharp close
        # alone. When soft IS fresh, the original soft set is kept and sharp rows are
        # added below.
        snaps = []
    if sharp_snaps:
        snaps = [*snaps, *sharp_snaps]
        logger.info(
            "pick %d: injected %d sharp archive close rows (fresh=%s)",
            pick.id,
            len(sharp_snaps),
            sharp_fresh,
        )
    grouped = group_market_prices(snaps)
    fair_by_key: dict[tuple[str, str], float] = {}
    anchor_by_key: dict[tuple[str, str], str] = {}
    for (_event, market, _detail), (anchor, fair_by_sel) in event_fair_probs(
        grouped, devig_method
    ).items():
        for sel, p in fair_by_sel.items():
            fair_by_key[(str(market), sel)] = p
            anchor_by_key[(str(market), sel)] = anchor
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
    close_odds: float | None
    if pick.bookmaker in books:
        close_odds = books[pick.bookmaker]
    else:
        # best remaining SOFT close, never a sharp/anchor (audit #3) — and None
        # when only sharp books quote it (no soft close to mark).
        _, close_odds = _best_soft_book(books)
    pick.closing_fair_probability = Decimal(f"{fair:.6f}")
    pick.clv_log = Decimal(f"{clv:.6f}")
    pick.beat_close = clv > 0
    # clv-1: a fair was ANCHORED from our own odds_snapshots history — this IS a
    # genuine snapshot close, regardless of whether a soft book also quoted it
    # (close_odds may be None when only sharp books priced the selection). Mark it
    # explicitly so the trusted sharp-CLV subset is gated on this flag, not on the
    # presence of a soft display price (closing_odds).
    pick.has_snapshot_close = True
    # Keep current_edge consistent with the refreshed close fair (audit 2026-06-26):
    # finalize rewrites the fair, so a stale current_edge would contradict it.
    pick.current_edge = _consistent_current_edge(pick, fair)
    close_anchor = anchor_by_key.get((pick.market, pick.selection))
    if close_anchor:
        # CLOSE-side provenance from the SNAPSHOT close (pinnacle/sharp/
        # consensus). Together with closing_odds (written just below as the
        # snapshot-close marker), a sharp value here marks a genuine sharp
        # close the per-anchor and headline CLV can trust.
        pick.closing_anchor_type = anchor_type_for(close_anchor)
        # INDEPENDENCE provenance (P0-1/P0-3): is the close anchored by a book
        # OTHER than this pick's own fill book? A close priced by the fill book
        # itself is CIRCULAR (closing == fill, |clv_log|~0) — fake CLV that
        # masked the -EV. Stamped beside the anchor type so the trusted sharp
        # subset can exclude self-priced closes. Consensus -> True (a >=3-book
        # median is independent of any single fill by construction).
        pick.close_independent_of_fill = close_is_independent_of_fill(
            close_anchor,
            pick.bookmaker,
            pick_anchor_type=pick.anchor_type or "",
            close_anchor_type=pick.closing_anchor_type or "",
            # CLV-3: a Smarkets-anchored pick validated by a Betfair-exchange close
            # is independent (different sharp BOOKS) though both are anchor_type
            # 'sharp'; book identity is the precise test, type-equality the fallback.
            pick_anchor_book=pick.anchor_book or "",
        )
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


async def resolve_betfair_back_snaps(
    session: "AsyncSession", external_ref: str, kickoff: datetime
) -> list[OddsSnapshotIn]:
    """Captured Betfair Exchange BACK snapshots for an event, re-keyed to its own
    external_ref, or []. EXACT resolution (no fuzzy/alias): the Betfair capture
    persists under external_ref ``"betfair:" + ref`` (betfair_exchange.
    _namespace_event_ref), globally unique, so one deterministic lookup finds it.
    Re-keys rows off the "betfair:" namespace to ``external_ref`` so they group
    with that event's market in event_fair_probs. Used BOTH at settlement (the
    close) and at PICK TIME (the live sharp anchor). Each row carries captured_at,
    so a pick-time caller can gate freshness on the event's most-recent row.
    """
    from app.ingestion.betfair_exchange import _namespace_event_ref

    betfair_ref = _namespace_event_ref(external_ref)
    betfair_event_id = await session.scalar(
        select(Event.id).where(Event.external_ref == betfair_ref)
    )
    if betfair_event_id is None:
        return []
    snaps, _last = await closing_odds_from_snapshots(
        session, betfair_event_id, betfair_ref, kickoff
    )
    return [snap.model_copy(update={"event_id": external_ref}) for snap in snaps]


async def _betfair_exchange_close(
    session: "AsyncSession",
    pick: Pick,
    external_ref: str,
    kickoff: datetime,
) -> list[OddsSnapshotIn]:
    """Settlement-time wrapper: the Betfair Exchange BACK close for this pick."""
    return await resolve_betfair_back_snaps(session, external_ref, kickoff)


def build_sharp_anchor_loader(
    session_factory: "async_sessionmaker",
    directory: EventDirectory,
    *,
    use_betfair: bool,
    use_pinnacle: bool,
    max_age_seconds: float,
) -> Callable[[str, Sequence[OddsSnapshotIn]], Awaitable[list[OddsSnapshotIn]]]:
    """Pick-time SHARP-ANCHOR loader for PipelineDeps.sharp_anchor_loader.

    For each scraped event it returns the captured free Betfair Exchange (EXACT
    ref) and/or Pinnacle ARCADIA (STRICT name match) snapshots re-keyed to that
    event, so run_value_pipeline anchors the pick on the SHARP book instead of
    the soft-book consensus median. Reuses the SAME resolution as the
    settlement-time CLV close — no new false-match surface. Per-event failures
    propagate to the pipeline's isolated try/except (picking never breaks).

    FRESHNESS-GATED (review 2026-06-21): a LIVE pick must anchor on a CURRENT
    sharp line, so any captured snapshot older than ``max_age_seconds`` is
    dropped. The 'old price still valid' (change-only) reasoning applies to the
    settlement CLOSE, never to a live pick-time anchor.
    """
    from app.storage.repositories import resolve_pinnacle_close_snaps

    async def loader(sport_key: str, snapshots: Sequence[OddsSnapshotIn]) -> list[OddsSnapshotIn]:
        base = arcadia_base_sport(sport_key)
        now = datetime.now(tz=UTC)
        out: list[OddsSnapshotIn] = []
        seen: set[str] = set()

        # temporal-leakage-1: gate freshness PER SOURCE, not on a single event-wide
        # clock. A fresh Betfair capture must NOT drag a STALE Pinnacle line in under
        # the event's max(captured_at) — the stale Pinnacle rows would then anchor the
        # live pick on an outdated sharp price (temporal leakage). Each source keeps
        # its rows only if ITS OWN most-recent row is within max_age_seconds (the same
        # change-only 'steady price stays current' logic, applied per source).
        def _fresh_source(rows: list[OddsSnapshotIn]) -> list[OddsSnapshotIn]:
            last = max(
                (s.captured_at for s in rows if s.captured_at is not None),
                default=None,
            )
            if last is None or (now - last).total_seconds() > max_age_seconds:
                return []
            return rows

        async with session_factory() as session:
            for snap in snapshots:
                ref = snap.event_id
                if ref in seen:
                    continue
                seen.add(ref)
                teams = directory.lookup(ref)
                if teams is None or teams.starts_at is None:
                    continue  # need a kickoff for the cutoff + the pinnacle match
                event_snaps: list[OddsSnapshotIn] = []
                if use_betfair:
                    event_snaps.extend(
                        _fresh_source(
                            await resolve_betfair_back_snaps(session, ref, teams.starts_at)
                        )
                    )
                if use_pinnacle:
                    event_snaps.extend(
                        _fresh_source(
                            await resolve_pinnacle_close_snaps(
                                session,
                                pinnacle_sport_key=f"pinnacle_{base}",
                                pick_external_ref=ref,
                                home=teams.home,
                                away=teams.away,
                                kickoff=teams.starts_at,
                            )
                        )
                    )
                if not event_snaps:
                    continue
                out.extend(event_snaps)
        return out

    return loader


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
