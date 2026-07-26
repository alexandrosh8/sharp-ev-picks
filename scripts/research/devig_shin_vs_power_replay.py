"""Shin-vs-power devig replay — paired, read-only, NO default change.

For settled picks with a NAMED sharp anchor and a trusted independent close,
recompute the MINT fair and the CLOSE fair from the STORED anchor odds
vectors (odds_snapshots groups — never re-scraped, never reimplemented devig:
app/probabilities/devig is called for BOTH methods) under POWER and SHIN, and
report per sport x market x anchor book:

  - EDGE SIGN-FLIP RATE AT THE 3% GATE: share of picks whose premium-gate
    decision (edge >= 0.03, edge = fair_prob * fill_odds - 1) DIFFERS between
    power and Shin at mint time. This measures how much the devig-method
    choice alone moves the premium gate.
  - PAIRED TRUSTED-CLV DELTA: clv_shin - clv_power per pick, where
    clv_method = ln(fill_odds * close_fair_method) and the close fair is
    recomputed from the SAME stored close-book snapshot group under each
    method (paired by construction — same pick, same vectors). Restricted to
    picks passing the production trusted sharp-CLV gate
    (scripts/research/sport_quality_report.is_trusted_clv_row, reused
    verbatim). Mean +/- 2SE (ddof=1); honesty floor n >= 50 — below it the
    cell is labelled insufficient and its mean is NULLED.

Consensus(median)-anchored picks have no single stored book vector and are
SKIPPED AND COUNTED, never approximated. Devig fallbacks (either method, on
either side) are skipped and counted — a fallback pair is not a method
comparison. NO ROI. NO winner selection. Nothing here changes
DEVIG_METHOD or any policy default.

EXPLORATORY — any threshold later frozen from this data must treat this
readout as spent.

  uv run python scripts/research/devig_shin_vs_power_replay.py [--gate 0.03] [--dry-run]

Decision-support only — this system never places bets.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.research.devig_comparison import (  # noqa: E402
    anchor_group_at_mint,
    resolve_selection_index,
)
from scripts.research.sport_quality_report import (  # noqa: E402
    EXPLORATORY_BANNER,
    SHARP_BOOK_NORMS,
    SettledPick,
    _fetch_all,
    allowed_snapshot_markets,
    database_url,
    is_trusted_clv_row,
    mean_se,
)

PREMIUM_GATE = 0.03  # the production premium edge gate this replay stresses
MIN_STRATUM_N = 50  # honesty floor for any per-cell claim


# --------------------------------------------------------------------------- #
# Pure helpers (numpy/stdlib + app pure-math devig only) — unit-tested in
# tests/test_devig_shin_vs_power_replay.py
# --------------------------------------------------------------------------- #
def fair_pair(odds: Sequence[float], idx: int) -> tuple[float, float] | None:
    """(p_power, p_shin) for outcome `idx` of one stored odds vector.

    None when EITHER method fell back to multiplicative (a fallback pair is
    not a method comparison) or the devig rejects the vector — skipped and
    counted by the caller, never guessed."""
    from app.probabilities.devig import DevigMethod, devig_with_provenance

    try:
        p_power, fb_power = devig_with_provenance(odds, method=DevigMethod.POWER)
        p_shin, fb_shin = devig_with_provenance(odds, method=DevigMethod.SHIN)
    except ValueError:
        return None
    if fb_power or fb_shin:
        return None
    return p_power[idx], p_shin[idx]


def edge_sign_flip(
    p_power: float, p_shin: float, fill_odds: float, gate: float = PREMIUM_GATE
) -> bool:
    """True when the premium-gate decision differs between the two methods."""
    return (p_power * fill_odds - 1.0 >= gate) != (p_shin * fill_odds - 1.0 >= gate)


def paired_clv_delta(p_close_power: float, p_close_shin: float, fill_odds: float) -> float:
    """clv_shin - clv_power = ln(odds*p_shin) - ln(odds*p_power) = ln(p_shin/p_power)."""
    return math.log(fill_odds * p_close_shin) - math.log(fill_odds * p_close_power)


def cell_summary(
    flips: Sequence[bool], deltas: Sequence[float], floor: int = MIN_STRATUM_N
) -> dict[str, Any]:
    """Per-cell flip rate + paired delta with HONESTY-FLOOR NULLING below
    `floor` pairs (rates/means from a handful of picks are noise)."""
    n_flip, n_delta = len(flips), len(deltas)
    out: dict[str, Any] = {"n_mint_pairs": n_flip, "n_clv_pairs": n_delta}
    if n_flip >= floor:
        out["flip_rate"] = sum(flips) / n_flip
        out["flip_label"] = "ok"
    else:
        out["flip_rate"] = None
        out["flip_label"] = f"insufficient (n<{floor})"
    mean, se = mean_se(list(deltas))
    if n_delta >= floor and mean is not None and se is not None:
        out["delta_mean"] = mean
        out["delta_2se"] = 2 * se
        out["delta_label"] = "ok"
    else:
        out["delta_mean"] = None
        out["delta_2se"] = None
        out["delta_label"] = f"insufficient (n<{floor})"
    return out


def summarize_cells(
    flips_by_cell: dict[tuple[str, str, str], list[bool]],
    deltas_by_cell: dict[tuple[str, str, str], list[float]],
    floor: int = MIN_STRATUM_N,
) -> list[dict[str, Any]]:
    cells = []
    for key in sorted(set(flips_by_cell) | set(deltas_by_cell)):
        sport, market, book = key
        cells.append(
            {
                "sport": sport,
                "market": market,
                "anchor_book": book,
                **cell_summary(flips_by_cell.get(key, []), deltas_by_cell.get(key, []), floor),
            }
        )
    return cells


# --------------------------------------------------------------------------- #
# DB collection (READ-ONLY SELECTs; same skeleton as devig_comparison.py)
# --------------------------------------------------------------------------- #
async def collect(gate: float) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.resolution.matching import default_aliases, normalize_name

    aliases = default_aliases()
    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as conn:
            rows = await _fetch_all(
                conn,
                """
                SELECT p.id, s.key, p.market, p.selection, p.event_id,
                       p.anchor_type, p.anchor_book, p.closing_anchor_type,
                       p.has_snapshot_close, p.close_independent_of_fill,
                       p.mint_devig_fell_back, p.close_devig_fell_back,
                       p.anchor_staleness_decision,
                       p.clv_log, p.closing_fair_probability, p.model_probability,
                       p.decimal_odds, p.created_at, e.starts_at, rt.outcome,
                       p.close_anchor_book, p.close_snapshot_captured_at
                FROM picks p
                JOIN result_tracking rt ON rt.pick_id = p.id
                JOIN events e ON e.id = p.event_id
                JOIN sports s ON s.id = e.sport_id
                WHERE p.close_independent_of_fill IS TRUE
                """,
                {},
            )
            picks: list[tuple[SettledPick, str | None, datetime | None]] = [
                (
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
                    ),
                    r[20],
                    r[21],
                )
                for r in rows
            ]
            event_ids = sorted({p.event_id for p, _, _ in picks})
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
            snap_rows = await _fetch_all(
                conn,
                """
                SELECT event_id, bookmaker, market, selection, decimal_odds, captured_at
                FROM odds_snapshots
                WHERE event_id = ANY(:ids) AND lower(bookmaker) = ANY(:sharp)
                """,
                {"ids": anchor_event_ids, "sharp": list(SHARP_BOOK_NORMS)},
            )
            snaps: dict[tuple[int, str], list[tuple[str, str, float, datetime]]] = defaultdict(list)
            for eid, book, market, sel, odds, cap in snap_rows:
                snaps[(eid, book.strip().lower())].append((market, sel, float(odds), cap))
    finally:
        await engine.dispose()

    skips: dict[str, int] = defaultdict(int)
    flips_by_cell: dict[tuple[str, str, str], list[bool]] = defaultdict(list)
    deltas_by_cell: dict[tuple[str, str, str], list[float]] = defaultdict(list)

    def snap_event(pick: SettledPick, book: str) -> int | None:
        return counterpart.get(pick.event_id) if book.startswith("pinnacle") else pick.event_id

    for pick, close_book_raw, close_captured_at in picks:
        mint_book = (pick.anchor_book or "").strip().lower()
        if mint_book not in SHARP_BOOK_NORMS:
            skips["consensus_or_unnamed_mint_anchor"] += 1
            continue
        if pick.decimal_odds is None:
            skips["no_fill_odds"] += 1
            continue
        allowed = allowed_snapshot_markets(pick.sport, pick.market, pick.selection)
        eid = snap_event(pick, mint_book)
        mint_group = (
            anchor_group_at_mint(snaps.get((eid, mint_book), []), pick.created_at, allowed)
            if eid is not None
            else None
        )
        if mint_group is None:
            skips["no_mint_anchor_group"] += 1
            continue
        ordered = sorted(mint_group.items())
        idx = resolve_selection_index(
            [s for s, _ in ordered], pick.selection, aliases, normalize_name
        )
        if idx is None:
            skips["mint_selection_unresolved"] += 1
            continue
        pair = fair_pair([o for _, o in ordered], idx)
        if pair is None:
            skips["mint_devig_fallback_or_error"] += 1
            continue
        cell = (pick.sport, pick.market, mint_book)
        flips_by_cell[cell].append(edge_sign_flip(pair[0], pair[1], pick.decimal_odds, gate))

        # ---- paired close-fair delta (trusted rows only) --------------------
        if not is_trusted_clv_row(pick):
            skips["untrusted_close"] += 1
            continue
        close_book = (close_book_raw or "").strip().lower()
        if close_book not in SHARP_BOOK_NORMS:
            skips["consensus_or_unnamed_close_anchor"] += 1
            continue
        close_cutoff = close_captured_at or pick.starts_at
        if close_cutoff is None:
            skips["no_close_cutoff"] += 1
            continue
        ceid = snap_event(pick, close_book)
        close_group = (
            anchor_group_at_mint(snaps.get((ceid, close_book), []), close_cutoff, allowed)
            if ceid is not None
            else None
        )
        if close_group is None:
            skips["no_close_anchor_group"] += 1
            continue
        cordered = sorted(close_group.items())
        cidx = resolve_selection_index(
            [s for s, _ in cordered], pick.selection, aliases, normalize_name
        )
        if cidx is None:
            skips["close_selection_unresolved"] += 1
            continue
        cpair = fair_pair([o for _, o in cordered], cidx)
        if cpair is None:
            skips["close_devig_fallback_or_error"] += 1
            continue
        deltas_by_cell[cell].append(paired_clv_delta(cpair[0], cpair[1], pick.decimal_odds))

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "label": EXPLORATORY_BANNER,
        "population": "settled picks, named sharp mint anchor; CLV delta on the trusted subset",
        "gate": gate,
        "honesty_floor_n": MIN_STRATUM_N,
        "n_picks": len(picks),
        "skips": dict(skips),
        "cells": summarize_cells(flips_by_cell, deltas_by_cell),
    }


# --------------------------------------------------------------------------- #
# Rendering + dry run
# --------------------------------------------------------------------------- #
def render(report: dict[str, Any]) -> str:
    lines = [
        report["label"],
        "SHIN-vs-POWER PAIRED DEVIG REPLAY — read-only; NO default change; NO",
        "winner selection. Deltas are clv_shin - clv_power on the SAME stored",
        f"vectors; flip rate is at the {report['gate']:.0%} premium gate.",
        f"population: {report['population']} (n={report['n_picks']}, "
        f"honesty floor n>={report['honesty_floor_n']})",
        f"skipped (counted, never guessed): {report['skips']}",
        "",
        f"{'sport':<14}{'market':<10}{'anchor_book':<18}{'nMint':>6} {'flip%':>8} "
        f"{'nCLV':>6} {'d(shin-power)':>16}",
    ]
    for c in report["cells"]:
        flip = f"{c['flip_rate'] * 100:>7.2f}%" if c["flip_rate"] is not None else f"{'--':>8}"
        if c["delta_mean"] is not None:
            delta = f"{c['delta_mean']:+.5f}±{c['delta_2se']:.5f}"
        else:
            delta = c["delta_label"]
        lines.append(
            f"{c['sport']:<14}{c['market']:<10}{c['anchor_book']:<18}"
            f"{c['n_mint_pairs']:>6d} {flip} {c['n_clv_pairs']:>6d} {delta:>16}"
        )
    lines += ["", report["label"], "Decision-support only — this system never places bets."]
    return "\n".join(lines)


def _dry_run_report(gate: float) -> dict[str, Any]:
    """Synthetic vectors through the REAL pure path (fair_pair/edge/delta)."""
    flips: dict[tuple[str, str, str], list[bool]] = defaultdict(list)
    deltas: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    cell = ("soccer", "h2h", "pinnacle")
    for i in range(60):
        odds = [2.05 + 0.001 * i, 3.4, 3.6]
        pair = fair_pair(odds, 0)
        assert pair is not None
        flips[cell].append(edge_sign_flip(pair[0], pair[1], 2.16 + 0.001 * i, gate))
        deltas[cell].append(paired_clv_delta(pair[0], pair[1], 2.16))
    thin = ("tennis", "h2h", "betfair exchange")
    flips[thin].append(False)  # below floor -> nulled in the summary
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "label": EXPLORATORY_BANNER,
        "population": "DRY RUN — synthetic vectors, no DB",
        "gate": gate,
        "honesty_floor_n": MIN_STRATUM_N,
        "n_picks": 61,
        "skips": {},
        "cells": summarize_cells(flips, deltas),
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", type=float, default=PREMIUM_GATE)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="no DB: run the pure replay path on synthetic vectors",
    )
    args = ap.parse_args(argv)
    report = _dry_run_report(args.gate) if args.dry_run else asyncio.run(collect(args.gate))
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
