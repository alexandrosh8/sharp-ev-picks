#!/usr/bin/env python
"""LOCKED scorer for run tag `autoresearch/2026-07-07-capture-freshness`.

Emits EXACTLY ONE number to stdout: ``cycle_health_score`` (higher is better),
a comprehensive, deterministic measure of the OddsChecker parse layer's
capture health, replayed over the frozen corpus in ``research/corpus.py`` with a
fixed ``now`` (no network, read-only, repeatable). A component breakdown is
written to STDERR (for the results log); only the final number is on STDOUT.

DO NOT EDIT during a run. This file + ``research/corpus.py`` define "better";
the run doctrine forbids changing that mid-run. Only
``app/ingestion/oddschecker.py`` is editable.

    cycle_health_score =
          1000 * fresh_mintable_candidate_rate   # correct live snapshots emitted (recall vs GT)
        +  500 * sharp_anchor_score              # OTHER-capture anchor decision correctness
        +  250 * matched_event_rate              # events registered with correct home/away
        - 1000 * stale_drop_ratio                # share of relevant emissions stale / wrong-game
        - 5000 * swap_count                      # orientation-swapped registration (wrong game)
        - 5000 * crash_count                     # parser crashed on a handleable payload
        - 10000 * safety_audit_fails             # scripts/safety_audit.sh non-zero
        - 10000 * gate_tests_fail                # parser/wrong-game/matcher contract regression

Anti-Goodhart properties (see research/program.md):
  * recall (fresh_mintable) is SET intersection vs objective GT -> emitting
    garbage or duplicates cannot inflate it; dropping everything tanks it.
  * stale_drop counts forbidden odds + wrong-game (spurious event-id) emissions,
    so "keep everything" is punished, not rewarded.
  * orientation swaps and crashes are catastrophic.
  * safety_audit + real contract tests are hard-gated into the number, so no
    unsafe or regressing edit can ever score higher.
Overfitting guard is procedural: kept diffs must be GENERAL parser fixes (the
gate runs the real test suite), never corpus-specific special-casing.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.ingestion.base import EventDirectory  # noqa: E402
from app.ingestion.oddschecker import (  # noqa: E402
    parse_legacy_match_page,
    parse_market_api_payloads,
    parse_match_page,
)
from research.corpus import NOW, build_corpus  # noqa: E402

# Weights mirror the run's scoring rule (research/program.md). Frozen.
W_FRESH = 1000.0
W_ANCHOR = 500.0
W_MATCHED = 250.0
W_STALE = 1000.0
P_SWAP = 5000.0
P_CRASH = 5000.0
P_SAFETY = 10000.0
P_GATE = 10000.0

# Hard-gate contract tests: parser + wrong-game safety + matcher. No DB, ~6s.
GATE_TESTS = (
    "tests/test_oddschecker.py",
    "tests/test_wrong_game_audit.py",
    "tests/test_resolution.py",
)


def _snap_key(s: object) -> tuple:
    return (s.event_id, s.market.value, s.selection, s.market_detail, s.bookmaker)


def _gt_key(c: dict) -> tuple:
    return (c["event_id"], c["market"], c["selection"], c["market_detail"], c["bookmaker"])


def _replay(fx: dict, now: datetime):
    """Return (emitted_key_set, directory, exception_or_None)."""
    directory = EventDirectory()
    ep = fx["entrypoint"]
    try:
        if ep == "bestodds":
            snaps = parse_match_page(fx["input"], url=fx["url"], directory=directory, now=now)
        elif ep == "api":
            snaps = parse_market_api_payloads(
                fx["input"], url=fx["url"], directory=directory, now=now, capture_other=True
            )
        elif ep == "legacy":
            snaps = parse_legacy_match_page(
                fx["input"], url=fx["url"], directory=directory, now=now
            )
        else:
            raise ValueError(f"unknown entrypoint {ep!r}")
    except Exception as exc:  # noqa: BLE001 - the parser's error surface is what we measure
        return None, directory, exc
    return {_snap_key(s) for s in snaps}, directory, None


def _run(cmd: list[str]) -> int:
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
    return proc.returncode


def compute() -> tuple[float, dict]:
    now = datetime.fromisoformat(NOW)
    total_good = total_cand = total_bad = 0
    anchor_correct = anchor_total = 0
    matched = events_total = 0
    swap = crash = 0

    for fx in build_corpus():
        exp = fx["expect"]
        cand = {_gt_key(c) for c in exp["candidates"]}
        total_cand += len(cand)
        emitted, directory, exc = _replay(fx, now)

        if exc is not None:
            if not exp.get("may_raise"):
                crash += 1
            events_total += len(exp["events"])
            anchor_total += len(exp["anchor_events"])
            continue

        forb = {_gt_key(c) for c in exp["forbidden"]}
        allowed = set(exp.get("allowed_event_ids") or [e["event_id"] for e in exp["events"]])
        good = emitted & cand
        bad = (emitted & forb) | {k for k in emitted if k[0] not in allowed}
        total_good += len(good)
        total_bad += len(bad)

        for ae in exp["anchor_events"]:
            anchor_total += 1
            parser_anchored = any(k[0] == ae["event_id"] and k[1] == "other" for k in emitted)
            if parser_anchored == ae["should_be_anchored"]:
                anchor_correct += 1

        for e in exp["events"]:
            events_total += 1
            got = directory.lookup(e["event_id"])
            if got is None or not got.home or not got.away:
                continue
            if (got.home, got.away) == (e["home"], e["away"]):
                matched += 1
            elif (got.home, got.away) == (e["away"], e["home"]):
                swap += 1  # plausible-looking wrong game — catastrophic

    fresh = total_good / total_cand if total_cand else 1.0
    stale = total_bad / max(1, total_good + total_bad)
    anchor = anchor_correct / anchor_total if anchor_total else 1.0
    matched_rate = matched / events_total if events_total else 1.0

    safety_fail = 1 if _run(["bash", "scripts/safety_audit.sh"]) != 0 else 0
    gate_fail = (
        1
        if _run(
            [
                sys.executable,
                "-m",
                "pytest",
                *GATE_TESTS,
                "-q",
                "-p",
                "no:cacheprovider",
                "--no-header",
            ]
        )
        != 0
        else 0
    )

    score = (
        W_FRESH * fresh
        + W_ANCHOR * anchor
        + W_MATCHED * matched_rate
        - W_STALE * stale
        - P_SWAP * swap
        - P_CRASH * crash
        - P_SAFETY * safety_fail
        - P_GATE * gate_fail
    )
    breakdown = {
        "fresh_mintable_candidate_rate": round(fresh, 4),
        "sharp_anchor_score": round(anchor, 4),
        "matched_event_rate": round(matched_rate, 4),
        "stale_drop_ratio": round(stale, 4),
        "swap_count": swap,
        "crash_count": crash,
        "safety_audit_fails": safety_fail,
        "gate_tests_fail": gate_fail,
        "n_good": total_good,
        "n_candidates": total_cand,
        "n_bad": total_bad,
        "n_events": events_total,
        "n_anchor_events": anchor_total,
    }
    return score, breakdown


def main() -> None:
    score, breakdown = compute()
    for key, value in breakdown.items():
        print(f"  {key}: {value}", file=sys.stderr)
    print(f"{score:.4f}")


if __name__ == "__main__":
    main()
