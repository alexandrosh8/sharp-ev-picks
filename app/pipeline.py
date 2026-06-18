"""Pick pipeline: snapshots -> devig -> model join -> gates -> stake -> alert.

Composition layer: pure math stays in app/probabilities|edge|risk; this module
wires it to IO (loader, dispatcher). Persistence of picks/edges to Postgres
joins in roadmap phase 2 alongside event/entity resolution.
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from app.backtesting import clv as _clv  # noqa: F401  (settlement uses this module)
from app.edge.gates import GatePolicy, PickCandidate, evaluate
from app.edge.value_policy import (
    ValuePolicy,
    distinct_book_count,
    min_books_for,
    min_edge_for,
    odds_in_bands,
)
from app.ingestion.base import EventDirectory, EventTeams, OddsLoader
from app.models.base import ProbabilityModel
from app.models.value_filter import ValueFilterModel, live_features
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

# Liveness registry, surfaced by GET /health and the dashboard banner: the
# difference between "engine alive, no new value found" and "engine dead,
# showing day-old picks" must be visible. In-memory; repopulated each cycle.
LAST_POLL: dict[str, dict[str, Any]] = {}

# Latest unrestricted fixture view, surfaced by GET /games and the dashboard.
# It is intentionally separate from picks: games list what the read-only odds
# poll saw; picks remain the post-edge-gate recommendation stream.
AVAILABLE_GAMES: dict[str, list[dict[str, Any]]] = {}

# --- change-only odds-snapshot persistence ----------------------------------
# Last odds written to odds_snapshots per (event_ref, bookmaker, line-
# qualified market, selection) -> (decimal_odds, last_seen UTC). Process-
# local by design: after a restart the cache is cold and ONE extra
# (unchanged) row per live key is written — accepted, documented in
# docs/db-schema.md. Bounded: when it exceeds ODDS_SEEN_MAX entries, keys
# not seen for ODDS_SEEN_TTL are swept, then oldest-seen down to the cap.
ODDS_SEEN_TTL = timedelta(days=3)
ODDS_SEEN_MAX = 100_000

OddsSeenCache = dict[tuple[str, str, str, str], tuple[float, datetime]]


def _sweep_odds_seen(cache: OddsSeenCache, now: datetime, max_size: int = ODDS_SEEN_MAX) -> None:
    """Bound the last-seen cache: a no-op under max_size; above it, evict
    TTL-stale entries first, then oldest-seen until back at the cap. An
    evicted live key just re-writes one unchanged row — same cost as a
    restart, so eviction is always safe, never lossy."""
    if len(cache) <= max_size:
        return
    cutoff = now - ODDS_SEEN_TTL
    for key in [k for k, (_, seen) in cache.items() if seen < cutoff]:
        del cache[key]
    overflow = len(cache) - max_size
    if overflow > 0:
        for key, _ in sorted(cache.items(), key=lambda kv: kv[1][1])[:overflow]:
            del cache[key]


def _record_poll(
    sport_key: str,
    snapshots: Sequence[OddsSnapshotIn],
    picks: int,
    matches_found: int | None,
    snapshots_persisted: int | None = None,
    volume_picks: int = 0,
    stale_candidates: int = 0,
) -> None:
    per_market: dict[str, int] = {}
    for snap in snapshots:
        key = snap.market_detail or str(snap.market)
        per_market[key] = per_market.get(key, 0) + 1
    LAST_POLL[sport_key] = {
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "snapshots": len(snapshots),
        # PREMIUM picks only — the alerted tier the operator acts on. The
        # shadow tier rides separately in volume_picks so it can never
        # inflate the headline cycle count.
        "picks": picks,
        "volume_picks": volume_picks,
        # None = the loader does not report listing counts (e.g. odds_api).
        "matches_found": matches_found,
        # Per-market counts: a selector break craters ONE market's count
        # while cycles keep completing — the dashboard can show which.
        "per_market": per_market,
        # NEW odds rows appended to odds_snapshots this cycle (change-only).
        # None = persistence is off (no DB) or this cycle's write failed.
        "snapshots_persisted": snapshots_persisted,
        # Value candidates silently lost to the odds-age gate this cycle:
        # nonzero means the scrape outlasted MAX_ODDS_AGE_SECONDS — the
        # cycle is too slow for its slate (trim markets/leagues, raise
        # concurrency). Surfaced so a slate collapse is visible, not silent.
        "stale_candidates": stale_candidates,
        # Listings parsed but ZERO odds rows: selector/DOM break or anti-bot
        # wall. finished_at alone would look healthy — flag it explicitly.
        "degraded": bool(matches_found) and not snapshots,
    }


def _loader_event_ids(loader: OddsLoader, sport_key: str) -> tuple[str, ...] | None:
    """Event ids from the loader's last fetch when it reports them.

    OddsPortal reports listed fixtures even when every requested odds market
    parses empty, allowing /games to show "0 snapshots" rows instead of
    pretending the slate vanished.
    """
    events = getattr(loader, "last_fetch_event_ids", None)
    if not isinstance(events, dict):
        return None
    value = events.get(sport_key)
    if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


def _sport_label(sport_key: str) -> str:
    """Human label for the AVAILABLE GAMES view (mirrors storage._sport_label)."""
    if sport_key.startswith("soccer"):
        return "Football"
    if sport_key.startswith("basketball"):
        return "NBA"
    if sport_key.startswith("tennis"):
        return "Tennis"
    if sport_key.startswith("american_football"):
        return "NFL"
    return sport_key


def _record_available_games(
    sport_key: str,
    snapshots: Sequence[OddsSnapshotIn],
    loader: OddsLoader,
    directory: EventDirectory | None,
    default_league: str,
    now: datetime,
    unvalidated: bool = False,
) -> None:
    """Publish every listed game from the latest poll, independent of picks.

    `unvalidated=True` tags every row of a VISIBILITY-ONLY sport (e.g. tennis):
    the sport is scraped for this view but has NOT cleared the doctrine CLV
    gate, so it mints no picks/alerts. The dashboard badges these rows; the
    flag is the single source of truth that a row is informational only.
    """
    snapshots_by_event: dict[str, list[OddsSnapshotIn]] = defaultdict(list)
    for snap in snapshots:
        snapshots_by_event[snap.event_id].append(snap)

    event_ids = _loader_event_ids(loader, sport_key)
    if event_ids is None:
        event_ids = tuple(sorted(snapshots_by_event))

    known = directory.snapshot() if directory is not None else {}
    rows: list[dict[str, Any]] = []
    for event_id in dict.fromkeys(event_ids):
        snaps = snapshots_by_event.get(event_id, [])
        teams = known.get(event_id)
        if teams is not None:
            event_label = f"{teams.home} vs {teams.away}"
            league = teams.league or default_league or sport_key
            starts_at = teams.starts_at
            home = teams.home
            away = teams.away
        else:
            event_label = event_id
            league = default_league or sport_key
            starts_at = None
            home = None
            away = None

        markets = sorted({snap.market_detail or str(snap.market) for snap in snaps})
        bookmakers = sorted({snap.bookmaker for snap in snaps})
        captured = [snap.captured_at for snap in snaps]
        rows.append(
            {
                "sport": sport_key,
                "sport_label": _sport_label(sport_key),
                "event_id": event_id,
                "event": event_label,
                "home": home,
                "away": away,
                "league": league,
                "starts_at": starts_at.isoformat() if starts_at is not None else None,
                "market_count": len(markets),
                "markets": markets,
                "bookmaker_count": len(bookmakers),
                "bookmakers": bookmakers,
                "snapshot_count": len(snaps),
                "first_captured_at": min(captured).isoformat() if captured else None,
                "last_captured_at": max(captured).isoformat() if captured else None,
                "updated_at": now.isoformat(),
                # VISIBILITY-ONLY sports (e.g. tennis) carry no validated edge;
                # the dashboard badges these rows UNVALIDATED. Always present so
                # consumers can rely on the key (False for football/basketball).
                "unvalidated": unvalidated,
            }
        )

    def sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
        starts = row["starts_at"]
        return (1 if starts is None else 0, starts or "", str(row["event"]))

    AVAILABLE_GAMES[sport_key] = sorted(rows, key=sort_key)


def _loader_matches_found(loader: OddsLoader, sport_key: str) -> int | None:
    """Listing count from the loader's last fetch, when it reports one
    (OddsPortalLoader.last_fetch_matches). Duck-typed so OddsLoader stays a
    minimal protocol and loaders without the attribute keep working."""
    counts = getattr(loader, "last_fetch_matches", None)
    if isinstance(counts, dict):
        value = counts.get(sport_key)
        if isinstance(value, int):
            return value
    return None


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
    league: str = ""
    directory: EventDirectory | None = None  # resolves event_id -> readable "Home vs Away"
    session_factory: "async_sessionmaker | None" = None  # set => persist picks to DB
    model_name: str = "model"
    model_version: str = "0"
    # value-strategy thresholds (run_value_pipeline). value_min_edge gates
    # the PREMIUM tier (alert + exposure); value_volume_min_edge gates the
    # VOLUME shadow tier (persist + CLV-revalidate only). Equal values
    # disable the volume tier — the defaults keep it off unless the
    # composition root (Settings) opens a gap between them.
    value_min_edge: float = 0.015
    value_volume_min_edge: float = 0.015
    value_min_odds: float = 1.30
    # OPTIONAL value-gate refinements (app/edge/value_policy.py): per-market
    # premium floors, raw-odds bands, per-market min book counts. The default
    # all-empty policy is a strict no-op — current behavior, untouched. Built
    # from Settings at the composition root only (app/config.value_policy);
    # evidence requirements before enabling any knob live in
    # docs/backtesting/value-findings.md (spent-holdout discipline).
    value_policy: ValuePolicy = ValuePolicy()
    # value-filter meta-model (app/models/value_filter.py). When loaded,
    # every in-scope candidate gets a calibrated score annotated on its
    # pick; the score only CHANGES behavior (premium -> volume demotion
    # below the manifest's frozen operating point) when the composition
    # root also sets value_ml_filter_enabled (Settings.value_ml_filter,
    # default OFF — held-out evidence cited in app/config.py) AND the
    # loaded manifest is a true ADOPT (model.shadow False): a SHADOW-
    # CANDIDATE manifest (v2, VALUE_ML_MANIFEST_ALLOW_SHADOW) annotates
    # only and is refused for demotion both here and at the root.
    value_filter: ValueFilterModel | None = None
    value_ml_filter_enabled: bool = False
    # VISIBILITY-ONLY sport keys (e.g. {"tennis"}): scraped for the AVAILABLE
    # GAMES view ONLY. A cycle for one of these keys publishes its slate tagged
    # unvalidated=true and records the poll, but mints NO picks, sends NO
    # alerts, and touches NO exposure ledger — they have not cleared the
    # doctrine CLV gate. Default empty: football/basketball are validated.
    visibility_only_sports: frozenset[str] = frozenset()
    # change-only persistence cache (see ODDS_SEEN_* above) — one per deps,
    # i.e. per process: both sport keys share it (event refs are distinct).
    odds_seen: OddsSeenCache = field(default_factory=dict)


async def _persist_snapshots(
    deps: "PipelineDeps",
    snapshots: Sequence[OddsSnapshotIn],
    sport: str,
    default_league: str,
    now: datetime,
) -> int | None:
    """Change-only append of this cycle's odds into odds_snapshots — the
    dataset for backtests, line-movement features, and CLV verification.

    Returns NEW rows written; None when persistence is unavailable (no DB /
    no directory) or this cycle's write failed — recorded verbatim in
    LAST_POLL. Raw append-only would explode (5-20k observations per back-
    to-back cycle), so rows whose odds equal the last-seen cache are
    skipped. The cache is updated ONLY after a successful write: a failed
    batch must be retried next cycle, not silently dropped. Failure here
    never breaks pick generation.
    """
    if deps.session_factory is None or deps.directory is None:
        return None
    to_write: list[OddsSnapshotIn] = []
    seen_updates: OddsSeenCache = {}
    teams_by_event: dict[str, EventTeams] = {}
    for snap in snapshots:
        teams = teams_by_event.get(snap.event_id) or deps.directory.lookup(snap.event_id)
        if teams is None:
            continue  # unresolvable this cycle; do NOT cache — retry later
        teams_by_event[snap.event_id] = teams
        key = (
            snap.event_id,
            snap.bookmaker,
            snap.market_detail or str(snap.market),  # line-qualified market
            snap.selection,
        )
        last = seen_updates.get(key) or deps.odds_seen.get(key)
        if last is not None and last[0] == snap.decimal_odds:
            seen_updates[key] = (snap.decimal_odds, now)  # refresh recency only
            continue
        to_write.append(snap)
        seen_updates[key] = (snap.decimal_odds, now)

    from app.storage import repositories

    try:
        written = 0
        if to_write:
            written = await repositories.persist_odds_snapshots(
                deps.session_factory, to_write, teams_by_event, sport, default_league
            )
    except Exception as exc:  # snapshot history must never break picking
        logger.warning(
            "odds snapshot persistence failed (%d rows): %s",
            len(to_write),
            type(exc).__name__,
        )
        return None
    deps.odds_seen.update(seen_updates)
    _sweep_odds_seen(deps.odds_seen, now)
    return written


async def run_pick_pipeline(deps: PipelineDeps, sport_key: str) -> list[PickOut]:
    """One polling cycle. Returns the accepted picks (alerts already sent)."""
    snapshots = await deps.loader.fetch_odds(sport_key)
    # `now` AFTER the fetch: live scrapes take minutes and stamp captured_at
    # during the run — taking now first yields negative odds ages.
    now = datetime.now(tz=UTC)
    if sport_key in deps.visibility_only_sports:
        # Defense in depth: a visibility-only sport must mint no pick under
        # ANY strategy. Tennis only runs the value pipeline, but keep the
        # invariant strategy-agnostic — publish the slate (unvalidated), record
        # the poll, no picks/alerts. (No persist here: the model strategy is
        # football-only and has no visibility-only sports in practice.)
        _record_available_games(
            sport_key,
            snapshots,
            deps.loader,
            deps.directory,
            deps.league or sport_key,
            now,
            unvalidated=True,
        )
        _record_poll(sport_key, snapshots, 0, _loader_matches_found(deps.loader, sport_key))
        return []
    if not snapshots:
        logger.info("no snapshots for %s", sport_key)
        _record_available_games(
            sport_key, snapshots, deps.loader, deps.directory, deps.league or sport_key, now
        )
        _record_poll(sport_key, snapshots, 0, _loader_matches_found(deps.loader, sport_key))
        return []

    persisted = await _persist_snapshots(deps, snapshots, sport_key, deps.league or sport_key, now)
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
                sport=sport_key,
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
                # The model strategy has NO volume tier: the volume-tier
                # validation evidence (v2 holdout n=379, CLV +0.019) is
                # value-strategy-specific; every model pick is full-behavior.
                tier="premium",
                created_at=now,
            )
            outcome = await _maybe_persist(deps, pick, snap.event_id)
            if outcome == "duplicate":
                # Confirmed DB duplicate (the picks unique key ignores odds):
                # hand the exposure grant back — a re-detection is not new
                # exposure — but STILL dispatch. The alert dedupe key includes
                # decimal_odds (notifications/base.py), so an unchanged price
                # stays quiet for the idempotency TTL (7d default) while a
                # material price move re-alerts by design; and because the
                # dispatcher releases the claim when no sink delivers, an
                # alert whose dispatch failed is retried by this very
                # re-dispatch on the next cycle.
                deps.ledger.release(now.date(), granted)
            else:  # inserted / upgraded / unpersisted (uncertainty = "new")
                picks.append(pick)
            await deps.dispatcher.dispatch(
                build_pick_alert(
                    pick,
                    model_name=deps.model_name,
                    model_version=deps.model_version,
                )
            )

    logger.info("pipeline cycle for %s: %d picks", sport_key, len(picks))
    _record_available_games(
        sport_key, snapshots, deps.loader, deps.directory, deps.league or sport_key, now
    )
    _record_poll(
        sport_key,
        snapshots,
        len(picks),
        _loader_matches_found(deps.loader, sport_key),
        snapshots_persisted=persisted,
    )
    return picks


async def _refresh_kickoffs(deps: "PipelineDeps", event_ids: set[str]) -> None:
    """Upgrade stored kickoffs for every scraped event (the source reports
    the real match start; early rows carried pick-time placeholders)."""
    if deps.session_factory is None or deps.directory is None:
        return
    kickoffs = {
        event_id: teams.starts_at
        for event_id in event_ids
        if (teams := deps.directory.lookup(event_id)) is not None and teams.starts_at is not None
    }
    if not kickoffs:
        return
    from app.storage.repositories import refresh_event_kickoffs

    try:
        async with deps.session_factory() as session:
            changed = await refresh_event_kickoffs(session, kickoffs)
            await session.commit()
        if changed:
            logger.info("kickoff refresh updated %d events", changed)
    except Exception as exc:  # kickoff hygiene must never break picking
        logger.error("kickoff refresh failed: %s", type(exc).__name__)


def pick_tier(edge: float, premium_min_edge: float, volume_min_edge: float) -> str | None:
    """Tier for a candidate edge — pure boundary logic, tested directly.

    'premium' when edge >= premium_min_edge (alert + exposure reservation);
    'volume' when volume_min_edge <= edge < premium_min_edge (informational
    shadow tier); None below both floors. Floors are INCLUSIVE, matching the
    backtests' >= gates (edge exactly 0.03 is a premium pick). Equal floors
    disable the volume tier: no edge satisfies >= x and < x at once.
    """
    if edge >= premium_min_edge:
        return "premium"
    if edge >= volume_min_edge:
        return "volume"
    return None


def _score_value_candidate(
    deps: "PipelineDeps",
    event_id: str,
    market: Market,
    detail: str | None,
    selection: str,
    prices: dict[str, dict[str, float]],
    fair_by_sel: dict[str, float],
    anchor_book: str,
    sport_key: str,
    now: datetime,
) -> float | None:
    """Calibrated meta-model score for one candidate that SURVIVED the edge
    gate, or None. None means: no artifact loaded, candidate outside the
    model's trained scope (market/league/anchor/odds-floor — see
    app/models/value_filter.py), or the scorer failed (logged by exception
    type only). Scoring must never break picking.
    """
    if deps.value_filter is None:
        return None
    league = deps.league or sport_key
    kickoff = None
    if deps.directory is not None:
        teams = deps.directory.lookup(event_id)
        if teams is not None:
            kickoff = teams.starts_at
            if teams.league:  # scraped per-event league beats config csv
                league = teams.league
    try:
        feats = live_features(
            market=market,
            market_detail=detail,
            selection=selection,
            prices=prices,
            fair_by_sel=fair_by_sel,
            anchor_book=anchor_book,
            league=league,
            kickoff_utc=kickoff,
            now=now,
            min_odds=deps.value_filter.min_odds,
        )
        if feats is None:
            return None
        return deps.value_filter.score([feats])[0]
    except Exception as exc:  # scoring must never break the pick pipeline
        logger.warning("value-filter scoring failed: %s", type(exc).__name__)
        return None


PersistOutcome = Literal["inserted", "upgraded", "duplicate", "unpersisted"]


async def _maybe_persist(deps: "PipelineDeps", pick: PickOut, event_id: str) -> PersistOutcome:
    """Persist the pick to the DB when a session factory + directory are set.

    Passes through repositories.persist_pick's outcome ("inserted" /
    "upgraded" / "duplicate"); "unpersisted" means persistence was
    unavailable (no DB/directory/teams) or this write failed. PREMIUM
    callers treat "unpersisted" like "inserted" — treating uncertainty as
    "new" keeps the cap conservative and the alert flowing. VOLUME callers
    drop "unpersisted" picks instead: a shadow pick that never reaches the
    DB can accumulate no CLV evidence, which is its only purpose.
    """
    if deps.session_factory is None or deps.directory is None:
        return "unpersisted"
    teams = deps.directory.lookup(event_id)
    if teams is None:
        return "unpersisted"
    from app.storage import repositories

    try:
        async with deps.session_factory() as session:
            outcome: PersistOutcome = await repositories.persist_pick(
                session, pick, teams, deps.model_name, deps.model_version
            )
            await session.commit()
        return outcome
    except Exception as exc:  # persistence must never break alerting
        logger.error("pick persistence failed for %s: %s", pick.pick_id, type(exc).__name__)
        return "unpersisted"


async def run_value_pipeline(deps: PipelineDeps, sport_key: str) -> list[PickOut]:
    """One polling cycle of the VALIDATED strategy (sharp-vs-soft value,
    docs/backtesting/value-findings.md): group multi-book odds per market,
    anchor fair value on the sharpest book, flag better prices elsewhere.

    No prediction model involved; deps.model is unused here.
    """
    from app.edge.value import CONSENSUS_ANCHOR, anchor_type_for, find_value_bets_with_fair

    snapshots = await deps.loader.fetch_odds(sport_key)
    # `now` AFTER the fetch — see run_pick_pipeline comment (negative ages).
    now = datetime.now(tz=UTC)

    if sport_key in deps.visibility_only_sports:
        # VISIBILITY-ONLY sport (e.g. tennis): publish the slate for the
        # AVAILABLE GAMES view tagged unvalidated=true and record the poll,
        # but mint NO picks, send NO alerts, and reserve NO exposure — it has
        # not cleared the doctrine CLV gate. Snapshots are still persisted so
        # the warehouse can accumulate the data a future backtest would need.
        persisted = (
            await _persist_snapshots(deps, snapshots, sport_key, deps.league or sport_key, now)
            if snapshots
            else None
        )
        _record_available_games(
            sport_key,
            snapshots,
            deps.loader,
            deps.directory,
            deps.league or sport_key,
            now,
            unvalidated=True,
        )
        _record_poll(
            sport_key,
            snapshots,
            0,
            _loader_matches_found(deps.loader, sport_key),
            snapshots_persisted=persisted,
        )
        logger.info(
            "value pipeline %s: visibility-only (unvalidated) — %d snapshots, no picks",
            sport_key,
            len(snapshots),
        )
        return []

    if not snapshots:
        logger.info("no snapshots for %s", sport_key)
        _record_available_games(
            sport_key, snapshots, deps.loader, deps.directory, deps.league or sport_key, now
        )
        _record_poll(sport_key, snapshots, 0, _loader_matches_found(deps.loader, sport_key))
        return []

    grouped = group_market_prices(snapshots)
    fair = event_fair_probs(grouped, deps.devig_method)
    await _refresh_kickoffs(deps, {s.event_id for s in snapshots})
    persisted = await _persist_snapshots(deps, snapshots, sport_key, deps.league or sport_key, now)

    # In-play gate: a listed match can kick off between page listing and its
    # scrape (multi-minute cycles); OddsPortal then serves IN-PLAY prices.
    # Those must never mint, upgrade, or re-alert picks — the operator
    # cannot take a pre-match price on a started game.
    started: set[str] = set()
    if deps.directory is not None:
        for event_id in {key[0] for key in grouped}:
            teams = deps.directory.lookup(event_id)
            if teams is not None and teams.starts_at is not None and teams.starts_at <= now:
                started.add(event_id)

    picks: list[PickOut] = []
    n_volume = 0
    n_stale = 0
    n_ml_demoted = 0
    n_off_band = 0
    n_thin_books = 0
    # Scan down to the VOLUME floor; pick_tier splits candidates per edge.
    # min() guards a deps-level inversion (Settings already validates the
    # ordering at startup) so a bad override can widen nothing. Per-market
    # premium overrides join the scan floor so an override BELOW the global
    # premium floor (>= the volume floor, Settings-validated) still scans.
    scan_min_edge = min(
        deps.value_volume_min_edge,
        deps.value_min_edge,
        *(edge for _, edge in deps.value_policy.min_edge_by_market),
    )
    for (event_id, market, detail), (prices, captured) in grouped.items():
        if event_id in started:
            continue  # kicked off: in-play odds never become picks/upgrades
        # Per-market book-count floor (default 0 = off): a market quoted by
        # too few books is skipped wholesale — scaffolding for new lines/
        # divisions where thin coverage makes the anchor untrustworthy.
        min_books = min_books_for(deps.value_policy, str(market), detail)
        if min_books and distinct_book_count(prices) < min_books:
            n_thin_books += 1
            continue
        anchored = fair.get((event_id, market, detail))
        if anchored is None:
            continue  # no trustworthy fair value for this market
        anchor_book, fair_by_sel = anchored
        value_bets = find_value_bets_with_fair(
            prices,
            fair_by_sel,
            anchor_book,
            min_edge=scan_min_edge,
            min_odds=deps.value_min_odds,
        )
        for v in value_bets:
            cap = captured.get((v.selection, v.best_book))
            age = max((now - cap).total_seconds(), 0.0) if cap else 0.0
            if age > deps.gate_policy.max_odds_age_seconds:
                n_stale += 1
                continue
            # Odds-band refinement (default empty = off): RAW best odds must
            # fall inside a configured band — same convention as the
            # value_min_odds floor, which also gates on raw odds.
            if not odds_in_bands(v.best_odds, deps.value_policy.odds_bands):
                n_off_band += 1
                continue
            # Per-market PREMIUM floor override (default: global floor).
            premium_floor = min_edge_for(
                deps.value_policy, str(market), detail, deps.value_min_edge
            )
            tier = pick_tier(v.edge, premium_floor, deps.value_volume_min_edge)
            if tier is None:
                continue  # below both floors (unreachable via scan_min_edge)
            # Meta-model score AFTER the edge gate (meta-labeling: the
            # deterministic rule generates, the model only filters).
            ml_score = _score_value_candidate(
                deps,
                event_id,
                market,
                detail,
                v.selection,
                prices,
                fair_by_sel,
                anchor_book,
                sport_key,
                now,
            )
            ml_note = ""
            if (
                deps.value_ml_filter_enabled
                and deps.value_filter is not None
                # a SHADOW-CANDIDATE manifest (verdict != ADOPT, loaded via
                # VALUE_ML_MANIFEST_ALLOW_SHADOW) must NEVER demote — its
                # scores are live-shadow evidence only. Defense in depth:
                # the composition root already refuses to enable enforcement
                # with a shadow model (app/scheduler.py).
                and not deps.value_filter.shadow
                and tier == "premium"
                and ml_score is not None
                and ml_score < deps.value_filter.threshold
            ):
                # VALUE_ML_FILTER on: a sub-threshold premium candidate is
                # DEMOTED to the volume (shadow) tier — persisted for CLV
                # evidence, never alerted, never reserving exposure. Out-of-
                # scope candidates (ml_score None) always pass unfiltered:
                # the model must not veto markets it has never seen.
                tier = "volume"
                n_ml_demoted += 1
                ml_note = (
                    f" | ml-filter {ml_score:.3f} < q* "
                    f"{deps.value_filter.threshold:.3f}: demoted to volume"
                )
            # Stake from the sharp fair prob at the EFFECTIVE (net) price.
            breakdown = recommended_stake(
                v.sharp_fair_prob, v.best_odds_effective, deps.stake_policy
            )
            if tier == "premium":
                granted = deps.ledger.reserve(now.date(), breakdown.final)
                if granted <= 0.0:
                    logger.info("daily exposure cap reached; skipping %s", v.selection)
                    continue
            else:
                # VOLUME tier: the stake breakdown is computed (what WOULD
                # be recommended) but the daily exposure ledger is NEVER
                # touched — the informational shadow tier must not consume
                # the cap premium picks need.
                granted = breakdown.final
            # Named sharp anchors are backtested; consensus anchors are the
            # fallback path with weaker evidence — reflected in confidence.
            confidence = 0.7 if v.sharp_book == CONSENSUS_ANCHOR else 0.9

            event_label = event_id
            league_label = deps.league or sport_key
            if deps.directory is not None:
                teams = deps.directory.lookup(event_id)
                if teams is not None:
                    event_label = f"{teams.home} vs {teams.away}"
                    if teams.league:  # scraped per-event league beats config csv
                        league_label = teams.league

            pick = PickOut(
                pick_id=str(uuid.uuid4()),
                sport=sport_key,  # one deps serves soccer AND basketball polls
                league=league_label,
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
                    + ml_note
                ),
                tier=tier,
                value_filter_score=ml_score,
                # anchor stratification key for live CLV (PIN/SHARP/CONS)
                anchor_type=anchor_type_for(v.sharp_book),
                created_at=now,
            )
            outcome = await _maybe_persist(deps, pick, event_id)
            if tier == "volume":
                # Shadow tier: NEVER alerted, NEVER on the exposure ledger.
                # Its picks ride the same event pages as premium ones, so
                # the CLV revalidation below re-prices them for free — that
                # accumulating live evidence is the tier's entire purpose.
                # "duplicate" covers both a volume re-detection and a key
                # already held by a PREMIUM row (which must never be touched
                # by the shadow tier); "unpersisted" volume picks are
                # dropped — without a DB row there is no evidence to gather.
                if outcome == "inserted":
                    picks.append(pick)
                    n_volume += 1
                continue
            if outcome == "duplicate":
                # Confirmed DB duplicate (the picks unique key ignores odds):
                # hand the exposure grant back — a re-detection is not new
                # exposure — but STILL dispatch. The alert dedupe key includes
                # decimal_odds (notifications/base.py), so an unchanged price
                # stays quiet for the idempotency TTL (7d default) while a
                # material price move re-alerts by design; and because the
                # dispatcher releases the claim when no sink delivers, an
                # alert whose dispatch failed is retried by this very
                # re-dispatch on the next cycle.
                deps.ledger.release(now.date(), granted)
            else:
                # "inserted", "unpersisted" (uncertainty = "new"), or
                # "upgraded" — a volume row just cleared the premium
                # threshold: THIS is its alert moment, and the reservation
                # made above stays (the shadow row never held one).
                picks.append(pick)
            # value_min_edge adds the "Still +EV down to X.XX" execution
            # line (value-strategy semantics: model_probability holds the
            # sharp fair prob here — see build_pick_alert).
            await deps.dispatcher.dispatch(
                build_pick_alert(
                    pick,
                    deps.value_min_edge,
                    model_name=deps.model_name,
                    model_version=deps.model_version,
                )
            )

    # Re-price every OPEN pick from this cycle's snapshots: CLV true-up +
    # current odds/edge ("still worth betting?") — no second scrape. Picks
    # on games OUTSIDE the dated window (taken weeks ahead) get their match
    # pages scraped directly so they revalidate every cycle too.
    if deps.session_factory is not None:
        from app.clv_trueup import revalidate_offwindow_picks, revalidate_open_picks

        try:
            await revalidate_open_picks(deps.session_factory, snapshots, deps.devig_method)
            await revalidate_offwindow_picks(
                deps.loader,
                deps.session_factory,
                sport_key,
                covered_event_ids={s.event_id for s in snapshots},
                devig_method=deps.devig_method,
            )
        except Exception as exc:  # revalidation must never break picking
            logger.error("open-pick revalidation failed: %s", type(exc).__name__)

    n_premium = len(picks) - n_volume
    logger.info(
        "value pipeline cycle for %s: %d premium picks, %d volume (shadow)",
        sport_key,
        n_premium,
        n_volume,
    )
    if n_ml_demoted:
        # VALUE_ML_FILTER intervention is never silent: these candidates
        # cleared the premium edge gate and were demoted by the meta-model.
        logger.info(
            "value pipeline %s: ml-filter demoted %d premium candidate(s) to volume",
            sport_key,
            n_ml_demoted,
        )
    if n_off_band:
        # VALUE_ODDS_BANDS intervention is never silent either: these
        # candidates cleared the edge scan and were rejected on price band.
        logger.info(
            "value pipeline %s: %d candidate(s) outside VALUE_ODDS_BANDS",
            sport_key,
            n_off_band,
        )
    if n_thin_books:
        logger.info(
            "value pipeline %s: %d market(s) skipped below their VALUE_MIN_BOOKS_PER_MARKET floor",
            sport_key,
            n_thin_books,
        )
    if n_stale:
        # The silent failure mode of a too-slow cycle: candidates captured
        # more than MAX_ODDS_AGE_SECONDS before the cycle ended are dropped
        # — with a big slate that can be nearly EVERYTHING. Make it loud.
        logger.warning(
            "value pipeline %s: %d candidate(s) discarded by the odds-age gate "
            "(captured >%.0fs before cycle end) — the scrape outlasted the "
            "freshness window; trim markets/leagues or raise concurrency",
            sport_key,
            n_stale,
            deps.gate_policy.max_odds_age_seconds,
        )
    _record_available_games(
        sport_key, snapshots, deps.loader, deps.directory, deps.league or sport_key, now
    )
    _record_poll(
        sport_key,
        snapshots,
        n_premium,
        _loader_matches_found(deps.loader, sport_key),
        snapshots_persisted=persisted,
        volume_picks=n_volume,
        stale_candidates=n_stale,
    )
    return picks


GroupedMarkets = dict[
    tuple[str, Market, str | None],
    tuple[dict[str, dict[str, float]], dict[tuple[str, str], datetime]],
]


def group_market_prices(snapshots: Sequence[OddsSnapshotIn]) -> GroupedMarkets:
    """Group snapshots into {(event_id, market, market_detail):
    (selection->{book: odds}, (selection, book)->captured_at)} for the value
    finder and CLV true-up. `market_detail` keeps distinct lines (handicaps,
    totals) in separate devig groups — mixing lines corrupts fair value."""
    out: GroupedMarkets = {}
    for snap in snapshots:
        key = (snap.event_id, snap.market, snap.market_detail)
        prices, captured = out.setdefault(key, ({}, {}))
        prices.setdefault(snap.selection, {})[snap.bookmaker] = snap.decimal_odds
        captured[(snap.selection, snap.bookmaker)] = snap.captured_at
    return out


# Markets whose outcomes are mutually exclusive and exhaustive — direct
# anchor devig of one book is sound. Loader config guarantees SPREADS groups
# are half-line AH (no pushes) or 3-way European handicap. Double chance is
# NOT direct (overlapping legs, quotes sum ~200%) — derived from 1X2.
_DIRECT_MARKETS = frozenset({Market.H2H, Market.TOTALS, Market.BTTS, Market.DNB, Market.SPREADS})

EventFairProbs = dict[tuple[str, Market, str | None], tuple[str, dict[str, float]]]


def event_fair_probs(grouped: GroupedMarkets, devig_method: DevigMethod) -> EventFairProbs:
    """Trustworthy (anchor_book, selection->fair) per (event, market, line).

    Shared by the live value pipeline and the CLV true-up so picks and their
    closing-line values are priced by the SAME rules."""
    from app.edge.value import anchor_fair_probs, double_chance_fair

    out: EventFairProbs = {}
    h2h_3way: dict[str, tuple[tuple[str, dict[str, float]], list[str]]] = {}
    for (event_id, market, detail), (prices, _) in grouped.items():
        if market in _DIRECT_MARKETS:
            anchored = anchor_fair_probs(prices, devig_method=devig_method)
            if anchored is not None:
                out[(event_id, market, detail)] = anchored
                if market is Market.H2H and len(prices) == 3:
                    h2h_3way[event_id] = (anchored, list(prices.keys()))
    for (event_id, market, detail), _group in grouped.items():
        if market is Market.DOUBLE_CHANCE and event_id in h2h_3way:
            anchored, selections = h2h_3way[event_id]
            home, away = selections[0], selections[-1]  # loader order: 1, X, 2
            dc_fair = double_chance_fair(anchored[1], home, away)
            if dc_fair:
                out[(event_id, market, detail)] = (anchored[0], dc_fair)
    return out


def _fair_probabilities(
    snapshots: Sequence[OddsSnapshotIn],
    method: DevigMethod,
) -> dict[tuple[str, str, str, str], float]:
    """Devig each (event, bookmaker, market, line) book into fair probabilities.

    `market_detail` is part of the grouping key: distinct lines of one Market
    (over_under_2_5 vs over_under_3_5) are separate books — pooling them
    devigs a fake 4-leg market and corrupts every fair probability (the same
    rule group_market_prices enforces for the value pipeline). The returned
    key stays (event, bookmaker, market, selection): line-bearing selections
    ("Over 3.5") keep lines distinct after flattening."""
    books: dict[tuple[str, str, str, str | None], list[OddsSnapshotIn]] = defaultdict(list)
    for snap in snapshots:
        books[(snap.event_id, snap.bookmaker, snap.market, snap.market_detail)].append(snap)

    fair: dict[tuple[str, str, str, str], float] = {}
    for (event_id, bookmaker, market, _detail), legs in books.items():
        if len(legs) < 2:
            continue  # cannot devig a one-sided book
        probs = devig([leg.decimal_odds for leg in legs], method=method)
        for leg, p in zip(legs, probs, strict=True):
            fair[(event_id, bookmaker, market, leg.selection)] = p
    return fair
