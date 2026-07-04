"""Per-sport pick-quality shadow report — coverage, trusted CLV, freshness, H6 replay.

READ-ONLY research instrument (SELECT-only; adds and mutates nothing; places no
bets). One CLI, four sections per sport (soccer / basketball / tennis /
american_football — discovered from the warehouse, pinnacle_*/betfair_* capture
namespaces excluded from the base list):

  (a) COVERAGE (last --days days): events, pinnacle-namespace counterparts,
      event_source_links matched rate, sharp-anchored pick share, per-event
      soft-book count distribution. Plus a PINNACLE MATCH-CEILING
      DECOMPOSITION of the unmatched OddsPortal-side events:
        STRUCTURAL   — Pinnacle has zero in-window events for the event's
                       league, so no match was ever possible (excluded from
                       the corrected match-rate denominator);
        ADDRESSABLE  — Pinnacle prices the league in-window but the matcher
                       missed the event;
        UNKNOWN      — no league-identity evidence either way (kept in the
                       denominator: the corrected rate is a LOWER bound).
      League identity heuristic (conservative, in classify_unmatched_event):
      (1) co-occurrence — leagues of already-matched event pairs (all-time
      event_source_links) map canonical league -> pinnacle league(s); if
      mapped, structural/addressable is decided by whether any mapped
      pinnacle league has an in-window event. (2) Otherwise a normalized
      exact league-name match (countries must agree unless one side is
      empty) against in-window pinnacle leagues => addressable. (3) No
      fuzzy matching — anything else is UNKNOWN, never guessed.
  (b) TRUSTED CLV (all settled picks): n / mean / ddof=1 SE by market, using the
      SAME trust guards as the production report (app/storage/repositories.py:
      tautology, fabricated-close, close-independence, sharp close anchor,
      symmetric devig fallback). Untrusted-close and no-close rates;
      anchor_staleness_decision counts.
  (c) FRESHNESS STRATIFICATION: each trusted settled pick joined to its
      mint-time anchor snapshot (latest odds_snapshots row for the pick's
      anchor book + mapped market with captured_at <= pick.created_at; Pinnacle
      anchors resolve through event_source_links to the pinnacle_* counterpart
      event — never fuzzy SQL). anchor_age_at_mint buckets [0-15m, 15-60m,
      1-4h, 4h+] x mint-to-kickoff buckets [0-2h, 2-12h, 12h+], trusted CLV
      mean/SE/n per bucket; n<30 buckets are labelled insufficient.
  (d) H6 RETROSPECTIVE REPLAY: for settled SHARP-ANCHORED picks, recompute the
      at-mint soft consensus (median of per-book devigged probs from snapshots
      captured within 30 min before created_at; devig via
      app.probabilities.devig — never reimplemented) and run
      app.backtesting.agreement.agreement_verdict. Pass/fail/reference_missing
      counts + trusted CLV of pass vs fail groups (no claims below n=30).

EXPLORATORY — any threshold later frozen from this data must treat this
readout as spent. Nothing here is a pre-registered acceptance readout.

DB access: the DATABASE_URL is read from the repo .env with the host:port
swapped (default 172.19.0.4:5432, override REPORT_DB_HOST) — the URL and its
credentials are NEVER printed. Output: stdout tables + a JSON dump under
docs/research/ (refuses to overwrite an existing file).

  uv run python scripts/research/sport_quality_report.py [--days 30]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

EXPLORATORY_BANNER = (
    "EXPLORATORY — any threshold later frozen from this data must treat this readout as spent."
)

# Honesty floor for any per-bucket/per-group claim in this report (mirrors the
# MIN_STRATUM_N doctrine in app/backtesting/live_evidence.py at report scale;
# the task freezes 30 for these exploratory buckets).
MIN_BUCKET_N = 30

# H6 replay: minimum distinct soft books for an at-mint consensus median —
# fewer and the "median" is one book's opinion, so the reference is MISSING
# (fail-closed exclusion), never a faked consensus.
MIN_CONSENSUS_BOOKS = 3
CONSENSUS_WINDOW = timedelta(minutes=30)

ANCHOR_AGE_BUCKETS = ("0-15m", "15-60m", "1-4h", "4h+")
MINT_TO_KICKOFF_BUCKETS = ("0-2h", "2-12h", "12h+", "post-kickoff")

# Books that are never part of a SOFT consensus / soft-book count — mirrors
# app/edge/value.SHARP_BOOKS normalization.
SHARP_BOOK_NORMS = frozenset({"pinnacle", "pinnacle sports", "betfair exchange", "smarkets"})

# Snapshot market key -> expected complete outcome-set size (default 2).
_THREE_WAY_MARKETS = frozenset({"1x2", "double_chance"})


# --------------------------------------------------------------------------- #
# Pure helpers (no DB, no env) — unit-tested in tests/test_sport_quality_report.py
# --------------------------------------------------------------------------- #
def bucket_anchor_age(seconds: float) -> str:
    """Bucket a mint-time anchor age (created_at - captured_at, seconds >= 0)."""
    if seconds < 0:
        raise ValueError(f"anchor age cannot be negative, got {seconds}")
    if seconds < 15 * 60:
        return "0-15m"
    if seconds < 60 * 60:
        return "15-60m"
    if seconds < 4 * 3600:
        return "1-4h"
    return "4h+"


def bucket_mint_to_kickoff(seconds: float) -> str:
    """Bucket the mint-to-kickoff lead time (starts_at - created_at, seconds).

    Negative = the pick was minted after the stored kickoff (placeholder
    kickoffs / late mints) — reported honestly as its own bucket rather than
    silently folded into 0-2h."""
    if seconds < 0:
        return "post-kickoff"
    if seconds < 2 * 3600:
        return "0-2h"
    if seconds < 12 * 3600:
        return "2-12h"
    return "12h+"


def mean_se(values: list[float]) -> tuple[float | None, float | None]:
    """(mean, ddof=1 SE). SE is None below n=2 — never a fake-zero SE."""
    n = len(values)
    if n == 0:
        return None, None
    m = sum(values) / n
    if n < 2:
        return m, None
    se = math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1)) / math.sqrt(n)
    return m, se


def group_stats(values: list[float]) -> dict[str, Any]:
    """n / mean / se / sample-size label for one stratum of clv_log values."""
    m, se = mean_se(values)
    return {
        "n": len(values),
        "mean": m,
        "se": se,
        "ci95": ([m - 2 * se, m + 2 * se] if m is not None and se is not None else None),
        "label": "ok" if len(values) >= MIN_BUCKET_N else f"insufficient (n<{MIN_BUCKET_N})",
    }


def consensus_median(book_probs: list[float]) -> float | None:
    """Median of per-book devigged probs; None (reference missing) below
    MIN_CONSENSUS_BOOKS — a thin 'consensus' is not a consensus."""
    if len(book_probs) < MIN_CONSENSUS_BOOKS:
        return None
    return float(statistics.median(book_probs))


def is_soft_book(bookmaker: str) -> bool:
    return bookmaker.strip().lower() not in SHARP_BOOK_NORMS


def expected_outcomes(snapshot_market: str) -> int:
    return 3 if snapshot_market in _THREE_WAY_MARKETS else 2


@dataclass(frozen=True)
class SettledPick:
    """The per-pick columns the report needs (mirrors the SELECT below)."""

    pick_id: int
    sport: str
    market: str
    selection: str
    event_id: int
    anchor_type: str | None
    anchor_book: str | None
    closing_anchor_type: str | None
    has_snapshot_close: bool | None
    close_independent_of_fill: bool | None
    mint_devig_fell_back: bool | None
    close_devig_fell_back: bool | None
    anchor_staleness_decision: str | None
    clv_log: float | None
    closing_fair_probability: float | None
    model_probability: float | None
    decimal_odds: float | None
    created_at: datetime
    starts_at: datetime | None
    outcome: str


def is_trusted_clv_row(p: SettledPick) -> bool:
    """The production trusted-sharp-CLV gate, reused verbatim (never restated):
    guards from app/storage/repositories.py + genuine snapshot close + sharp
    close anchor + close independence exactly True + symmetric devig fallback."""
    from app.storage.repositories import (
        _SHARP_CLOSE_ANCHORS,
        _clv_row_is_fabricated,
        _clv_row_is_tautological,
        _devig_fallback_asymmetric,
    )

    if p.clv_log is None:
        return False
    if not p.has_snapshot_close:
        return False
    if p.closing_anchor_type not in _SHARP_CLOSE_ANCHORS:
        return False
    if p.close_independent_of_fill is not True:
        return False
    if _clv_row_is_tautological(p.clv_log, p.closing_fair_probability, p.model_probability):
        return False
    if _clv_row_is_fabricated(p.clv_log, p.decimal_odds, p.closing_fair_probability):
        return False
    return not _devig_fallback_asymmetric(p.mint_devig_fell_back, p.close_devig_fell_back)


def allowed_snapshot_markets(sport: str, market: str, selection: str) -> set[str]:
    """Snapshot market keys that can carry this pick's anchor/consensus prices.

    Reuses the production pick->provider market mapping
    (app/clv_trueup._pick_market_keys) and always includes the pick's own
    market key (some capture paths store the pick-native key)."""
    from app.clv_trueup import _pick_market_keys

    mapped = _pick_market_keys(sport, market, selection) or ()
    return set(mapped) | {market}


def latest_capture_at_or_before(
    snaps: list[tuple[str, datetime]],
    created_at: datetime,
    allowed_markets: set[str],
) -> tuple[datetime | None, bool]:
    """Latest captured_at <= created_at among ``(market, captured_at)`` rows.

    Prefers rows in ``allowed_markets``; falls back to any market of the anchor
    book on the event (second return value True = market-level fallback used).
    """
    in_market = [c for m, c in snaps if c <= created_at and m in allowed_markets]
    if in_market:
        return max(in_market), False
    any_market = [c for _, c in snaps if c <= created_at]
    if any_market:
        return max(any_market), True
    return None, False


def normalize_league(name: str, country: str = "") -> tuple[str, str]:
    """(normalized country, normalized name): lowercase, non-alphanumerics
    collapsed to single spaces. Used ONLY for exact-equality league-name
    matching — never fuzzy/substring (wrong-league risk)."""

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    return norm(country), norm(name)


def classify_unmatched_event(
    league_id: int,
    league_name: str,
    league_country: str,
    *,
    co_map: dict[int, set[int]],
    pinnacle_inwindow_ids: set[int],
    pinnacle_inwindow_names: set[tuple[str, str]],
) -> str:
    """Classify one UNMATCHED OddsPortal-side event by league evidence.

    Returns 'structural' | 'addressable' | 'unknown' (see module docstring).
    ``co_map`` = canonical league_id -> pinnacle league_ids from all-time
    matched pairs; ``pinnacle_inwindow_ids``/``_names`` describe pinnacle
    leagues with >=1 event inside the coverage window."""
    mapped = co_map.get(league_id)
    if mapped:
        return "addressable" if mapped & pinnacle_inwindow_ids else "structural"
    country, name = normalize_league(league_name, league_country)
    for p_country, p_name in pinnacle_inwindow_names:
        if name and p_name == name and (not country or not p_country or p_country == country):
            return "addressable"
    return "unknown"


def corrected_match_rates(
    events: int, matched: int, structural: int, unknown: int
) -> tuple[float | None, float | None]:
    """(lower, upper) corrected match rates: lower excludes only proven
    STRUCTURAL events from the denominator; upper also excludes UNKNOWN
    (the optimistic bound if every unknown league is truly unpriced)."""
    lower_den = events - structural
    upper_den = events - structural - unknown
    return (
        matched / lower_den if lower_den > 0 else None,
        matched / upper_den if upper_den > 0 else None,
    )


def _ceiling_block(events: int, matched: int, counts: Counter[str]) -> dict[str, Any]:
    """JSON block for one sport's unmatched-event decomposition."""
    structural = counts.get("structural", 0)
    addressable = counts.get("addressable", 0)
    unknown = counts.get("unknown", 0)
    lower, upper = corrected_match_rates(events, matched, structural, unknown)
    return {
        "unmatched": structural + addressable + unknown,
        "structural": structural,
        "addressable": addressable,
        "unknown_league": unknown,
        "corrected_match_rate_lower": lower,
        "corrected_match_rate_upper": upper,
        "note": (
            "structural = no in-window pinnacle event for the event's league "
            "(co-occurrence map, else exact normalized name); unknown = no "
            "league-identity evidence, kept in the lower-bound denominator"
        ),
    }


