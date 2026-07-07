#!/usr/bin/env python
"""Read-only live-DB cycle-health MONITOR snapshot (context only, NOT the score).

Wraps the SELECT-only `scripts/research/sport_quality_report.py` and emits a
compact per-sport slice (coverage / matched / sharp-anchor share / trusted CLV)
as JSON on stdout. Recorded ALONGSIDE each kept experiment for visibility; a
parser edit cannot move live production state without a fresh scrape, so this is
monitoring, never part of `cycle_health_score`.

Usage:
  uv run python research/db_context.py                 # run the report (--days 2)
  uv run python research/db_context.py --days 1
  uv run python research/db_context.py --from-json PATH # reuse an existing report JSON

Never prints the DB URL or any credential (the wrapped tool reads .env itself).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "scripts" / "research" / "sport_quality_report.py"


def compact(report: dict) -> dict:
    """Reduce a full sport_quality_report JSON to per-sport scalar health."""
    out: dict = {
        "generated_at": report.get("generated_at"),
        "coverage_window_days": report.get("coverage_window_days"),
        "sports": {},
    }
    for sport, block in (report.get("sports") or {}).items():
        cov = block.get("coverage") or {}
        clv = (block.get("trusted_clv") or {}).get("trusted") or {}
        out["sports"][sport] = {
            "events": cov.get("events"),
            "matched_events": cov.get("matched_events"),
            "matched_rate": cov.get("matched_rate"),
            "sharp_anchored_share": cov.get("sharp_anchored_share"),
            "trusted_clv_n": clv.get("n"),
            "trusted_clv_mean": clv.get("mean"),
        }
    return out


def _run_report(days: int) -> dict:
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tmp:
        path = tmp.name
    subprocess.run(
        [sys.executable, str(REPORT), "--days", str(days), "--json-out", path],
        cwd=str(REPO),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(Path(path).read_text())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--from-json", type=str, default=None)
    args = ap.parse_args()
    report = (
        json.loads(Path(args.from_json).read_text()) if args.from_json else _run_report(args.days)
    )
    print(json.dumps(compact(report), separators=(",", ":")))


if __name__ == "__main__":
    main()
