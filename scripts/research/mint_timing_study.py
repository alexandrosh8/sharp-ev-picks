"""Mint-timing study — trusted CLV split by hours-to-kickoff bucket.

RETROACTIVE FIRST PASS of the instrument that will later arm
`VALUE_PREMIUM_MAX_HOURS_TO_KICKOFF` (app/config.py — default 0.0 = gate OFF;
the pre-registered mint-timing hypothesis is "h2h picks minted >24h out carry
negative CLV"). READ-ONLY research: SELECT-only DB access, prints a report,
changes NO default, flips NO flag, places NO bet.

METHOD
  - Population: settled picks (result_tracking join) restricted to the
    production TRUSTED sharp-CLV subset — the gate is reused verbatim via
    scripts/research/sport_quality_report.is_trusted_clv_row (tautology,
    fabricated-close, close-independence, sharp close anchor, symmetric devig
    fallback guards from app/storage/repositories.py). Never restated here.
  - hours_to_kickoff: the NEW picks.hours_to_kickoff telemetry column when
    present; for rows predating it, the fallback is
    (events.starts_at - picks.created_at) in hours. Rows with neither are
    SKIPPED and counted, never guessed.
  - Buckets per sport x market: post-kickoff (<0), 0-2h, 2-6h, 6-12h,
    12-24h, 24-48h, 48h+. Per bucket: n, mean trusted clv_log, ddof=1 SE and
    a Student-t 95% CI. HONESTY FLOOR n >= 50 (MIN_STRATUM_N doctrine,
    app/backtesting/live_evidence.py): below it mean/SE/CI are NULLED and the
    bucket is labelled insufficient — small-n means are never printed as
    evidence.
  - ARM/HOLD recommendation per sport x market: for each candidate threshold
    h in {12, 24, 48}, pool the trusted CLV of picks minted MORE than h hours
    out (the slice the gate would drop). ARM is recommended at the smallest h
    whose slice has n >= 50 AND a t-CI entirely below 0 (the dropped slice is
    significantly CLV-negative); otherwise HOLD. This prints a recommendation
    only — arming remains an operator decision.

EXPLORATORY — any threshold later frozen from this data must treat this
readout as spent.

  uv run python scripts/research/mint_timing_study.py [--days 0] [--dry-run]

Decision-support only — this system never places bets.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.research.sport_quality_report import (  # noqa: E402
    EXPLORATORY_BANNER,
    SettledPick,
    _fetch_all,
    database_url,
    is_trusted_clv_row,
    mean_se,
)

# Honesty floor for ANY per-bucket / per-slice claim in this study (mirrors
# MIN_STRATUM_N in app/backtesting/live_evidence.py; the task freezes 50).
MIN_STRATUM_N = 50

# Bucket edges in hours (upper-exclusive); <0 is its own post-kickoff bucket.
BUCKET_EDGES: tuple[float, ...] = (0.0, 2.0, 6.0, 12.0, 24.0, 48.0)
BUCKET_LABELS: tuple[str, ...] = (
    "post-kickoff",
    "0-2h",
    "2-6h",
    "6-12h",
    "12-24h",
    "24-48h",
    "48h+",
)

# Candidate VALUE_PREMIUM_MAX_HOURS_TO_KICKOFF values the recommendation scans.
ARM_THRESHOLDS: tuple[float, ...] = (12.0, 24.0, 48.0)


# --------------------------------------------------------------------------- #
# Pure helpers (no DB, no env) — unit-tested in tests/test_mint_timing_study.py
# --------------------------------------------------------------------------- #
def bucket_hours(hours: float) -> str:
    """Bucket label for a mint-to-kickoff lead time in hours.

    Negative = minted after the stored kickoff — its own honest bucket, never
    silently folded into 0-2h."""
    if hours < 0:
        return "post-kickoff"
    for edge, label in zip(BUCKET_EDGES[1:], BUCKET_LABELS[1:-1], strict=True):
        if hours < edge:
            return label
    return BUCKET_LABELS[-1]


def effective_hours(
    hours_to_kickoff: float | None,
    created_at: datetime | None,
    starts_at: datetime | None,
) -> float | None:
    """The pick's mint-to-kickoff lead time in hours.

    Prefers the stored picks.hours_to_kickoff telemetry column (new); falls
    back to (starts_at - created_at) for rows predating it. None (skip,
    counted) when neither is derivable."""
    if hours_to_kickoff is not None:
        return float(hours_to_kickoff)
    if created_at is None or starts_at is None:
        return None
    return (starts_at - created_at).total_seconds() / 3600.0


def t_ci95(mean: float, se: float, n: int) -> tuple[float, float]:
    """Student-t 95% CI. Requires n >= 2 (SE exists)."""
    from scipy.stats import t as t_dist

    half = float(t_dist.ppf(0.975, n - 1)) * se
    return mean - half, mean + half


def bucket_stats(values: Sequence[float], floor: int = MIN_STRATUM_N) -> dict[str, Any]:
    """n / mean / ddof=1 SE / t-CI for one bucket, with HONESTY-FLOOR NULLING:
    below `floor` (or where SE is undefined) mean/se/ci are None and the
    bucket is labelled insufficient — never a small-n point estimate."""
    n = len(values)
    if n < floor:
        return {
            "n": n,
            "mean": None,
            "se": None,
            "ci95": None,
            "label": f"insufficient (n<{floor})",
        }
    mean, se = mean_se(list(values))
    if mean is None or se is None:
        return {
            "n": n,
            "mean": None,
            "se": None,
            "ci95": None,
            "label": f"insufficient (n<{floor})",
        }
    lo, hi = t_ci95(mean, se, n)
    return {"n": n, "mean": mean, "se": se, "ci95": [lo, hi], "label": "ok"}


def arm_recommendation(
    rows: Sequence[tuple[float, float]],
    thresholds: Sequence[float] = ARM_THRESHOLDS,
    floor: int = MIN_STRATUM_N,
) -> dict[str, Any]:
    """ARM/HOLD for one sport x market. `rows` = (hours_to_kickoff, clv_log).

    ARM at the smallest threshold h whose dropped slice (hours > h) has
    n >= floor and a t-CI entirely below 0. Otherwise HOLD, with the reason.
    """
    slices: list[dict[str, Any]] = []
    verdict = "HOLD"
    armed_at: float | None = None
    for h in thresholds:
        vals = [clv for hours, clv in rows if hours > h]
        st = bucket_stats(vals, floor)
        st["threshold_h"] = h
        neg = st["ci95"] is not None and st["ci95"][1] < 0.0
        st["slice_significantly_negative"] = neg
        slices.append(st)
        if neg and armed_at is None:
            armed_at = h
            verdict = f"ARM candidate: VALUE_PREMIUM_MAX_HOURS_TO_KICKOFF={h:g}"
    return {"verdict": verdict, "armed_at": armed_at, "slices": slices}


# --------------------------------------------------------------------------- #
# Row model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TimingRow:
    sport: str
    market: str
    hours: float
    clv_log: float


def build_report(rows: Sequence[TimingRow], skips: dict[str, int]) -> dict[str, Any]:
    """Assemble the bucket table + per-(sport,market) recommendation."""
    by_cell: dict[tuple[str, str, str], list[float]] = {}
    by_group: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for r in rows:
        by_cell.setdefault((r.sport, r.market, bucket_hours(r.hours)), []).append(r.clv_log)
        by_group.setdefault((r.sport, r.market), []).append((r.hours, r.clv_log))
    buckets = [
        {
            "sport": sport,
            "market": market,
            "bucket": label,
            **bucket_stats(by_cell.get((sport, market, label), [])),
        }
        for (sport, market) in sorted(by_group)
        for label in BUCKET_LABELS
    ]
    recommendations = {
        f"{sport}/{market}": arm_recommendation(pairs)
        for (sport, market), pairs in sorted(by_group.items())
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "label": EXPLORATORY_BANNER,
        "population": "settled picks in the production trusted sharp-CLV subset",
        "honesty_floor_n": MIN_STRATUM_N,
        "n_trusted": len(rows),
        "skips": skips,
        "buckets": buckets,
        "recommendations": recommendations,
    }


# --------------------------------------------------------------------------- #
# DB collection (READ-ONLY SELECTs)
# --------------------------------------------------------------------------- #
async def collect(days: int) -> tuple[list[TimingRow], dict[str, int]]:
    from sqlalchemy.ext.asyncio import create_async_engine

    where_days = "AND p.created_at >= :cutoff" if days > 0 else ""
    params: dict[str, Any] = (
        {"cutoff": datetime.now(UTC) - timedelta(days=days)} if days > 0 else {}
    )
    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as conn:
            db_rows = await _fetch_all(
                conn,
                f"""
                SELECT p.id, s.key, p.market, p.selection, p.event_id,
                       p.anchor_type, p.anchor_book, p.closing_anchor_type,
                       p.has_snapshot_close, p.close_independent_of_fill,
                       p.mint_devig_fell_back, p.close_devig_fell_back,
                       p.anchor_staleness_decision,
                       p.clv_log, p.closing_fair_probability, p.model_probability,
                       p.decimal_odds, p.created_at, e.starts_at, rt.outcome,
                       p.hours_to_kickoff
                FROM picks p
                JOIN result_tracking rt ON rt.pick_id = p.id
                JOIN events e ON e.id = p.event_id
                JOIN sports s ON s.id = e.sport_id
                {where_days}
                """,
                params,
            )
    finally:
        await engine.dispose()

    rows: list[TimingRow] = []
    skips: dict[str, int] = {}

    def skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    for r in db_rows:
        pick = SettledPick(
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
        if not is_trusted_clv_row(pick):
            skip("untrusted_clv")
            continue
        hours = effective_hours(
            float(r[20]) if r[20] is not None else None, pick.created_at, pick.starts_at
        )
        if hours is None:
            skip("no_kickoff_lead_time")
            continue
        assert pick.clv_log is not None  # is_trusted_clv_row guarantees it
        rows.append(TimingRow(pick.sport, pick.market, hours, pick.clv_log))
    return rows, skips


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(report: dict[str, Any]) -> str:
    lines = [
        report["label"],
        "MINT-TIMING STUDY — trusted CLV by hours-to-kickoff bucket (read-only;",
        "no default changed; arming VALUE_PREMIUM_MAX_HOURS_TO_KICKOFF stays an",
        "operator decision).",
        f"population: {report['population']} (n={report['n_trusted']}, "
        f"honesty floor n>={report['honesty_floor_n']})",
        f"skipped (counted, never guessed): {report['skips']}",
        "",
        f"{'sport':<18}{'market':<15}{'bucket':<14}{'n':>5} {'meanCLV':>9} {'t-CI95':>22}",
    ]
    for b in report["buckets"]:
        if b["mean"] is None:
            stat = f"{'--':>9} {b['label']:>22}"
        else:
            lo, hi = b["ci95"]
            stat = f"{b['mean']:>+9.4f} [{lo:+.4f}, {hi:+.4f}]".rjust(9)
        lines.append(f"{b['sport']:<18}{b['market']:<15}{b['bucket']:<14}{b['n']:>5d} {stat}")
    lines.append("")
    lines.append("RECOMMENDATIONS (ARM only when the dropped slice hours>h has n>=50")
    lines.append("and a t-CI entirely below 0; otherwise HOLD):")
    for key, rec in report["recommendations"].items():
        lines.append(f"  {key}: {rec['verdict']}")
        for s in rec["slices"]:
            if s["mean"] is None:
                lines.append(f"    >h={s['threshold_h']:g}: n={s['n']} {s['label']}")
            else:
                lo, hi = s["ci95"]
                flag = " <-- significantly negative" if s["slice_significantly_negative"] else ""
                lines.append(
                    f"    >h={s['threshold_h']:g}: n={s['n']} mean {s['mean']:+.4f} "
                    f"CI [{lo:+.4f}, {hi:+.4f}]{flag}"
                )
    lines += ["", report["label"], "Decision-support only — this system never places bets."]
    return "\n".join(lines)


def _dry_run_rows() -> list[TimingRow]:
    """Deterministic synthetic rows exercising bucketing, floor nulling and the
    ARM path: soccer/h2h has a >24h slice that is large and CLV-negative."""
    rows: list[TimingRow] = []
    for i in range(60):  # 0-2h bucket, mildly positive
        rows.append(TimingRow("soccer", "h2h", 1.0, 0.01 + 0.0001 * (i % 5)))
    for i in range(200):  # 12-24h bucket, positive — dilutes the >12h slice
        rows.append(TimingRow("soccer", "h2h", 18.0, 0.05 + 0.0001 * (i % 5)))
    for i in range(70):  # 24-48h bucket, clearly negative -> ARM at 24
        rows.append(TimingRow("soccer", "h2h", 30.0, -0.05 - 0.0001 * (i % 7)))
    for _i in range(10):  # tennis thin stratum -> honesty-floor nulled
        rows.append(TimingRow("tennis", "h2h", 5.0, 0.2))
    rows.append(TimingRow("soccer", "h2h", -0.5, 0.0))  # post-kickoff bucket
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=0, help="0 = all settled picks (default)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="no DB: run the full bucket/recommendation path on synthetic rows",
    )
    args = ap.parse_args(argv)

    if args.dry_run:
        rows, skips = _dry_run_rows(), {"dry_run": 0}
    else:
        rows, skips = asyncio.run(collect(args.days))
    print(render(build_report(rows, skips)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