def soft_book_bucket(n: int) -> str:
    if n == 0:
        return "0"
    if n <= 3:
        return "1-3"
    if n <= 9:
        return "4-9"
    if n <= 19:
        return "10-19"
    return "20+"


def consensus_prob_at_mint(
    snaps: list[tuple[str, str, str, float, datetime]],
    *,
    created_at: datetime,
    selection: str,
    allowed_markets: set[str],
) -> tuple[float | None, int]:
    """At-mint soft-consensus probability for ``selection``.

    ``snaps`` = (bookmaker, market, selection, decimal_odds, captured_at) rows
    for the pick's event, SOFT books only. Per book: take its latest capture
    within [created_at - 30min, created_at] restricted to ``allowed_markets``,
    require a COMPLETE outcome set at that (market, captured_at), devig it
    (POWER — the frozen H4 global default) via app.probabilities.devig, and
    read the pick's selection. Returns (median prob or None, contributing-book
    count); None = reference missing (fail-closed), never a faked consensus."""
    from app.probabilities.devig import DevigMethod, devig

    lo = created_at - CONSENSUS_WINDOW
    per_book: dict[str, list[tuple[str, str, float, datetime]]] = defaultdict(list)
    for book, market, sel, odds, cap in snaps:
        if market in allowed_markets and lo <= cap <= created_at:
            per_book[book].append((market, sel, odds, cap))
    probs: list[float] = []
    for _book, rows in per_book.items():
        latest = max(c for _m, _s, _o, c in rows)
        # the complete outcome set at that book's latest capture instant
        group: dict[tuple[str, str], float] = {}
        for m, s, o, c in rows:
            if c == latest:
                group[(m, s)] = o
        by_market: dict[str, dict[str, float]] = defaultdict(dict)
        for (m, s), o in group.items():
            by_market[m][s] = o
        for m, sel_odds in by_market.items():
            if selection not in sel_odds or len(sel_odds) < expected_outcomes(m):
                continue
            ordered = sorted(sel_odds.items())
            try:
                fair = devig([o for _, o in ordered], method=DevigMethod.POWER)
            except ValueError:
                continue
            idx = [s for s, _ in ordered].index(selection)
            probs.append(fair[idx])
            break  # one contribution per book
    return consensus_median(probs), len(probs)


