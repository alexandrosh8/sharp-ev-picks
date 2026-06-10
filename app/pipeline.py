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
from typing import TYPE_CHECKING

from app.backtesting import clv as _clv  # noqa: F401  (settlement uses this module)
from app.edge.gates import GatePolicy, PickCandidate, evaluate
from app.ingestion.base import EventDirectory, OddsLoader
from app.models.base import ProbabilityModel
from app.notifications.base import build_pick_alert
from app.notifications.dispatcher import AlertDispatcher
from app.probabilities.devig import DevigMethod, devig
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import StakePolicy, recommended_stake, stake_amount
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker


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
    directory: EventDirectory | None = None  # resolves event_id -> readable "Home vs Away"
    session_factory: "async_sessionmaker | None" = None  # set => persist picks to DB
    model_name: str = "model"
    model_version: str = "0"
    # value-strategy thresholds (run_value_pipeline)
    value_min_edge: float = 0.015
    value_min_odds: float = 1.30


async def run_pick_pipeline(deps: PipelineDeps, sport_key: str) -> list[PickOut]:
    """One polling cycle. Returns the accepted picks (alerts already sent)."""
    snapshots = await deps.loader.fetch_odds(sport_key)
    # `now` AFTER the fetch: live scrapes take minutes and stamp captured_at
    # during the run — taking now first yields negative odds ages.
    now = datetime.now(tz=UTC)
    if not snapshots:
        logger.info("no snapshots for %s", sport_key)
        return []

    fair = _fair_probabilities(snapshots, deps.devig_method)
    picks: list[PickOut] = []

    for event_id in sorted({s.event_id for s in snapshots}):
        predictions = {(p.market, p.selection): p for p in await deps.model.predict(event_id)}
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

            event_label = snap.event_id
            if deps.directory is not None:
                teams = deps.directory.lookup(snap.event_id)
                if teams is not None:
                    event_label = f"{teams.home} vs {teams.away}"

            pick = PickOut(
                pick_id=str(uuid.uuid4()),
                sport=deps.sport,
                league=deps.league or sport_key,
                event=event_label,
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
                odds_age_seconds=max(candidate.odds_age_seconds, 0.0),
                liquidity=snap.liquidity,
                reason_summary=(
                    f"model {prediction.probability:.3f} vs fair {fair_p:.3f} "
                    f"({deps.devig_method}) at {snap.bookmaker}"
                ),
                created_at=now,
            )
            picks.append(pick)
            await _maybe_persist(deps, pick, snap.event_id)
            await deps.dispatcher.dispatch(build_pick_alert(pick))

    logger.info("pipeline cycle for %s: %d picks", sport_key, len(picks))
    return picks


async def _maybe_persist(deps: "PipelineDeps", pick: PickOut, event_id: str) -> None:
    """Persist the pick to the DB when a session factory + directory are set."""
    if deps.session_factory is None or deps.directory is None:
        return
    teams = deps.directory.lookup(event_id)
    if teams is None:
        return
    from app.storage.repositories import persist_pick

    try:
        async with deps.session_factory() as session:
            await persist_pick(session, pick, teams, deps.model_name, deps.model_version)
            await session.commit()
    except Exception as exc:  # persistence must never break alerting
        logger.error("pick persistence failed for %s: %s", pick.pick_id, type(exc).__name__)


async def run_value_pipeline(deps: PipelineDeps, sport_key: str) -> list[PickOut]:
    """One polling cycle of the VALIDATED strategy (sharp-vs-soft value,
    docs/backtesting/value-findings.md): group multi-book odds per market,
    anchor fair value on the sharpest book, flag better prices elsewhere.

    No prediction model involved; deps.model is unused here.
    """
    from app.edge.value import CONSENSUS_ANCHOR, find_value_bets

    snapshots = await deps.loader.fetch_odds(sport_key)
    # `now` AFTER the fetch — see run_pick_pipeline comment (negative ages).
    now = datetime.now(tz=UTC)
    if not snapshots:
        logger.info("no snapshots for %s", sport_key)
        return []

    picks: list[PickOut] = []
    for (event_id, market), (prices, captured) in group_market_prices(snapshots).items():
        for v in find_value_bets(
            prices,
            min_edge=deps.value_min_edge,
            min_odds=deps.value_min_odds,
            devig_method=deps.devig_method,
        ):
            cap = captured.get((v.selection, v.best_book))
            age = max((now - cap).total_seconds(), 0.0) if cap else 0.0
            if age > deps.gate_policy.max_odds_age_seconds:
                continue
            # Stake from the sharp fair prob at the EFFECTIVE (net) price.
            breakdown = recommended_stake(
                v.sharp_fair_prob, v.best_odds_effective, deps.stake_policy
            )
            granted = deps.ledger.reserve(now.date(), breakdown.final)
            if granted <= 0.0:
                logger.info("daily exposure cap reached; skipping %s", v.selection)
                continue
            # Named sharp anchors are backtested; consensus anchors are the
            # fallback path with weaker evidence — reflected in confidence.
            confidence = 0.7 if v.sharp_book == CONSENSUS_ANCHOR else 0.9

            event_label = event_id
            if deps.directory is not None:
                teams = deps.directory.lookup(event_id)
                if teams is not None:
                    event_label = f"{teams.home} vs {teams.away}"

            pick = PickOut(
                pick_id=str(uuid.uuid4()),
                sport=deps.sport,
                league=deps.league or sport_key,
                event=event_label,
                event_id=event_id,
                market=market,
                selection=v.selection,
                bookmaker=v.best_book,
                decimal_odds=v.best_odds,
                model_probability=v.sharp_fair_prob,
                fair_probability=v.implied_prob,
                edge=v.edge,
                ev=v.ev,
                confidence=confidence,
                recommended_stake_fraction=granted,
                recommended_stake_amount=stake_amount(granted, deps.bankroll),
                stake_breakdown=StakeBreakdownOut(
                    raw_kelly=breakdown.raw_kelly,
                    fractional=breakdown.fractional,
                    capped=breakdown.capped,
                    final=granted,
                ),
                odds_age_seconds=age,
                liquidity=None,
                reason_summary=(
                    f"value: {v.sharp_book} fair {v.sharp_fair_prob:.3f} vs "
                    f"{v.best_book} {v.best_odds:.2f}"
                    + (
                        f" (eff {v.best_odds_effective:.2f} after commission)"
                        if v.best_odds_effective != v.best_odds
                        else ""
                    )
                ),
                created_at=now,
            )
            picks.append(pick)
            await _maybe_persist(deps, pick, event_id)
            await deps.dispatcher.dispatch(build_pick_alert(pick))

    logger.info("value pipeline cycle for %s: %d picks", sport_key, len(picks))
    return picks


GroupedMarkets = dict[
    tuple[str, Market],
    tuple[dict[str, dict[str, float]], dict[tuple[str, str], datetime]],
]


def group_market_prices(snapshots: Sequence[OddsSnapshotIn]) -> GroupedMarkets:
    """Group snapshots into {(event_id, market): (selection->{book: odds},
    (selection, book)->captured_at)} for the value finder and CLV true-up."""
    out: GroupedMarkets = {}
    for snap in snapshots:
        prices, captured = out.setdefault((snap.event_id, snap.market), ({}, {}))
        prices.setdefault(snap.selection, {})[snap.bookmaker] = snap.decimal_odds
        captured[(snap.selection, snap.bookmaker)] = snap.captured_at
    return out


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
