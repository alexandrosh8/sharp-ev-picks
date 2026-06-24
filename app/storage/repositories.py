"""Persistence for generated picks — closes the loop so /picks serves real data.

Lean entity resolution (get-or-create sport/league/teams/event/model_version),
then insert the pick. Picks are deduped by their natural key
(event, market, selection, model_version) via ON CONFLICT DO NOTHING, so a
re-poll of the same market state never duplicates rows.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.ingestion.base import EventTeams, prefer_kickoff
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut
from app.settlement.outcomes import provisional_result
from app.storage.models import (
    DashboardCredential,
    Event,
    League,
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


async def select_betfair_targets(
    session_factory: "async_sessionmaker",
    *,
    sport: str,
    now: datetime | None = None,
    window: timedelta = timedelta(days=3),
    limit: int = 20,
) -> list[BetfairTarget]:
    """Bounded, rotating list of canonical ``sport`` events for the Betfair
    Exchange capture to read THIS cycle — read-only (a single SELECT).

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
    stmt = (
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
        # never-captured first (NULLS FIRST), then stalest capture, then soonest
        # kickoff, then ref — a total, deterministic rotation order.
        .order_by(
            last_betfair.asc().nulls_first(),
            Event.starts_at.asc(),
            Event.external_ref.asc(),
        )
        .limit(limit)
    )
    async with session_factory() as session:
        rows = (await session.execute(stmt)).all()
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


async def _get_or_create_league(session: AsyncSession, sport_id: int, key: str) -> int:
    where = (League.sport_id == sport_id, League.key == key)
    found = await session.scalar(select(League.id).where(*where))
    if found is not None:
        return found
    await session.execute(
        pg_insert(League)
        .values(sport_id=sport_id, key=key, name=key)
        .on_conflict_do_nothing(constraint="uq_leagues_sport_key")
    )
    found = await session.scalar(select(League.id).where(*where))
    if found is None:  # pragma: no cover
        raise RuntimeError(f"could not resolve league {key!r}")
    return found


async def _get_or_create_team(
    session: AsyncSession, sport_id: int, league_id: int, name: str
) -> int:
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


