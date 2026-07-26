"""Proactive Pinnacle slate linkage — pre-close ``event_source_links`` minting.

Arcadia capture lands sharp rows on shadow ``pinnacle_<sport>`` events, but the
demand path (``app.clv_trueup._pinnacle_archive_close`` ->
``repositories.resolve_pinnacle_close_snaps``) mints the cross-source
``event_source_links`` row only at CLV true-up/close time — so TODAY'S slate
shows "Pinnacle 0%" on the dashboard Sources panel all day even while capture
is healthy. This pass walks the upcoming soft-priced canonical slate and runs
the SAME strict resolution the demand path uses (identical matcher, marker/
ambiguity/kickoff guards, and link persistence — it *is*
``resolve_pinnacle_close_snaps``), so links exist pre-close and the demand
path later finds them pre-linked.

NO matching logic lives here: this module only selects WHICH canonical events
to try (upcoming, soft-priced, arcadia-covered sport, not already actively
linked) and delegates. Wrong-game safety is therefore byte-identical to the
close-resolution path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.resolution.shadow import arcadia_base_sport

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

import logging

logger = logging.getLogger(__name__)

#: Per-pass cap on resolver calls — the pass re-runs every ~15 min, so a huge
#: slate simply finishes over a few cycles instead of hogging one.
_MAX_ATTEMPTS_PER_PASS = 400

#: Bookmakers whose presence does NOT make an event part of the soft slate —
#: mirrors the ``soft`` CTE in ``repositories.sharp_slate_coverage`` exactly.
_NON_SOFT_BOOKS = ("betfair exchange", "smarkets", "consensus(median)")


@dataclass(frozen=True)
class SlateLinkageReport:
    """One pass's outcome: ``attempted`` resolver calls, ``linked`` accepted
    matches (link minted by the demand-path resolver), ``already_linked``
    slate events skipped because an active link already exists."""

    attempted: int
    linked: int
    already_linked: int


async def link_upcoming_slate(
    session: AsyncSession,
    *,
    horizon: timedelta = timedelta(hours=48),
    now: datetime | None = None,
) -> SlateLinkageReport:
    """Attempt strict Pinnacle linkage for the upcoming soft-priced canonical
    slate. Delegates each attempt to ``resolve_pinnacle_close_snaps`` — the
    demand path — so acceptance criteria and the persisted link row are
    identical to close-time resolution. The caller owns the COMMIT."""
    from sqlalchemy import func

    from app.storage.models import Event, EventSourceLink, OddsSnapshot, Sport, Team
    from app.storage.repositories import resolve_pinnacle_close_snaps

    now = now if now is not None else datetime.now(tz=UTC)
    # Arcadia namespaces that actually hold events — a sport without capture is
    # skipped outright (nothing to link against).
    namespace_rows = (
        (
            await session.execute(
                select(Sport.key)
                .join(Event, Event.sport_id == Sport.id)
                .where(Sport.key.startswith("pinnacle_", autoescape=True))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    namespaces = set(namespace_rows)
    if not namespaces:
        return SlateLinkageReport(attempted=0, linked=0, already_linked=0)

    home_t, away_t = aliased(Team), aliased(Team)
    # Soft-slate membership — mirrors the `soft` CTE in
    # repositories.sharp_slate_coverage (any capture age; the metric windows,
    # the linkage pass does not need to).
    bk = func.lower(OddsSnapshot.bookmaker)
    soft_exists = (
        select(OddsSnapshot.id)
        .where(
            OddsSnapshot.event_id == Event.id,
            ~bk.like("pinnacle%"),
            bk.not_in(_NON_SOFT_BOOKS),
        )
        .exists()
    )
    linked_exists = (
        select(EventSourceLink.id)
        .where(
            EventSourceLink.canonical_event_id == Event.id,
            EventSourceLink.source == "pinnacle_arcadia",
            EventSourceLink.active.is_(True),
        )
        .exists()
    )
    rows = (
        await session.execute(
            select(
                Event.external_ref,
                Sport.key,
                home_t.name,
                away_t.name,
                Event.starts_at,
                linked_exists.label("already_linked"),
            )
            .join(Sport, Event.sport_id == Sport.id)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .where(
                ~Sport.key.startswith("pinnacle_", autoescape=True),
                Event.starts_at.is_not(None),
                Event.starts_at >= now,
                Event.starts_at <= now + horizon,
                soft_exists,
            )
            .order_by(Event.starts_at)
        )
    ).all()

    attempted = 0
    linked = 0
    already_linked = 0
    for external_ref, sport_key, home, away, kickoff, is_linked in rows:
        pinnacle_sport_key = f"pinnacle_{arcadia_base_sport(sport_key)}"
        if pinnacle_sport_key not in namespaces:
            continue  # no arcadia capture for this sport
        if is_linked:
            already_linked += 1
            continue  # demand path will refresh it; nothing to mint
        if attempted >= _MAX_ATTEMPTS_PER_PASS:
            break
        attempted += 1
        provenance: dict[str, tuple[float, str]] = {}
        # The demand-path resolver: SAME matcher + guards; on acceptance it
        # persists the event_source_links row itself (savepoint-wrapped) and
        # fills provenance_out. The returned close snaps are irrelevant here.
        await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key=pinnacle_sport_key,
            pick_external_ref=external_ref,
            home=home,
            away=away,
            kickoff=kickoff,
            provenance_out=provenance,
        )
        if external_ref in provenance:
            linked += 1
            confidence, method = provenance[external_ref]
            logger.info(
                "pinnacle slate linkage: %s v %s (%s) linked pre-close "
                "(confidence=%.4f, method=%s)",
                home,
                away,
                sport_key,
                confidence,
                method,
            )
    return SlateLinkageReport(attempted=attempted, linked=linked, already_linked=already_linked)
