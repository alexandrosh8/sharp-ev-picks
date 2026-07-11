"""Pick pipeline: snapshots -> devig -> model join -> gates -> stake -> alert.

Composition layer: pure math stays in app/probabilities|edge|risk; this module
wires it to IO (loader, dispatcher). Persistence of picks/edges to Postgres
joins in roadmap phase 2 alongside event/entity resolution.
"""

import asyncio
import logging
import math
import re
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
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
from app.probabilities.devig import (
    EXPECTED_FALLBACKS,
    DevigMethod,
    devig_with_diagnostics,
)
from app.risk.exposure import DailyExposureLedger
from app.risk.staking import (
    StakeBreakdown,
    StakePolicy,
    UncertaintyShrinkPolicy,
    recommended_stake,
    stake_amount,
    uncertainty_phi,
    uncertainty_shrink,
)
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.settlement.outcomes import is_tennis_game_line

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
    stale_drop_ratio: float = 0.0,
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
        # Fraction of this cycle's mintable candidates dropped SOLELY for
        # staleness (n_stale / candidates reaching the freshness gate). 0.0 when
        # nothing was mintable. A value near 1.0 means the scrape outran the
        # freshness window and the slate is STARVING — the self-audit/alert layer
        # watches this to catch a too-slow cycle that stale_candidates alone hides.
        "stale_drop_ratio": stale_drop_ratio,
        # Listings parsed but ZERO odds rows: selector/DOM break or anti-bot
        # wall. finished_at alone would look healthy — flag it explicitly.
        "degraded": bool(matches_found) and not snapshots,
    }


def _candidate_age_seconds(now: datetime, captured_at: datetime | None) -> float:
    """Freshness age (seconds) of a candidate's best-book price.

    SAFETY (fails CLOSED): the odds-age gate is a strict guard — the operator
    cannot take a price of unknown age. An UNKNOWABLE capture time (None) returns
    +inf so the gate ALWAYS drops the candidate rather than minting from it. (The
    prior ``... if cap else 0.0`` failed OPEN: a None cap became age 0.0 and
    sailed through the gate.) ``now`` is taken AFTER the fetch, so a captured_at
    in the FUTURE relative to it is a clock/data error (or an in-play row with a
    bad stamp), NOT a fresh price — it too fails closed (+inf). The old clamp to
    0.0 let such a stamp pose as fresh and mint."""
    if captured_at is None:
        return float("inf")
    age = (now - captured_at).total_seconds()
    if age < 0.0:
        return float("inf")  # future stamp: stale/invalid, never "fresh"
    return age


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


def _anchor_match_provenance(
    anchor_type: str,
    event_ref: str,
    provenance: Mapping[tuple[str, str], tuple[float, str]],
    *,
    api_promote_enabled: bool = False,
) -> tuple[float | None, str | None]:
    """(anchor_match_confidence, anchor_match_method) for one value pick.

    - 'pinnacle': the injector's hardened-matcher score for this event. A
      pinnacle-typed pick with NO map entry (theoretically impossible — only the
      injector produces "Pinnacle" rows on scraped events) stores None +
      'unscored': fail HONEST, never fabricate 1.0.
    - 'sharp' (inline Betfair/Smarkets): rows live on the pick's OWN canonical
      event — no pick-time matching happened, so confidence is 1.0 by
      construction (both the main-scrape inline rows and the loader-injected
      dedicated capture, which was exact-ref-matched at ingestion).
      EXCEPT under VALUE_BETFAIR_API_PROMOTE: promoted API rows were attached
      by the FUZZY hardened matcher at ingestion and are indistinguishable
      per-row from inline rows here, so with promotion enabled every sharp
      anchor stores None/'inline_or_promoted_unattributed' — fail HONEST
      (per-row snapshot provenance is the prerequisite for restoring 1.0).
    - 'consensus' (or anything else): None/None — no cross-source match exists.
    Observability only: never influences minting or anchor selection.
    """
    if anchor_type == "pinnacle":
        entry = provenance.get((event_ref, "pinnacle"))
        return entry if entry is not None else (None, "unscored")
    if anchor_type == "sharp":
        if api_promote_enabled:
            return (None, "inline_or_promoted_unattributed")
        return (1.0, "inline_betfair_canonical")
    return (None, None)


#: Pick-time sharp-anchor injector (PipelineDeps.sharp_anchor_loader): returns
#: extra OddsSnapshotIn rows (captured free Betfair/Pinnacle prices) to merge
#: into the scrape before anchoring, PLUS a match-provenance map keyed
#: ``(event_ref, anchor_type)`` -> ``(match_confidence, match_method)`` so a
#: pick can persist HOW its sharp anchor was matched (observability only).
SharpAnchorLoader = Callable[
    [str, Sequence[OddsSnapshotIn]],
    Awaitable[tuple[Sequence[OddsSnapshotIn], Mapping[tuple[str, str], tuple[float, str]]]],
]

#: Pick-time odds-history reader (PipelineDeps.steam_history_loader): given the
#: cycle's current snapshots, returns recent odds_snapshots HISTORY rows (per-book
#: time series, captured_at <= now) for those events so the steam gate can read
#: each book's trajectory. Read-only; bound to a repository at the composition
#: root, stubbed in tests. One line so ruff format is version-stable.
SteamHistoryLoader = Callable[[str, Sequence[OddsSnapshotIn]], Awaitable[Sequence[OddsSnapshotIn]]]

#: Pick-time Betfair staleness-verdict reader (PipelineDeps.staleness_verdict_
#: loader — the verdict_loader of the P3 design, sibling of sharp_anchor_loader):
#: async (sport_key) -> {event_ref: effective decision} from the persisted
#: betfair_anchor_verdicts table with the freshness TTL applied at READ time
#: (over-TTL => 'stale_api', a no-op — stale API evidence never demotes a live
#: anchor). STRICTLY a DB read: the mint path NEVER calls the Betfair API.
#: Bound to repositories.load_betfair_staleness_verdicts at the composition
#: root; stubbed in tests. A loader failure yields an empty map (type-only
#: log) and NEVER blocks minting.
StalenessVerdictLoader = Callable[[str], Awaitable[Mapping[str, str]]]

#: Task 5 n_eff source (PipelineDeps.stake_neff_lookup): SYNCHRONOUS, CHEAP
#: (cached/pre-aggregated — NEVER a per-pick blocking query) lookup of the
#: settled trusted-CLV sample count for a (strategy, sport, market) cell.
#: None (the default, and the honest fallback when a cell is unknown) means
#: the shadow annotation records n_eff=None / phi=None — never fabricated.
#: Bound at the composition root; stubbed in tests. A lookup failure is
#: isolated (type-only log) and NEVER blocks minting.
NEffLookup = Callable[[str, str, str], int | None]


class ShrinkAnnotatedStakeBreakdownOut(StakeBreakdownOut):
    """StakeBreakdownOut + the Task 5 uncertainty-shrink SHADOW annotation.

    Extends the contract model WITHOUT changing app/schemas/picks.py: pydantic
    keeps subclass instances as-is (revalidate_instances default), and
    persist_pick serializes via ``pick.stake_breakdown.model_dump()``, so the
    persisted JSON gains ``{"phi", "n_eff", "shrunk_fraction"}``. All three
    stay None when no n_eff source is wired — honest, never fabricated.
    ``final`` is UNCHANGED unless UncertaintyShrinkPolicy.enabled (default
    off; flipping it is gated by the ADR-0022 pre-registered review).
    """

    phi: float | None = None
    n_eff: int | None = None
    shrunk_fraction: float | None = None


class ValuePickOut(PickOut):
    """PickOut + value-strategy mint telemetry (Task 6, log-only).

    Same subclass pattern as ShrinkAnnotatedStakeBreakdownOut: the schema
    contract module is untouched; persist_pick feature-detects the extra
    attribute (getattr default None), so model-strategy picks and pre-column
    rows stay NULL.
    """

    # Distinct NON-SHARP books quoting this pick's devig market group at mint
    # (exactly the thin-coverage floor's number — value_policy.
    # distinct_book_count with the sharp set excluded, so injected sharp
    # anchor lines never inflate it). Anchor-thinness telemetry ONLY: nothing
    # gates on it; the age half is already covered by steam_anchor_age_seconds.
    anchor_book_count: int | None = None


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
    # Stale-starvation alarm threshold: when MORE than this fraction of a cycle's
    # mintable candidates are dropped SOLELY for staleness (the scrape outran the
    # freshness window), run_value_pipeline logs a WARNING-level "picks starving"
    # line and the ratio rides on LAST_POLL for the self-audit/alert layer. Set
    # from Settings.stale_drop_ratio_warn_threshold at the composition root; the
    # 0.5 default means "warn once a slow cycle costs us over half the slate".
    stale_drop_ratio_warn: float = 0.5
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
    # OPTIONAL Betfair staleness-verdict reader (P3 guard; default None = no
    # verdicts => guard inert). Consulted ONLY when value_policy.betfair_
    # staleness_guard is True (guard off => never called, byte-identical).
    # Under value_policy.betfair_staleness_shadow (default) the verdicts only
    # log would-demote + stamp picks.anchor_staleness_decision; anchoring is
    # unchanged. Enforce mode threads the fresh-demote event set into
    # event_fair_probs -> _named_sharp_anchor (exchange skipped -> next sharp
    # -> consensus, fail-closed). Failure is isolated: empty map + type-only
    # log, NEVER blocks minting. Wired at the composition root; tests stub it.
    staleness_verdict_loader: StalenessVerdictLoader | None = None
    # Task 5 uncertainty-shrink policy (SHADOW by default: enabled False keeps
    # the final stake bit-for-bit unchanged; phi/n_eff/shrunk_fraction only
    # ANNOTATE stake_breakdown). Built from Settings at the composition root
    # (app/config.uncertainty_shrink_policy).
    stake_shrink: UncertaintyShrinkPolicy = UncertaintyShrinkPolicy()
    # Task 5 n_eff source (see NEffLookup above). None (default) => the shadow
    # annotation records n_eff=None/phi=None — no hot-path queries, ever.
    stake_neff_lookup: NEffLookup | None = None
    # change-only persistence cache (see ODDS_SEEN_* above) — one per deps,
    # i.e. per process: both sport keys share it (event refs are distinct).
    odds_seen: OddsSeenCache = field(default_factory=dict)


