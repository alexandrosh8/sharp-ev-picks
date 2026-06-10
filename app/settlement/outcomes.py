"""Pure outcome mapping: (market, selection, final score) -> Outcome.

Selection strings are exactly what app/ingestion/oddsportal.py::_selections
emits (and what picks persist). Pure stdlib — no env/DB/HTTP/log side effects
(same boundary as app/probabilities/).

Spreads semantics: half-line selections are Asian handicap (push impossible);
INTEGER-line team selections are European handicap legs of a 3-way market, so
an adjusted draw LOSES (the separate "Draw (line)" leg wins it). Integer-line
Asian handicaps would push instead, but the loader config rejects push lines
(decision log 2026-06-10) — if they ever appear here, that decision changed.
"""

import re
from decimal import Decimal

from app.schemas.base import Outcome

_TOTALS_RE = re.compile(r"(Over|Under) (\d+(?:\.\d+)?)")
_EH_DRAW_RE = re.compile(r"Draw \(([+-]?\d+(?:\.\d+)?)\)")
_SIGNED_LINE_RE = re.compile(r"[+-]\d+(?:\.\d+)?")


def settle_selection(
    market: str,
    selection: str,
    home: str,
    away: str,
    home_score: int,
    away_score: int,
) -> Outcome:
    """Outcome of one selection given the full-time score.

    Raises ValueError for selections that cannot be mapped — callers must
    skip (and log) rather than guess.
    """
    if home_score < 0 or away_score < 0:
        raise ValueError(f"negative score: {home_score}-{away_score}")

    if market == "h2h":
        return _settle_h2h(selection, home, away, home_score, away_score)
    if market == "totals":
        return _settle_totals(selection, home_score + away_score)
    if market == "btts":
        return _settle_btts(selection, home_score, away_score)
    if market == "dnb":
        return _settle_dnb(selection, home, away, home_score, away_score)
    if market == "double_chance":
        return _settle_double_chance(selection, home, away, home_score, away_score)
    if market == "spreads":
        return _settle_spreads(selection, home, away, home_score, away_score)
    raise ValueError(f"market {market!r} is not settleable")


def pick_pnl(outcome: Outcome, stake: Decimal, decimal_odds: Decimal) -> Decimal:
    """Profit/loss of a stake at decimal odds. Push/void return the stake;
    half outcomes (Asian quarter lines) settle half the stake, return half."""
    if outcome is Outcome.WON:
        return (stake * (decimal_odds - 1)).quantize(Decimal("0.01"))
    if outcome is Outcome.LOST:
        return (-stake).quantize(Decimal("0.01"))
    if outcome is Outcome.HALF_WON:
        return (stake / 2 * (decimal_odds - 1)).quantize(Decimal("0.01"))
    if outcome is Outcome.HALF_LOST:
        return (-stake / 2).quantize(Decimal("0.01"))
    return Decimal("0.00")  # void | push


def pick_roi(pnl: Decimal, stake: Decimal) -> Decimal | None:
    """Return on the stake; None when the stake is zero."""
    if stake == 0:
        return None
    return pnl / stake


def _won(condition: bool) -> Outcome:  # noqa: FBT001 — internal binary helper
    return Outcome.WON if condition else Outcome.LOST


def _settle_h2h(selection: str, home: str, away: str, hs: int, as_: int) -> Outcome:
    if selection == home:
        return _won(hs > as_)
    if selection == away:
        return _won(as_ > hs)
    if selection == "Draw":
        return _won(hs == as_)
    raise ValueError(f"h2h selection {selection!r} matches neither team nor Draw")


def _settle_totals(selection: str, total: int) -> Outcome:
    match = _TOTALS_RE.fullmatch(selection)
    if match is None:
        raise ValueError(f"totals selection {selection!r} unparseable")
    direction, raw_line = match.groups()
    line = float(raw_line)
    if total == line:
        return Outcome.PUSH
    over = total > line
    return _won(over if direction == "Over" else not over)


def _settle_btts(selection: str, hs: int, as_: int) -> Outcome:
    both = hs > 0 and as_ > 0
    if selection == "BTTS Yes":
        return _won(both)
    if selection == "BTTS No":
        return _won(not both)
    raise ValueError(f"btts selection {selection!r} unparseable")


def _settle_dnb(selection: str, home: str, away: str, hs: int, as_: int) -> Outcome:
    if selection not in (home, away):
        raise ValueError(f"dnb selection {selection!r} matches neither team")
    if hs == as_:
        return Outcome.PUSH
    return _won(hs > as_ if selection == home else as_ > hs)


def _settle_double_chance(selection: str, home: str, away: str, hs: int, as_: int) -> Outcome:
    if selection == f"{home} or Draw":
        return _won(hs >= as_)
    if selection == f"{home} or {away}":
        return _won(hs != as_)
    if selection == f"Draw or {away}":
        return _won(as_ >= hs)
    raise ValueError(f"double_chance selection {selection!r} unparseable")


def _settle_spreads(selection: str, home: str, away: str, hs: int, as_: int) -> Outcome:
    eh_draw = _EH_DRAW_RE.fullmatch(selection)
    if eh_draw is not None:
        # European handicap draw leg: home + line must equal away exactly.
        return _won(hs + float(eh_draw.group(1)) == as_)

    team, _, raw_line = selection.rpartition(" ")
    if not team or _SIGNED_LINE_RE.fullmatch(raw_line) is None:
        raise ValueError(f"spreads selection {selection!r} unparseable")
    line = float(raw_line)
    if team == home:
        base = float(hs - as_)
    elif team == away:
        base = float(as_ - hs)
    else:
        raise ValueError(f"spreads selection {selection!r} matches neither team")

    if not (line * 2).is_integer():
        # Asian QUARTER line: two half-stakes on the adjacent half-lines
        # (e.g. -0.25 = 0.0 and -0.5). Integer components PUSH on the
        # adjusted tie here (Asian), unlike standalone integer-line
        # selections which are European handicap (see below).
        components = {_ah_component(base + line - 0.25), _ah_component(base + line + 0.25)}
        if components == {Outcome.WON}:
            return Outcome.WON
        if components == {Outcome.LOST}:
            return Outcome.LOST
        if components == {Outcome.WON, Outcome.PUSH}:
            return Outcome.HALF_WON
        if components == {Outcome.LOST, Outcome.PUSH}:
            return Outcome.HALF_LOST
        raise ValueError(f"impossible quarter-line split for {selection!r}")  # defensive

    margin = base + line
    if margin > 0:
        return Outcome.WON
    # margin == 0 only on whole lines = European handicap team leg -> LOST
    # (see module docstring; Asian push lines are rejected upstream).
    return Outcome.LOST


def _ah_component(margin: float) -> Outcome:
    """One half-stake of an Asian handicap: adjusted tie is a PUSH."""
    if margin > 0:
        return Outcome.WON
    if margin == 0:
        return Outcome.PUSH
    return Outcome.LOST