# --------------------------------------------------------------------------- #
# DB access (READ-ONLY SELECTs)
# --------------------------------------------------------------------------- #
def database_url() -> str:
    """DATABASE_URL from the repo .env with host:port swapped for this sandbox
    (default 172.19.0.4:5432; override REPORT_DB_HOST). NEVER printed/logged."""
    env_path = _REPO_ROOT / ".env"
    raw = ""
    for line in env_path.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            raw = line.split("=", 1)[1].strip()
            break
    if not raw:
        raise RuntimeError("DATABASE_URL not found in .env (value never printed)")
    host = os.environ.get("REPORT_DB_HOST", "172.19.0.4:5432")
    return re.sub(r"@[^/]+/", f"@{host}/", raw)


async def _fetch_all(conn: Any, sql: str, params: dict[str, Any]) -> list[Any]:
    from sqlalchemy import text

    return list((await conn.execute(text(sql), params)).fetchall())


async def collect_report(days: int) -> dict[str, Any]:
    """Run every SELECT and assemble the full per-sport report payload."""
    from sqlalchemy.ext.asyncio import create_async_engine

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as conn:
            sports = [
                r[0]
                for r in await _fetch_all(conn, "SELECT key FROM sports ORDER BY key", {})
                if not (r[0].startswith("pinnacle_") or r[0].startswith("betfair_"))
            ]

            coverage = await _fetch_all(
                conn,
                """
                SELECT s.key AS sport, count(*) AS n_events,
                       count(*) FILTER (WHERE esl.id IS NOT NULL) AS n_matched
                FROM events e JOIN sports s ON s.id = e.sport_id
                LEFT JOIN LATERAL (
                    SELECT l.id FROM event_source_links l
                    WHERE l.canonical_event_id = e.id
                      AND l.source = 'pinnacle_arcadia' AND l.active
                    LIMIT 1
                ) esl ON true
                WHERE s.key = ANY(:base) AND e.starts_at >= :cutoff AND e.starts_at <= :now
                GROUP BY 1
                """,
                {"base": sports, "cutoff": cutoff, "now": now},
            )
            # Pinnacle match-ceiling decomposition inputs (all SELECT-only):
            # unmatched OddsPortal-side events + their league row, ...
            unmatched_rows = await _fetch_all(
                conn,
                """
                SELECT s.key AS sport, e.league_id, l.name, l.country
                FROM events e
                JOIN sports s ON s.id = e.sport_id
                JOIN leagues l ON l.id = e.league_id
                LEFT JOIN LATERAL (
                    SELECT lk.id FROM event_source_links lk
                    WHERE lk.canonical_event_id = e.id
                      AND lk.source = 'pinnacle_arcadia' AND lk.active
                    LIMIT 1
                ) esl ON true
                WHERE s.key = ANY(:base) AND e.starts_at >= :cutoff AND e.starts_at <= :now
                  AND esl.id IS NULL
                """,
                {"base": sports, "cutoff": cutoff, "now": now},
            )
            # ... the all-time co-occurrence league map from matched pairs, ...
            co_rows = await _fetch_all(
                conn,
                r"""
                SELECT DISTINCT ce.league_id, pe.league_id
                FROM event_source_links esl
                JOIN events ce ON ce.id = esl.canonical_event_id
                JOIN events pe ON pe.external_ref = esl.source_event_id
                JOIN sports ps ON ps.id = pe.sport_id AND ps.key LIKE 'pinnacle\_%'
                WHERE esl.source = 'pinnacle_arcadia' AND esl.active
                """,
                {},
            )
            # ... and the pinnacle-namespace leagues with >=1 in-window event.
            pinn_league_rows = await _fetch_all(
                conn,
                r"""
                SELECT DISTINCT ps.key AS pinn_sport, l.id, l.name, l.country
                FROM events pe
                JOIN sports ps ON ps.id = pe.sport_id AND ps.key LIKE 'pinnacle\_%'
                JOIN leagues l ON l.id = pe.league_id
                WHERE pe.starts_at >= :cutoff AND pe.starts_at <= :now
                """,
                {"cutoff": cutoff, "now": now},
            )
            pinn_counts = await _fetch_all(
                conn,
                r"""
                SELECT s.key, count(*) FROM events e JOIN sports s ON s.id = e.sport_id
                WHERE s.key LIKE 'pinnacle\_%'
                  AND e.starts_at >= :cutoff AND e.starts_at <= :now
                GROUP BY 1
                """,
                {"cutoff": cutoff, "now": now},
            )
            pick_share = await _fetch_all(
                conn,
                """
                SELECT s.key, count(*) AS n_picks,
                       count(*) FILTER (WHERE p.anchor_type IN ('pinnacle','sharp')) AS n_sharp
                FROM picks p JOIN events e ON e.id = p.event_id
                JOIN sports s ON s.id = e.sport_id
                WHERE p.created_at >= :cutoff
                GROUP BY 1
                """,
                {"cutoff": cutoff},
            )
            soft_counts = await _fetch_all(
                conn,
                """
                SELECT s.key AS sport, cnt.n_soft
                FROM (
                  SELECT e.id AS eid, e.sport_id, count(DISTINCT os.bookmaker) AS n_soft
                  FROM events e JOIN odds_snapshots os ON os.event_id = e.id
                  WHERE lower(os.bookmaker) != ALL(:sharp)
                    AND e.starts_at >= :cutoff AND e.starts_at <= :now
                  GROUP BY e.id, e.sport_id
                ) cnt JOIN sports s ON s.id = cnt.sport_id
                WHERE s.key = ANY(:base)
                """,
                {"base": sports, "cutoff": cutoff, "now": now, "sharp": list(SHARP_BOOK_NORMS)},
            )
            settled_rows = await _fetch_all(
                conn,
                """
                SELECT p.id, s.key, p.market, p.selection, p.event_id,
                       p.anchor_type, p.anchor_book, p.closing_anchor_type,
                       p.has_snapshot_close, p.close_independent_of_fill,
                       p.mint_devig_fell_back, p.close_devig_fell_back,
                       p.anchor_staleness_decision,
                       p.clv_log, p.closing_fair_probability, p.model_probability,
                       p.decimal_odds, p.created_at, e.starts_at, rt.outcome
                FROM picks p
                JOIN result_tracking rt ON rt.pick_id = p.id
                JOIN events e ON e.id = p.event_id
                JOIN sports s ON s.id = e.sport_id
                """,
                {},
            )
            picks = [
                SettledPick(
                    pick_id=r[0],
                    sport=r[1],
                    market=r[2],
                    selection=r[3],
                    event_id=r[4],
                    anchor_type=r[5],
                    anchor_book=r[6],
                    closing_anchor_type=r[7],
                    has_snapshot_close=r[8],
                    close_independent_of_fill=r[9],
                    mint_devig_fell_back=r[10],
                    close_devig_fell_back=r[11],
                    anchor_staleness_decision=r[12],
                    clv_log=float(r[13]) if r[13] is not None else None,
                    closing_fair_probability=float(r[14]) if r[14] is not None else None,
                    model_probability=float(r[15]) if r[15] is not None else None,
                    decimal_odds=float(r[16]) if r[16] is not None else None,
                    created_at=r[17],
                    starts_at=r[18],
                    outcome=r[19],
                )
                for r in settled_rows
            ]

            event_ids = sorted({p.event_id for p in picks})
            counterpart_rows = await _fetch_all(
                conn,
                r"""
                SELECT esl.canonical_event_id, pe.id
                FROM event_source_links esl
                JOIN events pe ON pe.external_ref = esl.source_event_id
                JOIN sports ps ON ps.id = pe.sport_id AND ps.key LIKE 'pinnacle\_%'
                WHERE esl.source = 'pinnacle_arcadia' AND esl.active
                  AND esl.canonical_event_id = ANY(:ids)
                """,
                {"ids": event_ids},
            )
            counterpart = {r[0]: r[1] for r in counterpart_rows}

            anchor_event_ids = sorted(set(counterpart.values()) | set(event_ids))
            anchor_snap_rows = await _fetch_all(
                conn,
                """
                SELECT event_id, bookmaker, market, captured_at FROM odds_snapshots
                WHERE event_id = ANY(:ids) AND lower(bookmaker) = ANY(:sharp)
                """,
                {"ids": anchor_event_ids, "sharp": list(SHARP_BOOK_NORMS)},
            )
            anchor_snaps: dict[tuple[int, str], list[tuple[str, datetime]]] = defaultdict(list)
            for eid, book, market, cap in anchor_snap_rows:
                anchor_snaps[(eid, book.strip().lower())].append((market, cap))

            sharp_picks = [p for p in picks if p.anchor_type in ("pinnacle", "sharp")]
            sharp_event_ids = sorted({p.event_id for p in sharp_picks})
            h6_markets = sorted(
                {
                    m
                    for p in sharp_picks
                    for m in allowed_snapshot_markets(p.sport, p.market, p.selection)
                }
            )
            soft_snap_rows = (
                await _fetch_all(
                    conn,
                    """
                    SELECT event_id, bookmaker, market, selection, decimal_odds, captured_at
                    FROM odds_snapshots
                    WHERE event_id = ANY(:ids) AND market = ANY(:mkts)
                      AND lower(bookmaker) != ALL(:sharp)
                    """,
                    {
                        "ids": sharp_event_ids,
                        "mkts": h6_markets,
                        "sharp": list(SHARP_BOOK_NORMS),
                    },
                )
                if sharp_event_ids and h6_markets
                else []
            )
            soft_snaps: dict[int, list[tuple[str, str, str, float, datetime]]] = defaultdict(list)
            for eid, book, market, sel, odds, cap in soft_snap_rows:
                soft_snaps[eid].append((book, market, sel, float(odds), cap))
    finally:
        await engine.dispose()

    return _assemble(
        sports=sports,
        days=days,
        coverage=coverage,
        pinn_counts=pinn_counts,
        pick_share=pick_share,
        soft_counts=soft_counts,
        picks=picks,
        counterpart=counterpart,
        anchor_snaps=anchor_snaps,
        unmatched_rows=unmatched_rows,
        co_rows=co_rows,
        pinn_league_rows=pinn_league_rows,
    ) | {"h6_replay": h6_replay(picks, soft_snaps)}