async def latest_picks_with_events(
    session: AsyncSession, limit: int = 50, tier: str | None = None, min_edge: float | None = None
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
    VALUE-strategy semantics: `model_probability` holds the devigged sharp
    fair probability on value picks (the deployed strategy — the dashboard
    documents the same caveat for its Fair column)."""
    from app.edge.value import ceil_odds, min_acceptable_odds

    def _min_acceptable(p: Pick) -> str | None:
        if min_edge is None:
            return None
        fair = float(p.model_probability)
        if not 0.0 < fair < 1.0:
            return None  # degenerate stored prob: no honest floor exists
        floor = min_acceptable_odds(fair, min_edge, book=p.bookmaker)
        return f"{ceil_odds(floor):.2f}" if floor is not None else None

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
            # sport of the pick (soccer/basketball/tennis/american_football) +
            # human label, so the multi-sport picks table can badge each row and
            # tag UNVALIDATED (experimental) sports honestly.
            "sport": sport_key,
            "sport_label": _sport_label(sport_key, sport_key),
            # CLOSE-anchor provenance (ADR-0017): which anchor priced the close
            # (pinnacle/sharp/consensus). With closing_odds set it marks a
            # genuine sharp close vs a consensus/fallback one.
            "closing_anchor_type": p.closing_anchor_type,
            "created_at": p.created_at.isoformat(),
            "clv_log": str(p.clv_log) if p.clv_log is not None else None,
            "beat_close": p.beat_close,
            "current_odds": str(p.current_odds) if p.current_odds is not None else None,
            "current_edge": str(p.current_edge) if p.current_edge is not None else None,
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
# picks. Everything else (e.g. tennis) is VISIBILITY-ONLY / UNVALIDATED and the
# dashboard badges it. Mirrors app/pipeline.visibility_only_sports (the runtime
# set), but is the warehouse-path source of truth: the restart-durability query
# has no access to the in-memory pipeline registry.
_VALIDATED_SPORT_PREFIXES = ("soccer", "basketball")


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


def _aggregate_settled(rows: Sequence[Any]) -> dict[str, Any]:
    """Aggregate (outcome, pnl, stake, clv_log, beat_close, closing_odds,
    closing_anchor_type) rows into the report fields. Decimals serialize as
    strings; undefined ratios are None.

    A TRUSTED sharp-close subset (``sharp_*``) is reported ALONGSIDE the blended
    headline: a close counts only when it is snapshot-sourced (closing_odds NOT
    NULL — not a poll-time revalidation fallback), anchored by a named sharp
    book (closing_anchor_type in pinnacle/sharp — not a soft-book consensus
    median), AND independent of the fill (close_independent_of_fill is not False —
    the close was NOT anchored by the pick's own fill book; a self-priced close
    is CIRCULAR fake CLV, closing == fill, |clv_log|~0, and is what masked the
    -EV). Those are the closes whose CLV the platform can stand behind; the
    blended ``stake_weighted_clv_log`` still mixes every close in for continuity.

    Each row is (outcome, pnl, stake, clv_log, beat_close, closing_odds,
    closing_anchor, close_independent). ``close_independent`` is None when the
    column is feature-detected absent (pre-column rows) — treated as "unknown,
    NOT circular" so historical sharp closes keep their status.
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
    for (
        outcome,
        pnl,
        stake,
        clv_log,
        beat_close,
        closing_odds,
        closing_anchor,
        close_independent,
    ) in rows:
        if outcome in counts:
            counts[outcome] += 1
        total_staked += stake
        total_pnl += pnl if pnl is not None else Decimal("0")
        if clv_log is not None:
            clv_weighted += stake * clv_log
            clv_stake += stake
        if beat_close is not None:
            beat_known += 1
            beat_true += int(beat_close)
        if (
            closing_odds is not None
            and closing_anchor in _SHARP_CLOSE_ANCHORS
            and clv_log is not None
            # INDEPENDENCE guard (P0-1/P0-3): a close anchored by the pick's OWN
            # fill book is CIRCULAR (closing == fill, |clv_log|~0) — fake CLV that
            # masked the -EV. Only a definite False excludes; None (pre-column /
            # unknown) is NOT treated as circular, preserving historical rows.
            and close_independent is not False
        ):
            # Genuine, INDEPENDENT sharp snapshot close with a measured CLV — the
            # trusted subset.
            n_sharp += 1
            sharp_clv_weighted += stake * clv_log
            sharp_clv_stake += stake
            sharp_all_independent = sharp_all_independent and close_independent is not False
            if beat_close is not None:
                sharp_beat_known += 1
                sharp_beat_true += int(beat_close)
    # Defense-in-depth: the gate above already excludes circular closes, so by
    # construction every row in the sharp subset is independent of its fill book
    # (closing_anchor != fill_book). Assert it so a future refactor of the gate
    # that re-admits a self-priced close trips here instead of silently faking CLV.
    assert sharp_all_independent, "sharp-close subset contains a circular (self-priced) close"
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
        "min_headline_n": MIN_HEADLINE_N,
        # TRUSTED subset — genuine sharp snapshot closes only (see docstring) —
        # gated on its own n (n_sharp_close), naturally thinner than n_settled.
        "n_sharp_close": n_sharp,
        "sharp_status": "ok" if sharp_ok else "insufficient",
        "sharp_stake_weighted_clv_log": (
            _ratio(sharp_clv_weighted, sharp_clv_stake) if sharp_ok else None
        ),
        "sharp_beat_close_rate": (
            _ratio(Decimal(sharp_beat_true), Decimal(sharp_beat_known)) if sharp_ok else None
        ),
    }


