"""Read-only loader for sportsbookreviewsonline NBA historical odds archives.

WHAT THIS IS. sportsbookreviewsonline.com publishes free, multi-season NBA odds
archives (``/scoresoddsarchives/nba-odds-{YYYY}-{YY}``) as a single HTML table
per season — historically also offered as ``.xlsx``. The table is the classic
two-rows-per-game grid (verified against the live 2022-23 page, 2026-06-29):

    Date  Rot  VH  Team  1st 2nd 3rd 4th  Final  Open  Close  ML  2H

Each game is a **Visitor row followed by a Home row** (the ``VH`` column). Of the
two ``Open`` values, the SMALLER is the point **spread** and the LARGER (>100) is
the game **total** (over/under) — the same convention the proven ``sbrscrape``
project decodes. ``ML`` is the CLOSING moneyline (American). ``Date`` is ``MMDD``
with a season rollover: Aug-Dec stay in the season's first calendar year, Jan-Jul
roll into the next.

HONEST SCOPE — CONSENSUS MARKET CLOSE, NOT A SHARP CLOSE. These are SBR's
consensus/market lines, NOT Pinnacle and NOT a sharp anchor. There is OPENING and
CLOSING data for the spread and total, but the moneyline is **closing only**
(there is no opening ML column). So this source carries a market CLOSE plus the
real result — it has NO pre-match takeable price of its own. Its role is breadth:
a consensus closing line + settled result for thousands of NBA games, usable as a
CLV/results anchor to be joined onto a pre-match NBA source (e.g. the Pinnacle
pre-match from ``app/ingestion/oddspapi.py``). It can NEVER, on its own, prove a
+EV edge — there is nothing pre-match to bet against the close. Read every number
from it as a consensus-close sanity check, not a proof.

This is a GET/READ-only loader over public files. It NEVER authenticates and
NEVER places a bet. Data is operator-placed on disk; ``load_sbr_nba_dir`` reads a
directory of season files (``.html``/``.htm``, ``.csv``, ``.xlsx``). Decimal at
the odds boundary (NUMERIC discipline — never float); tz-aware UTC dates only.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

# Canonical column order of the SBR archive grid (index -> meaning).
_COL_DATE, _COL_VH, _COL_TEAM, _COL_FINAL, _COL_OPEN, _COL_CLOSE, _COL_ML = 0, 2, 3, 8, 9, 10, 11
_N_COLS = 13

# Tokens that mean "no usable number" in the SBR sheet (pick'em / no-line / blank).
# ``pk``/``PK`` is pick'em (a 0-point spread) and is handled SEPARATELY where a
# spread is expected; everywhere else these reject to None (never a fake price).
_BLACKLIST = frozenset({"", "-", "nl", "na", "n/a", "off"})
_PICKEM = frozenset({"pk", "pick", "pickem", "pick'em"})

_SEASON_RE = re.compile(r"(\d{4})\s*[-_ ]\s*\d{2}")


@dataclass(frozen=True, slots=True)
class SbrNbaGame:
    """One NBA game: result + consensus opening/closing spread & total + closing
    moneyline. Odds are ``Decimal`` (NUMERIC discipline); point lines are
    ``Decimal`` points; the moneyline is stored BOTH as the raw American integer
    (``*_close_ml_us``) and as decimal odds (``*_close_ml``). There is no opening
    moneyline and no sharp/Pinnacle field — this is a consensus close.
    """

    season: int  # season's first year (2022 == the 2022-23 season)
    game_date: datetime  # tz-aware UTC midnight; never naive
    away_team: str
    home_team: str
    away_final: int
    home_final: int
    result: str  # "H" | "A" (no draw in the NBA)
    away_close_ml_us: int | None  # raw American closing moneyline
    home_close_ml_us: int | None
    away_close_ml: Decimal | None  # closing moneyline as decimal odds
    home_close_ml: Decimal | None
    away_open_spread: Decimal | None
    home_open_spread: Decimal | None
    away_close_spread: Decimal | None
    home_close_spread: Decimal | None
    open_total: Decimal | None
    close_total: Decimal | None


def american_to_decimal(american: object) -> Decimal | None:
    """American moneyline -> decimal odds, or None for garbage/no-line/zero.

    +135 -> 2.35 ; -155 -> 1 + 100/155. Goes through ``str`` so an xlsx float
    cell never leaks its binary artefact into the boundary value."""
    if american is None:
        return None
    text = str(american).strip()
    if text.casefold() in _BLACKLIST or text.casefold() in _PICKEM:
        return None
    try:
        value = int(Decimal(text))  # tolerate "135.0" from xlsx float cells
    except (InvalidOperation, ValueError):
        return None
    if value == 0:
        return None
    if value > 0:
        return Decimal(1) + Decimal(value) / Decimal(100)
    return Decimal(1) + Decimal(100) / Decimal(-value)


def _spread_num(token: object) -> Decimal | None:
    """A spread/total cell -> Decimal points, or None. ``pk`` (pick'em) -> 0."""
    if token is None:
        return None
    text = str(token).strip()
    if text.casefold() in _PICKEM:
        return Decimal(0)
    if text.casefold() in _BLACKLIST:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _int_or_none(token: object) -> int | None:
    if token is None:
        return None
    text = str(token).strip()
    if not text:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def _american_int(token: object) -> int | None:
    if token is None:
        return None
    text = str(token).strip()
    if text.casefold() in _BLACKLIST or text.casefold() in _PICKEM:
        return None
    try:
        value = int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None
    return value or None


def _game_date(mmdd: object, season: int) -> datetime | None:
    """``MMDD`` (e.g. ``1018``/``115``) + season -> tz-aware UTC date.

    Aug-Dec stay in ``season``; Jan-Jul roll into ``season + 1`` (the SBR/sbrscrape
    season-rollover convention). None for an unparseable cell."""
    raw = str(mmdd).strip()
    if raw.endswith(".0"):  # xlsx numeric date cell
        raw = raw[:-2]
    if not raw.isdigit() or not (3 <= len(raw) <= 4):
        return None
    raw = raw.zfill(4)
    month, day = int(raw[:2]), int(raw[2:])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    year = season if month >= 8 else season + 1
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _assign_spread_total(
    away_open: Decimal | None,
    home_open: Decimal | None,
    away_close: Decimal | None,
    home_close: Decimal | None,
    *,
    home_is_favourite: bool,
) -> tuple[
    Decimal | None, Decimal | None, Decimal | None, Decimal | None, Decimal | None, Decimal | None
]:
    """Decode the two ``Open``/``Close`` cells into (home/away spread, total).

    The SMALLER ``Open`` of the pair is the point spread; the LARGER is the game
    total. The favourite carries the negative spread. Returns
    ``(home_open_spread, away_open_spread, home_close_spread, away_close_spread,
    open_total, close_total)``.
    """
    if away_open is None or home_open is None:
        return None, None, None, None, None, None
    if home_open <= away_open:
        spread_open, spread_close = home_open, home_close
        open_total, close_total = away_open, away_close
    else:
        spread_open, spread_close = away_open, away_close
        open_total, close_total = home_open, home_close
    # The spread magnitude is the favourite's line; sign it onto home/away.
    open_mag = abs(spread_open) if spread_open is not None else None
    close_mag = abs(spread_close) if spread_close is not None else None
    if home_is_favourite:
        home_open_spread = -open_mag if open_mag is not None else None
        home_close_spread = -close_mag if close_mag is not None else None
    else:
        home_open_spread = open_mag
        home_close_spread = close_mag
    away_open_spread = -home_open_spread if home_open_spread is not None else None
    away_close_spread = -home_close_spread if home_close_spread is not None else None
    return (
        home_open_spread,
        away_open_spread,
        home_close_spread,
        away_close_spread,
        open_total,
        close_total,
    )


def _result(home_final: int, away_final: int) -> str | None:
    if home_final > away_final:
        return "H"
    if away_final > home_final:
        return "A"
    return None  # an NBA game cannot tie; equal scores => bad row, quarantine


def _looks_like_header(row: Sequence[object]) -> bool:
    return str(row[_COL_DATE]).strip().casefold() in {"date", ""}


def parse_grid(rows: Sequence[Sequence[object]], *, season: int) -> list[SbrNbaGame]:
    """Decode the raw two-rows-per-game grid into ``SbrNbaGame`` objects.

    Pure (stdlib only). ``rows`` is the table body incl. an optional header row;
    each game is a Visitor row then a Home row. A row shorter than 13 columns,
    an unpaired trailing row, a header row, or a game with missing/equal scores is
    skipped (logged, never half-mapped — a wrong score corrupts results)."""
    body = [r for r in rows if not _looks_like_header(r) and len(r) >= _N_COLS]
    games: list[SbrNbaGame] = []
    for i in range(0, len(body) - 1, 2):
        away_row, home_row = body[i], body[i + 1]
        # The pair must be Visitor then Home (defensive: skip a malformed pair).
        if (
            str(away_row[_COL_VH]).strip().upper() != "V"
            or str(home_row[_COL_VH]).strip().upper() != "H"
        ):
            logger.warning("skip sbr-nba pair: not a V/H pair at row %d", i)
            continue
        game_date = _game_date(away_row[_COL_DATE], season)
        away_final = _int_or_none(away_row[_COL_FINAL])
        home_final = _int_or_none(home_row[_COL_FINAL])
        if game_date is None or away_final is None or home_final is None:
            continue
        result = _result(home_final, away_final)
        if result is None:
            logger.warning("skip sbr-nba game with equal/garbage score on %s", game_date.date())
            continue
        away_ml_us = _american_int(away_row[_COL_ML])
        home_ml_us = _american_int(home_row[_COL_ML])
        # Favourite = the more-negative (smaller) American moneyline. When the ML
        # is missing fall back to the spread sign (home_open<=away_open => fav).
        if home_ml_us is not None and away_ml_us is not None:
            home_is_fav = home_ml_us < away_ml_us
        else:
            home_open_raw = _spread_num(home_row[_COL_OPEN])
            away_open_raw = _spread_num(away_row[_COL_OPEN])
            home_is_fav = (
                home_open_raw is not None
                and away_open_raw is not None
                and home_open_raw <= away_open_raw
            )
        (
            home_open_spread,
            away_open_spread,
            home_close_spread,
            away_close_spread,
            open_total,
            close_total,
        ) = _assign_spread_total(
            _spread_num(away_row[_COL_OPEN]),
            _spread_num(home_row[_COL_OPEN]),
            _spread_num(away_row[_COL_CLOSE]),
            _spread_num(home_row[_COL_CLOSE]),
            home_is_favourite=home_is_fav,
        )
        games.append(
            SbrNbaGame(
                season=season,
                game_date=game_date,
                away_team=str(away_row[_COL_TEAM]).strip(),
                home_team=str(home_row[_COL_TEAM]).strip(),
                away_final=away_final,
                home_final=home_final,
                result=result,
                away_close_ml_us=away_ml_us,
                home_close_ml_us=home_ml_us,
                away_close_ml=american_to_decimal(away_row[_COL_ML]),
                home_close_ml=american_to_decimal(home_row[_COL_ML]),
                away_open_spread=away_open_spread,
                home_open_spread=home_open_spread,
                away_close_spread=away_close_spread,
                home_close_spread=home_close_spread,
                open_total=open_total,
                close_total=close_total,
            )
        )
    return games


class _FirstTableParser(HTMLParser):
    """Collect cells of the FIRST <table> into a list of row-lists (stdlib only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._in_table = False
        self._table_done = False
        self._in_cell = False
        self._cur: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if self._table_done:
            return
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._cur = []
        elif tag in ("td", "th") and self._in_table:
            self._in_cell = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if self._table_done or not self._in_table:
            return
        if tag in ("td", "th"):
            self._cur.append("".join(self._buf).strip())
            self._in_cell = False
        elif tag == "tr":
            if self._cur:
                self.rows.append(self._cur)
            self._cur = []
        elif tag == "table":
            self._in_table = False
            self._table_done = True  # only the first table

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._buf.append(data)


def parse_html(text: str, *, season: int) -> list[SbrNbaGame]:
    """Parse the first HTML <table> on an SBR season page into games (stdlib)."""
    parser = _FirstTableParser()
    parser.feed(text)
    return parse_grid(parser.rows, season=season)


def parse_csv(text: str, *, season: int) -> list[SbrNbaGame]:
    """Parse an operator-exported CSV of the SBR grid into games (stdlib)."""
    reader = csv.reader(io.StringIO(text))
    return parse_grid(list(reader), season=season)


def parse_xlsx(data: bytes, *, season: int) -> list[SbrNbaGame]:
    """Parse an SBR ``.xlsx`` workbook into games (openpyxl, lazy import)."""
    import openpyxl  # lazy: only the .xlsx path needs the backtest extra

    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        worksheet = workbook.active
        if worksheet is None:
            return []
        rows = [
            ["" if v is None else str(v) for v in values]
            for values in worksheet.iter_rows(values_only=True)
        ]
        return parse_grid(rows, season=season)
    finally:
        workbook.close()


def season_from_filename(name: str) -> int | None:
    """``nba-odds-2022-23.html`` / ``nba odds 2019-20.xlsx`` -> 2022 / 2019.

    The season's FIRST year. None when the name carries no ``YYYY-YY`` token."""
    match = _SEASON_RE.search(name)
    return int(match.group(1)) if match else None


def load_sbr_nba_dir(path: Path) -> list[SbrNbaGame]:
    """Read-only: parse every operator-placed SBR NBA season file in ``path``.

    Dispatches by extension: ``.html``/``.htm`` -> stdlib HTML table parse;
    ``.csv`` -> stdlib CSV; ``.xlsx`` -> openpyxl. The season is read from the
    filename (``nba-odds-2022-23``); a file with no season token is skipped with a
    log line. An absent directory returns ``[]`` (the caller prints the
    operator-place instruction). Sorted by (date, home_team) for determinism."""
    if not path.is_dir():
        return []
    games: list[SbrNbaGame] = []
    patterns = ("*.html", "*.htm", "*.csv", "*.xlsx")
    for f in sorted({p for pat in patterns for p in path.glob(pat)}):
        season = season_from_filename(f.name)
        if season is None:
            logger.warning("skip sbr-nba file %s: no YYYY-YY season in name", f.name)
            continue
        try:
            if f.suffix.lower() in (".html", ".htm"):
                games.extend(
                    parse_html(f.read_text(encoding="utf-8", errors="replace"), season=season)
                )
            elif f.suffix.lower() == ".csv":
                games.extend(
                    parse_csv(f.read_text(encoding="utf-8", errors="replace"), season=season)
                )
            else:
                games.extend(parse_xlsx(f.read_bytes(), season=season))
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("skip sbr-nba file %s: %s", f.name, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 — openpyxl raises its own types on bad input
            logger.warning("skip sbr-nba file %s: %s", f.name, type(exc).__name__)
    games.sort(key=lambda g: (g.game_date, g.home_team))
    return games


__all__ = [
    "SbrNbaGame",
    "american_to_decimal",
    "load_sbr_nba_dir",
    "parse_csv",
    "parse_grid",
    "parse_html",
    "parse_xlsx",
    "season_from_filename",
]