# --------------------------------------------------------------------------- #
# Assembly (pure given fetched rows)
# --------------------------------------------------------------------------- #
def _assemble(
    *,
    sports: list[str],
    days: int,
    coverage: list[Any],
    pinn_counts: list[Any],
    pick_share: list[Any],
    soft_counts: list[Any],
    picks: list[SettledPick],
    counterpart: dict[int, int],
    anchor_snaps: dict[tuple[int, str], list[tuple[str, datetime]]],
    unmatched_rows: list[Any],
    co_rows: list[Any],
    pinn_league_rows: list[Any],
) -> dict[str, Any]:
    cov = {r[0]: {"events": int(r[1]), "matched": int(r[2])} for r in coverage}

    # Pinnacle match-ceiling decomposition of unmatched OddsPortal-side events.
    co_map: dict[int, set[int]] = defaultdict(set)
    for can_lid, pinn_lid in co_rows:
        co_map[int(can_lid)].add(int(pinn_lid))
    pinn_ids_by_sport: dict[str, set[int]] = defaultdict(set)
    pinn_names_by_sport: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for pinn_sport, lid, lname, lcountry in pinn_league_rows:
        base_sport = pinn_sport.removeprefix("pinnacle_")
        pinn_ids_by_sport[base_sport].add(int(lid))
        pinn_names_by_sport[base_sport].add(normalize_league(lname or "", lcountry or ""))
    ceiling: dict[str, Counter[str]] = defaultdict(Counter)
    for sport, league_id, lname, lcountry in unmatched_rows:
        ceiling[sport][
            classify_unmatched_event(
                int(league_id),
                lname or "",
                lcountry or "",
                co_map=co_map,
                pinnacle_inwindow_ids=pinn_ids_by_sport.get(sport, set()),
                pinnacle_inwindow_names=pinn_names_by_sport.get(sport, set()),
            )
        ] += 1
    pinn = {r[0].removeprefix("pinnacle_"): int(r[1]) for r in pinn_counts}
    share = {r[0]: {"picks": int(r[1]), "sharp": int(r[2])} for r in pick_share}
    soft_dist: dict[str, Counter[str]] = defaultdict(Counter)
    for sport, n_soft in soft_counts:
        soft_dist[sport][soft_book_bucket(int(n_soft))] += 1

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "label": EXPLORATORY_BANNER,
        "coverage_window_days": days,
        "sports": {},
    }
    for sport in sports:
        c = cov.get(sport, {"events": 0, "matched": 0})
        s = share.get(sport, {"picks": 0, "sharp": 0})
        settled = [p for p in picks if p.sport == sport]
        trusted = [p for p in settled if is_trusted_clv_row(p)]
        with_close = [p for p in settled if p.clv_log is not None]
        by_market: dict[str, Any] = {}
        for market in sorted({p.market for p in trusted}):
            by_market[market] = group_stats(
                [p.clv_log for p in trusted if p.market == market and p.clv_log is not None]
            )
        staleness = Counter(p.anchor_staleness_decision or "none" for p in settled)

        # (c) freshness stratification — trusted picks with a NAMED anchor book
        # (consensus anchors have no single mint snapshot; counted separately).
        age_buckets: dict[str, list[float]] = defaultdict(list)
        kick_buckets: dict[str, list[float]] = defaultdict(list)
        no_anchor_snapshot = consensus_anchor = market_fallbacks = 0
        for p in trusted:
            book = (p.anchor_book or "").strip().lower()
            if book not in SHARP_BOOK_NORMS:
                consensus_anchor += 1
                continue
            eid = counterpart.get(p.event_id) if book.startswith("pinnacle") else p.event_id
            snaps = anchor_snaps.get((eid, book), []) if eid is not None else []
            allowed = allowed_snapshot_markets(p.sport, p.market, p.selection)
            captured, fallback = latest_capture_at_or_before(snaps, p.created_at, allowed)
            if captured is None:
                no_anchor_snapshot += 1
                continue
            if fallback:
                market_fallbacks += 1
            if p.clv_log is None:
                continue
            age_buckets[bucket_anchor_age((p.created_at - captured).total_seconds())].append(
                p.clv_log
            )
            if p.starts_at is not None:
                kick_buckets[
                    bucket_mint_to_kickoff((p.starts_at - p.created_at).total_seconds())
                ].append(p.clv_log)

        report["sports"][sport] = {
            "coverage": {
                "events": c["events"],
                "pinnacle_namespace_events": pinn.get(sport, 0),
                "matched_events": c["matched"],
                "matched_rate": (c["matched"] / c["events"]) if c["events"] else None,
                "unmatched_decomposition": _ceiling_block(
                    c["events"], c["matched"], ceiling.get(sport, Counter())
                ),
                "picks": s["picks"],
                "sharp_anchored_picks": s["sharp"],
                "sharp_anchored_share": (s["sharp"] / s["picks"]) if s["picks"] else None,
                "soft_book_count_distribution": dict(soft_dist.get(sport, {})),
            },
            "trusted_clv": {
                "settled": len(settled),
                "trusted": group_stats([p.clv_log for p in trusted if p.clv_log is not None]),
                "by_market": by_market,
                "untrusted_close_rate": (
                    (len(with_close) - len(trusted)) / len(settled) if settled else None
                ),
                "no_close_rate": (
                    (len(settled) - len(with_close)) / len(settled) if settled else None
                ),
                "anchor_staleness_decision": dict(staleness),
            },
            "freshness": {
                "anchor_age_at_mint": {b: group_stats(v) for b, v in sorted(age_buckets.items())},
                "mint_to_kickoff": {b: group_stats(v) for b, v in sorted(kick_buckets.items())},
                "consensus_anchor_excluded": consensus_anchor,
                "no_anchor_snapshot": no_anchor_snapshot,
                "market_level_fallbacks": market_fallbacks,
            },
        }
    return report