async def performance_report(session: AsyncSession) -> dict[str, Any]:
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
    select_cols: list[Any] = [
        ResultTracking.outcome,  # 0
        ResultTracking.pnl,  # 1
        Pick.recommended_stake_amount,  # 2
        Pick.clv_log,  # 3
        Pick.beat_close,  # 4
        Pick.tier,  # 5 — split key, not passed to _aggregate_settled
        Pick.closing_odds,  # 6 — snapshot-close marker
    ]
    close_anchor_idx = indep_idx = None
    if close_anchor_attr is not None:
        close_anchor_idx = len(select_cols)
        select_cols.append(close_anchor_attr)  # 7
    if indep_attr is not None:
        indep_idx = len(select_cols)
        select_cols.append(indep_attr)  # 8 — INDEPENDENCE provenance (P0-1/P0-3)
    rows = (
        await session.execute(select(*select_cols).join(Pick, ResultTracking.pick_id == Pick.id))
    ).all()
    pending_by_tier: dict[str, int] = {
        tier: int(n)
        for tier, n in (
            await session.execute(
                select(Pick.tier, func.count()).where(Pick.status == "alerted").group_by(Pick.tier)
            )
        ).all()
    }

    def _tier_rows(tier_name: str) -> list[tuple[Any, ...]]:
        # (outcome, pnl, stake, clv_log, beat_close, closing_odds, closing_anchor,
        #  close_independent) — close_independent is None when feature-detected
        # absent (pre-column), which the sharp gate treats as "unknown, NOT
        # circular".
        out: list[tuple[Any, ...]] = []
        for r in rows:
            if r[5] != tier_name:
                continue
            closing_anchor = r[close_anchor_idx] if close_anchor_idx is not None else None
            close_independent = r[indep_idx] if indep_idx is not None else None
            out.append((r[0], r[1], r[2], r[3], r[4], r[6], closing_anchor, close_independent))
        return out

    premium = _aggregate_settled(_tier_rows("premium"))
    volume = _aggregate_settled(_tier_rows("volume"))
    volume["n_pending"] = pending_by_tier.get("volume", 0)
    return {
        **premium,
        "n_pending": pending_by_tier.get("premium", 0),
        "tier_scope": "premium",
        "volume": volume,
    }


