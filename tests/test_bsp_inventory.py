"""Tests for scripts/bsp_inventory.py — synthetic cache fixture, no network/DB."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bsp_inventory.py"


@pytest.fixture(scope="module")
def inv() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bsp_inventory", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # dataclasses' string-annotation resolution needs the module registered.
    sys.modules["bsp_inventory"] = mod
    spec.loader.exec_module(mod)
    return mod


def _record(
    market_id: str,
    kickoff: str | None,
    *,
    settled: bool = True,
    winner: bool = True,
    close: str | None = "2.5",
) -> dict:
    """A minimal BetfairMarketClose-shaped cache record (see _market_to_dict)."""
    return {
        "market_id": market_id,
        "event_type_id": "1",
        "event_name": "Home FC v Away FC",
        "competition": "Test League",
        "market_type": "MATCH_ODDS",
        "kickoff_utc": kickoff,
        "in_play_utc": None,
        "settled": settled,
        "bsp_reconciled": False,
        "runners": [
            {
                "selection_id": 101,
                "name": "Home FC",
                "sort_priority": 1,
                "status": "WINNER" if winner else "ACTIVE",
                "close_price": close,
                "bsp": None,
                "won": True if winner else None,
            },
            {
                "selection_id": 102,
                "name": "Away FC",
                "sort_priority": 2,
                "status": "LOSER" if winner else "ACTIVE",
                "close_price": close,
                "bsp": None,
                "won": False if winner else None,
            },
        ],
    }


@pytest.fixture()
def cache_path(tmp_path: Path) -> Path:
    """4 records: ok, missing kickoff, duplicate market_id, missing close+result."""
    records = [
        _record("1.001", "2025-11-27T14:30:00+00:00"),
        _record("1.002", None),  # missing kickoff_utc
        _record("1.001", "2025-11-28T18:00:00+00:00"),  # duplicate market_id
        _record("1.003", "2026-01-05T20:00:00+00:00", settled=False, winner=False, close=None),
    ]
    path = tmp_path / "soccer_match_odds.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        fh.write("not-json{{{\n")  # one parse error
    return path


def test_cache_counts_and_flags(inv: ModuleType, cache_path: Path) -> None:
    report = inv.inventory_cache(cache_path)
    assert report.rows == 4
    assert report.parse_errors == 1
    assert report.distinct_market_ids == 3
    assert report.duplicate_market_ids == 1
    assert report.duplicate_extra_rows == 1
    assert report.missing_kickoff == 1
    assert report.missing_close == 1
    assert report.missing_result == 1
    assert report.missing_runners == 0
    assert report.kickoff_min == "2025-11-27"
    assert report.kickoff_max == "2026-01-05"
    assert report.events_per_month == {"2025-11": 2, "2026-01": 1}


def test_readiness_do_not_run_without_h2(inv: ModuleType, cache_path: Path) -> None:
    report = inv.inventory_cache(cache_path)
    lines = inv.readiness_lines([report], [])
    text = "\n".join(lines)
    assert "2026-H2 months present in any input: NONE" in text
    assert "SPENT_DATA_SHA256S" in text
    assert "DO-NOT-RUN" in text
    assert "Verdict: NOT READY (do not run)" in text


def test_member_month_parsing(inv: ModuleType) -> None:
    assert inv._member_month("BASIC/2025/Nov/27/35000000/1.251018684.bz2") == "2025-11"
    assert inv._member_month("BASIC/2026/Jun/20/35732830/1.259268835.bz2") == "2026-06"
    assert inv._member_month("garbage") is None
    assert inv._member_month("BASIC/20xx/Nov/1/x.bz2") is None
