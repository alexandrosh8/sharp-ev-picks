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

# Tennis SET-SCORE guard (settlement bug 2026-07-10, 106 mis-graded picks):
# the scraped tennis result is a SET score (best-of-5 -> home+away <= 5), but
# some captured totals/spreads lines are GAME lines ("Over 22.5",
# "Karolina Muchova -4.5"). Grading a game line against a set score reads
# 2-1 as "3 total, margin 1" — provably wrong. A line beyond the set range
# (totals > 4.5, |spread| > 2.5) is game-based by construction; set-plausible
# lines (total sets over/under 2.5, set spread -1.5) stay gradeable.
TENNIS_MAX_SET_SUM = 5
_TENNIS_MAX_SET_TOTAL_LINE = 4.5
_TENNIS_MAX_SET_SPREAD_LINE = 2.5


def is_tennis_game_line(market: str, selection: str) -> bool:
    """True when a tennis totals/spreads selection carries a GAME-sized line:
    totals line > 4.5 or spread |line| > 2.5 (parsed from the selection tail,
    same forms _settle_totals/_settle_spreads accept). Unparseable selections
    return False — the ordinary settle path raises its own loud error."""
    if market == "totals":
        match = _TOTALS_RE.fullmatch(selection)
        return match is not None and float(match.group(2)) > _TENNIS_MAX_SET_TOTAL_LINE
    if market == "spreads":
        team, _, raw_line = selection.rpartition(" ")
        if not team or _SIGNED_LINE_RE.fullmatch(raw_line) is None:
            return False
        return abs(float(raw_line)) > _TENNIS_MAX_SET_SPREAD_LINE
    return False


def tennis_set_score_ungradeable(
    market: str, selection: str, home_score: int, away_score: int
) -> bool:
    """True when a tennis pick must NOT be graded from this score: the
    selection carries a GAME-sized line while the final is SET-sized
    (home+away <= TENNIS_MAX_SET_SUM). Doctrine: leave the pick unsettled for
    manual result entry — never void, never guess."""
    if home_score + away_score > TENNIS_MAX_SET_SUM:
        return False
    return is_tennis_game_line(market, selection)


# Sports (Sport.key) whose h2h market is TWO-WAY — no Draw leg is offered or
# minted, so a tied final has no winning leg to absorb it; standard book rules
# refund a 2-way moneyline on a tie (push), never grade both sides LOST.
# Keyed by SPORT, not market: NFL h2h is minted from the same "home_away"
# 2-way key basketball uses, so the market string cannot distinguish them —
# and only american_football can actually tie (NFL OT rules; basketball/tennis
# cannot tie, soccer h2h is 3-way with an explicit Draw leg).
_TWO_WAY_H2H_SPORTS = frozenset({"american_football"})

#: Sports whose spreads are TWO-WAY handicap markets (no Draw leg): an
#: integer-line adjusted tie PUSHES (Asian/US convention). Soccer stays
#: European-handicap semantics (adjusted draw loses the team leg) — see the
#: module docstring. Audit 2026-07-10 (M360): 2 live tennis integer-line
#: spreads existed; basketball is the shadow tier's biggest CLV cell.
_TWO_WAY_HANDICAP_SPORTS = frozenset({"basketball", "tennis", "american_football"})


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

    if sport_key == "tennis" and tennis_set_score_ungradeable(
        market, selection, home_score, away_score
    ):
        raise ValueError(
            f"tennis {market} selection {selection!r} carries a game-sized line but "
            f"the final {home_score}-{away_score} is a set score — left for manual "
            "settlement, never graded from set counts"
        )

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
        return _settle_spreads(
            selection,
            home,
            away,
            home_score,
            away_score,
            integer_tie_pushes=sport_key in _TWO_WAY_HANDICAP_SPORTS,
        )
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


