#!/usr/bin/env python
"""READ-ONLY inventory of the Betfair BSP historical archives + parsed caches.

MECHANICAL inventory only (dates, counts, fields, parse health). This tool
NEVER runs a backtest, NEVER computes CLV/ROI/edge, and must never be extended
to — every tar under ``data/betfair/bsp/`` is registered in
``app/backtesting/arcadia_anchor.py::SPENT_DATA_SHA256S`` (ADR-0019): the
slates were consumed by the 2026-07-02 single-shot and any further strategy
selection or evaluation over them is re-tuning on a spent holdout.

What it reports:
* per parsed cache (``*.jsonl.gz``, written by
  ``app.ingestion.betfair_bsp.write_market_cache``): rows, parse errors,
  distinct/duplicate market_ids, kickoff_utc date range, events per month, and
  rows missing key fields (kickoff, runners, close prices, settled result);
* per ``.tar`` archive: filename, size, sha256 (ONLY with ``--hash`` — the
  archives are multi-GB), member count and per-month member counts from the
  ``BASIC/YYYY/Mon/DD/...`` member paths — listing only, NEVER extracting;
* a VALIDATION-READINESS section: whether any 2026-H2 (Jul-Dec) month is
  present in ANY input, plus explicit DO-NOT-RUN reasons.

Usage: ``uv run python scripts/bsp_inventory.py [--bsp-dir DIR] [--hash]``
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import tarfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.ingestion.betfair_bsp import (  # noqa: E402  (path bootstrap above)
    BetfairMarketClose,
    _market_from_dict,
)

# 2026-H2 — the ONLY eligible ARCADIA validation window (arcadia_anchor.py
# ELIGIBLE_WINDOW_START/END = 2026-07-01 .. 2026-12-31).
H2_2026_MONTHS: tuple[str, ...] = tuple(f"2026-{m:02d}" for m in range(7, 13))

# Kickoffs outside this sanity band are counted as implausible (e.g. the
# epoch-garbage "1970-01-21" marketTime seen in the wild).
_PLAUSIBLE_YEARS = range(2000, 2031)

_MONTH_NUM = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

# Stop listing a tar after this many seconds and report it as SAMPLED.
TAR_TIME_BUDGET_S = 300.0
TAR_SAMPLE_MEMBERS = 200_000


@dataclass
class CacheReport:
    """Mechanical health counters for one parsed jsonl.gz market cache."""

    path: Path
    rows: int = 0
    parse_errors: int = 0
    distinct_market_ids: int = 0
    duplicate_market_ids: int = 0  # ids appearing more than once
    duplicate_extra_rows: int = 0  # rows beyond the first per duplicated id
    kickoff_min: str | None = None
    kickoff_max: str | None = None
    missing_kickoff: int = 0
    implausible_kickoff: int = 0
    missing_runners: int = 0
    missing_close: int = 0  # no runner carries a close_price
    missing_result: int = 0  # not settled, or settled without a WINNER runner
    events_per_month: Counter[str] = field(default_factory=Counter)
    market_types: Counter[str] = field(default_factory=Counter)
    event_type_ids: Counter[str] = field(default_factory=Counter)


def _tally_market(report: CacheReport, market: BetfairMarketClose) -> None:
    report.rows += 1
    ko = market.kickoff_utc
    if ko is None:
        report.missing_kickoff += 1
    elif ko.year not in _PLAUSIBLE_YEARS:
        report.implausible_kickoff += 1
    else:
        month = f"{ko.year:04d}-{ko.month:02d}"
        report.events_per_month[month] += 1
        iso = ko.date().isoformat()
        if report.kickoff_min is None or iso < report.kickoff_min:
            report.kickoff_min = iso
        if report.kickoff_max is None or iso > report.kickoff_max:
            report.kickoff_max = iso
    if not market.runners:
        report.missing_runners += 1
    if not any(r.close_price is not None for r in market.runners):
        report.missing_close += 1
    if not market.settled or not any(r.won is True for r in market.runners):
        report.missing_result += 1
    report.market_types[market.market_type or "?"] += 1
    report.event_type_ids[market.event_type_id or "?"] += 1


def inventory_cache(path: Path) -> CacheReport:
    """Read one gzip JSONL cache (read-only) and return its health counters."""
    report = CacheReport(path=path)
    ids: Counter[str] = Counter()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                market = _market_from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                report.parse_errors += 1
                continue
            _tally_market(report, market)
            if market.market_id:
                ids[market.market_id] += 1
    report.distinct_market_ids = len(ids)
    dupes = {mid: n for mid, n in ids.items() if n > 1}
    report.duplicate_market_ids = len(dupes)
    report.duplicate_extra_rows = sum(n - 1 for n in dupes.values())
    return report


@dataclass
class TarReport:
    """Member-listing inventory for one Betfair Basic .tar (never extracted)."""

    path: Path
    size_bytes: int = 0
    sha256: str | None = None
    member_count: int = 0
    members_per_month: Counter[str] = field(default_factory=Counter)
    unparsed_paths: int = 0
    sampled: bool = False
    elapsed_s: float = 0.0
    is_symlink: bool = False
    error: str | None = None


def _member_month(name: str) -> str | None:
    """``BASIC/YYYY/Mon/DD/EVENT/MARKET.bz2`` -> ``YYYY-MM`` (or None)."""
    parts = name.split("/")
    if len(parts) < 3:
        return None
    year, mon = parts[1], _MONTH_NUM.get(parts[2])
    if mon is None or not (len(year) == 4 and year.isdigit()):
        return None
    return f"{year}-{mon:02d}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_tar(
    path: Path,
    *,
    with_hash: bool = False,
    time_budget_s: float = TAR_TIME_BUDGET_S,
    sample_members: int = TAR_SAMPLE_MEMBERS,
) -> TarReport:
    """List one tar's members (streaming, header-only — NO extraction)."""
    report = TarReport(path=path, is_symlink=path.is_symlink())
    report.size_bytes = path.stat().st_size
    if with_hash:
        report.sha256 = _sha256_file(path)
    start = time.monotonic()
    try:
        with tarfile.open(path, mode="r|") as tar:
            for member in tar:
                report.member_count += 1
                month = _member_month(member.name)
                if month is None:
                    report.unparsed_paths += 1
                else:
                    report.members_per_month[month] += 1
                if time.monotonic() - start > time_budget_s and (
                    report.member_count >= sample_members
                ):
                    report.sampled = True
                    break
    except (tarfile.TarError, OSError, EOFError) as exc:
        # e.g. the documented truncated tail members — record, keep going.
        report.error = type(exc).__name__
    report.elapsed_s = time.monotonic() - start
    return report


