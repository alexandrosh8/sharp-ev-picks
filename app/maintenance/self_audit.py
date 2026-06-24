"""Runtime self-audit: cheap READ-ONLY DB invariant checks that surface
operational problems as WARNING/ERROR log lines (caught by the health monitor)
so issues are flagged proactively instead of discovered by hand.

Two checks today, both bounded aggregate queries:
- awaiting_backlog: alerted picks well past kickoff still unsettled (a large pile
  means the finished-score capture or the settle cycle is stuck);
- stale_odds: the newest odds snapshot is too old (odds ingestion may be down).

The threshold logic lives in the PURE `evaluate_anomalies` (unit-tested with no
DB); `run_self_audit` is the thin DB wrapper; `self_audit_job` logs the result
and NEVER raises (a monitoring job must not crash the scheduler).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import Event, OddsSnapshot, Pick

logger = logging.getLogger(__name__)

# The sharp-archive captures (Pinnacle arcadia, Betfair Exchange) ingest on their
# OWN ~60s cadence, independent of the live OddsPortal scrape. They are EXCLUDED
# from the stale-odds freshness check so it stays a true signal of LIVE (soft-book)
# ingestion — otherwise the archive heartbeat would keep MAX(ingested_at) fresh and
# silently mask a dead OddsPortal scrape (code-review finding, pre-merge).
_SHARP_ARCHIVE_BOOKMAKERS = ("Pinnacle", "Betfair Exchange")


@dataclass(frozen=True)
class Anomaly:
    severity: str  # "WARN" | "ERROR"
    code: str
    detail: str


def evaluate_anomalies(
    now: datetime,
    *,
    awaiting_backlog: int,
    newest_odds: datetime | None,
    awaiting_threshold: int = 25,
    awaiting_grace_hours: int = 6,
    stale_odds_after: timedelta = timedelta(hours=3),
) -> list[Anomaly]:
    """Pure anomaly evaluation from the two aggregates the DB wrapper reads.

    Empty list == healthy. Kept pure (no DB / no clock) so it is exhaustively
    unit-tested; the wrapper below feeds it live values."""
    found: list[Anomaly] = []
    if awaiting_backlog > awaiting_threshold:
        found.append(
            Anomaly(
                "WARN",
                "awaiting_backlog",
                f"{awaiting_backlog} alerted picks >{awaiting_grace_hours}h past kickoff "
                "still unsettled — finished-score capture/settle may be stuck",
            )
        )
    if newest_odds is None or newest_odds < now - stale_odds_after:
        age = "none" if newest_odds is None else str(now - newest_odds).split(".")[0]
        found.append(
            Anomaly(
                "ERROR",
                "stale_odds",
                f"newest odds-snapshot age {age} exceeds {stale_odds_after} "
                "— odds ingestion may be down",
            )
        )
    return found


async def run_self_audit(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    *,
    awaiting_grace: timedelta = timedelta(hours=6),
) -> list[Anomaly]:
    """READ-ONLY DB self-audit -> anomalies (empty == healthy)."""
    now = now or datetime.now(tz=UTC)
    async with session_factory() as session:
        backlog = (
            await session.scalar(
                select(func.count(Pick.id))
                .join(Event, Pick.event_id == Event.id)
                .where(
                    Pick.status == "alerted",
                    Event.starts_at.is_not(None),
                    Event.starts_at < now - awaiting_grace,
                )
            )
        ) or 0
        newest = await session.scalar(
            select(func.max(OddsSnapshot.ingested_at)).where(
                OddsSnapshot.bookmaker.notin_(_SHARP_ARCHIVE_BOOKMAKERS)
            )
        )
    found = evaluate_anomalies(
        now,
        awaiting_backlog=backlog,
        newest_odds=newest,
        awaiting_grace_hours=int(awaiting_grace.total_seconds() // 3600),
    )
    # WRONG-GAME SAFETY NET (go-live, hardened Pinnacle matcher): independently
    # re-verify recently-accepted live Pinnacle anchors are the SAME fixture. A
    # wrong-game close is fake CLV — the cardinal sin — so any mismatch surfaces
    # here as an ERROR through the same monitor channel. Read-only; imported lazily
    # to keep the resolution import out of the hot self-audit path.
    from app.maintenance.wrong_game_audit import audit_live_pinnacle_anchors

    found.extend(await audit_live_pinnacle_anchors(session_factory, now))
    return found


async def self_audit_job(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> int:
    """Run the self-audit and emit one WARNING/ERROR per anomaly (so the health
    monitor catches them), or one INFO when clean. NEVER raises — a monitoring
    job must not crash the scheduler. Returns the anomaly count (-1 on failure)."""
    try:
        anomalies = await run_self_audit(session_factory, now)
    except Exception as exc:  # a monitoring job must never take the scheduler down
        logger.error("self_audit failed: %s", type(exc).__name__)
        return -1
    for anomaly in anomalies:
        emit = logger.error if anomaly.severity == "ERROR" else logger.warning
        emit("self_audit %s: %s", anomaly.code, anomaly.detail)
    if not anomalies:
        logger.info("self_audit: ok — no anomalies")
    return len(anomalies)
