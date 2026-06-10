"""Ingestion contracts. ALL loaders are READ-ONLY (GET) by design — no code
in this package may write to any bookmaker, exchange, or odds provider."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.schemas.odds import OddsSnapshotIn


class OddsLoader(Protocol):
    """A source of odds snapshots for one sport key."""

    async def fetch_odds(self, sport_key: str) -> Sequence[OddsSnapshotIn]: ...


@dataclass(frozen=True)
class EventTeams:
    """Team context for an event — the seam between loaders and models."""

    home: str
    away: str
    league: str = ""


class EventDirectory:
    """Shared event_id -> teams registry, populated by loaders and read by
    models (e.g. Dixon-Coles needs team names, snapshots carry only ids)."""

    def __init__(self) -> None:
        self._events: dict[str, EventTeams] = {}

    def register(self, event_id: str, teams: EventTeams) -> None:
        self._events[event_id] = teams

    def lookup(self, event_id: str) -> EventTeams | None:
        return self._events.get(event_id)

    def __len__(self) -> int:
        return len(self._events)