def readiness_lines(cache_reports: list[CacheReport], tar_reports: list[TarReport]) -> list[str]:
    """VALIDATION-READINESS verdict + explicit DO-NOT-RUN reasons."""
    months_seen: Counter[str] = Counter()
    for c in cache_reports:
        months_seen.update(c.events_per_month)
    for t in tar_reports:
        months_seen.update(t.members_per_month)
    h2_present = sorted(m for m in H2_2026_MONTHS if m in months_seen)
    lines = ["", "=== VALIDATION-READINESS (2026-H2: Jul-Dec 2026) ==="]
    if h2_present:
        detail = ", ".join(f"{m}={months_seen[m]}" for m in h2_present)
        lines.append(
            f"2026-H2 months present in at least one input (rows/members): {detail} "
            "— a handful of rows means stray mis-dated markets, NOT real coverage."
        )
    else:
        lines.append("2026-H2 months present in any input: NONE")
    covered = sorted(months_seen)
    if covered:
        lines.append(
            f"Months covered overall: {covered[0]} .. {covered[-1]} ({len(covered)} months)"
        )
    lines.append("")
    lines.append("DO-NOT-RUN — these inputs must NOT feed any backtest/strategy evaluation:")
    lines.append(
        "  1. SPENT DATA: every tar here is registered in app/backtesting/"
        "arcadia_anchor.py::SPENT_DATA_SHA256S — the ARCADIA preflight REFUSES "
        "runs whose input sha256 matches (slate already consumed by the "
        "2026-07-02 single-shot; re-use = re-tuning on a spent holdout, ADR-0019)."
    )
    lines.append(
        "  2. WINDOW INELIGIBLE: the only eligible validation window is "
        "2026-07-01..2026-12-31 (arcadia_anchor ELIGIBLE_WINDOW_START/END); "
        + (
            "2026-H2 data present — but reason 1 still forbids these tars."
            if h2_present
            else "NO 2026-H2 month exists in any input, so nothing here can "
            "satisfy the window even mechanically."
        )
    )
    lines.append(
        "  3. SCOPE: this script is mechanical inventory only — it computes no "
        "CLV, ROI, EV or edge, and must not be extended to do so."
    )
    verdict = (
        "NOT READY (do not run)" if not h2_present else "H2 DATA PRESENT — still spent, do not run"
    )
    lines.append(f"Verdict: {verdict}")
    return lines


