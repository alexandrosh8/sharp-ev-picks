#!/usr/bin/env python
"""LOCKED scorer for run tag `autoresearch/2026-07-07-evidence-flow`.

Emits ONE number to stdout: ``evidence_flow_score`` (higher is better) — a
deterministic, network-free replay of the OddsChecker parse layer over the frozen
corpus in `research/corpus.py`. Breakdown on stderr. DO NOT EDIT during a run.

  evidence_flow_score =
      1000 * fresh_snapshot_coverage          # bookmaker-AGNOSTIC 4-tuple recall (guard)
    +  500 * independent_sharp_close_coverage # anchor-event has a sharp (Betfair) price (guard)
    +  800 * canonical_bookmaker_coverage     # non-raw-code books  [HEADROOM asset C]
    +  250 * strict_cross_source_match_rate   # events registered with correct home/away (guard)
    -  600 * stale_drop_ratio                 # forbidden / wrong-event emissions (guard)
    -  400 * unknown_timestamp_ratio          # captured_at == ingestion wall-clock (guard)
    -  800 * duplicate_bookmaker_ratio        # raw 2-letter codes  [HEADROOM asset C]
    - 5000 * wrong_game_count                 # orientation-swapped registration (guard)
    - 5000 * circular_close_count             # echo/circular close (guard; none in corpus)
    - 100000 * (safety_audit_fail | gate_tests_fail)   # hard fails (safety#10 bans app->research)

Only the two bookmaker terms have honest headroom (asset group C — the all-odds /
legacy paths pass empty bookmaker entities so off-map books emit raw codes).
Everything else is a regression guard, at max on the corpus. A raw code is
``len(name) == 2 and name.isupper()`` (e.g. "SM"); resolved names ("Smarkets")
are canonical. Recall is bookmaker-agnostic so the bookmaker fix cannot inflate it.
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

W_FRESH, W_SHARP, W_BOOK, W_MATCH = 1000.0, 500.0, 800.0, 250.0
P_STALE, P_TS, P_DUP, P_WRONG, P_CIRC = 600.0, 400.0, 800.0, 5000.0, 5000.0
P_HARD = 100000.0
SHARP_NAMES = {"betfair exchange", "pinnacle", "pinnacle sports"}
GATE_TESTS = (
    "tests/test_oddschecker.py",
    "tests/test_wrong_game_audit.py",
    "tests/test_resolution.py",
)


def _is_raw_code(name: str) -> bool:
    return len(name) == 2 and name.isupper()


def _4t(event_id: str, market: str, selection: str, detail: str | None) -> tuple:
    return (event_id, market, selection, detail)


def _replay(fx: dict, now: datetime):
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
            raise ValueError(ep)
    except Exception as exc:  # noqa: BLE001
        return None, directory, exc
    return snaps, directory, None


def _run(cmd: list[str]) -> int:
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True).returncode


def compute() -> tuple[float, dict]:
    now = datetime.fromisoformat(NOW)
    total_cand = good = bad = 0
    n_books = n_canonical = n_raw = 0
    anchor_ok = anchor_tot = 0
    matched = events_tot = 0
    unknown_ts = emitted_tot = 0
    wrong = crash = 0

    for fx in build_corpus():
        exp = fx["expect"]
        cand = {
            _4t(c["event_id"], c["market"], c["selection"], c["market_detail"])
            for c in exp["candidates"]
        }
        total_cand += len(cand)
        snaps, directory, exc = _replay(fx, now)
        if exc is not None:
            crash += 1
            events_tot += len(exp["events"])
            anchor_tot += len(exp["anchor_events"])
            continue

        allowed = {e["event_id"] for e in exp["events"]}
        forb = {
            _4t(c["event_id"], c["market"], c["selection"], c["market_detail"])
            for c in exp.get("forbidden", [])
        }
        emitted_4t = {_4t(s.event_id, s.market.value, s.selection, s.market_detail) for s in snaps}
        good += len(emitted_4t & cand)
        bad += len((emitted_4t & forb) | {k for k in emitted_4t if k[0] not in allowed})

        for s in snaps:
            emitted_tot += 1
            n_books += 1
            if _is_raw_code(s.bookmaker):
                n_raw += 1
            else:
                n_canonical += 1
            if s.captured_at == now:
                unknown_ts += 1

        for ae in exp["anchor_events"]:
            anchor_tot += 1
            has_sharp = any(
                s.event_id == ae["event_id"] and s.bookmaker.lower() in SHARP_NAMES for s in snaps
            )
            if has_sharp == ae["should_be_anchored"]:
                anchor_ok += 1

        for e in exp["events"]:
            events_tot += 1
            got = directory.lookup(e["event_id"])
            if got is None or not got.home or not got.away:
                continue
            if (got.home, got.away) == (e["home"], e["away"]):
                matched += 1
            elif (got.home, got.away) == (e["away"], e["home"]):
                wrong += 1

    fresh = good / total_cand if total_cand else 1.0
    sharp = anchor_ok / anchor_tot if anchor_tot else 1.0
    canonical = n_canonical / n_books if n_books else 1.0
    match_rate = matched / events_tot if events_tot else 1.0
    stale = bad / max(1, good + bad)
    unknown = unknown_ts / emitted_tot if emitted_tot else 0.0
    dup = n_raw / n_books if n_books else 0.0
    circ = 0  # no echo-close fixtures in this corpus

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
        + W_SHARP * sharp
        + W_BOOK * canonical
        + W_MATCH * match_rate
        - P_STALE * stale
        - P_TS * unknown
        - P_DUP * dup
        - P_WRONG * wrong
        - P_CIRC * circ
        - P_HARD * safety_fail
        - P_HARD * gate_fail
        + crash * -P_WRONG
    )
    bd = {
        "fresh_snapshot_coverage": round(fresh, 4),
        "independent_sharp_close_coverage": round(sharp, 4),
        "canonical_bookmaker_coverage": round(canonical, 4),
        "strict_cross_source_match_rate": round(match_rate, 4),
        "stale_drop_ratio": round(stale, 4),
        "unknown_timestamp_ratio": round(unknown, 4),
        "duplicate_bookmaker_ratio": round(dup, 4),
        "wrong_game_count": wrong,
        "circular_close_count": circ,
        "crash_count": crash,
        "safety_audit_fail": safety_fail,
        "gate_tests_fail": gate_fail,
        "n_emitted": emitted_tot,
        "n_raw_code_books": n_raw,
    }
    return score, bd


def main() -> None:
    score, bd = compute()
    for k, v in bd.items():
        print(f"  {k}: {v}", file=sys.stderr)
    print(f"{score:.4f}")


if __name__ == "__main__":
    main()
