"""tennis-data.co.uk loader — free historical tennis results + PRE-MATCH odds.

Sister site of football-data.co.uk (which we already load in
``app/ingestion/football_data.py``): one per-season workbook for ATP (2001+)
and WTA (2007+), published at ``http://www.tennis-data.co.uk/alldata.php``.
Read-only GET of public ``.xlsx`` files (or operator-exported ``.csv``); this
loader never authenticates and never places a bet.

CRITICAL SCOPE — PRE-MATCH ONLY, **NO CLOSING COLUMN**.
    football-data publishes Pinnacle CLOSING odds (the ``PSC*`` quartet), which
    is what makes CLV measurable there. tennis-data does NOT: the only odds
    columns are ``PSW``/``PSL`` (Pinnacle PRE-MATCH win/loss decimal odds — the
    sharp pre-match price), ``MaxW``/``MaxL`` (best-of-books pre-match) and
    ``AvgW``/``AvgL`` (average pre-match). There is NO ``PSC*``/``MaxC*``
    analogue. This source therefore gives a pre-match Pinnacle sharp price + Max
    soft price + the real result, but it **cannot measure CLV on its own** —
    there is no close to compare against. To measure a tennis close it must be
    paired with a Betfair-BSP tennis loader (``app/ingestion/betfair_bsp.py``
    today parses soccer/basketball MATCH_ODDS only — a tennis BSP parser does
    not yet exist), so tennis stays pre-match / visibility-only until then.

MARKET: 2-way match winner (H2H). No draw. Each row carries the WINNER and the
LOSER (the ``Winner`` column IS the match winner), so the result is implicit in
the orientation; a leak-safe backtest must randomize which side the winner sits
on before selection (this loader reports the honest winner-first truth and
leaves that randomization to the backtest layer).

DATA QUALITY: only ``Comment == "Completed"`` matches settle cleanly; Retired /
Walkover / Awarded matches are surfaced via ``completed=False`` so the caller can
quarantine them (a staked match-winner market would void or resolve ambiguously).

Columns verified against the ATP 2023 workbook (36 cols, 2026-06-28). Decimal at
the odds boundary (NUMERIC discipline — never float); tz-aware UTC dates only.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

BASE_URL = "http://www.tennis-data.co.uk"

# Per-season workbook URL schemes (verified 2026-06-28):
#   ATP: http://www.tennis-data.co.uk/{year}/{year}.xlsx   (2001+)
#   WTA: http://www.tennis-data.co.uk/{year}w/{year}.xlsx  (2007+)
TOURS = {
    "atp": "{base}/{year}/{year}.xlsx",
    "wta": "{base}/{year}w/{year}.xlsx",
}


@dataclass(frozen=True, slots=True)
class TennisMatchRow:
    """One historical singles match: result + PRE-MATCH odds (no close exists).

    ``winner``/``loser`` carry the result (the source's ``Winner`` is the match
    winner). Odds are decimal, winner-first: ``psw``/``psl`` Pinnacle pre-match,
    ``maxw``/``maxl`` best-of-books pre-match, ``avgw``/``avgl`` average. All odds
    are ``Decimal`` (NUMERIC discipline) or None when missing/<=1.0. There is no
    closing-odds field — this source has none.
    """

    match_date: datetime  # tz-aware UTC (00:00:00Z); never naive
    tournament: str | None
    surface: str | None
    round: str | None
    winner: str
    loser: str
    completed: bool  # Comment == "Completed"; else quarantine (Retired/Walkover)
    psw: Decimal | None  # Pinnacle pre-match, WINNER side (sharp)
    psl: Decimal | None  # Pinnacle pre-match, LOSER side (sharp)
    maxw: Decimal | None  # best-of-books pre-match, WINNER side (soft)
    maxl: Decimal | None  # best-of-books pre-match, LOSER side (soft)
    avgw: Decimal | None  # average pre-match, WINNER side
    avgl: Decimal | None  # average pre-match, LOSER side


def _to_decimal(value: object) -> Decimal | None:
    """Decimal-odds cell -> Decimal, or None for missing/<=1.0/garbage.

    Goes through ``str`` so a float cell (xlsx) never leaks its binary artefact
    into the boundary value (same discipline as betfair_bsp._to_decimal)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return dec if dec > 1 else None


