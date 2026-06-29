"""Tests for the sportsbookreviewsonline NBA loader (app/ingestion/sbr_nba).

Synthetic data only — no network. Validates the documented SBR archive layout
(verified against the live 2022-23 NBA archive table, 2026-06-29): the classic
two-rows-per-game grid

    Date  Rot  VH  Team  1st 2nd 3rd 4th  Final  Open  Close  ML  2H

where each game is a Visitor row followed by a Home row, the SMALLER of the two
``Open`` values is the point spread and the larger is the game total, and ``ML``
is the CONSENSUS CLOSING moneyline (American). There is NO opening moneyline and
NO sharp (Pinnacle) anchor in this source — the close is a market consensus.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.ingestion.sbr_nba import (
    SbrNbaGame,
    american_to_decimal,
    load_sbr_nba_dir,
    parse_grid,
    parse_html,
    season_from_filename,
)

# Header + one real game (Philadelphia @ Boston, 2022-10-18, from the live
# 2022-23 archive): V Philadelphia Open=229(total) Close=216 ML=+135; H Boston
# Open=7(spread) Close=3 ML=-155. Boston (home) is the favourite.
_HEADER = [
    "Date",
    "Rot",
    "VH",
    "Team",
    "1st",
    "2nd",
    "3rd",
    "4th",
    "Final",
    "Open",
    "Close",
    "ML",
    "2H",
]
_AWAY = [
    "1018",
    "501",
    "V",
    "Philadelphia",
    "29",
    "34",
    "25",
    "29",
    "117",
    "229",
    "216",
    "135",
    "107",
]
_HOME = ["1018", "502", "H", "Boston", "24", "39", "35", "28", "126", "7", "3", "-155", "2"]


def _grid(*game_rows: list[str]) -> list[list[str]]:
    return [_HEADER, *game_rows]


# --- American -> Decimal odds ----------------------------------------------
def test_american_to_decimal_positive_and_negative() -> None:
    assert american_to_decimal("135") == Decimal("2.35")  # +135 -> 1 + 135/100
    assert american_to_decimal(-155) == Decimal("100") / Decimal("155") + 1
    assert american_to_decimal("-200") == Decimal("1.5")


def test_american_to_decimal_rejects_garbage_and_pickem() -> None:
    for bad in ("", "NL", "pk", None, "-", "0"):
        assert american_to_decimal(bad) is None


# --- one-game grid mapping --------------------------------------------------
def test_sample_game_maps_teams_scores_result() -> None:
    games = parse_grid(_grid(_AWAY, _HOME), season=2022)
    assert len(games) == 1
    g = games[0]
    assert g.away_team == "Philadelphia" and g.home_team == "Boston"
    assert g.away_final == 117 and g.home_final == 126
    assert g.result == "H"  # Boston (home) won 126-117 — no draw in the NBA


def test_sample_game_date_is_utc_with_season_rollover() -> None:
    g = parse_grid(_grid(_AWAY, _HOME), season=2022)[0]
    # October is in the Aug-Dec block -> stays in the season's first year (2022)
    assert g.game_date == datetime(2022, 10, 18, tzinfo=UTC)
    assert g.game_date.tzinfo is not None  # never naive (schema-boundary rule)


def test_january_game_rolls_into_second_calendar_year() -> None:
    jan_away = [
        "115",
        "501",
        "V",
        "Miami",
        "20",
        "20",
        "20",
        "20",
        "80",
        "210",
        "208",
        "120",
        "100",
    ]
    jan_home = ["115", "502", "H", "Denver", "25", "25", "25", "30", "105", "6", "5", "-140", "2"]
    g = parse_grid(_grid(jan_away, jan_home), season=2022)[0]
    assert g.game_date == datetime(2023, 1, 15, tzinfo=UTC)  # Jan -> season+1


def test_smaller_open_is_spread_larger_is_total() -> None:
    g = parse_grid(_grid(_AWAY, _HOME), season=2022)[0]
    # Boston Open=7 is the spread; Philadelphia Open=229 is the total.
    assert g.open_total == Decimal("229") and g.close_total == Decimal("216")
    # Home (Boston) is the favourite (ML -155 < +135) -> negative spread.
    assert g.home_open_spread == Decimal("-7") and g.away_open_spread == Decimal("7")
    assert g.home_close_spread == Decimal("-3") and g.away_close_spread == Decimal("3")


def test_moneyline_stored_as_decimal_odds_and_american() -> None:
    g = parse_grid(_grid(_AWAY, _HOME), season=2022)[0]
    assert g.away_close_ml_us == 135 and g.home_close_ml_us == -155
    assert g.away_close_ml == Decimal("2.35")  # +135
    assert g.home_close_ml == Decimal("100") / Decimal("155") + 1  # -155
    assert isinstance(g.home_close_ml, Decimal)  # NUMERIC discipline at boundary


# --- hygiene / quarantine ---------------------------------------------------
def test_missing_scores_game_is_skipped() -> None:
    away = ["1018", "501", "V", "Philadelphia", "", "", "", "", "", "229", "216", "135", "107"]
    home = ["1018", "502", "H", "Boston", "", "", "", "", "", "7", "3", "-155", "2"]
    assert parse_grid(_grid(away, home), season=2022) == []


def test_pickem_and_blacklisted_odds_become_none_not_fake() -> None:
    # 'NL' moneyline (no line) and 'pk' spread must not fabricate a price.
    away = ["1018", "501", "V", "Atlanta", "20", "20", "20", "20", "80", "200", "198", "NL", "100"]
    home = ["1018", "502", "H", "Chicago", "20", "20", "20", "25", "85", "pk", "pk", "NL", "2"]
    g = parse_grid(_grid(away, home), season=2022)[0]
    assert g.home_close_ml is None and g.away_close_ml is None  # 'NL' -> None
    assert g.home_open_spread == Decimal("0")  # 'pk' = pick'em = 0, not None
    assert g.result == "H"  # 85 > 80, settled on the real score regardless of odds


def test_odd_row_count_drops_unpaired_trailing_row() -> None:
    # a dangling visitor row with no matching home row must not crash / half-map
    games = parse_grid(_grid(_AWAY, _HOME, _AWAY), season=2022)
    assert len(games) == 1


# --- HTML table parsing (the live archive format) --------------------------
def test_parse_html_extracts_the_odds_table() -> None:
    html = (
        "<html><body><table>"
        "<tr>" + "".join(f"<th>{c}</th>" for c in _HEADER) + "</tr>"
        "<tr>" + "".join(f"<td>{c}</td>" for c in _AWAY) + "</tr>"
        "<tr>" + "".join(f"<td>{c}</td>" for c in _HOME) + "</tr>"
        "</table></body></html>"
    )
    games = parse_html(html, season=2022)
    assert len(games) == 1 and games[0].home_team == "Boston"


def test_season_from_filename() -> None:
    assert season_from_filename("nba-odds-2022-23.html") == 2022
    assert season_from_filename("nba odds 2019-20.xlsx") == 2019
    assert season_from_filename("garbage.csv") is None


# --- directory loader -------------------------------------------------------
def test_absent_dir_returns_empty_clean_skip(tmp_path: Path) -> None:
    assert load_sbr_nba_dir(tmp_path / "nope") == []


def test_dir_loads_html_and_sorts_by_date(tmp_path: Path) -> None:
    def _html(*rows: list[str]) -> str:
        body = "<tr>" + "".join(f"<th>{c}</th>" for c in _HEADER) + "</tr>"
        for r in rows:
            body += "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
        return f"<table>{body}</table>"

    jan_away = [
        "115",
        "501",
        "V",
        "Miami",
        "20",
        "20",
        "20",
        "20",
        "80",
        "210",
        "208",
        "120",
        "100",
    ]
    jan_home = ["115", "502", "H", "Denver", "25", "25", "25", "30", "105", "6", "5", "-140", "2"]
    (tmp_path / "nba-odds-2022-23.html").write_text(_html(jan_away, jan_home), encoding="utf-8")
    (tmp_path / "nba-odds-2021-22.html").write_text(_html(_AWAY, _HOME), encoding="utf-8")
    games = load_sbr_nba_dir(tmp_path)
    assert len(games) == 2
    # 2021-22 Oct game sorts before 2022-23 Jan game
    assert games[0].game_date == datetime(2021, 10, 18, tzinfo=UTC)
    assert games[1].game_date == datetime(2023, 1, 15, tzinfo=UTC)


def test_unreadable_file_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "nba-odds-2022-23.html").write_text("<html>no table here</html>", encoding="utf-8")
    (tmp_path / "broken.bin").write_bytes(b"\x00\x01\x02")
    assert load_sbr_nba_dir(tmp_path) == []  # nothing parseable, no crash


def test_game_has_no_pinnacle_or_sharp_field() -> None:
    # honest scope guard: this source is consensus-close only, never a sharp anchor
    fields = {f.lower() for f in SbrNbaGame.__annotations__}
    assert not any("pinnacle" in f or "sharp" in f or "open_ml" in f for f in fields)
