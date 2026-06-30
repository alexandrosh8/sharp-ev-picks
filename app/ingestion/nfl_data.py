"""Read-only NFL data spine via nflreadpy (nflverse, MIT).

GET-only: loads nflverse's pre-built parquet releases — schedules with final
scores and CONSENSUS betting lines (spread/total/moneyline, maintained by Lee
Sharpe/PFR). This module NEVER places bets and is NEVER an order venue.

Doctrine (research 2026-06-21, ADR-0017): NFL is SCREEN / visibility-only — the
nflverse line is a single CONSENSUS close, NOT a sharp Pinnacle anchor, so it
cannot measure incremental CLV. NFL picks stay un-staked until forward held-out
CLV (> 2 SE) clears against the project's OWN sharp capture. These games feed
visibility + a future model screen only.

nflreadpy is an OPTIONAL dependency (the `nfl` extra). The loader is INJECTED so
tests run with synthetic rows and no network; `_default_loader` pulls the parquet
in a worker thread and crosses the polars->pandas boundary with `.to_pandas()`.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import field_validator

from app.schemas.base import InternalModel, to_utc

# nflverse gametime is US Eastern wall-clock; convert to UTC DST-aware (IANA).
_ET = ZoneInfo("America/New_York")


class NflGame(InternalModel):
    """One NFL game: UTC kickoff, final score (None until played), and the
    nflverse CONSENSUS lines (home-perspective spread, total, moneylines).
    Frozen, extra=forbid — these are NOT a sharp anchor."""

    game_id: str
    season: int
    week: int
    kickoff_utc: datetime
    home_team: str
    away_team: str
    home_score: int | None = None
    away_score: int | None = None
    spread_line: float | None = None  # home perspective (consensus close)
    total_line: float | None = None
    home_moneyline: int | None = None
    away_moneyline: int | None = None

    _utc_kickoff = field_validator("kickoff_utc")(to_utc)

    @property
    def is_final(self) -> bool:
        return self.home_score is not None and self.away_score is not None


def _opt_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None  # one bad cell -> skip THIS field, never abort the season
    return None if math.isnan(f) else int(f)


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None  # one bad cell -> skip THIS field, never abort the season
    return None if math.isnan(f) else f


def _kickoff_utc(gameday: Any, gametime: Any) -> datetime | None:
    """Combine nflverse `gameday` (YYYY-MM-DD) + `gametime` (HH:MM, ET) into a
    UTC instant, or None when either is missing/unparseable — never invent a
    kickoff (UTC discipline)."""
    if not gameday or not gametime:
        return None
    try:
        local = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None
    return local.replace(tzinfo=_ET).astimezone(UTC)


def parse_nfl_games(rows: Sequence[Mapping[str, Any]]) -> list[NflGame]:
    """Map nflverse schedule rows to NflGame, skipping rows without a resolvable
    kickoff. Pure: no IO, no network — the unit-testable core of the spine."""
    games: list[NflGame] = []
    for r in rows:
        kickoff = _kickoff_utc(r.get("gameday"), r.get("gametime"))
        if kickoff is None:
            continue
        games.append(
            NflGame(
                game_id=str(r["game_id"]),
                season=int(r["season"]),
                week=int(r["week"]),
                kickoff_utc=kickoff,
                home_team=str(r["home_team"]),
                away_team=str(r["away_team"]),
                home_score=_opt_int(r.get("home_score")),
                away_score=_opt_int(r.get("away_score")),
                spread_line=_opt_float(r.get("spread_line")),
                total_line=_opt_float(r.get("total_line")),
                home_moneyline=_opt_int(r.get("home_moneyline")),
                away_moneyline=_opt_int(r.get("away_moneyline")),
            )
        )
    return games


NflLoader = Callable[[int], Awaitable[Sequence[Mapping[str, Any]]]]


async def load_nfl_games(season: int, *, loader: NflLoader | None = None) -> list[NflGame]:
    """Read-only: load + parse one season's NFL schedule (scores + consensus
    lines). GET-only — NEVER an order venue. The default loader pulls the
    nflverse parquet via nflreadpy in a worker thread (`uv sync --extra nfl`)."""
    load = loader or _default_loader
    rows = await load(season)
    return parse_nfl_games(rows)


async def _default_loader(season: int) -> Sequence[Mapping[str, Any]]:
    import asyncio

    return await asyncio.to_thread(_load_schedule_sync, season)


def _load_schedule_sync(season: int) -> list[Mapping[str, Any]]:
    try:
        import nflreadpy as nfl
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError("nflreadpy not installed; run `uv sync --extra nfl`") from exc
    # polars -> pandas at the boundary (research 2026-06-21), then plain records.
    frame = nfl.load_schedules(seasons=[season]).to_pandas()
    records: list[Mapping[str, Any]] = frame.to_dict("records")
    return records