def h6_replay(
    picks: list[SettledPick],
    soft_snaps: dict[int, list[tuple[str, str, str, float, datetime]]],
) -> dict[str, Any]:
    """(d) H6 retrospective replay over settled SHARP-ANCHORED picks.

    Anchor prob = the pick's persisted pick-time market fair
    (Pick.model_probability — the VALUE-strategy invariant documented in
    app/storage/repositories.py). Reference = the at-mint soft-consensus median
    (consensus_prob_at_mint). Tolerance = PROPOSED 0.02 abs-prob
    (app/backtesting/agreement.H6_TOLERANCE) — SIGNED 2026-07-04; no
    numeric tolerance was ever recorded in the research log."""
    from app.backtesting.agreement import (
        H6_TOLERANCE,
        REASON_REFERENCE_MISSING,
        REASON_SELECTION_MISSING,
        agreement_verdict,
    )

    out: dict[str, Any] = {
        "tolerance": H6_TOLERANCE,
        "tolerance_provenance": (
            "PROPOSED 0.02 abs-prob — ADR-0019 H6 says 'frozen at the value recorded "
            "in the research log' but no numeric value was recorded there"
        ),
        "per_sport": {},
    }
    sharp_picks = [p for p in picks if p.anchor_type in ("pinnacle", "sharp")]
    for sport in sorted({p.sport for p in sharp_picks}):
        rows = [p for p in sharp_picks if p.sport == sport]
        counts: Counter[str] = Counter()
        clv_pass: list[float] = []
        clv_fail: list[float] = []
        for p in rows:
            if p.model_probability is None:
                counts["anchor_prob_missing"] += 1
                continue
            allowed = allowed_snapshot_markets(p.sport, p.market, p.selection)
            median, n_books = consensus_prob_at_mint(
                soft_snaps.get(p.event_id, []),
                created_at=p.created_at,
                selection=p.selection,
                allowed_markets=allowed,
            )
            reference = {p.selection: median} if median is not None else None
            verdict = agreement_verdict(
                {p.selection: p.model_probability},
                reference,
                p.selection,
                H6_TOLERANCE,
            )
            if verdict.reason in (REASON_REFERENCE_MISSING, REASON_SELECTION_MISSING):
                counts[verdict.reason] += 1
                continue
            counts["pass" if verdict.passes else "fail"] += 1
            if is_trusted_clv_row(p) and p.clv_log is not None:
                (clv_pass if verdict.passes else clv_fail).append(p.clv_log)
        out["per_sport"][sport] = {
            "n_sharp_anchored_settled": len(rows),
            "counts": dict(counts),
            "trusted_clv_pass": group_stats(clv_pass),
            "trusted_clv_fail": group_stats(clv_fail),
            "min_consensus_books": MIN_CONSENSUS_BOOKS,
        }
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _fmt_stats(s: dict[str, Any]) -> str:
    if s["n"] == 0:
        return "n=0 (no rows)"
    mean = f"{s['mean']:+.4f}" if s["mean"] is not None else "n/a"
    se = f"+/-{2 * s['se']:.4f}(2SE)" if s["se"] is not None else "SE n/a"
    return f"n={s['n']:4d} mean {mean} {se} [{s['label']}]"


