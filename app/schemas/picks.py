"""Pick output contract — what alerts, the API, and the dashboard consume."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.identity import (
    BOOKMAKER_MAX_BYTES,
    EVENT_REF_MAX_BYTES,
    LEAGUE_KEY_MAX_BYTES,
    MARKET_DETAIL_MAX_BYTES,
    SELECTION_MAX_BYTES,
    SPORT_KEY_MAX_BYTES,
    require_bounded_identity,
)
from app.schemas.base import InternalModel, Market, to_utc

# Formal safety statement. The literal "This system does not place bets" is
# asserted by scripts/safety_audit.sh (CI gate) and is the platform's picks-only
# guarantee — kept here even though pick alerts render the compact ALERT_FOOTER.
MANUAL_BETTING_REMINDER = "Manual review required. This system does not place bets."

# Compact one-line disclaimer at the foot of every pick alert: informational
# only, the user places any bet themselves (the system never does), no guarantee.
ALERT_FOOTER = "ℹ️ Informational only — you place any bet. No profit guaranteed."


class StakeBreakdownOut(InternalModel):
    raw_kelly: float
    fractional: float
    capped: bool  # per-bet cap hit (fractional > max_stake_fraction)
    final: float  # the GRANTED fraction (after the daily-exposure ledger clip)
    # True when the daily-exposure ledger clipped `final` below the per-bet-capped
    # fraction (granted < breakdown.final). Distinguishes a daily clip from the
    # per-bet cap (`capped`) so `final` is reproducible from the inputs.
    daily_clipped: bool = False


class PickOut(InternalModel):
    pick_id: str
    sport: str
    league: str
    event: str
    event_id: str
    market: Market
    selection: str
    # CANONICAL devig-group detail at mint (app/pipeline.py::
    # canonical_market_detail — e.g. "totals_2_5", "asian_handicap_-1_0").
    # Persisted so the CLV true-up matches the close on the EXACT group,
    # bypassing the line-blind (event, market, selection) ambiguity guard.
    # None = lineless market (h2h/1x2/btts canonicalize to None) or a
    # pre-column row — those follow the legacy line-blind path.
    market_detail: str | None = None
    bookmaker: str
    decimal_odds: float = Field(gt=1.0)
    model_probability: float = Field(ge=0.0, le=1.0)
    fair_probability: float = Field(ge=0.0, le=1.0)
    edge: float
    ev: float
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_stake_fraction: float = Field(ge=0.0)
    recommended_stake_amount: Decimal = Field(ge=0)
    stake_breakdown: StakeBreakdownOut
    odds_age_seconds: float = Field(ge=0.0)
    liquidity: float | None = None
    reason_summary: str
    # "premium" (edge >= VALUE_MIN_EDGE: alerted + exposure-reserved) or
    # "volume" (shadow tier: persisted + CLV-tracked only, never alerted or
    # exposure-reserved — see app/pipeline.py).
    tier: str = "premium"
    # Calibrated meta-model score P(candidate beats the vig-free Max close)
    # from app/models/value_filter.py — None when the artifact is absent or
    # the candidate is outside the model's trained scope. Informational
    # unless VALUE_ML_FILTER is on (then sub-threshold premium candidates
    # are demoted to the volume tier before alerting).
    value_filter_score: float | None = Field(default=None, ge=0.0, le=1.0)
    # Fair-value anchor that produced this pick: "pinnacle" | "sharp" |
    # "consensus" (app/edge/value.py::anchor_type_for). None for the model
    # strategy. Persisted so live CLV can be stratified by anchor — the
    # consensus fallback's live verdict mechanism.
    anchor_type: str | None = None
    # The concrete pick-time sharp anchor BOOK NAME (e.g. "Pinnacle", "Betfair
    # Exchange", "Smarkets") or the CONSENSUS_ANCHOR sentinel. anchor_type collapses
    # every named sharp book to "sharp"; this keeps the actual book so the CLV close
    # can test BOOK independence (CLV-3: a Smarkets-anchored pick vs a Betfair-exchange
    # close is independent though both are anchor_type "sharp"). None for the model
    # strategy or a pre-column row.
    anchor_book: str | None = None
    # MATCH-CONFIDENCE provenance of the pick-time sharp anchor (observability
    # only — never gates minting). Pinnacle (cross-source fuzzy-matched) anchor:
    # the accepted candidate's min per-side Jaro-Winkler in [0,1] with method
    # 'exact_canonical'/'jw_two_tier' ('slug_' prefix on the OddsPortal slug-
    # fallback path; 'unscored' + None confidence when a pinnacle-typed pick's
    # provenance was unavailable — never a fabricated 1.0). Inline Betfair/
    # Smarkets anchor (same canonical event, no pick-time match): 1.0 /
    # 'inline_betfair_canonical'. None/None = consensus anchor or model pick.
    anchor_match_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    anchor_match_method: str | None = None
    # BETFAIR STALENESS GUARD mint stamp (observability ONLY — never gating):
    # the effective per-event verdict the guard read at mint for this pick's
    # H2H market: "pass" | "demote" | "no_api_match" | "no_api_price" |
    # "stale_api" (over-TTL verdict — always a no-op; stale API evidence never
    # demotes a live anchor). Under SHADOW a "demote" marks a WOULD-demote
    # (anchor unchanged); under enforce it marks an actual exchange-anchor
    # demotion (fell to the next sharp book / consensus). None = guard off,
    # no verdict for the event, or non-H2H market.
    anchor_staleness_decision: str | None = None
    # A5 STEAM SHADOW VERDICT (observability ONLY — never gates, demotes,
    # filters, or reorders; the steam gate itself stays OFF, memory rule
    # 2026-06-28). What app/edge/steam.py decided AT MINT over the same inputs
    # the real gate would see, persisted so future in-season settled evidence
    # can validate or refute the gate without enabling it. steam_tripped None =
    # never evaluated (gate unconfigured / consensus anchor / eval error /
    # pre-column row); False = evaluated and clean; True = would demote (did
    # demote only if the gate is enforcing). steam_reasons = comma-joined
    # SteamVerdict.reasons slugs (None when no component flag raised); the two
    # numeric detail fields stay None whenever the gate could not compute them
    # (never fabricated).
    steam_tripped: bool | None = None
    steam_reasons: str | None = None
    steam_closed_fraction: float | None = None
    steam_anchor_age_seconds: float | None = Field(default=None, ge=0.0)
    # P2-2: did the anchor's devig FALL BACK to multiplicative when this pick's
    # MINT fair was computed (underround book / solver failure)? Persisted as
    # Pick.mint_devig_fell_back so the trusted CLV subset can drop ASYMMETRIC
    # mint/close fallbacks. None = model-strategy pick / pre-column row.
    mint_devig_fell_back: bool | None = None
    # TIMING TELEMETRY (2026-07-26, observability first): hours between MINT
    # (created_at) and the event's best-known kickoff (starts_at) — positive =
    # minted before kickoff. Stamped at mint by both strategies; None = kickoff
    # unknown at mint (never fabricated) or a pre-column row. The INERT
    # premium_max_hours_to_kickoff policy ceiling reads this same number when a
    # future config flip arms it (named reason 'premium_mint_too_early').
    hours_to_kickoff: float | None = None
    # Final score of the settled game ("HOME-AWAY", e.g. "2-1"). None until the
    # pick settles (or when no score was recorded). Surfaced in the dashboard
    # SETTLED view; /picks serializes the repo dict, so this keeps the contract
    # model in step with the served payload.
    score: str | None = None
    # Compact, human-debuggable POLICY FINGERPRINT of the live value-strategy
    # policy that minted this pick (H3): the active thresholds (value_min_edge /
    # value_volume_min_edge / value_min_odds), the devig method, require-sharp-
    # anchor on/off, the data-error edge ceiling, and the ML value-filter manifest
    # identity (manifest created_utc @ q*) WHEN enforcement is on. Lets CLV
    # attribution SCOPE each row to the exact policy regime that produced it,
    # instead of silently mixing regimes across config changes, and lets a pick be
    # replayed against the policy that made it. None = model-strategy pick or a
    # pre-column row (nullable + tolerated everywhere it is read).
    policy_fingerprint: str | None = None
    created_at: datetime
    risk_warning: str = "Betting involves risk. Nothing here is guaranteed profit."
    manual_betting_reminder: str = MANUAL_BETTING_REMINDER

    @field_validator("sport")
    @classmethod
    def _bounded_sport(cls, value: str) -> str:
        return require_bounded_identity(value, maximum_bytes=SPORT_KEY_MAX_BYTES, field="sport")

    @field_validator("league")
    @classmethod
    def _bounded_league(cls, value: str) -> str:
        return require_bounded_identity(value, maximum_bytes=LEAGUE_KEY_MAX_BYTES, field="league")

    @field_validator("event_id")
    @classmethod
    def _bounded_event_id(cls, value: str) -> str:
        return require_bounded_identity(value, maximum_bytes=EVENT_REF_MAX_BYTES, field="event_id")

    @field_validator("selection")
    @classmethod
    def _bounded_selection(cls, value: str) -> str:
        return require_bounded_identity(value, maximum_bytes=SELECTION_MAX_BYTES, field="selection")

    @field_validator("market_detail")
    @classmethod
    def _bounded_market_detail(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_bounded_identity(
            value,
            maximum_bytes=MARKET_DETAIL_MAX_BYTES,
            field="market_detail",
            allow_empty=True,
        )

    @field_validator("bookmaker", "anchor_book")
    @classmethod
    def _bounded_book(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_bounded_identity(value, maximum_bytes=BOOKMAKER_MAX_BYTES, field="bookmaker")

    _utc_created = field_validator("created_at")(to_utc)
