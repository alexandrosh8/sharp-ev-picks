"""Alert model and sink protocol.

Every alert ends with the manual-betting reminder — alerts inform a human
decision; nothing here (or anywhere) places bets.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.edge.value import ceil_odds, min_acceptable_odds
from app.schemas.picks import ALERT_FOOTER, PickOut

#: Same-game correlation warning (dashboard-chip parity for Telegram/webhook
#: alerts). The value pipeline passes it to `build_pick_alert` when the pick's
#: event already carries reserved exposure from a PRIOR grant today. BODY text
#: only — never part of the dedupe-key inputs (`raw_key`), so flagging
#: correlation can never re-alert an otherwise-identical market state.
#: Informational: the ledger's per-event cap already bounds combined exposure.
CORRELATED_EXPOSURE_WARNING = (
    "⚠ Correlated: other premium exposure already on this game today — combined cap 4% applies."
)

# Per-sport emoji for the alert header (neutral fallback for any new sport).
_SPORT_EMOJI = {
    "soccer": "⚽",
    "basketball": "🏀",
    "basketball_nba": "🏀",
    "basketball_euroleague": "🏀",
    "tennis": "🎾",
    "american_football": "🏈",
}


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


def build_pick_alert(
    pick: PickOut,
    value_min_edge: float | None = None,
    *,
    model_name: str = "",
    model_version: str = "",
    correlation_warning: str | None = None,
    repriced: bool = False,
) -> Alert:
    """Render a pick into an alert with a stable idempotency key.

    The key deliberately EXCLUDES pick_id (a fresh uuid per cycle): the same
    market state must not re-alert every poll; a price or execution-venue
    change produces a new key and a fresh alert.

    The key DOES include `model_name`/`model_version` (the strategy identity
    from PipelineDeps): a strategy-version bump re-emits the same opportunity
    as a genuinely new signal, and its alert must not be suppressed by a
    stale Redis dedupe key left by the previous version. Empty strings
    (legacy/model-strategy callers) keep the historical key shape.

    `value_min_edge` (the VALUE pipeline's premium threshold, passed from
    PipelineDeps) adds the execution line "Still +EV down to X.XX": the
    minimum displayed odds at which the pick retains >= that edge. VALUE-
    strategy semantics only: for value picks `model_probability` holds the
    devigged sharp fair probability (app/pipeline.py maps
    v.sharp_fair_prob there) — the model strategy must pass None, its edge
    (p_model - p_fair) does not shrink with the price the same way.

    `correlation_warning` (default None -> no line) appends one informational
    line to the BODY — the same-game correlation note the dashboard shows as a
    chip (pass CORRELATED_EXPOSURE_WARNING). It is deliberately excluded from
    `raw_key`: the idempotency key hashes market state, and gaining/losing the
    warning must neither re-alert nor suppress an otherwise-identical pick.
    """
    # Tier tag: ⭐ PREMIUM (alerted + exposure-reserved) vs 🔵 VOLUME (shadow
    # tier — tracked for CLV, never reserves exposure). The tier is included in
    # the dedupe key so a VOLUME alert never suppresses a later PREMIUM *upgrade*
    # alert for the same market at the same odds (distinct keys, distinct alerts).
    tier_tag = "⭐ PREMIUM" if pick.tier == "premium" else "🔵 VOLUME"
    # Preserve the exact pre-market-detail hash shape for lineless/legacy
    # picks. Inserting an empty detail field changed every unchanged open
    # pick's key during rollout and replayed alerts. Only a real detail adds a
    # new component, where it is required to distinguish instruments.
    market_identity = (
        f"{pick.market}|{pick.selection}"
        if not pick.market_detail
        else f"{pick.market}|{pick.market_detail}|{pick.selection}"
    )
    raw_key = (
        f"{pick.event_id}|{pick.bookmaker}|{market_identity}"
        f"|{pick.decimal_odds}|{pick.tier}|{model_name}|{model_version}"
    )
    dedupe_key = hashlib.sha256(raw_key.encode()).hexdigest()[:32]
    title = f"{tier_tag} +EV pick: {pick.event} — {pick.selection} @ {pick.decimal_odds:.2f}"
    # The displayed "🎯 Fair" line must show the TRUE fair ODDS, apples-to-apples
    # with the offered odds. The field that holds the true fair differs by pick
    # type (app/pipeline.py): for VALUE picks (value_min_edge is not None)
    # model_probability carries the devigged sharp fair prob, while
    # fair_probability carries the OFFERED odds' implied prob; for MODEL picks
    # fair_probability IS the devigged market fair. Sourcing the fair from the
    # wrong field renders the offered odds as the fair (e.g. "Fair 1.83 → 1.83").
    true_fair_prob = pick.model_probability if value_min_edge is not None else pick.fair_probability
    fair_odds = 1.0 / true_fair_prob if true_fair_prob > 0 else 0.0
    anchor = f" ({pick.anchor_type.title()})" if pick.anchor_type else ""
    value_line: list[str] = []
    if value_min_edge is not None:
        floor = min_acceptable_odds(pick.model_probability, value_min_edge, book=pick.bookmaker)
        if floor is not None:
            value_line.append(f"⏳ Value holds to {ceil_odds(floor):.2f} — skip below")
    sport_emoji = _SPORT_EMOJI.get(pick.sport, "🏟️")
    liq = f" · liquidity {pick.liquidity}" if pick.liquidity is not None else ""
    stake_line = (
        f"💰 Updated TOTAL target {pick.recommended_stake_fraction:.1%} of bankroll "
        f"(~{pick.recommended_stake_amount}) — do not add this total again"
        if repriced
        else f"💰 Stake {pick.recommended_stake_fraction:.1%} of bankroll "
        f"(~{pick.recommended_stake_amount})"
    )
    body = "\n".join(
        [
            f"🎯 {tier_tag} +EV PICK — {pick.event}",
            f"✅ {pick.selection} @ {pick.decimal_odds:.2f} · {pick.bookmaker}",
            "",
            f"📈 Edge {pick.edge:+.1%} · EV {pick.ev:+.1%} · Conf {pick.confidence:.0%}",
            f"🎯 Fair {fair_odds:.2f}{anchor} → {pick.decimal_odds:.2f} beats it",
            stake_line,
            *value_line,
            f"{sport_emoji} {pick.sport.replace('_', ' ').title()} · {pick.league}"
            f" · odds {pick.odds_age_seconds:.0f}s old{liq}",
            "",
            f"💡 {pick.reason_summary}",
            # Informational same-game correlation note — body-only, never keyed.
            *([correlation_warning] if correlation_warning else []),
            "",
            ALERT_FOOTER,
        ]
    )
    return Alert(pick_id=pick.pick_id, title=title, body=body, dedupe_key=dedupe_key)


def build_value_lost_alert(
    *,
    pick_id: int,
    event: str,
    market: str,
    selection: str,
    bookmaker: str,
    decimal_odds: Decimal,
    current_edge: float,
    edge_floor: float,
    value_lost_at: datetime,
) -> Alert:
    """Render a premium VALUE-LOST transition as a "mentioned" event (operator
    item 2, 2026-08-04): the pick's re-priced edge crossed below its tier floor
    and the operator must hear about it without opening the dashboard.

    The dedupe key hashes (pick DB id, value_lost_at) — the TRANSITION identity.
    The revalidation loop only builds this alert at the set-crossing (never
    while the state persists), so one transition dispatches exactly once; a
    later re-loss after re-qualification carries a fresh timestamp and therefore
    a fresh key. Informational wording only: this platform never places,
    modifies, or cashes out bets — the message says "do not bet", nothing more.
    Body carries names/odds/percentages only — never URLs or credentials."""
    raw_key = f"value-lost|{pick_id}|{value_lost_at.isoformat()}"
    dedupe_key = hashlib.sha256(raw_key.encode()).hexdigest()[:32]
    title = f"⚠️ VALUE LOST: {event} — {selection}"
    body = "\n".join(
        [
            f"⚠️ VALUE LOST — {event}",
            f"❌ {selection} @ {bookmaker} ({market}) — minted at {decimal_odds:.2f}",
            "",
            f"📉 Re-priced edge {current_edge:+.1%} is below the premium floor {edge_floor:.1%}.",
            "This pick no longer qualifies — do not bet it now.",
            "",
            ALERT_FOOTER,
        ]
    )
    return Alert(pick_id=str(pick_id), title=title, body=body, dedupe_key=dedupe_key)
