"""Persistence for generated picks — closes the loop so /picks serves real data.

Lean entity resolution (get-or-create sport/league/teams/event/model_version),
then insert the pick. Picks are deduped by their natural key
(event, market, selection, model_version) via ON CONFLICT DO NOTHING, so a
re-poll of the same market state never duplicates rows.
"""

import contextlib
import logging
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import and_, func, select, text
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.backtesting.clv import mean_significance, wilson_interval
from app.ingestion.base import EventTeams, prefer_kickoff
from app.schemas.base import Market, to_utc
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut
from app.settlement.outcomes import provisional_result
from app.storage.models import (
    BankrollLedgerEntry,
    BetfairAnchorVerdict,
    DashboardCredential,
    Event,
    EventSourceLink,
    League,
    MatchReviewQueue,
    ModelVersion,
    OddsSnapshot,
    Pick,
    ResultTracking,
    Sport,
    Team,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.backtesting.calibration import BetBandObservation
    from app.backtesting.live_evidence import SettledPickRow
    from app.resolution.shadow import BetfairCoverageOutcome, ShadowOutcome

logger = logging.getLogger(__name__)


#: Bookmaker name the Betfair Exchange capture persists under (mirrors
#: app.ingestion.betfair_exchange.BOOKMAKER). Kept as a local literal so this
#: read-only query module never imports the ingestion layer.
_BETFAIR_BOOKMAKER = "Betfair Exchange"


@dataclass(frozen=True)
class BetfairTarget:
    """One canonical soccer event the Betfair Exchange capture should read this
    cycle: its OddsPortal match URL (the event identity throughout the platform)
    plus the team/league/kickoff context the reader needs to persist the row.

    Sourced from the DB (recent upcoming events that already have odds), NOT from
    the last completed full scrape — so the capture is decoupled from poll_odds
    completion (the prod wedge: one slow CPU-bound scrape held poll_odds's single
    slot, so last_fetch_event_ids stayed empty and the reader saw no targets)."""

    external_ref: str  # the OddsPortal match URL (== Event.external_ref)
    home: str
    away: str
    league: str
    starts_at: datetime | None


#: D1 close-boost band defaults (close-evidence package, 2026-07): events
#: kicking off within the window sort FIRST (soonest kickoff first), capped at
#: the slot count — a pure REALLOCATION of the caller's existing ``limit``
#: (never a raise), so the capture densely observes the final pre-kickoff
#: window and a genuine (non-echo) sharp close exists for CLV. Overridable via
#: BETFAIR_EXCHANGE_CLOSE_BOOST_WINDOW_MINUTES / _SLOTS (0 disables).
BETFAIR_CLOSE_BOOST_WINDOW = timedelta(minutes=75)
BETFAIR_CLOSE_BOOST_SLOTS = 20


async def select_betfair_targets(
    session_factory: "async_sessionmaker",
    *,
    sport: str,
    now: datetime | None = None,
    window: timedelta = timedelta(days=3),
    limit: int = 20,
    boost_window: timedelta = BETFAIR_CLOSE_BOOST_WINDOW,
    boost_slots: int = BETFAIR_CLOSE_BOOST_SLOTS,
) -> list[BetfairTarget]:
    """Bounded, rotating list of canonical ``sport`` events for the Betfair
    Exchange capture to read THIS cycle — read-only (SELECTs only).

    CLOSE-BOOST BAND (D1, close-evidence package): events with
    ``starts_at <= now + boost_window`` sort FIRST (soonest kickoff first),
    capped at ``boost_slots`` — they are the fixtures whose final pre-kickoff
    Betfair row becomes the CLV close, so they must not wait behind the
    rotation while their market disappears. The REMAINDER of ``limit`` keeps
    the never-captured-first/stalest rotation over everything else. This is a
    pure reallocation of the same ``limit`` budget (zero added page load);
    ``boost_slots == 0`` or a non-positive ``boost_window`` disables the band
    (the plain pre-D1 rotation).

    DECOUPLING (prod fix): targets come from the warehouse, not from the loader's
    ``last_fetch_event_ids`` (populated only when a poll_odds full scrape
    COMPLETES). On a CPU-bound box poll_odds skips every slot, so that map stayed
    empty and the capture got nothing — even £270k-liquidity majors. Sourcing
    from the DB means a still-open, already-priced event is a target regardless of
    whether the current scrape finished.

    Eligibility — an event qualifies when it:
      * is in ``sport`` (the canonical namespace, e.g. "soccer"),
      * has a navigable OddsPortal URL ref (``http...``; synthetic
        "home|away|date" ids are skipped — the reader can't open them),
      * has a KNOWN kickoff strictly in the future and at most ``window`` ahead
        (NULL kickoff / already-started events are skipped: the pre-match Betfair
        BACK row is gone and re-reading wastes the scarce per-cycle budget),
      * already has at least one NON-Betfair odds snapshot (the main scrape
        priced it — so it is a real, liquid fixture, not a Betfair-only shell).

    BOUND + ROTATION (CPU-aware): ordered never-captured-first, then
    longest-since-last-Betfair-capture (stalest first), then soonest kickoff,
    then ref for determinism — and capped at ``limit``. A small ``limit`` over
    successive cycles sweeps the whole slate (each cycle the freshly-captured
    events fall to the back), so the capture NEVER opens all ~91 match pages at
    once. The per-cycle page-load cost is therefore exactly ``min(limit, eligible)``.
    """
    now = now or datetime.now(tz=UTC)
    horizon = now + window
    home_t = aliased(Team)
    away_t = aliased(Team)
    # Latest Betfair Exchange capture time for this event (NULL = never): the
    # rotation key. Correlated MAX over the SAME canonical event row (Betfair
    # binds inline onto it, bookmaker="Betfair Exchange").
    last_betfair = (
        select(func.max(OddsSnapshot.captured_at))
        .where(
            OddsSnapshot.event_id == Event.id,
            OddsSnapshot.bookmaker == _BETFAIR_BOOKMAKER,
        )
        .scalar_subquery()
    )
    # The event must have been priced by the MAIN scrape (a non-Betfair snapshot
    # exists) — otherwise it is not a real liquid fixture to read.
    has_real_odds = (
        select(OddsSnapshot.id)
        .where(
            OddsSnapshot.event_id == Event.id,
            OddsSnapshot.bookmaker != _BETFAIR_BOOKMAKER,
        )
        .exists()
    )
    base = (
        select(
            Event.external_ref,
            home_t.name,
            away_t.name,
            League.name,
            Event.starts_at,
        )
        .select_from(Event)
        .join(Sport, Event.sport_id == Sport.id)
        .join(League, Event.league_id == League.id)
        .join(home_t, Event.home_team_id == home_t.id)
        .join(away_t, Event.away_team_id == away_t.id)
        .where(
            Sport.key == sport,
            Event.external_ref.like("http%"),
            Event.starts_at.is_not(None),
            Event.starts_at > now,
            Event.starts_at <= horizon,
            has_real_odds,
        )
    )
    rows: list[Any] = []
    async with session_factory() as session:
        # D1 CLOSE-BOOST BAND first: imminent kickoffs (soonest first), capped at
        # boost_slots and never above the caller's limit — a reallocation, not a
        # budget raise.
        if boost_slots > 0 and boost_window > timedelta(0):
            boost_stmt = (
                base.where(Event.starts_at <= now + boost_window)
                .order_by(Event.starts_at.asc(), Event.external_ref.asc())
                .limit(min(boost_slots, limit))
            )
            rows = list((await session.execute(boost_stmt)).all())
        remaining = limit - len(rows)
        if remaining > 0:
            # never-captured first (NULLS FIRST), then stalest capture, then
            # soonest kickoff, then ref — a total, deterministic rotation order.
            rotation_stmt = base.order_by(
                last_betfair.asc().nulls_first(),
                Event.starts_at.asc(),
                Event.external_ref.asc(),
            ).limit(limit)
            taken = {row[0] for row in rows}
            for row in (await session.execute(rotation_stmt)).all():
                if row[0] in taken:
                    continue  # already claimed by the boost band
                rows.append(row)
                if len(rows) >= limit:
                    break
    return [
        BetfairTarget(
            external_ref=ref,
            home=home,
            away=away,
            league=league,
            starts_at=starts_at,
        )
        for ref, home, away, league, starts_at in rows
    ]


# Race-safe get-or-create (audit #11): a concurrent inserter may create the same
# entity between the SELECT and the INSERT. ON CONFLICT DO NOTHING + re-SELECT
# avoids the IntegrityError that would otherwise abort the session and silently
# drop the pick — the same discipline persist_odds_snapshots already uses. Not
# reachable under today's single sequential writer, but mandatory before any
# parallel writer / second poller.
async def _get_or_create_sport(session: AsyncSession, key: str, name: str) -> int:
    found = await session.scalar(select(Sport.id).where(Sport.key == key))
    if found is not None:
        return found
    await session.execute(
        pg_insert(Sport).values(key=key, name=name).on_conflict_do_nothing(index_elements=["key"])
    )
    found = await session.scalar(select(Sport.id).where(Sport.key == key))
    if found is None:  # pragma: no cover - insert+select in one tx always resolves
        raise RuntimeError(f"could not resolve sport {key!r}")
    return found


async def _get_or_create_league(
    session: AsyncSession, sport_id: int, key: str, country: str = ""
) -> int:
    # Country is part of league IDENTITY (uq_leagues_sport_key_country): the same
    # league KEY in different countries must be DISTINCT rows, else the first-seen
    # country freezes and mislabels the rest ("Ethiopia — Premier League" bug).
    # Normalize NULL/absent to '' — a NULL would be treated as distinct by the
    # unique index and defeat dedup for country-less sources.
    country = country or ""
    where = (League.sport_id == sport_id, League.key == key, League.country == country)
    found = await session.scalar(select(League.id).where(*where))
    if found is not None:
        return found
    await session.execute(
        pg_insert(League)
        .values(sport_id=sport_id, key=key, name=key, country=country)
        .on_conflict_do_nothing(constraint="uq_leagues_sport_key_country")
    )
    found = await session.scalar(select(League.id).where(*where))
    if found is None:  # pragma: no cover
        raise RuntimeError(f"could not resolve league {key!r}")
    return found


_LIVE_STATUS_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


def _strip_live_status(name: str) -> str:
    """Drop a trailing scraper live-status token (e.g. ' [In Running]') that the
    in-play OddsChecker/OddsPortal pages append to team names. It is NOT a
    distinguishing fixture marker, so leaving it forks a team's canonical identity
    (a clean-named twin already exists) and fragments stats/matching/settlement."""
    return _LIVE_STATUS_RE.sub("", name).strip()


async def _get_or_create_team(
    session: AsyncSession, sport_id: int, league_id: int, name: str
) -> int:
    name = _strip_live_status(name)
    normalized = name.strip().lower()
    where = (Team.sport_id == sport_id, Team.normalized_name == normalized)
    found = await session.scalar(select(Team.id).where(*where))
    if found is not None:
        return found
    await session.execute(
        pg_insert(Team)
        .values(sport_id=sport_id, league_id=league_id, name=name, normalized_name=normalized)
        .on_conflict_do_nothing(constraint="uq_teams_sport_normalized")
    )
    found = await session.scalar(select(Team.id).where(*where))
    if found is None:  # pragma: no cover
        raise RuntimeError(f"could not resolve team {name!r}")
    return found


# Forward mint-time dedup resolver (PR1a). Two same-sport events with the same
# ORIENTED team pair whose kickoffs fall within this bound are the SAME real
# fixture — a deterministic key, no fuzzy matching, so it can never merge two
# DISTINCT games (the same two teams cannot start a second meeting within ~2h;
# leg reversals swap the ids and so miss the oriented key entirely).
_RESOLVER_TOLERANCE = timedelta(hours=2)


def _source_of_ref(external_ref: str) -> str:
    """The event_source_links.source tag for an external_ref — the prefix before
    the first ':' ('oddschecker:101644967' -> 'oddschecker'); 'unknown' for a
    ref with no prefix."""
    prefix = external_ref.split(":", 1)[0]
    return prefix if prefix and prefix != external_ref else "unknown"


def _is_date_only_midnight(dt: datetime) -> bool:
    """True for the 00:00:00 date-only sentinel (OddsPortal's date-only header) —
    a placeholder, never a real kickoff, so the resolver must not key on it (it
    would collapse distinct same-day fixtures stored under the placeholder)."""
    return dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0


async def _resolve_canonical_event(
    session: AsyncSession,
    sport_id: int,
    home_id: int,
    away_id: int,
    starts_at: datetime,
    external_ref: str,
) -> int | None:
    """Tier-1: the existing canonical event for this fixture (same sport, same
    ORIENTED team pair, kickoff within _RESOLVER_TOLERANCE), or None.

    On a hit: upgrades the canonical kickoff (prefer_kickoff — a real time from
    this source improves a stored midnight/NULL) and records the source ref in
    event_source_links. The link is a best-effort fast-path + audit cache;
    correctness never depends on it — a lost write just means the next cycle
    re-resolves to the SAME id deterministically. Multiple in-window rows are all
    the same fixture (same oriented ids), so it collapses to the nearest."""
    rows = (
        await session.execute(
            select(Event.id, Event.external_ref, Event.starts_at).where(
                Event.sport_id == sport_id,
                Event.home_team_id == home_id,
                Event.away_team_id == away_id,
                Event.starts_at.is_not(None),
                Event.starts_at >= starts_at - _RESOLVER_TOLERANCE,
                Event.starts_at <= starts_at + _RESOLVER_TOLERANCE,
            )
        )
    ).all()
    if not rows:
        return None
    canon_id, canon_ref, _ = min(
        rows, key=lambda r: (abs((r[2] - starts_at).total_seconds()), r[0])
    )
    canon = await session.get(Event, canon_id)
    if canon is not None:
        target = prefer_kickoff(canon.starts_at, starts_at)
        if target != canon.starts_at:
            canon.starts_at = target
    await upsert_event_source_links(
        session,
        [
            SourceLinkByRef(
                source=_source_of_ref(external_ref),
                source_event_id=external_ref,
                canonical_external_ref=canon_ref,
                confidence=1.0,
                method="exact_team_id" if len(rows) == 1 else "exact_team_id_multi",
                matched_at=datetime.now(UTC),
            )
        ],
    )
    await session.flush()
    return canon_id


async def _resolve_canonical_event_by_pair(
    session: AsyncSession,
    sport_id: int,
    home_id: int,
    away_id: int,
    starts_at: datetime,
    external_ref: str,
) -> int | None:
    """Tier-2: the existing canonical event for this fixture matched by the
    NORMALIZED UNORDERED team pair (``fixture_pair_key``) within the sport-aware
    ``_dedup_tolerance`` — or None. Runs ONLY after Tier-1's exact team-id key
    misses.

    Catches ``[In Running]`` / name-twin forks where the two source rows minted
    DIFFERENT ``home_team_id``/``away_team_id`` (so the deterministic Tier-1 key
    misses) yet the normalized unordered team pair is identical — e.g. a
    club-form suffix ("Alpha FC" vs "Alpha") or an accent variant. Uses the SAME
    proven pure ``fixture_pair_key`` the settlement dedup guard uses: strict
    normalized-pair equality only, NO fuzzy/substring matching, so a distinct
    club sharing a base token ("CD Nacional" vs "Nacional") never merges.

    Safety (wrong-game-unsafe zone): never crosses ``sport_id``; excludes NULL
    and date-only-midnight kickoffs on BOTH sides (unsafe merge keys); requires
    the same UNORDERED normalized pair AND a kickoff within the per-sport
    tolerance (tennis 6h — a 1v1 pair meets once/day; team sports 2h). When
    several canonicals match, the LOWEST-id one is chosen (deterministic). On a
    hit: upgrade the canonical kickoff (prefer_kickoff) and record the source ref
    in ``event_source_links`` with method ``fixture_pair_key`` — the same
    best-effort link + return contract as Tier-1."""
    # Lazy imports: the pure fixture-pair key + the sport-aware tolerance already
    # proven by the settlement dedup guard. Lazy to keep this module's import
    # graph free of the settlement engine at load time.
    from app.resolution.matching import fixture_pair_key
    from app.settlement.engine import _dedup_tolerance

    name_rows = (
        await session.execute(select(Team.id, Team.name).where(Team.id.in_({home_id, away_id})))
    ).all()
    names = {tid: name for tid, name in name_rows}
    target_pair = fixture_pair_key(names.get(home_id, ""), names.get(away_id, ""))
    if target_pair is None:  # degenerate (empty side / both sides normalize equal)
        return None

    sport_key = await session.scalar(select(Sport.key).where(Sport.id == sport_id))
    tol = _dedup_tolerance(sport_key)
    home_t, away_t = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(Event.id, Event.external_ref, Event.starts_at, home_t.name, away_t.name)
            .select_from(Event)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .where(
                Event.sport_id == sport_id,
                Event.external_ref != external_ref,
                Event.starts_at.is_not(None),
                Event.starts_at >= starts_at - tol,
                Event.starts_at <= starts_at + tol,
            )
        )
    ).all()
    matches = [
        (eid, ref, kickoff)
        for (eid, ref, kickoff, cand_home, cand_away) in rows
        if not _is_date_only_midnight(kickoff)  # candidate placeholder excluded
        and fixture_pair_key(cand_home, cand_away) == target_pair
    ]
    if not matches:
        return None
    # Deterministic: lowest-id canonical when several same-fixture rows match.
    canon_id, canon_ref, _ = min(matches, key=lambda r: r[0])
    canon = await session.get(Event, canon_id)
    if canon is not None:
        target = prefer_kickoff(canon.starts_at, starts_at)
        if target != canon.starts_at:
            canon.starts_at = target
    await upsert_event_source_links(
        session,
        [
            SourceLinkByRef(
                source=_source_of_ref(external_ref),
                source_event_id=external_ref,
                canonical_external_ref=canon_ref,
                confidence=1.0,
                method="fixture_pair_key",
                matched_at=datetime.now(UTC),
            )
        ],
    )
    await session.flush()
    return canon_id


async def _get_or_create_event(
    session: AsyncSession,
    sport_id: int,
    league_id: int,
    home_id: int,
    away_id: int,
    external_ref: str,
    starts_at: datetime | None,
) -> int:
    """starts_at=None means the source reported no kickoff — stored as NULL
    (the dashboard's "TBD" signal), never as a pick-time placeholder.

    Event.scraped_home_score/away_score are written ONLY by the finished-gated
    capture_finished_scores path (app/clv_trueup.py) — NEVER by this routine
    scrape upsert. A pre-kickoff / in-play scrape carries no FINAL score
    (OddsPortal shows a live running score, OddsHarvester exposes no finished
    flag), so letting it write scraped_* could record an in-play partial as the
    result and corrupt settlement + ROI (review 2026-06-21)."""
    # STAGE 0b — REDIRECT consult (PR2b live-shell guard). A fold event that the
    # PR2b merge could not delete (still referenced by an actively-live fixture)
    # lingers as a shell whose OWN external_ref equals this incoming ref — so the
    # fast-path below would re-SELECT that fold row and re-mint duplicate picks on
    # it (the operator then sees one fixture double-alerted on two event rows).
    # An ACTIVE event_source_link whose source_event_id is this exact ref and
    # whose canonical target is a DIFFERENT, SAME-SPORT event is a REDIRECT
    # (written by PR2b Phase 4): resolve to that keep event and NEVER touch the
    # fold shell. Runs BEFORE the own-row fast-path so the redirect wins over the
    # lingering shell. Safety: EXACT (source, source_event_id) ref match only (no
    # fuzzy — source_event_id encodes the source prefix); lowest-id canonical wins
    # (deterministic, mirrors PR2b keep = min(id)); a cross-sport target
    # (Event.sport_id != sport_id) or a missing/dangling target is IGNORED — fall
    # through to the fast-path / mint (fail toward a separate event, never across
    # sport). This is the same active-link lookup Stage-1 uses, ordered for the
    # fold case and sport-fenced.
    redirect_id = await session.scalar(
        select(Event.id)
        .join(EventSourceLink, EventSourceLink.canonical_event_id == Event.id)
        .where(
            EventSourceLink.source_event_id == external_ref,
            EventSourceLink.active.is_(True),
            Event.sport_id == sport_id,
            Event.external_ref != external_ref,
        )
        .order_by(Event.id.asc())
        .limit(1)
    )
    if redirect_id is not None:
        canon = await session.get(Event, redirect_id)
        if canon is not None:
            target = prefer_kickoff(canon.starts_at, starts_at)
            if target != canon.starts_at:
                canon.starts_at = target
                await session.flush()
            return canon.id
    existing = await session.scalar(select(Event).where(Event.external_ref == external_ref))
    if existing is not None:
        # Earlier rows may be NULL (or carry a legacy placeholder); a real kickoff
        # from the source upgrades them. Apply the SAME precedence rule as the
        # in-memory EventDirectory (app.ingestion.base.prefer_kickoff): a real time
        # always wins, but a date-only midnight (00:00:00 UTC sentinel) or a None
        # must NEVER overwrite an already-stored REAL time. Without this, the
        # residual-tail midnight (OddsPortal's date-only basketball header) clobbers
        # a real time captured on an earlier cycle (root cause 2026-06-24).
        target = prefer_kickoff(existing.starts_at, starts_at)
        if target != existing.starts_at:
            existing.starts_at = target
            await session.flush()
        return existing.id
    # STAGE 1 — link fast-path: this ref was merged into a canonical on an earlier
    # cycle (no own event row, only an active source link). Deterministic
    # (most-recent active link) so a re-get never splits back into two rows.
    linked_id = await session.scalar(
        select(EventSourceLink.canonical_event_id)
        .where(
            EventSourceLink.source_event_id == external_ref,
            EventSourceLink.active.is_(True),
        )
        .order_by(EventSourceLink.matched_at.desc(), EventSourceLink.id.desc())
        .limit(1)
    )
    if linked_id is not None:
        canon = await session.get(Event, linked_id)
        if canon is not None:
            target = prefer_kickoff(canon.starts_at, starts_at)
            if target != canon.starts_at:
                canon.starts_at = target
                await session.flush()
            return canon.id
        # dangling link (canonical row gone) -> fall through to Tier-1 / mint
    # STAGE 2 — Tier-1 deterministic cross-source resolve. Only with a REAL
    # kickoff: NULL and the date-only-midnight sentinel are unsafe merge keys.
    if starts_at is not None and not _is_date_only_midnight(starts_at):
        canonical_id = await _resolve_canonical_event(
            session, sport_id, home_id, away_id, starts_at, external_ref
        )
        if canonical_id is not None:
            return canonical_id
        # STAGE 2b — Tier-2 fixture_pair_key resolve: an [In Running]/name-twin
        # fork minted DIFFERENT team ids (Tier-1 exact-id key missed) but the
        # normalized unordered team pair is identical. Same real-kickoff gate.
        canonical_id = await _resolve_canonical_event_by_pair(
            session, sport_id, home_id, away_id, starts_at, external_ref
        )
        if canonical_id is not None:
            return canonical_id
    # STAGE 3 — mint (unchanged): a genuinely new fixture.
    await session.execute(
        pg_insert(Event)
        .values(
            sport_id=sport_id,
            league_id=league_id,
            home_team_id=home_id,
            away_team_id=away_id,
            external_ref=external_ref,
            starts_at=starts_at,
        )
        .on_conflict_do_nothing(constraint="uq_events_external_ref")
    )
    event_id = await session.scalar(select(Event.id).where(Event.external_ref == external_ref))
    if event_id is None:  # pragma: no cover
        raise RuntimeError(f"could not resolve event {external_ref!r}")
    return event_id


async def _get_or_create_model_version(
    session: AsyncSession, sport_id: int, name: str, version: str
) -> int:
    where = (
        ModelVersion.sport_id == sport_id,
        ModelVersion.name == name,
        ModelVersion.version == version,
    )
    found = await session.scalar(select(ModelVersion.id).where(*where))
    if found is not None:
        return found
    await session.execute(
        pg_insert(ModelVersion)
        .values(name=name, version=version, sport_id=sport_id)
        .on_conflict_do_nothing(constraint="uq_model_versions_sport_name_version")
    )
    found = await session.scalar(select(ModelVersion.id).where(*where))
    if found is None:  # pragma: no cover
        raise RuntimeError(f"could not resolve model version {name!r}/{version!r}")
    return found


