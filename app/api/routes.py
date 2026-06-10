"""API routes: latest picks, manual result tracking, health.

POST /picks/{id}/result is the MANUAL result-tracking entrypoint — the user
records what THEY did (bet placed or not, stake, outcome). Nothing here can
place a bet.
"""

import logging
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.schemas.events import ResultIn
from app.storage.models import ManualBetLog, Pick, ResultTracking

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "picks-only"}


@router.get("/picks")
async def latest_picks(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    result = await session.execute(select(Pick).order_by(Pick.created_at.desc()).limit(limit))
    picks = result.scalars().all()
    return [
        {
            "id": p.id,
            "event_id": p.event_id,
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
            "status": p.status,
            "created_at": p.created_at.isoformat(),
            "clv_log": str(p.clv_log) if p.clv_log is not None else None,
            "beat_close": p.beat_close,
            "manual_betting_reminder": "Manual review required. This system does not place bets.",
        }
        for p in picks
    ]


@router.post("/picks/{pick_id}/result", status_code=201)
async def record_result(
    pick_id: int,
    payload: ResultIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    pick = await session.get(Pick, pick_id)
    if pick is None:
        raise HTTPException(status_code=404, detail="pick not found")

    pnl: Decimal | None = None
    roi: Decimal | None = None
    if payload.bet_placed and payload.actual_stake is not None:
        odds = payload.actual_odds or float(pick.decimal_odds)
        if payload.outcome == "won":
            pnl = payload.actual_stake * Decimal(str(odds - 1.0))
        elif payload.outcome == "lost":
            pnl = -payload.actual_stake
        else:  # void / push: stake returned
            pnl = Decimal("0.00")
        if payload.actual_stake > 0:
            roi = pnl / payload.actual_stake

    await session.execute(
        insert(ManualBetLog).values(
            pick_id=pick_id,
            bet_placed=payload.bet_placed,
            actual_stake=payload.actual_stake,
            actual_odds=payload.actual_odds,
            bookmaker_used=payload.bookmaker_used,
            notes=payload.notes,
        )
    )
    await session.execute(
        insert(ResultTracking).values(
            pick_id=pick_id,
            outcome=str(payload.outcome),
            pnl=pnl,
            roi=roi,
            settled_at=payload.settled_at,
        )
    )
    await session.execute(update(Pick).where(Pick.id == pick_id).values(status="settled"))
    await session.commit()
    return {"status": "recorded", "outcome": str(payload.outcome)}
