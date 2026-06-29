"""Pick pipeline: snapshots -> devig -> model join -> gates -> stake -> alert.

Composition layer: pure math stays in app/probabilities|edge|risk; this module
wires it to IO (loader, dispatcher). Persistence of picks/edges to Postgres
joins in roadmap phase 2 alongside event/entity resolution.
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from app.backtesting import clv as _clv  # noqa: F401  (settlement uses this module)
from app.edge.gates import GatePolicy, PickCandidate, evaluate
from app.edge.steam import (
    SteamPolicy,
    build_trajectories,
    evaluate_steam,
    lookup_trajectory,
)
from app.edge.value_policy import (
    ValuePolicy,
    devig_method_for,
    distinct_book_count,
    is_major_league,
    is_visibility_only_market,
    max_edge_for,
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
from app.risk.staking import StakeBreakdown, StakePolicy, recommended_stake, stake_amount
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
        return "Basketball"  # ALL basketball scraped, not NBA-only
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


#: Pick-time sharp-anchor injector (PipelineDeps.sharp_anchor_loader): returns
#: extra OddsSnapshotIn rows (captured free Betfair/Pinnacle prices) to merge
#: into the scrape before anchoring. One line so ruff format is version-stable.
SharpAnchorLoader = Callable[[str, Sequence[OddsSnapshotIn]], Awaitable[Sequence[OddsSnapshotIn]]]

#: Pick-time odds-history reader (PipelineDeps.steam_history_loader): given the
#: cycle's current snapshots, returns recent odds_snapshots HISTORY rows (per-book
#: time series, captured_at <= now) for those events so the steam gate can read
#: each book's trajectory. Read-only; bound to a repository at the composition
#: root, stubbed in tests. One line so ruff format is version-stable.
SteamHistoryLoader = Callable[[str, Sequence[OddsSnapshotIn]], Awaitable[Sequence[OddsSnapshotIn]]]


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
    clv_record_drift: bool = False  # build #6: append pick_line_drift on re-price (OFF)
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
    # EXPERIMENTAL sport keys (e.g. {"tennis", "american_football"}) when the
    # operator opts in (ENABLE_UNVALIDATED_PICKS): these DO mint picks, but every
    # pick is FORCED to the volume (shadow) tier — persisted + CLV-tracked + (via
    # ESPN) auto-settled, yet NEVER alerted and NEVER reserving exposure, because
    # the sport has not cleared the > 2 SE held-out CLV gate. Honest "give me
    # picks for tennis/NFL" without claiming a validated edge. A sport is either
    # visibility_only OR experimental, never both. Default empty.
    experimental_sports: frozenset[str] = frozenset()
    # OPTIONAL pick-time SHARP-ANCHOR injector (default None = current behavior).
    # When set, it returns extra OddsSnapshotIn rows — the captured free Betfair
    # Exchange + Pinnacle ARCADIA prices, re-keyed to the scraped events — which
    # are MERGED into the live scrape BEFORE anchoring, so a pick anchors on the
    # SHARP book (Pinnacle/Betfair) instead of the soft-book consensus median.
    # This makes live picks match the validated Pinnacle-anchored backtest where
    # a free sharp price is available. Signature: async (sport_key, snapshots)
    # -> list[OddsSnapshotIn]. Wired at the composition root (app/scheduler.py)
    # behind VALUE_SHARP_ANCHOR_FROM_ARCHIVES; tests inject a stub.
    sharp_anchor_loader: SharpAnchorLoader | None = None
    # OPTIONAL line-movement / steam-awareness gate (app/edge/steam.py). Default
    # None = gate ABSENT: a strict no-op, zero extra work, current behavior. When
    # the composition root builds a SteamPolicy (always, from Settings) the gate
    # RUNS: with policy.enabled False it is SHADOW (computes + logs the per-
    # candidate verdict, never changes the tier — measure before enforcing); with
    # policy.enabled True a tripped verdict DEMOTES a premium candidate to volume
    # (shadow) — persisted + CLV-tracked, never alerted — exactly like the other
    # built-but-off premium gates (never a silent drop). NO leakage: only odds
    # captured_at <= now are consulted (see app/edge/steam.py).
    steam_policy: SteamPolicy | None = None
    # Reader of recent odds_snapshots HISTORY for the steam gate (per-book
    # trajectories). Default None => only the current cycle's snapshots are
    # available (one point per book => the gate stays inert until history
    # accumulates). Bound to a repository at the composition root; stubbed in
    # tests. Failure is isolated — a history-read error never breaks picking.
    steam_history_loader: SteamHistoryLoader | None = None
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

            event_label = snap.event_id
            if deps.directory is not None:
                teams = deps.directory.lookup(snap.event_id)
                if teams is not None:
                    event_label = f"{teams.home} vs {teams.away}"

            # Build the pick with the per-bet-capped fraction (NO daily clip yet);
            # the daily-exposure ledger is consumed below and ONLY for brand-new
            # detections, so the persisted row carries the reproducible
            # breakdown.final and a re-alert can rebuild the exact same stake.
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
                recommended_stake_fraction=breakdown.final,
                recommended_stake_amount=stake_amount(breakdown.final, deps.bankroll),
                stake_breakdown=StakeBreakdownOut(
                    raw_kelly=breakdown.raw_kelly,
                    fractional=breakdown.fractional,
                    capped=breakdown.capped,
                    final=breakdown.final,
                    daily_clipped=False,
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
            # Persist FIRST, then reserve only on a genuinely new detection
            # (inserted/upgraded). This (a) lets a re-detected 'duplicate' — and
            # an 'unpersisted' pick whose DB state we cannot confirm — avoid
            # reserving exposure they could never release, so a sustained
            # duplicate/unpersisted pick never silently exhausts the daily cap
            # (kr-1 / kelly-risk-r2-1); and (b) never lets an exhausted cap
            # (granted<=0) skip the re-dispatch of an ALREADY-persisted pick.
            outcome = await _maybe_persist(deps, pick, snap.event_id)
            staked = await _reserve_for_outcome(deps, pick, breakdown, outcome, snap.event_id, now)
            if staked is None:
                # brand-new pick with no remaining daily/event capacity: skip it
                logger.info("daily exposure cap reached; skipping %s", snap.selection)
                continue
            pick = staked
            if outcome in ("inserted", "upgraded", "unpersisted"):
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


def _is_asian_handicap(market_detail: str | None) -> bool:
    """True for a 2-way Asian-handicap line key ("asian_handicap_-1_5",
    "asian_handicap_games_-7_5") — the scope of the AH sentinel/implausibility
    guard. European handicap (3-way) and totals are deliberately excluded; the
    guard reasons about a 2-way AH line specifically."""
    if not market_detail:
        return False
    return market_detail.strip().lower().startswith("asian_handicap")


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


async def _persist_stake_clip(deps: "PipelineDeps", pick: PickOut, event_id: str) -> None:
    """Rewrite the persisted row's stake to the daily-clipped amount (BUG 2).

    The row was persisted with the pre-clip (per-bet-capped) stake BEFORE the
    daily-exposure reservation ran; when that reservation clips the stake, the
    stored/reported value must be brought in line with what the ledger actually
    reserved, or the persisted stake escapes the daily cap. Best-effort, with
    the SAME guards as `_maybe_persist`: without a session factory, directory,
    or resolvable teams there is no row to correct, and a failure here must
    never break alerting (the in-memory pick already carries the clip)."""
    if deps.session_factory is None or deps.directory is None:
        return
    teams = deps.directory.lookup(event_id)
    if teams is None:
        return
    from app.storage import repositories

    try:
        async with deps.session_factory() as session:
            await repositories.update_pick_stake(
                session, pick, teams, deps.model_name, deps.model_version
            )
            await session.commit()
    except Exception as exc:  # persistence must never break alerting
        logger.error(
            "pick stake-clip persistence failed for %s: %s", pick.pick_id, type(exc).__name__
        )


async def _reserve_for_outcome(
    deps: "PipelineDeps",
    pick: PickOut,
    breakdown: StakeBreakdown,
    outcome: PersistOutcome,
    event_id: str,
    now: datetime,
) -> PickOut | None:
    """Apply the daily-exposure ledger AFTER persistence (kr-1 ordering).

    - inserted/upgraded: reserve breakdown.final, bounded by the daily AND the
      optional per-event cap. A clip below breakdown.final rebuilds the pick
      with the daily-clipped stake AND rewrites the persisted row to match (the
      row was stored pre-clip — BUG 2). A brand-new INSERTED pick with a zero
      grant returns None (no capacity -> skip); an already-persisted UPGRADED
      pick is never skipped on a zero grant — its alert moment must still fire.
    - duplicate: already persisted; reserve NOTHING (a re-detection is not new
      exposure). The pick keeps breakdown.final, matching its persisted row.
    - unpersisted: DB state is unknown; reserve NOTHING it could never release,
      so a sustained-unpersisted pick cannot accumulate standing daily exposure
      across cycles and silently exhaust the cap (kelly-risk-r2-1).
    """
    if outcome in ("duplicate", "unpersisted"):
        return pick
    granted = deps.ledger.reserve(now.date(), breakdown.final, event_id)
    if granted <= 0.0 and outcome == "inserted":
        return None
    if granted < breakdown.final:
        clipped = pick.model_copy(
            update={
                "recommended_stake_fraction": granted,
                "recommended_stake_amount": stake_amount(granted, deps.bankroll),
                "stake_breakdown": pick.stake_breakdown.model_copy(
                    update={"final": granted, "daily_clipped": True}
                ),
            }
        )
        # BUG 2: the row was persisted with the pre-clip stake; correct it so the
        # stored/reported stake honours the daily cap and matches the reservation.
        await _persist_stake_clip(deps, clipped, event_id)
        return clipped
    return pick


async def run_value_pipeline(deps: PipelineDeps, sport_key: str) -> list[PickOut]:
    """One polling cycle of the VALIDATED strategy (sharp-vs-soft value,
    docs/backtesting/value-findings.md): group multi-book odds per market,
    anchor fair value on the sharpest book, flag better prices elsewhere.

    No prediction model involved; deps.model is unused here.
    """
    from app.edge.value import (
        CONSENSUS_ANCHOR,
        SHARP_BOOKS,
        ah_candidate_plausible,
        anchor_type_for,
        find_value_bets_with_fair,
        is_sharp_anchored,
    )

    # thin-coverage gate measures SOFT liquidity — exclude sharp/injected books
    _sharp_norm = frozenset(b.lower() for b in SHARP_BOOKS)

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

    # Optionally MERGE the free Betfair/Pinnacle sharp prices (re-keyed to these
    # events) into the set used FOR ANCHORING ONLY — so a pick anchors on the
    # sharp book, not the soft-book consensus median. The original `snapshots`
    # (scrape) is what gets persisted/counted; only `grouped` sees the extras.
    # Failure is isolated: sharp injection must NEVER break picking.
    anchor_snapshots: Sequence[OddsSnapshotIn] = snapshots
    if deps.sharp_anchor_loader is not None:
        try:
            extra = await deps.sharp_anchor_loader(sport_key, snapshots)
        except Exception as exc:
            logger.error("sharp-anchor injection failed for %s: %s", sport_key, type(exc).__name__)
            extra = []
        if extra:
            anchor_snapshots = [*snapshots, *extra]
            logger.info(
                "value pipeline %s: merged %d free sharp-anchor snapshot(s) for pick anchoring",
                sport_key,
                len(extra),
            )
    grouped = group_market_prices(anchor_snapshots)
    fair = event_fair_probs(grouped, deps.devig_method, deps.value_policy)
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

    # Steam gate trajectories: per-(market) per-(selection, book) recent price
    # history for the line-movement / steam-awareness gate. Built ONLY when the
    # gate is configured (default None => skipped entirely, zero extra work). The
    # current cycle's anchor_snapshots seed the LATEST point per book; the
    # optional history loader appends recent odds_snapshots rows so a trajectory
    # exists. NO leakage: build_trajectories drops any captured_at > now.
    steam_trajectories: dict[
        tuple[str, object, str | None], dict[tuple[str, str], list[tuple[datetime, float]]]
    ] = {}
    if deps.steam_policy is not None:
        steam_history: list[OddsSnapshotIn] = list(anchor_snapshots)
        if deps.steam_history_loader is not None:
            try:
                steam_history.extend(await deps.steam_history_loader(sport_key, snapshots))
            except Exception as exc:  # history read must NEVER break picking
                logger.error("steam history load failed for %s: %s", sport_key, type(exc).__name__)
        steam_trajectories = build_trajectories(
            steam_history, now, deps.steam_policy.lookback_seconds
        )

    picks: list[PickOut] = []
    n_volume = 0
    n_stale = 0
    n_ml_demoted = 0
    n_major_demoted = 0
    n_no_sharp_demoted = 0
    n_steam_demoted = 0
    n_steam_shadow = 0
    n_experimental = 0
    n_off_band = 0
    n_thin_books = 0
    n_visibility_capped = 0
    n_ah_rejected = 0
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
        if min_books and distinct_book_count(prices, exclude=_sharp_norm) < min_books:
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
            # Per-market DATA-ERROR ceiling override (default: the global
            # value_policy.max_edge). Resolved per (market, detail) at this
            # chokepoint exactly like devig_method_for in event_fair_probs; an
            # empty map leaves the global ceiling in force (bit-identical).
            max_edge=max_edge_for(
                deps.value_policy, str(market), detail, deps.value_policy.max_edge
            ),
        )
        for v in value_bets:
            # AH SENTINEL/IMPLAUSIBILITY guard (app/edge/value.ah_candidate_plausible):
            # a corrupt/sentinel AH feed price (a backtest found odds like 22.0) or an
            # implausibly large sharp-vs-soft implied-prob gap fabricates a phantom edge.
            # Reject the candidate at the candidate-building boundary BEFORE it can mint
            # ANY pick (premium OR volume shadow). Scoped to asian_handicap lines, so
            # non-AH markets are untouched; bounds are Settings-driven with sane defaults.
            if _is_asian_handicap(detail) and not ah_candidate_plausible(
                v,
                max_odds=deps.value_policy.ah_max_odds,
                max_sharp_soft_ratio=deps.value_policy.ah_max_sharp_soft_ratio,
            ):
                n_ah_rejected += 1
                continue
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
            # VISIBILITY-ONLY market cap: a market in value_policy.visibility_only_markets
            # can NEVER be premium — it is CAPPED at the volume (shadow) tier regardless
            # of edge (even above the premium floor), so a brand-new market (football AH)
            # accrues forward shadow CLV before it is trusted to alert. Empty set = no-op
            # (current behavior). Runs FIRST among the demotion gates so the cap dominates;
            # the gates below then no-op on an already-volume tier (their `tier ==
            # "premium"` guards). Never a silent drop — surfaced on the pick + logged.
            visibility_note = ""
            if tier == "premium" and is_visibility_only_market(
                deps.value_policy, str(market), detail, sport_key
            ):
                tier = "volume"
                n_visibility_capped += 1
                visibility_note = " | visibility-only market: capped at volume (shadow)"
            # Major-league gate: a PREMIUM candidate whose scraped league is not
            # in the configured major set is DEMOTED to the volume (shadow) tier
            # — persisted + CLV-tracked, never alerted, never reserving exposure.
            # Empty value_policy.major_leagues disables the gate (no-op, the
            # default). The honest-high-ROI lever: alert + risk exposure only
            # where a sharp anchor + liquidity actually exist
            # (.claude/memory/pitfalls.md 2026-06-20 — ~37% sharp coverage is
            # structural on obscure slates; scope premium, don't fuzzy-match).
            # Runs BEFORE the ML demotion so the two interventions never stack.
            major_note = ""
            if tier == "premium":
                event_league = ""
                if deps.directory is not None:
                    teams = deps.directory.lookup(event_id)
                    if teams is not None and teams.league:
                        event_league = teams.league
                if not is_major_league(deps.value_policy, event_league):
                    tier = "volume"
                    n_major_demoted += 1
                    major_note = " | non-major league: demoted to volume (shadow)"
            # Require-sharp-anchor gate: a PREMIUM candidate whose fair value came
            # from the soft CONSENSUS median (no genuine sharp book — Pinnacle or
            # Betfair — backed the price) is DEMOTED to the volume (shadow) tier —
            # persisted + CLV-tracked, never alerted, never reserving exposure.
            # deps.value_policy.require_sharp_anchor False disables the gate (no-op,
            # the default). This is the season-proof, name-proof sibling of the
            # major-league gate: it stops obscure-league bleed (e.g. "GFA League")
            # by DATA (no sharp anchor) rather than by league name. The `tier ==
            # "premium"` guard means a pick already demoted above STAYS volume —
            # the interventions never stack confusingly (anchor_book == v.sharp_book
            # exactly; see find_value_bets_with_fair).
            sharp_note = ""
            if (
                tier == "premium"
                and deps.value_policy.require_sharp_anchor
                and not is_sharp_anchored(anchor_book)
            ):
                tier = "volume"
                n_no_sharp_demoted += 1
                sharp_note = " | no sharp anchor (consensus): demoted to volume (shadow)"
            # Experimental (unvalidated) sport: FORCE every pick to the volume
            # (shadow) tier — never alerted, no exposure — regardless of edge.
            # It is still persisted + CLV-tracked + (via ESPN) auto-settled so it
            # builds its OWN forward evidence; it just never claims a validated
            # edge until its held-out incremental CLV clears > 2 SE.
            experimental_note = ""
            if tier == "premium" and sport_key in deps.experimental_sports:
                tier = "volume"
                n_experimental += 1
                experimental_note = " | UNVALIDATED sport: experimental (shadow) only"
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
            # Line-movement / steam-awareness gate (app/edge/steam.py): reads the
            # recent trajectory of BOTH the fill book and the sharp anchor for this
            # selection and trips when the soft price is CONVERGING toward the anchor
            # (edge correcting/evaporating) or the anchor is STALE (last seen beyond
            # the freshness window -> phantom edge). Scoped to NAMED-anchor picks:
            # a consensus(median) anchor has no single-book trajectory to test, and
            # the require-sharp-anchor gate already targets that path. Default
            # steam_policy None disables it entirely. With policy.enabled False the
            # gate is SHADOW: the verdict is computed + logged but the tier is
            # UNCHANGED, so its effect on real picks is measured before it enforces.
            # With policy.enabled True a tripped verdict DEMOTES a premium candidate
            # to volume (shadow) — persisted + CLV-tracked, never alerted — exactly
            # like the gates above (never a silent drop).
            steam_note = ""
            if deps.steam_policy is not None and anchor_book != CONSENSUS_ANCHOR:
                market_traj = steam_trajectories.get((event_id, market, detail), {})
                verdict = evaluate_steam(
                    fill_trajectory=lookup_trajectory(market_traj, v.selection, v.best_book),
                    anchor_trajectory=lookup_trajectory(market_traj, v.selection, anchor_book),
                    now=now,
                    policy=deps.steam_policy,
                )
                if verdict.tripped:
                    reason_str = ",".join(verdict.reasons)
                    if deps.steam_policy.enabled and tier == "premium":
                        tier = "volume"
                        n_steam_demoted += 1
                        steam_note = f" | steam ({reason_str}): demoted to volume (shadow)"
                    else:
                        # SHADOW (gate off) or an already-demoted candidate: record
                        # the verdict, never change the tier. Surfaced on the pick so
                        # its forward CLV can be measured against the would-be demote.
                        n_steam_shadow += 1
                        steam_note = f" | steam(shadow) ({reason_str}): would demote"
                        logger.info(
                            "value pipeline %s: steam(shadow) %s/%s/%s closed_frac=%s "
                            "anchor_age_s=%s reasons=%s",
                            sport_key,
                            event_id,
                            market,
                            v.selection,
                            f"{verdict.closed_fraction:.3f}"
                            if verdict.closed_fraction is not None
                            else "na",
                            f"{verdict.anchor_age_seconds:.0f}"
                            if verdict.anchor_age_seconds is not None
                            else "na",
                            reason_str,
                        )
            # Stake from the sharp fair prob at the EFFECTIVE (net) price. The
            # daily-exposure ledger is consumed AFTER persistence (below), and
            # ONLY for brand-new premium detections — so the pick is built with
            # the per-bet-capped breakdown.final and a re-alert can reproduce it.
            breakdown = recommended_stake(
                v.sharp_fair_prob, v.best_odds_effective, deps.stake_policy
            )
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
                recommended_stake_fraction=breakdown.final,
                recommended_stake_amount=stake_amount(breakdown.final, deps.bankroll),
                stake_breakdown=StakeBreakdownOut(
                    raw_kelly=breakdown.raw_kelly,
                    fractional=breakdown.fractional,
                    capped=breakdown.capped,
                    final=breakdown.final,
                    daily_clipped=False,
                ),
                odds_age_seconds=age,
                liquidity=None,
                reason_summary=(
                    # Show the sharp fair as ODDS (1/sharp_fair_prob), apples-to-
                    # apples with the offered odds — NOT the fair probability,
                    # which mixed units against best_odds (display only; the edge/
                    # EV math above is unchanged).
                    f"value: {v.sharp_book} fair {1.0 / v.sharp_fair_prob:.2f} vs "
                    f"{v.best_book} {v.best_odds:.2f}"
                    + (
                        f" (eff {v.best_odds_effective:.2f} after commission)"
                        if v.best_odds_effective != v.best_odds
                        else ""
                    )
                    + visibility_note
                    + major_note
                    + sharp_note
                    + experimental_note
                    + ml_note
                    + steam_note
                ),
                tier=tier,
                value_filter_score=ml_score,
                # anchor stratification key for live CLV (PIN/SHARP/CONS)
                anchor_type=anchor_type_for(v.sharp_book),
                # CLV-3: the concrete pick-time anchor BOOK behind anchor_type, so the
                # CLV close can test BOOK independence (a Smarkets-anchored pick vs a
                # Betfair-exchange close is independent though both are 'sharp').
                anchor_book=v.sharp_book,
                created_at=now,
            )
            # Persist FIRST (kr-1 ordering), then reserve only on a genuinely
            # new premium detection. A re-detected 'duplicate' (already in the
            # DB) and an 'unpersisted' pick (DB state unknown) reserve NOTHING —
            # so a sustained duplicate/unpersisted pick never accumulates
            # standing exposure that would silently exhaust the daily cap
            # (kr-1 / kelly-risk-r2-1) — and an exhausted cap never skips the
            # re-dispatch of an already-persisted pick.
            outcome = await _maybe_persist(deps, pick, event_id)
            if tier == "volume":
                # Shadow tier: persisted + CLV-tracked but NOT alerted and NEVER
                # on the exposure ledger. (Volume alerting was trialed 2026-06-23
                # then reverted: live CLV ~0% (-0.3% over n=21) showed no edge vs
                # premium's +11.9% — premium-only alerts. The build_pick_alert
                # 🔵 VOLUME tag + tier-keyed dedupe stay in place, so re-enabling
                # is a one-line `await deps.dispatcher.dispatch(...)` here.) Its
                # picks ride the same event pages as premium ones, so the CLV
                # revalidation below re-prices them for free — the tier's purpose.
                if outcome == "inserted":
                    picks.append(pick)
                    n_volume += 1
                continue
            staked = await _reserve_for_outcome(deps, pick, breakdown, outcome, event_id, now)
            if staked is None:
                # brand-new premium pick with no remaining daily/event capacity
                logger.info("daily exposure cap reached; skipping %s", v.selection)
                continue
            pick = staked
            if outcome in ("inserted", "upgraded", "unpersisted"):
                # "inserted"/"unpersisted" (uncertainty = "new") or "upgraded"
                # — a volume row just cleared the premium threshold: THIS is its
                # alert moment. A "duplicate" is re-dispatched (below) but is not
                # a NEW pick this cycle, so it is not appended.
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
            # Re-price against the SAME anchored set used at mint (anchor_snapshots =
            # scrape + injected Pinnacle/Betfair sharp lines), NOT raw snapshots — else
            # current_edge re-anchors on the soft consensus and can flip from an anchor
            # SWITCH rather than a true line move (audit #8, 2026-06-26).
            await revalidate_open_picks(
                deps.session_factory,
                anchor_snapshots,
                deps.devig_method,
                record_drift=deps.clv_record_drift,
                value_policy=deps.value_policy,
            )
            await revalidate_offwindow_picks(
                deps.loader,
                deps.session_factory,
                sport_key,
                covered_event_ids={s.event_id for s in snapshots},
                devig_method=deps.devig_method,
                value_policy=deps.value_policy,
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
    if n_steam_demoted:
        # ENFORCING steam gate: premium candidates demoted because the soft price
        # is converging on the anchor (edge correcting) or the anchor is stale.
        logger.info(
            "value pipeline %s: steam gate demoted %d premium candidate(s) to volume",
            sport_key,
            n_steam_demoted,
        )
    if n_steam_shadow:
        # SHADOW steam gate: candidates the gate WOULD demote if enforcing — tier
        # unchanged, surfaced for measurement before VALUE_STEAM_GATE_ENABLED flips.
        logger.info(
            "value pipeline %s: steam(shadow) flagged %d candidate(s) (no tier change)",
            sport_key,
            n_steam_shadow,
        )
    if n_ml_demoted:
        # VALUE_ML_FILTER intervention is never silent: these candidates
        # cleared the premium edge gate and were demoted by the meta-model.
        logger.info(
            "value pipeline %s: ml-filter demoted %d premium candidate(s) to volume",
            sport_key,
            n_ml_demoted,
        )
    if n_major_demoted:
        # The major-league gate is never silent either: these candidates cleared
        # the premium edge gate but their scraped league is not in the configured
        # VALUE_MAJOR_LEAGUES set, so they were demoted to the shadow tier.
        logger.info(
            "value pipeline %s: major-league gate demoted %d premium candidate(s) to volume",
            sport_key,
            n_major_demoted,
        )
    if n_no_sharp_demoted:
        # The require-sharp-anchor gate is never silent either: these candidates
        # cleared the premium edge gate but their fair value came from the soft
        # consensus median (no Pinnacle/Betfair sharp anchor), so they were demoted
        # to the shadow tier under VALUE_REQUIRE_SHARP_ANCHOR.
        logger.info(
            "value pipeline %s: require-sharp-anchor gate demoted %d premium candidate(s) "
            "to volume",
            sport_key,
            n_no_sharp_demoted,
        )
    if n_experimental:
        # Experimental (unvalidated) sport: these would-be premium candidates were
        # forced to the volume/shadow tier — surfaced + CLV-tracked, never alerted.
        logger.info(
            "value pipeline %s: UNVALIDATED sport — %d candidate(s) kept experimental (shadow)",
            sport_key,
            n_experimental,
        )
    if n_visibility_capped:
        # The visibility-only cap is never silent: these candidates cleared the
        # premium edge gate but their market is capped at the shadow tier
        # (VALUE_VISIBILITY_ONLY_MARKETS) — persisted + CLV-tracked, never alerted.
        logger.info(
            "value pipeline %s: visibility-only cap held %d candidate(s) at volume (shadow)",
            sport_key,
            n_visibility_capped,
        )
    if n_ah_rejected:
        # The AH sentinel/implausibility guard is never silent: these AH
        # candidates carried a corrupt/sentinel feed price or an implausible
        # sharp-vs-soft gap and were rejected before minting any pick.
        logger.info(
            "value pipeline %s: AH sentinel/implausibility guard rejected %d candidate(s)",
            sport_key,
            n_ah_rejected,
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

# Shared frozen no-op policy for the default-OFF path (ruff B008: no call in a
# function default). ValuePolicy is immutable, so one instance is safe to share.
_EMPTY_VALUE_POLICY = ValuePolicy()


def event_fair_probs(
    grouped: GroupedMarkets,
    devig_method: DevigMethod,
    value_policy: ValuePolicy = _EMPTY_VALUE_POLICY,
) -> EventFairProbs:
    """Trustworthy (anchor_book, selection->fair) per (event, market, line).

    Shared by the live value pipeline and the CLV true-up so picks and their
    closing-line values are priced by the SAME rules. ``value_policy`` carries
    the optional per-market devig override (``devig_by_market``) and the
    consensus logit-pool flag (``consensus_logit_pool``); the default empty
    policy reproduces the global-method, median-consensus behavior exactly.
    Both knobs flow through this single chokepoint so the pick pipeline and the
    CLV true-up always price fill and close with the identical method."""
    from app.edge.value import anchor_fair_probs, double_chance_fair

    out: EventFairProbs = {}
    h2h_3way: dict[str, tuple[tuple[str, dict[str, float]], list[str]]] = {}
    for (event_id, market, detail), (prices, _) in grouped.items():
        if market in _DIRECT_MARKETS:
            anchored = anchor_fair_probs(
                prices,
                devig_method=devig_method_for(value_policy, str(market), detail, devig_method),
                consensus_logit_pool=value_policy.consensus_logit_pool,
            )
            if anchored is not None:
                out[(event_id, market, detail)] = anchored
                if market is Market.H2H and len(prices) == 3:
                    h2h_3way[event_id] = (anchored, list(prices.keys()))
    for (event_id, market, detail), _group in grouped.items():
        if market is Market.DOUBLE_CHANCE and event_id in h2h_3way:
            anchored, selections = h2h_3way[event_id]
            # DC fair = pairwise sums of the 1X2 anchor, valid ONLY for the
            # canonical 1/X/2 order (home, Draw, away). Verify the MIDDLE outcome
            # IS the draw before treating [0]/[-1] as home/away — a feed/label
            # reorder (cf. the 1X2 Draw<->away swap) would otherwise silently
            # mis-derive every DC fair. Fail safe (skip DC) when not canonical.
            if len(selections) != 3 or selections[1] != "Draw":
                continue
            home, away = selections[0], selections[-1]
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