def _shrink_annotated(
    deps: "PipelineDeps",
    breakdown: StakeBreakdown,
    strategy: str,
    sport_key: str,
    market: str,
) -> tuple[StakeBreakdown, int | None, float | None, float | None]:
    """Task 5 uncertainty-shrink SHADOW annotation for one staking decision.

    Returns ``(breakdown, n_eff, phi, shrunk_fraction)``. n_eff is the
    (strategy, sport, market) cell's settled trusted-CLV count from the cheap
    cached lookup (deps.stake_neff_lookup); when the lookup is unwired,
    returns None, or fails (isolated, type-only log) the annotation is
    honestly ``(breakdown, None, None, None)`` — never fabricated, never a
    hot-path query. ``breakdown`` is returned UNCHANGED unless
    deps.stake_shrink.enabled (default OFF — ADR-0022 gated), in which case
    ``final`` becomes ``min(shrunk_fraction, final)`` (the shrink can only
    ever lower a stake, never raise one).
    """
    n_eff: int | None = None
    if deps.stake_neff_lookup is not None:
        try:
            n_eff = deps.stake_neff_lookup(strategy, sport_key, market)
        except Exception as exc:  # annotation must NEVER break minting
            logger.error(
                "stake n_eff lookup failed for %s/%s/%s: %s",
                strategy,
                sport_key,
                market,
                type(exc).__name__,
            )
            n_eff = None
    if n_eff is None:
        return breakdown, None, None, None
    kappa = deps.stake_shrink.kappa
    phi = uncertainty_phi(n_eff, kappa)
    shrunk = uncertainty_shrink(breakdown.fractional, n_eff, kappa)
    if deps.stake_shrink.enabled:
        breakdown = replace(breakdown, final=min(shrunk, breakdown.final))
    return breakdown, n_eff, phi, shrunk


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
    # POST-KICKOFF DROP (leakage guard): identical protection to
    # run_value_pipeline via the SAME shared helpers. An in-play snapshot —
    # captured at or after its event's kickoff — must never mint a model pick
    # nor stamp a CLV close. The raw ``snapshots`` (persisted/counted above)
    # stay intact; only the pricing view drops in-play rows. ``started`` then
    # skips a started event wholesale (belt-and-suspenders for an event whose
    # surviving rows are pre-match but which has since kicked off). The
    # persisted ``events.starts_at`` is preferred over the ephemeral directory,
    # and a NULL/unknown kickoff is KEPT (cannot be proven post-KO).
    kickoff_by_event = await _load_kickoffs(deps, {s.event_id for s in snapshots})
    priced_snapshots = drop_post_kickoff_snapshots(snapshots, kickoff_by_event)
    started = started_event_ids({s.event_id for s in priced_snapshots}, kickoff_by_event, now)
    fair = _fair_probabilities(priced_snapshots, deps.devig_method)
    picks: list[PickOut] = []
    n_unpersisted_withheld = 0

    for event_id in sorted({s.event_id for s in priced_snapshots}):
        if event_id in started:
            continue  # kicked off: in-play odds never become picks/upgrades
        predictions = {(p.market, p.selection): p for p in await deps.model.predict(event_id)}
        if not predictions:
            continue
        for snap in (s for s in priced_snapshots if s.event_id == event_id):
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
                # Fail-closed freshness age (same handling as the value path's
                # _candidate_age_seconds): a captured_at in the FUTURE relative
                # to now — taken AFTER the fetch — is provider clock skew, not
                # a fresh price. The raw signed age_seconds() let the negative
                # age sail through the odds-age gate; +inf always drops it.
                odds_age_seconds=_candidate_age_seconds(now, snap.captured_at),
                liquidity=snap.liquidity or 0.0,
                bookmaker=snap.bookmaker,
            )
            decision = evaluate(candidate, deps.gate_policy)
            if not decision.accepted:
                continue

            # Kelly on the COMMISSION-NETTED price (decision.effective_odds),
            # never the gross exchange odds — same netting the value strategy
            # applies via best_odds_effective (audit 2026-07-09). Non-exchange
            # books have effective_odds == decimal_odds (bit-identical).
            breakdown = recommended_stake(
                prediction.probability, decision.effective_odds, deps.stake_policy
            )
            # Task 5 uncertainty-shrink SHADOW annotation (default: final
            # unchanged; phi/n_eff/shrunk ride stake_breakdown only).
            breakdown, shrink_n_eff, shrink_phi, shrunk_fraction = _shrink_annotated(
                deps, breakdown, "model", sport_key, str(snap.market)
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
                # Mint-time CANONICAL devig-group detail: the CLV true-up
                # matches this pick's close on the EXACT group, bypassing the
                # line-blind ambiguity guard. None for lineless markets.
                market_detail=canonical_market_detail(snap.market_detail),
                bookmaker=snap.bookmaker,
                decimal_odds=snap.decimal_odds,
                model_probability=prediction.probability,
                fair_probability=fair_p,
                edge=decision.edge,
                ev=decision.ev,
                confidence=prediction.confidence,
                recommended_stake_fraction=breakdown.final,
                recommended_stake_amount=stake_amount(breakdown.final, deps.bankroll),
                stake_breakdown=ShrinkAnnotatedStakeBreakdownOut(
                    raw_kelly=breakdown.raw_kelly,
                    fractional=breakdown.fractional,
                    capped=breakdown.capped,
                    final=breakdown.final,
                    daily_clipped=False,
                    # Task 5 SHADOW annotation (all None when no n_eff source).
                    phi=shrink_phi,
                    n_eff=shrink_n_eff,
                    shrunk_fraction=shrunk_fraction,
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
            # SHIELDED: the watchdog must never cancel between the persist and
            # its reservation (see _persist_and_reserve).
            outcome, staked = await asyncio.shield(
                _persist_and_reserve(deps, pick, breakdown, snap.event_id, now)
            )
            if staked is None:
                # brand-new pick with no remaining daily/event capacity: skip it
                logger.info("daily exposure cap reached; skipping %s", snap.selection)
                continue
            pick = staked
            if outcome == "unpersisted" and _persistence_configured(deps):
                # WP2 fail-closed: persistence is configured but THIS pick could
                # not be persisted (DB outage / unresolvable event) — it can
                # never be settled, ledger-seeded, or CLV-tracked, so withhold.
                n_unpersisted_withheld += 1
                continue
            if outcome in ("inserted", "upgraded", "unpersisted"):
                picks.append(pick)
            await deps.dispatcher.dispatch(
                build_pick_alert(
                    pick,
                    model_name=deps.model_name,
                    model_version=deps.model_version,
                )
            )

    if n_unpersisted_withheld:
        logger.warning(
            "withheld %d premium alert(s) for %s: pick persistence failed (fail closed)",
            n_unpersisted_withheld,
            sport_key,
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


async def _load_kickoffs(deps: "PipelineDeps", event_ids: set[str]) -> dict[str, datetime | None]:
    """event_id -> best-known kickoff (UTC), PREFERRING the persisted
    ``events.starts_at`` over the ephemeral in-memory directory.

    Persisted wins because a late-arriving or absent in-memory kickoff (tennis
    start times land late) would otherwise let an in-play price mint a pre-match
    pick / stamp a CLV close. Falls back to the directory for any event not yet
    persisted (or when there is no DB). A DB read failure degrades to the
    directory alone — kickoff hygiene must never block picking."""
    kickoffs: dict[str, datetime | None] = {}
    if deps.session_factory is not None:
        from app.storage.repositories import load_event_kickoffs

        try:
            async with deps.session_factory() as session:
                kickoffs = await load_event_kickoffs(session, event_ids)
        except Exception as exc:  # kickoff read must never break picking
            logger.error("kickoff load failed: %s (directory fallback)", type(exc).__name__)
            kickoffs = {}
    if deps.directory is not None:
        for event_id in event_ids:
            if kickoffs.get(event_id) is None:
                teams = deps.directory.lookup(event_id)
                if teams is not None and teams.starts_at is not None:
                    kickoffs[event_id] = teams.starts_at
    return kickoffs


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


# Sub-markets the settler cannot grade from a full-time home/away score — period
# markets (halves / quarters / sets), corners, and cards/bookings. The results
# feed carries no score for them, so a pick on one can only ever VOID (or
# mis-grade as full-match), cluttering the ledger and corrupting the void-rate.
# Detected on the line-qualified market_detail at the candidate boundary.
_NON_SETTLEABLE_DETAIL_RE = re.compile(
    r"(?:1st_half|2nd_half|first_half|second_half|_quarter|1st_set|2nd_set|3rd_set|_set_|corner|card|booking)"
)


def _is_settleable_market_detail(detail: str | None) -> bool:
    """False for period / corner / card sub-markets that can never be graded from
    a final score (they only ever void). A None / full-match detail is
    settleable — the settler grades it from the final home/away score."""
    if not detail:
        return True
    return _NON_SETTLEABLE_DETAIL_RE.search(detail.lower()) is None


def _is_tennis_game_line_group(
    sport_key: str,
    market: Market,
    prices: Mapping[str, Mapping[str, float]],
) -> bool:
    """True for a tennis totals/spreads candidate group priced on a GAME line
    (totals line > 4.5 or |spread| > 2.5, parsed from the selection tails via
    the settler's own line parser). Our tennis results feed carries SET scores
    only, so a game-line pick can never be auto-settled honestly — the
    settlement set-score guard would hold it for manual entry forever. Dropped
    at the candidate boundary, same mechanism as the period/corner/card
    sub-market drop above. Set-plausible tennis lines (sets total 2.5, set
    spread 1.5) and every other sport pass through untouched."""
    if sport_key != "tennis" or market not in (Market.TOTALS, Market.SPREADS):
        return False
    return any(is_tennis_game_line(str(market), sel) for sel in prices)


# Cross-provider vocabulary equivalences for the SAME full-match line
# (instrumented live evidence 2026-07-10: 'h2h'/None, 'btts'/None,
# 'over_under_2_5'/'totals_2_5' collisions were skipping every CLV write on
# the affected picks). ONLY provably line-identical classes are folded:
# h2h/1x2/btts have no full-match line variants, and over_under_X_Y carries
# the identical line encoding as totals_X_Y. Asian-handicap vs spreads_minus
# is deliberately NOT folded — audited UNSAFE 2026-07-10 (the OddsChecker
# spreads_* key space mixes 2-way AH and 3-way EH products on identical
# selection strings, key sign conventions are producer-dependent, and +L/-L
# books coexist per event, so no selection-independent canonical form keeps
# different books apart) — see
# docs/research/2026-07-10-ah-spreads-vocabulary-audit.md. That class stays
# fail-closed; stamped picks bypass it via the exact-detail match instead.
_LINELESS_DETAILS = frozenset({"h2h", "1x2", "btts"})
_OU_DETAIL_RE = re.compile(r"^over_under_(\d+(?:_\d+)?)$")


def canonical_market_detail(detail: str | None) -> str | None:
    """Canonical detail label for one full-match devig group (see above).

    Pure (str/re only). Used BOTH by the CLV true-up's vocabulary merge and
    as the mint-time ``PickOut.market_detail`` stamp, so a pick minted from
    one provider's vocabulary matches the close group of another. NOTE: the
    lineless classes canonicalize to None — such picks persist a NULL
    market_detail and follow the legacy line-blind path (already collision-
    free for them after the vocabulary merge)."""
    if detail is None:
        return None
    d = detail.lower()
    if d in _LINELESS_DETAILS:
        return None
    m = _OU_DETAIL_RE.match(d)
    if m:
        return f"totals_{m.group(1)}"
    return detail


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


PersistOutcome = Literal["inserted", "upgraded", "duplicate", "duplicate_denied", "unpersisted"]


def _persistence_configured(deps: "PipelineDeps") -> bool:
    """True when the deps CAN persist picks (session factory + directory set).

    Splits "unpersisted" into two premium policies (WP2): with persistence
    CONFIGURED, an unpersisted pick is a FAILURE (DB outage / unresolvable
    event) — it can never be settled, ledger-seeded, or CLV-tracked, so its
    alert is withheld (fail closed). With persistence deliberately UNCONFIGURED
    the operator accepted no accounting and the alert flows as before."""
    return deps.session_factory is not None and deps.directory is not None


async def _maybe_persist(deps: "PipelineDeps", pick: PickOut, event_id: str) -> PersistOutcome:
    """Persist the pick to the DB when a session factory + directory are set.

    Passes through repositories.persist_pick's outcome ("inserted" /
    "upgraded" / "duplicate" / "duplicate_denied"); "unpersisted" means
    persistence was unavailable (no DB/directory/teams) or this write failed.
    PREMIUM callers withhold the alert of an "unpersisted" pick when
    persistence is configured (fail closed — see _persistence_configured) and
    reserve nothing either way. VOLUME callers drop "unpersisted" picks: a
    shadow pick that never reaches the DB can accumulate no CLV evidence,
    which is its only purpose.
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


async def _persist_stake_clip(
    deps: "PipelineDeps", pick: PickOut, event_id: str, *, persist_tier: bool = False
) -> None:
    """Rewrite the persisted row's stake to the daily-clipped amount (BUG 2).

    The row was persisted with the pre-clip (per-bet-capped) stake BEFORE the
    daily-exposure reservation ran; when that reservation clips the stake, the
    stored/reported value must be brought in line with what the ledger actually
    reserved, or the persisted stake escapes the daily cap. Best-effort, with
    the SAME guards as `_maybe_persist`: without a session factory, directory,
    or resolvable teams there is no row to correct, and a failure here must
    never break alerting (the in-memory pick already carries the clip).

    ``persist_tier=True`` (the stake-zero demotion) additionally rewrites the
    row's tier + reason_summary from the pick — see update_pick_stake."""
    if deps.session_factory is None or deps.directory is None:
        return
    teams = deps.directory.lookup(event_id)
    if teams is None:
        return
    from app.storage import repositories

    try:
        async with deps.session_factory() as session:
            await repositories.update_pick_stake(
                session,
                pick,
                teams,
                deps.model_name,
                deps.model_version,
                persist_tier=persist_tier,
            )
            await session.commit()
    except Exception as exc:  # persistence must never break alerting
        logger.error(
            "pick stake-clip persistence failed for %s: %s", pick.pick_id, type(exc).__name__
        )


async def _record_candidate_audit(
    deps: "PipelineDeps",
    pick: PickOut,
    market_detail: str,
    reasons: tuple[str, ...],
    anchor_age_seconds: float | None,
    now: datetime,
) -> None:
    """Stage ONE candidate-evaluation audit row (external-audit #3 + fill #2).

    Pure MEASUREMENT: records the tier a candidate landed in ('premium' kept vs
    'volume' demoted/shadow), the demotion reason slug(s) that fired (empty for a
    clean premium keep), and the anchor/fill provenance behind it — so later ROI
    diagnosis can tune false positives, tier demotions, and fill realism. It NEVER
    gates minting; a failure here must NEVER drop or alter a real pick, so it is
    fully isolated and type-only logged (project logging rule — never the exception
    string or a URL). Mirrors _maybe_persist's own-session pattern (the pipeline
    opens a session per pick, not a shared one); evaluated_at=now makes the write
    idempotent per cycle (ON CONFLICT DO NOTHING on the cycle key).

    The realistic (post-book-allowlist) fill is recorded via best_book/best_odds/
    edge — the fill side of audit #2. The theoretical-vs-fill GAP number itself has
    no column on CandidateEvaluationInput, so it is not persisted here (a schema
    extension would be needed — see app/storage/candidate_audit.py).
    """
    if deps.session_factory is None:
        return
    from sqlalchemy import select

    from app.storage.candidate_audit import (
        CandidateEvaluationInput,
        record_candidate_evaluation,
    )
    from app.storage.models import Event

    try:
        async with deps.session_factory() as session:
            event_pk = await session.scalar(
                select(Event.id).where(Event.external_ref == pick.event_id)
            )
            if event_pk is None:
                return  # event not persisted (e.g. unpersisted pick): no FK target
            await record_candidate_evaluation(
                session,
                CandidateEvaluationInput(
                    event_id=event_pk,
                    sport_key=pick.sport,
                    market=str(pick.market),
                    market_detail=market_detail,
                    selection=pick.selection,
                    tier=pick.tier,
                    reasons=reasons,
                    anchor_book=pick.anchor_book,
                    anchor_type=pick.anchor_type,
                    anchor_age_seconds=(
                        Decimal(str(anchor_age_seconds)) if anchor_age_seconds is not None else None
                    ),
                    best_book=pick.bookmaker,
                    best_odds=Decimal(str(pick.decimal_odds)),
                    edge=Decimal(str(pick.edge)),
                    # PickOut.model_probability carries the sharp devigged fair prob
                    # (v.sharp_fair_prob) the edge was measured against.
                    fair_probability=Decimal(str(pick.model_probability)),
                    evaluated_at=now,
                ),
            )
            await session.commit()
    except Exception as exc:  # audit write must NEVER break the pick flow
        logger.error(
            "candidate audit write failed for %s/%s: %s",
            pick.sport,
            pick.event_id,
            type(exc).__name__,
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
      row was stored pre-clip — BUG 2). A ZERO grant (cap exhausted) returns
      None (never a stake-0 alert — pick 2332 mis-stated the strategy) AND
      DEMOTES the persisted row — stored at FULL stake before the reservation
      ran — to the volume tier at stake 0 with the 'stake_zero' note (Task 1,
      2026-07-10). The demoted row stays CLV-tracked and, because it is
      tier='volume', a later premium re-detection takes persist_pick's
      volume->premium UPGRADE path — the denial lifts once daily capacity
      frees (the old premium stake-0 'duplicate_denied' marker was permanent).
    - duplicate: already persisted AND dispatch-eligible at insert (stake > 0);
      reserve NOTHING (a re-detection is not new exposure — its budget was
      consumed on its creation day, which is exactly what seed_exposure_ledger
      re-counts after a restart). The pick keeps breakdown.final.
    - duplicate_denied: the persisted row is a cap-denial marker (stake 0) —
      never dispatch the alert the cap already refused (returns None).
    - unpersisted: DB state is unknown; reserve NOTHING it could never release,
      so a sustained-unpersisted pick cannot accumulate standing daily exposure
      across cycles and silently exhaust the cap (kelly-risk-r2-1).
    """
    if outcome == "duplicate_denied":
        return None
    if outcome in ("duplicate", "unpersisted"):
        return pick
    granted = deps.ledger.reserve(now.date(), breakdown.final, event_id)
    if granted <= 0.0 and outcome in ("inserted", "upgraded"):
        # WP2 + Task 1 (stake_zero): the row was already persisted at full
        # stake — a zero grant must NEVER alert (an 'upgraded' zero grant used
        # to fire the premium alert at stake 0: pick 2332). Demote the row to
        # the volume tier at stake 0 with the stake_zero note: still
        # CLV-tracked, reserving nothing, and re-promotable via persist_pick's
        # volume->premium upgrade path once daily capacity frees.
        demoted = pick.model_copy(
            update={
                "tier": "volume",
                "recommended_stake_fraction": 0.0,
                "recommended_stake_amount": stake_amount(0.0, deps.bankroll),
                "stake_breakdown": pick.stake_breakdown.model_copy(
                    update={"final": 0.0, "daily_clipped": True}
                ),
                "reason_summary": pick.reason_summary
                + " | stake_zero: daily exposure cap granted 0 — demoted to volume",
            }
        )
        await _persist_stake_clip(deps, demoted, event_id, persist_tier=True)
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


async def _persist_and_reserve(
    deps: "PipelineDeps",
    pick: PickOut,
    breakdown: StakeBreakdown,
    event_id: str,
    now: datetime,
) -> tuple[PersistOutcome, PickOut | None]:
    """Persist + daily-exposure reserve as ONE unit, run under ``asyncio.shield``.

    The scheduler watchdog (asyncio.wait_for) can cancel a cycle at ANY await;
    a cancellation landing between a pick's DB persist and its ledger
    reservation left a full-stake premium row the daily/per-event caps never
    counted (until a restart re-ran seed_exposure_ledger) — the next cycle's
    re-detected 'duplicate' deliberately reserves nothing, and its alert still
    dispatched (2026-07-08 audit). Shielding this pair lets an in-flight
    persist run on to its reservation while the cycle itself still cancels
    promptly; the alert dispatch stays OUTSIDE the shield, so a cancelled
    cycle's alert is simply lost and the next cycle's duplicate re-dispatches
    it — with the exposure already accounted."""
    outcome = await _maybe_persist(deps, pick, event_id)
    staked = await _reserve_for_outcome(deps, pick, breakdown, outcome, event_id, now)
    return outcome, staked


# Schema tag for the policy fingerprint encoding — bump if the field set or the
# format ever changes so a stored string is never ambiguous about its scheme.
POLICY_FINGERPRINT_SCHEMA = "p1"


def policy_fingerprint(
    *,
    value_min_edge: float,
    value_volume_min_edge: float,
    value_min_odds: float,
    devig_method: DevigMethod,
    require_sharp_anchor: bool,
    max_edge: float,
    ml_manifest_created_utc: str | None = None,
    ml_threshold: float | None = None,
) -> str:
    """Compact, human-debuggable encoding of the live value-strategy policy that
    minted a pick (H3) — so CLV attribution can SCOPE rows to their exact policy
    regime instead of mixing regimes across config changes, and a pick can be
    replayed against the policy that made it.

    Pure + deterministic: identical inputs -> identical string; ANY change to a
    threshold, the devig method, the require-sharp-anchor gate, the data-error
    ceiling, or the enforced ML manifest -> a different string. Decoded by eye::

        p1|me=0.0150|vme=0.0150|mo=1.30|dv=power|rsa=0|mxe=inf|ml=off

    ``ml`` is ``off`` unless the value-filter is ENFORCING (not shadow), in which
    case it carries the manifest identity ``<created_utc>@<q*>`` — a newer
    manifest (different created_utc) or a moved operating point is a new regime.
    """
    ceiling = "inf" if math.isinf(max_edge) else f"{max_edge:.4f}"
    if ml_manifest_created_utc is not None and ml_threshold is not None:
        ml = f"{ml_manifest_created_utc}@{ml_threshold:.3f}"
    else:
        ml = "off"
    return (
        f"{POLICY_FINGERPRINT_SCHEMA}"
        f"|me={value_min_edge:.4f}"
        f"|vme={value_volume_min_edge:.4f}"
        f"|mo={value_min_odds:.2f}"
        f"|dv={devig_method.value}"
        f"|rsa={int(require_sharp_anchor)}"
        f"|mxe={ceiling}"
        f"|ml={ml}"
    )


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
        dc_candidate_plausible,
        find_value_bets_with_fair,
        is_sharp_anchored,
        structural_sanity_violation,
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
    # (event_ref, anchor_type) -> (match_confidence, match_method): HOW each
    # injected sharp anchor was matched to its fixture (observability only —
    # persisted per pick as anchor_match_confidence/anchor_match_method).
    anchor_provenance: Mapping[tuple[str, str], tuple[float, str]] = {}
    if deps.sharp_anchor_loader is not None:
        try:
            extra, anchor_provenance = await deps.sharp_anchor_loader(sport_key, snapshots)
        except Exception as exc:
            logger.error("sharp-anchor injection failed for %s: %s", sport_key, type(exc).__name__)
            extra, anchor_provenance = [], {}
        if extra:
            anchor_snapshots = [*snapshots, *extra]
            logger.info(
                "value pipeline %s: merged %d free sharp-anchor snapshot(s) for pick anchoring",
                sport_key,
                len(extra),
            )
    # BETFAIR STALENESS GUARD (P3): read the latest persisted API-vs-inline
    # verdicts (a DB read — the mint path NEVER calls the Betfair API). Guard
    # off (the default) => the loader is never called: byte-identical. The
    # loader applies the freshness TTL at read time, so an over-TTL verdict
    # arrives as 'stale_api' (a no-op — stale API evidence never demotes a
    # live anchor); only a FRESH 'demote' can alter anchoring, and only when
    # shadow mode is off. Loader failure => empty map, type-only log, minting
    # proceeds untouched (fail-open on missing evidence).
    staleness_verdicts: Mapping[str, str] = {}
    if deps.value_policy.betfair_staleness_guard and deps.staleness_verdict_loader is not None:
        try:
            staleness_verdicts = await deps.staleness_verdict_loader(sport_key)
        except Exception as exc:  # verdict read must NEVER block minting
            logger.error(
                "betfair staleness verdict load failed for %s: %s (guard no-op this cycle)",
                sport_key,
                type(exc).__name__,
            )
            staleness_verdicts = {}
    demote_events = frozenset(
        ref for ref, decision in staleness_verdicts.items() if decision == "demote"
    )
    enforced_demotions: frozenset[str] = frozenset()
    if demote_events and deps.value_policy.betfair_staleness_shadow:
        # SHADOW (rollout default): log the would-demote set + stamp the picks
        # below, but leave anchoring UNCHANGED — measure before enforcing.
        logger.info(
            "betfair staleness guard SHADOW %s: would demote the exchange anchor on "
            "%d event(s) (fresh API disagreement > threshold ticks) — anchoring unchanged",
            sport_key,
            len(demote_events),
        )
    elif demote_events:
        enforced_demotions = demote_events
        logger.info(
            "betfair staleness guard %s: demoting the exchange anchor on %d event(s) "
            "(fresh API disagreement) — falls to next sharp book / consensus",
            sport_key,
            len(demote_events),
        )
    # POST-KICKOFF DROP (leakage guard): filter IN-PLAY snapshots — captured at
    # or after their event's kickoff — out of the ANCHORING/pricing set before
    # grouping. A late-arriving kickoff (tennis) would otherwise let OddsPortal's
    # in-play page price a pre-match value pick and stamp a fabricated CLV close.
    # The persisted kickoff is preferred over the ephemeral directory; the raw
    # ``snapshots`` (persisted/counted below) is left intact — only the pricing
    # view drops in-play rows. The kickoff REFRESH runs FIRST: a match moved
    # EARLIER arrives via the fresh scrape (directory), and because
    # _load_kickoffs prefers the persisted time, refreshing AFTER the guard
    # (the old ordering) let the stale persisted kickoff price the started
    # match from in-play odds for one whole cycle (2026-07-08 audit).
    await _refresh_kickoffs(deps, {s.event_id for s in snapshots})
    kickoff_by_event = await _load_kickoffs(deps, {s.event_id for s in anchor_snapshots})
    anchor_snapshots = drop_post_kickoff_snapshots(anchor_snapshots, kickoff_by_event)
    grouped = group_market_prices(anchor_snapshots)
    fair = event_fair_probs(
        grouped,
        deps.devig_method,
        deps.value_policy,
        liquidity_by_market=group_market_liquidity(anchor_snapshots),
        exchange_demoted_events=enforced_demotions,
    )
    persisted = await _persist_snapshots(deps, snapshots, sport_key, deps.league or sport_key, now)

    # In-play gate: a listed match can kick off between page listing and its
    # scrape (multi-minute cycles); OddsPortal then serves IN-PLAY prices.
    # Those must never mint, upgrade, or re-alert picks — the operator
    # cannot take a pre-match price on a started game.
    # Persisted-preferred kickoff (see kickoff_by_event above): a late/absent
    # in-memory kickoff still excludes a started event. The per-snapshot drop
    # already removed in-play rows; this event-level skip is belt-and-suspenders.
    started = started_event_ids({key[0] for key in grouped}, kickoff_by_event, now)

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
    # PREMIUM persist+reserve+alert is DEFERRED out of the candidate loop into
    # this per-cycle list of (pick, breakdown, event_id). Reserving inline
    # binds the daily-exposure cap in ITERATION order, so on a busy slate a
    # high-growth pick iterated late loses the budget to a low-growth pick
    # iterated early. Collected here, then persisted+reserved AFTER the loop
    # edge-ranked by raw_kelly so the highest-growth picks fund first
    # (intra-cycle ordering only; raw_kelly uses already-computed fair/price —
    # no leakage). Deferring the PERSIST too keeps it adjacent to its reserve:
    # the pair runs as one shielded unit (see _persist_and_reserve), so a
    # watchdog cancellation anywhere in the cycle can never leave a persisted
    # full-stake row the ledger doesn't count (2026-07-08 audit).
    premium_candidates: list[tuple[PickOut, StakeBreakdown, str]] = []
    n_volume = 0
    n_stale = 0
    # Denominator for the stale-drop RATIO: candidates reaching the freshness
    # gate (i.e. cleared the AH guard) — the mintable universe this cycle.
    n_freshness_candidates = 0
    n_ml_demoted = 0
    n_major_demoted = 0
    n_no_sharp_demoted = 0
    n_steam_demoted = 0
    n_steam_shadow = 0
    n_experimental = 0
    n_off_band = 0
    n_thin_books = 0
    n_non_settleable = 0
    n_tennis_game_line = 0
    n_visibility_capped = 0
    n_ah_rejected = 0
    n_sanity_dropped = 0
    n_dc_rejected = 0
    n_moneyline_capped = 0
    n_sanity_demoted = 0
    # Task 8 probe (Buchalter bet-volume smoke detector, log-only): market
    # groups that reached the value scan this cycle (anchored fair present).
    n_eligible_markets = 0
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
    # Stamp the LIVE policy regime once per cycle (constant across this cycle's
    # candidates) so every minted pick records the exact thresholds/devig/anchor-
    # gate/ML-manifest that produced it (H3). The ML manifest identity rides the
    # fingerprint ONLY when enforcement is actually on (flag set + a loaded,
    # non-shadow model) — a shadow/annotation-only manifest changes no behavior,
    # so it must not change the regime tag.
    ml_created_utc: str | None = None
    ml_threshold: float | None = None
    if (
        deps.value_ml_filter_enabled
        and deps.value_filter is not None
        and not deps.value_filter.shadow
    ):
        ml_created_utc = deps.value_filter.manifest_created_utc
        ml_threshold = deps.value_filter.threshold
    fingerprint = policy_fingerprint(
        value_min_edge=deps.value_min_edge,
        value_volume_min_edge=deps.value_volume_min_edge,
        value_min_odds=deps.value_min_odds,
        devig_method=deps.devig_method,
        require_sharp_anchor=deps.value_policy.require_sharp_anchor,
        max_edge=deps.value_policy.max_edge,
        ml_manifest_created_utc=ml_created_utc,
        ml_threshold=ml_threshold,
    )
    for (event_id, market, detail), (prices, captured) in grouped.items():
        if event_id in started:
            continue  # kicked off: in-play odds never become picks/upgrades
        # NON-SETTLEABLE market drop: period (half/quarter/set), corner, and
        # card/booking sub-markets have no score in the results feed, so any pick
        # on them can only ever void (or mis-grade as full-match). Drop the whole
        # group at the candidate boundary — never mint a pick that cannot grade.
        if not _is_settleable_market_detail(detail):
            n_non_settleable += 1
            continue
        # TENNIS GAME-LINE drop: our tennis results feed carries SET scores
        # only, so a totals/spreads candidate on a GAME-sized line ("Over
        # 22.5", "Muchova -4.5") can never be auto-settled honestly — the
        # settlement set-score guard would hold such a pick for manual entry
        # forever. Same mechanism as the non-settleable sub-market drop above.
        if _is_tennis_game_line_group(sport_key, market, prices):
            n_tennis_game_line += 1
            continue
        # Per-market book-count floor (default 0 = off): a market quoted by
        # too few books is skipped wholesale — scaffolding for new lines/
        # divisions where thin coverage makes the anchor untrustworthy.
        # Task 6 anchor-thinness telemetry: the SAME distinct-soft-book count
        # the thin-coverage floor gates on, hoisted so every minted pick can
        # persist it (log-only — nothing new gates on it).
        anchor_book_count = distinct_book_count(prices, exclude=_sharp_norm)
        min_books = min_books_for(deps.value_policy, str(market), detail)
        if min_books and anchor_book_count < min_books:
            n_thin_books += 1
            continue
        anchored = fair.get((event_id, market, detail))
        if anchored is None:
            continue  # no trustworthy fair value for this market
        anchor_book, fair_by_sel = anchored
        # Buchalter bet-volume smoke detector (Task 8 probe, log-only): this
        # group reached the value scan — an ELIGIBLE market this cycle.
        n_eligible_markets += 1
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
            # DOUBLE-CHANCE IMPLAUSIBILITY guard (app/edge/value.dc_candidate_plausible):
            # a DERIVED DC fair (summed from the 1X2 anchor) that disagrees wildly with
            # the soft DC price signals a stale/mislabeled/swapped 1X2 anchor — reject
            # the candidate BEFORE it mints any pick (premium OR shadow). Scoped to
            # double_chance; complements the structural-sanity DEMOTE below (this fires
            # even when the edge sits below the sanity ceiling).
            if market is Market.DOUBLE_CHANCE and not dc_candidate_plausible(
                v, max_sharp_soft_ratio=deps.value_policy.dc_max_sharp_soft_ratio
            ):
                n_dc_rejected += 1
                continue
            # (The 1X2/moneyline odds ceiling is applied as a SHADOW-tier CAP in the
            # demotion chain below — NOT a hard drop — so the CLV-negative longshot
            # band keeps accruing forward CLV on own-captured data, never alerted or
            # staked. See the moneyline_note gate. ADR-0019 H1 self-validation.)
            # Freshness gate (a candidate that cleared the AH guard is part of
            # the mintable universe — count it so the stale-drop RATIO below is
            # measured against the right denominator).
            n_freshness_candidates += 1
            cap = captured.get((v.selection, v.best_book))
            age = _candidate_age_seconds(now, cap)
            if age > deps.gate_policy.max_odds_age_seconds:
                n_stale += 1
                continue
            # Odds-band refinement (default empty = off): RAW best odds must
            # fall inside a configured band — same convention as the
            # value_min_odds floor, which also gates on raw odds.
            if not odds_in_bands(v.best_odds, deps.value_policy.odds_bands):
                n_off_band += 1
                continue
            # Market-agnostic SANITY ceiling: the per-market AH/DC guards do not
            # cover spreads/handicaps, so mis-mapped longshots (100-151.0) reached
            # alerted picks with impossible EV. Drop above the ceiling, never alert.
            if float(v.best_odds) > _SANITY_MAX_ODDS:
                n_sanity_dropped += 1
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
            # 1X2/MONEYLINE ODDS CEILING (research 2026-06-30, ADR-0019 H1): a PREMIUM
            # H2H candidate whose RAW best price exceeds moneyline_max_odds is in the
            # structurally CLV-NEGATIVE away/draw LONGSHOT band (held-out CLV -0.087,
            # >4 SE; favourite-longshot bias). CAP it at the volume (shadow) tier —
            # persisted + CLV-tracked, NEVER alerted, NEVER reserving exposure — so the
            # alerted set stays longshot-free AND the band self-validates forward on
            # own-captured Pinnacle+BSP data (drop→shadow, so the evidence keeps
            # flowing). Scoped to H2H; ValuePolicy default math.inf = OFF (Settings
            # sets 5.0). OU/AH/totals rarely price this high, so they are untouched.
            moneyline_note = ""
            if (
                tier == "premium"
                and market is Market.H2H
                and v.best_odds > deps.value_policy.moneyline_max_odds
            ):
                tier = "volume"
                n_moneyline_capped += 1
                moneyline_note = " | 1X2 longshot > odds ceiling: capped at volume (shadow)"
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
            # A5 SHADOW-VERDICT stamps (observability only — persisted on the
            # pick so future settled evidence can judge the OFF gate). None =
            # never evaluated (gate unconfigured / consensus anchor / eval
            # error); False = evaluated clean; True = would demote.
            steam_tripped: bool | None = None
            steam_reasons: str | None = None
            steam_closed_fraction: float | None = None
            steam_anchor_age_seconds: float | None = None
            verdict = None
            steam_gate_policy = deps.steam_policy
            if steam_gate_policy is not None and anchor_book != CONSENSUS_ANCHOR:
                try:
                    market_traj = steam_trajectories.get((event_id, market, detail), {})
                    verdict = evaluate_steam(
                        fill_trajectory=lookup_trajectory(market_traj, v.selection, v.best_book),
                        anchor_trajectory=lookup_trajectory(market_traj, v.selection, anchor_book),
                        now=now,
                        policy=steam_gate_policy,
                    )
                except Exception as exc:  # steam eval must NEVER break picking
                    # (A5 fail-safe: the shadow stamps stay NULL — never fabricated)
                    logger.error(
                        "steam eval failed for %s/%s: %s",
                        sport_key,
                        event_id,
                        type(exc).__name__,
                    )
            if verdict is not None and steam_gate_policy is not None:
                steam_tripped = verdict.tripped
                steam_reasons = ",".join(verdict.reasons) if verdict.reasons else None
                steam_closed_fraction = verdict.closed_fraction
                steam_anchor_age_seconds = verdict.anchor_age_seconds
                if verdict.tripped:
                    reason_str = ",".join(verdict.reasons)
                    if steam_gate_policy.enabled and tier == "premium":
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
            # FIX 1 — STRUCTURAL-SANITY HARD-DEMOTE (market-agnostic safety net).
            # Runs LAST among the demotion gates, at the value-mint chokepoint:
            # a premium candidate whose (fair, offered, edge) triple is
            # structurally impossible — edge above the SEPARATE, stricter sanity
            # ceiling (value_policy.sanity_max_edge, default 0.15), an inverted
            # fair/offered pair, or an offered price below its own min-acceptable
            # floor — is HARD-DEMOTED to the volume (shadow) tier: still
            # persisted + CLV-tracked, NEVER alerted, NEVER a silent drop. This is
            # the permanent backstop that stops the phantom impossible-edge picks
            # from EVER alerting as premium, regardless of which upstream data
            # defect produced them (spreads mispairing, totals line-loss, a
            # derived double-chance fair inheriting a stale/mislabeled anchor).
            # The existing value_max_edge (0.20) data-error cap and the overround
            # gate are left intact — this is a SEPARATE, stricter ceiling. The
            # `tier == "premium"` guard means an already-demoted candidate stays
            # volume (the gates never stack confusingly).
            sanity_note = ""
            if tier == "premium" and structural_sanity_violation(
                v,
                min_edge=premium_floor,
                sanity_max_edge=deps.value_policy.sanity_max_edge,
            ):
                tier = "volume"
                n_sanity_demoted += 1
                sanity_note = (
                    " | STRUCTURAL SANITY: impossible fair/offered pair — demoted to shadow"
                )
            # Stake from the sharp fair prob at the EFFECTIVE (net) price. The
            # daily-exposure ledger is consumed AFTER persistence (below), and
            # ONLY for brand-new premium detections — so the pick is built with
            # the per-bet-capped breakdown.final and a re-alert can reproduce it.
            breakdown = recommended_stake(
                v.sharp_fair_prob, v.best_odds_effective, deps.stake_policy
            )
            # Task 5 uncertainty-shrink SHADOW annotation (default: final
            # unchanged; phi/n_eff/shrunk ride stake_breakdown only).
            breakdown, shrink_n_eff, shrink_phi, shrunk_fraction = _shrink_annotated(
                deps, breakdown, "value", sport_key, str(market)
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

            anchor_match_confidence, anchor_match_method = _anchor_match_provenance(
                anchor_type_for(v.sharp_book),
                event_id,
                anchor_provenance,
                api_promote_enabled=deps.value_policy.betfair_api_promote,
            )
            # Betfair staleness-guard mint stamp (OBSERVABILITY only — never
            # gates): the event's effective verdict read this cycle. Scoped to
            # H2H (the only market the API capture covers, v1); None when the
            # guard is off (verdicts never loaded) or no verdict exists. Under
            # SHADOW a 'demote' stamp marks a WOULD-demote (anchoring
            # unchanged); under enforce the anchor above already fell through.
            anchor_staleness_decision = (
                staleness_verdicts.get(event_id) if market is Market.H2H else None
            )
            pick: PickOut = ValuePickOut(
                pick_id=str(uuid.uuid4()),
                sport=sport_key,  # one deps serves soccer AND basketball polls
                league=league_label,
                event=event_label,
                event_id=event_id,
                market=market,
                selection=v.selection,
                # Mint-time CANONICAL devig-group detail (this candidate's
                # group `detail` canonicalized): the CLV true-up matches the
                # close on the EXACT group, bypassing the line-blind
                # ambiguity guard. None for lineless markets (h2h/1x2/btts).
                market_detail=canonical_market_detail(detail),
                bookmaker=v.best_book,
                decimal_odds=v.best_odds,
                model_probability=v.sharp_fair_prob,
                fair_probability=v.implied_prob,
                edge=v.edge,
                ev=v.ev,
                confidence=confidence,
                recommended_stake_fraction=breakdown.final,
                recommended_stake_amount=stake_amount(breakdown.final, deps.bankroll),
                stake_breakdown=ShrinkAnnotatedStakeBreakdownOut(
                    raw_kelly=breakdown.raw_kelly,
                    fractional=breakdown.fractional,
                    capped=breakdown.capped,
                    final=breakdown.final,
                    daily_clipped=False,
                    # Task 5 SHADOW annotation (all None when no n_eff source).
                    phi=shrink_phi,
                    n_eff=shrink_n_eff,
                    shrunk_fraction=shrunk_fraction,
                ),
                # Task 6 anchor-thinness telemetry (log-only; the age half is
                # steam_anchor_age_seconds below).
                anchor_book_count=anchor_book_count,
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
                    + moneyline_note
                    + major_note
                    + sharp_note
                    + experimental_note
                    + ml_note
                    + steam_note
                    + sanity_note
                ),
                tier=tier,
                value_filter_score=ml_score,
                # anchor stratification key for live CLV (PIN/SHARP/CONS)
                anchor_type=anchor_type_for(v.sharp_book),
                # CLV-3: the concrete pick-time anchor BOOK behind anchor_type, so the
                # CLV close can test BOOK independence (a Smarkets-anchored pick vs a
                # Betfair-exchange close is independent though both are 'sharp').
                anchor_book=v.sharp_book,
                # anchor MATCH-CONFIDENCE provenance (observability only): how the
                # sharp anchor above was matched to this fixture. Pinnacle = the
                # injector's hardened-matcher score; a missing map entry stores
                # None/'unscored' — HONEST, never a fabricated 1.0. Inline sharp
                # (Betfair/Smarkets, same canonical event) = 1.0 by construction.
                # Consensus = None/None (no cross-source match happened).
                anchor_match_confidence=anchor_match_confidence,
                anchor_match_method=anchor_match_method,
                # Betfair staleness-guard verdict at mint (observability only).
                anchor_staleness_decision=anchor_staleness_decision,
                # A5: steam SHADOW verdict at mint (observability only — never
                # gates/demotes/reorders; NULLs = not evaluated, never fabricated).
                steam_tripped=steam_tripped,
                steam_reasons=steam_reasons,
                steam_closed_fraction=steam_closed_fraction,
                steam_anchor_age_seconds=steam_anchor_age_seconds,
                # P2-2: whether the anchor devig fell back to multiplicative for this
                # MINT fair — the trusted CLV subset drops asymmetric mint/close fallbacks.
                mint_devig_fell_back=v.sharp_devig_fell_back,
                # H3: the live policy regime that minted this pick (thresholds,
                # devig, sharp-anchor gate, ceiling, enforced ML manifest) so CLV
                # is never attributed across mixed regimes.
                policy_fingerprint=fingerprint,
                created_at=now,
            )
            # Persist-before-reserve (kr-1 ordering) is preserved, but a
            # PREMIUM candidate's persist is DEFERRED to the reserve loop below
            # so the persist + ledger-reserve pair runs back-to-back under a
            # cancellation shield — the watchdog can no longer land between a
            # persisted full-stake row and its reservation, which leaked
            # uncounted daily exposure until restart (2026-07-08 audit). A
            # re-detected 'duplicate' (already in the DB) and an 'unpersisted'
            # pick (DB state unknown) still reserve NOTHING — so a sustained
            # duplicate/unpersisted pick never accumulates standing exposure
            # that would silently exhaust the daily cap (kr-1 /
            # kelly-risk-r2-1) — and an exhausted cap never skips the
            # re-dispatch of an already-persisted pick.
            # Candidate/rejection audit trail (external-audit #3, fill #2): record
            # EVERY evaluated candidate — premium kept (empty reasons) AND every
            # tier demotion — with its tier + demotion reason slug(s) + anchor/fill
            # provenance, so ROI diagnosis can later tune false positives, tier
            # demotions, and fill realism. The slugs derive from the demotion NOTES
            # already computed above (each note is set only when its gate demotes;
            # steam's SHADOW note — a would-demote that leaves the tier premium — is
            # excluded so an empty reasons tuple stays the clean-keep signal). Pure
            # MEASUREMENT: isolated, never gates or alters a pick (see helper).
            audit_reasons: list[str] = []
            if visibility_note:
                audit_reasons.append("visibility_only")
            if moneyline_note:
                audit_reasons.append("odds_ceiling")
            if major_note:
                audit_reasons.append("non_major_league")
            if sharp_note:
                audit_reasons.append("no_sharp_anchor")
            if experimental_note:
                audit_reasons.append("experimental_sport")
            if ml_note:
                audit_reasons.append("ml_filter")
            if steam_note.startswith(" | steam ("):  # DEMOTE form only (not shadow)
                audit_reasons.append("steam")
            if sanity_note:
                audit_reasons.append("structural_sanity")
            await _record_candidate_audit(
                deps, pick, detail or "", tuple(audit_reasons), steam_anchor_age_seconds, now
            )
            if tier == "volume":
                # Shadow tier: persisted INLINE (it never reserves exposure, so
                # the persist->reserve cancellation window does not exist here)
                # + CLV-tracked but NOT alerted and NEVER on the exposure
                # ledger. (Volume alerting was trialed 2026-06-23
                # then reverted: live CLV ~0% (-0.3% over n=21) showed no edge vs
                # premium's +11.9% — premium-only alerts. The build_pick_alert
                # 🔵 VOLUME tag + tier-keyed dedupe stay in place, so re-enabling
                # is a one-line `await deps.dispatcher.dispatch(...)` here.) Its
                # picks ride the same event pages as premium ones, so the CLV
                # revalidation below re-prices them for free — the tier's purpose.
                outcome = await _maybe_persist(deps, pick, event_id)
                if outcome == "inserted":
                    picks.append(pick)
                    n_volume += 1
                continue
            # DEFER the persist + reserve + alert: the daily-exposure cap must
            # fund the highest-growth picks first, not whichever happened to
            # iterate first — and the persist must sit ADJACENT to its reserve
            # (one shielded unit) so a watchdog cancellation between them can
            # never orphan a persisted full-stake row off the ledger.
            premium_candidates.append((pick, breakdown, event_id))

    # Persist + reserve the deferred premium candidates edge-ranked by raw_kelly
    # (DESC) so that when the daily-exposure cap binds the highest-growth picks
    # fund first. The sort is STABLE, so equal-raw_kelly candidates keep their
    # deterministic iteration order. Per-candidate logic is the prior inline path
    # (persist -> reserve -> skip-on-None -> append-on-new-outcome -> dispatch).
    # raw_kelly derives only from already-computed fair/price (no leakage) and the
    # ledger.reserve math / kr-1 ordering inside _reserve_for_outcome are unchanged.
    premium_candidates.sort(key=lambda c: c[1].raw_kelly, reverse=True)
    n_unpersisted_withheld = 0
    for pick, breakdown, event_id in premium_candidates:
        # SHIELDED: a watchdog cancellation mid-pair lets the in-flight persist
        # run on to its ledger reservation (never a persisted full-stake row
        # the caps don't count); the dispatch below stays cancellable — a lost
        # alert re-dispatches next cycle as a 'duplicate'.
        outcome, staked = await asyncio.shield(
            _persist_and_reserve(deps, pick, breakdown, event_id, now)
        )
        if staked is None:
            # brand-new premium pick with no remaining daily/event capacity
            # (or a 'duplicate_denied' cap-denial marker — never re-dispatched)
            logger.info("daily exposure cap reached; skipping %s", pick.selection)
            continue
        pick = staked
        if outcome == "unpersisted" and _persistence_configured(deps):
            # WP2 fail-closed: persistence is configured but THIS pick could not
            # be persisted (DB outage / unresolvable event) — it can never be
            # settled, ledger-seeded, or CLV-tracked, so withhold its alert.
            n_unpersisted_withheld += 1
            continue
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
    if n_unpersisted_withheld:
        logger.warning(
            "withheld %d premium alert(s) for %s: pick persistence failed (fail closed)",
            n_unpersisted_withheld,
            sport_key,
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
        except Exception as exc:  # revalidation must never break picking
            # exc_info=True: the intermittent TypeError here was undiagnosable
            # from the class name alone — log the full traceback so the root
            # cause is pinpointable. Isolated from the off-window call below so
            # one failing revalidation path cannot skip the other.
            logger.error("open-pick revalidation failed: %s", type(exc).__name__, exc_info=True)
        try:
            await revalidate_offwindow_picks(
                deps.loader,
                deps.session_factory,
                sport_key,
                covered_event_ids={s.event_id for s in snapshots},
                devig_method=deps.devig_method,
                value_policy=deps.value_policy,
            )
        except Exception as exc:  # revalidation must never break picking
            logger.error("off-window revalidation failed: %s", type(exc).__name__, exc_info=True)

    n_premium = len(picks) - n_volume
    logger.info(
        "value pipeline cycle for %s: %d premium picks, %d volume (shadow)",
        sport_key,
        n_premium,
        n_volume,
    )
    # Buchalter bet-volume smoke detector (Task 8 probe): one INFO line per
    # cycle — picks minted / events evaluated / eligible markets. Log-only:
    # no persistence, no alerting, no thresholds; a drifting minted-to-
    # eligible ratio is the smoke a future review inspects.
    logger.info(
        "value pipeline %s bet-volume probe: %d pick(s) minted / "
        "%d event(s) evaluated / %d eligible market(s)",
        sport_key,
        len(picks),
        len({key[0] for key in grouped} - started),
        n_eligible_markets,
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
    if n_dc_rejected:
        # The DC implausibility guard is never silent: these double-chance candidates
        # carried a derived fair that disagreed wildly with the soft price (a defective
        # 1X2 anchor) and were rejected before minting any pick.
        logger.info(
            "value pipeline %s: double-chance implausibility guard rejected %d candidate(s)",
            sport_key,
            n_dc_rejected,
        )
    if n_moneyline_capped:
        # The moneyline odds ceiling is never silent: these premium H2H candidates
        # priced above VALUE_MONEYLINE_MAX_ODDS are in the structurally CLV-negative
        # 1X2 longshot band and were CAPPED at the volume (shadow) tier — tracked for
        # forward CLV, never alerted or staked (ADR-0019 H1 self-validation).
        logger.info(
            "value pipeline %s: moneyline odds ceiling capped %d longshot(s) at volume (shadow)",
            sport_key,
            n_moneyline_capped,
        )
    if n_sanity_demoted:
        # FIX 1 — the structural-sanity backstop is never silent: these premium
        # candidates carried a structurally impossible (fair, offered, edge)
        # triple (a phantom edge from an upstream data defect) and were HARD-
        # DEMOTED to the volume (shadow) tier — persisted + CLV-tracked, never
        # alerted, never a silent drop.
        logger.warning(
            "value pipeline %s: structural-sanity net demoted %d impossible-edge "
            "candidate(s) to volume (shadow)",
            sport_key,
            n_sanity_demoted,
        )
    if n_off_band:
        # VALUE_ODDS_BANDS intervention is never silent either: these
        # candidates cleared the edge scan and were rejected on price band.
        logger.info(
            "value pipeline %s: %d candidate(s) outside VALUE_ODDS_BANDS",
            sport_key,
            n_off_band,
        )
    if n_sanity_dropped:
        logger.info(
            "value pipeline %s: %d candidate(s) dropped above the %.0f sanity odds ceiling",
            sport_key,
            n_sanity_dropped,
            _SANITY_MAX_ODDS,
        )
    if n_non_settleable:
        logger.info(
            "value pipeline %s: %d ungradeable market group(s) dropped "
            "(period/corner/card — no results-feed score, would only ever void)",
            sport_key,
            n_non_settleable,
        )
    if n_tennis_game_line:
        logger.info(
            "value pipeline %s: %d tennis game-line market group(s) dropped "
            "(results feed carries set scores only — a game-line pick can never "
            "auto-settle honestly)",
            sport_key,
            n_tennis_game_line,
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
    # STALE-DROP RATIO: fraction of this cycle's mintable universe (candidates
    # that reached the freshness gate) lost SOLELY to staleness. The count above
    # is invisible without a denominator — 50 stale of 50 is a starving slate; 50
    # of 5000 is noise. Surfaced on LAST_POLL so the self-audit/alert layer (added
    # on main) can fire on starvation; a per-cycle WARNING makes a slow cycle loud
    # here too. NOT wired to the dispatcher here (out of this module's scope).
    stale_drop_ratio = n_stale / n_freshness_candidates if n_freshness_candidates else 0.0
    if n_freshness_candidates and stale_drop_ratio > deps.stale_drop_ratio_warn:
        logger.warning(
            "value pipeline %s: cycle too slow for freshness window — picks starving "
            "(stale-drop ratio %.0f%% = %d/%d mintable candidates dropped for staleness "
            ">%.0fs old; threshold %.0f%%) — trim markets/leagues or raise concurrency",
            sport_key,
            stale_drop_ratio * 100.0,
            n_stale,
            n_freshness_candidates,
            deps.gate_policy.max_odds_age_seconds,
            deps.stale_drop_ratio_warn * 100.0,
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
        stale_drop_ratio=stale_drop_ratio,
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


def drop_post_kickoff_snapshots(
    snapshots: Sequence[OddsSnapshotIn],
    kickoffs: Mapping[str, datetime | None],
) -> list[OddsSnapshotIn]:
    """Drop every snapshot captured AT OR AFTER its event's kickoff — an in-play
    price must never mint a pre-match value pick nor stamp a CLV close.

    A snapshot whose event has an UNKNOWN kickoff (absent from ``kickoffs`` or
    mapped to None) is KEPT: a NULL kickoff cannot be proven post-KO (the same
    rule the started-set and the off-window close guards use). Strictly-pre-
    kickoff snapshots pass through unchanged, so the surviving 'close' for an
    event is its last snapshot strictly before kickoff, never an in-play one."""
    kept: list[OddsSnapshotIn] = []
    for snap in snapshots:
        kickoff = kickoffs.get(snap.event_id)
        if kickoff is not None and snap.captured_at >= kickoff:
            continue
        kept.append(snap)
    return kept


def started_event_ids(
    event_ids: Iterable[str],
    kickoffs: Mapping[str, datetime | None],
    now: datetime,
) -> set[str]:
    """Events whose kickoff is KNOWN and at/before ``now`` — the in-play set the
    candidate loop skips wholesale. ``kickoffs`` should already PREFER the
    persisted ``events.starts_at`` over the ephemeral in-memory directory so a
    late-arriving or absent in-memory kickoff (tennis start times land late)
    still excludes a started event. A NULL/absent kickoff is never 'started' — it
    cannot be proven, so re-pricing continues (same rule as the CLV guards)."""
    return {
        event_id
        for event_id in event_ids
        if (kickoff := kickoffs.get(event_id)) is not None and kickoff <= now
    }


LiquidityByMarket = dict[tuple[str, Market, str | None], dict[str, dict[str, float]]]


def group_market_liquidity(snapshots: Sequence[OddsSnapshotIn]) -> LiquidityByMarket:
    """KNOWN matched liquidity per market, keyed like ``group_market_prices``:
    {(event_id, market, market_detail): {selection: {book: liquidity}}}.

    Only snapshots with a KNOWN (non-None) ``liquidity`` (£ best-back size —
    today the dedicated Betfair capture) contribute; the dominant main-scrape
    rows carry liquidity=None and are deliberately ABSENT here, which keeps
    them anchor-eligible under the WP5 exchange floor (known-thin is rejected,
    unknown stays as before — see app/edge/value._named_sharp_anchor)."""
    out: LiquidityByMarket = {}
    for snap in snapshots:
        if snap.liquidity is None:
            continue
        key = (snap.event_id, snap.market, snap.market_detail)
        out.setdefault(key, {}).setdefault(snap.selection, {})[snap.bookmaker] = snap.liquidity
    return out


# Markets whose outcomes are mutually exclusive and exhaustive — direct
# anchor devig of one book is sound. Loader config guarantees SPREADS groups
# are half-line AH (no pushes) or 3-way European handicap. Double chance is
# NOT direct (overlapping legs, quotes sum ~200%) — derived from 1X2.
# Market-agnostic absolute price ceiling. Above this, a value pick's best price is
# a data artefact (mis-mapped/garbage longshot), not a real edge — the per-market
# AH/DC guards miss spreads/handicaps, which surfaced picks at 100-151.0 with
# impossible EV. Generous (real value picks sit near even money) so it only clips
# garbage, never legitimate edges.
_SANITY_MAX_ODDS = 50.0
_DIRECT_MARKETS = frozenset({Market.H2H, Market.TOTALS, Market.BTTS, Market.DNB, Market.SPREADS})

EventFairProbs = dict[tuple[str, Market, str | None], tuple[str, dict[str, float]]]

# Shared frozen no-op policy for the default-OFF path (ruff B008: no call in a
# function default). ValuePolicy is immutable, so one instance is safe to share.
_EMPTY_VALUE_POLICY = ValuePolicy()


def event_fair_probs(
    grouped: GroupedMarkets,
    devig_method: DevigMethod,
    value_policy: ValuePolicy = _EMPTY_VALUE_POLICY,
    *,
    fell_back_out: dict[tuple[str, Market, str | None], bool] | None = None,
    liquidity_by_market: LiquidityByMarket | None = None,
    exchange_demoted_events: AbstractSet[str] | None = None,
) -> EventFairProbs:
    """Trustworthy (anchor_book, selection->fair) per (event, market, line).

    Shared by the live value pipeline and the CLV true-up so picks and their
    closing-line values are priced by the SAME rules. ``value_policy`` carries
    the optional per-market devig override (``devig_by_market``) and the
    consensus logit-pool flag (``consensus_logit_pool``); the default empty
    policy reproduces the global-method, median-consensus behavior exactly.
    Both knobs flow through this single chokepoint so the pick pipeline and the
    CLV true-up always price fill and close with the identical method.

    ``liquidity_by_market`` (``group_market_liquidity``) carries KNOWN matched
    exchange liquidity so ``value_policy.exchange_min_liquidity`` can reject a
    KNOWN-thin exchange anchor (WP5); None (the default, and every market
    absent from the map) leaves anchor selection unchanged — the unknown-
    liquidity main-scrape rows stay eligible.

    When ``fell_back_out`` is provided it is POPULATED (additively, by the same
    keys as the return) with the P2-2 devig-fallback flag per market — True when
    the anchor devig fell back to multiplicative. The return value is unchanged
    (callers that ignore provenance pass nothing).

    ``exchange_demoted_events`` (Betfair staleness guard, P3) carries the event
    refs whose FRESH persisted API verdict is 'demote': for those events' H2H
    markets ONLY (v1 scope — the only market the API capture covers) the
    exchange anchor is skipped inside ``_named_sharp_anchor`` (fail-closed to
    the next sharp book / consensus). None / empty (the default, and always the
    close/true-up path) leaves anchor selection bit-identical."""
    from app.edge.value import anchor_fair_probs_with_provenance, double_chance_fair

    out: EventFairProbs = {}
    h2h_3way: dict[str, tuple[tuple[str, dict[str, float]], list[str], bool]] = {}
    for (event_id, market, detail), (prices, _) in grouped.items():
        if market in _DIRECT_MARKETS:
            result = anchor_fair_probs_with_provenance(
                prices,
                devig_method=devig_method_for(value_policy, str(market), detail, devig_method),
                consensus_logit_pool=value_policy.consensus_logit_pool,
                liquidity=(
                    liquidity_by_market.get((event_id, market, detail))
                    if liquidity_by_market is not None
                    else None
                ),
                exchange_min_liquidity=value_policy.exchange_min_liquidity,
                exchange_demoted=(
                    market is Market.H2H
                    and exchange_demoted_events is not None
                    and event_id in exchange_demoted_events
                ),
            )
            if result is not None:
                book, fair, fell_back = result
                out[(event_id, market, detail)] = (book, fair)
                if fell_back_out is not None:
                    fell_back_out[(event_id, market, detail)] = fell_back
                if market is Market.H2H and len(prices) == 3:
                    h2h_3way[event_id] = ((book, fair), list(prices.keys()), fell_back)
    for (event_id, market, detail), _group in grouped.items():
        if market is Market.DOUBLE_CHANCE and event_id in h2h_3way:
            anchored, selections, fell_back = h2h_3way[event_id]
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
                # DC inherits the 1X2 anchor's fallback (derived from the same devig).
                if fell_back_out is not None:
                    fell_back_out[(event_id, market, detail)] = fell_back
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
        probs, fallback = devig_with_diagnostics([leg.decimal_odds for leg in legs], method=method)
        if fallback is not None:
            # The pure devig module returns the fallback condition as DATA
            # (no logging inside app/probabilities/ — audit 2026-07-09); THIS
            # io layer decides: documented-expected fallbacks follow the debug
            # doctrine (e.g. Shin on underround books), the rest are anomalous.
            log = logger.debug if fallback in EXPECTED_FALLBACKS else logger.warning
            log(
                "%s devig fell back to multiplicative (%s) for %s %s/%s",
                method,
                fallback,
                event_id,
                bookmaker,
                market,
            )
        for leg, p in zip(legs, probs, strict=True):
            fair[(event_id, bookmaker, market, leg.selection)] = p
    return fair