def _fmt_ceiling(d: dict[str, Any]) -> str:
    def pct(v: float | None) -> str:
        return f"{v:.1%}" if v is not None else "n/a"

    return (
        f"unmatched decomposition: unmatched={d['unmatched']} "
        f"structural={d['structural']} addressable={d['addressable']} "
        f"unknown-league={d['unknown_league']} | corrected match rate "
        f"[{pct(d['corrected_match_rate_lower'])}, {pct(d['corrected_match_rate_upper'])}]"
    )


def render(report: dict[str, Any]) -> str:
    lines = [report["label"], f"coverage window: last {report['coverage_window_days']} days", ""]
    for sport, data in report["sports"].items():
        c, t, f = data["coverage"], data["trusted_clv"], data["freshness"]
        lines += [
            f"=== {sport} ===",
            (
                f"coverage: events={c['events']} pinnacle_ns={c['pinnacle_namespace_events']} "
                f"matched={c['matched_events']} "
                f"({c['matched_rate']:.1%})"
                if c["matched_rate"] is not None
                else "coverage: n/a"
            ),
            _fmt_ceiling(c["unmatched_decomposition"]),
            (
                f"picks={c['picks']} sharp-anchored={c['sharp_anchored_picks']} "
                + (
                    f"({c['sharp_anchored_share']:.1%})"
                    if c["sharp_anchored_share"] is not None
                    else ""
                )
            ),
            f"soft-book count distribution (events): {c['soft_book_count_distribution']}",
            f"settled={t['settled']} | trusted CLV: {_fmt_stats(t['trusted'])}",
            (
                "untrusted-close rate: "
                + (
                    f"{t['untrusted_close_rate']:.1%}"
                    if t["untrusted_close_rate"] is not None
                    else "n/a"
                )
                + " | no-close rate: "
                + (f"{t['no_close_rate']:.1%}" if t["no_close_rate"] is not None else "n/a")
            ),
            f"anchor_staleness_decision: {t['anchor_staleness_decision']}",
        ]
        for market, s in t["by_market"].items():
            lines.append(f"  trusted CLV [{market:14s}]: {_fmt_stats(s)}")
        lines.append(
            "freshness (trusted, named-anchor only; "
            f"consensus-anchor excluded={f['consensus_anchor_excluded']} "
            f"no-anchor-snapshot={f['no_anchor_snapshot']} "
            f"market-fallbacks={f['market_level_fallbacks']}):"
        )
        for b in ANCHOR_AGE_BUCKETS:
            if b in f["anchor_age_at_mint"]:
                lines.append(f"  anchor age {b:>7s}: {_fmt_stats(f['anchor_age_at_mint'][b])}")
        for b in MINT_TO_KICKOFF_BUCKETS:
            if b in f["mint_to_kickoff"]:
                lines.append(f"  mint->KO  {b:>8s}: {_fmt_stats(f['mint_to_kickoff'][b])}")
        lines.append("")
    h6 = report["h6_replay"]
    lines += [
        "=== H6 retrospective replay (sharp-anchored settled picks) ===",
        f"tolerance: {h6['tolerance']} — {h6['tolerance_provenance']}",
    ]
    for sport, d in h6["per_sport"].items():
        lines += [
            f"[{sport}] n={d['n_sharp_anchored_settled']} counts={d['counts']}",
            f"  trusted CLV pass: {_fmt_stats(d['trusted_clv_pass'])}",
            f"  trusted CLV fail: {_fmt_stats(d['trusted_clv_fail'])}",
        ]
    lines += ["", report["label"]]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="coverage window (days)")
    parser.add_argument(
        "--json-out",
        default=str(
            _REPO_ROOT
            / "docs"
            / "research"
            / f"sport_quality_report_{datetime.now(UTC).date().isoformat()}.json"
        ),
    )
    args = parser.parse_args()
    json_path = Path(args.json_out)
    if json_path.exists():
        raise SystemExit(f"refusing to overwrite existing report: {json_path}")
    report = asyncio.run(collect_report(args.days))
    print(render(report))
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nJSON written: {json_path}")


if __name__ == "__main__":
    main()