def _provisional_result_fields(
    pick: Pick, home: str, away: str, shs: int | None, saws: int | None
) -> dict[str, str | None]:
    """CLOSED-tab read-time RESULT: how the value bet landed from the scraped
    final score, BEFORE formal settlement. provisional_* are null until a final
    score exists / when the selection can't be graded; the SETTLED tab still
    uses the authoritative persisted outcome + P&L (ResultTracking)."""
    # Superseded (deduplicated) picks are terminal-but-resultless: they never
    # settle and carry no ResultTracking row, yet a scraped final score would
    # otherwise grade them here — inflating the dashboard's provisional-keyed
    # count tiles ("Settled (loaded)") with dedup'd bets that never actually
    # landed. Suppress the provisional grade so a superseded row is never
    # counted as settled/landed (the P&L ledger already excludes them).
    if pick.status == "superseded":
        return {"provisional_outcome": None, "provisional_pnl": None}
    outcome, pnl = provisional_result(
        pick.market,
        pick.selection,
        home,
        away,
        shs,
        saws,
        pick.recommended_stake_amount,
        pick.decimal_odds,
    )
    return {"provisional_outcome": outcome, "provisional_pnl": pnl}


def banner_fair_odds(
    closing_fair_probability: float | None, model_probability: float | None
) -> str | None:
    """FIX 5: the reconciled fair ODDS string for the /picks banner.

    Reasons against the SAME fair as ``min_acceptable_odds`` and the live edge:
    the re-priced live fair (``closing_fair_probability``) once it exists, else
    the entry sharp fair (``model_probability``). Deliberately NOT
    ``fair_probability`` — on value picks that column stores the OFFERED implied
    prob, so 1/fair_probability structurally equals the offered odds (the
    reported "OFFERED 1.67 == FAIR 1.67" self-contradiction). Returns None for a
    degenerate stored prob (outside (0, 1)). Pure function."""
    fair = closing_fair_probability if closing_fair_probability is not None else model_probability
    if fair is None or not 0.0 < fair < 1.0:
        return None
    return f"{1.0 / fair:.2f}"


async def latest_picks_with_events(
    session: AsyncSession,
    limit: int = 50,
    tier: str | None = None,
    min_edge: float | None = None,
    volume_min_edge: float | None = None,
) -> list[dict[str, Any]]:
    """Latest picks joined with their event (match label, league, kickoff) —
    the payload served by GET /picks and rendered by the dashboard.
    All datetimes are UTC ISO-8601; the frontend converts for display only.

    `tier` scopes the window SERVER-side ("premium"/"volume"; None = both):
    the volume shadow tier runs ~6x premium volume, so an unscoped
    latest-N window fills with volume rows and pushes open premium picks
    out of the feed entirely.

    `min_edge` (Settings.value_min_edge, passed by the route) adds
    `min_acceptable_odds` per row — "still +EV down to X.XX": the minimum
    displayed odds retaining >= that edge vs the pick's sharp fair prob.
    Each row also carries `edge_floor` — the tier-resolved minimum edge the pick
    was held to (volume_min_edge on volume rows, min_edge on premium) — so the
    dashboard colours edges/verdicts against the row's OWN floor instead of a
    hardcoded 3% (dash-2 / EEV-1).
    VALUE-strategy semantics: `model_probability` holds the devigged sharp
    fair probability on value picks (the deployed strategy — the dashboard
    documents the same caveat for its Fair column)."""
    from app.edge.value import ceil_odds, min_acceptable_odds

    def _edge_floor(p: Pick) -> str | None:
        # The floor the pick was actually minted at: volume rows at
        # volume_min_edge, premium at min_edge (mirrors _min_acceptable). null
        # when the route passed no threshold (legacy feed) — the dashboard then
        # falls back to the /health value_min_edge / value_volume_min_edge.
        eff = volume_min_edge if (volume_min_edge is not None and p.tier == "volume") else min_edge
        return str(eff) if eff is not None else None

    def _min_acceptable(p: Pick) -> str | None:
        # Use the floor the pick was actually held to: volume-tier picks are minted at
        # volume_min_edge, premium at min_edge (audit #2) — the wrong tier shows a
        # stricter-than-real floor on volume rows.
        eff_min_edge = (
            volume_min_edge if (volume_min_edge is not None and p.tier == "volume") else min_edge
        )
        if eff_min_edge is None:
            return None
        # Reason against the SAME fair the LIVE verdict (current_edge) uses: the
        # re-priced live fair (closing_fair_probability) once a re-price exists, else
        # the entry fair (model_probability). Otherwise "ok >= X" (entry fair) would
        # contradict "no value now" (live fair) at an UNCHANGED price (audit 2026-06-26).
        fair = (
            float(p.closing_fair_probability)
            if p.closing_fair_probability is not None
            else float(p.model_probability)
        )
        if not 0.0 < fair < 1.0:
            return None  # degenerate stored prob: no honest floor exists
        floor = min_acceptable_odds(fair, eff_min_edge, book=p.current_bookmaker or p.bookmaker)
        return f"{ceil_odds(floor):.2f}" if floor is not None else None

    def _fair_odds(p: Pick) -> str | None:
        # FIX 5: the banner's "Fair odds" must reference the SAME fair the Edge
        # and Min-acceptable numbers reason against — NOT fair_probability (see
        # banner_fair_odds). Delegated to the module-level pure helper.
        return banner_fair_odds(
            float(p.closing_fair_probability) if p.closing_fair_probability is not None else None,
            float(p.model_probability) if p.model_probability is not None else None,
        )

    home = aliased(Team)
    away = aliased(Team)
    # LEFT JOIN ResultTracking so settled rows carry their recorded outcome and
    # realized P&L (the dashboard SETTLED tab's Result/P&L columns). The join is
    # outer: open/unverified picks have no result row and keep outcome/pnl NULL.
    stmt = (
        select(
            Pick,
            home.name,
            away.name,
            League.name,
            League.country,
            Event.starts_at,
            ResultTracking.outcome,
            ResultTracking.pnl,
            ResultTracking.home_score,
            ResultTracking.away_score,
            Event.scraped_home_score,
            Event.scraped_away_score,
            Sport.key,
        )
        .join(Event, Pick.event_id == Event.id)
        .join(Sport, Event.sport_id == Sport.id)
        .join(home, Event.home_team_id == home.id)
        .join(away, Event.away_team_id == away.id)
        .join(League, Event.league_id == League.id)
        .outerjoin(ResultTracking, ResultTracking.pick_id == Pick.id)
    )
    if tier is not None:
        stmt = stmt.where(Pick.tier == tier)
    rows = await session.execute(stmt.order_by(Pick.created_at.desc()).limit(limit))
    return [
        {
            "id": p.id,
            "event_id": p.event_id,
            "event": f"{home_name} vs {away_name}",
            "league": league_name,
            # league's country (OddsPortal eventData.countryName) — disambiguates
            # same-named leagues on the dashboard ("Ethiopia — Premier League").
            "country": league_country or "",
            # null = kickoff unknown ("TBD" row: no countdown, no settle)
            "starts_at": starts_at.isoformat() if starts_at is not None else None,
            "market": p.market,
            "selection": p.selection,
            "bookmaker": p.bookmaker,
            "decimal_odds": str(p.decimal_odds),
            "model_probability": str(p.model_probability),
            "fair_probability": str(p.fair_probability),
            "edge": str(p.edge),
            "ev": str(p.ev),
            "confidence": str(p.confidence),
            "recommended_stake_fraction": str(p.recommended_stake_fraction),
            "recommended_stake_amount": str(p.recommended_stake_amount),
            "reason_summary": p.reason_summary,
            "status": p.status,
            # "premium" = alerted tier; "volume" = CLV-evidence shadow tier
            # (default view on the dashboard shows premium only).
            "tier": p.tier,
            # value-filter meta-model score (null = unscored / out of scope)
            "value_filter_score": (
                str(p.value_filter_score) if p.value_filter_score is not None else None
            ),
            # fair-value anchor that produced the pick (pinnacle/sharp/
            # consensus) — live CLV stratification key; null = model pick
            # or pre-column row
            "anchor_type": p.anchor_type,
            # anchor MATCH-CONFIDENCE provenance (observability): matcher
            # min-side JW in [0,1] (string-serialized NUMERIC, like the other
            # Decimal fields) + the accept method. null = consensus/model pick
            # or pre-column row.
            "anchor_match_confidence": (
                str(p.anchor_match_confidence) if p.anchor_match_confidence is not None else None
            ),
            "anchor_match_method": p.anchor_match_method,
            # Betfair staleness-guard mint stamp (observability only): effective
            # verdict at mint (pass/demote/no_api_match/no_api_price/stale_api);
            # null = guard off / no verdict / non-H2H / pre-column row.
            "anchor_staleness_decision": p.anchor_staleness_decision,
            # sport of the pick (soccer/basketball/tennis/american_football) +
            # human label, so the multi-sport picks table can badge each row and
            # tag UNVALIDATED (experimental) sports honestly.
            "sport": sport_key,
            "sport_label": _sport_label(sport_key, sport_key),
            # CLOSE-anchor provenance (ADR-0017): which anchor priced the close
            # (pinnacle/sharp/consensus). With closing_odds set it marks a
            # genuine sharp close vs a consensus/fallback one.
            "closing_anchor_type": p.closing_anchor_type,
            # CLV-1: per-row close independence — True = the close was anchored by a
            # book OTHER than the pick's own fill book (a genuine, independent close);
            # False = circular self-priced close (fake CLV, |clv_log|~0); null =
            # unknown / no snapshot close yet. Drives the per-pick CLV tile's trust
            # marker so a circular close is never shown as honest CLV.
            "close_independent_of_fill": p.close_independent_of_fill,
            "created_at": p.created_at.isoformat(),
            "clv_log": str(p.clv_log) if p.clv_log is not None else None,
            "beat_close": p.beat_close,
            "current_odds": str(p.current_odds) if p.current_odds is not None else None,
            "current_edge": str(p.current_edge) if p.current_edge is not None else None,
            # the LIVE re-priced fair the current_edge + "ok >=" floor reason against
            # (entry fair is model_probability; this is what moved, not the odds).
            "closing_fair_probability": str(p.closing_fair_probability)
            if p.closing_fair_probability is not None
            else None,
            # the de-vigged CLOSING price (last odds before kickoff), set at
            # settlement by finalize_closing_from_snapshots. null until then —
            # for a kicked-off-but-unsettled pick the frozen current_odds is the
            # de-facto close (re-pricing stops at kickoff). The dashboard shows
            # "close X.XX" so the pick→close price move is visible alongside CLV.
            "closing_odds": str(p.closing_odds) if p.closing_odds is not None else None,
            # the book current_odds came from (= p.bookmaker by default; differs
            # only when the original book dropped the selection at revalidation)
            "current_bookmaker": p.current_bookmaker,
            "revalidated_at": p.revalidated_at.isoformat() if p.revalidated_at else None,
            # execution helper: "still +EV down to X.XX" (null = not
            # computable — min_edge unset or fair prob >= floor impossible)
            "min_acceptable_odds": _min_acceptable(p),
            # FIX 5: reconciled fair ODDS for the banner — 1/(closing_fair_probability
            # ?? model_probability), the SAME fair min_acceptable_odds and the live
            # edge reason against. NOT 1/fair_probability (which equals offered on
            # value picks). null = degenerate stored prob.
            "fair_odds": _fair_odds(p),
            # tier-resolved edge floor (premium=min_edge, volume=volume_min_edge)
            # so the dashboard colours edges/verdicts tier-aware (dash-2/EEV-1).
            "edge_floor": _edge_floor(p),
            # settlement result + realized P&L from ResultTracking (LEFT JOIN):
            # the dashboard SETTLED tab's Result/P&L columns. null = no result
            # row yet (open/unverified picks, or settled-but-unrecorded).
            "outcome": outcome,
            "pnl": str(pnl) if pnl is not None else None,
            # final score of the settled game (HOME-AWAY, e.g. "2-1") from
            # ResultTracking; null until settled or when no score was recorded
            # (void settlements, pre-column rows). The dashboard SETTLED view's
            # Score column.
            "score": f"{hs}-{aws}" if hs is not None and aws is not None else None,
            # best-effort scraped final score (HOME-AWAY, e.g. "2-1") from the
            # EVENT, captured only when we scraped the match after it finished.
            # CONVENIENCE pre-fill for the manual settle prompt + a CLOSED-tab
            # hint — NOT the confirmed result (that is `score`, above). null when
            # either side is unscraped (the common case — the user types as today).
            "scraped_score": (f"{shs}-{saws}" if shs is not None and saws is not None else None),
            # CLOSED-tab read-time RESULT: how the value bet landed from the
            # scraped final score, BEFORE formal settlement (null until a score
            # exists / if ungradeable). SETTLED uses the authoritative outcome.
            **_provisional_result_fields(p, home_name, away_name, shs, saws),
        }
        for (
            p,
            home_name,
            away_name,
            league_name,
            league_country,
            starts_at,
            outcome,
            pnl,
            hs,
            aws,
            shs,
            saws,
            sport_key,
        ) in rows.all()
    ]


def _sport_label(sport_key: str, sport_name: str) -> str:
    if sport_key.startswith("soccer"):
        return "Football"
    if sport_key.startswith("basketball"):
        return "Basketball"  # ALL basketball scraped, not NBA-only
    if sport_key.startswith("tennis"):
        return "Tennis"
    if sport_key.startswith("american_football"):
        return "NFL"
    return sport_name


# Sports that have cleared the held-out CLV doctrine gate and are alerted as
# picks. Everything else (tennis, NFL — and, since the Batch 3 audit 2026-06-26,
# basketball, demoted to EXPERIMENTAL/shadow until its per-sport CLV clears) is
# VISIBILITY-ONLY / UNVALIDATED and the dashboard badges it. Mirrors
# app/pipeline.visibility_only_sports + experimental_sports (the runtime sets),
# but is the warehouse-path source of truth: the restart-durability query has no
# access to the in-memory pipeline registry. Basketball is intentionally absent —
# it is shown (the display query lists it explicitly) but badged unvalidated.
_VALIDATED_SPORT_PREFIXES = ("soccer",)


def _is_unvalidated_sport(sport_key: str) -> bool:
    return not sport_key.startswith(_VALIDATED_SPORT_PREFIXES)


