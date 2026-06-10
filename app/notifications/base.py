"""Alert model and sink protocol.

Every alert ends with the manual-betting reminder — alerts inform a human
decision; nothing here (or anywhere) places bets.
"""

import hashlib
from dataclasses import dataclass
from typing import Protocol

from app.schemas.picks import MANUAL_BETTING_REMINDER, PickOut


@dataclass(frozen=True)
class Alert:
    pick_id: str
    title: str
    body: str
    dedupe_key: str


class AlertSink(Protocol):
    """Delivery channel. Implementations NEVER raise — they return success."""

    name: str

    async def send(self, alert: Alert) -> bool: ...


def build_pick_alert(pick: PickOut) -> Alert:
    """Render a pick into an alert with a stable idempotency key.

    The key deliberately EXCLUDES pick_id (a fresh uuid per cycle): the same
    market state must not re-alert every poll; a price change produces a new
    key and a fresh alert.
    """
    raw_key = f"{pick.event_id}|{pick.bookmaker}|{pick.market}|{pick.selection}|{pick.decimal_odds}"
    dedupe_key = hashlib.sha256(raw_key.encode()).hexdigest()[:32]
    title = f"+EV pick: {pick.event} — {pick.selection} @ {pick.decimal_odds:.2f}"
    body = "\n".join(
        [
            title,
            f"Sport/League: {pick.sport} / {pick.league}",
            f"Market: {pick.market} | Bookmaker: {pick.bookmaker}",
            f"Model probability: {pick.model_probability:.3f}",
            f"Fair (vig-free) probability: {pick.fair_probability:.3f}",
            f"Edge: {pick.edge:+.3f} | EV: {pick.ev:+.3f}",
            f"Confidence: {pick.confidence:.2f}",
            (
                f"Recommended stake: {pick.recommended_stake_fraction:.2%} of bankroll"
                f" (~{pick.recommended_stake_amount}) — informational only"
            ),
            f"Odds age: {pick.odds_age_seconds:.0f}s"
            + (f" | Liquidity: {pick.liquidity}" if pick.liquidity is not None else ""),
            f"Why: {pick.reason_summary}",
            f"Generated: {pick.created_at.isoformat()}",
            pick.risk_warning,
            MANUAL_BETTING_REMINDER,
        ]
    )
    return Alert(pick_id=pick.pick_id, title=title, body=body, dedupe_key=dedupe_key)
