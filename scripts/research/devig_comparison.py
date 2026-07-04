"""Devig-method comparison harness — VALIDATION-ONLY, read-only, no ROI.

Over settled picks with TRUSTED INDEPENDENT closes (close_independent_of_fill
IS TRUE), fetch the mint-time anchor snapshot GROUP (every outcome of the
pick's market at the anchor book's latest captured_at <= created_at; Pinnacle
anchors resolve through event_source_links to the pinnacle_* counterpart
event), recompute fair probabilities under EVERY DevigMethod
(app/probabilities/devig — never reimplemented), and report per
method x sport x market:

  - Brier score and log-loss of the pick-selection fair vs the settled outcome
    (ONLY where the binary outcome is derivable: won/lost; void/push/half_* and
    unresolved selections are SKIPPED and counted);
  - mean |fair - closing_fair_probability| (close consistency) where a close
    fair is persisted.

NO ROI. NO headline. NO selection of a winner — this is descriptive evidence
for a future pre-registered decision only.

EXPLORATORY — any threshold later frozen from this data must treat this
readout as spent.

DB access is SELECT-only via the same .env-derived URL as
sport_quality_report.py (credentials never printed). JSON dump under
docs/research/ (refuses to overwrite).

  uv run python scripts/research/devig_comparison.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.research.sport_quality_report import (  # noqa: E402
    EXPLORATORY_BANNER,
    SHARP_BOOK_NORMS,
    SettledPick,
    _fetch_all,
    allowed_snapshot_markets,
    database_url,
    expected_outcomes,
    mean_se,
)


def resolve_selection_index(
    selections: list[str], pick_selection: str, aliases: Any, normalize: Any
) -> int | None:
    """Index of the pick's selection within a snapshot outcome group.

    Exact match first; else alias-canonical normalized-name equality (the
    Pinnacle namespace stores its own team-name forms) — NEVER fuzzy. None =
    unresolved (the row is skipped and counted, not guessed)."""
    if pick_selection in selections:
        return selections.index(pick_selection)
    want = aliases.canonical(normalize(pick_selection))
    hits = [i for i, s in enumerate(selections) if aliases.canonical(normalize(s)) == want]
    return hits[0] if len(hits) == 1 else None


def anchor_group_at_mint(
    snaps: list[tuple[str, str, float, datetime]],
    created_at: datetime,
    allowed_markets: set[str],
) -> dict[str, float] | None:
    """The COMPLETE outcome set (selection -> decimal odds) at the anchor
    book's latest captured_at <= created_at within the pick's market keys.
    None when no complete set exists — fail closed, never a partial devig."""
    eligible = [(m, s, o, c) for m, s, o, c in snaps if m in allowed_markets and c <= created_at]
    if not eligible:
        return None
    by_capture: dict[tuple[str, datetime], dict[str, float]] = defaultdict(dict)
    for m, s, o, c in eligible:
        by_capture[(m, c)][s] = o
    complete = [
        (c, group) for (m, c), group in by_capture.items() if len(group) >= expected_outcomes(m)
    ]
    if not complete:
        return None
    return max(complete, key=lambda x: x[0])[1]


async def collect(json_path: Path) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.probabilities.devig import DevigMethod, devig
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
                       p.decimal_odds, p.created_at, e.starts_at, rt.outcome
                FROM picks p
                JOIN result_tracking rt ON rt.pick_id = p.id
                JOIN events e ON e.id = p.event_id
                JOIN sports s ON s.id = e.sport_id
                WHERE p.close_independent_of_fill IS TRUE
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
                for r in rows
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

    skips: Counter[str] = Counter()
    # (method, sport, market) -> metric accumulators
    briers: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    loglosses: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    close_gaps: dict[tuple[str, str, str], list[float]] = defaultdict(list)

    for p in picks:
        book = (p.anchor_book or "").strip().lower()
        if book not in SHARP_BOOK_NORMS:
            skips["consensus_or_unnamed_anchor"] += 1
            continue
        eid = counterpart.get(p.event_id) if book.startswith("pinnacle") else p.event_id
        group = (
            anchor_group_at_mint(
                snaps.get((eid, book), []),
                p.created_at,
                allowed_snapshot_markets(p.sport, p.market, p.selection),
            )
            if eid is not None
            else None
        )
        if group is None:
            skips["no_anchor_group"] += 1
            continue
        ordered = sorted(group.items())
        idx = resolve_selection_index([s for s, _ in ordered], p.selection, aliases, normalize_name)
        if idx is None:
            skips["selection_unresolved"] += 1
            continue
        y: int | None
        if p.outcome == "won":
            y = 1
        elif p.outcome == "lost":
            y = 0
        else:
            y = None
            skips[f"outcome_{p.outcome}"] += 1
        for method in DevigMethod:
            try:
                fair = devig([o for _, o in ordered], method=method)
            except ValueError:
                skips[f"devig_error_{method.value}"] += 1
                continue
            prob = fair[idx]
            key = (method.value, p.sport, p.market)
            if y is not None and 0.0 < prob < 1.0:
                briers[key].append((prob - y) ** 2)
                loglosses[key].append(-(y * math.log(prob) + (1 - y) * math.log(1.0 - prob)))
            if p.closing_fair_probability is not None:
                close_gaps[key].append(abs(prob - p.closing_fair_probability))

    cells: list[dict[str, Any]] = []
    for key in sorted(set(briers) | set(close_gaps)):
        method, sport, market = key
        b_mean, b_se = mean_se(briers.get(key, []))
        ll_mean, ll_se = mean_se(loglosses.get(key, []))
        g_mean, g_se = mean_se(close_gaps.get(key, []))
        cells.append(
            {
                "method": method,
                "sport": sport,
                "market": market,
                "n_outcome": len(briers.get(key, [])),
                "brier": b_mean,
                "brier_se": b_se,
                "log_loss": ll_mean,
                "log_loss_se": ll_se,
                "n_close": len(close_gaps.get(key, [])),
                "mean_abs_fair_minus_close": g_mean,
                "mean_abs_fair_minus_close_se": g_se,
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "label": EXPLORATORY_BANNER,
        "population": "settled picks with close_independent_of_fill IS TRUE",
        "n_picks": len(picks),
        "skips": dict(skips),
        "cells": cells,
        "json_path": str(json_path),
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        report["label"],
        "DEVIG COMPARISON — validation-only; NO ROI, NO headline, NO winner selection.",
        f"population: {report['population']} (n={report['n_picks']})",
        f"skipped (counted, never guessed): {report['skips']}",
        "",
        f"{'method':<28}{'sport':<12}{'market':<10}{'n':>5} "
        f"{'brier':>8} {'logloss':>8} {'nCl':>5} {'|f-close|':>10}",
    ]
    for c in report["cells"]:
        gap = c["mean_abs_fair_minus_close"]
        gap = gap if gap is not None else float("nan")
        lines.append(
            f"{c['method']:<28}{c['sport']:<12}{c['market']:<10}{c['n_outcome']:>5d} "
            f"{c['brier'] if c['brier'] is not None else float('nan'):>8.4f} "
            f"{c['log_loss'] if c['log_loss'] is not None else float('nan'):>8.4f} "
            f"{c['n_close']:>5d} "
            f"{gap:>10.4f}"
        )
    lines += ["", report["label"]]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        default=str(
            _REPO_ROOT
            / "docs"
            / "research"
            / f"devig_comparison_{datetime.now(UTC).date().isoformat()}.json"
        ),
    )
    args = parser.parse_args()
    json_path = Path(args.json_out)
    if json_path.exists():
        raise SystemExit(f"refusing to overwrite existing report: {json_path}")
    report = asyncio.run(collect(json_path))
    print(render(report))
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nJSON written: {json_path}")


if __name__ == "__main__":
    main()