def format_cache_report(report: CacheReport) -> list[str]:
    lines = [
        f"--- cache: {report.path.name} ---",
        f"rows: {report.rows}   parse_errors: {report.parse_errors}",
        f"distinct market_ids: {report.distinct_market_ids}   "
        f"duplicated ids: {report.duplicate_market_ids} "
        f"(+{report.duplicate_extra_rows} extra rows)",
        f"kickoff_utc range: {report.kickoff_min} .. {report.kickoff_max}",
        f"missing: kickoff={report.missing_kickoff} "
        f"implausible_kickoff={report.implausible_kickoff} "
        f"runners={report.missing_runners} close_prices={report.missing_close} "
        f"settled_result={report.missing_result}",
        f"market_types: {dict(report.market_types.most_common())}",
        f"event_type_ids: {dict(report.event_type_ids.most_common())}",
        "events per month: "
        + ", ".join(f"{m}={n}" for m, n in sorted(report.events_per_month.items())),
    ]
    return lines


def format_tar_report(report: TarReport) -> list[str]:
    size_gb = report.size_bytes / 1e9
    lines = [
        f"--- tar: {report.path.name}" + (" (symlink)" if report.is_symlink else "") + " ---",
        f"size: {report.size_bytes} bytes ({size_gb:.2f} GB)   "
        f"sha256: {report.sha256 or '(skipped — pass --hash)'}",
        f"members listed: {report.member_count}"
        + (" (SAMPLED — time budget hit, counts are partial)" if report.sampled else "")
        + f"   unparsed paths: {report.unparsed_paths}   "
        f"listed in {report.elapsed_s:.0f}s"
        + (f"   read error: {report.error}" if report.error else ""),
        "members per month: "
        + ", ".join(f"{m}={n}" for m, n in sorted(report.members_per_month.items())),
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bsp-dir",
        type=Path,
        default=Path("data/betfair/bsp"),
        help="Directory holding the BSP tars and jsonl.gz caches",
    )
    parser.add_argument(
        "--hash",
        action="store_true",
        help="Also compute sha256 of each tar (slow — multi-GB reads)",
    )
    parser.add_argument(
        "--skip-tars",
        action="store_true",
        help="Only inventory the parsed caches (skip tar member listing)",
    )
    args = parser.parse_args(argv)
    bsp_dir: Path = args.bsp_dir
    if not bsp_dir.is_dir():
        print(f"bsp dir not found: {bsp_dir}", file=sys.stderr)
        return 2

    print(f"=== BSP DATA INVENTORY (read-only) — {bsp_dir} ===")

    cache_reports: list[CacheReport] = []
    for cache_path in sorted(bsp_dir.glob("*.jsonl.gz")):
        report = inventory_cache(cache_path)
        cache_reports.append(report)
        print()
        print("\n".join(format_cache_report(report)))

    tar_reports: list[TarReport] = []
    if not args.skip_tars:
        seen_real: set[Path] = set()
        for tar_path in sorted(bsp_dir.rglob("*.tar")):
            real = tar_path.resolve()
            if real in seen_real:
                print(
                    f"\n--- tar: {tar_path.name} — symlink to already-listed "
                    f"{real.name}, skipped ---"
                )
                continue
            seen_real.add(real)
            tar_report = inventory_tar(tar_path, with_hash=args.hash)
            tar_reports.append(tar_report)
            print()
            print("\n".join(format_tar_report(tar_report)))

    print("\n".join(readiness_lines(cache_reports, tar_reports)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
