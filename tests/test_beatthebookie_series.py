"""BeatTheBookie odds_series loader — pure-parser tests (no network, no DB).

Covers the documented per-game matrix format (32 bookies x 216 cols =
72h x {HOME,DRAW,AWAY}) from generate_odds_series_csv.php, the filename
parser, per-book open/close snapshot extraction, consensus/best aggregation,
the football-data-row adapter consumed by scripts/value_backtest.py, and
bad-row skipping.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingestion.beatthebookie_series import (
    BOOKIES,
    N_COLS,
    SeriesMatch,
    load_series_dir,
    parse_match_filename,
    parse_odds_matrix,
    parse_series_match,
    to_fd_row,
)


def _row(values: dict[int, float]) -> str:
    """One bookie line: 216 comma-separated cells, 'nan' unless set."""
    cells = ["nan"] * N_COLS
    for col, v in values.items():
        cells[col] = f"{v:.4f}"
    return ",".join(cells)


def _home(col: int) -> int:
    return col  # cols 0..71


def _draw(col: int) -> int:
    return 72 + col  # cols 72..143


def _away(col: int) -> int:
    return 144 + col  # cols 144..215


def _matrix(book_rows: dict[int, str], n_books: int = 32) -> str:
    blank = ",".join(["nan"] * N_COLS)
    lines = [book_rows.get(i, blank) for i in range(n_books)]
    return "\n".join(lines) + "\n"


# A two-book matrix: book 0 opens at col 10, closes at col 71; book 1 opens at
# col 5, closes at col 70. Everything else nan.
def _sample_matrix() -> str:
    book0 = _row(
        {
            _home(10): 2.00,
            _draw(10): 3.30,
            _away(10): 3.50,
            _home(71): 2.10,
            _draw(71): 3.40,
            _away(71): 3.60,
        }
    )
    book1 = _row(
        {
            _home(5): 2.20,
            _draw(5): 3.10,
            _away(5): 3.40,
            _home(70): 2.05,
            _draw(70): 3.20,
            _away(70): 3.70,
        }
    )
    return _matrix({0: book0, 1: book1})


FNAME = "match_879672_2015_09_12_14_30_00_2_1.txt"


def test_bookie_roster_is_32_pinnacle_row_9() -> None:
    assert len(BOOKIES) == 32
    assert BOOKIES[9] == "Pinnacle Sports"


def test_parse_match_filename() -> None:
    parsed = parse_match_filename(FNAME)
    assert parsed is not None
    match_id, kickoff, hs, as_ = parsed
    assert match_id == 879672
    assert kickoff == datetime(2015, 9, 12, 14, 30, 0, tzinfo=UTC)
    assert kickoff.tzinfo is not None  # UTC-aware, never naive
    assert hs == 2 and as_ == 1


def test_parse_match_filename_rejects_garbage() -> None:
    assert parse_match_filename("not_a_match.txt") is None
    assert parse_match_filename("match_1_2015_09_12_14_30_00.txt") is None  # no scores


def test_parse_odds_matrix_shape() -> None:
    matrix = parse_odds_matrix(_sample_matrix())
    assert len(matrix) == 32
    assert all(len(r) == N_COLS for r in matrix)
    assert matrix[0][_home(10)] == 2.00
    assert matrix[0][_home(0)] is None  # nan -> None
    assert matrix[31][_home(10)] is None  # untouched book


def test_parse_series_match_open_close_consensus_and_best() -> None:
    m = parse_series_match(FNAME, _sample_matrix())
    assert m is not None
    assert isinstance(m, SeriesMatch)
    assert m.match_id == 879672
    assert m.result == "H"  # 2-1
    assert m.n_books_open == 2 and m.n_books_close == 2
    # opening: book0 @ col10 (2.00,3.30,3.50), book1 @ col5 (2.20,3.10,3.40)
    assert m.open_consensus == pytest.approx((2.10, 3.20, 3.45))  # means
    assert m.open_best == pytest.approx((2.20, 3.30, 3.50))  # maxima
    # closing: book0 @ col71 (2.10,3.40,3.60), book1 @ col70 (2.05,3.20,3.70)
    assert m.close_consensus == pytest.approx((2.075, 3.30, 3.65))
    assert m.close_best == pytest.approx((2.10, 3.40, 3.70))


def test_result_draw_and_away() -> None:
    draw = parse_series_match("match_1_2016_05_01_18_00_00_1_1.txt", _sample_matrix())
    away = parse_series_match("match_2_2016_05_01_18_00_00_0_3.txt", _sample_matrix())
    assert draw is not None and draw.result == "D"
    assert away is not None and away.result == "A"


def test_skip_match_with_no_coherent_book() -> None:
    # a book with only HOME priced (no draw/away) cannot form a 1x2 triple
    only_home = _matrix({0: _row({_home(10): 2.0, _home(71): 2.1})})
    assert parse_series_match(FNAME, only_home) is None


def test_skip_bad_filename() -> None:
    assert parse_series_match("garbage.txt", _sample_matrix()) is None


def test_to_fd_row_maps_to_football_data_columns() -> None:
    m = parse_series_match(FNAME, _sample_matrix())
    assert m is not None
    row = to_fd_row(m)
    # consensus open -> Pinnacle-pre-match slots (honest: consensus, not sharp)
    assert float(row["PSH"]) == 2.10
    assert float(row["PSD"]) == 3.20
    assert float(row["PSA"]) == 3.45
    # best open -> Max pre-match
    assert float(row["MaxH"]) == 2.20
    # consensus close -> Pinnacle close slots
    assert float(row["PSCH"]) == 2.075
    # best close -> Max close
    assert float(row["MaxCA"]) == 3.70
    assert row["FTR"] == "H"
    assert row["FTHG"] == "2" and row["FTAG"] == "1"


def test_load_series_dir(tmp_path: Path) -> None:
    (tmp_path / FNAME).write_text(_sample_matrix())
    (tmp_path / "match_2_2016_05_01_18_00_00_1_1.txt").write_text(_sample_matrix())
    (tmp_path / "README.txt").write_text("not a match file")  # ignored
    matches = load_series_dir(tmp_path)
    assert len(matches) == 2
    assert {mm.match_id for mm in matches} == {879672, 2}
    # deterministic order: by kickoff then id
    assert [mm.match_id for mm in matches] == [879672, 2]
