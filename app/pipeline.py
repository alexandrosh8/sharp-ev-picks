"""Pick pipeline: snapshots -> devig -> model join -> gates -> stake -> alert.

Composition layer: pure math stays in app/probabilities|edge|risk; this module
wires it to IO (loader, dispatcher). Persistence of picks/edges to Postgres
joins in roadmap phase 2 alongside event/entity resolution.
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.backtesting import clv as _clv  # noqa: F401  (settlement uses this module)
from app.edge.gates import GatePolicy, PickCandidate, evaluate
from app.ingestion.base import OddsLoader
from app.models.base import ProbabilityModel
from app.notifications.base import build_pick_alert
from app.notifications.dispatcher import AlertDispatcher
from app.probabilities.devig import DevigMethod, devig
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy, recommended_stake, stake_amount
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut

logger = logging.getLogger(__name__)


@dataclass
class PipelineDeps:
    loader: OddsLoader
    model: ProbabilityModel
    dispatcher: AlertDispatcher
    gate_policy: GatePolicy
    stake_policy: StakePolicy
    ledger: DailyExposureLedger
    bankroll: Decimal
    devig_method: DevigMethod = DevigMethod.POWER
    sport: str = "soccer"
    league: str = ""


async def run_pick_pipeline(deps: PipelineDeps, sport_key: str) -> list[PickOut]:
    """One polling cycle. Returns the accepted picks (alerts already sent)."""
    now = datetime.now(tz=UTC)
    snapshots = await deps.loader.fetch_odds(sport_key)
    if not snapshots:
        logger.info("no snapshots for %s", sport_key)
        return []

    fair = _fair_probabilities(snapshots, deps.devig_method)
    picks: list[PickOut] = []

    for event_id in sorted({s.event_id for s in snapshots}):
        predictions = {
            (p.market, p.selection): p for p in await deps.model.predict(event_id)
        }
        if not predictions:
            continue
        for snap in (s for s in snapshots if s.event_id == event_id):
            prediction = predictions.get((snap.market, snap.selection))
            fair_p = fair.get((snap.event_id, snap.bookmaker, snap.market, snap.selection))
            if prediction is None or fair_p is None:
                continue
            candidate = PickCandidate(
                event_id=snap.event_id,
                market=str(snap.market),
                selection=snap.selection,
                decimal_odds=snap.decimal_odds,
                model_probability=prediction.probability,
                fair_probability=fair_p,
                confidence=prediction.confidence,
                odds_age_seconds=snap.age_seconds(now),
                liquidity=snap.liquidity or 0.0,
            )
            decision = evaluate(candidate, deps.gate_policy)
            if not decision.accepted:
                continue

            breakdown = recommended_stake(
                prediction.probability, snap.decimal_odds, deps.stake_policy
            )
            granted = deps.ledger.reserve(now.date(), breakdown.final)
            if granted <= 0.0:
                logger.info("daily exposure cap reached; skipping %s", snap.selection)
                continue

            pick = PickOut(
                pick_id=str(uuid.uuid4()),
                sport=deps.sport,
                league=deps.league or sport_key,
                event=snap.event_id,
                event_id=snap.event_id,
                market=snap.market,
                selection=snap.selection,
                bookmaker=snap.bookmaker,
                decimal_odds=snap.decimal_odds,
                model_probability=prediction.probability,
                fair_probability=fair_p,
                edge=decision.edge,
                ev=decision.ev,
                confidence=prediction.confidence,
                recommended_stake_fraction=granted,
                recommended_stake_amount=stake_amount(granted, deps.bankroll),
                stake_breakdown=StakeBreakdownOut(
                    raw_kelly=breakdown.raw_kelly,
                    fractional=breakdown.fractional,
                    capped=breakdown.capped,
                    final=granted,
                ),
                odds_age_seconds=candidate.odds_age_seconds,
                liquidity=snap.liquidity,
                reason_summary=(
                    f"model {prediction.probability:.3f} vs fair {fair_p:.3f} "
                    f"({deps.devig_method}) at {snap.bookmaker}"
                ),
                created_at=now,
            )
            picks.append(pick)
            await deps.dispatcher.dispatch(build_pick_alert(pick))

    logger.info("pipeline cycle for %s: %d picks", sport_key, len(picks))
    return picks


def _fair_probabilities(
    snapshots: Sequence[OddsSnapshotIn],
    method: DevigMethod,
) -> dict[tuple[str, str, str, str], float]:
    """Devig each (event, bookmaker, market) book into fair probabilities."""
    books: dict[tuple[str, str, str], list[OddsSnapshotIn]] = defaultdict(list)
    for snap in snapshots:
        books[(snap.event_id, snap.bookmaker, snap.market)].append(snap)

    fair: dict[tuple[str, str, str, str], float] = {}
    for (event_id, bookmaker, market), legs in books.items():
        if len(legs) < 2:
            continue  # cannot devig a one-sided book
        probs = devig([leg.decimal_odds for leg in legs], method=method)
        for leg, p in zip(legs, probs, strict=True):
            fair[(event_id, bookmaker, market, leg.selection)] = p
    return fair
