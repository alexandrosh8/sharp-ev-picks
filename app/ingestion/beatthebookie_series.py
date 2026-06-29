"""Read-only loader for BeatTheBookie (arXiv 1710.02824) ``odds_series``.

WHAT THIS IS. The ``odds_series`` / ``odds_series_b`` datasets are continuous
per-bookmaker 1x2 odds *series* for ~114k worldwide football matches
(odds_series: Sep 2015-Mar 2016, 553 leagues; odds_series_b: Mar-Nov 2016,
658 leagues) — including non-European leagues football-data.co.uk lacks. Each
match is one plain-text file (``generate_odds_series_csv.php`` in the upstream
repo) holding a **32 x 216 matrix**: 32 bookmakers (rows, fixed order — see
``BOOKIES``) by 216 columns laid out as [72 HOME | 72 DRAW | 72 AWAY], one
column per hour over the 72h before kick-off. Missing/disabled quotes are the
literal token ``nan``. Column 71 within each block is the kick-off (closing)
sample; column 0 is ~72h before (the opener). This matches the SQL
``odds_history_series.opening_closing`` flag: the kick-off-time row is the
``opening_closing = 1`` (closing) entry, the earliest is the opener.

The filename carries the rest:
``match_{ID}_{YYYY}_{MM}_{DD}_{HH}_{MM}_{SS}_{home_score}_{away_score}.txt``
The timestamp is UTC (the PHP generator sets ``date_default_timezone_set('UTC')``).

HONEST SCOPE — THIS IS SOFT/CONSENSUS DATA, NOT A SHARP CLOSE. There is no
clean Pinnacle or Betfair anchor here (Pinnacle is merely one of the 32 books,
row 9, and is often missing per league). We therefore derive a market
**consensus** (mean across available books) and a **best** line (max across
books) at both the opening and the closing snapshot. Any "CLV" computed from
this source is value vs the **market-consensus close**, NOT a sharp close. The
strategy's sharp-CLV proof still rests on the football-data Pinnacle close
(``app/ingestion/football_data.py``) and the upcoming Betfair BSP source; this
loader adds BREADTH — worldwide-league backtest sanity checks and many more
matches per market for probability calibration (crossing the isotonic n>=1000
threshold). State that clearly anywhere these numbers are reported.

This is a GET/READ-only loader over static academic files (GPL-3.0 research
release; cite arXiv:1710.02824). It NEVER places bets and NEVER authenticates
to any venue. The data is operator-placed on disk; ``load_series_dir`` reads a
directory of ``*.txt`` files. The on-disk parse mirrors
``app/ingestion/football_data.py``'s dataclass + pure-parser pattern, and
``to_fd_row`` adapts a match into the football-data-style row dict that
``scripts/value_backtest.py`` already consumes.
"""

from __future__ import annotations

import csv
import gzip
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

logger = logging.getLogger(__name__)

N_HOURS = 72
N_OUTCOMES = 3  # HOME, DRAW, AWAY
N_COLS = N_HOURS * N_OUTCOMES  # 216
N_BOOKS = 32  # fixed bookmaker count (see BOOKIES); also the CSV b1..b32 width

# Bookmaker row order (index -> name), verbatim from the upstream
# ``$bookie_name_to_index`` map in generate_odds_series_csv.php. Pinnacle is
# row 9; it is one soft book among many here, NOT a trusted sharp anchor.
BOOKIES: tuple[str, ...] = (
    "Interwetten",  # 0
    "bwin",  # 1
    "bet-at-home",  # 2
    "Unibet",  # 3
    "Stan James",  # 4
    "Expekt",  # 5
    "10Bet",  # 6
    "William Hill",  # 7
    "bet365",  # 8
    "Pinnacle Sports",  # 9
    "DOXXbet",  # 10
    "Betsafe",  # 11
    "Betway",  # 12
    "888sport",  # 13
    "Ladbrokes",  # 14
    "Betclic",  # 15
    "Sportingbet",  # 16
    "myBet",  # 17
    "Betsson",  # 18
    "188BET",  # 19
    "Jetbull",  # 20
    "Paddy Power",  # 21
    "Tipico",  # 22
    "Coral",  # 23
    "SBOBET",  # 24
    "BetVictor",  # 25
    "12BET",  # 26
    "Titanbet",  # 27
    "youwin",  # 28
    "ComeOn",  # 29
    "Betadonis",  # 30
    "Betfair Sports",  # 31
)
PINNACLE_ROW = 9

