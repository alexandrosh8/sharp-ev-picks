"""Tests for the tennis-data.co.uk pre-match loader (app/ingestion/tennis_data).

Synthetic data only — no network. Validates the documented column schema
(verified against the ATP 2023 workbook): a sample row maps to the right
players/odds/result/date; odds are Decimal at the boundary; xlsx and csv both
parse; an absent directory skips cleanly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.ingestion.tennis_data import (
    TennisMatchRow,
    load_tennis_dir,
    parse_season_csv,
    parse_season_xlsx,
    season_url,
)

# Documented tennis-data.co.uk header (load-bearing subset; real order from the
# ATP 2023 workbook) plus one real sample row.
_HEADER = (
    "ATP,Location,Tournament,Date,Series,Court,Surface,Round,Best of,Winner,Loser,"
    "WRank,LRank,Comment,B365W,B365L,PSW,PSL,MaxW,MaxL,AvgW,AvgL"
)
_ROW = (
    "1,Adelaide,Adelaide International 1,01/01/2023,ATP250,Outdoor,Hard,1st Round,3,"
    "Giron M.,Gasquet R.,61,68,Completed,1.91,1.91,1.93,1.95,1.99,1.95,1.89,1.89"
)


def _csv(*rows: str) -> str:
    return "\n".join((_HEADER, *rows)) + "\n"


# --- sample row mapping ----------------------------------------------------
def test_sample_row_maps_players_odds_result_date() -> None:
    rows = parse_season_csv(_csv(_ROW))
    assert len(rows) == 1
    r = rows[0]
    assert r.winner == "Giron M." and r.loser == "Gasquet R."
    assert r.tournament == "Adelaide International 1"
    assert r.surface == "Hard" and r.round == "1st Round"
    assert r.completed is True
    # pre-match Pinnacle (sharp) winner-first, then Max soft
    assert r.psw == Decimal("1.93") and r.psl == Decimal("1.95")
    assert r.maxw == Decimal("1.99") and r.maxl == Decimal("1.95")
    assert r.avgw == Decimal("1.89") and r.avgl == Decimal("1.89")
    # 2-way market: there is no draw / closing field on the row
    assert not hasattr(r, "draw")
    assert not any("clos" in f.lower() for f in TennisMatchRow.__annotations__)


def test_odds_are_decimal_not_float() -> None:
    r = parse_season_csv(_csv(_ROW))[0]
    for value in (r.psw, r.psl, r.maxw, r.maxl, r.avgw, r.avgl):
        assert isinstance(value, Decimal)


def test_date_is_utc_aware_never_naive() -> None:
    r = parse_season_csv(_csv(_ROW))[0]
    assert r.match_date == datetime(2023, 1, 1, tzinfo=UTC)
    assert r.match_date.tzinfo is not None  # never naive (schema-boundary rule)


def test_two_digit_year_date_form_parses() -> None:
    row = _ROW.replace("01/01/2023", "01/01/23")
    r = parse_season_csv(_csv(row))[0]
    assert r.match_date == datetime(2023, 1, 1, tzinfo=UTC)


# --- result / quarantine ---------------------------------------------------
def test_winner_is_the_winner_column_two_way_no_draw() -> None:
    # the 'Winner' column IS the match winner; orientation carries the result
    r = parse_season_csv(_csv(_ROW))[0]
    assert r.winner == "Giron M."  # row 'Winner'
    assert r.loser == "Gasquet R."


def test_non_completed_match_is_flagged_for_quarantine() -> None:
    retired = _ROW.replace(",Completed,", ",Retired,")
    r = parse_season_csv(_csv(retired))[0]
    assert r.completed is False  # caller quarantines; still parsed honestly


# --- odds hygiene ----------------------------------------------------------
def test_blank_and_sub_evens_odds_become_none() -> None:
    # blank PSW, and a <=1.0 PSL must both reject to None (no fake price)
    bad = (
        "1,Adelaide,Adelaide International 1,02/01/2023,ATP250,Outdoor,Hard,1st Round,3,"
        "A B.,C D.,1,2,Completed,1.91,1.91,,1.00,1.99,1.95,1.89,1.89"
    )
    r = parse_season_csv(_csv(bad))[0]
    assert r.psw is None  # blank
    assert r.psl is None  # 1.00 is not a valid decimal price (<=1.0)
    assert r.maxw == Decimal("1.99")


def test_rows_missing_key_fields_are_skipped() -> None:
    # missing Winner -> skipped; missing Date -> skipped; trailing blank -> skipped
    no_winner = (
        "1,Adelaide,T,03/01/2023,S,C,Hard,R,3,,Loser X.,1,2,Completed,,,2.0,2.0,2.1,2.1,1.9,1.9"
    )
    no_date = "1,Adelaide,T,,S,C,Hard,R,3,W X.,L Y.,1,2,Completed,,,2.0,2.0,2.1,2.1,1.9,1.9"
    rows = parse_season_csv(_csv(no_winner, no_date, _ROW))
    assert len(rows) == 1 and rows[0].winner == "Giron M."


# --- xlsx parsing ----------------------------------------------------------
def _xlsx_bytes(header: Sequence[object], *data_rows: Sequence[object]) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    import io as _io

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for row in data_rows:
        ws.append(row)
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_parses_with_native_datetime_and_float_odds() -> None:
    pytest.importorskip("openpyxl")
    header = [
        "Tournament",
        "Date",
        "Surface",
        "Round",
        "Winner",
        "Loser",
        "Comment",
        "PSW",
        "PSL",
        "MaxW",
        "MaxL",
        "AvgW",
        "AvgL",
    ]
    # xlsx Date arrives as a native datetime; odds as floats
    data = [
        "Adelaide",
        datetime(2023, 1, 1),
        "Hard",
        "1st Round",
        "Giron M.",
        "Gasquet R.",
        "Completed",
        1.93,
        1.95,
        1.99,
        1.95,
        1.89,
        1.89,
    ]
    rows = parse_season_xlsx(_xlsx_bytes(header, data))
    assert len(rows) == 1
    r = rows[0]
    assert r.winner == "Giron M." and r.loser == "Gasquet R."
    assert r.match_date == datetime(2023, 1, 1, tzinfo=UTC)  # tz attached, UTC
    assert r.psw == Decimal("1.93") and r.maxw == Decimal("1.99")
    assert isinstance(r.psw, Decimal)  # float cell -> Decimal at the boundary


# --- directory loader ------------------------------------------------------
def test_absent_dir_returns_empty_clean_skip(tmp_path: Path) -> None:
    assert load_tennis_dir(tmp_path / "does_not_exist") == []


def test_dir_loads_and_sorts_csv_and_xlsx(tmp_path: Path) -> None:
    pytest.importorskip("openpyxl")
    # a CSV season file (Jan) + an xlsx season file (Feb) -> combined, date-sorted
    (tmp_path / "atp_2023.csv").write_text(_csv(_ROW), encoding="utf-8")
    header = [
        "Tournament",
        "Date",
        "Surface",
        "Round",
        "Winner",
        "Loser",
        "Comment",
        "PSW",
        "PSL",
        "MaxW",
        "MaxL",
        "AvgW",
        "AvgL",
    ]
    later = [
        "Melbourne",
        datetime(2023, 2, 1),
        "Hard",
        "Final",
        "Sinner J.",
        "Medvedev D.",
        "Completed",
        1.80,
        2.05,
        1.85,
        2.10,
        1.78,
        2.0,
    ]
    (tmp_path / "atp_2023.xlsx").write_bytes(_xlsx_bytes(header, later))
    rows = load_tennis_dir(tmp_path)
    assert len(rows) == 2
    assert [r.match_date for r in rows] == [
        datetime(2023, 1, 1, tzinfo=UTC),
        datetime(2023, 2, 1, tzinfo=UTC),
    ]  # sorted ascending by date


def test_unreadable_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    # a .xlsx that is not a real workbook must be skipped with a log line
    (tmp_path / "broken.xlsx").write_bytes(b"not a zip / not a workbook")
    (tmp_path / "atp_2023.csv").write_text(_csv(_ROW), encoding="utf-8")
    rows = load_tennis_dir(tmp_path)
    assert len(rows) == 1 and rows[0].winner == "Giron M."


# --- URL scheme ------------------------------------------------------------
def test_season_url_atp_and_wta() -> None:
    assert season_url("atp", 2023) == "http://www.tennis-data.co.uk/2023/2023.xlsx"
    assert season_url("wta", 2023) == "http://www.tennis-data.co.uk/2023w/2023.xlsx"
    assert season_url("ATP", 2024).endswith("/2024/2024.xlsx")  # case-insensitive


def test_season_url_unknown_tour_raises() -> None:
    with pytest.raises(ValueError, match="unknown tour"):
        season_url("itf", 2023)


# --- games-level scores (settlement audit 2026-08-02 follow-up) -------------
_GAMES_HEADER = (
    "ATP,Location,Tournament,Date,Series,Court,Surface,Round,Best of,Winner,Loser,"
    "WRank,LRank,W1,L1,W2,L2,W3,L3,W4,L4,W5,L5,Wsets,Lsets,Comment,"
    "PSW,PSL,MaxW,MaxL,AvgW,AvgL"
)


def _games_csv(*rows: str) -> str:
    return "\n".join((_GAMES_HEADER, *rows)) + "\n"


def test_per_set_games_sum_to_totals_with_comment() -> None:
    row = (
        "1,Umag,Croatia Open,19/07/2026,ATP250,Outdoor,Clay,Final,3,"
        "Rublev A.,Darderi L.,11,53,6,4,6,3,,,,,,,2,0,Completed,"
        "1.60,2.40,1.65,2.45,1.58,2.35"
    )
    r = parse_season_csv(_games_csv(row))[0]
    assert r.comment == "Completed" and r.completed is True
    assert (r.winner_games, r.loser_games) == (12, 7)  # 6-4 6-3
    assert (r.winner_sets, r.loser_sets) == (2, 0)


def test_half_blank_set_pair_refuses_games_totals() -> None:
    # W2 present but L2 blank: source anomaly -> totals None (refuse, no guess)
    row = (
        "1,Umag,Croatia Open,19/07/2026,ATP250,Outdoor,Clay,Final,3,"
        "Rublev A.,Darderi L.,11,53,6,4,6,,,,,,,,2,0,Completed,"
        "1.60,2.40,1.65,2.45,1.58,2.35"
    )
    r = parse_season_csv(_games_csv(row))[0]
    assert r.winner_games is None and r.loser_games is None
    assert r.completed is True  # the completion flag is independent of the anomaly


def test_no_set_columns_at_all_leaves_games_none() -> None:
    # legacy header (no W1..L5): totals None, comment still surfaced
    r = parse_season_csv(_csv(_ROW.replace(",Completed,", ",Retired,")))[0]
    assert r.winner_games is None and r.loser_games is None
    assert r.comment == "Retired" and r.completed is False


def test_retired_row_keeps_partial_games_and_raw_comment() -> None:
    row = (
        "1,Umag,Croatia Open,20/07/2026,ATP250,Outdoor,Clay,QF,3,"
        "Alpha A.,Beta B.,1,2,6,4,2,1,,,,,,,1,0,Retired,"
        "1.60,2.40,,,,"
    )
    r = parse_season_csv(_games_csv(row))[0]
    assert r.completed is False and r.comment == "Retired"
    assert (r.winner_games, r.loser_games) == (8, 5)  # games at stoppage


def test_games_cells_reject_negative_and_fractional() -> None:
    row = (
        "1,Umag,Croatia Open,20/07/2026,ATP250,Outdoor,Clay,QF,3,"
        "Alpha A.,Beta B.,1,2,-6,4,6.5,1,,,,,,,2,0,Completed,"
        "1.60,2.40,,,,"
    )
    r = parse_season_csv(_games_csv(row))[0]
    # -6 and 6.5 are garbage -> their pairs half-blank -> totals refuse
    assert r.winner_games is None and r.loser_games is None
