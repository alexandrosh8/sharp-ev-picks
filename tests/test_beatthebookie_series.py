"""BeatTheBookie odds_series loader — pure-parser tests (no network, no DB).

Covers the documented per-game matrix format (32 bookies x 216 cols =
72h x {HOME,DRAW,AWAY}) from generate_odds_series_csv.php, the filename
parser, per-book open/close snapshot extraction, consensus/best aggregation,
the football-data-row adapter consumed by scripts/value_backtest.py, and
bad-row skipping.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingestion.beatthebookie_series import (
    BOOKIES,
    N_BOOKS,
    N_COLS,
    N_HOURS,
    SeriesMatch,
    load_series_any,
    load_series_csv,
    load_series_dir,
    parse_match_datetime,
    parse_match_filename,
    parse_odds_matrix,
    parse_score,
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


# ---------------------------------------------------------------------------
# Kaggle CSV format: wide odds CSV + match-metadata CSV joined by match_id.
# The flattened matrix is {home,draw,away}_b{1..32}_{0..71}, BOOK-MAJOR: each
# book occupies a contiguous 216-col block laid out [72 HOME | 72 DRAW | 72
# AWAY] (so draw_b1_0 sits at offset 72, NOT after all 32 home columns), then
# the next book. nan = missing. Mirrors the real Kaggle files.
# ---------------------------------------------------------------------------

_N_CSV_COLS = 5 + N_BOOKS * N_COLS  # 6917


def _csv_idx(outcome: str, book0: int, t: int) -> int:
    """Index of cell {outcome}_b{book0+1}_{t} in a wide-CSV data row."""
    within = {"home": 0, "draw": N_HOURS, "away": 2 * N_HOURS}[outcome]
    return 5 + book0 * N_COLS + within + t


def _csv_header() -> list[str]:
    cols = ["match_id", "match_date", "match_time", "score_home", "score_away"]
    for b in range(1, N_BOOKS + 1):
        for outcome in ("home", "draw", "away"):
            for t in range(N_HOURS):
                cols.append(f"{outcome}_b{b}_{t}")
    return cols


def _sample_csv_row(match_id: int = 879672) -> list[str]:
    """Mirror _sample_matrix() exactly: book0 opens t10/closes t71, book1
    opens t5/closes t70. Same prices -> same consensus/best as the matrix
    sample test, so the two parsers are proven to agree."""
    cells = ["nan"] * _N_CSV_COLS
    cells[0] = str(match_id)
    cells[1] = "2015-09-12"
    cells[2] = "14:30:00"
    cells[3] = "9"  # ignored: result comes from the metadata join
    cells[4] = "9"

    def setc(outcome: str, book0: int, t: int, v: float) -> None:
        cells[_csv_idx(outcome, book0, t)] = f"{v:.4f}"

    # book 0
    setc("home", 0, 10, 2.00)
    setc("draw", 0, 10, 3.30)
    setc("away", 0, 10, 3.50)
    setc("home", 0, 71, 2.10)
    setc("draw", 0, 71, 3.40)
    setc("away", 0, 71, 3.60)
    # book 1
    setc("home", 1, 5, 2.20)
    setc("draw", 1, 5, 3.10)
    setc("away", 1, 5, 3.40)
    setc("home", 1, 70, 2.05)
    setc("draw", 1, 70, 3.20)
    setc("away", 1, 70, 3.70)
    return cells


_META_HEADER = "match_id, league, home_team, away_team, score, detailed_score, match_datetime"


def _write_csv(path: Path, rows: list[list[str]], gz: bool = False) -> None:
    body = ",".join(_csv_header()) + "\n"
    body += "\n".join(",".join(r) for r in rows) + "\n"
    if gz:
        path.write_bytes(gzip.compress(body.encode("utf-8")))
    else:
        path.write_text(body, encoding="utf-8")


def _write_meta(path: Path, lines: list[str], gz: bool = False) -> None:
    body = _META_HEADER + "\n" + "\n".join(lines) + "\n"
    if gz:
        path.write_bytes(gzip.compress(body.encode("utf-8")))
    else:
        path.write_text(body, encoding="utf-8")


def test_parse_score() -> None:
    assert parse_score("3:1") == (3, 1)
    assert parse_score(" 0:0 ") == (0, 0)
    assert parse_score("bad") is None
    assert parse_score("3:1:2") is None


def test_parse_match_datetime_is_utc_aware() -> None:
    dt = parse_match_datetime("2015-09-12 14:30:00")
    assert dt == datetime(2015, 9, 12, 14, 30, 0, tzinfo=UTC)
    assert dt is not None and dt.tzinfo is not None  # never naive
    assert parse_match_datetime("nonsense") is None


def test_load_series_csv_matches_matrix_parser(tmp_path: Path) -> None:
    odds = tmp_path / "odds_series.csv"
    meta = tmp_path / "odds_series_matches.csv"
    _write_csv(odds, [_sample_csv_row(879672)])
    # result + kickoff + league come from the metadata join, NOT the odds row
    _write_meta(meta, ["879672, Test: League,Home FC,Away FC,2:1,(1:0; 2:1),2015-09-12 14:30:00"])

    matches = load_series_csv(odds, meta)
    assert len(matches) == 1
    m = matches[0]
    assert isinstance(m, SeriesMatch)
    assert m.match_id == 879672
    assert m.kickoff_utc == datetime(2015, 9, 12, 14, 30, 0, tzinfo=UTC)
    assert m.result == "H"  # 2:1 from metadata join (NOT the 9:9 odds-row score)
    assert m.home_score == 2 and m.away_score == 1
    assert m.league == "Test: League"
    assert m.n_books_open == 2 and m.n_books_close == 2
    # IDENTICAL aggregation to test_parse_series_match_open_close_consensus_and_best
    assert m.open_consensus == pytest.approx((2.10, 3.20, 3.45))
    assert m.open_best == pytest.approx((2.20, 3.30, 3.50))
    assert m.close_consensus == pytest.approx((2.075, 3.30, 3.65))
    assert m.close_best == pytest.approx((2.10, 3.40, 3.70))
    # the row adapter still produces football-data columns + league context
    row = to_fd_row(m)
    assert float(row["PSH"]) == 2.10
    assert float(row["MaxCA"]) == 3.70
    assert row["FTR"] == "H"
    assert row["BtbLeague"] == "Test: League"


def test_load_series_csv_quarantines_unjoined_match(tmp_path: Path) -> None:
    odds = tmp_path / "odds_series.csv"
    meta = tmp_path / "odds_series_matches.csv"
    # two odds rows, but metadata only carries one -> the other is skipped
    _write_csv(odds, [_sample_csv_row(879672), _sample_csv_row(111111)])
    _write_meta(meta, ["879672, Test: League,Home FC,Away FC,2:1,(1:0; 2:1),2015-09-12 14:30:00"])
    matches = load_series_csv(odds, meta)
    assert [m.match_id for m in matches] == [879672]


def test_load_series_csv_gzip_transparent(tmp_path: Path) -> None:
    odds = tmp_path / "odds_series.csv.gz"
    meta = tmp_path / "odds_series_matches.csv.gz"
    _write_csv(odds, [_sample_csv_row(879672)], gz=True)
    _write_meta(
        meta,
        ["879672, Test: League,Home FC,Away FC,0:2,(0:1; 0:2),2015-09-12 14:30:00"],
        gz=True,
    )
    matches = load_series_csv(odds, meta)
    assert len(matches) == 1
    assert matches[0].result == "A"  # 0:2


def test_load_series_any_autodetects_csv_and_dir(tmp_path: Path) -> None:
    # CSV file -> derives the *_matches sibling automatically
    odds = tmp_path / "odds_series.csv"
    meta = tmp_path / "odds_series_matches.csv"
    _write_csv(odds, [_sample_csv_row(879672)])
    _write_meta(meta, ["879672, Test: League,Home FC,Away FC,1:1,(0:0; 1:1),2015-09-12 14:30:00"])
    csv_matches = load_series_any(odds)
    assert len(csv_matches) == 1
    assert csv_matches[0].result == "D"

    # directory -> matrix .txt parser
    d = tmp_path / "matrix_dir"
    d.mkdir()
    (d / FNAME).write_text(_sample_matrix())
    dir_matches = load_series_any(d)
    assert len(dir_matches) == 1
    assert dir_matches[0].match_id == 879672