# match_{ID}_{Y}_{m}_{d}_{H}_{i}_{s}_{score1}_{score2}.txt
_FNAME_RE = re.compile(
    r"^match_(\d+)_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d+)_(\d+)\.txt$"
)

Triple = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class SeriesMatch:
    """One match: consensus + best 1x2 odds at the opening and closing snapshot.

    ``*_consensus`` is the mean across books that had a coherent 1x2 triple at
    that snapshot; ``*_best`` is the max across those books (the line-shopping
    price). ``open_*`` is the earliest available quote (opener), ``close_*`` is
    the kick-off-time quote (the ``opening_closing = 1`` close). Decimal odds.
    """

    match_id: int
    kickoff_utc: datetime  # tz-aware UTC, never naive
    home_score: int
    away_score: int
    result: str  # H | D | A
    open_consensus: Triple
    open_best: Triple
    close_consensus: Triple
    close_best: Triple
    n_books_open: int
    n_books_close: int
    # Worldwide league label (e.g. "Chile: Primera Division"). Only the Kaggle
    # CSV path carries it (joined from the *_matches metadata); the per-game
    # .txt path has no league in the filename, so it stays None there.
    league: str | None = None


def parse_match_filename(name: str) -> tuple[int, datetime, int, int] | None:
    """``match_{id}_{Y_m_d_H_i_s}_{home}_{away}.txt`` -> (id, UTC kickoff, hs, as).

    Returns None for any name that does not match the documented pattern."""
    m = _FNAME_RE.match(name)
    if m is None:
        return None
    mid, y, mo, d, hh, mm, ss, hs, as_ = (int(x) for x in m.groups())
    try:
        kickoff = datetime(y, mo, d, hh, mm, ss, tzinfo=UTC)
    except ValueError:
        return None
    return mid, kickoff, hs, as_


def _cell(token: str) -> float | None:
    """A matrix cell: decimal odds (> 1.0) or None for nan/disabled/garbage."""
    t = token.strip()
    if not t or t.lower() == "nan" or t == "disabled":
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 1.0 else None


