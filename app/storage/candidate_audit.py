"""Standalone writer for the candidate/rejection audit trail (external-audit #3).

Pure MEASUREMENT infrastructure: records one row per candidate evaluation per
cycle in ``candidate_evaluations`` (decision tier + demotion/rejection reasons +
anchor/fill provenance), so later ROI diagnosis can tune false positives,
freshness drops, book misses, and tier demotions. It NEVER gates minting and
NEVER changes a pick — the pipeline wires this writer in a separate change.

Idempotent: safe to call once per candidate per cycle. The insert does
ON CONFLICT DO NOTHING on ``uq_candidate_evaluations_cycle`` (event, market,
market_detail, selection, evaluated_at), so a retried cycle can never
double-insert. Transaction control (commit/rollback) is the CALLER's — this
writer only stages the INSERT on the passed session.

Reason vocabulary (``CandidateEvaluationInput.reasons``) — the demotion/rejection
reasons ``run_value_pipeline`` already distinguishes as per-cycle counters:

  Demotions premium -> volume (shadow):
    - "visibility_only"    n_visibility_capped  (visibility-only market cap)
    - "odds_ceiling"       n_moneyline_capped   (1X2 longshot > moneyline_max_odds)
    - "non_major_league"   n_major_demoted      (league outside the major set)
    - "no_sharp_anchor"    n_no_sharp_demoted   (consensus median, no sharp book)
    - "experimental_sport" n_experimental       (unvalidated sport)
    - "ml_filter"          n_ml_demoted         (value-filter score < q*)
    - "steam"              n_steam_demoted      (line-movement/steam gate)
    - "structural_sanity"  n_sanity_demoted     (impossible fair/offered pair)

  Hard rejects (candidate never minted, so no pick row today):
    - "stale"              n_stale              (odds age > freshness window;
                                                 the "freshness_drop" case)
    - "off_band"           n_off_band           (raw odds outside configured band)
    - "thin_books"         n_thin_books         (too few books; the "book_miss" case)
    - "ah_implausible"     n_ah_rejected        (AH sentinel/implausibility guard)
    - "dc_implausible"     n_dc_rejected        (double-chance implausibility guard)

``reasons`` is stored free-form (any slug list is accepted) so the pipeline
wiring stays flexible; this frozenset only DOCUMENTS the known slugs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity import (
    BOOKMAKER_MAX_BYTES,
    MARKET_DETAIL_MAX_BYTES,
    SELECTION_MAX_BYTES,
    SPORT_KEY_MAX_BYTES,
    require_bounded_identity,
)
from app.storage.models import CandidateEvaluation

#: Known reason slugs the value pipeline distinguishes (documentation only — the
#: writer accepts any slug so wiring is not blocked on this staying in sync).
CANDIDATE_EVALUATION_REASONS: frozenset[str] = frozenset(
    {
        "visibility_only",
        "odds_ceiling",
        "non_major_league",
        "no_sharp_anchor",
        "experimental_sport",
        "ml_filter",
        "steam",
        "structural_sanity",
        "stale",
        "off_band",
        "thin_books",
        "ah_implausible",
        "dc_implausible",
    }
)


@dataclass(frozen=True, slots=True)
class CandidateEvaluationInput:
    """One candidate's evaluation for the audit trail. Decimal for odds/edge/
    liquidity/probability at the boundary (project rule: float only in numpy
    kernels). ``evaluated_at`` is the pipeline's per-cycle timestamp and MUST be
    UTC-aware. Empty ``reasons`` = a clean premium keep."""

    event_id: int
    sport_key: str
    market: str
    selection: str
    tier: str  # 'premium' (kept) | 'volume' (demoted/shadow)
    evaluated_at: datetime  # cycle timestamp (UTC-aware) — idempotency discriminator
    market_detail: str = ""  # '' when the market has no line suffix
    reasons: tuple[str, ...] = ()
    anchor_book: str | None = None
    anchor_type: str | None = None  # pinnacle | sharp | consensus
    anchor_age_seconds: Decimal | None = None
    anchor_liquidity: Decimal | None = None
    best_book: str | None = None
    best_odds: Decimal | None = None
    edge: Decimal | None = None
    fair_probability: Decimal | None = None

    def __post_init__(self) -> None:
        require_bounded_identity(
            self.sport_key,
            maximum_bytes=SPORT_KEY_MAX_BYTES,
            field="candidate sport_key",
        )
        require_bounded_identity(
            self.market,
            maximum_bytes=64,
            field="candidate market",
        )
        require_bounded_identity(
            self.market_detail,
            maximum_bytes=MARKET_DETAIL_MAX_BYTES,
            field="candidate market_detail",
            allow_empty=True,
        )
        require_bounded_identity(
            self.selection,
            maximum_bytes=SELECTION_MAX_BYTES,
            field="candidate selection",
        )
        for field, value in (
            ("candidate anchor_book", self.anchor_book),
            ("candidate best_book", self.best_book),
        ):
            if value is not None:
                require_bounded_identity(
                    value,
                    maximum_bytes=BOOKMAKER_MAX_BYTES,
                    field=field,
                )


async def record_candidate_evaluation(
    session: AsyncSession, evaluation: CandidateEvaluationInput
) -> None:
    """Stage one idempotent audit-trail INSERT on ``session`` (caller commits)."""
    reasons_payload = {"reasons": list(evaluation.reasons)} if evaluation.reasons else None
    stmt = (
        pg_insert(CandidateEvaluation)
        .values(
            event_id=evaluation.event_id,
            sport_key=evaluation.sport_key,
            market=evaluation.market,
            market_detail=evaluation.market_detail,
            selection=evaluation.selection,
            tier=evaluation.tier,
            reasons=reasons_payload,
            anchor_book=evaluation.anchor_book,
            anchor_type=evaluation.anchor_type,
            anchor_age_seconds=evaluation.anchor_age_seconds,
            anchor_liquidity=evaluation.anchor_liquidity,
            best_book=evaluation.best_book,
            best_odds=evaluation.best_odds,
            edge=evaluation.edge,
            fair_probability=evaluation.fair_probability,
            evaluated_at=evaluation.evaluated_at,
        )
        .on_conflict_do_nothing(constraint="uq_candidate_evaluations_cycle")
    )
    await session.execute(stmt)
