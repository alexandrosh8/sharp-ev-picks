"""Ingestion contracts. ALL loaders are READ-ONLY (GET) by design — no code
in this package may write to any bookmaker, exchange, or odds provider."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from app.schemas.odds import OddsSnapshotIn


def is_date_only_midnight(value: datetime | None) -> bool:
    """True for a kickoff that is exactly 00:00:00 UTC — the sentinel a DATE-ONLY
    source parses to when it knows the day but not the time.

    OddsPortal serves a date-only midnight ``eventBody.startDate`` for a residual
    tail of fixtures (Puerto Rico BSN, some WNBA; live-verified 2026-06-24). Such
    a value is "better than NULL" (the dashboard shows the date, not TBD) but it
    must never overwrite a REAL time. A genuine 00:00 UTC kickoff is
    indistinguishable from date-only at the source and vanishingly rare in sport,
    so treating exact-midnight as date-only is the correct, conservative rule.
    None (truly TBD) is NOT midnight — it is handled by the caller's own guard."""
    if value is None:
        return False
    return (
        value.hour == 0
        and value.minute == 0
        and value.second == 0
        and value.microsecond == 0
    )


def prefer_kickoff(existing: datetime | None, incoming: datetime | None) -> datetime | None:
    """Pick the better of an existing vs an incoming kickoff (precedence rule).

    A REAL (non-midnight) time always wins. A date-only midnight never overwrites
    an already-known REAL time, and a None (TBD) never overwrites any known time.
    Otherwise the incoming value is taken (it is at least as good — a real time
    upgrading a midnight, or midnight upgrading None). This single rule is shared
    by the in-memory ``EventDirectory`` and the DB upsert so both overwrite layers
    behave identically."""
    if incoming is None:
        return existing  # TBD never clobbers a known time
    if existing is None:
        return incoming  # first known time (real OR date-only midnight) wins
    if is_date_only_midnight(incoming) and not is_date_only_midnight(existing):
        return existing  # a date-only midnight must not clobber a real time
    return incoming  # real-over-midnight upgrade, or same-quality refresh


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
    # Best-effort final score the loader scraped AFTER the match finished
    # (OddsPortal surfaces it once the game is over), threaded to the event row.
    # The finished-score capture path DOES settle from these (gated by
    # `finished` below) — so an in-play partial must NEVER reach here as a final.
    # None when no score was scraped (pre-kickoff / in-play scrapes).
    home_score: int | None = None
    away_score: int | None = None
    # Explicit OddsPortal finished-status for the scraped score, so capture can
    # trust the page's "Finished" flag instead of only a conservative time-floor:
    #   True  = page reports Finished -> score is FINAL, safe to settle now
    #   False = reported but in-play/scheduled -> reject (never settle a partial)
    #   None  = source gave no status -> caller falls back to the time-floor
    finished: bool | None = None


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
        # Kickoff precedence: every other field is last-write-wins (the freshest
        # scrape carries the latest score/finished flag), but a date-only midnight
        # or a None must NEVER overwrite an already-known REAL kickoff time — only
        # a real time upgrades. Prevents the residual-tail midnight (OddsPortal's
        # date-only basketball header) from clobbering a real time seen earlier.
        existing = self._events.get(event_id)
        if existing is not None:
            kickoff = prefer_kickoff(existing.starts_at, teams.starts_at)
            if kickoff != teams.starts_at:
                teams = replace(teams, starts_at=kickoff)
        self._events[event_id] = teams

    def lookup(self, event_id: str) -> EventTeams | None:
        return self._events.get(event_id)

    def snapshot(self) -> Mapping[str, EventTeams]:
        """Read-only event map for status/API views."""
        return MappingProxyType(self._events)

    def __len__(self) -> int:
        return len(self._events)
