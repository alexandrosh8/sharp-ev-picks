"""Persistence for generated picks — closes the loop so /picks serves real data.

Lean entity resolution (get-or-create sport/league/teams/event/model_version),
then insert the pick. Picks are deduped by their natural key
(event, market, selection, model_version) via ON CONFLICT DO NOTHING, so a
re-poll of the same market state never duplicates rows.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut
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

    from app.backtesting.live_evidence import SettledPickRow
    from app.resolution.shadow import ShadowOutcome

logger = logging.getLogger(__name__)


async def _get_or_create_sport(session: AsyncSession, key: str, name: str) -> int:
    found = await session.scalar(select(Sport.id).where(Sport.key == key))
    if found is not None:
        return found
    sport = Sport(key=key, name=name)
    session.add(sport)
    await session.flush()
    return sport.id


async def _get_or_create_league(session: AsyncSession, sport_id: int, key: str) -> int:
    found = await session.scalar(
        select(League.id).where(League.sport_id == sport_id, League.key == key)
    )
    if found is not None:
        return found
    league = League(sport_id=sport_id, key=key, name=key)
    session.add(league)
    await session.flush()
    return league.id


async def _get_or_create_team(
    session: AsyncSession, sport_id: int, league_id: int, name: str
) -> int:
    normalized = name.strip().lower()
    found = await session.scalar(
        select(Team.id).where(Team.sport_id == sport_id, Team.normalized_name == normalized)
    )
    if found is not None:
        return found
    team = Team(sport_id=sport_id, league_id=league_id, name=name, normalized_name=normalized)
    session.add(team)
    await session.flush()
    return team.id


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
    (the dashboard's "TBD" signal), never as a pick-time placeholder."""
    existing = await session.scalar(select(Event).where(Event.external_ref == external_ref))
    if existing is not None:
        # Earlier rows may be NULL (or carry a legacy placeholder); a real
        # kickoff from the source upgrades them to the true start.
        if starts_at is not None and existing.starts_at != starts_at:
            existing.starts_at = starts_at
            await session.flush()
        return existing.id
    event = Event(
        sport_id=sport_id,
        league_id=league_id,
        home_team_id=home_id,
        away_team_id=away_id,
        external_ref=external_ref,
        starts_at=starts_at,
    )
    session.add(event)
    await session.flush()
    return event.id


async def _get_or_create_model_version(
    session: AsyncSession, sport_id: int, name: str, version: str
) -> int:
    found = await session.scalar(
        select(ModelVersion.id).where(
            ModelVersion.sport_id == sport_id,
            ModelVersion.name == name,
            ModelVersion.version == version,
        )
    )
    if found is not None:
        return found
    mv = ModelVersion(name=name, version=version, sport_id=sport_id)
    session.add(mv)
    await session.flush()
    return mv.id


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
        )
        .join(Event, Pick.event_id == Event.id)
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
        }
        for p, home_name, away_name, league_name, starts_at, outcome, pnl in rows.all()
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
        .where((Event.starts_at >= event_cutoff) | (OddsSnapshot.ingested_at >= recent_odds_cutoff))
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
        target = kickoffs[event.external_ref]
        if event.starts_at != target:
            event.starts_at = target
            changed += 1
    if changed:
        await session.flush()
    return changed


def _aggregate_settled(rows: Sequence[Any]) -> dict[str, Any]:
    """Aggregate (outcome, pnl, stake, clv_log, beat_close) rows into the
    report fields. Decimals serialize as strings; undefined ratios are None."""
    counts = {"won": 0, "lost": 0, "void": 0, "push": 0, "half_won": 0, "half_lost": 0}
    total_staked = Decimal("0")
    total_pnl = Decimal("0")
    clv_weighted = Decimal("0")
    clv_stake = Decimal("0")
    beat_known = beat_true = 0
    for outcome, pnl, stake, clv_log, beat_close in rows:
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
    return {
        "n_settled": len(rows),
        **counts,
        "total_staked": str(total_staked),
        "total_pnl": str(total_pnl),
        "roi": _ratio(total_pnl, total_staked),
        "stake_weighted_clv_log": _ratio(clv_weighted, clv_stake),
        "beat_close_rate": _ratio(Decimal(beat_true), Decimal(beat_known)),
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
    rows = (
        await session.execute(
            select(
                ResultTracking.outcome,
                ResultTracking.pnl,
                Pick.recommended_stake_amount,
                Pick.clv_log,
                Pick.beat_close,
                Pick.tier,
            ).join(Pick, ResultTracking.pick_id == Pick.id)
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

    premium = _aggregate_settled([tuple(r)[:5] for r in rows if r[5] == "premium"])
    volume = _aggregate_settled([tuple(r)[:5] for r in rows if r[5] == "volume"])
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
    columns = [
        Pick.tier,
        Pick.value_filter_score,
        Pick.clv_log,
        Pick.beat_close,
        Pick.recommended_stake_amount,
        ResultTracking.pnl,
    ]
    if anchor_attr is not None:
        columns.append(anchor_attr)
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
            anchor_type=row[6] if anchor_attr is not None else None,
        )
        for row in rows
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
    selection vocabulary (bookmaker stays "Pinnacle").

    Returns [] when there is no UNAMBIGUOUS match or no Pinnacle coverage — a
    wrong close corrupts CLV, so this never guesses. Matching is the pure
    app.resolution matcher (exact normalized names + alias table + a small
    kickoff window; NO fuzzy). Selections that cannot be mapped to the pick's
    home/away/Draw outcome are dropped rather than mis-attached.
    """
    from app.resolution import EventCandidate, default_aliases, match_event, normalize_name

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
        EventCandidate(ref=str(eid), home=h, away=a, kickoff=ko) for eid, _ext, h, a, ko in rows
    ]
    matched = match_event(
        home, away, kickoff, candidates, aliases=default_aliases(), max_day_drift=max_day_drift
    )
    if matched is None:
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
    from app.resolution import EventCandidate, default_aliases, match_event
    from app.resolution.shadow import ShadowOutcome, arcadia_base_sport

    home_t, away_t = aliased(Team), aliased(Team)
    conds: list[Any] = [Event.starts_at.is_not(None)]
    if since is not None:
        conds.append(Event.starts_at >= since)
    pick_rows = (
        await session.execute(
            select(Pick.id, Sport.key, League.key, home_t.name, away_t.name, Event.starts_at)
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
        arc_rows = (
            await session.execute(
                select(arc_home.name, arc_away.name, Event.starts_at)
                .join(Sport, Event.sport_id == Sport.id)
                .join(arc_home, Event.home_team_id == arc_home.id)
                .join(arc_away, Event.away_team_id == arc_away.id)
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
            for i, (h, a, ko) in enumerate(arc_rows)
        ]
        for pick_id, sport_key, league_key, home, away, kickoff in picks:
            # Same day window the matcher uses internally — count first so a
            # no-coverage pick is distinguishable from a strict-rejection.
            in_window = [
                c for c in archive if abs((c.kickoff.date() - kickoff.date()).days) <= max_day_drift
            ]
            matched = (
                match_event(
                    home, away, kickoff, in_window, aliases=aliases, max_day_drift=max_day_drift
                )
                is not None
            )
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