def pick_pnl(
    outcome: Outcome,
    stake: Decimal,
    decimal_odds: Decimal,
    *,
    bookmaker: str | None = None,
) -> Decimal:
    """Profit/loss of a stake at decimal odds. Push/void return the stake;
    half outcomes (Asian quarter lines) settle half the stake, return half.

    When ``bookmaker`` names an exchange, WINNINGS are netted through the
    same commission table EV/Kelly use at mint (audit 2026-07-10 M171: live
    Matchbook fills were credited gross). Losses are never commissioned.
    ``bookmaker=None`` keeps the gross behaviour for callers without a book.
    """
    if bookmaker is not None and outcome in (Outcome.WON, Outcome.HALF_WON):
        from app.edge.value import effective_odds  # lazy: keep module deps flat

        decimal_odds = Decimal(str(effective_odds(bookmaker, float(decimal_odds))))
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
    *,
    sport_key: str | None = None,
    bookmaker: str | None = None,
) -> tuple[str | None, str | None]:
    """Best-effort (market, selection, scraped final score) -> (outcome, pnl)
    for a kicked-off-but-unsettled pick, so the CLOSED tab can show how the
    value bet landed BEFORE formal settlement. Returns (None, None) when the
    score is missing or the selection cannot be graded — it NEVER guesses. The
    authoritative, persisted outcome + P&L still come from settlement (the
    SETTLED tab); this is a read-time convenience only. outcome is an Outcome
    value string ("won"/"lost"/...); pnl is a 2dp string, or None when stake or
    odds is absent. ``sport_key`` threads the same sport-aware refusals the
    settler applies (tennis game-line-vs-set-score, 2-way tie push) into the
    display grade — a tennis "Over 22.5" against a 2-1 SET score must show no
    provisional outcome, not a wrong one (operator report 2026-07-10)."""
    if home_score is None or away_score is None:
        return None, None
    try:
        outcome = settle_selection(
            market,
            selection,
            home,
            away,
            int(home_score),
            int(away_score),
            sport_key=sport_key,
        )
    except (ValueError, TypeError):
        return None, None  # unmappable / sport-ungradeable selection -> no guess
    pnl: str | None = None
    if stake is not None and decimal_odds is not None:
        pnl = str(
            pick_pnl(outcome, Decimal(str(stake)), Decimal(str(decimal_odds)), bookmaker=bookmaker)
        )
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
    if not (line * 2).is_integer():
        # Asian QUARTER totals line (x.25/x.75): two half-stakes on the
        # adjacent half-lines, each graded with Asian push-on-tie semantics
        # (audit 2026-07-10 — previously graded as one line, paying full
        # WON/LOST where the correct grade is HALF_WON/HALF_LOST).
        components = {
            _totals_component(direction, total, line - 0.25),
            _totals_component(direction, total, line + 0.25),
        }
        return _combine_quarter(components, selection)
    if total == line:
        return Outcome.PUSH
    over = total > line
    return _won(over if direction == "Over" else not over)


def _totals_component(direction: str, total: int, comp_line: float) -> Outcome:
    """One half-stake of an Asian quarter total: margin sign decides, tie pushes."""
    margin = float(total) - comp_line if direction == "Over" else comp_line - float(total)
    return _ah_component(margin)


def _settle_btts(selection: str, hs: int, as_: int) -> Outcome:
    # Two vocabularies for one two-outcome market: OddsPortal emits the
    # prefixed 'BTTS Yes'/'BTTS No'; OddsChecker's canonical form is the bare
    # 'Yes'/'No' (app.ingestion.oddschecker._canonical_selection strips the
    # legacy prefix). Both grade from the same full-time score; no push/void
    # exists on this market (a 0-0 is a clean 'No' win).
    both = hs > 0 and as_ > 0
    if selection in ("BTTS Yes", "Yes"):
        return _won(both)
    if selection in ("BTTS No", "No"):
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


def _settle_spreads(
    selection: str,
    home: str,
    away: str,
    hs: int,
    as_: int,
    *,
    integer_tie_pushes: bool = False,
) -> Outcome:
    eh_draw = _EH_DRAW_RE.fullmatch(selection)
    if eh_draw is not None:
        # European handicap draw leg: home + line must equal away exactly.
        return _won(hs + float(eh_draw.group(1)) == as_)

    team, _, raw_line = selection.rpartition(" ")
    if not team or _SIGNED_LINE_RE.fullmatch(raw_line) is None:
        raise ValueError(f"spreads selection {selection!r} unparseable")
    line = float(raw_line)
    if team == "Draw":
        # OddsChecker 3-way (European) handicap draw leg, bare-line form
        # ("Draw -1" via _line_bearing_selection). The line is the HOME
        # team's handicap — grounded 2026-07-26 against live same-capture
        # snapshot triples ({home -1, Draw -1, away +1} devig to one book's
        # market; the mirrored home +1 market carries "Draw +1") — i.e. the
        # SAME instrument as OddsPortal's parenthesised "Draw (L)" leg above,
        # graded identically: the draw leg absorbs the adjusted tie exactly.
        # A draw leg only exists on integer lines; anything else is not a
        # gradable instrument — fail loud, never guess.
        if not line.is_integer():
            raise ValueError(f"spreads draw selection {selection!r} carries a non-integer line")
        return _won(hs + line == as_)
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
        return _combine_quarter(components, selection)

    margin = base + line
    if margin > 0:
        return Outcome.WON
    if margin == 0:
        # Whole-line adjusted tie. TWO-WAY handicap sports (basketball/tennis/
        # NFL — no Draw leg) PUSH per Asian/US convention (audit 2026-07-10);
        # soccer keeps European handicap semantics: the team leg LOSES the
        # adjusted draw (see module docstring).
        return Outcome.PUSH if integer_tie_pushes else Outcome.LOST
    return Outcome.LOST


def _combine_quarter(components: set[Outcome], selection: str) -> Outcome:
    """Combine the two half-stake outcomes of an Asian quarter line."""
    if components == {Outcome.WON}:
        return Outcome.WON
    if components == {Outcome.LOST}:
        return Outcome.LOST
    if components == {Outcome.WON, Outcome.PUSH}:
        return Outcome.HALF_WON
    if components == {Outcome.LOST, Outcome.PUSH}:
        return Outcome.HALF_LOST
    raise ValueError(f"impossible quarter-line split for {selection!r}")  # defensive


def _ah_component(margin: float) -> Outcome:
    """One half-stake of an Asian handicap: adjusted tie is a PUSH."""
    if margin > 0:
        return Outcome.WON
    if margin == 0:
        return Outcome.PUSH
    return Outcome.LOST