def _parse_date(value: object) -> datetime | None:
    """A Date cell -> tz-aware UTC datetime (midnight), or None.

    Handles an xlsx ``datetime``/``date`` cell and a CSV ``dd/mm/YYYY`` /
    ``dd/mm/yy`` / ISO string. A naive value is assumed UTC — never left naive."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        text = value.strip()
        if text:
            for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text, fmt).replace(tzinfo=UTC)
                except ValueError:
                    continue
    return None


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _row_from_mapping(raw: Mapping[str, object]) -> TennisMatchRow | None:
    """Map one header->value mapping (CSV DictReader row or xlsx row) to a
    TennisMatchRow, or None when the load-bearing fields are missing."""
    winner = _opt_str(raw.get("Winner"))
    loser = _opt_str(raw.get("Loser"))
    when = _parse_date(raw.get("Date"))
    if winner is None or loser is None or when is None:
        return None
    comment = _opt_str(raw.get("Comment")) or ""
    return TennisMatchRow(
        match_date=when,
        tournament=_opt_str(raw.get("Tournament")),
        surface=_opt_str(raw.get("Surface")),
        round=_opt_str(raw.get("Round")),
        winner=winner,
        loser=loser,
        completed=comment.casefold() == "completed",
        psw=_to_decimal(raw.get("PSW")),
        psl=_to_decimal(raw.get("PSL")),
        maxw=_to_decimal(raw.get("MaxW")),
        maxl=_to_decimal(raw.get("MaxL")),
        avgw=_to_decimal(raw.get("AvgW")),
        avgl=_to_decimal(raw.get("AvgL")),
    )


def parse_season_csv(text: str) -> list[TennisMatchRow]:
    """Parse one season's CSV text (operator-exported) into match rows.

    Tolerant: rows missing Winner/Loser/Date are skipped (e.g. trailing blanks).
    Pure: stdlib only, no IO."""
    rows: list[TennisMatchRow] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for raw in reader:
        row = _row_from_mapping(raw)
        if row is not None:
            rows.append(row)
    return rows


def parse_season_xlsx(data: bytes) -> list[TennisMatchRow]:
    """Parse one season's ``.xlsx`` workbook bytes into match rows.

    Uses openpyxl (read-only, data_only) — supplied by the ``backtest`` extra.
    The first row is the header; each subsequent row is zipped to it."""
    import openpyxl  # lazy: only the .xlsx path needs the extra

    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        worksheet = workbook.active
        if worksheet is None:
            return []
        row_iter = worksheet.iter_rows(values_only=True)
        try:
            header = [_opt_str(h) or "" for h in next(row_iter)]
        except StopIteration:
            return []
        rows: list[TennisMatchRow] = []
        for values in row_iter:
            raw = {header[i]: values[i] for i in range(min(len(header), len(values)))}
            row = _row_from_mapping(raw)
            if row is not None:
                rows.append(row)
        return rows
    finally:
        workbook.close()


def load_tennis_dir(path: Path) -> list[TennisMatchRow]:
    """Read-only: parse every operator-placed season file in ``path``.

    Dispatches by extension: ``.xlsx``/``.xls`` -> openpyxl; ``.csv`` -> stdlib
    CSV. (Legacy binary ``.xls`` is not openpyxl-readable; such a file is skipped
    with a log line — re-export it to ``.xlsx`` or ``.csv``.) An absent directory
    returns ``[]`` (the caller prints the operator-place instruction). Sorted by
    (date, tournament, winner, loser) for determinism."""
    if not path.is_dir():
        return []
    rows: list[TennisMatchRow] = []
    patterns = ("*.xlsx", "*.xls", "*.csv")
    for f in sorted({p for pat in patterns for p in path.glob(pat)}):
        try:
            if f.suffix.lower() in (".xlsx", ".xls"):
                rows.extend(parse_season_xlsx(f.read_bytes()))
            else:
                rows.extend(parse_season_csv(f.read_text(encoding="utf-8", errors="replace")))
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("skip tennis file %s: %s", f.name, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 — openpyxl raises its own types on bad .xls
            logger.warning("skip tennis file %s: %s", f.name, type(exc).__name__)
    rows.sort(key=lambda r: (r.match_date, r.tournament or "", r.winner, r.loser))
    return rows


def season_url(tour: str, year: int) -> str:
    """Per-season workbook URL for a tour ('atp'/'wta') and 4-digit year."""
    tour_key = tour.strip().lower()
    if tour_key not in TOURS:
        raise ValueError(f"unknown tour: {tour!r}")
    return TOURS[tour_key].format(base=BASE_URL, year=year)


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=8.0),
    reraise=True,
)
async def fetch_season(client: httpx.AsyncClient, tour: str, year: int) -> bytes:
    """Read-only GET of one season's ``.xlsx`` workbook (binary). GET-only;
    never authenticates, never places a bet."""
    response = await client.get(season_url(tour, year), timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    return response.content


__all__ = [
    "BASE_URL",
    "TOURS",
    "TennisMatchRow",
    "fetch_season",
    "load_tennis_dir",
    "parse_season_csv",
    "parse_season_xlsx",
    "season_url",
]