def _ratio(numerator: Decimal, denominator: Decimal) -> str | None:
    """Exact ratio without Decimal trailing-zero noise; None when undefined."""
    if not denominator:
        return None
    return format((numerator / denominator).normalize(), "f")


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
    columns = [
        Pick.tier,  # 0
        Pick.value_filter_score,  # 1
        Pick.clv_log,  # 2
        Pick.beat_close,  # 3
        Pick.recommended_stake_amount,  # 4
        ResultTracking.pnl,  # 5
        Pick.closing_odds,  # 6 — snapshot-close marker (NON-NULL = a true close)
    ]
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
    rows = (
        await session.execute(select(*columns).join(Pick, ResultTracking.pick_id == Pick.id))
    ).all()
    return [
        SettledPickRow(
            tier=row[0],
            value_filter_score=float(row[1]) if row[1] is not None else None,
            clv_log=float(row[2]) if row[2] is not None else None,
            beat_close=row[3],
            stake=float(row[4]),
            pnl=float(row[5]) if row[5] is not None else None,
            anchor_type=row[anchor_idx] if anchor_idx is not None else None,
            closing_anchor_type=row[close_anchor_idx] if close_anchor_idx is not None else None,
            # closing_odds NON-NULL marks a genuine snapshot close (not a
            # poll-time revalidation fallback) — the SOURCE half of "trusted".
            has_snapshot_close=row[6] is not None,
            # INDEPENDENCE half (P0-1/P0-3): False = circular self-priced close
            # (excluded from sharp subset); None = unknown (pre-column, NOT
            # treated as circular).
            close_independent_of_fill=row[indep_idx] if indep_idx is not None else None,
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
    a fake multi-leg book. Clamped to the column's 32 chars (the longest
    configured key, asian_handicap_games_-10_5_games, is exactly 32)."""
    return (snapshot.market_detail or str(snapshot.market))[:32]


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
    if market is None:
        return None
    return market, key


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
                        session, sport_id, teams.league or default_league
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
    captured at-or-before kickoff, rebuilt as OddsSnapshotIn (keyed by the
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
                    OddsSnapshot.captured_at <= kickoff,
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


async def resolve_pinnacle_close_snaps(
    session: AsyncSession,
    *,
    pinnacle_sport_key: str,
    pick_external_ref: str,
    home: str,
    away: str,
    kickoff: datetime,
    max_day_drift: int = 1,
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
        default_aliases,
        distinguishing_markers,
        match_event_hardened,
        normalize_name,
        oddsportal_slug_names,
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
    matched = match_event_hardened(
        qhome,
        qaway,
        kickoff,
        candidates,
        aliases=aliases,
        ordered=not is_tennis,
        league=None,  # cross-source league taxonomies are incomparable here
        candidate_leagues=None,
        max_minute_drift=minute_drift,
    )
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
        slug = oddsportal_slug_names(pick_external_ref)
        if slug is not None:
            sh = canonical_tennis_name(slug[0]) if is_tennis else slug[0]
            sa = canonical_tennis_name(slug[1]) if is_tennis else slug[1]
            display_markers = distinguishing_markers(home) | distinguishing_markers(away)
            slug_markers = distinguishing_markers(sh) | distinguishing_markers(sa)
            if display_markers <= slug_markers:
                matched = match_event_hardened(
                    sh,
                    sa,
                    kickoff,
                    candidates,
                    aliases=aliases,
                    ordered=not is_tennis,
                    league=None,  # cross-source league taxonomies are incomparable here
                    candidate_leagues=None,
                    max_minute_drift=minute_drift,
                )
    if matched is None:
        return []
    # tennis: require a shared normalized token between the pick and the matched
    # arcadia event, so a degenerate surname+initial pair can't attach same-day
    # noise (the readiness-probe collision guard, audit #7).
    if is_tennis and not (
        (_toks(home) | _toks(away)) & (_toks(matched.home) | _toks(matched.away))
    ):
        return []
    pin_id, pin_ref, pin_home, pin_away, pin_kickoff = by_ref[matched.ref]
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
    # Re-key arcadia selections (its own team names / "Draw") to the pick's
    # selection vocabulary BY OUTCOME so the close groups with the pick's market.
    selection_map = {normalize_name(pin_home): home, normalize_name(pin_away): away}
    out: list[OddsSnapshotIn] = []
    for snap in snaps:
        if snap.selection == "Draw":
            mapped_selection: str | None = "Draw"
        else:
            mapped_selection = selection_map.get(normalize_name(snap.selection))
        if mapped_selection is None:
            continue  # a selection we cannot confidently map -> drop (safe)
        out.append(
            snap.model_copy(update={"event_id": pick_external_ref, "selection": mapped_selection})
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
        match_event,
        match_event_hardened,
        oddsportal_slug_names,
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
                # OddsPortal slug fallback (drops the women "W" suffix + sponsor
                # tails) — same strict unique match, just a cleaner key.
                slug = oddsportal_slug_names(ext_ref)
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
    (app.clv_trueup._betfair_exchange_close) would resolve — an EXACT lookup of
    the ``"betfair:"+ref`` event (external_ref is globally unique, no fuzz/alias)
    — and whether that event carries a USABLE BACK close: an anchorable H2H close
    set whose event-wide last pre-kickoff capture is within SNAPSHOT_CLOSE_MAX_GAP
    of kickoff (the same gate finalize_closing_from_snapshots applies). Writes
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
            select(Pick.id, Sport.key, League.key, Event.external_ref, Event.starts_at)
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
    for pick_id, sport_key, league_key, external_ref, kickoff in pick_rows:
        betfair_ref = f"betfair:{external_ref}"
        betfair_event_id = await session.scalar(
            select(Event.id).where(Event.external_ref == betfair_ref)
        )
        has_event = betfair_event_id is not None
        has_close = False
        if betfair_event_id is not None and kickoff is not None:
            snaps, last_capture = await closing_odds_from_snapshots(
                session, betfair_event_id, betfair_ref, kickoff
            )
            # USABLE = event scraped near kickoff (coverage gate) AND the close
            # set has the FULL H2H width for the sport (soccer 3-way home/draw/away,
            # basketball 2-way home/away) — the same two conditions the consumption
            # path requires to attach a fair.
            in_window = (
                last_capture is not None and kickoff - last_capture <= SNAPSHOT_CLOSE_MAX_GAP
            )
            h2h_rows = sum(1 for s in snaps if s.market is Market.H2H)
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
                        select(OddsSnapshot.id)
                        .where(OddsSnapshot.event_id == Event.id)
                        .exists(),
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
                        OddsSnapshot.market == moneyline_key,
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


PickPersistOutcome = Literal["inserted", "upgraded", "duplicate"]


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

    `status` stays "alerted" for both tiers: it is the lifecycle column
    (open -> settled/superseded/void) shared by revalidation and settlement;
    `tier` alone scopes alerting, exposure, and reporting.
    """
    sport_id = await _get_or_create_sport(session, pick.sport, pick.sport.title())
    league_id = await _get_or_create_league(session, sport_id, pick.league)
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
        existing.current_odds = None
        existing.current_edge = None
        existing.current_bookmaker = None
        existing.revalidated_at = None
        await session.flush()
        await _supersede_older_versions(
            session, event_id, str(pick.market), pick.selection, model_version_id, "premium"
        )
        return "upgraded"
    return "duplicate"


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
