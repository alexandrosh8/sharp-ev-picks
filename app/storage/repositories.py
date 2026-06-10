"""Persistence for generated picks — closes the loop so /picks serves real data.

Lean entity resolution (get-or-create sport/league/teams/event/model_version),
then insert the pick. Picks are deduped by their natural key
(event, market, selection, model_version) via ON CONFLICT DO NOTHING, so a
re-poll of the same market state never duplicates rows.
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.base import EventTeams
from app.schemas.picks import PickOut
from app.storage.models import Event, League, ModelVersion, Pick, Sport, Team


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
    starts_at: datetime,
) -> int:
    found = await session.scalar(select(Event.id).where(Event.external_ref == external_ref))
    if found is not None:
        return found
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
        select(ModelVersion.id).where(ModelVersion.name == name, ModelVersion.version == version)
    )
    if found is not None:
        return found
    mv = ModelVersion(name=name, version=version, sport_id=sport_id)
    session.add(mv)
    await session.flush()
    return mv.id


async def persist_pick(
    session: AsyncSession,
    pick: PickOut,
    teams: EventTeams,
    model_name: str,
    model_version: str,
) -> bool:
    """Resolve entities and insert the pick. Returns True if a new row was
    written, False if it already existed (dedupe)."""
    sport_id = await _get_or_create_sport(session, pick.sport, pick.sport.title())
    league_id = await _get_or_create_league(session, sport_id, pick.league)
    home_id = await _get_or_create_team(session, sport_id, league_id, teams.home)
    away_id = await _get_or_create_team(session, sport_id, league_id, teams.away)
    event_id = await _get_or_create_event(
        session, sport_id, league_id, home_id, away_id, pick.event_id, pick.created_at
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
            created_at=datetime.now(tz=UTC),
        )
        .on_conflict_do_nothing(constraint="uq_picks_event_market_selection_model")
        .returning(Pick.id)
    )
    result = await session.execute(stmt)
    inserted = result.scalar_one_or_none()
    return inserted is not None
