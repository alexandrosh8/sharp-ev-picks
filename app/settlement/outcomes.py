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

from app.resolution.matching import normalize_name
from app.schemas.base import Outcome

# DECLARED tennis settlement convention (ADR-0019 sport-shadow appendix,
# encoded 2026-07-04). Book conventions differ (Pinnacle grades the moneyline
# once >=1 full set is completed and a player advanced; bet365 voids on any
# retirement; walkovers void everywhere). We settle to the Pinnacle rule —
# matching our sharp-anchor book — spelled out:
#   * walkover, or any abnormal completion BEFORE one completed set
#     -> ALL markets VOID (stake returned, pnl 0);
#   * retirement/default AFTER >=1 completed set -> h2h (match_winner) graded
#     to the ADVANCING player; every other market (e.g. over_under_sets)
#     VOID — the match total is undefined, never inferred from a fragment;
#   * anything not affirmatively classifiable (no winner flag, incomplete
#     set pattern on a "final") is NOT settled — the pick stays open for
#     manual /events/{id}/result entry. Never guess.
TENNIS_SETTLEMENT_CONVENTION = "pinnacle_one_set"

_TOTALS_RE = re.compile(r"(Over|Under) (\d+(?:\.\d+)?)")
_EH_DRAW_RE = re.compile(r"Draw \(([+-]?\d+(?:\.\d+)?)\)")
_SIGNED_LINE_RE = re.compile(r"[+-]\d+(?:\.\d+)?")

# Sports (Sport.key) whose h2h market is TWO-WAY — no Draw leg is offered or
# minted, so a tied final has no winning leg to absorb it; standard book rules
# refund a 2-way moneyline on a tie (push), never grade both sides LOST.
# Keyed by SPORT, not market: NFL h2h is minted from the same "home_away"
# 2-way key basketball uses, so the market string cannot distinguish them —
# and only american_football can actually tie (NFL OT rules; basketball/tennis
# cannot tie, soccer h2h is 3-way with an explicit Draw leg).
_TWO_WAY_H2H_SPORTS = frozenset({"american_football"})


def settle_selection(
    market: str,
    selection: str,
    home: str,
    away: str,
    home_score: int,
    away_score: int,
    *,
    sport_key: str | None = None,
) -> Outcome:
    """Outcome of one selection given the full-time score.

    `sport_key` (optional) enables sport-convention grading: a tied final on a
    TWO-WAY h2h market (see _TWO_WAY_H2H_SPORTS) pushes instead of losing.
    Callers without a sport in hand keep the 3-way default unchanged.

    Raises ValueError for selections that cannot be mapped — callers must
    skip (and log) rather than guess.
    """
    if home_score < 0 or away_score < 0:
        raise ValueError(f"negative score: {home_score}-{away_score}")

    if market == "h2h":
        return _settle_h2h(
            selection,
            home,
            away,
            home_score,
            away_score,
            tie_pushes=sport_key in _TWO_WAY_H2H_SPORTS,
        )
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


def settle_selection_retired(
    market: str,
    selection: str,
    home: str,
    away: str,
    winner_side: str | None,
) -> Outcome:
    """Outcome of one selection for a RETIRED tennis match under
    TENNIS_SETTLEMENT_CONVENTION ("pinnacle_one_set"): >=1 completed set and
    an advancing player — h2h grades to that player, all other markets VOID.

    Raises ValueError when the winner side is unknown or the selection cannot
    be mapped — callers must skip (and log) rather than guess.
    """
    if winner_side not in ("home", "away"):
        raise ValueError(f"retired match without a known advancing side: {winner_side!r}")
    if market == "h2h":
        if selection == home:
            return _won(winner_side == "home")
        if selection == away:
            return _won(winner_side == "away")
        raise ValueError(f"h2h selection {selection!r} matches neither player")
    return Outcome.VOID  # totals/other markets: undefined on retirement -> stake returned


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


def provisional_result(
    market: str,
    selection: str,
    home: str,
    away: str,
    home_score: int | None,
    away_score: int | None,
    stake: Decimal | None = None,
    decimal_odds: Decimal | None = None,
) -> tuple[str | None, str | None]:
    """Best-effort (market, selection, scraped final score) -> (outcome, pnl)
    for a kicked-off-but-unsettled pick, so the CLOSED tab can show how the
    value bet landed BEFORE formal settlement. Returns (None, None) when the
    score is missing or the selection cannot be graded — it NEVER guesses. The
    authoritative, persisted outcome + P&L still come from settlement (the
    SETTLED tab); this is a read-time convenience only. outcome is an Outcome
    value string ("won"/"lost"/...); pnl is a 2dp string, or None when stake or
    odds is absent."""
    if home_score is None or away_score is None:
        return None, None
    try:
        outcome = settle_selection(market, selection, home, away, int(home_score), int(away_score))
    except (ValueError, TypeError):
        return None, None  # unmappable selection -> no guess
    pnl: str | None = None
    if stake is not None and decimal_odds is not None:
        pnl = str(pick_pnl(outcome, Decimal(str(stake)), Decimal(str(decimal_odds))))
    return outcome.value, pnl


def _won(condition: bool) -> Outcome:  # noqa: FBT001 — internal binary helper
    return Outcome.WON if condition else Outcome.LOST


def _settle_h2h(
    selection: str,
    home: str,
    away: str,
    hs: int,
    as_: int,
    *,
    tie_pushes: bool = False,
) -> Outcome:
    if tie_pushes and hs == as_ and selection in (home, away):
        # Two-way moneyline (no Draw leg — see _TWO_WAY_H2H_SPORTS): a tied
        # final refunds the stake. Only a 3-way market's team legs lose a draw.
        return Outcome.PUSH
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
    """Grade double-chance ORIENTATION-INDEPENDENTLY (like h2h/dnb).

    A cross-source duplicate can mint the event with home/away swapped relative
    to the pick's stored selection text; rebuilding "{home} or Draw" from the
    CURRENT order would then wrongly raise. Instead we parse the selection into
    its component token(s) and resolve which physical side each named team is by
    exact NORMALIZED-name match against either side — never by string rebuild.
    A token matching neither team is genuinely unparseable and still fails loud.
    """
    parts = [part.strip() for part in selection.split(" or ")]
    if len(parts) != 2:
        raise ValueError(f"double_chance selection {selection!r} unparseable")

    if "Draw" in parts:
        # "{T} or Draw" -> WON iff T did not lose (T is home & hs>=as, or away & as>=hs).
        team = parts[0] if parts[1] == "Draw" else parts[1]
        side = _dc_side(team, home, away)
        if side is None:
            raise ValueError(f"double_chance selection {selection!r} matches neither team")
        return _won(hs >= as_ if side == "home" else as_ >= hs)

    # "{A} or {B}" (no Draw) -> "not a draw"; both tokens must be the two teams.
    named = {normalize_name(parts[0]), normalize_name(parts[1])}
    if named != {normalize_name(home), normalize_name(away)}:
        raise ValueError(f"double_chance selection {selection!r} matches neither team")
    return _won(hs != as_)


def _dc_side(team: str, home: str, away: str) -> str | None:
    """Physical side of a named double-chance team by exact normalized match."""
    token = normalize_name(team)
    if not token:
        return None
    if token == normalize_name(home):
        return "home"
    if token == normalize_name(away):
        return "away"
    return None


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
