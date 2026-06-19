"""Ingestion contracts. ALL loaders are READ-ONLY (GET) by design — no code
in this package may write to any bookmaker, exchange, or odds provider."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
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
    starts_at: datetime | None = None  # kickoff (UTC) when the source knows it


@dataclass(frozen=True)
class ScraperProxy:
    """One live-scrape outbound proxy. ``url`` is scheme+host:port ONLY; the
    credentials live in ``username``/``password`` and reach Playwright as
    separate fields, never embedded in the URL, so the scraper's INFO log of the
    proxy URL cannot leak them."""

    url: str
    username: str
    password: str


class EventDirectory:
    """Shared event_id -> teams registry, populated by loaders and read by
    models (e.g. Dixon-Coles needs team names, snapshots carry only ids)."""

    def __init__(self) -> None:
        self._events: dict[str, EventTeams] = {}

    def register(self, event_id: str, teams: EventTeams) -> None:
        self._events[event_id] = teams

    def lookup(self, event_id: str) -> EventTeams | None:
        return self._events.get(event_id)

    def snapshot(self) -> Mapping[str, EventTeams]:
        """Read-only event map for status/API views."""
        return MappingProxyType(self._events)

    def __len__(self) -> int:
        return len(self._events)
