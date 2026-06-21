"""Read-only loader for BeatTheBookie (arXiv 1710.02824) closing_odds.csv.

~880k worldwide matches (2000-2015, 912 leagues) with CONSENSUS (avg) + best-price
(max) 1X2 odds + final results. GET-only: a static historical CSV on GitHub raw
(TilenKopac mirror; cite the arXiv paper for data terms) — NEVER an order venue,
NEVER places bets.

NOT a sharp book: `max_odds >= avg_odds` by construction, so a max-beats-devig(avg)
"edge" partly reflects the mechanical best-of-N price premium, not sharp-beating
skill. Read any ROI against the bet-everything baseline (the null).

A slotted frozen dataclass keeps 880k rows light; the loader is INJECTED so tests
run offline; `_default_loader` streams the raw CSV through csv.DictReader.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

_RAW_URL = (
    "https://raw.githubusercontent.com/TilenKopac/beat-the-bookie-kaggle/main/data/closing_odds.csv"
)
_MISSING = ("", "None", "NA", "nan", "NaN")


@dataclass(frozen=True, slots=True)
class BttMatch:
    """One match: consensus (avg) + best-price (max) 1X2 odds + final score."""

    league: str
    match_date: date
    home_score: int
    away_score: int
    avg_home: float
    avg_draw: float
    avg_away: float
    max_home: float
    max_draw: float
    max_away: float


def _odds(v: Any) -> float | None:
    if v is None or v in _MISSING:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 1.0 else None  # decimal odds must exceed 1.0


def _int(v: Any) -> int | None:
    if v is None or v in _MISSING:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_btb_rows(rows: Iterable[Mapping[str, Any]]) -> list[BttMatch]:
    """Map DictReader rows to BttMatch, skipping rows without complete 1X2 avg+max
    odds, a score, or a parseable date. Pure: no IO. Streams (Iterable) so the
    full 880k file need not be materialized as dicts."""
    out: list[BttMatch] = []
    for r in rows:
        avg = (
            _odds(r.get("avg_odds_home_win")),
            _odds(r.get("avg_odds_draw")),
            _odds(r.get("avg_odds_away_win")),
        )
        mx = (
            _odds(r.get("max_odds_home_win")),
            _odds(r.get("max_odds_draw")),
            _odds(r.get("max_odds_away_win")),
        )
        if None in avg or None in mx:
            continue
        hs, as_ = _int(r.get("home_score")), _int(r.get("away_score"))
        if hs is None or as_ is None:
            continue
        try:
            match_date = date.fromisoformat(str(r.get("match_date", "")))
        except ValueError:
            continue
        out.append(
            BttMatch(
                league=str(r.get("league", "")),
                match_date=match_date,
                home_score=hs,
                away_score=as_,
                avg_home=avg[0],  # type: ignore[arg-type]
                avg_draw=avg[1],  # type: ignore[arg-type]
                avg_away=avg[2],  # type: ignore[arg-type]
                max_home=mx[0],  # type: ignore[arg-type]
                max_draw=mx[1],  # type: ignore[arg-type]
                max_away=mx[2],  # type: ignore[arg-type]
            )
        )
    return out


BttLoader = Callable[[], Awaitable[Iterable[Mapping[str, Any]]]]


async def load_btb_matches(*, loader: BttLoader | None = None) -> list[BttMatch]:
    """Read-only: fetch + parse closing_odds.csv. GET-only. The loader is injected
    so tests run offline; the default streams the raw CSV."""
    load = loader or _default_loader
    rows = await load()
    return parse_btb_rows(rows)


async def _default_loader() -> Iterable[Mapping[str, Any]]:  # pragma: no cover - network
    import httpx

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.get(_RAW_URL)
        resp.raise_for_status()
        text = resp.text
    return csv.DictReader(io.StringIO(text))
