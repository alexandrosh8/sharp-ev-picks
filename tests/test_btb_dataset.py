"""BeatTheBookie -> value-candidate emitter tests (calibration breadth, part b).

Exercises build_value_dataset.btb_candidates on synthetic SeriesMatch objects:
the emitted rows must be consensus-anchored (no sharp price), carry the
consensus-close CLV label, sit outside the trainer's LEAGUES_18 universe, and
satisfy the dataset's own leakage gate. No network, no files written.
"""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pandas")

from app.ingestion.beatthebookie_series import SeriesMatch  # noqa: E402

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml" / "build_value_dataset.py"
_spec = importlib.util.spec_from_file_location("build_value_dataset", _SCRIPT)
assert _spec is not None and _spec.loader is not None
bvd: Any = importlib.util.module_from_spec(_spec)
sys.modules["build_value_dataset"] = bvd
_spec.loader.exec_module(bvd)


def _match(match_id: int, result: str = "H", month: int = 11, year: int = 2015) -> SeriesMatch:
    hs, as_ = {"H": (2, 1), "D": (1, 1), "A": (0, 2)}[result]
    return SeriesMatch(
        match_id=match_id,
        kickoff_utc=datetime(year, month, 12, 15, 0, tzinfo=UTC),
        home_score=hs,
        away_score=as_,
        result=result,
        # best > consensus by construction -> a positive-edge candidate exists
        open_consensus=(2.00, 3.50, 4.00),
        open_best=(2.20, 3.70, 4.40),
        close_consensus=(1.95, 3.55, 4.20),
        close_best=(2.05, 3.70, 4.40),
        n_books_open=11,
        n_books_close=9,
    )


def test_btb_candidates_are_consensus_anchored_no_sharp() -> None:
    cands = bvd.btb_candidates([_match(1)])
    assert cands, "expected at least one positive-edge candidate"
    for c in cands:
        assert c.anchor_type == bvd.ANCHOR_CONSENSUS
        assert c.league == "BTB"  # outside LEAGUES_18 -> never in frozen eval
        assert c.era == "maxavg"
        assert c.pinn_price is None  # no sharp book in this source
        assert c.overround_pinn is None
        assert c.price_ratio_best_pinn is None
        assert c.book_count == 11  # real per-snapshot book count, not a column proxy
        assert c.edge >= bvd.MIN_EDGE_DEFAULT
        # CLV label exists but is vs the CONSENSUS close (honest scope)
        assert c.clv_pinn is not None


def test_btb_season_code_from_kickoff() -> None:
    assert bvd._btb_season(datetime(2015, 11, 1, tzinfo=UTC)) == "1516"
    assert bvd._btb_season(datetime(2016, 6, 1, tzinfo=UTC)) == "1516"
    assert bvd._btb_season(datetime(2016, 8, 1, tzinfo=UTC)) == "1617"


def test_btb_dataframe_matches_schema_and_passes_leak_gate() -> None:
    bvd.assert_no_label_leak()  # gate must hold with the new code present
    df = bvd._to_dataframe(bvd.btb_candidates([_match(i) for i in range(5)]), schema_version=3)
    assert list(df.columns) == list(bvd.SCHEMA)
    assert (df["anchor_type"] == bvd.ANCHOR_CONSENSUS).all()
    assert df["pinn_price"].isna().all()
    # match identity is unique per game (id-derived team names)
    assert df["home_team"].nunique() == 5
