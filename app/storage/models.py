"""SQLAlchemy ORM models — the 14-table warehouse (docs/db-schema.md).

Conventions (postgres-schema skill): TIMESTAMPTZ everywhere, NUMERIC for
odds/probabilities/money (never float columns), append-only odds_snapshots,
and no credential-shaped columns anywhere — manual_bet_logs records only
user-entered facts about bets THEY placed manually.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ODDS = Numeric(10, 4)
PROB = Numeric(8, 6)
MONEY = Numeric(12, 2)
METRIC = Numeric(12, 6)


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


class Sport(Base):
    __tablename__ = "sports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)  # e.g. "soccer", "basketball_nba"
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class League(Base):
    __tablename__ = "leagues"
    # Country is part of league IDENTITY: same-named leagues in different countries
    # (e.g. many "Premier League"s) must be DISTINCT rows, else the first-seen
    # country freezes and mislabels all the others (the "Ethiopia — Premier League"
    # merge bug). country is NOT NULL ('' when the source omits it) — a NULL would be
    # treated as distinct by the Postgres unique index and defeat dedup.
    __table_args__ = (
        UniqueConstraint("sport_id", "key", "country", name="uq_leagues_sport_key_country"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    key: Mapped[str] = mapped_column(String(64))  # e.g. "soccer_epl"
    name: Mapped[str] = mapped_column(String(128))
    country: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("sport_id", "normalized_name", name="uq_teams_sport_normalized"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    league_id: Mapped[int | None] = mapped_column(ForeignKey("leagues.id"))
    name: Mapped[str] = mapped_column(String(128))
    normalized_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("external_ref", name="uq_events_external_ref"),
        Index("idx_events_starts_at", "starts_at"),
        Index("idx_events_league_starts", "league_id", "starts_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    external_ref: Mapped[str] = mapped_column(String(128))  # provider event key
    status: Mapped[str] = mapped_column(String(32), server_default="scheduled")
    # NULL = the source never reported a kickoff ("TBD" on the dashboard:
    # no countdown, no settle button). Healed by refresh_event_kickoffs /
    # _get_or_create_event once a scrape reports the real start. Never a
    # pick-time placeholder — that rendered fake kickoffs as real.
    starts_at: Mapped[datetime | None]
    # Best-effort final score scraped from OddsPortal AFTER the match finished
    # (OddsHarvester surfaces it in the match dict). Plain ints — not money/odds,
    # so no NUMERIC. Nullable: present only when we scraped the match post-finish,
    # NULL otherwise (the common case) and for rows from before this column.
    # CONVENIENCE ONLY: pre-fills the manual settle prompt and the CLOSED-tab
    # hint — never auto-settles, never the confirmed result.
    scraped_home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scraped_away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(onupdate=func.now())


class OddsSnapshot(Base):
    """Append-only price observations. Never updated, never deleted."""

    __tablename__ = "odds_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "bookmaker",
            "market",
            "selection",
            "captured_at",
            name="uq_odds_snapshot_observation",
        ),
        Index("idx_odds_event_market_captured", "event_id", "market", "captured_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    bookmaker: Mapped[str] = mapped_column(String(64))
    # 64, not 32: a 33-char quarter-line handicap-games key
    # ("asian_handicap_games_-10_25_games") was truncated under the old width,
    # merging distinct lines into one devig group (see snapshot_market_key).
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(64))
    decimal_odds: Mapped[Decimal] = mapped_column(ODDS)
    liquidity: Mapped[Decimal | None] = mapped_column(MONEY)
    captured_at: Mapped[datetime]  # provider-reported price time
    ingested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # RESERVED (audit #12): never set by app code today — the live close marker is
    # Pick.closing_odds / Pick.closing_anchor_type, not this column.
    is_closing: Mapped[bool] = mapped_column(Boolean, server_default="false")


class ModelVersion(Base):
    __tablename__ = "model_versions"
    # Identity is (sport_id, name, version): the value strategy is sport-
    # agnostic and reuses one name/version ("value-sharp-vs-soft"/"v3") for
    # soccer AND basketball. Keying on (name, version) alone let the first
    # sport's row win the sport_id and the second sport silently reuse it
    # (wrong sport attribution). Per-sport models keep one row per sport.
    __table_args__ = (
        UniqueConstraint(
            "sport_id", "name", "version", name="uq_model_versions_sport_name_version"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))  # e.g. "football-dixon-coles"
    version: Mapped[str] = mapped_column(String(64))
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    trained_at: Mapped[datetime | None]
    training_window_start: Mapped[datetime | None]
    training_window_end: Mapped[datetime | None]
    features_hash: Mapped[str | None] = mapped_column(String(64))
    hyperparameters: Mapped[dict[str, Any] | None]
    calibration_method: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict[str, Any] | None]  # brier, log_loss, ece per market
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ModelPrediction(Base):
    """RESERVED (audit #12): migrated but UNWRITTEN — no app code constructs
    ModelPrediction today (the value strategy persists picks directly). Populate it
    for the model strategy, or treat it as reserved capacity."""

    __tablename__ = "model_predictions"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "model_version_id",
            "market",
            "selection",
            name="uq_model_predictions_unique",
        ),
        Index("idx_predictions_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"))
    market: Mapped[str] = mapped_column(String(32))
    selection: Mapped[str] = mapped_column(String(64))
    probability: Mapped[Decimal] = mapped_column(PROB)
    confidence: Mapped[Decimal | None] = mapped_column(PROB)
    predicted_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DetectedEdge(Base):
    """RESERVED (audit #12): intended as a per-gate audit trail (every gate
    evaluation — accepted AND rejected), but NO app code writes DetectedEdge today
    and Pick.detected_edge_id is always NULL. The table/columns exist via migration;
    either wire the value/edge pipeline to populate it or treat it as reserved
    capacity — it is not a live audit trail yet."""

    __tablename__ = "detected_edges"
    __table_args__ = (Index("idx_detected_edges_event", "event_id", "detected_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    model_prediction_id: Mapped[int] = mapped_column(ForeignKey("model_predictions.id"))
    odds_snapshot_id: Mapped[int] = mapped_column(ForeignKey("odds_snapshots.id"))
    devig_method: Mapped[str] = mapped_column(String(32))
    fair_probability: Mapped[Decimal] = mapped_column(PROB)
    edge: Mapped[Decimal] = mapped_column(METRIC)
    ev: Mapped[Decimal] = mapped_column(METRIC)
    accepted: Mapped[bool] = mapped_column(Boolean)
    reject_reasons: Mapped[dict[str, Any] | None]  # {"reasons": [...]}
    detected_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Pick(Base):
    __tablename__ = "picks"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "market",
            "selection",
            "model_version_id",
            name="uq_picks_event_market_selection_model",
        ),
        Index("idx_picks_created", "created_at"),
        Index("idx_picks_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"))
    detected_edge_id: Mapped[int | None] = mapped_column(ForeignKey("detected_edges.id"))
    market: Mapped[str] = mapped_column(String(32))
    selection: Mapped[str] = mapped_column(String(64))
    # CANONICAL devig-group detail at mint (app/pipeline.py::
    # canonical_market_detail, e.g. "totals_2_5", "asian_handicap_-1_0"): the
    # CLV true-up matches the close on this EXACT group, bypassing the
    # line-blind (event, market, selection) ambiguity guard. NULL = lineless
    # market (h2h/1x2/btts canonicalize to None) or a pre-column row — those
    # keep the legacy fail-closed line-blind behavior.
    market_detail: Mapped[str | None] = mapped_column(Text)
    bookmaker: Mapped[str] = mapped_column(String(64))
    decimal_odds: Mapped[Decimal] = mapped_column(ODDS)
    model_probability: Mapped[Decimal] = mapped_column(PROB)
    fair_probability: Mapped[Decimal] = mapped_column(PROB)
    edge: Mapped[Decimal] = mapped_column(METRIC)
    ev: Mapped[Decimal] = mapped_column(METRIC)
    confidence: Mapped[Decimal] = mapped_column(PROB)
    recommended_stake_fraction: Mapped[Decimal] = mapped_column(PROB)
    recommended_stake_amount: Mapped[Decimal] = mapped_column(MONEY)
    stake_breakdown: Mapped[dict[str, Any] | None]
    reason_summary: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    # Two-tier picks: 'premium' (edge >= VALUE_MIN_EDGE — alerted, reserves
    # daily exposure) or 'volume' (VALUE_VOLUME_MIN_EDGE <= edge < premium —
    # informational shadow tier: persisted + CLV-revalidated only, never
    # alerted, never consumes the exposure cap). `status` stays the lifecycle
    # (alerted/settled/superseded...) for BOTH tiers, so revalidation and
    # settlement treat them identically; tier scopes alerts/exposure/reports.
    tier: Mapped[str] = mapped_column(String(16), server_default="premium")
    # Calibrated value-filter meta-model score (P(beats the Max close) —
    # app/models/value_filter.py). NULL = artifact absent or candidate
    # outside the model's trained scope; historical rows stay NULL.
    value_filter_score: Mapped[Decimal | None] = mapped_column(PROB)
    # Fair-value anchor that produced the pick: 'pinnacle' | 'sharp' (named
    # non-Pinnacle sharp book) | 'consensus' (>=3-book median fallback) —
    # lets live CLV be stratified by anchor (the consensus fallback's live
    # verdict mechanism). NULL = model-strategy pick or pre-column row.
    anchor_type: Mapped[str | None] = mapped_column(String(16))
    # The pick-time sharp anchor BOOK NAME (e.g. 'Pinnacle', 'Betfair Exchange',
    # or the CONSENSUS_ANCHOR sentinel) — the concrete book behind anchor_type.
    # anchor_type collapses every named sharp book to 'sharp'; this keeps the
    # actual book so per-book anchor analysis (which sharp book sourced the fair,
    # finding CLV-3) is possible without re-deriving it. NULL = model-strategy
    # pick or pre-column row.
    anchor_book: Mapped[str | None] = mapped_column(String(64))
    # MATCH-CONFIDENCE provenance of the pick-time sharp anchor (observability
    # only — never gates minting). For a Pinnacle (cross-source, fuzzy-matched)
    # anchor: min per-side Jaro-Winkler of the accepted candidate in [0,1] with
    # method 'exact_canonical'/'jw_two_tier' ('slug_' prefix on the OddsPortal
    # slug-fallback path; 'unscored' = pinnacle-typed pick whose provenance was
    # unavailable — recorded honestly as NULL confidence, never fabricated 1.0).
    # For an inline Betfair/Smarkets anchor (same canonical event, no pick-time
    # match): 1.0 / 'inline_betfair_canonical'. NULL/NULL = consensus anchor,
    # model-strategy pick, or pre-column row.
    anchor_match_confidence: Mapped[Decimal | None] = mapped_column(PROB)
    anchor_match_method: Mapped[str | None] = mapped_column(String(32))
    # Compact, human-debuggable POLICY FINGERPRINT of the live value-strategy
    # policy that minted this pick (H3): active thresholds (value_min_edge /
    # volume_min_edge / min_odds), devig method, require-sharp-anchor on/off, the
    # data-error edge ceiling, and the enforced ML value-filter manifest identity
    # (created_utc @ q*). Lets CLV attribution SCOPE each row to its exact policy
    # regime instead of mixing regimes across config changes, and lets a pick be
    # replayed against the policy that made it. NULL = model-strategy pick or a
    # pre-column row (additive + nullable; historical rows stay NULL).
    policy_fingerprint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # --- CLV (filled at/after market close) ---------------------------------
    closing_odds: Mapped[Decimal | None] = mapped_column(ODDS)
    closing_fair_probability: Mapped[Decimal | None] = mapped_column(PROB)
    clv_log: Mapped[Decimal | None] = mapped_column(METRIC)
    beat_close: Mapped[bool | None] = mapped_column(Boolean)
    # Anchor that produced the CLOSE (pinnacle/sharp/consensus) — independent of
    # the creation anchor_type above. Together with closing_odds (NON-NULL =
    # snapshot-sourced) it separates an honest sharp close from a consensus-
    # median or a poll-time revalidation fallback, so the per-anchor and headline
    # CLV can trust only genuine sharp closes. NULL = no close computed yet /
    # pre-column row.
    closing_anchor_type: Mapped[str | None] = mapped_column(String(16))
    # True whenever finalize_closing_from_snapshots ANCHORED a close fair from our
    # own odds_snapshots history — INDEPENDENT of whether a SOFT book also priced
    # the selection (closing_odds). When only sharp books quote the close,
    # closing_odds stays NULL yet the close fair is real; deriving the snapshot-
    # close flag from `closing_odds IS NOT NULL` then false-negatives those rows
    # (finding clv-1). This explicit flag records the genuine state: True = real
    # snapshot close anchored; NULL = no snapshot close computed yet / pre-column
    # row. Additive + nullable — rows closed before this column stay NULL.
    has_snapshot_close: Mapped[bool | None] = mapped_column(Boolean)
    # INDEPENDENCE provenance (P0-1/P0-3 fake-CLV guard): True = the book that
    # ANCHORED the close is NOT this pick's own fill book (bookmaker) — a genuine,
    # independent close; False = the close was anchored by the fill book itself
    # (CIRCULAR: the pick's own book pricing its own close, closing == fill,
    # |clv_log|~0 — the fake CLV that masked the -EV). A consensus(median) close
    # spans >=3 books, so it is independent of any single fill by construction
    # (True). NULL = no snapshot close computed yet / pre-column row. The trusted
    # sharp-CLV subset (n_sharp / sharp_stake_weighted_clv_log) excludes rows
    # where this is False, so a self-priced close can never count as honest CLV.
    close_independent_of_fill: Mapped[bool | None] = mapped_column(Boolean)
    # P2-2 devig-fallback provenance. True when the devig method FELL BACK to
    # multiplicative for the MINT (pick-time) fair / the CLOSE fair respectively
    # — an underround book or solver failure made the configured method not
    # apply (app/probabilities/devig.devig_with_provenance). When the two
    # DISAGREE (one fell back, the other did not) the mint and close fairs were
    # computed by different effective methods, so their CLV is a devig-method
    # artifact rather than a real line move: the trusted sharp-CLV subset
    # EXCLUDES such asymmetric rows. NULL = not recorded (model-strategy pick or
    # pre-column row); additive + nullable, so historical rows stay NULL and are
    # treated as symmetric (not excluded).
    mint_devig_fell_back: Mapped[bool | None] = mapped_column(Boolean)
    close_devig_fell_back: Mapped[bool | None] = mapped_column(Boolean)
    # D3 CLOSE PROVENANCE (close-evidence package, 2026-07). The concrete BOOK
    # that anchored the close fair (close-side twin of the mint-side anchor_book;
    # the consensus sentinel for a median close) and the CAPTURE TIME of the
    # anchor rows behind it. Together they finally separate an ECHO of the mint
    # anchor row (close_snapshot_captured_at <= created_at — the close merely
    # re-read the line the pick was minted on) from a FRESH observation of a
    # genuinely unmoved line (> created_at). Stamped by BOTH close writers
    # (revalidate_open_picks + finalize_closing_from_snapshots); observability
    # only for now — the tautology guard is NOT rekeyed on these until the
    # deferred single-shot validation (ADR-0019 discipline). NULL = pre-column
    # row / no close computed yet. Additive + nullable.
    close_anchor_book: Mapped[str | None] = mapped_column(String(64))
    close_snapshot_captured_at: Mapped[datetime | None]
    # A4 CLOSE-EXCLUSION REASON (closed vocabulary — app/edge/value.py
    # CLOSE_EXCLUSION_REASONS): WHY this pick's close is excluded from trusted
    # CLV — 'fabricated' | 'circular_self_priced' | 'stale_echo' |
    # 'tautological' | 'asymmetric_devig_fallback' — or 'trusted' when no
    # guard trips. Stamped beside close_independent_of_fill by BOTH close
    # writers (app/clv_trueup.py); observability only (nothing gates on it —
    # the boolean stays the gate input). NULL = pre-column row / no close
    # computed yet. Additive + nullable; no backfill (provenance accrues
    # forward).
    close_exclusion_reason: Mapped[str | None] = mapped_column(String(32))
    # BETFAIR STALENESS GUARD mint stamp (observability ONLY — never gating).
    # The effective per-event verdict the guard read at mint for this pick's
    # market: 'pass' | 'demote' | 'no_api_match' | 'no_api_price' | 'stale_api'
    # (a persisted verdict older than the TTL reads as stale_api => no-op). In
    # SHADOW mode a 'demote' stamp marks a WOULD-demote (anchor unchanged); in
    # enforce mode it marks an actual exchange-anchor demotion (fell to the
    # next sharp book / consensus). NULL = guard off, no verdict for the
    # event, non-H2H market, or pre-column row. Mirrors anchor_match_method.
    anchor_staleness_decision: Mapped[str | None] = mapped_column(String(16))
    # A5 STEAM SHADOW VERDICT mint stamp (observability ONLY — never gating;
    # the steam gate stays OFF per the 2026-06-28 walk-forward verdict). What
    # app/edge/steam.py decided at mint over the inputs the real gate would
    # see: tripped (NULL = never evaluated — gate unconfigured / consensus
    # anchor / eval error / pre-column row; False = evaluated clean; True =
    # would demote), the comma-joined reason slugs ('soft_toward_anchor' /
    # 'stale_anchor' / 'soft_steamed_away'), and the numeric detail (fraction
    # of the mint fill-vs-anchor gap already closed; anchor age at mint). NULL
    # numerics = the gate could not compute them (never fabricated). Additive
    # + nullable, no backfill — evidence accrues forward so future settled
    # data can validate/refute the gate without ever enabling it.
    steam_tripped: Mapped[bool | None] = mapped_column(Boolean)
    steam_reasons: Mapped[str | None] = mapped_column(String(64))
    steam_closed_fraction: Mapped[Decimal | None] = mapped_column(METRIC)
    steam_anchor_age_seconds: Mapped[Decimal | None] = mapped_column(METRIC)
    # ANCHOR-THINNESS TELEMETRY (Task 6, log-only — nothing gates on it): the
    # count of distinct NON-SHARP books quoting the pick's devig market group
    # at mint — exactly the number the thin-coverage floor (value_policy.
    # distinct_book_count, sharp set excluded) measures. Where the anchor's
    # own market is thin, a large "edge vs anchor" is usually fake (arbusers
    # community evidence); telemetry accrues until a walk-forward review
    # defines a threshold (never tuned on the spent holdout). The AGE half of
    # anchor thinness is already carried by steam_anchor_age_seconds above.
    # NULL = model-strategy pick or pre-column row. Additive + nullable.
    anchor_book_count: Mapped[int | None] = mapped_column(SmallInteger)
    # --- live revalidation (refreshed every poll while the pick is open) ----
    current_odds: Mapped[Decimal | None] = mapped_column(ODDS)
    current_edge: Mapped[Decimal | None] = mapped_column(METRIC)
    # The book whose live price current_odds reflects. Normally the pick's own
    # bookmaker (re-priced in place); differs ONLY in the fallback case where
    # the original book dropped the selection and the best remaining book is
    # shown instead — so the dashboard can label "now at <book>" honestly.
    current_bookmaker: Mapped[str | None] = mapped_column(String(64))
    # revalidated_at is SUCCESS-only (the dashboard "verified" badge: the pick
    # actually re-priced). revalidation_attempted_at advances on EVERY fetch
    # of the event's match page — priced or not — and drives the off-window
    # round-robin so dead links rotate to the back instead of starving the
    # queue (app/clv_trueup.py).
    revalidated_at: Mapped[datetime | None]
    revalidation_attempted_at: Mapped[datetime | None]


class ManualBetLog(Base):
    """What the user chose to do with a pick — entered manually by the user.

    Holds NO credentials, cookies, or account identifiers (ADR-0002).
    """

    __tablename__ = "manual_bet_logs"
    __table_args__ = (Index("idx_manual_bet_logs_pick", "pick_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    pick_id: Mapped[int] = mapped_column(ForeignKey("picks.id"))
    bet_placed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    actual_stake: Mapped[Decimal | None] = mapped_column(MONEY)
    actual_odds: Mapped[Decimal | None] = mapped_column(ODDS)
    bookmaker_used: Mapped[str | None] = mapped_column(String(64))
    placed_at: Mapped[datetime | None]
    notes: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ResultTracking(Base):
    __tablename__ = "result_tracking"
    __table_args__ = (UniqueConstraint("pick_id", name="uq_result_tracking_pick"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    pick_id: Mapped[int] = mapped_column(ForeignKey("picks.id"))
    outcome: Mapped[str] = mapped_column(String(16))  # won | lost | void | push
    pnl: Mapped[Decimal | None] = mapped_column(MONEY)  # vs actual or recommended stake
    roi: Mapped[Decimal | None] = mapped_column(METRIC)
    # Final score of the game that settled this pick (HOME, AWAY). Plain ints —
    # not money/odds, so no NUMERIC. Nullable: void settlements (no score known)
    # and rows persisted before this column stay NULL.
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    settled_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BankrollSnapshot(Base):
    __tablename__ = "bankroll_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_date", name="uq_bankroll_snapshots_date"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date)
    balance: Mapped[Decimal] = mapped_column(MONEY)
    note: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BankrollLedgerEntry(Base):
    """Append-only HYPOTHETICAL bankroll ledger (A8): a manual starting balance
    plus running settled P&L — informational ONLY, no money movement, never an
    input to live staking (ADR-0002; Kelly extensions stay off until evaluated).

    entry_type: 'starting_balance' | 'settled_pnl' | 'manual_adjustment'.
    ``amount`` for settled_pnl is result_tracking.pnl exactly as settlement
    computed it (actual logged stake when present, else the recommended
    fractional-Kelly stake) — never re-derived here. The UNIQUE on pick_id
    (NULLs exempt, so non-pick entries are unconstrained) makes the settled-P&L
    append idempotent: one ledger row per settled pick, ever.
    """

    __tablename__ = "bankroll_ledger"
    __table_args__ = (
        UniqueConstraint("pick_id", name="uq_bankroll_ledger_pick"),
        Index("idx_bankroll_ledger_occurred", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime]  # settled_at for settled_pnl; entry time otherwise
    entry_type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    balance_after: Mapped[Decimal] = mapped_column(MONEY)
    pick_id: Mapped[int | None] = mapped_column(ForeignKey("picks.id"))
    note: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_alerts_dedupe_key"),
        Index("idx_alerts_pick", "pick_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    pick_id: Mapped[int] = mapped_column(ForeignKey("picks.id"))
    channel: Mapped[str] = mapped_column(String(32))  # telegram | webhook
    dedupe_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # sent | failed | skipped
    sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    model_version_id: Mapped[int | None] = mapped_column(ForeignKey("model_versions.id"))
    window_start: Mapped[datetime]
    window_end: Mapped[datetime]
    gate_policy: Mapped[dict[str, Any] | None]
    cost_assumptions: Mapped[dict[str, Any] | None]  # slippage, commission
    n_picks: Mapped[int | None] = mapped_column(BigInteger)
    roi: Mapped[Decimal | None] = mapped_column(METRIC)
    clv_log_mean: Mapped[Decimal | None] = mapped_column(METRIC)
    max_drawdown: Mapped[Decimal | None] = mapped_column(METRIC)
    metrics: Mapped[dict[str, Any] | None]
    seed: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DashboardCredential(Base):
    """The single admin login set by the first-run /setup screen.

    One row only — the ``singleton`` column is a constant TRUE with a UNIQUE
    constraint, so a second INSERT fails at the DB layer (defence-in-depth on
    top of the repo's already-configured guard). The plaintext password is
    NEVER stored — only the salted PBKDF2 hash (app/api/auth.py). The session
    secret signs the auth cookie. Both live ONLY here in the DB, never in the
    repo or .env.
    """

    __tablename__ = "dashboard_credentials"
    __table_args__ = (UniqueConstraint("singleton", name="uq_dashboard_credentials_singleton"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    singleton: Mapped[bool] = mapped_column(server_default="true")
    username: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(256))
    session_secret: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(onupdate=func.now())


class EventSourceLink(Base):
    """Persisted cross-source event identity: one row per CONFIRMED link between
    a canonical (OddsPortal) event and a per-source stable id (Pinnacle arcadia
    matchup id, Betfair API event id). Seeded from live matcher confirmations —
    the matcher itself stays authoritative and is NOT gated by this table
    (observability: makes matches stable, auditable, and reviewable). Re-links
    upsert in place (matched_at/confidence refresh); ``active`` is a soft-delete
    for a human reviewer to retire a wrong link without losing the audit trail.
    Never breaks anchor resolution: writers wrap failures and log type-only."""

    __tablename__ = "event_source_links"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_event_id",
            "canonical_event_id",
            name="uq_event_source_links_source_event",
        ),
        Index("idx_event_source_links_canonical", "canonical_event_id"),
        # _get_or_create_event's Stage-0b redirect and Stage-1 link fast-path
        # filter on source_event_id ALONE (per event, per persist cycle); the
        # composite unique key leads on `source`, so without this dedicated
        # index every event upsert seq-scans a monotonically growing table.
        Index("idx_event_source_links_source_event_id", "source_event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    canonical_event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    source: Mapped[str] = mapped_column(String(32))  # 'pinnacle_arcadia' | 'betfair_api'
    source_event_id: Mapped[str] = mapped_column(String(128))  # per-source stable id
    source_market_id: Mapped[str | None] = mapped_column(String(64))  # e.g. Betfair market_id
    confidence_score: Mapped[Decimal] = mapped_column(PROB)  # matcher min-side JW in [0,1]
    match_method: Mapped[str] = mapped_column(String(32))  # exact_canonical/jw_two_tier/slug_*
    matched_at: Mapped[datetime]  # last time the live matcher confirmed this link
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    # Raw source-side identity at match time (audit trail for a human reviewer).
    raw_sport: Mapped[str | None] = mapped_column(String(64))
    raw_league: Mapped[str | None] = mapped_column(String(128))
    raw_home: Mapped[str | None] = mapped_column(String(128))
    raw_away: Mapped[str | None] = mapped_column(String(128))
    raw_start_time_utc: Mapped[datetime | None]
    evidence_json: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MatchReviewQueue(Base):
    """Borderline matcher REJECTS captured for human review — a TAP on the
    hardened matcher's silently-discarded bands (JW review band 0.84<=JW<0.92,
    token-sort misses, ambiguity-margin and kickoff-window rejects). The match
    itself still fails exactly as before: this queue is observability, NEVER a
    gate, and recovery of a near-miss goes through reviewed per-club aliases
    (aliases_seed.json), never a threshold drop. Idempotent by the unique key,
    so re-running the matcher enqueues each (source event, candidate, reason)
    once (ON CONFLICT DO NOTHING at write time)."""

    __tablename__ = "match_review_queue"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_event_id",
            "candidate_canonical_event_id",
            "reason",
            name="uq_match_review_queue_dedupe",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    source_event_id: Mapped[str] = mapped_column(String(128))
    source_market_id: Mapped[str | None] = mapped_column(String(64))
    candidate_canonical_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    confidence_score: Mapped[Decimal] = mapped_column(PROB)  # rejected candidate's min-side JW
    reason: Mapped[str] = mapped_column(String(64))  # jw_below_accept / ambiguity_margin / ...
    evidence_json: Mapped[dict[str, Any] | None]  # name forms + scores + kickoff delta
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    reviewed_at: Mapped[datetime | None]
    review_status: Mapped[str] = mapped_column(String(16), server_default="pending")


class BetfairAnchorVerdict(Base):
    """Latest Betfair staleness-guard verdict per (event, market, selection).

    Written by the read-only Betfair-API capture's verdict sink (a best-effort
    tap on the existing per-cycle compare — app/ingestion/betfair_api.py):
    each compared selection upserts ONE row (keep-latest per key), so the table
    stays at ~slate size instead of append-only growth; a retention sweep in
    the sink drops rows older than the retention window. The pick pipeline
    reads the latest FRESH verdicts per event at mint (verdict_loader) — the
    mint path NEVER calls the Betfair API. The row is simultaneously the
    compare persistence, the mint-time input, and the provenance/diagnostic
    record (P3/P4, staleness-guard package 2026-07-02).

    ``decision`` is write-time: pass | demote | no_api_match | no_api_price
    (stale_api is a READ-time classification — age > TTL — never stored).
    """

    __tablename__ = "betfair_anchor_verdicts"
    __table_args__ = (
        UniqueConstraint(
            "event_ref",
            "market",
            "selection_role",
            name="uq_betfair_anchor_verdicts_selection",
        ),
        Index("idx_betfair_anchor_verdicts_api_captured", "api_captured_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Canonical OddsPortal match URL — same width as events.external_ref (the
    # value it references by name; deliberately NOT an FK so a verdict write can
    # never fail on event churn and the sink stays a tap).
    event_ref: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(64), server_default="h2h")
    selection_role: Mapped[str] = mapped_column(String(16))  # home | draw | away
    inline_price: Mapped[Decimal | None] = mapped_column(ODDS)  # scrape reference back
    api_price: Mapped[Decimal | None] = mapped_column(ODDS)
    api_best_back_size: Mapped[Decimal | None] = mapped_column(MONEY)  # £ @ best back
    tick_diff: Mapped[Decimal | None] = mapped_column(METRIC)  # |delta| / coarser tick
    inline_captured_at: Mapped[datetime | None]
    api_captured_at: Mapped[datetime]
    decision: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PickLineDrift(Base):
    """Append-only time-series of a pick's vig-free fair line drift, bet-time ->
    close (build #6 / plan C8). One row per (pick, re-price observation): the
    de-vigged FAIR probability at that moment + the implied CLV-so-far vs the
    pick's fill. The ``picks`` row keeps only a SINGLE close snapshot
    (closing_fair_probability / clv_log); this preserves the whole drift PATH so
    good/bad-variance attribution + steam analysis become possible. ADDITIVE —
    never touches picks/odds_snapshots; written ONLY when CLV_RECORD_DRIFT is on
    (default OFF), so the table simply stays empty until the flag is enabled."""

    __tablename__ = "pick_line_drift"
    __table_args__ = (Index("idx_pick_line_drift_pick", "pick_id", "captured_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    pick_id: Mapped[int] = mapped_column(ForeignKey("picks.id"))
    captured_at: Mapped[datetime]  # provider-reported time of this observation
    fair_probability: Mapped[Decimal] = mapped_column(PROB)
    fair_odds: Mapped[Decimal | None] = mapped_column(ODDS)
    clv_log: Mapped[Decimal | None] = mapped_column(METRIC)
    anchor_type: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CandidateEvaluation(Base):
    """Per-candidate audit trail for the value pipeline (external-audit #3).

    Today the pipeline only keeps AGGREGATE counters (n_stale, n_no_sharp_demoted,
    ...), so a false-positive spike, a freshness drop, a book miss, or a tier
    demotion cannot be diagnosed after the fact. This table records ONE row per
    candidate evaluation per cycle: what tier it landed in (``premium`` kept vs
    ``volume`` demoted), WHY (the demotion/rejection reason slugs the pipeline
    already distinguishes, in ``reasons``), and the anchor/fill provenance behind
    the decision. Pure MEASUREMENT infrastructure — never gates minting, never
    changes a pick. See ``app/storage/candidate_audit.py`` for the writer and the
    documented reason vocabulary.

    Idempotent by (event, market, market_detail, selection, evaluated_at): the
    pipeline's per-cycle ``now`` is the cycle discriminator, so one call per
    candidate per cycle can never double-insert (ON CONFLICT DO NOTHING at write
    time). ``market_detail`` is NOT NULL ('' when the market has no line suffix) —
    a NULL would read as distinct in the unique index and defeat that dedup (the
    leagues.country lesson). Standalone tap like event_source_links /
    match_review_queue: no FK to model_versions/model_predictions, so an audit
    write can never fail on pipeline churn. Additive only — leaves the RESERVED
    DetectedEdge (model-strategy shaped) untouched for its intended use."""

    __tablename__ = "candidate_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "market",
            "market_detail",
            "selection",
            "evaluated_at",
            name="uq_candidate_evaluations_cycle",
        ),
        Index("idx_candidate_evaluations_event", "event_id", "evaluated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    sport_key: Mapped[str] = mapped_column(String(64))  # sports.key form, e.g. "soccer"
    market: Mapped[str] = mapped_column(String(64))  # matches odds_snapshots.market width
    # '' when the market has no line suffix (NOT NULL — see class docstring).
    market_detail: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    selection: Mapped[str] = mapped_column(String(64))
    # Decision tier: 'premium' (kept — alerted, reserves exposure) or 'volume'
    # (demoted/shadow — persisted + CLV-tracked only). Same vocabulary as picks.tier.
    tier: Mapped[str] = mapped_column(String(16))
    # Demotion/rejection reason slugs, {"reasons": [...]} (DetectedEdge.reject_reasons
    # shape). NULL/absent = a clean premium keep (no gate tripped).
    reasons: Mapped[dict[str, Any] | None]
    # --- anchor + fill provenance behind the decision (all nullable) ------------
    anchor_book: Mapped[str | None] = mapped_column(String(64))
    anchor_type: Mapped[str | None] = mapped_column(String(16))  # pinnacle|sharp|consensus
    anchor_age_seconds: Mapped[Decimal | None] = mapped_column(METRIC)
    anchor_liquidity: Mapped[Decimal | None] = mapped_column(MONEY)
    best_book: Mapped[str | None] = mapped_column(String(64))  # best fill book
    best_odds: Mapped[Decimal | None] = mapped_column(ODDS)
    edge: Mapped[Decimal | None] = mapped_column(METRIC)
    fair_probability: Mapped[Decimal | None] = mapped_column(PROB)
    # The pipeline's per-cycle timestamp — the idempotency discriminator.
    evaluated_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