def parse_odds_matrix(text: str) -> list[list[float | None]]:
    """Parse the 32 x 216 matrix. Tolerant: short/long lines are padded or
    truncated to N_COLS; missing bookie rows are filled with None so the
    result is always a rectangular ``len(BOOKIES) x N_COLS`` grid."""
    rows: list[list[float | None]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells: list[float | None] = [_cell(tok) for tok in line.split(",")]
        if len(cells) < N_COLS:
            cells.extend([None] * (N_COLS - len(cells)))
        rows.append(cells[:N_COLS])
    # normalise to exactly len(BOOKIES) rows (pad short, never drop data)
    while len(rows) < len(BOOKIES):
        rows.append([None] * N_COLS)
    return rows[: len(BOOKIES)]


def _coherent_triple(row: list[float | None], col: int) -> Triple | None:
    """The (HOME, DRAW, AWAY) prices for one book at hour-column ``col``, or
    None unless all three outcomes are priced (a coherent 1x2 quote)."""
    h = row[col]
    d = row[N_HOURS + col]
    a = row[2 * N_HOURS + col]
    if h is None or d is None or a is None:
        return None
    return (h, d, a)


def _book_open_close(row: list[float | None]) -> tuple[Triple | None, Triple | None]:
    """Earliest and latest coherent 1x2 triple for one book over 72h.

    Opener = first column (toward 72h-before) with a full triple; closer =
    last column (toward kick-off) with a full triple. They may be the same
    column when the book quoted only once."""
    opening: Triple | None = None
    closing: Triple | None = None
    for col in range(N_HOURS):
        triple = _coherent_triple(row, col)
        if triple is None:
            continue
        if opening is None:
            opening = triple
        closing = triple  # keep overwriting -> ends on the latest
    return opening, closing


def _aggregate(triples: list[Triple]) -> tuple[Triple, Triple]:
    """(consensus mean, best max) per outcome across books' triples."""
    n = len(triples)
    consensus: Triple = tuple(sum(t[i] for t in triples) / n for i in range(N_OUTCOMES))  # type: ignore[assignment]
    best: Triple = tuple(max(t[i] for t in triples) for i in range(N_OUTCOMES))  # type: ignore[assignment]
    return consensus, best


def _result(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "H"
    if home_score < away_score:
        return "A"
    return "D"


def build_series_match(
    match_id: int,
    kickoff_utc: datetime,
    home_score: int,
    away_score: int,
    matrix: list[list[float | None]],
    league: str | None = None,
) -> SeriesMatch | None:
    """Derive a SeriesMatch from a parsed 32x216 odds matrix (shared by the
    per-game .txt and the Kaggle-CSV paths so they produce IDENTICAL shapes).

    Returns None when no book quoted a coherent 1x2 triple at the open OR at
    the close (cannot price both sides of CLV)."""
    opens: list[Triple] = []
    closes: list[Triple] = []
    for row in matrix:
        opening, closing = _book_open_close(row)
        if opening is not None:
            opens.append(opening)
        if closing is not None:
            closes.append(closing)
    if not opens or not closes:
        logger.warning("skip btb match %s: no coherent 1x2 quotes", match_id)
        return None
    open_consensus, open_best = _aggregate(opens)
    close_consensus, close_best = _aggregate(closes)
    return SeriesMatch(
        match_id=match_id,
        kickoff_utc=kickoff_utc,
        home_score=home_score,
        away_score=away_score,
        result=_result(home_score, away_score),
        open_consensus=open_consensus,
        open_best=open_best,
        close_consensus=close_consensus,
        close_best=close_best,
        n_books_open=len(opens),
        n_books_close=len(closes),
        league=league,
    )


def parse_series_match(filename: str, text: str) -> SeriesMatch | None:
    """Parse one per-game file into a SeriesMatch, or None if unusable.

    Skips: unparseable filename; a matrix with no book quoting a coherent 1x2
    triple at the open OR at the close (cannot price both sides of CLV)."""
    parsed = parse_match_filename(filename)
    if parsed is None:
        logger.warning("skip btb file with bad name: %s", filename)
        return None
    match_id, kickoff, home_score, away_score = parsed
    matrix = parse_odds_matrix(text)
    return build_series_match(match_id, kickoff, home_score, away_score, matrix)


def to_fd_row(m: SeriesMatch) -> dict[str, str]:
    """Adapt to the football-data-style row dict ``scripts/value_backtest.py``
    consumes. HONEST MAPPING: the consensus line stands in for the Pinnacle
    (PS*) "fair" anchor — it is a market consensus, NOT a sharp price — and the
    best line maps to the Max* line-shopping price. Closing slots carry the
    consensus/best CLOSE. Stringified so it is drop-in for ``csv.DictReader``
    rows (``bets_for`` float-parses every cell)."""
    oc, ob = m.open_consensus, m.open_best
    cc, cb = m.close_consensus, m.close_best
    return {
        # anchor ("fair") = consensus open; PS* slot, but consensus not sharp
        "PSH": f"{oc[0]:.6f}",
        "PSD": f"{oc[1]:.6f}",
        "PSA": f"{oc[2]:.6f}",
        # best takeable pre-match price
        "MaxH": f"{ob[0]:.6f}",
        "MaxD": f"{ob[1]:.6f}",
        "MaxA": f"{ob[2]:.6f}",
        # consensus close (CLV reference: consensus, NOT sharp)
        "PSCH": f"{cc[0]:.6f}",
        "PSCD": f"{cc[1]:.6f}",
        "PSCA": f"{cc[2]:.6f}",
        # best-of-books close (stricter CLV reference)
        "MaxCH": f"{cb[0]:.6f}",
        "MaxCD": f"{cb[1]:.6f}",
        "MaxCA": f"{cb[2]:.6f}",
        "FTR": m.result,
        "FTHG": str(m.home_score),
        "FTAG": str(m.away_score),
        # context (ignored by bets_for, useful for splitting/inspection)
        "Date": m.kickoff_utc.date().isoformat(),
        "BtbMatchId": str(m.match_id),
        "BtbLeague": m.league or "",
    }


def load_series_dir(path: Path | str) -> list[SeriesMatch]:
    """Read-only: parse every ``match_*.txt`` file in ``path`` into
    SeriesMatch objects, sorted by (kickoff, id) for determinism. Unparseable
    files are skipped with a log line (no fuzzy recovery).

    If the directory holds no per-game ``match_*.txt`` files but DOES hold the
    Kaggle wide CSVs (``odds_series*.csv[.gz]``), it transparently falls back
    to the CSV path so pointing this loader at ``data/beatthebookie/`` works
    for either on-disk layout."""
    path = Path(path)
    txt_files = sorted(path.glob("match_*.txt"))
    if not txt_files:
        csv_matches = [
            m for c in _csv_odds_files(path) for m in load_series_csv(c, _derive_matches_csv(c))
        ]
        if csv_matches:
            csv_matches.sort(key=lambda m: (m.kickoff_utc, m.match_id))
            return csv_matches
    matches: list[SeriesMatch] = []
    for f in txt_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        parsed = parse_series_match(f.name, text)
        if parsed is not None:
            matches.append(parsed)
    matches.sort(key=lambda m: (m.kickoff_utc, m.match_id))
    return matches


def _csv_odds_files(path: Path) -> list[Path]:
    """Wide odds CSVs in ``path`` (``odds_series*.csv[.gz]``), excluding the
    ``*_matches`` metadata siblings. Sorted for deterministic load order."""
    found: list[Path] = []
    for pattern in ("odds_series*.csv", "odds_series*.csv.gz"):
        for f in path.glob(pattern):
            if "_matches" not in f.name:
                found.append(f)
    return sorted(found)


# ---------------------------------------------------------------------------
# Kaggle CSV path: the same odds_series data shipped as two gzipped CSVs
# instead of one .txt per match. The wide odds CSV flattens the 32x216 matrix
# into ``{home,draw,away}_b{1..32}_{0..71}`` columns. The layout is BOOK-MAJOR:
# each book b occupies a contiguous 216-col block laid out exactly like a
# matrix row ([72 HOME | 72 DRAW | 72 AWAY]), so ``draw_b1_0`` sits at offset
# 72 (NOT after all 32 home columns). A sibling ``*_matches`` CSV carries the
# league + result + kick-off, joined by ``match_id``. Same SeriesMatch shape.
# ---------------------------------------------------------------------------

_CSV_DATA_START = 5  # match_id, match_date, match_time, score_home, score_away
_CSV_N_COLS = _CSV_DATA_START + N_BOOKS * N_COLS  # 5 + 32*216 = 6917


@contextmanager
def _open_text(path: Path) -> Iterator[TextIO]:
    """Open a UTF-8 text stream, gunzipping transparently for ``*.gz``."""
    if path.suffix == ".gz":
        fh: TextIO = gzip.open(path, mode="rt", encoding="utf-8", errors="replace")  # noqa: SIM115 (closed in finally)
    else:
        fh = path.open(encoding="utf-8", errors="replace")  # noqa: SIM115 (closed in finally)
    try:
        yield fh
    finally:
        fh.close()


def parse_score(score: str) -> tuple[int, int] | None:
    """Metadata ``score`` cell ``"H:A"`` -> (home, away), or None if malformed."""
    parts = score.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def parse_match_datetime(value: str) -> datetime | None:
    """Metadata ``match_datetime`` ``"YYYY-MM-DD HH:MM:SS"`` -> tz-aware UTC.

    The upstream generator fixes the timezone to UTC, so we attach UTC rather
    than guess a local zone (naive datetimes are a bug in this stack)."""
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _MatchMeta:
    league: str
    home_score: int
    away_score: int
    kickoff_utc: datetime


def _load_matches_meta(matches_csv: Path) -> dict[int, _MatchMeta]:
    """Read the ``*_matches`` metadata CSV into ``{match_id: _MatchMeta}``.

    Header has a space after each comma; ``skipinitialspace`` strips it from
    both header names and values. Rows missing match_id/score/datetime are
    skipped (quarantined) with a log line — no fuzzy recovery."""
    meta: dict[int, _MatchMeta] = {}
    with _open_text(matches_csv) as fh:
        reader = csv.DictReader(fh, skipinitialspace=True)
        for raw in reader:
            mid_s = (raw.get("match_id") or "").strip()
            try:
                match_id = int(mid_s)
            except ValueError:
                continue
            scores = parse_score(raw.get("score") or "")
            kickoff = parse_match_datetime(raw.get("match_datetime") or "")
            if scores is None or kickoff is None:
                logger.warning("skip btb meta %s: bad score/datetime", mid_s)
                continue
            meta[match_id] = _MatchMeta(
                league=(raw.get("league") or "").strip(),
                home_score=scores[0],
                away_score=scores[1],
                kickoff_utc=kickoff,
            )
    return meta


def _matrix_from_csv_values(values: list[str]) -> list[list[float | None]]:
    """Reshape one wide-CSV data row's odds cells into the 32x216 matrix the
    matrix parser produces. The CSV is BOOK-MAJOR: book ``b`` occupies the
    216-col slice ``[5 + b*216, 5 + (b+1)*216)`` already laid out exactly as a
    matrix row ([72 HOME | 72 DRAW | 72 AWAY]), so each row is a direct slice."""
    matrix: list[list[float | None]] = []
    for b in range(N_BOOKS):
        base = _CSV_DATA_START + b * N_COLS
        cells = values[base : base + N_COLS]
        row: list[float | None] = [_cell(c) for c in cells]
        if len(row) < N_COLS:  # tolerate a short/truncated final row
            row.extend([None] * (N_COLS - len(row)))
        matrix.append(row)
    return matrix


def load_series_csv(odds_csv: Path, matches_csv: Path) -> list[SeriesMatch]:
    """Read-only: join the wide odds CSV to its ``*_matches`` metadata CSV and
    produce SeriesMatch objects (same shape/aggregation as ``load_series_dir``).

    Result, kick-off, and league come from the metadata join (authoritative);
    odds rows whose ``match_id`` is absent from the metadata are quarantined
    with a log line (no fuzzy auto-merge). Sorted by (kickoff, id)."""
    meta = _load_matches_meta(matches_csv)
    matches: list[SeriesMatch] = []
    skipped_unjoined = 0
    with _open_text(odds_csv) as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        for values in reader:
            if not values:
                continue
            try:
                match_id = int(values[0].strip())
            except (IndexError, ValueError):
                continue
            m = meta.get(match_id)
            if m is None:
                skipped_unjoined += 1
                continue
            matrix = _matrix_from_csv_values(values)
            sm = build_series_match(
                match_id,
                m.kickoff_utc,
                m.home_score,
                m.away_score,
                matrix,
                league=m.league,
            )
            if sm is not None:
                matches.append(sm)
    if skipped_unjoined:
        logger.warning(
            "btb csv %s: %d odds rows had no metadata join (quarantined)",
            odds_csv.name,
            skipped_unjoined,
        )
    matches.sort(key=lambda mm: (mm.kickoff_utc, mm.match_id))
    return matches


def _derive_matches_csv(odds_csv: Path) -> Path:
    """``odds_series.csv[.gz]`` -> sibling ``odds_series_matches.csv[.gz]``."""
    name = odds_csv.name
    for suffix in (".csv.gz", ".csv"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return odds_csv.with_name(f"{stem}_matches{suffix}")
    return odds_csv.with_name(f"{odds_csv.stem}_matches{odds_csv.suffix}")


def load_series_any(path: Path) -> list[SeriesMatch]:
    """Auto-detect the source layout: a directory -> the per-game ``*.txt``
    matrix parser; a ``*.csv`` / ``*.csv.gz`` file -> the Kaggle CSV path
    (the ``*_matches`` metadata sibling is derived by filename convention)."""
    if path.is_dir():
        return load_series_dir(path)
    return load_series_csv(path, _derive_matches_csv(path))