async def latest_available_games_with_events(
    session: AsyncSession,
    limit: int = 1000,
    sport: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Durable fallback for GET /games, rebuilt from the warehouse.

    The live pipeline publishes the freshest poll slate in memory. After a
    deploy/restart that registry is empty until the first poll, while the
    dashboard can still show picks from Postgres. This query makes the games
    table survive restarts by reading current events and their latest
    odds-snapshot coverage from the warehouse — the validated alerting sports
    (football, NBA) AND the visibility-only ones (tennis, NFL). It does not
    apply pick status, edge, tier, exposure, or odds-age gates.
    """
    as_of = now or datetime.now(tz=UTC)
    event_cutoff = as_of - timedelta(hours=12)
    recent_odds_cutoff = as_of - timedelta(hours=24)
    # Hide already-FINISHED games: a fixture whose kickoff is more than this long
    # ago is over and must not render as bettable in GET /games (the old query had
    # NO upper bound, so kicked-off events with recent odds leaked in). A NULL
    # kickoff (TBD) is kept — it has no finish to be past. 3h30m covers a full
    # match incl. stoppage/extra-time/penalties so a live fixture is never hidden.
    in_play_grace = as_of - timedelta(hours=3, minutes=30)

    home = aliased(Team)
    away = aliased(Team)
    market_values = (
        func.array_agg(OddsSnapshot.market.distinct())
        .filter(OddsSnapshot.market.is_not(None))
        .label("markets")
    )
    bookmaker_values = (
        func.array_agg(OddsSnapshot.bookmaker.distinct())
        .filter(OddsSnapshot.bookmaker.is_not(None))
        .label("bookmakers")
    )

    stmt = (
        select(
            Sport.key,
            Sport.name,
            Event.external_ref,
            home.name,
            away.name,
            League.name,
            Event.starts_at,
            func.count(OddsSnapshot.id).label("snapshot_count"),
            func.min(OddsSnapshot.captured_at).label("first_captured_at"),
            func.max(OddsSnapshot.captured_at).label("last_captured_at"),
            func.max(OddsSnapshot.ingested_at).label("updated_at"),
            market_values,
            bookmaker_values,
        )
        .join(Sport, Event.sport_id == Sport.id)
        .join(League, Event.league_id == League.id)
        .join(home, Event.home_team_id == home.id)
        .join(away, Event.away_team_id == away.id)
        .outerjoin(OddsSnapshot, OddsSnapshot.event_id == Event.id)
        .where(
            (Event.starts_at >= event_cutoff) | (OddsSnapshot.ingested_at >= recent_odds_cutoff),
            (Event.starts_at.is_(None)) | (Event.starts_at > in_play_grace),
        )
        .group_by(
            Sport.key,
            Sport.name,
            Event.external_ref,
            home.name,
            away.name,
            League.name,
            Event.starts_at,
        )
        .order_by(Event.starts_at.is_(None), Event.starts_at, home.name, away.name)
        .limit(limit)
    )
    if sport is None:
        # Include the validated alerting sports AND visibility-only sports
        # (tennis, american_football): the in-memory pipeline publishes these to
        # AVAILABLE GAMES, so the restart-durability fallback must too — otherwise
        # they vanish from the view (with their UNVALIDATED badge) until the first
        # poll. Visibility-only membership is enforced elsewhere
        # (_VALIDATED_SPORT_PREFIXES); this query only decides what to DISPLAY.
        stmt = stmt.where(
            (Sport.key == "soccer")
            | Sport.key.startswith("soccer_")
            | (Sport.key == "basketball")
            | Sport.key.startswith("basketball_")
            | (Sport.key == "tennis")
            | Sport.key.startswith("tennis_")
            | (Sport.key == "american_football")
            | Sport.key.startswith("american_football_")
        )
    else:
        stmt = stmt.where((Sport.key == sport) | Sport.key.startswith(f"{sport}_"))

    rows = await session.execute(stmt)
    payload: list[dict[str, Any]] = []
    for (
        sport_key,
        sport_name,
        external_ref,
        home_name,
        away_name,
        league_name,
        starts_at,
        snapshot_count,
        first_captured_at,
        last_captured_at,
        updated_at,
        markets_raw,
        bookmakers_raw,
    ) in rows.all():
        markets = sorted(str(item) for item in (markets_raw or []) if item is not None)
        bookmakers = sorted(str(item) for item in (bookmakers_raw or []) if item is not None)
        payload.append(
            {
                "sport": sport_key,
                "sport_label": _sport_label(sport_key, sport_name),
                "event_id": external_ref,
                "event": f"{home_name} vs {away_name}",
                "home": home_name,
                "away": away_name,
                "league": league_name,
                "starts_at": starts_at.isoformat() if starts_at is not None else None,
                "market_count": len(markets),
                "markets": markets,
                "bookmaker_count": len(bookmakers),
                "bookmakers": bookmakers,
                "snapshot_count": int(snapshot_count or 0),
                "first_captured_at": (
                    first_captured_at.isoformat() if first_captured_at is not None else None
                ),
                "last_captured_at": (
                    last_captured_at.isoformat() if last_captured_at is not None else None
                ),
                "updated_at": (
                    (updated_at or last_captured_at or starts_at).isoformat()
                    if (updated_at or last_captured_at or starts_at) is not None
                    else None
                ),
                # Mirrors the in-memory pipeline contract: VISIBILITY-ONLY sports
                # (tennis) carry unvalidated=True so the dashboard badges them;
                # validated football/NBA rows carry False. Always present so the
                # restart-durability path never strips the doctrine-safety flag.
                "unvalidated": _is_unvalidated_sport(sport_key),
            }
        )
    return payload


async def refresh_event_kickoffs(session: AsyncSession, kickoffs: dict[str, datetime]) -> int:
    """Upgrade stored events' starts_at to the kickoff the source reports.

    Earlier rows carried a pick-time placeholder; this fixes ALL known events
    seen in a scrape, independent of whether their picks re-emit. Returns the
    number of rows changed."""
    if not kickoffs:
        return 0
    changed = 0
    rows = (
        (await session.execute(select(Event).where(Event.external_ref.in_(kickoffs.keys()))))
        .scalars()
        .all()
    )
    for event in rows:
        # Precedence (prefer_kickoff): a real time upgrades a stored midnight/NULL,
        # but a date-only midnight in the refresh map must NOT downgrade an event
        # that already has a REAL time — same rule as the upsert + EventDirectory.
        target = prefer_kickoff(event.starts_at, kickoffs[event.external_ref])
        if event.starts_at != target:
            event.starts_at = target
            changed += 1
    if changed:
        await session.flush()
    return changed


async def load_event_kickoffs(
    session: AsyncSession, external_refs: "Collection[str]"
) -> dict[str, datetime | None]:
    """external_ref -> persisted ``Event.starts_at`` (UTC-aware) for each ref that
    exists in the events table. Absent refs are simply not in the map; a persisted
    NULL kickoff maps to None.

    The pick/CLV pipelines use this to exclude IN-PLAY (post-kickoff) snapshots.
    The PERSISTED kickoff is authoritative when the ephemeral in-memory directory
    copy is late or missing (tennis start times land late) — the leak that let an
    in-play OddsPortal price mint a pre-match pick / stamp a CLV close."""
    refs = list(external_refs)
    if not refs:
        return {}
    rows = (
        await session.execute(
            select(Event.external_ref, Event.starts_at).where(Event.external_ref.in_(refs))
        )
    ).all()
    return {ref: (to_utc(starts_at) if starts_at is not None else None) for ref, starts_at in rows}


# Close anchors that make a close TRUSTABLE for honest CLV — a NAMED sharp book
# priced it, not a soft-book consensus median. Mirrors app/edge/value
# anchor_type_for (pinnacle / sharp); kept local to avoid a heavy import here.
_SHARP_CLOSE_ANCHORS = ("pinnacle", "sharp")

# P2-1 HEADLINE min-n: below this many settled picks the headline roi /
# beat_close_rate / stake-weighted CLV are NOISE (a 10-pick -8.7% reads as
# signal), so they are SUPPRESSED at the source and flagged. Mirrors the
# per-stratum MIN_STRATUM_N honesty gate in app/backtesting/live_evidence.py —
# the headline had no such guard. The trusted sharp subset is gated on its OWN
# n (n_sharp_close), which is naturally thinner than n_settled.
MIN_HEADLINE_N = 50

# CLOSE/FRESHNESS SLA (external-audit item #8) default — REPORT-ONLY. The share
# of a sport-market's settled picks that must carry a TRUSTED independent sharp
# snapshot close (n_sharp_close / n_settled) for that sport-market's CLV/ROI to
# be presented as trustworthy. The live value is Settings.value_close_coverage_sla,
# threaded in by the route (this module stays Settings-free — the default here is
# only the fallback used by callers that don't pass one, e.g. tests). Below the
# SLA the CLAIM is flagged; the picks are never hidden and NO selection/stake/
# threshold changes.
DEFAULT_CLOSE_COVERAGE_SLA = 0.85

# CLV-1 data-error tripwire. A settled pick whose CLOSE-IMPLIED edge
# (closing_fair_probability - 1/decimal_odds) exceeds this ceiling carries a
# physically impossible close: it is the residue of the since-fixed
# double-chance orientation bug (a favorite-side probability mis-assigned to the
# underdog leg), which mints |clv_log| of 0.5-1.76 and a fabricated beat_close.
# Mirrors the mint-side value_max_edge=0.20 guard (app/config.py) that already
# rejects such edges at signal time; kept local as a plain constant to preserve
# this module's no-Settings-import boundary. Such a row stays a real settled pick
# (its pnl/outcome are honest) but its CLV/beat_close are EXCLUDED from the
# blended headline and the trusted sharp subset so fabricated CLV cannot inflate
# either aggregate. A secondary |clv_log| ceiling catches the same pollution when
# the close-implied edge cannot be computed (fair prob or odds absent).
CLV_IMPLAUSIBLE_CLOSE_EDGE = 0.20
CLV_IMPLAUSIBLE_LOG = 0.5

# CLV TAUTOLOGY tripwire (live audit 2026-06-28). When a settled pick's close fair
# equals its pick-time fair (closing_fair_probability == model_probability — the SAME
# archived sharp line reused at pick-time and close-time), clv_log = ln(fill_eff *
# closing_fair) merely re-encodes the pick-time edge: a TAUTOLOGY, not close evidence
# (133/272 settled picks had round(model,4) == round(close_fair,4) yet a nonzero clv_log).
# Such rows are EXCLUDED from BOTH the blended headline and the trusted sharp subset.
# Kept local (mirrors app.edge.value.CLV_TAUTOLOGY_EPS = 1e-3, the 4-dp archived-line
# resolution) to preserve this module's no-Settings-import boundary.
CLV_TAUTOLOGY_EPS = 1e-3

# D4 close-age staleness threshold for the clv_quality diagnostics: a close whose
# anchor rows were captured more than this many minutes before kickoff counts as
# STALE (mirrors clv_trueup.SNAPSHOT_CLOSE_MAX_GAP = 4h; kept as a local constant
# to preserve this module's no-Settings-import boundary). Diagnostics only —
# nothing here gates or reclassifies a close.
STALE_CLOSE_MAX_GAP_MINUTES = 240


def _percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (no numpy — this module stays stdlib-only for
    math). None on an empty series — an honest absence, never a fabricated 0."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def _clv_row_is_tautological(
    clv_log: Any,
    closing_fair_probability: Any,
    model_probability: Any,
) -> bool:
    """True when the close fair equals the pick-time fair (identical archived line).

    clv_log is then a tautology that re-encodes the pick-time edge — not independent
    close evidence — so its clv_log/beat_close are dropped from every CLV aggregate.
    Only excludes when BOTH probabilities are present (we can PROVE the tautology); a
    row with no CLV (clv_log is None) or an unknowable fair is not tautological here.

    STRATEGY COUPLING (P2-3): ``model_probability`` is the pick-time MARKET fair only
    under the deployed VALUE strategy, where the pipeline persists the devigged sharp
    fair into this column (``model_probability = ValueBet.sharp_fair_prob``;
    app/pipeline.py). Under the OFF-BY-DEFAULT model strategy it holds the goals-model
    probability instead (the market fair lives in ``fair_probability`` there), so this
    guard — comparing close MARKET fair against it — silently no-ops on model-strategy
    rows. That is a KNOWN, accepted limitation, not a bug: the model strategy is not
    deployed and contributes no trusted CLV. Closing the coupling for real (routing to
    fair_probability on model-strategy rows) is DEFERRED — it cannot key safely on
    ``anchor_type`` because a pre-column legacy VALUE pick also has anchor_type NULL,
    so the reroute would corrupt legacy value-row CLV. The invariant this guard relies
    on: on every row that feeds the trusted sharp subset, ``model_probability`` IS the
    pick-time market fair.
    """
    if clv_log is None:
        return False
    if closing_fair_probability is None or model_probability is None:
        return False
    try:
        delta = abs(float(closing_fair_probability) - float(model_probability))
    except (ValueError, TypeError):
        return False
    return delta <= CLV_TAUTOLOGY_EPS


def _clv_row_is_fabricated(
    clv_log: Any,
    decimal_odds: Any,
    closing_fair_probability: Any,
) -> bool:
    """True when a settled row's CLV is physically impossible (CLV-1 pollution).

    Primary test: close-implied edge = closing_fair_probability - 1/decimal_odds
    exceeds CLV_IMPLAUSIBLE_CLOSE_EDGE (the favorite-prob-on-underdog-leg signature).
    When BOTH real inputs (decimal_odds + closing_fair_probability) are present the
    row's CLV is computed from them, so this close-implied edge is the ONLY fabrication
    test — a legitimate plausible-close longshot (modest edge but |clv_log| > 0.5) is
    NOT fabricated. The |clv_log| magnitude cutoff is a FALLBACK, evaluated ONLY when
    the row LACKS an input (odds or fair prob absent / unusable), where the edge cannot
    be computed. A row with no CLV at all (clv_log is None) is not fabricated — it
    simply has no close to judge.
    """
    if clv_log is None:
        return False
    if decimal_odds is not None and closing_fair_probability is not None:
        try:
            implied = 1.0 / float(decimal_odds)
        except (ZeroDivisionError, ValueError, TypeError):
            implied = None
        if implied is not None:
            # Both real inputs present: judge by the close-implied edge ONLY; the
            # |clv_log| magnitude cutoff must NOT fire on a genuine odds+close row.
            close_edge = float(closing_fair_probability) - implied
            return close_edge > CLV_IMPLAUSIBLE_CLOSE_EDGE
    # Fallback (fair prob or odds absent / unusable): edge uncomputable, so the
    # |clv_log| magnitude is the only available tripwire.
    return abs(float(clv_log)) > CLV_IMPLAUSIBLE_LOG


# P2 SIGNIFICANCE alpha. The whole strategy's proof rests on mean clv_log being
# statistically > 0; a point estimate without a significance test reads small-sample
# noise as signal. 0.05 -> a two-sided 95% CI; ``*_clv_significant`` is True only when
# that CI excludes 0 on the positive side (ci_low > 0), ``*_beat_close_wilson_significant``
# only when the Wilson lower bound clears 0.5. Honest by construction: with the live
# trusted sharp n ~3 the flags read NOT significant.
CLV_SIGNIFICANCE_ALPHA = 0.05


def _significance_fields(
    prefix: str,
    clv_series: Sequence[float],
    beat_true: int,
    beat_known: int,
    alpha: float = CLV_SIGNIFICANCE_ALPHA,
) -> dict[str, Any]:
    """Expand a CLEAN clv_log series + beat-close tally into JSON-ready
    significance fields for one stratum (prefix "" = blended, "sharp_" = trusted).

    Pure passthrough to the math helpers in app.backtesting.clv: a one-sample
    t-test of mean CLV vs 0 (with a t-based 95% CI) and a Wilson 95% CI on the
    beat-close rate. Numerics are None when not computable (n<2 for the t-test,
    n==0 for Wilson); the boolean flags default to False — an honest "not
    significant", never a fabricated claim. ``clv_series``/``beat_*`` MUST already
    exclude fabricated + tautological rows (significance on the clean subset only).
    """
    sig = mean_significance(clv_series, alpha=alpha)
    wilson = wilson_interval(beat_true, beat_known, alpha=alpha) if beat_known else None
    return {
        f"{prefix}clv_n": len(clv_series),
        f"{prefix}clv_mean": sig.mean if sig is not None else None,
        f"{prefix}clv_std": sig.std if sig is not None else None,
        f"{prefix}clv_tstat": sig.tstat if sig is not None else None,
        f"{prefix}clv_ci_low": sig.ci_low if sig is not None else None,
        f"{prefix}clv_ci_high": sig.ci_high if sig is not None else None,
        f"{prefix}clv_significant": bool(sig is not None and sig.significant),
        f"{prefix}clv_alpha": alpha,
        f"{prefix}beat_close_wilson_low": wilson[0] if wilson is not None else None,
        f"{prefix}beat_close_wilson_high": wilson[1] if wilson is not None else None,
        f"{prefix}beat_close_wilson_significant": bool(wilson is not None and wilson[0] > 0.5),
    }


def _devig_fallback_asymmetric(mint_fell_back: bool | None, close_fell_back: bool | None) -> bool:
    """P2-2: True when the MINT and CLOSE fairs were devigged by DIFFERENT
    effective methods — exactly one of them fell back to multiplicative. Such a
    CLV reflects a devig-method change, not a real line move, so it is dropped
    from the trusted sharp-CLV subset.

    Conservative: a None on EITHER side (provenance not recorded — historical or
    model-strategy rows) is treated as SYMMETRIC (not excluded), so the trusted
    subset is unchanged until both flags are populated."""
    if mint_fell_back is None or close_fell_back is None:
        return False
    return mint_fell_back != close_fell_back


def _aggregate_settled(rows: Sequence[Any]) -> dict[str, Any]:
    """Aggregate (outcome, pnl, stake, clv_log, beat_close, closing_odds,
    closing_anchor_type) rows into the report fields. Decimals serialize as
    strings; undefined ratios are None.

    A TRUSTED sharp-close subset (``sharp_*``) is reported ALONGSIDE the blended
    headline: a close counts only when it is a GENUINE snapshot close
    (has_snapshot_close — clv-1: NOT a poll-time revalidation fallback, and
    independent of whether a soft book also quoted it), anchored by a named sharp
    book (closing_anchor_type in pinnacle/sharp — not a soft-book consensus
    median), AND independent of the fill (close_independent_of_fill is not False —
    the close was NOT anchored by the pick's own fill book; a self-priced close
    is CIRCULAR fake CLV, closing == fill, |clv_log|~0, and is what masked the
    -EV). Those are the closes whose CLV the platform can stand behind; the
    blended ``stake_weighted_clv_log`` still mixes every close in for continuity.

    EVIDENTIAL MARKERS (CLV audit P1 / H5 — labelling only, NO math change): the
    payload carries ``blended_clv_evidential`` (always False) on the blended
    headline and ``sharp_clv_evidential`` (always True) on the trusted subset, so a
    consumer can distinguish the INDICATIVE blended figure (mixes consensus +
    poll-time fallback closes) from the EVIDENTIAL sharp edge (independent sharp
    closes only) without inspecting which closes fed each aggregate. The blended
    fields are NOT removed — only flagged.

    Each row is (outcome, pnl, stake, clv_log, beat_close, closing_odds,
    closing_anchor, close_independent, has_snapshot_close, decimal_odds,
    closing_fair_probability, model_probability). ``closing_odds`` is now purely the
    optional SOFT display price (a sharp-only close has it NULL yet is a real close).
    ``close_independent`` / ``has_snapshot_close`` are None when feature-detected
    absent (pre-column rows). A None snapshot flag is "not a genuine snapshot close";
    independence is now required to be exactly True for the trusted subset (audit
    2026-06-28 P2: ``IS NOT FALSE`` let NULL/unknown independence leak in). ``decimal_odds``
    / ``closing_fair_probability`` feed the CLV-1 fabricated-CLV guard
    (_clv_row_is_fabricated); ``closing_fair_probability`` / ``model_probability`` feed
    the TAUTOLOGY guard (_clv_row_is_tautological): a row whose close fair equals its
    pick-time fair (identical archived line) has its clv_log/beat_close dropped from
    BOTH the blended headline and the trusted sharp subset.
    """
    counts = {"won": 0, "lost": 0, "void": 0, "push": 0, "half_won": 0, "half_lost": 0}
    total_staked = Decimal("0")
    total_pnl = Decimal("0")
    clv_weighted = Decimal("0")
    clv_stake = Decimal("0")
    beat_known = beat_true = 0
    sharp_clv_weighted = Decimal("0")
    sharp_clv_stake = Decimal("0")
    sharp_beat_known = sharp_beat_true = n_sharp = 0
    sharp_all_independent = True  # invariant: no circular close in the sharp subset
    # P2 SIGNIFICANCE: the per-pick CLEAN clv_log series for each stratum (the rows
    # that actually feed the blended / trusted point estimates — fabricated and
    # tautological rows already excluded). These drive the t-test + CI; collecting
    # the series (not just the stake-weighted sum) is what makes significance possible.
    blended_clv_series: list[float] = []
    sharp_clv_series: list[float] = []
    # D4 EVIDENCE-QUALITY tallies (diagnostics only — no gate/estimate changes):
    # the per-row guard verdicts were previously computed and DISCARDED; the
    # counts below make the exclusion mass visible under "clv_quality".
    q_missing = q_tautological = q_fabricated = q_circular = 0
    q_snapshot_close = q_fallback_close = 0
    close_ages_minutes: list[float] = []
    q_stale_close = 0
    # A4: per-reason counts of the PERSISTED close_exclusion_reason (closed
    # vocabulary stamped by the close writers; 'trusted' included). Counts what
    # is stored — NULL (pre-column / no close yet) rows are simply not counted.
    reason_counts: dict[str, int] = {}
    for row in rows:
        (
            outcome,
            pnl,
            stake,
            clv_log,
            beat_close,
            _closing_odds,  # optional SOFT display price — no longer the trusted-close gate
            closing_anchor,
            close_independent,
            has_snapshot_close,
            decimal_odds,
            closing_fair_probability,
            model_probability,
        ) = row[:12]
        # P2-2 devig-fallback provenance is FEATURE-DETECTED (trailing, optional)
        # exactly like the close-anchor/independence columns: rows built before
        # these columns existed are 12-tuples, so a missing flag reads None and
        # _devig_fallback_asymmetric treats it as symmetric (not excluded).
        mint_devig_fell_back = row[12] if len(row) > 12 else None
        close_devig_fell_back = row[13] if len(row) > 13 else None
        # D3/D4 close provenance (trailing, feature-detected like the flags
        # above): the close anchor rows' capture time + the event kickoff, so
        # close AGE at kickoff is measurable once provenance accrues.
        close_snapshot_captured_at = row[14] if len(row) > 14 else None
        kickoff_at = row[15] if len(row) > 15 else None
        # A4 close-exclusion reason (trailing, feature-detected like the rest).
        close_reason = row[16] if len(row) > 16 else None
        if outcome in counts:
            counts[outcome] += 1
        total_staked += stake
        total_pnl += pnl if pnl is not None else Decimal("0")
        # CLV-1 guard: a row whose close-implied edge is physically impossible
        # (or whose |clv_log| is implausibly large) is fabricated CLV from the
        # since-fixed double-chance orientation bug. Its outcome/pnl are honest
        # (counted above) but its clv_log/beat_close are dropped from BOTH the
        # blended headline and the trusted sharp subset so they cannot inflate
        # stake_weighted_clv_log / beat_close_rate.
        clv_fabricated = _clv_row_is_fabricated(clv_log, decimal_odds, closing_fair_probability)
        # TAUTOLOGY guard (audit 2026-06-28 P2): a row whose close fair equals its
        # pick-time fair (identical archived line) carries a clv_log that merely
        # re-encodes the pick-time edge — drop it from BOTH the blended headline and
        # the trusted sharp subset, exactly like a fabricated row.
        clv_tautological = _clv_row_is_tautological(
            clv_log, closing_fair_probability, model_probability
        )
        clv_excluded = clv_fabricated or clv_tautological
        # D4 tallies. Each guard is counted on its OWN verdict (a row can trip
        # both); circular is "close priced by the fill book and not otherwise
        # excluded" — the design's exclusive residual bucket.
        if clv_log is None:
            q_missing += 1
        if clv_tautological:
            q_tautological += 1
        if clv_fabricated:
            q_fabricated += 1
        if clv_log is not None and close_independent is False and not clv_excluded:
            q_circular += 1
        if bool(has_snapshot_close):
            q_snapshot_close += 1
        elif clv_log is not None:
            # A CLV without a snapshot close = the poll-time fallback close stood.
            q_fallback_close += 1
        if close_snapshot_captured_at is not None and kickoff_at is not None:
            age_minutes = (kickoff_at - close_snapshot_captured_at).total_seconds() / 60.0
            close_ages_minutes.append(age_minutes)
            if age_minutes > STALE_CLOSE_MAX_GAP_MINUTES:
                q_stale_close += 1
        if close_reason is not None:
            key = str(close_reason)
            reason_counts[key] = reason_counts.get(key, 0) + 1
        if clv_log is not None and not clv_excluded:
            clv_weighted += stake * clv_log
            clv_stake += stake
            blended_clv_series.append(float(clv_log))
        if beat_close is not None and not clv_excluded:
            beat_known += 1
            beat_true += int(beat_close)
        if (
            # CLV-1/tautology: never admit a fabricated (impossible close-implied
            # edge) or tautological (identical-line) CLV to the trusted subset.
            not clv_excluded
            # clv-1: a GENUINE snapshot close is marked by has_snapshot_close, NOT by
            # closing_odds. A close anchored only by sharp books (no soft book quoted
            # it) has closing_odds=None yet is a real snapshot close; gating on
            # closing_odds false-negatived it. closing_odds is now purely the optional
            # SOFT display price.
            and bool(has_snapshot_close)
            and closing_anchor in _SHARP_CLOSE_ANCHORS
            and clv_log is not None
            # INDEPENDENCE guard (P0-1/P0-3 + audit 2026-06-28 P2): a close anchored by
            # the pick's OWN fill book, OR an identical-archived-line close, is CIRCULAR
            # (closing == fill / closing_fair == pick_fair, |clv_log| fake). Require
            # independence to be EXACTLY True — None (pre-column / unknown) no longer
            # leaks in as "not circular": unproven independence is not trusted.
            and close_independent is True
            # P2-2: drop ASYMMETRIC devig fallbacks — when the mint and close
            # fairs were devigged by different effective methods (one fell back to
            # multiplicative, the other did not), the CLV is a devig-method
            # artifact, not a genuine line move. NULL flags are symmetric (kept).
            and not _devig_fallback_asymmetric(mint_devig_fell_back, close_devig_fell_back)
        ):
            # Genuine, INDEPENDENT sharp snapshot close with a measured CLV — the
            # trusted subset.
            n_sharp += 1
            sharp_clv_weighted += stake * clv_log
            sharp_clv_stake += stake
            sharp_clv_series.append(float(clv_log))
            sharp_all_independent = sharp_all_independent and close_independent is True
            if beat_close is not None:
                sharp_beat_known += 1
                sharp_beat_true += int(beat_close)
    # Defense-in-depth: the gate above already excludes circular closes, so by
    # construction every row in the sharp subset is independent of its fill book
    # (closing_anchor != fill_book). Enforce it with an explicit raise (NOT assert,
    # which `python -O` strips) so a future refactor of the gate that re-admits a
    # self-priced close trips here instead of silently faking CLV — even in an
    # optimized production run.
    if not sharp_all_independent:
        raise RuntimeError("sharp-close subset contains a circular (self-priced) close")
    # P2-1 HEADLINE min-n suppression: below MIN_HEADLINE_N settled picks the
    # blended roi / beat_close_rate / stake-weighted CLV are noise (a 10-pick
    # -8.7% reads as signal), so they are NULLED at the source and flagged
    # roi_status="insufficient" — no /performance consumer can read a headline
    # point estimate off a sub-floor sample. n / counts / totals survive so the
    # dashboard can render the "n too small" state. The trusted sharp subset is
    # gated independently on its OWN n (n_sharp_close).
    n_settled = len(rows)
    headline_ok = n_settled >= MIN_HEADLINE_N
    sharp_ok = n_sharp >= MIN_HEADLINE_N
    return {
        "n_settled": n_settled,
        **counts,
        "total_staked": str(total_staked),
        "total_pnl": str(total_pnl),
        "roi": _ratio(total_pnl, total_staked) if headline_ok else None,
        "roi_status": "ok" if headline_ok else "insufficient",
        "stake_weighted_clv_log": _ratio(clv_weighted, clv_stake) if headline_ok else None,
        "beat_close_rate": (
            _ratio(Decimal(beat_true), Decimal(beat_known)) if headline_ok else None
        ),
        # CLV audit P1 / H5 — EVIDENTIAL MARKER (labelling only, NO math change). The
        # blended stake_weighted_clv_log / beat_close_rate mix EVERY non-excluded close,
        # including consensus-anchored and poll-time re-scrape FALLBACK closes, which are
        # NOT independent sharp evidence. They are retained for continuity but are
        # INDICATIVE ONLY: a consumer (dashboard / "does it work?" reader) must NOT read
        # them as the strategy's proven edge. This structural flag (always False) makes
        # that machine-readable without inspecting which closes were mixed in. The honest
        # proof of edge is the trusted ``sharp_*`` subset below (``sharp_clv_evidential``).
        "blended_clv_evidential": False,
        "min_headline_n": MIN_HEADLINE_N,
        # TRUSTED subset — genuine sharp snapshot closes only (see docstring) —
        # gated on its own n (n_sharp_close), naturally thinner than n_settled.
        # ``sharp_clv_evidential`` (always True) is the counterpart marker: THIS is the
        # evidential edge metric — independent sharp closes only, the figure the platform
        # can stand behind — as opposed to the indicative blended headline above.
        "sharp_clv_evidential": True,
        "n_sharp_close": n_sharp,
        "sharp_status": "ok" if sharp_ok else "insufficient",
        "sharp_stake_weighted_clv_log": (
            _ratio(sharp_clv_weighted, sharp_clv_stake) if sharp_ok else None
        ),
        "sharp_beat_close_rate": (
            _ratio(Decimal(sharp_beat_true), Decimal(sharp_beat_known)) if sharp_ok else None
        ),
        # P2 SIGNIFICANCE (NOT min-n suppressed: this IS the honesty gate). One-sample
        # t-test of mean clv_log vs 0 + t-CI, and a Wilson CI on beat_close_rate, for
        # BOTH strata on the CLEAN subset. At tiny live n the flags read False — honest.
        **_significance_fields("", blended_clv_series, beat_true, beat_known),
        **_significance_fields("sharp_", sharp_clv_series, sharp_beat_true, sharp_beat_known),
        # D4 EVIDENCE-QUALITY diagnostics (labelling/observability only — every
        # figure above is computed exactly as before). How much of the settled
        # sample the CLV guards excluded and what kind of close each row got;
        # close-age fields stay None/0 until D3 provenance accrues.
        "clv_quality": {
            "n_settled": n_settled,
            "clv_missing": q_missing,
            "clv_excluded_tautological": q_tautological,
            "clv_excluded_fabricated": q_fabricated,
            "clv_excluded_circular": q_circular,
            # tautological share of the rows that HAVE a CLV — the headline
            # exclusion rate the dashboard shows. None when no row has CLV.
            "tautological_rate": (
                q_tautological / (n_settled - q_missing) if n_settled > q_missing else None
            ),
            "n_snapshot_close": q_snapshot_close,
            "n_fallback_close": q_fallback_close,
            "n_close_age_known": len(close_ages_minutes),
            "close_age_p50_minutes": _percentile(close_ages_minutes, 0.5),
            "close_age_p90_minutes": _percentile(close_ages_minutes, 0.9),
            "n_stale_close": q_stale_close,
            "stale_close_max_gap_minutes": STALE_CLOSE_MAX_GAP_MINUTES,
            # A4: counts per PERSISTED close-exclusion reason (closed vocabulary
            # incl. 'trusted'; app/edge/value.py CLOSE_EXCLUSION_REASONS). Rows
            # with no stamped reason (pre-column / no close yet) are the gap
            # between n_close_reason_known and n_settled.
            "n_close_reason_known": sum(reason_counts.values()),
            "close_exclusion_reasons": dict(sorted(reason_counts.items())),
        },
    }


def _aggregate_settled_by_sport(
    rows: Sequence[tuple[str, Sequence[Any]]],
) -> dict[str, dict[str, Any]]:
    """Per-sport split of the settled-pick headline (Batch 3 PER-SPORT EVIDENCE).

    ``rows`` are (sport_key, settled_row) pairs where ``settled_row`` is the
    9-tuple ``_aggregate_settled`` consumes. Each sport is aggregated on its OWN
    sample, so MIN_HEADLINE_N suppression applies per sport — a thin or
    experimental sport (e.g. basketball, currently shadow-only) can never borrow
    another sport's sufficiency. TIER-AGNOSTIC by design: it spans both premium
    and the volume/shadow tier, because accumulating an experimental sport's
    forward evidence (which is entirely shadow) IS the point of this split.
    """
    by_sport: dict[str, list[Sequence[Any]]] = {}
    for sport_key, settled_row in rows:
        by_sport.setdefault(sport_key, []).append(settled_row)
    return {k: _aggregate_settled(v) for k, v in sorted(by_sport.items())}


def _close_coverage_by_sport_market(
    rows: Sequence[tuple[tuple[str, str], Sequence[Any]]],
    *,
    sla_threshold: float,
) -> list[dict[str, Any]]:
    """CLOSE/FRESHNESS SLA (external-audit item #8) — per (sport, market) close
    coverage + SLA verdict. REPORT-ONLY.

    ``rows`` are ((sport_key, market), settled_row) pairs where ``settled_row`` is
    the same tuple ``_aggregate_settled`` consumes. Close coverage reuses the
    EXACT trust logic already in ``_aggregate_settled`` (no reinvention): the
    numerator is ``n_sharp_close`` — settled picks whose close is a GENUINE
    independent sharp snapshot (has_snapshot_close + a named sharp anchor +
    independent of the fill + not tautological/fabricated), i.e. the closes whose
    CLV the platform can stand behind — over ``n_settled`` (all settled picks in
    that sport-market).

    When coverage is below ``sla_threshold`` the sport-market's CLV/ROI number is
    built on too-thin closing-line coverage to be trustworthy, so the CLAIM is
    flagged ``below_sla`` with a human-readable verdict. Nothing is hidden and NO
    selection/stake/threshold behaviour changes — this only annotates what the
    REPORT asserts. ``close_coverage`` is None (verdict "no settled picks") when a
    sport-market has no settled picks yet — there is no claim to flag.
    """
    grouped: dict[tuple[str, str], list[Sequence[Any]]] = {}
    for key, settled_row in rows:
        grouped.setdefault(key, []).append(settled_row)
    out: list[dict[str, Any]] = []
    for (sport_key, market), settled_rows in sorted(grouped.items()):
        agg = _aggregate_settled(settled_rows)
        n_settled = int(agg["n_settled"])
        n_trusted_close = int(agg["n_sharp_close"])
        coverage = (n_trusted_close / n_settled) if n_settled else None
        below_sla = coverage is not None and coverage < sla_threshold
        out.append(
            {
                "sport": sport_key,
                "market": market,
                "n_settled": n_settled,
                "n_trusted_close": n_trusted_close,
                "close_coverage": coverage,
                "sla_threshold": sla_threshold,
                "below_sla": below_sla,
                # convenience verdict for the dashboard/report consumer — the
                # audit's exact wording when the CLV number is not trustworthy.
                "verdict": (
                    "coverage below SLA — CLV unreliable"
                    if below_sla
                    else ("no settled picks" if coverage is None else "ok")
                ),
            }
        )
    return out


#: Mint-week trend window for the B4 steam shadow-verdict summary — long enough
#: to see a drift, short enough that one SELECT stays cheap.
STEAM_WEEKLY_WINDOW_WEEKS = 8

#: The steam_shadow settled split carries ONLY the trusted-sharp evidence
#: fields of _aggregate_settled — the same trust guards + MIN_HEADLINE_N
#: suppression as every other aggregate (below the floor the payload carries
#: n + "insufficient", never a point estimate).
_STEAM_SHARP_EVIDENCE_FIELDS = (
    "n_settled",
    "n_sharp_close",
    "sharp_status",
    "sharp_stake_weighted_clv_log",
    "min_headline_n",
)


def _weekly_steam_counts(
    rows: Sequence[tuple[bool | None, datetime | None]],
) -> list[dict[str, Any]]:
    """B4: per-mint-week steam shadow-verdict counts (pure — no DB).

    ``rows`` = (steam_tripped, created_at) per pick. Weeks are Monday-anchored
    (ISO) and reported ascending; the three counts per week sum to that week's
    pick rows — no silent loss (NULL verdicts count as unevaluated)."""
    weeks: dict[str, dict[str, int]] = {}
    for tripped, created_at in rows:
        if created_at is None:
            continue
        day = created_at.date()
        week_start = (day - timedelta(days=day.weekday())).isoformat()
        cell = weeks.setdefault(week_start, {"would_demote": 0, "clear": 0, "unevaluated": 0})
        if tripped is True:
            cell["would_demote"] += 1
        elif tripped is False:
            cell["clear"] += 1
        else:
            cell["unevaluated"] += 1
    return [{"week_start": ws, **counts} for ws, counts in sorted(weeks.items())]


async def performance_report(
    session: AsyncSession,
    *,
    close_coverage_sla: float = DEFAULT_CLOSE_COVERAGE_SLA,
) -> dict[str, Any]:
    """ROI + stake-weighted log-CLV over settled picks (phase 4 report).

    Headline numbers are PREMIUM-scoped ("tier_scope" says so): the volume
    tier is an informational shadow — letting its many small edges into the
    headline would mask the alerted strategy's real performance. The same
    aggregates over the volume tier ride along under "volume" (accumulating
    that tier's CLV/ROI evidence IS its purpose).

    Staking/weighting uses the platform's recommended stake — the same
    sizing the backtests report — while pnl/roi per pick already reflect
    the user's actual stake when they logged one.
    """
    # closing_anchor_type is FEATURE-DETECTED (same migration contract as
    # live_evidence_rows): until the ORM attr lands, the close anchor is None and
    # the sharp-close subset is simply empty (n_sharp_close == 0).
    close_anchor_attr = getattr(Pick, "closing_anchor_type", None)
    indep_attr = getattr(Pick, "close_independent_of_fill", None)
    snapshot_attr = getattr(Pick, "has_snapshot_close", None)
    mint_fb_attr = getattr(Pick, "mint_devig_fell_back", None)  # P2-2 provenance
    close_fb_attr = getattr(Pick, "close_devig_fell_back", None)
    select_cols: list[Any] = [
        ResultTracking.outcome,  # 0
        ResultTracking.pnl,  # 1
        Pick.recommended_stake_amount,  # 2
        Pick.clv_log,  # 3
        Pick.beat_close,  # 4
        Pick.tier,  # 5 — split key, not passed to _aggregate_settled
        Pick.closing_odds,  # 6 — optional SOFT display price (no longer the gate)
        Sport.key,  # 7 — per-sport split key, not passed to _aggregate_settled
        Pick.decimal_odds,  # 8 — CLV-1 close-implied-edge guard (fill price)
        Pick.closing_fair_probability,  # 9 — CLV-1 close-implied-edge guard (close fair)
        Pick.model_probability,  # 10 — TAUTOLOGY guard (pick-time fair; P2-3: this
        # IS the market fair only under the deployed value strategy — see
        # _clv_row_is_tautological for the documented strategy coupling)
    ]
    sport_idx = 7
    # Per (sport, market) split key for the CLOSE/FRESHNESS SLA panel (audit #8).
    # Appended like the other split keys — not passed to _aggregate_settled.
    market_idx = len(select_cols)
    select_cols.append(Pick.market)
    close_anchor_idx = indep_idx = snapshot_idx = None
    if close_anchor_attr is not None:
        close_anchor_idx = len(select_cols)
        select_cols.append(close_anchor_attr)  # 7
    if indep_attr is not None:
        indep_idx = len(select_cols)
        select_cols.append(indep_attr)  # 8 — INDEPENDENCE provenance (P0-1/P0-3)
    if snapshot_attr is not None:
        snapshot_idx = len(select_cols)
        select_cols.append(snapshot_attr)  # 9 — clv-1 genuine-snapshot-close marker
    mint_fb_idx = close_fb_idx = None
    if mint_fb_attr is not None:
        mint_fb_idx = len(select_cols)
        select_cols.append(mint_fb_attr)  # P2-2 mint devig-fallback provenance
    if close_fb_attr is not None:
        close_fb_idx = len(select_cols)
        select_cols.append(close_fb_attr)  # P2-2 close devig-fallback provenance
    # D3/D4 close provenance (feature-detected, same migration contract): the
    # close anchor rows' capture time + the event kickoff feed the clv_quality
    # close-age diagnostics (echo vs fresh-but-unmoved becomes measurable).
    close_cap_attr = getattr(Pick, "close_snapshot_captured_at", None)
    close_cap_idx = None
    if close_cap_attr is not None:
        close_cap_idx = len(select_cols)
        select_cols.append(close_cap_attr)
    starts_at_idx = len(select_cols)
    select_cols.append(Event.starts_at)  # kickoff — the close-age clock
    # A4 close-exclusion reason (feature-detected, same migration contract):
    # the persisted per-row reason feeds the clv_quality per-reason counts.
    reason_attr = getattr(Pick, "close_exclusion_reason", None)
    reason_idx = None
    if reason_attr is not None:
        reason_idx = len(select_cols)
        select_cols.append(reason_attr)
    # A5/B4 steam shadow verdict (feature-detected, same migration contract):
    # the persisted mint-time verdict splits the SETTLED sample for the
    # steam_shadow trusted-CLV split below.
    steam_attr = getattr(Pick, "steam_tripped", None)
    steam_idx = None
    if steam_attr is not None:
        steam_idx = len(select_cols)
        select_cols.append(steam_attr)
    rows = (
        await session.execute(
            select(*select_cols)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .join(Event, Pick.event_id == Event.id)
            .join(Sport, Event.sport_id == Sport.id)
        )
    ).all()
    pending_by_tier: dict[str, int] = {
        tier: int(n)
        for tier, n in (
            await session.execute(
                select(Pick.tier, func.count()).where(Pick.status == "alerted").group_by(Pick.tier)
            )
        ).all()
    }

    def _settled_tuple(r: Any) -> tuple[Any, ...]:
        # (outcome, pnl, stake, clv_log, beat_close, closing_odds, closing_anchor,
        #  close_independent, has_snapshot_close, decimal_odds,
        #  closing_fair_probability) — close_independent /
        #  has_snapshot_close are None when feature-detected absent (pre-column):
        #  the sharp gate treats unknown independence as "NOT circular" and a None
        #  snapshot flag as "not a genuine snapshot close" (excluded).
        closing_anchor = r[close_anchor_idx] if close_anchor_idx is not None else None
        close_independent = r[indep_idx] if indep_idx is not None else None
        has_snapshot_close = r[snapshot_idx] if snapshot_idx is not None else None
        mint_fell_back = r[mint_fb_idx] if mint_fb_idx is not None else None
        close_fell_back = r[close_fb_idx] if close_fb_idx is not None else None
        close_captured_at = r[close_cap_idx] if close_cap_idx is not None else None
        close_reason = r[reason_idx] if reason_idx is not None else None
        return (
            r[0],
            r[1],
            r[2],
            r[3],
            r[4],
            r[6],
            closing_anchor,
            close_independent,
            has_snapshot_close,
            r[8],  # decimal_odds — CLV-1 close-implied-edge guard
            r[9],  # closing_fair_probability — CLV-1 close-implied-edge guard
            r[10],  # model_probability — TAUTOLOGY guard (pick-time fair)
            mint_fell_back,  # P2-2 mint devig-fallback provenance (or None)
            close_fell_back,  # P2-2 close devig-fallback provenance (or None)
            close_captured_at,  # D3 close provenance — close-age diagnostics
            r[starts_at_idx],  # kickoff — the close-age clock (D4)
            close_reason,  # A4 close-exclusion reason (or None pre-column)
        )

    def _tier_rows(tier_name: str) -> list[tuple[Any, ...]]:
        return [_settled_tuple(r) for r in rows if r[5] == tier_name]

    premium = _aggregate_settled(_tier_rows("premium"))
    volume = _aggregate_settled(_tier_rows("volume"))
    volume["n_pending"] = pending_by_tier.get("volume", 0)
    # PER-SPORT split (Batch 3): TIER-AGNOSTIC — spans premium + the volume/shadow
    # tier so an experimental sport (basketball, currently all-shadow) accrues its
    # own forward CLV/ROI evidence. Each sport is gated on its OWN n inside
    # _aggregate_settled (MIN_HEADLINE_N), so a thin sport reads "insufficient".
    by_sport = _aggregate_settled_by_sport([(r[sport_idx], _settled_tuple(r)) for r in rows])
    # CLOSE/FRESHNESS SLA (audit #8): per (sport, market), the share of settled
    # picks that carry a TRUSTED independent sharp close — below the SLA the
    # sport-market's CLV/ROI claim is flagged UNRELIABLE (report annotation only;
    # no pick is hidden and NO selection/stake/threshold changes).
    close_coverage = _close_coverage_by_sport_market(
        [((r[sport_idx], r[market_idx]), _settled_tuple(r)) for r in rows],
        sla_threshold=close_coverage_sla,
    )
    # D4 EVIDENCE QUALITY: tier-AGNOSTIC (the exclusion mass spans both tiers,
    # matching the audit SQL's population) + per-stratum tautology tallies. Pure
    # diagnostics — no headline/trusted figure above changes.
    clv_quality = _aggregate_settled([_settled_tuple(r) for r in rows])["clv_quality"]
    clv_quality["scope"] = "all_tiers"
    clv_quality["strata"] = await clv_quality_strata(session)
    # A5 STEAM SHADOW counts (observability only — feature-detected so a
    # pre-migration DB serves the report unchanged). Splits ALL picks by the
    # persisted mint-time shadow verdict: would_demote (tripped) / clear
    # (evaluated, no trip) / unevaluated (NULL: gate unconfigured, consensus
    # anchor, eval error, or pre-column row). The three counts sum to every
    # pick row — no silent loss.
    steam_shadow: dict[str, Any] | None = None
    if steam_attr is not None and steam_idx is not None:
        s_idx: int = steam_idx  # narrowed binding for the closure below
        steam_counts = {
            tripped: int(n)
            for tripped, n in (
                await session.execute(select(steam_attr, func.count()).group_by(steam_attr))
            ).all()
        }
        # B4: mint-week verdict trend (would-demote count over time) + the
        # trusted-CLV split of SETTLED picks by verdict. The split runs through
        # _aggregate_settled so the sharp fields inherit the exact same trust
        # guards and MIN_HEADLINE_N suppression as every other aggregate —
        # below the floor the payload carries n + "insufficient", never a
        # point estimate. Observability only: no verdict demotes anything.
        weekly_rows = (
            await session.execute(
                select(steam_attr, Pick.created_at).where(
                    Pick.created_at
                    >= datetime.now(tz=UTC) - timedelta(weeks=STEAM_WEEKLY_WINDOW_WEEKS)
                )
            )
        ).all()

        def _steam_sharp_evidence(verdict: bool) -> dict[str, Any]:
            agg = _aggregate_settled([_settled_tuple(r) for r in rows if r[s_idx] is verdict])
            return {k: agg[k] for k in _STEAM_SHARP_EVIDENCE_FIELDS}

        steam_shadow = {
            "would_demote": steam_counts.get(True, 0),
            "clear": steam_counts.get(False, 0),
            "unevaluated": steam_counts.get(None, 0),
            "weekly": _weekly_steam_counts(
                [(tripped, created_at) for tripped, created_at in weekly_rows]
            ),
            "settled_by_verdict": {
                "would_demote": _steam_sharp_evidence(True),
                "clear": _steam_sharp_evidence(False),
            },
        }
    return {
        **premium,
        "n_pending": pending_by_tier.get("premium", 0),
        "tier_scope": "premium",
        "volume": volume,
        "by_sport": by_sport,
        "close_coverage_sla": close_coverage,
        "clv_quality": clv_quality,
        "steam_shadow": steam_shadow,
    }


def _ratio(numerator: Decimal, denominator: Decimal) -> str | None:
    """Exact ratio without Decimal trailing-zero noise; None when undefined."""
    if not denominator:
        return None
    return format((numerator / denominator).normalize(), "f")


async def clv_quality_strata(
    session: AsyncSession,
    *,
    days: int = 21,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """D4: per-stratum tautological-close tallies over recently settled picks —
    the Q2 audit SQL shape (sport x market x mint anchor x close anchor x close
    book), READ-ONLY. Same-source cells (consensus->consensus, pinnacle->
    pinnacle) carry ~all tautologies; cross-source cells are ~0% — this split
    is what makes the headline exclusion rate diagnosable instead of a single
    opaque number. Rows are ordered most-tautological first and capped."""
    since = datetime.now(tz=UTC) - timedelta(days=days)
    taut_cond = and_(
        Pick.closing_fair_probability.is_not(None),
        Pick.model_probability.is_not(None),
        func.abs(Pick.closing_fair_probability - Pick.model_probability) <= CLV_TAUTOLOGY_EPS,
    )
    n_taut = func.count().filter(taut_cond)
    rows = (
        await session.execute(
            select(
                Sport.key,
                Pick.market,
                Pick.anchor_type,
                Pick.closing_anchor_type,
                Pick.close_anchor_book,
                func.count().label("n"),
                n_taut.label("n_tautological"),
            )
            .select_from(ResultTracking)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .join(Event, Pick.event_id == Event.id)
            .join(Sport, Event.sport_id == Sport.id)
            .where(ResultTracking.settled_at >= since, Pick.clv_log.is_not(None))
            .group_by(
                Sport.key,
                Pick.market,
                Pick.anchor_type,
                Pick.closing_anchor_type,
                Pick.close_anchor_book,
            )
            .order_by(n_taut.desc(), func.count().desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "sport": sport,
            "market": market,
            "anchor_type": anchor_type,
            "closing_anchor_type": closing_anchor_type,
            "close_anchor_book": close_anchor_book,
            "n": int(n),
            "n_tautological": int(taut),
            "tautological_rate": (int(taut) / int(n)) if n else None,
        }
        for sport, market, anchor_type, closing_anchor_type, close_anchor_book, n, taut in rows
    ]


async def sharp_close_capture_density(
    session: AsyncSession,
    *,
    days: int = 7,
    final_window: timedelta = timedelta(hours=1),
) -> dict[str, Any]:
    """D4 capture-density panel for /resolution/match-rate: how many FINAL-HOUR
    sharp rows per source landed on recently kicked-off events (the Q5/Q6 audit
    shape) — the instrument that says whether the D1 close-boost band is
    actually producing a fresh sharp close to anchor on. READ-ONLY.

    Betfair rows live INLINE on the canonical event (bookmaker 'Betfair%');
    Pinnacle archive rows live on their own ``pinnacle_<sport>`` namespace
    events — each source is counted on its own event population (no
    cross-source matching here; this measures capture, not matching)."""
    now = datetime.now(tz=UTC)
    since = now - timedelta(days=days)

    async def _density(*conds: Any) -> dict[str, int]:
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.count(func.distinct(OddsSnapshot.event_id)),
                )
                .select_from(OddsSnapshot)
                .join(Event, OddsSnapshot.event_id == Event.id)
                .join(Sport, Event.sport_id == Sport.id)
                .where(
                    Event.starts_at.is_not(None),
                    Event.starts_at >= since,
                    Event.starts_at <= now,
                    OddsSnapshot.captured_at <= Event.starts_at,
                    OddsSnapshot.captured_at >= Event.starts_at - final_window,
                    *conds,
                )
            )
        ).one()
        return {"final_window_rows": int(row[0]), "events_with_rows": int(row[1])}

    events_kicked_off = (
        await session.scalar(
            select(func.count(func.distinct(Event.id)))
            .select_from(Event)
            .join(Sport, Event.sport_id == Sport.id)
            .join(OddsSnapshot, OddsSnapshot.event_id == Event.id)
            .where(
                Sport.key.not_like("pinnacle%"),
                Event.starts_at.is_not(None),
                Event.starts_at >= since,
                Event.starts_at <= now,
            )
        )
    ) or 0
    return {
        "window_days": days,
        "final_window_minutes": int(final_window.total_seconds() // 60),
        # canonical (non-archive) events with any odds history that kicked off
        # in the window — the shared denominator for per-source coverage.
        "events_kicked_off": int(events_kicked_off),
        "sources": {
            "betfair": await _density(
                OddsSnapshot.bookmaker.ilike("betfair%"),
                Sport.key.not_like("pinnacle%"),
            ),
            "pinnacle": await _density(Sport.key.like("pinnacle%")),
        },
    }


async def live_evidence_rows(session: AsyncSession) -> list["SettledPickRow"]:
    """Settled picks reduced to plain-float rows for the pure stratified
    live-evidence report (app/backtesting/live_evidence.py) — the DB read
    half of the GET /performance "live_evidence" section.

    anchor_type is FEATURE-DETECTED: a separate migration is adding the
    column; until the ORM model carries it, every row gets None and the
    report omits the anchor grouping. Detection is on the ORM attribute —
    the agreed contract with the migration work — never a DB introspection.
    """
    from app.backtesting.live_evidence import SettledPickRow

    anchor_attr = getattr(Pick, "anchor_type", None)
    close_anchor_attr = getattr(Pick, "closing_anchor_type", None)
    indep_attr = getattr(Pick, "close_independent_of_fill", None)
    mint_fb_attr = getattr(Pick, "mint_devig_fell_back", None)  # P2-2 provenance
    close_fb_attr = getattr(Pick, "close_devig_fell_back", None)
    columns = [
        Pick.tier,  # 0
        Pick.value_filter_score,  # 1
        Pick.clv_log,  # 2
        Pick.beat_close,  # 3
        Pick.recommended_stake_amount,  # 4
        ResultTracking.pnl,  # 5
        Pick.closing_odds,  # 6 — snapshot-close marker (NON-NULL = a true close)
        Sport.key,  # 7 — per-sport split key (Batch 3)
        Pick.closing_fair_probability,  # 8 — close fair, for the TAUTOLOGY guard (#137)
        Pick.model_probability,  # 9 — pick-time fair, for the TAUTOLOGY guard (#137)
    ]
    sport_idx = 7
    # closing_anchor_type / close_independent_of_fill are FEATURE-DETECTED like
    # anchor_type (same migration contract): until the ORM attr lands, every
    # row's value is None and the close-anchor grouping / sharp-close subset are
    # simply empty (or, for independence, "unknown" — never treated as circular).
    anchor_idx = close_anchor_idx = indep_idx = None
    if anchor_attr is not None:
        anchor_idx = len(columns)
        columns.append(anchor_attr)
    if close_anchor_attr is not None:
        close_anchor_idx = len(columns)
        columns.append(close_anchor_attr)
    if indep_attr is not None:
        indep_idx = len(columns)
        columns.append(indep_attr)
    mint_fb_idx = close_fb_idx = None
    if mint_fb_attr is not None:
        mint_fb_idx = len(columns)
        columns.append(mint_fb_attr)  # P2-2 mint devig-fallback provenance
    if close_fb_attr is not None:
        close_fb_idx = len(columns)
        columns.append(close_fb_attr)  # P2-2 close devig-fallback provenance
    # has_snapshot_close: prefer the DEDICATED column (mirrors the headline path at
    # 1137/1199); fall back to closing_odds-non-null only pre-migration (dead today).
    snap_close_attr = getattr(Pick, "has_snapshot_close", None)
    snap_close_idx = None
    if snap_close_attr is not None:
        snap_close_idx = len(columns)
        columns.append(snap_close_attr)
    # decimal_odds: the fabricated-CLV guard input (mirror of _clv_row_is_fabricated).
    decimal_odds_idx = len(columns)
    columns.append(Pick.decimal_odds)
    rows = (
        await session.execute(
            select(*columns)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .join(Event, Pick.event_id == Event.id)
            .join(Sport, Event.sport_id == Sport.id)
        )
    ).all()
    return [
        SettledPickRow(
            tier=row[0],
            value_filter_score=float(row[1]) if row[1] is not None else None,
            clv_log=float(row[2]) if row[2] is not None else None,
            beat_close=row[3],
            stake=float(row[4]),
            pnl=float(row[5]) if row[5] is not None else None,
            sport=row[sport_idx],
            anchor_type=row[anchor_idx] if anchor_idx is not None else None,
            closing_anchor_type=row[close_anchor_idx] if close_anchor_idx is not None else None,
            # closing_odds NON-NULL marks a genuine snapshot close (not a
            # poll-time revalidation fallback) — the SOURCE half of "trusted".
            has_snapshot_close=(
                bool(row[snap_close_idx]) if snap_close_idx is not None else (row[6] is not None)
            ),
            decimal_odds=float(row[decimal_odds_idx])
            if row[decimal_odds_idx] is not None
            else None,
            # INDEPENDENCE half (P0-1/P0-3): False = circular self-priced close
            # (excluded from sharp subset); None = unknown (pre-column, NOT
            # treated as circular).
            close_independent_of_fill=row[indep_idx] if indep_idx is not None else None,
            # TAUTOLOGY guard inputs (#137): close fair vs pick-time fair. A close
            # that merely ECHOES the pick-time sharp anchor (closing == model) re-encodes
            # the pick-time edge — fake CLV the fill-book-only independence flag missed.
            closing_fair_probability=float(row[8]) if row[8] is not None else None,
            model_probability=float(row[9]) if row[9] is not None else None,
            # P2-2 devig-fallback provenance (feature-detected; None = symmetric).
            mint_devig_fell_back=row[mint_fb_idx] if mint_fb_idx is not None else None,
            close_devig_fell_back=row[close_fb_idx] if close_fb_idx is not None else None,
        )
        for row in rows
    ]


async def bet_band_observations(session: AsyncSession) -> list["BetBandObservation"]:
    """Settled, BINARY-outcome PREMIUM picks reduced to plain-float observations
    for the claimed-fair reliability monitor (P1-1, app/backtesting/calibration.
    bet_band_reliability) — the DB read half of GET /performance "calibration".

    Maps each pick to (claimed_fair=model_probability — the probability the
    strategy claimed at bet time, won=outcome=='won', fill_odds=decimal_odds —
    the price actually taken). Only binary settlements (won/lost) carry a
    calibration label; push/void/half_* are excluded (no win/lose outcome).
    Scoped to the PREMIUM tier so the monitor judges the ACTUALLY-ALERTED
    strategy, matching the headline's premium scope. Pure floats out — the
    odds-band scoping and ECE math stay in the pure calibration module.
    """
    from app.backtesting.calibration import BetBandObservation

    rows = (
        await session.execute(
            select(
                Pick.model_probability,
                ResultTracking.outcome,
                Pick.decimal_odds,
            )
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .where(ResultTracking.outcome.in_(("won", "lost")))
            .where(Pick.tier == "premium")
        )
    ).all()
    return [
        BetBandObservation(
            claimed_fair=float(model_probability),
            won=(outcome == "won"),
            fill_odds=float(decimal_odds),
        )
        for model_probability, outcome, decimal_odds in rows
    ]


# asyncpg runs prepared statements: keep each INSERT comfortably under
# Postgres's 32767 bind-parameter limit (8 params/row -> 4000 params/chunk).
_SNAPSHOT_INSERT_CHUNK = 500


def snapshot_market_key(snapshot: OddsSnapshotIn) -> str:
    """The `market` string stored in odds_snapshots: the provider submarket
    key ("asian_handicap_-1_5") when present, else the Market enum value.
    Distinct lines MUST stay distinct observations or downstream devig pools
    a fake multi-leg book. The old 32-char clamp silently truncated real
    quarter-line handicap-games keys (e.g. "asian_handicap_games_-10_25_games",
    33 chars), dropping the trailing axis token so two distinct lines collapsed
    into one devig group AND the reverse mapping mis-parsed. The column is now
    String(64); clamp only as a last-resort overflow guard (no realistic key
    approaches 64) so a distinct line can never be lost to truncation."""
    return (snapshot.market_detail or str(snapshot.market))[:64]


def market_from_snapshot_key(key: str) -> tuple[Market, str | None] | None:
    """Reverse of `snapshot_market_key`: a stored odds_snapshots.market string
    back to (Market enum, market_detail). Plain enum values ("h2h", "totals")
    were stored detail-less; provider submarket keys ("1x2", "home_away",
    "over_under_2_5", "asian_handicap_-1_5") map through the oddsportal
    loader's own key table — single source of truth — so a rebuilt snapshot
    groups EXACTLY like the live scrape did (distinct lines stay distinct
    devig groups via market_detail). Unknown keys return None: skip the row,
    never guess a market."""
    try:
        return Market(key), None
    except ValueError:
        pass
    # Lazy: keep app.storage import-time free of the scraper module.
    from app.ingestion.oddsportal import _market_for_key

    market = _market_for_key(key)
    if market is not None:
        return market, key
    # Provider-neutral fallback for the OddsChecker submarket scheme
    # (app.ingestion.oddschecker._market_detail): line-bearing keys are encoded
    # as ``<enum_value>[_period][_line]`` — "totals_2_5", "spreads_minus_1_5",
    # "team_totals_1_5" — DISTINCT from OddsPortal's OddsHarvester keys
    # ("over_under_2_5", "asian_handicap_-1_5") that _market_for_key handles.
    # Without this, an OddsChecker totals/spreads/team-totals close silently
    # fails to round-trip and closing_odds_from_snapshots drops it, biasing
    # OddsChecker close coverage. The FULL key is kept as market_detail so
    # distinct lines rebuild as distinct devig groups (same invariant as above).
    # TEAM_TOTALS is tested first so its "team_totals_" prefix wins over any
    # shorter enum value. Capture-only OTHER keys ("oc_<slug>") are deliberately
    # NOT mapped — they never mint picks or CLV and must never enter a devig pool.
    for line_market in (Market.TEAM_TOTALS, Market.SPREADS, Market.TOTALS):
        if key.startswith(f"{line_market.value}_"):
            return line_market, key
    return None


async def persist_odds_snapshots(
    session_factory: "async_sessionmaker",
    snapshots: Sequence[OddsSnapshotIn],
    teams_by_event: Mapping[str, EventTeams],
    sport: str,
    default_league: str,
    *,
    attach_only_to_existing: bool = False,
) -> int:
    """Append price observations into odds_snapshots (the backtest /
    line-movement / CLV dataset). Returns the number of NEW rows written.

    Entity resolution reuses the SAME get-or-create helpers persist_pick
    uses — one resolution per event, never a second resolution path — so
    snapshots and picks land on the same events rows. Events missing from
    teams_by_event are skipped (unresolvable this cycle; the caller retries
    next cycle). Re-observations dedupe on uq_odds_snapshot_observation
    (event, bookmaker, market, selection, captured_at) via ON CONFLICT DO
    NOTHING. Odds cross the boundary Decimal-via-string; captured_at is the
    provider-reported observation time, never now().

    ATTACH-ONLY mode (``attach_only_to_existing=True``): persist ONLY for
    external_refs whose Event row ALREADY exists; refs with no event are
    skipped this cycle (logged as a count, never an error) and attach next
    cycle once the canonical event lands. This is the Betfair inline-binding
    safety contract (ADR-0015): the Betfair capture rides the MAIN scrape's
    canonical event and must NEVER MINT one from its own partial data
    (creating an event from Betfair-only metadata could set wrong/partial
    fields and break settlement). The normal create path (default False) is
    unchanged for the main scrape + the pinnacle arcadia archive.

    Failure isolation: each event resolves and inserts inside its OWN
    SAVEPOINT — one poisoned event (e.g. an external_ref longer than its
    column) must not abort the whole cycle's history, every cycle, for as
    long as the bad match stays in the scrape window. A failed event is
    logged (team names + exception type only, never URLs) and skipped; its
    rows count as seen by the caller's change-only cache, which is correct:
    a deterministic overflow would fail identically on every retry.
    Free-text row fields (bookmaker, selection) are clamped to their column
    lengths up front — display strings, where truncation beats losing the
    event's whole history.
    """
    by_event: dict[str, list[OddsSnapshotIn]] = {}
    for snapshot in snapshots:
        if snapshot.event_id in teams_by_event:
            by_event.setdefault(snapshot.event_id, []).append(snapshot)
    if not by_event:
        return 0

    written = 0
    failed_events = 0
    async with session_factory() as session:
        if attach_only_to_existing:
            # ATTACH-ONLY: keep ONLY refs whose Event already exists. The
            # remainder are not errors — they are fixtures the main scrape has
            # not persisted YET (the capture runs in the gap before the next
            # main poll); they attach on a later cycle. One pre-query (a single
            # IN on the globally-unique external_ref), not a per-event create.
            present = set(
                (
                    await session.execute(
                        select(Event.external_ref).where(Event.external_ref.in_(list(by_event)))
                    )
                )
                .scalars()
                .all()
            )
            skipped = len(by_event) - len(present)
            by_event = {ref: snaps for ref, snaps in by_event.items() if ref in present}
            if skipped:
                logger.info(
                    "odds snapshot attach-only (%s): %d/%d events not yet created "
                    "by the main scrape — skipped this cycle, will attach next",
                    sport,
                    skipped,
                    skipped + len(by_event),
                )
            if not by_event:
                return 0
        sport_id = await _get_or_create_sport(session, sport, sport.title())
        for external_ref, event_snapshots in by_event.items():
            teams = teams_by_event[external_ref]
            try:
                event_written = 0
                async with session.begin_nested():
                    league_id = await _get_or_create_league(
                        session, sport_id, teams.league or default_league, teams.country
                    )
                    home_id = await _get_or_create_team(session, sport_id, league_id, teams.home)
                    away_id = await _get_or_create_team(session, sport_id, league_id, teams.away)
                    event_id = await _get_or_create_event(
                        session,
                        sport_id,
                        league_id,
                        home_id,
                        away_id,
                        external_ref,
                        starts_at=teams.starts_at,
                        # scraped scores are NOT written here — only the finished-
                        # gated capture_finished_scores path writes Event.scraped_*.
                    )
                    rows: list[dict[str, Any]] = [
                        {
                            "event_id": event_id,
                            "bookmaker": snapshot.bookmaker[:64],
                            "market": snapshot_market_key(snapshot),
                            "selection": snapshot.selection[:64],
                            "decimal_odds": Decimal(str(snapshot.decimal_odds)),
                            "liquidity": (
                                Decimal(str(snapshot.liquidity))
                                if snapshot.liquidity is not None
                                else None
                            ),
                            "captured_at": snapshot.captured_at,
                            "ingested_at": snapshot.ingested_at,
                        }
                        for snapshot in event_snapshots
                    ]
                    for start in range(0, len(rows), _SNAPSHOT_INSERT_CHUNK):
                        chunk = rows[start : start + _SNAPSHOT_INSERT_CHUNK]
                        stmt = (
                            pg_insert(OddsSnapshot)
                            .values(chunk)
                            .on_conflict_do_nothing(constraint="uq_odds_snapshot_observation")
                            .returning(OddsSnapshot.id)
                        )
                        event_written += len((await session.execute(stmt)).scalars().all())
                written += event_written
            except Exception as exc:  # poisoned event: skip it, keep the cycle
                failed_events += 1
                logger.warning(
                    "odds snapshot persistence skipped event '%s vs %s' (%d rows): %s",
                    teams.home,
                    teams.away,
                    len(event_snapshots),
                    type(exc).__name__,
                )
        await session.commit()
    if failed_events:
        logger.warning(
            "odds snapshot persistence: %d/%d events skipped this cycle",
            failed_events,
            len(by_event),
        )
    return written


async def closing_odds_from_snapshots(
    session: AsyncSession,
    event_id: int,
    external_ref: str,
    kickoff: datetime,
) -> tuple[list[OddsSnapshotIn], datetime | None]:
    """Per-bookmaker odds AT CLOSE from our own odds_snapshots history.

    For every (market, bookmaker, selection) of the event: the LAST row
    captured strictly before kickoff, rebuilt as OddsSnapshotIn (keyed by the
    event's external_ref) so the caller can run the exact live grouping +
    devig pipeline over it. Also returns the EVENT's overall last pre-kickoff
    capture time — the scrape-coverage clock.

    Change-only subtlety (this is the load-bearing design point): the
    pipeline persists a row only when a price MOVES, so a per-book close row
    may be days old and still be that book's true close — the price simply
    never changed while the event kept being scraped. Per-row age must
    therefore NEVER gate validity. What CAN invalidate the close is the event
    falling out of the scrape (dropped from listings days before kickoff):
    that is visible only on the event-wide last-capture time, which the
    caller compares against its staleness window.
    """
    rows = (
        (
            await session.execute(
                select(OddsSnapshot)
                .where(
                    OddsSnapshot.event_id == event_id,
                    # STRICTLY before kickoff: a row captured AT/after kickoff is
                    # an in-play price, never a pre-match close.
                    OddsSnapshot.captured_at < kickoff,
                )
                # Postgres DISTINCT ON: first row per (market, bookmaker,
                # selection) under captured_at-DESC ordering == the close row.
                .distinct(OddsSnapshot.market, OddsSnapshot.bookmaker, OddsSnapshot.selection)
                .order_by(
                    OddsSnapshot.market,
                    OddsSnapshot.bookmaker,
                    OddsSnapshot.selection,
                    OddsSnapshot.captured_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    # The event-wide last pre-kickoff row is the last row of its own group,
    # so the max over group winners IS the event's last-capture time. Taken
    # over ALL rows (even unmappable legacy keys): any row proves coverage.
    last_capture = max((row.captured_at for row in rows), default=None)
    snaps: list[OddsSnapshotIn] = []
    for row in rows:
        mapped = market_from_snapshot_key(row.market)
        if mapped is None or row.decimal_odds <= 1:
            continue  # unknown legacy key / degenerate price: skip, never guess
        market, detail = mapped
        snaps.append(
            OddsSnapshotIn(
                event_id=external_ref,
                bookmaker=row.bookmaker,
                market=market,
                selection=row.selection,
                decimal_odds=float(row.decimal_odds),
                liquidity=float(row.liquidity) if row.liquidity is not None else None,
                captured_at=row.captured_at,
                ingested_at=row.ingested_at,
                market_detail=detail,
            )
        )
    return snaps, last_capture


async def recent_odds_trajectories(
    session: AsyncSession,
    external_refs: Sequence[str],
    *,
    since: datetime,
    until: datetime,
) -> list[OddsSnapshotIn]:
    """Recent odds_snapshots HISTORY for a set of events, re-keyed to external_ref.

    Every observation with ``since <= captured_at <= until`` for the given events,
    rebuilt as OddsSnapshotIn (keyed by the event's external_ref + mapped market)
    so the steam gate (app/edge/steam.py) can read each book's per-selection price
    trajectory. ``until`` (the cycle's ``now``) is the NO-LEAKAGE upper bound — no
    future row ever crosses the boundary. Read-only; [] when no events match.

    The append-only, change-only odds_snapshots store means one row per price
    MOVE, so this returns exactly the recent movement history per book — the
    trajectory the gate needs, at no extra scrape cost.
    """
    refs = [r for r in external_refs if r]
    if not refs:
        return []
    rows = (
        await session.execute(
            select(OddsSnapshot, Event.external_ref)
            .join(Event, OddsSnapshot.event_id == Event.id)
            .where(
                Event.external_ref.in_(refs),
                OddsSnapshot.captured_at >= since,
                OddsSnapshot.captured_at <= until,
            )
        )
    ).all()
    out: list[OddsSnapshotIn] = []
    for row, external_ref in rows:
        mapped = market_from_snapshot_key(row.market)
        if mapped is None or row.decimal_odds <= 1:
            continue  # unknown legacy key / degenerate price: skip, never guess
        market, detail = mapped
        out.append(
            OddsSnapshotIn(
                event_id=external_ref,
                bookmaker=row.bookmaker,
                market=market,
                selection=row.selection,
                decimal_odds=float(row.decimal_odds),
                liquidity=float(row.liquidity) if row.liquidity is not None else None,
                captured_at=row.captured_at,
                ingested_at=row.ingested_at,
                market_detail=detail,
            )
        )
    return out


@dataclass(frozen=True)
class SourceLinkByRef:
    """One confirmed cross-source link keyed by the CANONICAL event's
    external_ref (resolved to events.id at write time). Neutral shape so
    ingestion modules can emit links without importing the ORM."""

    source: str
    source_event_id: str
    canonical_external_ref: str
    confidence: float
    method: str
    matched_at: datetime
    source_market_id: str | None = None
    raw_sport: str | None = None
    raw_league: str | None = None
    raw_home: str | None = None
    raw_away: str | None = None
    raw_start_time_utc: datetime | None = None
    evidence: dict[str, Any] | None = None


async def upsert_event_source_links(session: AsyncSession, links: Sequence[SourceLinkByRef]) -> int:
    """Bulk-upsert confirmed cross-source links (observability — NEVER gates
    matching). Canonical refs that do not resolve to an events row yet are
    skipped (nothing to link against). ON CONFLICT refreshes matched_at +
    confidence (+method), so a re-confirmed link stays one row. Returns the
    number of rows written. Raises on DB failure — callers that must never
    break (anchor resolution) wrap this themselves."""
    if not links:
        return 0
    refs = sorted({link.canonical_external_ref for link in links})
    id_rows = (
        await session.execute(
            select(Event.external_ref, Event.id).where(Event.external_ref.in_(refs))
        )
    ).all()
    id_by_ref = {ref: eid for ref, eid in id_rows}
    values = [
        {
            "canonical_event_id": id_by_ref[link.canonical_external_ref],
            "source": link.source,
            "source_event_id": link.source_event_id,
            "source_market_id": link.source_market_id,
            "confidence_score": Decimal(str(round(link.confidence, 6))),
            "match_method": link.method,
            "matched_at": link.matched_at,
            "raw_sport": link.raw_sport,
            "raw_league": link.raw_league,
            "raw_home": link.raw_home,
            "raw_away": link.raw_away,
            "raw_start_time_utc": link.raw_start_time_utc,
            "evidence_json": link.evidence,
        }
        for link in links
        if link.canonical_external_ref in id_by_ref
    ]
    if not values:
        return 0
    stmt = pg_insert(EventSourceLink).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_event_source_links_source_event",
        set_={
            "matched_at": stmt.excluded.matched_at,
            "confidence_score": stmt.excluded.confidence_score,
            "match_method": stmt.excluded.match_method,
        },
    )
    await session.execute(stmt)
    return len(values)


async def record_source_links(
    session_factory: "async_sessionmaker", links: Sequence[SourceLinkByRef]
) -> int:
    """Open a short-lived session, bulk-upsert the links, COMMIT. The
    composition-root sink for ingestion-time link observations (Betfair API
    capture). Raises on failure — callers wrap (observability must never break
    a capture)."""
    if not links:
        return 0
    async with session_factory() as session:
        written = await upsert_event_source_links(session, links)
        await session.commit()
        return written


@dataclass(frozen=True)
class MatchReviewIn:
    """One borderline matcher reject to enqueue for human review (a TAP on the
    matcher's silently-discarded bands — never a gate)."""

    source: str
    source_event_id: str
    candidate_canonical_event_id: int | None
    confidence: float
    reason: str
    source_market_id: str | None = None
    evidence: dict[str, Any] | None = None


async def enqueue_match_reviews(session: AsyncSession, rows: Sequence[MatchReviewIn]) -> int:
    """Bulk-enqueue borderline rejects into match_review_queue. Idempotent by
    the (source, source_event_id, candidate_canonical_event_id, reason) unique
    key — re-running the matcher never duplicates a queue row (ON CONFLICT DO
    NOTHING). Returns the number of rows offered (not necessarily inserted)."""
    if not rows:
        return 0
    values = [
        {
            "source": row.source,
            "source_event_id": row.source_event_id,
            "source_market_id": row.source_market_id,
            "candidate_canonical_event_id": row.candidate_canonical_event_id,
            "confidence_score": Decimal(str(round(row.confidence, 6))),
            "reason": row.reason,
            "evidence_json": row.evidence,
        }
        for row in rows
    ]
    stmt = (
        pg_insert(MatchReviewQueue)
        .values(values)
        .on_conflict_do_nothing(constraint="uq_match_review_queue_dedupe")
    )
    await session.execute(stmt)
    return len(values)


async def source_link_metrics(session: AsyncSession) -> dict[str, Any]:
    """Roll-up of the cross-source link tables for GET /resolution/match-rate:
    counts + per-source averages over event_source_links and the review-queue
    depth. Read-only and null-safe — empty tables yield zeros/empty maps."""
    auto_linked = await session.scalar(select(func.count()).select_from(EventSourceLink)) or 0
    weak_links = (
        await session.scalar(
            select(func.count())
            .select_from(EventSourceLink)
            .where(EventSourceLink.confidence_score < Decimal("0.95"))
        )
        or 0
    )
    review_queued = (
        await session.scalar(
            select(func.count())
            .select_from(MatchReviewQueue)
            .where(MatchReviewQueue.review_status == "pending")
        )
        or 0
    )
    rejected_observed = (
        await session.scalar(select(func.count()).select_from(MatchReviewQueue)) or 0
    )
    by_source_rows = (
        await session.execute(
            select(
                EventSourceLink.source,
                func.count(),
                func.avg(EventSourceLink.confidence_score),
            ).group_by(EventSourceLink.source)
        )
    ).all()
    return {
        "auto_linked": int(auto_linked),
        "review_queued": int(review_queued),
        "rejected_observed": int(rejected_observed),
        "weak_links": int(weak_links),
        "by_source": {
            source: {
                "links": int(n),
                "avg_confidence": float(avg) if avg is not None else None,
            }
            for source, n, avg in by_source_rows
        },
    }


async def review_queue_rows(
    session: AsyncSession, *, limit: int = 50
) -> list[tuple[MatchReviewQueue, datetime | None]]:
    """Newest ``match_review_queue`` rows for the dashboard's read-only browse
    (GET /resolution/review-queue), each paired with the candidate canonical
    event's kickoff (LEFT JOIN events.starts_at; None when the candidate is
    unlinked or has no kickoff). STRICTLY read-only — marking a row reviewed
    stays in tools/review_queue_cli.py, never the API."""
    result = await session.execute(
        select(MatchReviewQueue, Event.starts_at)
        .outerjoin(Event, MatchReviewQueue.candidate_canonical_event_id == Event.id)
        .order_by(MatchReviewQueue.created_at.desc(), MatchReviewQueue.id.desc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in result.all()]


@dataclass(frozen=True)
class AnchorVerdictIn:
    """One Betfair staleness-guard verdict to persist (betfair_anchor_verdicts).

    Neutral shape so the ingestion layer can emit verdicts without importing
    the ORM (mirrors SourceLinkByRef). ``decision`` is the WRITE-time enum
    (pass|demote|no_api_match|no_api_price); stale_api is a read-time
    classification and is never stored."""

    event_ref: str
    market: str
    selection_role: str
    inline_price: float | None
    api_price: float | None
    api_best_back_size: float | None
    tick_diff: float | None
    inline_captured_at: datetime | None
    api_captured_at: datetime
    decision: str


#: Verdict retention: rows whose api_captured_at is older than this are swept
#: on every sink write. With keep-latest upserts the table sits at ~slate size
#: (<= a few thousand rows); the sweep only clears events that left the window.
BETFAIR_VERDICT_RETENTION = timedelta(days=7)


async def record_betfair_anchor_verdicts(
    session_factory: "async_sessionmaker",
    rows: Sequence[AnchorVerdictIn],
    *,
    retention: timedelta = BETFAIR_VERDICT_RETENTION,
) -> int:
    """Keep-latest upsert of staleness verdicts + the retention sweep.

    One row per (event_ref, market, selection_role): ON CONFLICT replaces the
    stored verdict ONLY when the incoming api_captured_at is not older (a late
    or replayed cycle can never regress a fresher verdict). Commits its own
    short session (the composition-root sink shape, like record_source_links).
    Raises on DB failure — the capture's verdict tap wraps this (type-only log,
    never breaks the capture). Returns the number of rows offered."""
    if not rows:
        return 0
    # Dedupe within the batch (two Betfair markets can match one canonical
    # event): keep the LAST observation per key, or ON CONFLICT would hit the
    # same row twice inside one statement (a Postgres error).
    by_key: dict[tuple[str, str, str], AnchorVerdictIn] = {
        (row.event_ref, row.market, row.selection_role): row for row in rows
    }
    values = [
        {
            "event_ref": row.event_ref,
            "market": row.market,
            "selection_role": row.selection_role,
            "inline_price": (
                Decimal(str(round(row.inline_price, 4))) if row.inline_price is not None else None
            ),
            "api_price": (
                Decimal(str(round(row.api_price, 4))) if row.api_price is not None else None
            ),
            "api_best_back_size": (
                Decimal(str(round(row.api_best_back_size, 2)))
                if row.api_best_back_size is not None
                else None
            ),
            "tick_diff": (
                Decimal(str(round(row.tick_diff, 6))) if row.tick_diff is not None else None
            ),
            "inline_captured_at": row.inline_captured_at,
            "api_captured_at": row.api_captured_at,
            "decision": row.decision,
        }
        for row in by_key.values()
    ]
    stmt = pg_insert(BetfairAnchorVerdict).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_betfair_anchor_verdicts_selection",
        set_={
            "inline_price": stmt.excluded.inline_price,
            "api_price": stmt.excluded.api_price,
            "api_best_back_size": stmt.excluded.api_best_back_size,
            "tick_diff": stmt.excluded.tick_diff,
            "inline_captured_at": stmt.excluded.inline_captured_at,
            "api_captured_at": stmt.excluded.api_captured_at,
            "decision": stmt.excluded.decision,
        },
        where=(BetfairAnchorVerdict.api_captured_at <= stmt.excluded.api_captured_at),
    )
    cutoff = datetime.now(tz=UTC) - retention
    async with session_factory() as session:
        await session.execute(stmt)
        await session.execute(
            sa_delete(BetfairAnchorVerdict).where(BetfairAnchorVerdict.api_captured_at < cutoff)
        )
        await session.commit()
    return len(values)


#: Read-time staleness classification (mirrors app.ingestion.betfair_api's
#: VERDICT_* constants; kept as local literals so this query module never
#: imports the ingestion layer).
_VERDICT_DEMOTE = "demote"
_VERDICT_PASS = "pass"
_VERDICT_STALE_API = "stale_api"


async def load_betfair_staleness_verdicts(
    session_factory: "async_sessionmaker",
    *,
    ttl_seconds: float,
    market: str = "h2h",
    now: datetime | None = None,
) -> dict[str, str]:
    """Latest EFFECTIVE per-event staleness verdict for the mint-time guard.

    Returns ``{event_ref: decision}`` where decision aggregates the event's
    per-selection rows with the freshness TTL applied at READ time:

    * any FRESH (api_captured_at >= now - ttl) row with decision 'demote'
      -> 'demote' (the only value that can ever alter anchoring);
    * else any fresh 'pass' -> 'pass';
    * else any fresh row -> its write-time decision (no_api_match/
      no_api_price — no-ops, kept for the observability stamp);
    * else (only over-TTL rows) -> 'stale_api' — the API could have been down
      for hours; STALE EVIDENCE MUST NEVER DEMOTE A LIVE ANCHOR (direction
      verified in the design), so stale_api is a no-op at mint.

    READ-ONLY and DB-only: the mint path never calls the Betfair API. Raises
    on DB failure — the pipeline's verdict loader wraps this (empty map +
    type-only log; a verdict-read failure never blocks minting)."""
    now = now or datetime.now(tz=UTC)
    async with session_factory() as session:
        result = await session.execute(
            select(
                BetfairAnchorVerdict.event_ref,
                BetfairAnchorVerdict.decision,
                BetfairAnchorVerdict.api_captured_at,
            ).where(BetfairAnchorVerdict.market == market)
        )
        rows = result.all()
    return effective_staleness_verdicts(
        [(ref, decision, captured) for ref, decision, captured in rows],
        ttl_seconds=ttl_seconds,
        now=now,
    )


def effective_staleness_verdicts(
    rows: Sequence[tuple[str, str, datetime | None]],
    *,
    ttl_seconds: float,
    now: datetime,
) -> dict[str, str]:
    """PURE read-time aggregation behind ``load_betfair_staleness_verdicts``
    (tested directly): (event_ref, decision, api_captured_at) rows -> the
    per-event effective decision with the freshness TTL applied. An event whose
    ONLY rows are over the TTL reads 'stale_api' — the direction check: STALE
    API EVIDENCE NEVER DEMOTES, no matter how large the stored disagreement."""
    cutoff = now - timedelta(seconds=ttl_seconds)
    fresh_by_event: dict[str, list[str]] = {}
    seen_events: set[str] = set()
    for event_ref, decision, api_captured_at in rows:
        seen_events.add(event_ref)
        if api_captured_at is not None and api_captured_at >= cutoff:
            fresh_by_event.setdefault(event_ref, []).append(decision)
    out: dict[str, str] = {}
    for event_ref in seen_events:
        fresh = fresh_by_event.get(event_ref, [])
        if not fresh:
            out[event_ref] = _VERDICT_STALE_API
        elif _VERDICT_DEMOTE in fresh:
            out[event_ref] = _VERDICT_DEMOTE
        elif _VERDICT_PASS in fresh:
            out[event_ref] = _VERDICT_PASS
        else:
            out[event_ref] = fresh[0]
    return out


async def betfair_staleness_metrics(
    session: AsyncSession, *, ttl_seconds: float = 900.0
) -> dict[str, Any]:
    """Staleness-guard diagnostics for GET /resolution/match-rate.

    Write-time decision counts (all retained rows), the fresh-vs-stale split at
    the read TTL, the median tick distance and the median inline->API freshness
    gap. Read-only and NULL-SAFE: an empty or absent table yields zeros/None
    (the endpoint must render before the migration/verdicts exist)."""
    empty: dict[str, Any] = {
        "rows": 0,
        "decisions": {},
        "fresh_decisions": {},
        "stale_rows": 0,
        "median_tick_diff": None,
        "median_freshness_gap_seconds": None,
        "ttl_seconds": ttl_seconds,
    }
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=ttl_seconds)
    try:
        decision_rows = (
            await session.execute(
                select(BetfairAnchorVerdict.decision, func.count()).group_by(
                    BetfairAnchorVerdict.decision
                )
            )
        ).all()
        fresh_rows = (
            await session.execute(
                select(BetfairAnchorVerdict.decision, func.count())
                .where(BetfairAnchorVerdict.api_captured_at >= cutoff)
                .group_by(BetfairAnchorVerdict.decision)
            )
        ).all()
        median_tick = await session.scalar(
            select(
                func.percentile_cont(0.5).within_group(BetfairAnchorVerdict.tick_diff.asc())
            ).where(BetfairAnchorVerdict.tick_diff.is_not(None))
        )
        median_gap = await session.scalar(
            select(
                func.percentile_cont(0.5).within_group(
                    func.extract(
                        "epoch",
                        BetfairAnchorVerdict.api_captured_at
                        - BetfairAnchorVerdict.inline_captured_at,
                    ).asc()
                )
            ).where(BetfairAnchorVerdict.inline_captured_at.is_not(None))
        )
    except Exception as exc:  # absent table (pre-migration) -> honest zeros
        logger.warning("betfair staleness metrics unavailable: %s", type(exc).__name__)
        with contextlib.suppress(Exception):
            await session.rollback()
        return empty
    decisions = {decision: int(n) for decision, n in decision_rows}
    fresh = {decision: int(n) for decision, n in fresh_rows}
    total = sum(decisions.values())
    return {
        "rows": total,
        "decisions": decisions,
        "fresh_decisions": fresh,
        "stale_rows": total - sum(fresh.values()),
        "median_tick_diff": float(median_tick) if median_tick is not None else None,
        "median_freshness_gap_seconds": float(median_gap) if median_gap is not None else None,
        "ttl_seconds": ttl_seconds,
    }


async def _record_pinnacle_link_observability(
    session: AsyncSession,
    *,
    pick_external_ref: str,
    accepted_link: SourceLinkByRef | None,
    reviews: Sequence[MatchReviewIn],
) -> None:
    """Best-effort observability writes for one resolve call — NEVER breaks
    anchor resolution. Runs inside a SAVEPOINT so a failed write cannot poison
    the caller's transaction; failures log the exception type only (no odds/
    names/URLs — query strings can carry keys elsewhere)."""
    try:
        async with session.begin_nested():
            if accepted_link is not None:
                await upsert_event_source_links(session, [accepted_link])
            if reviews:
                await enqueue_match_reviews(session, reviews)
    except Exception as exc:  # pragma: no cover - defensive: observability only
        logger.warning(
            "pinnacle link observability write skipped for %s: %s",
            pick_external_ref,
            type(exc).__name__,
        )


async def resolve_pinnacle_close_snaps(
    session: AsyncSession,
    *,
    pinnacle_sport_key: str,
    pick_external_ref: str,
    home: str,
    away: str,
    kickoff: datetime,
    max_day_drift: int = 1,
    provenance_out: dict[str, tuple[float, str]] | None = None,
) -> list[OddsSnapshotIn]:
    """Strict-match a pick's fixture to its `pinnacle_<sport>` ARCHIVE event and
    return that event's CLOSE snapshots, re-keyed to the pick's event_id and
    selection vocabulary (bookmaker stays "Pinnacle"). Each row carries
    captured_at, so a pick-time caller can gate freshness on the event's most-
    recent row. [] when there is no unambiguous match.

    Returns [] when there is no UNAMBIGUOUS match or no Pinnacle coverage — a
    wrong close corrupts CLV, so this never guesses. Matching is the pure
    app.resolution matcher (exact normalized names + alias table + a small
    kickoff window; NO fuzzy). Selections that cannot be mapped to the pick's
    home/away/Draw outcome are dropped rather than mis-attached.
    """
    from app.resolution import (
        EventCandidate,
        MatchReviewCandidate,
        default_aliases,
        distinguishing_markers,
        match_event_hardened_scored,
        normalize_name,
        slug_names,
    )
    from app.resolution.tennis_names import canonical_tennis_name

    # GO-LIVE (shadow-validated, commit 1d697cd: 61.3% match-rate, 0 false merges
    # across 62 audited): the live Pinnacle anchor matcher is now the precision-
    # hardened cross-source matcher, NOT the exact-only match_event. It keeps every
    # cardinal-sin guard (marker veto, disambiguating-token blocklist, ambiguity
    # reject, degenerate-pair reject) and adds the two-tier Jaro-Winkler + token-
    # sort recall tier that the shadow harness measured. Cross-source league
    # taxonomies do NOT share a vocabulary here (OddsPortal league vs the per-
    # namespace pinnacle_<sport> key), so league is passed incomparable (None on
    # both sides) — exactly as the shadow harness effectively does; the matcher
    # never rejects on absent league metadata.
    #
    # WRONG-GAME FIX (2026-06-24, live audit Gigantes/Cangrejeros): the
    # candidate-FETCH window stays the wide (+/-(max_day_drift+1)-day) span the DB
    # query bounds to — so ambiguity detection sees EVERY same-teams leg of a
    # series — but the matcher's ACCEPT gate is the tight default
    # (``_ACCEPT_MINUTE_DRIFT`` = 6h) it carries internally. The go-live flip wrongly
    # passed this +/-2-DAY span as ``max_minute_drift`` AND let it gate acceptance,
    # so a same-teams BSN rematch 48h earlier (home/away swapped, matched via the
    # slug) was accepted as the close — fake CLV. We now keep the wide fetch window
    # for context but let acceptance default to the tight bound: a same-teams fixture
    # two days apart is a DIFFERENT game and is REJECTED, while a few hours of
    # cross-source timezone/rounding noise on the SAME game still matches.
    minute_drift = (max_day_drift + 1) * 24 * 60

    # audit #7: tennis is a two-player, UNORDERED fixture whose OddsPortal name
    # ("Surname I.") differs from arcadia's ("Firstname Surname"). Match it the
    # SAME way the readiness probe does (canonicalize + ordered=False + a shared-
    # token collision guard), or this consume path returns [] for every tennis
    # fixture and tennis CLV-vs-close never attaches.
    is_tennis = pinnacle_sport_key.removeprefix("pinnacle_") == "tennis"

    def _toks(name: str) -> set[str]:
        return set(normalize_name(name).split())

    home_t, away_t = aliased(Team), aliased(Team)
    window = timedelta(days=max_day_drift + 1)
    rows = (
        await session.execute(
            select(Event.id, Event.external_ref, home_t.name, away_t.name, Event.starts_at)
            .join(Sport, Event.sport_id == Sport.id)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .where(
                Sport.key == pinnacle_sport_key,
                Event.starts_at.is_not(None),
                Event.starts_at >= kickoff - window,
                Event.starts_at <= kickoff + window,
            )
        )
    ).all()
    if not rows:
        return []
    by_ref = {str(eid): (eid, ext, h, a, ko) for eid, ext, h, a, ko in rows}
    candidates = [
        EventCandidate(
            ref=str(eid),
            home=canonical_tennis_name(h) if is_tennis else h,
            away=canonical_tennis_name(a) if is_tennis else a,
            kickoff=ko,
        )
        for eid, _ext, h, a, ko in rows
    ]
    aliases = default_aliases()
    qhome = canonical_tennis_name(home) if is_tennis else home
    qaway = canonical_tennis_name(away) if is_tennis else away
    # Observability taps (NEVER gates): borderline rejects the matcher would
    # silently drop are collected here and enqueued for human review below.
    review_taps: list[MatchReviewCandidate] = []
    match_method: str | None = None
    outcome = match_event_hardened_scored(
        qhome,
        qaway,
        kickoff,
        candidates,
        aliases=aliases,
        ordered=not is_tennis,
        league=None,  # cross-source league taxonomies are incomparable here
        candidate_leagues=None,
        max_minute_drift=minute_drift,
        review_out=review_taps,
    )
    if outcome is not None:
        match_method = outcome.method
    matched = outcome.candidate if outcome is not None else None
    if matched is None:
        # Fallback: OddsPortal's URL slug recovers fixtures the scraped display
        # name spelled differently (sponsor tails, abbreviations; live basketball
        # match rate 36% -> 41%). BUT the slug also DROPS women/youth/reserve
        # markers ("W"/"U20"/"II") the display name carries — matching on the
        # marker-less slug would conflate a women's/youth pick with the men's/
        # senior fixture and attach ITS Pinnacle close (a WRONG-GAME CLV defect:
        # the men's "Brasiliense v Sobradinho" close onto a "... U20" pick). So
        # use the slug only when it RETAINS every distinguishing marker the
        # display name has; otherwise the recovery is unsafe and we skip it.
        slug = slug_names(pick_external_ref)
        if slug is not None:
            sh = canonical_tennis_name(slug[0]) if is_tennis else slug[0]
            sa = canonical_tennis_name(slug[1]) if is_tennis else slug[1]
            display_markers = distinguishing_markers(home) | distinguishing_markers(away)
            slug_markers = distinguishing_markers(sh) | distinguishing_markers(sa)
            if display_markers <= slug_markers:
                slug_outcome = match_event_hardened_scored(
                    sh,
                    sa,
                    kickoff,
                    candidates,
                    aliases=aliases,
                    ordered=not is_tennis,
                    league=None,  # cross-source league taxonomies are incomparable here
                    candidate_leagues=None,
                    max_minute_drift=minute_drift,
                    review_out=review_taps,
                )
                if slug_outcome is not None:
                    outcome = slug_outcome
                    matched = slug_outcome.candidate
                    # slug-fallback provenance: same score, 'slug_'-prefixed method
                    match_method = f"slug_{slug_outcome.method}"
    if matched is None or outcome is None:
        # UNMATCHED: enqueue any borderline (review-band) rejects so a human can
        # recover the near-miss via a reviewed per-club alias — the match itself
        # failed exactly as before (the queue is a tap, not a gate).
        if review_taps:
            canonical_id = await session.scalar(
                select(Event.id).where(Event.external_ref == pick_external_ref)
            )
            if canonical_id is not None:
                ext_by_ref = {str(eid): ext for eid, ext, _h, _a, _ko in rows}
                reviews = [
                    MatchReviewIn(
                        source="pinnacle_arcadia",
                        source_event_id=ext_by_ref.get(tap.candidate.ref, tap.candidate.ref),
                        candidate_canonical_event_id=canonical_id,
                        confidence=tap.confidence,
                        reason=tap.reason,
                        evidence=dict(tap.evidence),
                    )
                    for tap in review_taps
                ]
                await _record_pinnacle_link_observability(
                    session,
                    pick_external_ref=pick_external_ref,
                    accepted_link=None,
                    reviews=reviews,
                )
        return []
    # tennis: require a shared normalized token between the pick and the matched
    # arcadia event, so a degenerate surname+initial pair can't attach same-day
    # noise (the readiness-probe collision guard, audit #7).
    if is_tennis and not (
        (_toks(home) | _toks(away)) & (_toks(matched.home) | _toks(matched.away))
    ):
        return []
    pin_id, pin_ref, pin_home, pin_away, pin_kickoff = by_ref[matched.ref]
    # ACCEPTED match: expose the confidence provenance to the caller (per-pick
    # anchor_match_confidence/method) and persist the cross-source link
    # (observability only — a write failure never breaks anchor resolution).
    resolved_method = match_method or outcome.method
    if provenance_out is not None:
        provenance_out[pick_external_ref] = (outcome.confidence, resolved_method)
    await _record_pinnacle_link_observability(
        session,
        pick_external_ref=pick_external_ref,
        accepted_link=SourceLinkByRef(
            source="pinnacle_arcadia",
            source_event_id=pin_ref,
            canonical_external_ref=pick_external_ref,
            confidence=outcome.confidence,
            method=resolved_method,
            matched_at=datetime.now(tz=UTC),
            raw_sport=pinnacle_sport_key,
            raw_home=pin_home,
            raw_away=pin_away,
            raw_start_time_utc=pin_kickoff,
        ),
        reviews=(),
    )
    # Cap the close cutoff at the matched ARCADIA event's OWN kickoff: the match
    # window allows +/- a day of drift, so the arcadia event may start earlier
    # than the pick. Using the pick's kickoff would admit post-arcadia-kickoff
    # (in-play) Pinnacle rows as "the close" -> corrupted CLV (the cardinal sin).
    cutoff = pin_kickoff if pin_kickoff < kickoff else kickoff
    snaps, _last = await closing_odds_from_snapshots(session, pin_id, pin_ref, cutoff)
    # Cannot tell the two outcomes apart by name -> never risk mis-attributing a
    # price to the wrong side; drop the whole close. (The matcher guards this for
    # ordered events, but defend the re-key directly for the unordered path too.)
    if normalize_name(pin_home) == normalize_name(pin_away):
        return []
    # Re-key arcadia selections to the pick's selection vocabulary PER MARKET, so
    # the close groups with the pick's market/line. The selection vocabulary is
    # team-named only for H2H (1X2/moneyline); for the source-keyed markets
    # (totals, Asian handicap) the selection is ALREADY in the pick's vocabulary,
    # so re-keying those through the team-name map would silently DROP every
    # Over/Under and handicap close (a cardinal coverage bug — totals/spreads picks
    # could never get a Pinnacle anchor). market + market_detail (the line) are
    # preserved by model_copy; only event_id and the team-named part of selection
    # are re-keyed.
    if is_tennis:
        # UNORDERED tennis matches can accept a SWAPPED player order (the matcher
        # runs ordered=False for tennis), so arcadia's positional pin_home/pin_away
        # no longer correspond to the pick's home/away. Re-key by canonical NAME,
        # never by position — else a swap attaches the WRONG player's close
        # (wrong-side CLV, the cardinal sin). Degenerate/unmappable names drop (safe).
        ch, ca = canonical_tennis_name(home), canonical_tennis_name(away)
        selection_map = {}
        if ch and ca and ch != ca:
            for raw in (pin_home, pin_away):
                c = canonical_tennis_name(raw)
                if c == ch:
                    selection_map[normalize_name(raw)] = home
                elif c == ca:
                    selection_map[normalize_name(raw)] = away
    else:
        selection_map = {normalize_name(pin_home): home, normalize_name(pin_away): away}
    out: list[OddsSnapshotIn] = []
    for snap in snaps:
        mapped_selection: str | None
        if snap.market == Market.H2H:
            # Team-named 1X2/moneyline outcomes — UNCHANGED re-key. "Draw" is
            # source-independent; an unmappable team name is dropped, never guessed.
            if snap.selection == "Draw":
                mapped_selection = "Draw"
            else:
                mapped_selection = selection_map.get(normalize_name(snap.selection))
        elif snap.market == Market.TOTALS:
            # Over/Under vocabulary is SOURCE-INDEPENDENT (the line rides both the
            # selection text "Over 2.5" and market_detail "over_under_2_5"), so it is
            # already in the pick's vocabulary -> identity (preserve selection + line).
            mapped_selection = snap.selection
        elif snap.market == Market.SPREADS:
            # Asian handicap selection is "{team} {signed}". Re-key ONLY the team-name
            # prefix via the SAME outcome map (home->home, away->away; NEVER swapped),
            # preserving the signed handicap suffix and the line (market_detail). A
            # prefix matching neither side is dropped (safe) — never mis-attached.
            team_part, sep, suffix = snap.selection.rpartition(" ")
            mapped_team = selection_map.get(normalize_name(team_part)) if sep else None
            mapped_selection = f"{mapped_team} {suffix}" if mapped_team is not None else None
        else:
            # Any other market's selection vocabulary is not provably source-
            # independent here -> drop (the safe default; never guess a mapping).
            mapped_selection = None
        if mapped_selection is None:
            continue  # a selection we cannot confidently map -> drop (safe)
        out.append(
            snap.model_copy(update={"event_id": pick_external_ref, "selection": mapped_selection})
        )
    if snaps and not out:
        # MATCHED the fixture but EVERY close dropped in the re-key — the anomalous
        # case the silent per-selection drop hides (a re-key regression would be
        # invisible). Counts + ref only (no odds/names/URLs). Legit-empty is rare.
        logger.info(
            "pinnacle close: matched %s (%s) but re-key emitted 0 of %d snaps",
            pick_external_ref,
            pinnacle_sport_key,
            len(snaps),
        )
    return out


async def shadow_match_rate_outcomes(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    max_day_drift: int = 1,
) -> "list[ShadowOutcome]":
    """SHADOW Pinnacle-archive resolution over picks with a known kickoff — the
    read behind GET /resolution/match-rate.

    For each pick it runs the SAME strict matcher app.clv_trueup uses at
    settlement, but writes NOTHING and attaches no close: it records only
    whether a UNIQUE ``pinnacle_<sport>`` archive event exists for the fixture
    and, diagnostically, how many archive events fell in the kickoff window
    (0 = a coverage gap; >0 with no match = an alias/ambiguity gap). This is the
    instrument ADR-0014 asks be checked before CLV_USE_PINNACLE_ARCHIVE is
    enabled.

    Population: picks whose event has a known kickoff (``Event.starts_at`` NOT
    NULL), optionally limited to kickoffs at/after ``since``. Matching is
    settlement-independent (a future fixture already captured in the archive
    counts), so pass ``since`` to scope to recent fixtures when you only care
    about closes that are realizable now.
    """
    from app.resolution import (
        EventCandidate,
        default_aliases,
        marker_safe_slug_names,
        match_event,
        match_event_hardened,
    )
    from app.resolution.shadow import ShadowOutcome, arcadia_base_sport

    home_t, away_t = aliased(Team), aliased(Team)
    conds: list[Any] = [Event.starts_at.is_not(None)]
    if since is not None:
        conds.append(Event.starts_at >= since)
    pick_rows = (
        await session.execute(
            select(
                Pick.id,
                Sport.key,
                League.key,
                home_t.name,
                away_t.name,
                Event.starts_at,
                Event.external_ref,
            )
            .select_from(Pick)
            .join(Event, Pick.event_id == Event.id)
            .join(Sport, Event.sport_id == Sport.id)
            .join(League, Event.league_id == League.id)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .where(*conds)
        )
    ).all()
    if not pick_rows:
        return []

    # Group picks by their pinnacle_<base> archive namespace; load each
    # namespace's candidate events ONCE over the full kickoff span (+/- window)
    # rather than one query per pick.
    aliases = default_aliases()
    window = timedelta(days=max_day_drift + 1)
    by_namespace: dict[str, list[Any]] = {}
    for row in pick_rows:
        by_namespace.setdefault(f"pinnacle_{arcadia_base_sport(row[1])}", []).append(row)

    outcomes: list[ShadowOutcome] = []
    for pinnacle_key, picks in by_namespace.items():
        kickoffs = [p[5] for p in picks]
        arc_home, arc_away = aliased(Team), aliased(Team)
        arc_league = aliased(League)
        arc_rows = (
            await session.execute(
                select(arc_home.name, arc_away.name, Event.starts_at, arc_league.key)
                .join(Sport, Event.sport_id == Sport.id)
                .join(arc_home, Event.home_team_id == arc_home.id)
                .join(arc_away, Event.away_team_id == arc_away.id)
                .join(arc_league, Event.league_id == arc_league.id, isouter=True)
                .where(
                    Sport.key == pinnacle_key,
                    Event.starts_at.is_not(None),
                    Event.starts_at >= min(kickoffs) - window,
                    Event.starts_at <= max(kickoffs) + window,
                )
            )
        ).all()
        archive = [
            EventCandidate(ref=str(i), home=h, away=a, kickoff=ko)
            for i, (h, a, ko, _lg) in enumerate(arc_rows)
        ]
        # ref -> league for the hardened matcher's STAGE-0 league block. The
        # pinnacle archive namespace stores a single per-namespace league key
        # (pinnacle_<sport>), so cross-league agreement is usually a no-op today,
        # but the map keeps the block honest if/when leagues are populated.
        archive_leagues = {str(i): lg for i, (_h, _a, _ko, lg) in enumerate(arc_rows) if lg}
        for pick_id, sport_key, league_key, home, away, kickoff, ext_ref in picks:
            # Same day window the matcher uses internally — count first so a
            # no-coverage pick is distinguishable from a strict-rejection.
            in_window = [
                c for c in archive if abs((c.kickoff.date() - kickoff.date()).days) <= max_day_drift
            ]
            matched_ev = match_event(
                home, away, kickoff, in_window, aliases=aliases, max_day_drift=max_day_drift
            )
            if matched_ev is None:
                # OddsPortal slug fallback (drops sponsor tails) — same strict
                # unique match, just a cleaner key. Refused when the slug LOSES a
                # women/youth/reserve marker the display names carry: matching on
                # the marker-less slug would pseudo-merge a women's/youth pick
                # onto the men's/senior archive event and OVERSTATE the measured
                # match rate (the wrong-game class the live close-attach path
                # already guards against ~40 lines up).
                slug = marker_safe_slug_names(ext_ref, home, away)
                if slug is not None:
                    matched_ev = match_event(
                        slug[0],
                        slug[1],
                        kickoff,
                        in_window,
                        aliases=aliases,
                        max_day_drift=max_day_drift,
                    )
            if matched_ev is None:
                # SHADOW-only precision-hardened fallback (B): two-tier Jaro-Winkler
                # on marker-stripped base names, league + UTC-minute block, marker
                # veto, disambiguating-token blocklist, ambiguity reject. This path
                # is NEVER on the live anchor loader (which stays exact-only via
                # resolve_pinnacle_close_snaps) — it lifts the MEASURED match rate
                # so the alias/blocking gap can be closed before any live flip.
                matched_ev = match_event_hardened(
                    home,
                    away,
                    kickoff,
                    in_window,
                    aliases=aliases,
                    ordered=sport_key != "tennis",
                    league=league_key,
                    candidate_leagues=archive_leagues,
                )
            matched = matched_ev is not None
            outcomes.append(
                ShadowOutcome(
                    pick_id=pick_id,
                    sport=sport_key,
                    league=league_key,
                    candidates_in_window=len(in_window),
                    matched=matched,
                )
            )
    return outcomes


# Full Betfair-Exchange H2H BACK close width per sport: soccer is 3-way
# (home/draw/away), basketball is 2-way (home/away). Keyed by the arcadia BASE
# sport (so "basketball_nba" -> "basketball"); any unmapped sport falls back to
# the 3-way width (the conservative widest-market requirement).
_BETFAIR_FULL_MARKET_ROWS: dict[str, int] = {"soccer": 3, "basketball": 2}

# The MONEYLINE market KEY as it lands in ``odds_snapshots.market`` per sport —
# the OddsHarvester key string the ingestion persists (NOT the canonical
# ``Market.H2H`` enum value "h2h"): soccer 1X2 is stored as "1x2", basketball
# moneyline as "home_away" (app.ingestion.oddsportal._MARKET_KEYS). This is the
# market the value engine anchors a pick on, so it is the market whose inline
# Betfair Exchange row signals a USABLE sharp anchor.
_MONEYLINE_MARKET_KEY: dict[str, str] = {"soccer": "1x2", "basketball": "home_away"}


def _betfair_full_market_rows(sport_key: str) -> int:
    from app.resolution.shadow import arcadia_base_sport

    return _BETFAIR_FULL_MARKET_ROWS.get(arcadia_base_sport(sport_key), 3)


async def betfair_exchange_coverage_outcomes(
    session: AsyncSession,
    *,
    since: datetime | None = None,
) -> "list[BetfairCoverageOutcome]":
    """SHADOW Betfair-Exchange close coverage over picks with a known kickoff —
    the read-only instrument ADR-0015 asks be checked before
    CLV_USE_BETFAIR_EXCHANGE is enabled.

    For each pick it reproduces EXACTLY what the consumption path
    (app.clv_trueup._betfair_exchange_close / resolve_betfair_back_snaps) would
    resolve. ADR-0015 v2 INLINE BINDING (audit 2026-06-28): the dedicated capture
    persists Betfair rows on the CANONICAL event (``Event.external_ref == ref``,
    ``bookmaker="Betfair Exchange"``), NOT a ``"betfair:"+ref`` namespace — a DEAD
    namespace the producer no longer writes. So this mirrors the now-fixed resolver
    (#139): read the pick's OWN canonical event close set and FILTER it to the
    Betfair book. ``has_betfair_event`` therefore means "the canonical event carries
    Betfair Exchange rows"; ``has_usable_close`` means those Betfair rows form a
    USABLE BACK close — the FULL H2H width for the sport (soccer 3-way, basketball
    2-way), counted over BETFAIR ROWS ONLY (soft books never inflate it), whose own
    most-recent pre-kickoff capture is within SNAPSHOT_CLOSE_MAX_GAP of kickoff (the
    same per-source freshness the consumption path gates the sharp close on). Writes
    NOTHING and attaches no close.

    Population: picks whose event has a known kickoff (``Event.starts_at`` NOT
    NULL), optionally limited to kickoffs at/after ``since``.
    """
    from app.clv_trueup import SNAPSHOT_CLOSE_MAX_GAP
    from app.resolution.shadow import BetfairCoverageOutcome

    home_t, away_t = aliased(Team), aliased(Team)
    conds: list[Any] = [Event.starts_at.is_not(None)]
    if since is not None:
        conds.append(Event.starts_at >= since)
    pick_rows = (
        await session.execute(
            select(Pick.id, Sport.key, League.key, Event.id, Event.external_ref, Event.starts_at)
            .select_from(Pick)
            .join(Event, Pick.event_id == Event.id)
            .join(Sport, Event.sport_id == Sport.id)
            .join(League, Event.league_id == League.id)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .where(*conds)
        )
    ).all()
    if not pick_rows:
        return []

    outcomes: list[BetfairCoverageOutcome] = []
    for pick_id, sport_key, league_key, event_id, external_ref, kickoff in pick_rows:
        has_event = False
        has_close = False
        if kickoff is not None:
            # Mirror resolve_betfair_back_snaps (#139): the pick's OWN canonical
            # event close set, narrowed to the Betfair book.
            snaps, _last = await closing_odds_from_snapshots(
                session, event_id, external_ref, kickoff
            )
            betfair_snaps = [s for s in snaps if s.bookmaker.strip().lower().startswith("betfair")]
            has_event = bool(betfair_snaps)
            # USABLE = FULL H2H width over BETFAIR rows ONLY (the 'betfair%' filter
            # keeps soft books from inflating coverage) whose OWN most-recent capture
            # is within the gap — the consumption path gates the sharp close on its
            # own last capture, NOT the event-wide soft clock.
            betfair_last = max(
                (s.captured_at for s in betfair_snaps if s.captured_at is not None),
                default=None,
            )
            in_window = (
                betfair_last is not None and kickoff - betfair_last <= SNAPSHOT_CLOSE_MAX_GAP
            )
            h2h_rows = sum(1 for s in betfair_snaps if s.market is Market.H2H)
            has_close = in_window and h2h_rows >= _betfair_full_market_rows(sport_key)
        outcomes.append(
            BetfairCoverageOutcome(
                pick_id=pick_id,
                sport=sport_key,
                league=league_key,
                has_betfair_event=has_event,
                has_usable_close=has_close,
            )
        )
    return outcomes


async def betfair_archive_capture_by_sport(
    session: AsyncSession,
    *,
    horizon_days: int = 7,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Per-sport Betfair Exchange coverage for the dashboard panel: of OUR
    upcoming scraped fixtures, how many have a captured Betfair Exchange archive
    event. EXACT ``betfair:`` ref match (no fuzzy) — Betfair only renders on
    liquid majors behind a UK/EU proxy, so this is expected to be sparse. Read-
    only diagnostic; attaches no close, changes no pick."""
    from app.ingestion.betfair_exchange import _namespace_event_ref

    now = now if now is not None else datetime.now(tz=UTC)
    until = now + timedelta(days=horizon_days)
    out: list[dict[str, object]] = []
    for base in ("soccer", "basketball"):
        our_refs = (
            (
                await session.execute(
                    select(Event.external_ref)
                    .join(Sport, Event.sport_id == Sport.id)
                    .where(
                        Sport.key == base,
                        Event.starts_at.is_not(None),
                        Event.starts_at >= now,
                        Event.starts_at <= until,
                    )
                )
            )
            .scalars()
            .all()
        )
        captured = 0
        if our_refs:
            betfair_refs = {_namespace_event_ref(r) for r in our_refs}
            captured = (
                await session.scalar(
                    select(func.count(Event.id)).where(Event.external_ref.in_(betfair_refs))
                )
            ) or 0
        out.append({"sport": base, "scraped": len(our_refs), "captured": int(captured)})
    return out


async def betfair_inline_capture_by_sport(
    session: AsyncSession,
    *,
    horizon_days: int = 7,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Per-sport REAL Betfair-Exchange anchor availability — the number that feeds
    picks. Of OUR upcoming scraped fixtures that carry SOFT odds, how many ALSO
    carry an INLINE ``bookmaker='Betfair Exchange'`` MONEYLINE row on the SAME
    canonical event (the JSON-feed bind, OddsPortal bookie 44).

    "Moneyline" is the market a pick actually anchors on: soccer 1X2 (stored
    ``market='1x2'``), basketball moneyline (``market='home_away'``) — the
    OddsHarvester key strings the ingestion persists, NOT the canonical
    ``Market.H2H`` enum value "h2h". An inline Betfair Exchange row in THAT market
    means the value engine can anchor the pick on the sharp exchange: ``edge.value``
    recognises "Betfair Exchange" as sharp via ``SHARP_BOOKS`` name matching during
    ``derive_value_bets`` — no archive lookup, no ``CLV_USE_BETFAIR_EXCHANGE`` flag.
    It is the correct denominator/numerator for the dashboard's sharp-anchor
    headline.

    DELIBERATELY NOT the separate ``betfair:``-namespaced archive capture
    (``betfair_archive_capture_by_sport``): that path is gated behind
    ``BETFAIR_EXCHANGE_ENABLED`` (default OFF) and captures very few events, so it
    massively undercounts the inline availability that actually anchors picks.

    Output shape mirrors ``betfair_archive_capture_by_sport``
    (``{"sport", "scraped", "captured"}``) so the pure
    ``shadow.summarize_anchor_coverage`` math is unchanged. Read-only diagnostic —
    attaches no close, changes no pick. ``now`` is injectable for tests."""
    now = now if now is not None else datetime.now(tz=UTC)
    until = now + timedelta(days=horizon_days)
    out: list[dict[str, object]] = []
    for base in ("soccer", "basketball"):
        moneyline_key = _MONEYLINE_MARKET_KEY[base]
        # OUR upcoming canonical fixtures carrying SOFT odds (any snapshot at all):
        # a scraped market exists for the event. EXISTS keeps it one row per event.
        soft_event_ids = (
            (
                await session.execute(
                    select(Event.id)
                    .join(Sport, Event.sport_id == Sport.id)
                    .where(
                        Sport.key == base,
                        Event.starts_at.is_not(None),
                        Event.starts_at >= now,
                        Event.starts_at <= until,
                        select(OddsSnapshot.id).where(OddsSnapshot.event_id == Event.id).exists(),
                    )
                )
            )
            .scalars()
            .all()
        )
        captured = 0
        if soft_event_ids:
            captured = (
                await session.scalar(
                    select(func.count(func.distinct(OddsSnapshot.event_id))).where(
                        OddsSnapshot.event_id.in_(soft_event_ids),
                        func.lower(OddsSnapshot.bookmaker) == _BETFAIR_BOOKMAKER.lower(),
                        # OddsPortal stores moneyline as '1x2'/'home_away'; OddsChecker
                        # (the active source) stores it as 'h2h' — accept both so the
                        # inline-Betfair metric is not falsely 0% under oddschecker.
                        OddsSnapshot.market.in_((moneyline_key, "h2h")),
                    )
                )
            ) or 0
        out.append({"sport": base, "scraped": len(soft_event_ids), "captured": int(captured)})
    return out


async def pinnacle_archive_capture_by_sport(
    session: AsyncSession,
    *,
    horizon_days: int = 7,
    max_day_drift: int = 1,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Per-arcadia-sport upcoming coverage for the dashboard's Pinnacle panel.

    For each arcadia sport and kickoffs in the next ``horizon_days`` it reports:
      - ``captured``: fixtures the Pinnacle sharp-close archive holds,
      - ``scraped``:  fixtures WE scraped,
      - ``matched``:  of OURS, how many strict-match a captured Pinnacle close.

    Covers EVERY arcadia sport (tennis + american_football included), so the
    visibility-only sports — which mint no picks and so never appear in the
    pick-based match rate — still get an honest "can a sharp close be attached?"
    number. Uses the SAME strict matcher app.clv_trueup uses at settlement:
    ordered home/away for soccer/basketball/american_football, and the unordered
    two-player match with surname+initial canonicalization for tennis (mirroring
    scripts/research/tennis_clv_readiness.py). Read-only diagnostic — it attaches
    no close and changes no pick.
    """
    from app.resolution import EventCandidate, default_aliases, match_event
    from app.resolution.matching import normalize_name
    from app.resolution.shadow import ARCADIA_SPORTS
    from app.resolution.tennis_names import canonical_tennis_name

    def _toks(name: str) -> set[str]:
        return set(normalize_name(name).split())

    # ``now`` defaults to the wall clock; injectable so tests can window a
    # fixed slice that contains only their seeded fixtures (no behaviour change).
    now = now if now is not None else datetime.now(tz=UTC)
    until = now + timedelta(days=horizon_days)
    pad = timedelta(days=max_day_drift + 1)
    aliases = default_aliases()
    out: list[dict[str, object]] = []
    for base in sorted(ARCADIA_SPORTS):
        is_tennis = base == "tennis"
        fh, fa = aliased(Team), aliased(Team)
        fixtures = (
            await session.execute(
                select(fh.name, fa.name, Event.starts_at)
                .select_from(Event)
                .join(Sport, Event.sport_id == Sport.id)
                .join(fh, Event.home_team_id == fh.id)
                .join(fa, Event.away_team_id == fa.id)
                .where(Sport.key == base, Event.starts_at >= now, Event.starts_at <= until)
            )
        ).all()
        ah, aw = aliased(Team), aliased(Team)
        arc_rows = (
            await session.execute(
                select(ah.name, aw.name, Event.starts_at)
                .select_from(Event)
                .join(Sport, Event.sport_id == Sport.id)
                .join(ah, Event.home_team_id == ah.id)
                .join(aw, Event.away_team_id == aw.id)
                .where(
                    Sport.key == f"pinnacle_{base}",
                    Event.starts_at >= now - pad,
                    Event.starts_at <= until + pad,
                )
            )
        ).all()
        captured = sum(1 for _, _, ko in arc_rows if now <= ko <= until)
        candidates = [
            EventCandidate(
                ref=str(i),
                home=canonical_tennis_name(h) if is_tennis else h,
                away=canonical_tennis_name(a) if is_tennis else a,
                kickoff=ko,
            )
            for i, (h, a, ko) in enumerate(arc_rows)
        ]
        matched = 0
        for home, away, kickoff in fixtures:
            qh = canonical_tennis_name(home) if is_tennis else home
            qa = canonical_tennis_name(away) if is_tennis else away
            in_window = [
                c
                for c in candidates
                if abs((c.kickoff.date() - kickoff.date()).days) <= max_day_drift
            ]
            cand = match_event(
                qh,
                qa,
                kickoff,
                in_window,
                aliases=aliases,
                max_day_drift=max_day_drift,
                ordered=not is_tennis,
            )
            if cand is None:
                continue
            # tennis: require a shared normalized token so a degenerate
            # surname+initial pair can't match same-day noise (readiness-probe guard).
            if is_tennis and not (
                (_toks(home) | _toks(away)) & (_toks(cand.home) | _toks(cand.away))
            ):
                continue
            matched += 1
        out.append(
            {
                "sport": base,
                "captured": captured,
                "scraped": len(fixtures),
                "matched": matched,
            }
        )
    return out


PickPersistOutcome = Literal["inserted", "upgraded", "duplicate", "duplicate_denied"]


async def _supersede_older_versions(
    session: AsyncSession,
    event_id: int,
    market: str,
    selection: str,
    model_version_id: int,
    tier: str,
) -> None:
    """A strategy-version bump re-emits the same opportunity under the new
    version; older OPEN rows for the same (event, market, selection) are
    duplicates on the dashboard — supersede them, keep history. Tier rule:
    a PREMIUM pick supersedes any older open row, but a VOLUME pick may only
    supersede other volume rows — an open premium pick must never be
    displaced by the shadow tier."""
    conditions = [
        Pick.event_id == event_id,
        Pick.market == market,
        Pick.selection == selection,
        Pick.model_version_id != model_version_id,
        Pick.status == "alerted",
    ]
    if tier != "premium":
        conditions.append(Pick.tier != "premium")
    await session.execute(sa_update(Pick).where(*conditions).values(status="superseded"))


async def persist_pick(
    session: AsyncSession,
    pick: PickOut,
    teams: EventTeams,
    model_name: str,
    model_version: str,
) -> PickPersistOutcome:
    """Resolve entities and insert the pick (tier comes from pick.tier).

    Returns:
    - "inserted": a new row was written.
    - "upgraded": the natural key existed as an OPEN volume row and this
      pick clears the premium threshold — the row is promoted in place
      (tier, market numbers, created_at). The caller treats this like a new
      premium pick: dispatch the alert, keep the exposure grant.
    - "duplicate": the key already exists and nothing changed. Covers BOTH
      the same-tier re-detection and the deliberate premium-shield: a key
      already held by a premium row is never touched by a volume candidate
      (the unique key collides across tiers BY DESIGN — one market
      opportunity is one row, whose tier may only ratchet upward).
    - "duplicate_denied": the key exists but its row carries stake <= 0 —
      the CAP-DENIAL marker the pipeline writes when the daily-exposure
      ledger granted nothing at insert (WP2). The caller must NOT dispatch:
      a re-detection would otherwise late-fire, at full stake, the alert
      the cap already refused.

    `status` stays "alerted" for both tiers: it is the lifecycle column
    (open -> settled/superseded/void) shared by revalidation and settlement;
    `tier` alone scopes alerting, exposure, and reporting.
    """
    sport_id = await _get_or_create_sport(session, pick.sport, pick.sport.title())
    # country is part of league IDENTITY — pass the loader's country through so
    # the pick path resolves the SAME (sport, key, country) row the snapshot
    # path minted, instead of forking a ''-country twin per league key.
    league_id = await _get_or_create_league(session, sport_id, pick.league, teams.country)
    home_id = await _get_or_create_team(session, sport_id, league_id, teams.home)
    away_id = await _get_or_create_team(session, sport_id, league_id, teams.away)
    event_id = await _get_or_create_event(
        session,
        sport_id,
        league_id,
        home_id,
        away_id,
        pick.event_id,
        # real kickoff when the loader knows it; else NULL ("kickoff TBD")
        starts_at=teams.starts_at,
        # scraped scores are NOT written here — only the finished-gated
        # capture_finished_scores path writes Event.scraped_*.
    )
    model_version_id = await _get_or_create_model_version(
        session, sport_id, model_name, model_version
    )

    stmt = (
        pg_insert(Pick)
        .values(
            event_id=event_id,
            model_version_id=model_version_id,
            market=str(pick.market),
            selection=pick.selection,
            bookmaker=pick.bookmaker,
            decimal_odds=Decimal(str(pick.decimal_odds)),
            model_probability=Decimal(str(pick.model_probability)),
            fair_probability=Decimal(str(pick.fair_probability)),
            edge=Decimal(str(pick.edge)),
            ev=Decimal(str(pick.ev)),
            confidence=Decimal(str(pick.confidence)),
            recommended_stake_fraction=Decimal(str(pick.recommended_stake_fraction)),
            recommended_stake_amount=pick.recommended_stake_amount,
            stake_breakdown=pick.stake_breakdown.model_dump(),
            reason_summary=pick.reason_summary,
            status="alerted",
            tier=pick.tier,
            value_filter_score=(
                Decimal(str(round(pick.value_filter_score, 6)))
                if pick.value_filter_score is not None
                else None
            ),
            anchor_type=pick.anchor_type,
            # CLV-3: the concrete pick-time anchor BOOK (behind anchor_type) so the CLV
            # close can test BOOK independence, not just anchor-type equality.
            anchor_book=pick.anchor_book,
            # anchor MATCH-CONFIDENCE provenance (observability only): how the
            # sharp anchor was matched to this fixture and how confident the
            # matcher was. NULL/NULL = consensus/model pick or pre-column row.
            anchor_match_confidence=(
                Decimal(str(round(pick.anchor_match_confidence, 6)))
                if pick.anchor_match_confidence is not None
                else None
            ),
            anchor_match_method=pick.anchor_match_method,
            # Betfair staleness-guard mint stamp (observability ONLY): the effective
            # verdict the guard read for this pick's event at mint — would-demote
            # under SHADOW, actual demotion under enforce. NULL when the guard is
            # off / no verdict / non-H2H.
            anchor_staleness_decision=pick.anchor_staleness_decision,
            # A5: steam SHADOW verdict at mint (observability ONLY — nothing gates
            # on these; NULLs = not evaluated). Evidence for the OFF steam gate.
            steam_tripped=pick.steam_tripped,
            steam_reasons=pick.steam_reasons,
            steam_closed_fraction=(
                Decimal(str(round(pick.steam_closed_fraction, 6)))
                if pick.steam_closed_fraction is not None
                else None
            ),
            steam_anchor_age_seconds=(
                Decimal(str(round(pick.steam_anchor_age_seconds, 6)))
                if pick.steam_anchor_age_seconds is not None
                else None
            ),
            # P2-2: mint-side devig-fallback provenance (close side stamped by the CLV
            # true-up) — the trusted CLV subset drops asymmetric mint/close fallbacks.
            mint_devig_fell_back=pick.mint_devig_fell_back,
            # H3: the live policy regime that minted this pick — so CLV is scoped to
            # the exact policy, never mixed across config changes. None for the model
            # strategy (which sets no fingerprint).
            policy_fingerprint=pick.policy_fingerprint,
            created_at=datetime.now(tz=UTC),
        )
        .on_conflict_do_nothing(constraint="uq_picks_event_market_selection_model")
        .returning(Pick.id)
    )
    result = await session.execute(stmt)
    inserted = result.scalar_one_or_none()
    if inserted is not None:
        await _supersede_older_versions(
            session, event_id, str(pick.market), pick.selection, model_version_id, pick.tier
        )
        return "inserted"

    existing = await session.scalar(
        select(Pick).where(
            Pick.event_id == event_id,
            Pick.market == str(pick.market),
            Pick.selection == pick.selection,
            Pick.model_version_id == model_version_id,
        )
    )
    if (
        pick.tier == "premium"
        and existing is not None
        and existing.tier == "volume"
        and existing.status == "alerted"
    ):
        # volume -> premium UPGRADE: the shadow pick's edge now clears the
        # alert threshold. Promote the row in place with the premium
        # detection's market numbers (the alert must quote the row).
        existing.tier = "premium"
        existing.bookmaker = pick.bookmaker
        existing.decimal_odds = Decimal(str(pick.decimal_odds))
        existing.model_probability = Decimal(str(pick.model_probability))
        existing.fair_probability = Decimal(str(pick.fair_probability))
        existing.edge = Decimal(str(pick.edge))
        existing.ev = Decimal(str(pick.ev))
        existing.confidence = Decimal(str(pick.confidence))
        existing.recommended_stake_fraction = Decimal(str(pick.recommended_stake_fraction))
        existing.recommended_stake_amount = pick.recommended_stake_amount
        existing.stake_breakdown = pick.stake_breakdown.model_dump()
        existing.reason_summary = pick.reason_summary
        # the promoting detection's score replaces the shadow row's (it is
        # the score of the alert the operator will actually see)
        existing.value_filter_score = (
            Decimal(str(round(pick.value_filter_score, 6)))
            if pick.value_filter_score is not None
            else None
        )
        # likewise the promoting detection's anchor: the row must describe
        # the alert the operator acts on
        existing.anchor_type = pick.anchor_type
        existing.anchor_book = pick.anchor_book
        existing.anchor_match_confidence = (
            Decimal(str(round(pick.anchor_match_confidence, 6)))
            if pick.anchor_match_confidence is not None
            else None
        )
        existing.anchor_match_method = pick.anchor_match_method
        # the promoting detection's staleness verdict replaces the shadow row's
        # (observability only — describes the alert the operator acts on)
        existing.anchor_staleness_decision = pick.anchor_staleness_decision
        # A5: likewise the promoting detection's steam shadow verdict — the row
        # must describe the mint the operator acts on (observability only)
        existing.steam_tripped = pick.steam_tripped
        existing.steam_reasons = pick.steam_reasons
        existing.steam_closed_fraction = (
            Decimal(str(round(pick.steam_closed_fraction, 6)))
            if pick.steam_closed_fraction is not None
            else None
        )
        existing.steam_anchor_age_seconds = (
            Decimal(str(round(pick.steam_anchor_age_seconds, 6)))
            if pick.steam_anchor_age_seconds is not None
            else None
        )
        # the promoting detection's policy regime replaces the shadow row's: the
        # row now describes the premium alert the operator acts on, so its CLV must
        # attribute to the policy that promoted it (H3).
        existing.policy_fingerprint = pick.policy_fingerprint
        # P2-2: likewise the promoting detection's mint-side devig-fallback flag —
        # the row's fair/model numbers are now the premium detection's, so a stale
        # volume-mint flag would feed _devig_fallback_asymmetric the WRONG mint
        # side and corrupt the trusted-CLV admission verdict at settlement.
        existing.mint_devig_fell_back = pick.mint_devig_fell_back
        # created_at advances to the upgrade moment: it is when the pick
        # became an actionable premium alert AND when its exposure was
        # reserved — seed_exposure_ledger (premium-scoped, created_at within
        # today) must re-find this reservation after a restart.
        existing.created_at = datetime.now(tz=UTC)
        # Revalidation verdicts priced the OLD odds — reset; the next poll
        # cycle re-prices the promoted row from scratch.
        existing.closing_fair_probability = None
        existing.clv_log = None
        existing.beat_close = None
        # close-side provenance also described the OLD fill — clear it so a future
        # refactor that writes closing_odds earlier can't leave stale close data
        # on a re-priced row (audit #6; closing_odds is NULL here today).
        existing.closing_odds = None
        existing.closing_anchor_type = None
        # close-side provenance also described the OLD fill — clear the snapshot-close
        # marker and independence flag so a re-priced row never carries stale trusted-
        # CLV provenance (clv-1 / P0-1).
        existing.has_snapshot_close = None
        existing.close_independent_of_fill = None
        # A4: the exclusion reason described the OLD fill's close — clear it
        # with the boolean it annotates.
        existing.close_exclusion_reason = None
        # P2-2: the close-side devig flag, anchor book, and capture time were
        # stamped against the OLD fill (revalidation cycles write them on open
        # picks) — clear them with the rest of the close provenance; the next
        # revalidation / close true-up re-stamps the promoted row from scratch.
        existing.close_devig_fell_back = None
        existing.close_anchor_book = None
        existing.close_snapshot_captured_at = None
        existing.current_odds = None
        existing.current_edge = None
        existing.current_bookmaker = None
        existing.revalidated_at = None
        await session.flush()
        await _supersede_older_versions(
            session, event_id, str(pick.market), pick.selection, model_version_id, "premium"
        )
        return "upgraded"
    if existing is not None and existing.recommended_stake_fraction <= 0:
        # WP2: stake <= 0 is the cap-denial marker (the daily-exposure ledger
        # granted nothing when this row was inserted) — the re-detection must
        # never dispatch the alert the cap refused.
        return "duplicate_denied"
    return "duplicate"


async def update_pick_stake(
    session: AsyncSession,
    pick: PickOut,
    teams: EventTeams,
    model_name: str,
    model_version: str,
) -> bool:
    """Overwrite an already-persisted pick's recommended stake with the value
    actually reserved by the daily-exposure ledger.

    BUG 2: picks are persisted (per-bet-capped Kelly) BEFORE the daily-exposure
    reservation runs, so a pick whose stake the daily cap then clips would keep
    the pre-clip amount on its row — the persisted/reported stake would escape
    the daily cap and diverge from what the ledger reserved. The pipeline calls
    this AFTER a clip to bring the row in line with the granted stake.

    Resolves the same natural key as `persist_pick` (the get-or-create lookups
    are idempotent: the row already exists). Returns True when a row was
    updated, False when none matched (nothing to correct). Stakes remain
    informational/recommended only.
    """
    sport_id = await _get_or_create_sport(session, pick.sport, pick.sport.title())
    # same (sport, key, country) league identity as persist_pick — never a ''-twin
    league_id = await _get_or_create_league(session, sport_id, pick.league, teams.country)
    home_id = await _get_or_create_team(session, sport_id, league_id, teams.home)
    away_id = await _get_or_create_team(session, sport_id, league_id, teams.away)
    event_id = await _get_or_create_event(
        session, sport_id, league_id, home_id, away_id, pick.event_id, starts_at=teams.starts_at
    )
    model_version_id = await _get_or_create_model_version(
        session, sport_id, model_name, model_version
    )
    existing = await session.scalar(
        select(Pick).where(
            Pick.event_id == event_id,
            Pick.market == str(pick.market),
            Pick.selection == pick.selection,
            Pick.model_version_id == model_version_id,
        )
    )
    if existing is None:
        return False
    existing.recommended_stake_fraction = Decimal(str(pick.recommended_stake_fraction))
    existing.recommended_stake_amount = pick.recommended_stake_amount
    existing.stake_breakdown = pick.stake_breakdown.model_dump()
    await session.flush()
    return True


async def load_dashboard_credentials(
    session: AsyncSession,
) -> tuple[str, str, str] | None:
    """The stored admin credential as ``(username, password_hash,
    session_secret)``, or None if first-run /setup has not created one yet.
    Read once at startup and again right after /setup writes — never per
    request (auth keeps an in-memory copy)."""
    row = await session.scalar(select(DashboardCredential).limit(1))
    if row is None:
        return None
    return (row.username, row.password_hash, row.session_secret)


async def create_dashboard_credentials(
    session: AsyncSession,
    *,
    username: str,
    password_hash: str,
    session_secret: str,
) -> bool:
    """INSERT the single admin credential row. Returns False and writes nothing
    if one already exists — first-run /setup is one-shot, and a later password
    change must go through an authenticated path, never this endpoint. The
    UNIQUE(singleton) constraint backstops a concurrent double-insert."""
    existing = await session.scalar(select(DashboardCredential.id).limit(1))
    if existing is not None:
        return False
    session.add(
        DashboardCredential(
            username=username,
            password_hash=password_hash,
            session_secret=session_secret,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


_EMPTY_BANKROLL_REPORT: dict[str, Any] = {
    "active": False,
    "starting_balance": None,
    "current_balance": None,
    "max_drawdown": None,
    "n_entries": 0,
    "series": [],
}


async def bankroll_ledger_report(session: AsyncSession) -> dict[str, Any]:
    """Running HYPOTHETICAL-bankroll series + current balance + max drawdown
    (A8 read aggregate; feeds the B7 bankroll/ROI chart). Informational only.

    FEATURE-DETECTED at the TABLE level (unlike the per-column getattr pattern,
    the ORM class always exists in code): on a pre-migration DB
    ``to_regclass('bankroll_ledger')`` is NULL and the empty inactive shape is
    served instead of raising UndefinedTable. ``active`` is True once the
    ledger has rows (i.e. BANKROLL_STARTING_BALANCE was set and a sync ran).
    ``max_drawdown`` is the largest peak-to-trough fraction of the
    balance_after series (None until a drawdown exists, never fabricated).
    """
    if await session.scalar(text("SELECT to_regclass('bankroll_ledger')")) is None:
        return dict(_EMPTY_BANKROLL_REPORT, series=[])
    entries = (
        (
            await session.execute(
                select(BankrollLedgerEntry).order_by(
                    BankrollLedgerEntry.occurred_at, BankrollLedgerEntry.id
                )
            )
        )
        .scalars()
        .all()
    )
    if not entries:
        return dict(_EMPTY_BANKROLL_REPORT, series=[])
    peak = entries[0].balance_after
    max_dd = Decimal("0")
    for entry in entries:
        peak = max(peak, entry.balance_after)
        if peak > 0:
            max_dd = max(max_dd, (peak - entry.balance_after) / peak)
    starting = next(
        (e.amount for e in entries if e.entry_type == "starting_balance"),
        None,
    )
    return {
        "active": True,
        "starting_balance": float(starting) if starting is not None else None,
        "current_balance": float(entries[-1].balance_after),
        "max_drawdown": float(max_dd) if max_dd > 0 else None,
        "n_entries": len(entries),
        "series": [
            {
                "occurred_at": e.occurred_at.isoformat(),
                "entry_type": e.entry_type,
                "amount": float(e.amount),
                "balance_after": float(e.balance_after),
                "pick_id": e.pick_id,
                "note": e.note,
            }
            for e in entries
        ],
    }


# --------------------------------------------------------------------------- #
# B1 — per-(sport, market) promotion-distance (trusted-CLV evidence accrual)
# --------------------------------------------------------------------------- #

#: Per-(sport, market) evidence "ok" floor for the promotion-distance widget —
#: mirrors MIN_BUCKET_N in scripts/research/sport_quality_report.py (the
#: report-scale honesty floor for any per-bucket claim), kept app-local so the
#: app never imports research scripts. This is a REPORTING threshold only:
#: promotion itself stays gated by SportMarketClvGate
#: (app/backtesting/live_evidence.py, min_n_sharp_close=500, default-OFF) plus
#: operator ADR sign-off — nothing here promotes or implies imminence.
SPORT_MARKET_OK_N = 30

#: Trailing window for the sample-accrual cadence behind the days-to-threshold
#: estimate. Too short and one busy weekend fakes a cadence; two weeks spans
#: at least two full fixture cycles for every covered sport.
PROMOTION_CADENCE_WINDOW_DAYS = 14


def _settled_close_is_trusted(
    *,
    clv_log: Any,
    closing_anchor: Any,
    close_independent: Any,
    has_snapshot_close: Any,
    decimal_odds: Any,
    closing_fair_probability: Any,
    model_probability: Any,
    mint_devig_fell_back: Any,
    close_devig_fell_back: Any,
) -> bool:
    """The trusted sharp-close gate as a standalone predicate.

    Exactly the guards ``_aggregate_settled`` applies to its trusted subset
    (kept adjacent in this module — see that function for the full rationale):
    a measured clv_log, a GENUINE snapshot close, a named sharp close anchor,
    independence exactly True, non-tautological, non-fabricated, and a
    symmetric devig fallback. Used by per-(sport, market) accrual counts so
    they can never diverge from the headline's trust definition.
    """
    if clv_log is None or not bool(has_snapshot_close):
        return False
    if closing_anchor not in _SHARP_CLOSE_ANCHORS or close_independent is not True:
        return False
    if _clv_row_is_tautological(clv_log, closing_fair_probability, model_probability):
        return False
    if _clv_row_is_fabricated(clv_log, decimal_odds, closing_fair_probability):
        return False
    return not _devig_fallback_asymmetric(mint_devig_fell_back, close_devig_fell_back)


def promotion_distance_cells(
    rows: Sequence[tuple[Any, ...]],
    *,
    now: datetime,
    ok_n: int = SPORT_MARKET_OK_N,
    window_days: int = PROMOTION_CADENCE_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Per-(sport, market) trusted-CLV accrual cells (pure — no DB).

    ``rows`` are settled-pick tuples: (sport, market, settled_at, clv_log,
    closing_anchor, close_independent, has_snapshot_close, decimal_odds,
    closing_fair_probability, model_probability, mint_devig_fell_back,
    close_devig_fell_back). Feature-detected-absent columns arrive as None.

    Honesty rules (binding, mirrored by the dashboard widget):
      - every cell carries its denominators (n_settled, n_trusted);
      - mean/SE point estimates are NULLED at the source below ``ok_n`` — no
        consumer can read a sub-floor CLV estimate, whether or not it honors
        the status flag;
      - ``est_days_to_threshold`` is a LINEAR extrapolation of the trailing
        ``window_days`` trusted-close cadence; None when the threshold is
        already met or no trusted close accrued in the window (the dashboard
        renders "—", never a guess).
    """
    cutoff = now - timedelta(days=window_days)
    settled_counts: dict[tuple[str, str], int] = {}
    trusted: dict[tuple[str, str], list[tuple[float, datetime | None]]] = {}
    for (
        sport,
        market,
        settled_at,
        clv_log,
        closing_anchor,
        close_independent,
        has_snapshot_close,
        decimal_odds,
        closing_fair_probability,
        model_probability,
        mint_devig_fell_back,
        close_devig_fell_back,
    ) in rows:
        key = (str(sport), str(market))
        settled_counts[key] = settled_counts.get(key, 0) + 1
        if _settled_close_is_trusted(
            clv_log=clv_log,
            closing_anchor=closing_anchor,
            close_independent=close_independent,
            has_snapshot_close=has_snapshot_close,
            decimal_odds=decimal_odds,
            closing_fair_probability=closing_fair_probability,
            model_probability=model_probability,
            mint_devig_fell_back=mint_devig_fell_back,
            close_devig_fell_back=close_devig_fell_back,
        ):
            trusted.setdefault(key, []).append((float(clv_log), settled_at))
    cells: list[dict[str, Any]] = []
    for key in sorted(settled_counts):
        cell_trusted = trusted.get(key, [])
        n = len(cell_trusted)
        n_recent = sum(1 for _v, s in cell_trusted if s is not None and s >= cutoff)
        ok = n >= ok_n
        mean: float | None = None
        se: float | None = None
        if ok:
            vals = [v for v, _s in cell_trusted]
            mean = sum(vals) / n
            if n >= 2:
                se = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) / math.sqrt(n)
        est: float | None = None
        if not ok and n_recent > 0:
            est = (ok_n - n) * window_days / n_recent
        cells.append(
            {
                "sport": key[0],
                "market": key[1],
                "n_settled": settled_counts[key],
                "n_trusted": n,
                "ok_n": ok_n,
                "status": "ok" if ok else "accruing",
                "n_recent_trusted": n_recent,
                "cadence_window_days": window_days,
                "est_days_to_threshold": est,
                # nulled at the source below ok_n — never a sub-floor estimate
                "mean_clv_log": mean,
                "se_clv_log": se,
            }
        )
    cells.sort(key=lambda c: (-int(c["n_trusted"]), str(c["sport"]), str(c["market"])))
    return cells


async def sport_market_promotion_distance(session: AsyncSession) -> dict[str, Any]:
    """B1 aggregate behind GET /lab/promotion-distance (read-only).

    One SELECT over settled picks; the close-provenance columns are
    FEATURE-DETECTED exactly like performance_report (a pre-migration DB
    serves the report with those inputs None, so nothing counts as trusted
    — honest, never a 500)."""
    close_anchor_attr = getattr(Pick, "closing_anchor_type", None)
    indep_attr = getattr(Pick, "close_independent_of_fill", None)
    snapshot_attr = getattr(Pick, "has_snapshot_close", None)
    mint_fb_attr = getattr(Pick, "mint_devig_fell_back", None)
    close_fb_attr = getattr(Pick, "close_devig_fell_back", None)
    select_cols: list[Any] = [
        Sport.key,
        Pick.market,
        ResultTracking.settled_at,
        Pick.clv_log,
        Pick.decimal_odds,
        Pick.closing_fair_probability,
        Pick.model_probability,
    ]
    optional_idx: dict[str, int | None] = {}
    for name, attr in (
        ("closing_anchor", close_anchor_attr),
        ("close_independent", indep_attr),
        ("has_snapshot_close", snapshot_attr),
        ("mint_devig_fell_back", mint_fb_attr),
        ("close_devig_fell_back", close_fb_attr),
    ):
        if attr is not None:
            optional_idx[name] = len(select_cols)
            select_cols.append(attr)
        else:
            optional_idx[name] = None
    db_rows = (
        await session.execute(
            select(*select_cols)
            .select_from(ResultTracking)
            .join(Pick, ResultTracking.pick_id == Pick.id)
            .join(Event, Pick.event_id == Event.id)
            .join(Sport, Event.sport_id == Sport.id)
        )
    ).all()

    def _opt(r: Any, name: str) -> Any:
        idx = optional_idx[name]
        return r[idx] if idx is not None else None

    rows = [
        (
            r[0],
            r[1],
            r[2],
            r[3],
            _opt(r, "closing_anchor"),
            _opt(r, "close_independent"),
            _opt(r, "has_snapshot_close"),
            r[4],
            r[5],
            r[6],
            _opt(r, "mint_devig_fell_back"),
            _opt(r, "close_devig_fell_back"),
        )
        for r in db_rows
    ]
    return {
        "ok_n": SPORT_MARKET_OK_N,
        "cadence_window_days": PROMOTION_CADENCE_WINDOW_DAYS,
        "note": (
            "Distance to the trusted-CLV evidence floor only — informational. "
            "Promotion stays gated by SportMarketClvGate and operator ADR sign-off."
        ),
        "cells": promotion_distance_cells(rows, now=datetime.now(tz=UTC)),
    }


# --------------------------------------------------------------------------- #
# B3 — Pinnacle match-ceiling decomposition (structural vs addressable), LIVE
# --------------------------------------------------------------------------- #


def _normalize_league_identity(name: str, country: str) -> tuple[str, str]:
    """(normalized country, normalized name) for exact-equality league matching.

    Mirrors normalize_league in scripts/research/sport_quality_report.py (A1) —
    kept app-local so the app never imports research scripts; parity is pinned
    in tests/test_match_ceiling.py. Never fuzzy/substring (wrong-league risk).
    """

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    return norm(country), norm(name)


def _classify_unmatched_event(
    league_id: int,
    league_name: str,
    league_country: str,
    *,
    co_map: Mapping[int, set[int]],
    pinnacle_inwindow_ids: set[int],
    pinnacle_inwindow_names: set[tuple[str, str]],
) -> str:
    """Classify one UNMATCHED OddsPortal-side event by league evidence.

    'structural' = Pinnacle has zero in-window events for the event's league
    (no match was ever possible); 'addressable' = Pinnacle prices the league
    in-window but the matcher missed the event; 'unknown' = no league-identity
    evidence either way — never guessed. Conservative heuristic: (1) the
    all-time co-occurrence map of matched pairs; (2) else exact normalized
    league-name equality (countries must agree unless one side is empty);
    (3) no fuzzy matching. Mirrors classify_unmatched_event in
    scripts/research/sport_quality_report.py (A1); parity is pinned in
    tests/test_match_ceiling.py.
    """
    mapped = co_map.get(league_id)
    if mapped:
        return "addressable" if mapped & pinnacle_inwindow_ids else "structural"
    country, name = _normalize_league_identity(league_name, league_country)
    for p_country, p_name in pinnacle_inwindow_names:
        if name and p_name == name and (not country or not p_country or p_country == country):
            return "addressable"
    return "unknown"


def _corrected_match_rates(
    events: int, matched: int, structural: int, unknown: int
) -> tuple[float | None, float | None]:
    """(lower, upper) corrected match rates — mirrors corrected_match_rates in
    scripts/research/sport_quality_report.py: lower excludes only PROVEN
    structural events from the denominator; upper also excludes unknown-league
    events (the optimistic bound if every unknown league is truly unpriced)."""
    lower_den = events - structural
    upper_den = events - structural - unknown
    return (
        matched / lower_den if lower_den > 0 else None,
        matched / upper_den if upper_den > 0 else None,
    )


def match_ceiling_blocks(
    totals: Sequence[tuple[str, int]],
    matched: Sequence[tuple[str, int]],
    unmatched: Sequence[tuple[str, int, str | None, str | None]],
    co_rows: Sequence[tuple[int, int]],
    pinn_leagues: Sequence[tuple[str, int, str | None, str | None]],
) -> dict[str, dict[str, Any]]:
    """Assemble the per-sport ceiling blocks from fetched rows (pure — no DB).

    ``totals``/``matched`` = per-sport in-window event counts (all / with an
    active pinnacle_arcadia link); ``unmatched`` = (sport, league_id, league
    name, country) per unmatched event; ``co_rows`` = all-time (canonical
    league_id, pinnacle league_id) matched pairs; ``pinn_leagues`` =
    (pinnacle_* sport key, league_id, name, country) leagues with >=1
    in-window event.
    """
    co_map: dict[int, set[int]] = {}
    for can_lid, pinn_lid in co_rows:
        co_map.setdefault(int(can_lid), set()).add(int(pinn_lid))
    ids_by_sport: dict[str, set[int]] = {}
    names_by_sport: dict[str, set[tuple[str, str]]] = {}
    for pinn_sport, lid, lname, lcountry in pinn_leagues:
        base = pinn_sport.removeprefix("pinnacle_")
        ids_by_sport.setdefault(base, set()).add(int(lid))
        names_by_sport.setdefault(base, set()).add(
            _normalize_league_identity(lname or "", lcountry or "")
        )
    counts: dict[str, dict[str, int]] = {}
    for sport, league_id, lname, lcountry in unmatched:
        verdict = _classify_unmatched_event(
            int(league_id),
            lname or "",
            lcountry or "",
            co_map=co_map,
            pinnacle_inwindow_ids=ids_by_sport.get(sport, set()),
            pinnacle_inwindow_names=names_by_sport.get(sport, set()),
        )
        cell = counts.setdefault(sport, {"structural": 0, "addressable": 0, "unknown": 0})
        cell[verdict] += 1
    matched_by_sport = {s: int(n) for s, n in matched}
    blocks: dict[str, dict[str, Any]] = {}
    for sport, events in sorted(totals):
        n_events = int(events)
        n_matched = matched_by_sport.get(sport, 0)
        cell = counts.get(sport, {"structural": 0, "addressable": 0, "unknown": 0})
        lower, upper = _corrected_match_rates(
            n_events, n_matched, cell["structural"], cell["unknown"]
        )
        blocks[sport] = {
            "events": n_events,
            "matched": n_matched,
            "matched_rate": (n_matched / n_events) if n_events else None,
            "unmatched": n_events - n_matched,
            "structural": cell["structural"],
            "addressable": cell["addressable"],
            "unknown_league": cell["unknown"],
            "corrected_match_rate_lower": lower,
            "corrected_match_rate_upper": upper,
        }
    return blocks


async def match_ceiling_decomposition(session: AsyncSession, *, days: int = 30) -> dict[str, Any]:
    """B3 aggregate behind GET /resolution/match-ceiling — the A1 ceiling
    decomposition computed LIVE against the DB (READ-ONLY SELECTs; never the
    static research artifact). Same conservative classification as
    scripts/research/sport_quality_report.py."""
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=days)
    base_sport = and_(
        ~Sport.key.startswith("pinnacle_", autoescape=True),
        ~Sport.key.startswith("betfair_", autoescape=True),
    )
    in_window = and_(Event.starts_at >= cutoff, Event.starts_at <= now)
    link_exists = (
        select(EventSourceLink.id)
        .where(
            EventSourceLink.canonical_event_id == Event.id,
            EventSourceLink.source == "pinnacle_arcadia",
            EventSourceLink.active,
        )
        .exists()
    )
    totals = (
        await session.execute(
            select(Sport.key, func.count())
            .select_from(Event)
            .join(Sport, Event.sport_id == Sport.id)
            .where(base_sport, in_window)
            .group_by(Sport.key)
        )
    ).all()
    matched = (
        await session.execute(
            select(Sport.key, func.count())
            .select_from(Event)
            .join(Sport, Event.sport_id == Sport.id)
            .where(base_sport, in_window, link_exists)
            .group_by(Sport.key)
        )
    ).all()
    unmatched = (
        await session.execute(
            select(Sport.key, Event.league_id, League.name, League.country)
            .select_from(Event)
            .join(Sport, Event.sport_id == Sport.id)
            .join(League, Event.league_id == League.id)
            .where(base_sport, in_window, ~link_exists)
        )
    ).all()
    pinn_event = aliased(Event)
    pinn_sport = aliased(Sport)
    co_rows = (
        await session.execute(
            select(Event.league_id, pinn_event.league_id)
            .select_from(EventSourceLink)
            .join(Event, Event.id == EventSourceLink.canonical_event_id)
            .join(pinn_event, pinn_event.external_ref == EventSourceLink.source_event_id)
            .join(pinn_sport, pinn_event.sport_id == pinn_sport.id)
            .where(
                EventSourceLink.source == "pinnacle_arcadia",
                EventSourceLink.active,
                pinn_sport.key.startswith("pinnacle_", autoescape=True),
            )
            .distinct()
        )
    ).all()
    pinn_leagues = (
        await session.execute(
            select(Sport.key, League.id, League.name, League.country)
            .select_from(Event)
            .join(Sport, Event.sport_id == Sport.id)
            .join(League, Event.league_id == League.id)
            .where(Sport.key.startswith("pinnacle_", autoescape=True), in_window)
            .distinct()
        )
    ).all()
    return {
        "window_days": days,
        "source": "live",
        "note": (
            "structural = no in-window pinnacle event for the event's league "
            "(co-occurrence map, else exact normalized name); unknown = no "
            "league-identity evidence, kept in the lower-bound denominator"
        ),
        "sports": match_ceiling_blocks(
            [(str(s), int(n)) for s, n in totals],
            [(str(s), int(n)) for s, n in matched],
            [(str(s), int(lid), ln, lc) for s, lid, ln, lc in unmatched],
            [(int(a), int(b)) for a, b in co_rows],
            [(str(s), int(lid), ln, lc) for s, lid, ln, lc in pinn_leagues],
        ),
    }
