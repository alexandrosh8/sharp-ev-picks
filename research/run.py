#!/usr/bin/env python
"""Experiment runner/logger for the capture-freshness autoresearch run.

Runs the LOCKED scorer (`research/score.py::compute`), stamps the current commit
+ editable-asset hash, appends one row to the untracked `research/results.tsv`
ledger, and prints the score to stdout. Harness utility — NOT the scorer; it
never changes the definition of "better".

  uv run python research/run.py --iter 1 --status attempt --desc "SUB-1 drop expired"
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from research.score import compute  # noqa: E402

RESULTS = REPO / "research" / "results.tsv"
ASSET = REPO / "app" / "ingestion" / "oddschecker.py"
COLUMNS = (
    "iter",
    "utc",
    "commit",
    "asset_sha12",
    "score",
    "fresh_mintable",
    "sharp_anchor",
    "matched",
    "stale_drop",
    "swap",
    "crash",
    "safety_fail",
    "gate_fail",
    "status",
    "description",
)


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "nocommit"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iter", type=int, required=True)
    ap.add_argument("--status", required=True, help="baseline|attempt|keep|revert")
    ap.add_argument("--desc", required=True)
    args = ap.parse_args()

    score, bd = compute()
    asset_sha = hashlib.sha256(ASSET.read_bytes()).hexdigest()[:12]
    row = {
        "iter": args.iter,
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _git_head(),
        "asset_sha12": asset_sha,
        "score": f"{score:.4f}",
        "fresh_mintable": bd["fresh_mintable_candidate_rate"],
        "sharp_anchor": bd["sharp_anchor_score"],
        "matched": bd["matched_event_rate"],
        "stale_drop": bd["stale_drop_ratio"],
        "swap": bd["swap_count"],
        "crash": bd["crash_count"],
        "safety_fail": bd["safety_audit_fails"],
        "gate_fail": bd["gate_tests_fail"],
        "status": args.status,
        "description": args.desc,
    }
    if not RESULTS.exists():
        RESULTS.write_text("\t".join(COLUMNS) + "\n")
    with RESULTS.open("a") as fh:
        fh.write("\t".join(str(row[c]) for c in COLUMNS) + "\n")

    for key, value in bd.items():
        print(f"  {key}: {value}", file=sys.stderr)
    print(f"{score:.4f}")


if __name__ == "__main__":
    main()
