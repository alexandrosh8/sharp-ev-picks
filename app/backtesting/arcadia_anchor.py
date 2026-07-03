"""ARCADIA Pinnacle anchor dataset for the pre-registered H2 validation run.

PURE module (stdlib + app.resolution only — no env/DB/HTTP/log side effects):
the DB export lives in ``scripts/arcadia_anchor_export.py``; this module holds
the FROZEN protocol constants (mirroring the ADR-0019 2026-07-03 amendment),
the row schema, the fail-closed validation matcher, the preflight coverage
report, and the contamination guards.

VALIDATION/BACKTEST ONLY. Nothing in live pick minting (app/pipeline.py,
app/edge/) may import this module — tests enforce that. This system places no
bets; the dataset informs an offline CLV validation, never an execution path.

Honesty rules baked in:
- rejected rows are EMITTED with a reason, never silently dropped;
- provenance (snapshot ids, capture times, match method/confidence) rides
  every row;
- a missing/stale/ambiguous anchor is a REJECTION, never a fallback price;
- the ARCADIA close is SAME-SOURCE relative to the ARCADIA anchor: it is
  exported for secondary reporting only and must NEVER substitute the
  independent (Betfair BSP) close in the headline CLV.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.resolution import AliasTable
from app.resolution.matching import (
    EventCandidate,
    distinguishing_markers,
    match_event_hardened_scored,
    normalize_name,
)

EXPORT_SCHEMA_VERSION = 1
PARSER_METHOD = "arcadia-json-feed/odds_snapshots-v1"
ANCHOR_SOURCE = "pinnacle_arcadia"

# --------------------------------------------------------------------------- #
# FROZEN protocol constants — ADR-0019 amendment 2026-07-03. Changing any of
# these after the amendment is signed voids the pre-registration.
# --------------------------------------------------------------------------- #
ELIGIBLE_WINDOW_START = date(2026, 7, 1)
ELIGIBLE_WINDOW_END = date(2026, 12, 31)
ELIGIBLE_SPORTS: tuple[str, ...] = ("soccer",)
# Namespaces the exporter reads (anchor side) — the isolated Pinnacle capture.
ELIGIBLE_SOURCE_SPORT_KEYS: tuple[str, ...] = ("pinnacle_soccer",)
# Market map: odds_snapshots market key -> (market_type, period, line).
# ONLY these are eligible; anything else is a rejected row (unsupported_market).
ELIGIBLE_MARKETS: dict[str, tuple[str, str, float | None]] = {
    "h2h": ("1x2", "match", None),
    "over_under_2_5": ("ou25", "match", 2.5),
}
OUTCOMES_REQUIRED: dict[str, int] = {"1x2": 3, "ou25": 2}
# Anchor = LAST complete snapshot set captured at or before KO - 3600s, no
# older than 24h before kickoff. Close = LAST complete set inside the final
# hour. freshness_seconds is always (kickoff - captured_at).
ANCHOR_MIN_PRE_KO_SECONDS = 3600
ANCHOR_MAX_PRE_KO_SECONDS = 86400
CLOSE_WINDOW_SECONDS = 3600
# Tautological-close guard: the (secondary, same-source) ARCADIA close must be
# a genuinely distinct observation from the anchor — minimum separation.
MIN_ANCHOR_CLOSE_GAP_SECONDS = 1800
# Validation-join kickoff window (STRICTER than the live matcher's 360-min
# accept drift — frozen for the H2 run).
VALIDATION_KICKOFF_WINDOW_MINUTES = 60
# Preflight bars (frozen).
PREFLIGHT_MIN_USABLE_EVENTS_PER_MONTH = 300
PREFLIGHT_MAX_MISSING_ANCHOR_RATE = 0.50
PREFLIGHT_MAX_STALE_ANCHOR_RATE = 0.20
ACCEPTANCE_MIN_N_PER_MARKET = 150  # ADR-0019 acceptance bar (unchanged)
# Conservative bet-rate multiplier for the reachability ESTIMATE only (the
# 2025 train side bet ~20-40% of anchored rows at threshold 0; we freeze a
# deliberately pessimistic 5% so "reachable" is never optimistic).
EXPECTED_BET_RATE_ESTIMATE = 0.05

# Config freeze: sha256 over the ADR-0019 frozen live values, computed with the
# runbook's exact recipe on 2026-07-03 (value_devig=power,
# value_moneyline_max_odds=5.0, value_min_edge=0.03,
# value_volume_min_edge=0.015, fractional_kelly=0.25).
FROZEN_CONFIG_SHA256 = "6abe1a319fc4abfc3df0dbff8dfaf7aecce6b3c94eaae993b14efaa1fbceb20c"

# SPENT input data — any of these sha256s as a validation input is a hard STOP
# (ADR-0019: the 2024-07..2025-12 and 2026-Jan..Jun slates are spent for
# selection AND evaluation). Hashes measured 2026-07-03 from the on-disk tars
# + the 2026-07-02 single-shot header doc.
SPENT_DATA_SHA256S: frozenset[str] = frozenset(
    {
        # data (1).tar — 2025 train side (single-shot 2026-07-02)
        "7315dcbf1ccdabe02899f490976ebd3e38dea9669c0396eb83cc658b5ed2bc24",
        # data (2).tar — 2026-Jan..Jun holdout (single-shot 2026-07-02);
        # also reachable as the incoming/data2026.tar SYMLINK
        "9123d3203f79e33ac09e2a6a2f8d91ccff8d684ac74e0a02cbde9002440ed330",
        # data.tar — original 2024-07..2025-12 slate (BSP sharp-CLV runs)
        "02dfcbfd62c733da343f9cdbc77e782253be7a8bcf8cdb5c170bc519b00ce50f",
        # combined_train2025_holdout2026.tar — the single-shot's built input
        "dbcc3000dbf6dabeef9ea7e3b400500c50a38f1a307c93a8e5425e4c35ea258e",
    }
)

# Rejection reasons (closed vocabulary — preflight aggregates on these).
R_UNSUPPORTED_MARKET = "unsupported_market"
R_INCOMPLETE_OUTCOME_SET = "incomplete_outcome_set"
R_SELECTION_UNRESOLVED = "selection_unresolved"
R_WINDOW_INELIGIBLE = "window_ineligible"
R_SPORT_INELIGIBLE = "sport_ineligible"
R_ANCHOR_STALE = "anchor_stale"
R_ANCHOR_SUPERSEDED = "anchor_superseded"
R_ANCHOR_MISSING = "anchor_missing"
R_CLOSE_TAUTOLOGICAL = "close_tautological"
R_EVENT_UNMATCHED = "event_unmatched"
R_EVENT_AMBIGUOUS = "event_ambiguous"
R_KICKOFF_DRIFT = "kickoff_out_of_window"
R_MARKET_MISMATCH = "market_mismatch"
R_LINE_MISMATCH = "line_mismatch"
R_SELECTION_MISMATCH = "selection_mismatch"


@dataclass(frozen=True)
class SnapshotObs:
    """One odds_snapshots row (already read from the DB) — exporter input."""

    snapshot_id: int
    event_id: int
    sport_key: str  # e.g. "pinnacle_soccer"
    league: str
    home: str
    away: str
    starts_at: datetime  # UTC-aware
    market: str  # raw odds_snapshots key, e.g. "h2h" / "over_under_2_5"
    selection: str  # raw selection (team display name / "Draw" / "Over"...)
    decimal_odds: Decimal
    captured_at: datetime  # UTC-aware


@dataclass
class AnchorRow:
    """One exported dataset row — the auditable unit. usable=False rows are
    EMITTED with rejection_reason, never dropped."""

    source: str
    source_event_id: int
    source_market_id: str  # "<event_id>:<market_key>" (no separate market table)
    canonical_event_id: int | None
    sport: str
    league: str
    home: str
    away: str
    event_start_time_utc: str  # ISO-8601 Z
    market_type: str  # "1x2" / "ou25" / raw key when unsupported
    period: str
    line: float | None
    selection: str  # canonical side: home/draw/away/over/under (raw if unresolved)
    price: str  # Decimal as string (NUMERIC discipline at the boundary)
    captured_at: str  # ISO-8601 Z
    freshness_seconds: int | None
    raw_snapshot_id: int
    parser_method: str
    match_confidence: float | None
    match_method: str | None
    usable: bool
    rejection_reason: str | None
    role: str = "anchor"  # "anchor" | "close_secondary"


EXPORT_COLUMNS: tuple[str, ...] = tuple(AnchorRow.__dataclass_fields__)


def classify_market(market_key: str) -> tuple[str, str, float | None] | None:
    """(market_type, period, line) for an eligible odds_snapshots key, else None."""
    return ELIGIBLE_MARKETS.get(market_key)


def resolve_selection_side(
    selection: str, home: str, away: str, market_type: str, aliases: AliasTable
) -> str | None:
    """Map a raw snapshot selection onto the canonical side, or None.

    1x2: home/draw/away by alias-canonical name equality (NEVER fuzzy — a
    selection that matches neither side is unresolved, not guessed).
    ou25: over/under by case-insensitive prefix.
    """
    sel = selection.strip()
    if market_type == "ou25":
        low = sel.casefold()
        if low.startswith("over"):
            return "over"
        if low.startswith("under"):
            return "under"
        return None
    if market_type == "1x2":
        if sel.casefold() == "draw":
            return "draw"
        canon = aliases.canonical(sel)
        if canon and canon == aliases.canonical(home):
            return "home"
        if canon and canon == aliases.canonical(away):
            return "away"
        return None
    return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _row(
    obs: SnapshotObs,
    *,
    market_type: str,
    period: str,
    line: float | None,
    selection: str,
    usable: bool,
    reason: str | None,
    role: str = "anchor",
    canonical_event_id: int | None = None,
    match_confidence: float | None = None,
    match_method: str | None = None,
) -> AnchorRow:
    freshness = int((obs.starts_at - obs.captured_at).total_seconds())
    return AnchorRow(
        source=ANCHOR_SOURCE,
        source_event_id=obs.event_id,
        source_market_id=f"{obs.event_id}:{obs.market}",
        canonical_event_id=canonical_event_id,
        sport=obs.sport_key.removeprefix("pinnacle_"),
        league=obs.league,
        home=obs.home,
        away=obs.away,
        event_start_time_utc=_iso(obs.starts_at),
        market_type=market_type,
        period=period,
        line=line,
        selection=selection,
        price=str(obs.decimal_odds),
        captured_at=_iso(obs.captured_at),
        freshness_seconds=freshness,
        raw_snapshot_id=obs.snapshot_id,
        parser_method=PARSER_METHOD,
        match_confidence=match_confidence,
        match_method=match_method,
        usable=usable,
        rejection_reason=reason,
        role=role,
    )


def build_anchor_rows(
    observations: Sequence[SnapshotObs],
    *,
    aliases: AliasTable,
    canonical_matches: Mapping[int, tuple[int, float, str]] | None = None,
) -> list[AnchorRow]:
    """Turn raw snapshot observations into the auditable anchor dataset.

    Per (event, market): pick the LAST complete outcome set inside the frozen
    anchor window as the ANCHOR, and the LAST complete set inside the final
    hour as the SECONDARY same-source close. Everything else is emitted as a
    rejected row with a closed-vocabulary reason. ``canonical_matches`` maps
    source event_id -> (canonical oddsportal-side event id, confidence,
    method) when the export step ran the hardened matcher; observability only.
    """
    rows: list[AnchorRow] = []
    matches = canonical_matches or {}

    by_event_market: dict[tuple[int, str], list[SnapshotObs]] = {}
    for obs in observations:
        by_event_market.setdefault((obs.event_id, obs.market), []).append(obs)

    for (event_id, market_key), group in sorted(by_event_market.items()):
        first = group[0]
        cm = matches.get(event_id)
        canon_kwargs: dict = {
            "canonical_event_id": cm[0] if cm else None,
            "match_confidence": cm[1] if cm else None,
            "match_method": cm[2] if cm else None,
        }

        def emit_all(market_type: str, period: str, line: float | None, reason: str) -> None:
            for obs in group:  # noqa: B023 — group is loop-local and used eagerly
                rows.append(
                    _row(
                        obs,
                        market_type=market_type,
                        period=period,
                        line=line,
                        selection=obs.selection,
                        usable=False,
                        reason=reason,
                        **canon_kwargs,  # noqa: B023
                    )
                )

        classified = classify_market(market_key)
        if classified is None:
            emit_all(market_key, "match", None, R_UNSUPPORTED_MARKET)
            continue
        market_type, period, line = classified

        if first.sport_key not in ELIGIBLE_SOURCE_SPORT_KEYS:
            emit_all(market_type, period, line, R_SPORT_INELIGIBLE)
            continue

        ko_date = first.starts_at.astimezone(UTC).date()
        if not (ELIGIBLE_WINDOW_START <= ko_date <= ELIGIBLE_WINDOW_END):
            emit_all(market_type, period, line, R_WINDOW_INELIGIBLE)
            continue

        # Resolve selections; group complete sets by captured_at.
        need = OUTCOMES_REQUIRED[market_type]
        by_capture: dict[datetime, dict[str, SnapshotObs]] = {}
        for obs in group:
            side = resolve_selection_side(obs.selection, obs.home, obs.away, market_type, aliases)
            if side is None:
                rows.append(
                    _row(
                        obs,
                        market_type=market_type,
                        period=period,
                        line=line,
                        selection=obs.selection,
                        usable=False,
                        reason=R_SELECTION_UNRESOLVED,
                        **canon_kwargs,
                    )
                )
                continue
            by_capture.setdefault(obs.captured_at, {})[side] = obs

        complete = {ts: sides for ts, sides in by_capture.items() if len(sides) == need}
        for _ts, sides in sorted(by_capture.items()):
            if len(sides) == need:
                continue
            for side, obs in sorted(sides.items()):
                rows.append(
                    _row(
                        obs,
                        market_type=market_type,
                        period=period,
                        line=line,
                        selection=side,
                        usable=False,
                        reason=R_INCOMPLETE_OUTCOME_SET,
                        **canon_kwargs,
                    )
                )

        ko = first.starts_at
        anchor_ts: datetime | None = None
        close_ts: datetime | None = None
        for ts in sorted(complete):
            pre_ko = (ko - ts).total_seconds()
            if ANCHOR_MIN_PRE_KO_SECONDS <= pre_ko <= ANCHOR_MAX_PRE_KO_SECONDS:
                anchor_ts = ts  # last qualifying wins (ascending scan)
            elif 0 <= pre_ko < CLOSE_WINDOW_SECONDS:
                close_ts = ts

        for ts in sorted(complete):
            sides = complete[ts]
            if ts == anchor_ts:
                usable, reason, role = True, None, "anchor"
            elif ts == close_ts:
                role = "close_secondary"
                tautological = (
                    anchor_ts is not None
                    and (ts - anchor_ts).total_seconds() < MIN_ANCHOR_CLOSE_GAP_SECONDS
                )
                usable = not tautological
                reason = R_CLOSE_TAUTOLOGICAL if tautological else None
            else:
                pre_ko = (ko - ts).total_seconds()
                usable, role = False, "anchor"
                reason = (
                    R_ANCHOR_STALE if pre_ko > ANCHOR_MAX_PRE_KO_SECONDS else R_ANCHOR_SUPERSEDED
                )
            for side, obs in sorted(sides.items()):
                rows.append(
                    _row(
                        obs,
                        market_type=market_type,
                        period=period,
                        line=line,
                        selection=side,
                        usable=usable,
                        reason=reason,
                        role=role,
                        **canon_kwargs,
                    )
                )
    return rows


# --------------------------------------------------------------------------- #
# Fail-closed validation matcher (anchor rows -> backtest fixture)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ValidationFixture:
    """The fixture the validation run wants an anchor for (e.g. a BSP-joined
    football-data row)."""

    sport: str
    league: str  # free text; country-token contradiction is the veto
    home: str
    away: str
    kickoff: datetime  # UTC-aware
    market_type: str
    period: str
    line: float | None
    selection: str  # home/draw/away/over/under


def _country_token(league: str) -> str | None:
    """The 'Country - Competition' prefix if present, normalized; else None."""
    if " - " in league:
        return normalize_name(league.split(" - ", 1)[0]) or None
    return None


def match_anchor_to_fixture(
    fixture: ValidationFixture,
    anchor_rows: Sequence[AnchorRow],
    *,
    aliases: AliasTable,
) -> tuple[AnchorRow | None, str | None]:
    """Fail-closed: the ONE usable anchor row for the fixture, or (None, reason).

    Hard contradictions (any one rejects): sport, market_type, period, line,
    selection, kickoff outside the frozen 60-min window, one-sided
    women/youth/reserve marker, home/away orientation (flips are NEVER
    allowed), country-token league contradiction, ambiguity (two distinct
    source events both acceptable), stale/rejected anchor rows (only
    usable=True anchor-role rows are ever considered).
    """
    pool = [r for r in anchor_rows if r.usable and r.role == "anchor" and r.sport == fixture.sport]
    candidates = [
        r
        for r in pool
        if r.market_type == fixture.market_type
        and r.period == fixture.period
        and r.line == fixture.line
        and r.selection == fixture.selection
    ]
    if not candidates:
        if any(r.market_type != fixture.market_type for r in pool):
            return None, R_MARKET_MISMATCH
        if any(r.market_type == fixture.market_type and r.line != fixture.line for r in pool):
            return None, R_LINE_MISMATCH
        if any(
            r.market_type == fixture.market_type
            and r.line == fixture.line
            and r.selection != fixture.selection
            for r in pool
        ):
            return None, R_SELECTION_MISMATCH
        return None, R_ANCHOR_MISSING

    accepted: dict[int, AnchorRow] = {}
    any_in_window = False
    for row in candidates:
        row_ko = datetime.fromisoformat(row.event_start_time_utc.replace("Z", "+00:00"))
        drift_min = abs((row_ko - fixture.kickoff).total_seconds()) / 60.0
        if drift_min > VALIDATION_KICKOFF_WINDOW_MINUTES:
            continue
        any_in_window = True
        # Marker veto (one-sided women/youth/reserve = different fixture).
        if distinguishing_markers(row.home) != distinguishing_markers(
            fixture.home
        ) or distinguishing_markers(row.away) != distinguishing_markers(fixture.away):
            continue
        # League country-token contradiction (skip when either side lacks one).
        rc, fc = _country_token(row.league), _country_token(fixture.league)
        if rc is not None and fc is not None and rc != fc:
            continue
        # Participant + orientation via the hardened matcher, ORIENTATION
        # LOCKED (allow_orientation_flip=False) and the frozen tight window.
        outcome = match_event_hardened_scored(
            fixture.home,
            fixture.away,
            fixture.kickoff,
            [
                EventCandidate(
                    ref=str(row.source_event_id),
                    home=row.home,
                    away=row.away,
                    kickoff=row_ko,
                )
            ],
            aliases=aliases,
            ordered=True,
            allow_orientation_flip=False,
            max_minute_drift=VALIDATION_KICKOFF_WINDOW_MINUTES,
            max_accept_minute_drift=VALIDATION_KICKOFF_WINDOW_MINUTES,
        )
        if outcome is not None:
            accepted[row.source_event_id] = row
    if not accepted:
        return None, (R_EVENT_UNMATCHED if any_in_window else R_KICKOFF_DRIFT)
    if len(accepted) > 1:
        return None, R_EVENT_AMBIGUOUS
    return next(iter(accepted.values())), None


def attach_arcadia_anchor(
    fixtures_rows: Sequence[dict],
    anchor_rows: Sequence[AnchorRow],
    *,
    aliases: AliasTable,
    fixture_builder: Callable[[dict, str], ValidationFixture | None],
    column_map: Mapping[str, str],
) -> tuple[int, Counter[str]]:
    """VALIDATION-ONLY join: write anchor prices into backtest rows in place.

    ``fixture_builder(row, selection)`` adapts one backtest row + canonical
    selection; ``column_map`` maps selection -> destination column (e.g.
    {"home": "PSH", "draw": "PSD", "away": "PSA"}). A row gets anchor columns
    ONLY when EVERY selection resolves through the fail-closed matcher to the
    SAME source event; otherwise all destination columns are removed (a
    missing anchor drops the row from the bet universe downstream — no fake
    price, no fake CLV). Returns (n_attached, rejection_counter).
    """
    flat = list(anchor_rows)
    attached = 0
    reasons: Counter[str] = Counter()
    for row in fixtures_rows:
        picked: dict[str, AnchorRow] = {}
        failure: str | None = None
        source_events: set[int] = set()
        for selection in column_map:
            fixture = fixture_builder(row, selection)
            if fixture is None:
                failure = R_ANCHOR_MISSING
                break
            match, reason = match_anchor_to_fixture(fixture, flat, aliases=aliases)
            if match is None:
                failure = reason or R_ANCHOR_MISSING
                break
            picked[selection] = match
            source_events.add(match.source_event_id)
        if failure is None and len(source_events) != 1:
            failure = R_EVENT_AMBIGUOUS
        if failure is not None:
            for col in column_map.values():
                row.pop(col, None)
            reasons[failure] += 1
            continue
        for selection, col in column_map.items():
            row[col] = float(Decimal(picked[selection].price))
        row["_arcadia_anchor_event_id"] = next(iter(source_events))
        row["_arcadia_anchor_method"] = next(iter(picked.values())).match_method or "hardened"
        attached += 1
    return attached, reasons


# --------------------------------------------------------------------------- #
# Preflight coverage report + DO-NOT-RUN
# --------------------------------------------------------------------------- #
def preflight_report(rows: Sequence[AnchorRow]) -> dict:
    """The mandatory pre-run coverage report. verdict is 'PASS' or 'DO-NOT-RUN'
    with explicit reasons — a failed preflight must stop any validation run."""
    usable = [r for r in rows if r.usable and r.role == "anchor"]
    rejected = [r for r in rows if not r.usable]
    anchor_market_pairs = {(r.source_event_id, r.source_market_id) for r in rows}
    usable_pairs = {(r.source_event_id, r.source_market_id) for r in usable}

    def month(r: AnchorRow) -> str:
        return r.event_start_time_utc[:7]

    events_by_month: dict[str, set[int]] = {}
    markets_by_month: dict[str, Counter[str]] = {}
    for r in usable:
        events_by_month.setdefault(month(r), set()).add(r.source_event_id)
        markets_by_month.setdefault(month(r), Counter())[r.market_type] += 1
    rejection_reasons = Counter(r.rejection_reason for r in rejected)
    stale = rejection_reasons.get(R_ANCHOR_STALE, 0)
    missing_markets = max(len(anchor_market_pairs) - len(usable_pairs), 0)
    missing_rate = missing_markets / len(anchor_market_pairs) if anchor_market_pairs else 1.0
    stale_rate = stale / len(rows) if rows else 1.0
    confidences = [r.match_confidence for r in usable if r.match_confidence is not None]
    conf_dist = {
        "n": len(confidences),
        "min": min(confidences) if confidences else None,
        "median": (sorted(confidences)[len(confidences) // 2] if confidences else None),
        "max": max(confidences) if confidences else None,
    }
    usable_rows_per_market: Counter[str] = Counter(r.market_type for r in usable)
    expected_n = {
        mkt: int((count / OUTCOMES_REQUIRED.get(mkt, 1)) * EXPECTED_BET_RATE_ESTIMATE)
        for mkt, count in usable_rows_per_market.items()
    }
    bar_reachable = {mkt: n >= ACCEPTANCE_MIN_N_PER_MARKET for mkt, n in expected_n.items()}

    failures: list[str] = []
    if not usable:
        failures.append("no usable anchor rows")
    for m, events in sorted(events_by_month.items()):
        if len(events) < PREFLIGHT_MIN_USABLE_EVENTS_PER_MONTH:
            failures.append(
                f"month {m}: {len(events)} usable events < {PREFLIGHT_MIN_USABLE_EVENTS_PER_MONTH}"
            )
    if missing_rate > PREFLIGHT_MAX_MISSING_ANCHOR_RATE:
        failures.append(
            f"missing-anchor rate {missing_rate:.1%} > {PREFLIGHT_MAX_MISSING_ANCHOR_RATE:.0%}"
        )
    if stale_rate > PREFLIGHT_MAX_STALE_ANCHOR_RATE:
        failures.append(
            f"stale-anchor rate {stale_rate:.1%} > {PREFLIGHT_MAX_STALE_ANCHOR_RATE:.0%}"
        )
    if not usable or not any(bar_reachable.values()):
        failures.append(
            f"expected sample size {dict(expected_n)} — the n>={ACCEPTANCE_MIN_N_PER_MARKET} "
            "acceptance bar is not reachable in any market"
        )
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "event_coverage_by_month": {m: len(v) for m, v in sorted(events_by_month.items())},
        "market_coverage_by_month": {m: dict(c) for m, c in sorted(markets_by_month.items())},
        "usable_anchor_rows": len(usable),
        "rejected_rows": len(rejected),
        "rejection_reasons": {str(k): v for k, v in rejection_reasons.most_common()},
        "missing_anchor_rate": round(missing_rate, 4),
        "stale_anchor_rate": round(stale_rate, 4),
        "match_confidence_distribution": conf_dist,
        "expected_sample_size": expected_n,
        "acceptance_bar_reachable": bar_reachable,
        "verdict": "DO-NOT-RUN" if failures else "PASS",
        "failures": failures,
    }


# --------------------------------------------------------------------------- #
# Contamination guards
# --------------------------------------------------------------------------- #
def evaluate_contamination_guards(
    *,
    dataset_path: Path | None,
    dataset_sha256: str | None = None,
    input_sha256s: Iterable[str] = (),
    window_start: date | None = None,
    window_end: date | None = None,
    config_sha256: str | None = None,
    output_dir: Path | None = None,
    preflight: Mapping[str, object] | None = None,
    preflight_dataset_sha256: str | None = None,
    anchor_source: str = ANCHOR_SOURCE,
) -> list[str]:
    """PURE guard evaluation — returns violation strings; empty = clear to
    proceed. Every violation is a hard STOP for the validation workflow."""
    violations: list[str] = []
    if dataset_path is None or not dataset_path.is_file():
        violations.append(f"anchor dataset missing: {dataset_path}")
    for sha in input_sha256s:
        if sha in SPENT_DATA_SHA256S:
            violations.append(f"SPENT input data (sha256={sha[:16]}...) — slate already used")
    if window_start is not None and window_start < ELIGIBLE_WINDOW_START:
        violations.append(f"window start {window_start} before eligible {ELIGIBLE_WINDOW_START}")
    if window_end is not None and window_end > ELIGIBLE_WINDOW_END:
        violations.append(f"window end {window_end} after eligible {ELIGIBLE_WINDOW_END}")
    if config_sha256 is not None and config_sha256 != FROZEN_CONFIG_SHA256:
        violations.append(
            "config hash mismatch — live Settings drifted from the ADR-0019 frozen values"
        )
    if output_dir is not None and not output_dir.is_dir():
        violations.append(f"output directory missing: {output_dir}")
    if preflight is None:
        violations.append("no preflight report — run the preflight first")
    else:
        if preflight.get("verdict") != "PASS":
            violations.append("preflight verdict is not PASS")
        if (
            preflight_dataset_sha256 is not None
            and dataset_sha256 is not None
            and preflight_dataset_sha256 != dataset_sha256
        ):
            violations.append("preflight was run on a DIFFERENT dataset (sha256 mismatch)")
    if anchor_source != ANCHOR_SOURCE:
        violations.append(
            f"anchor source {anchor_source!r} is not approved in the pre-registration"
        )
    return violations


# --------------------------------------------------------------------------- #
# Dataset IO (append-only discipline: writers refuse to overwrite)
# --------------------------------------------------------------------------- #
def write_dataset(path: Path, rows: Sequence[AnchorRow]) -> str:
    """Write the dataset CSV (refusing to overwrite) and return its sha256."""
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing dataset: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_dataset(path: Path) -> list[AnchorRow]:
    rows: list[AnchorRow] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            rows.append(
                AnchorRow(
                    source=raw["source"],
                    source_event_id=int(raw["source_event_id"]),
                    source_market_id=raw["source_market_id"],
                    canonical_event_id=(
                        int(raw["canonical_event_id"]) if raw["canonical_event_id"] else None
                    ),
                    sport=raw["sport"],
                    league=raw["league"],
                    home=raw["home"],
                    away=raw["away"],
                    event_start_time_utc=raw["event_start_time_utc"],
                    market_type=raw["market_type"],
                    period=raw["period"],
                    line=float(raw["line"]) if raw["line"] else None,
                    selection=raw["selection"],
                    price=raw["price"],
                    captured_at=raw["captured_at"],
                    freshness_seconds=(
                        int(raw["freshness_seconds"]) if raw["freshness_seconds"] else None
                    ),
                    raw_snapshot_id=int(raw["raw_snapshot_id"]),
                    parser_method=raw["parser_method"],
                    match_confidence=(
                        float(raw["match_confidence"]) if raw["match_confidence"] else None
                    ),
                    match_method=raw["match_method"] or None,
                    usable=raw["usable"] == "True",
                    rejection_reason=raw["rejection_reason"] or None,
                    role=raw.get("role") or "anchor",
                )
            )
    return rows


def write_manifest(path: Path, manifest: Mapping[str, object]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing manifest: {path}")
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
