"""Alert model and sink protocol.

Every alert ends with the manual-betting reminder — alerts inform a human
decision; nothing here (or anywhere) places bets.
"""

import hashlib
from dataclasses import dataclass
from typing import Protocol

from app.edge.value import ceil_odds, min_acceptable_odds
from app.schemas.picks import PickOut

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
) -> Alert:
    """Render a pick into an alert with a stable idempotency key.

    The key deliberately EXCLUDES pick_id (a fresh uuid per cycle): the same
    market state must not re-alert every poll; a price change produces a new
    key and a fresh alert.

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
    """
    # Tier tag: ⭐ PREMIUM (alerted + exposure-reserved) vs 🔵 VOLUME (shadow
    # tier — tracked for CLV, never reserves exposure). The tier is included in
    # the dedupe key so a VOLUME alert never suppresses a later PREMIUM *upgrade*
    # alert for the same market at the same odds (distinct keys, distinct alerts).
    tier_tag = "⭐ PREMIUM" if pick.tier == "premium" else "🔵 VOLUME"
    raw_key = (
        f"{pick.event_id}|{pick.bookmaker}|{pick.market}|{pick.selection}"
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
    body = "\n".join(
        [
            f"🎯 {tier_tag} +EV PICK — {pick.event}",
            f"✅ {pick.selection} @ {pick.decimal_odds:.2f} · {pick.bookmaker}",
            "",
            f"📈 Edge {pick.edge:+.1%} · EV {pick.ev:+.1%} · Conf {pick.confidence:.0%}",
            f"🎯 Fair {fair_odds:.2f}{anchor} → {pick.decimal_odds:.2f} beats it",
            f"💰 Stake {pick.recommended_stake_fraction:.1%} of bankroll "
            f"(~{pick.recommended_stake_amount})",
            *value_line,
            f"{sport_emoji} {pick.sport.replace('_', ' ').title()} · {pick.league}"
            f" · odds {pick.odds_age_seconds:.0f}s old{liq}",
            "",
            f"💡 {pick.reason_summary}",
        ]
    )
    return Alert(pick_id=pick.pick_id, title=title, body=body, dedupe_key=dedupe_key)
