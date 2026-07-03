"""Read-only loader for free Betfair historical STREAM data — the SHARP CLOSE.

WHAT THIS IS. Betfair publishes historical exchange data at
``historicdata.betfair.com`` in the proprietary **stream** format: one
newline-delimited-JSON file per market (``.bz2``-compressed), where each line is
a Betfair Exchange Stream **market-change message** (``op == "mcm"``). The Basic
(free) tier covers settled markets but is ACCOUNT-GATED — the data is therefore
OPERATOR-PLACED on disk (same pattern as ``beatthebookie_series.py``); a live
fetch from this sandbox returns HTTP 401.

This loader is GET/READ-only over those static files. It NEVER authenticates, it
NEVER places a bet, and it stores no credentials. ``load_betfair_dir`` reads a
directory of operator-placed market files; ``parse_market_stream`` turns one
market's message sequence into a :class:`BetfairMarketClose`.

HONEST SCOPE — BSP IS A *CLOSE*, NOT A PRE-MATCH BET PRICE. A Betfair Starting
Price (BSP) / last-pre-in-play price is the SHARP CLOSING reference and the
settled result. It is NOT a price you could have bet pre-match in this dataset,
so it is joined (by date + team-name match, via ``app/resolution/matching``) to a
PRE-MATCH source (football-data Max soft) that the backtest already loads. The
backtest then measures CLV of the pre-match bet vs this real sharp close — the
sharp-anchor complement to the consensus-anchored BeatTheBookie breadth loader.

DOCUMENTED FORMAT (Betfair Exchange Stream API ``MarketChange`` message; the
public schema is mirrored by the betfair-datascientists historic-data docs).
Fields used — no invented fields, READ-ONLY (this loader parses files; it has no
client and never connects):

* message:        ``{"op":"mcm", "pt": <publish-time epoch MILLIS, UTC>,
                     "mc": [ <market change> ]}``
* market change:  ``{"id": "<marketId>", "marketDefinition": {...}, "rc": [...]}``
* marketDefinition: ``marketType`` ("MATCH_ODDS"), ``eventTypeId``
                    ("1"=Soccer, "7522"=Basketball), ``eventName``,
                    ``competition`` {``name``}, ``marketTime`` (ISO-8601 ``Z``
                    UTC scheduled start = kickoff), ``status``
                    ("OPEN"/"SUSPENDED"/"CLOSED"), ``inPlay`` (bool),
                    ``bspMarket`` / ``bspReconciled`` (bool),
                    ``runners`` [ runner definition ]
* runner def:     ``id`` (selectionId), ``name``, ``sortPriority``, ``status``
                  ("ACTIVE"/"WINNER"/"LOSER"/"REMOVED"), ``bsp`` (reconciled SP)
* runner change:  ``id`` (selectionId), ``ltp`` (last traded price), ``batb``
                  (best available to back ladder: ``[[level, price, size], ...]``),
                  ``bdatb`` (best display available to back) — same shape.

CLOSE DERIVATION. Walk the messages in order tracking each runner's best-back
(``batb``/``bdatb`` level 0, falling back to ``ltp``). When ``marketDefinition``
first flips ``inPlay: true`` we SNAPSHOT the prices accumulated from all PRIOR
messages — that is the last pre-in-play price. The final settled
``marketDefinition`` carries WINNER/LOSER and (for BSP markets) ``bsp``. The
per-runner CLOSE is the reconciled ``bsp`` when present, else the pre-in-play
snapshot, else the last seen price.
"""

from __future__ import annotations

import bz2
import json
import logging
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.resolution.matching import AliasTable

logger = logging.getLogger(__name__)

# Betfair event-type ids (stable across the platform).
SOCCER_EVENT_TYPE_ID = "1"
BASKETBALL_EVENT_TYPE_ID = "7522"

# "The Draw" has a FIXED selection id across every soccer MATCH_ODDS market, so
# the draw runner is identifiable even when a Basic-tier file omits runner names.
DRAW_SELECTION_ID = 58805


@dataclass(frozen=True, slots=True)
class BetfairRunner:
    """One market runner with its settled status and CLOSING price (Decimal).

    ``close_price`` is the reconciled ``bsp`` if present, otherwise the last
    pre-in-play best-back price. ``won`` is True for WINNER, False for LOSER,
    None for any non-settled / removed status."""

    selection_id: int
    name: str | None
    sort_priority: int | None
    status: str  # ACTIVE | WINNER | LOSER | REMOVED | ""
    close_price: Decimal | None
    bsp: Decimal | None
    won: bool | None


@dataclass(frozen=True, slots=True)
class BetfairMarketClose:
    """One settled market: definition + per-runner sharp close and result."""

    market_id: str
    event_type_id: str | None
    event_name: str | None
    competition: str | None
    market_type: str | None
    kickoff_utc: datetime | None  # marketTime, tz-aware UTC (never naive)
    in_play_utc: datetime | None  # pt at the in-play turn, tz-aware UTC
    settled: bool
    bsp_reconciled: bool
    runners: tuple[BetfairRunner, ...]
    # Parser audit 2026-07-03 F4: both ride every marketDefinition and were
    # dropped. event_id joins an event's markets exactly (OU<->MATCH_ODDS)
    # without name re-matching; settled_time_utc is the settlement timestamp
    # the settler wants. Optional with defaults so pre-F4 caches still load.
    event_id: str | None = None
    settled_time_utc: datetime | None = None


def _to_decimal(value: object) -> Decimal | None:
    """Odds/price -> Decimal, or None for missing/<=1.0/garbage. Goes through
    ``str`` so float artefacts never leak into the boundary value."""
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return dec if dec > 1 else None


def _parse_market_time(raw: object) -> datetime | None:
    """ISO-8601 ``marketTime`` -> tz-aware UTC datetime. Trailing ``Z`` is
    normalised to ``+00:00``; a naive value is assumed UTC (never left naive)."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _epoch_ms_to_utc(pt: object) -> datetime | None:
    if not isinstance(pt, (int, float)):
        return None
    return datetime.fromtimestamp(pt / 1000.0, tz=UTC)


class _RunnerLadder:
    """Per-runner ATB ladder state. Stream ``batb``/``bdatb`` entries are
    per-LEVEL DELTAS (``[level, price, size]`` — only changed levels arrive;
    size 0 removes a level), NOT a full snapshot. Reading ``ladder[0]`` of a
    delta message as best-back mis-prices any market whose file carries ATB
    updates (parser audit 2026-07-03 F1): a level-1-only update was read as
    the best price, and a level-0 removal fell back to a stale ``ltp``.
    Maintain the ladder, read the lowest populated level."""

    __slots__ = ("levels", "ltp")

    def __init__(self) -> None:
        self.levels: dict[str, dict[int, Decimal]] = {"bdatb": {}, "batb": {}}
        self.ltp: Decimal | None = None

    def apply(self, rc: dict) -> None:
        for key in ("bdatb", "batb"):
            deltas = rc.get(key)
            if not isinstance(deltas, list):
                continue
            book = self.levels[key]
            for entry in deltas:
                if not isinstance(entry, (list, tuple)) or len(entry) < 3:
                    continue
                try:
                    level = int(entry[0])
                except (TypeError, ValueError):
                    continue
                price = _to_decimal(entry[1])
                try:
                    size = float(entry[2])
                except (TypeError, ValueError):
                    size = 0.0
                if price is None or size <= 0:
                    book.pop(level, None)
                else:
                    book[level] = price
        ltp = _to_decimal(rc.get("ltp"))
        if ltp is not None:
            self.ltp = ltp

    def best_back(self) -> Decimal | None:
        """Displayed ATB first, raw ATB second (lowest populated level = best
        available), last-traded price as the final fallback."""
        for key in ("bdatb", "batb"):
            book = self.levels[key]
            if book:
                return book[min(book)]
        return self.ltp


def _won(status: str) -> bool | None:
    if status == "WINNER":
        return True
    if status == "LOSER":
        return False
    return None


def parse_market_stream(lines: Iterable[str]) -> BetfairMarketClose | None:
    """Parse one market's ``mcm`` message sequence into a BetfairMarketClose.

    Returns None if no usable ``marketDefinition`` with runners is seen. Prices
    accumulate across messages; the pre-in-play snapshot is taken the first time
    ``inPlay`` flips true (using the marketDefinition BEFORE that message's own
    ``rc`` is applied, so it is genuinely the last pre-in-play price)."""
    market_id: str | None = None
    latest_def: dict | None = None
    ladders: dict[int, _RunnerLadder] = {}
    pre_inplay: dict[int, Decimal] | None = None
    in_play_utc: datetime | None = None

    def _prices_now() -> dict[int, Decimal]:
        return {
            sid: price
            for sid, ladder in ladders.items()
            if (price := ladder.best_back()) is not None
        }

    for line in lines:
        if not line or not line.strip():
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            logger.warning("skip betfair stream line: not JSON")
            continue
        if not isinstance(msg, dict) or msg.get("op") != "mcm":
            continue
        pt = msg.get("pt")
        for mc in msg.get("mc", []) or []:
            if not isinstance(mc, dict):
                continue
            if mc.get("id"):
                market_id = str(mc["id"])
            mdef = mc.get("marketDefinition")
            if isinstance(mdef, dict):
                latest_def = mdef
                # in-play turn: snapshot prices from PRIOR messages once only.
                if mdef.get("inPlay") is True and pre_inplay is None:
                    pre_inplay = _prices_now()
                    in_play_utc = _epoch_ms_to_utc(pt)
            for rc in mc.get("rc", []) or []:
                if not isinstance(rc, dict):
                    continue
                sid = rc.get("id")
                if not isinstance(sid, int):
                    continue
                ladders.setdefault(sid, _RunnerLadder()).apply(rc)

    if latest_def is None:
        return None
    raw_runners = latest_def.get("runners")
    if not isinstance(raw_runners, list) or not raw_runners:
        return None

    snapshot = pre_inplay if pre_inplay is not None else _prices_now()
    runners: list[BetfairRunner] = []
    for rd in raw_runners:
        if not isinstance(rd, dict):
            continue
        sid = rd.get("id")
        if not isinstance(sid, int):
            continue
        status = str(rd.get("status") or "")
        bsp = _to_decimal(rd.get("bsp"))
        close = bsp if bsp is not None else snapshot.get(sid)
        sort_priority = rd.get("sortPriority")
        runners.append(
            BetfairRunner(
                selection_id=sid,
                name=(rd.get("name") if isinstance(rd.get("name"), str) else None),
                sort_priority=(sort_priority if isinstance(sort_priority, int) else None),
                status=status,
                close_price=close,
                bsp=bsp,
                won=_won(status),
            )
        )
    if not runners:
        return None
    runners.sort(
        key=lambda r: (r.sort_priority if r.sort_priority is not None else 99, r.selection_id)
    )

    competition = None
    comp = latest_def.get("competition")
    if isinstance(comp, dict) and isinstance(comp.get("name"), str):
        competition = comp["name"]

    def _str_field(key: str) -> str | None:
        value = latest_def.get(key)
        return value if isinstance(value, str) else None

    return BetfairMarketClose(
        market_id=market_id or "",
        event_type_id=_str_field("eventTypeId"),
        event_name=_str_field("eventName"),
        competition=competition,
        market_type=_str_field("marketType"),
        kickoff_utc=_parse_market_time(latest_def.get("marketTime")),
        in_play_utc=in_play_utc,
        settled=str(latest_def.get("status") or "") == "CLOSED",
        bsp_reconciled=bool(latest_def.get("bspReconciled")),
        runners=tuple(runners),
        event_id=_str_field("eventId"),
        settled_time_utc=_parse_market_time(latest_def.get("settledTime")),
    )


@dataclass(frozen=True, slots=True)
class HdaClose:
    """Home/Draw/Away closing prices + result mapped to a (home, away) pick.

    ``draw_close`` is None for 2-way (basketball moneyline) markets. ``result``
    is H/D/A (D only possible for 3-way) from the WINNER runner."""

    home_close: Decimal | None
    draw_close: Decimal | None
    away_close: Decimal | None
    result: str | None  # "H" | "D" | "A" | None


def home_draw_away_close(
    market: BetfairMarketClose,
    home: str,
    away: str,
    *,
    aliases: AliasTable,
) -> HdaClose | None:
    """Map a market's runners to the (home, away) pick orientation.

    Home/away runners are matched by canonical (alias-resolved) name; the draw
    runner is the fixed ``DRAW_SELECTION_ID`` (or any runner canonicalising to
    "draw"). Returns None if either side cannot be uniquely identified — a wrong
    orientation would attach a wrong close (fake CLV, the cardinal sin)."""
    target_home = aliases.canonical(home)
    target_away = aliases.canonical(away)
    if not target_home or not target_away or target_home == target_away:
        return None

    home_runner: BetfairRunner | None = None
    away_runner: BetfairRunner | None = None
    draw_runner: BetfairRunner | None = None
    for r in market.runners:
        if r.selection_id == DRAW_SELECTION_ID or (
            r.name is not None and aliases.canonical(r.name) == "draw"
        ):
            draw_runner = r
            continue
        if r.name is None:
            continue
        canon = aliases.canonical(r.name)
        if canon == target_home:
            if home_runner is not None:
                return None  # ambiguous
            home_runner = r
        elif canon == target_away:
            if away_runner is not None:
                return None
            away_runner = r
    if home_runner is None or away_runner is None:
        return None

    result: str | None = None
    if home_runner.won:
        result = "H"
    elif away_runner.won:
        result = "A"
    elif draw_runner is not None and draw_runner.won:
        result = "D"
    return HdaClose(
        home_close=home_runner.close_price,
        draw_close=draw_runner.close_price if draw_runner is not None else None,
        away_close=away_runner.close_price,
        result=result,
    )


# Betfair fixed selection ids for the soccer OVER_UNDER_25 (Over/Under 2.5 goals)
# market — stable across every soccer OU 2.5 market (like DRAW_SELECTION_ID for
# MATCH_ODDS), so the Over/Under runners are identifiable even if a Basic-tier
# file omits runner names.
OVER_25_SELECTION_ID = 47973
UNDER_25_SELECTION_ID = 47972


@dataclass(frozen=True, slots=True)
class OverUnderClose:
    """Over/Under 2.5 closing prices + settled result.

    ``result`` is "O" (Over won, total goals >= 3), "U" (Under won), or None when
    the market is not settled."""

    over_close: Decimal | None
    under_close: Decimal | None
    result: str | None  # "O" | "U" | None


def over_under_close(market: BetfairMarketClose) -> OverUnderClose | None:
    """Map an OVER_UNDER_25 market's runners to its Over/Under close + result.

    Over/Under runners are identified by the fixed 2.5-line selection ids
    (:data:`OVER_25_SELECTION_ID` / :data:`UNDER_25_SELECTION_ID`) or, as a
    fallback, by a runner name starting with "over"/"under". Returns None if
    either side cannot be identified — a missing side would attach a wrong close
    (fake CLV, the cardinal sin)."""
    over_runner: BetfairRunner | None = None
    under_runner: BetfairRunner | None = None
    for r in market.runners:
        name = (r.name or "").strip().lower()
        if r.selection_id == OVER_25_SELECTION_ID or name.startswith("over"):
            if over_runner is not None:
                return None  # ambiguous
            over_runner = r
        elif r.selection_id == UNDER_25_SELECTION_ID or name.startswith("under"):
            if under_runner is not None:
                return None
            under_runner = r
    if over_runner is None or under_runner is None:
        return None
    result: str | None = None
    if over_runner.won:
        result = "O"
    elif under_runner.won:
        result = "U"
    return OverUnderClose(
        over_close=over_runner.close_price,
        under_close=under_runner.close_price,
        result=result,
    )


def event_name_home_away(event_name: str | None) -> tuple[str, str] | None:
    """Parse (home, away) from a Betfair ``eventName`` ("Home v Away").

    OVER_UNDER / ASIAN_HANDICAP soccer runners are "Over 2.5 Goals"/team-with-
    handicap, NOT plain team names, so the join key for those markets comes from
    the event name. Betfair uses the " v " separator consistently. Returns None
    when the name is absent or does not split into EXACTLY two sides (more than
    one " v " is ambiguous -> refuse rather than guess a wrong fixture)."""
    if not event_name:
        return None
    parts = event_name.split(" v ")
    if len(parts) != 2:
        return None
    home, away = parts[0].strip(), parts[1].strip()
    if not home or not away:
        return None
    return home, away


def load_betfair_dir(path: Path) -> list[BetfairMarketClose]:
    """Read-only: parse every operator-placed market file in ``path``.

    Reads ``*.bz2`` (compressed) and plain ``*.json`` / ``*.jsonl`` / ``*.txt``
    (one market per file). Unparseable files are skipped with a log line. An
    absent directory returns ``[]`` (the caller prints the operator instruction).
    Sorted by (kickoff, market_id) for determinism."""
    if not path.is_dir():
        return []
    markets: list[BetfairMarketClose] = []
    patterns = ("*.bz2", "*.json", "*.jsonl", "*.txt")
    for f in sorted({p for pat in patterns for p in path.glob(pat)}):
        try:
            if f.suffix == ".bz2":
                text = bz2.decompress(f.read_bytes()).decode("utf-8", errors="replace")
            else:
                text = f.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            logger.warning("skip betfair file %s: %s", f.name, type(exc).__name__)
            continue
        market = parse_market_stream(text.splitlines())
        if market is not None and market.runners:
            markets.append(market)
    markets.sort(key=lambda m: (m.kickoff_utc or datetime.min.replace(tzinfo=UTC), m.market_id))
    return markets


def _peek_market_def(lines: Iterable[str]) -> tuple[str | None, str | None]:
    """Cheap filter probe: scan a stream's lines for the FIRST ``marketDefinition``
    and return ``(eventTypeId, marketType)`` without building any runner state.

    Used to skip the ~95% of Betfair markets that are not soccer MATCH_ODDS
    BEFORE paying for a full :func:`parse_market_stream`. Stops at the first
    definition seen (Betfair emits the full definition in the opening message)."""
    for line in lines:
        if not line or not line.strip():
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(msg, dict) or msg.get("op") != "mcm":
            continue
        for mc in msg.get("mc", []) or []:
            if not isinstance(mc, dict):
                continue
            mdef = mc.get("marketDefinition")
            if isinstance(mdef, dict):
                et = mdef.get("eventTypeId")
                mt = mdef.get("marketType")
                return (et if isinstance(et, str) else None, mt if isinstance(mt, str) else None)
    return (None, None)


def load_betfair_tar(
    tar_path: Path,
    *,
    event_type_id: str = SOCCER_EVENT_TYPE_ID,
    market_type: str = "MATCH_ODDS",
    log_every: int = 50_000,
) -> list[BetfairMarketClose]:
    """Read-only: stream soccer MATCH_ODDS closes straight out of a Betfair
    historical ``.tar`` archive WITHOUT extracting its (1M+) members to disk.

    The Basic archive packs one market per ``.bz2`` member under
    ``BASIC/YYYY/Mon/Day/EVENTID/MARKETID.bz2``; only a fraction are soccer
    (``eventTypeId == "1"``) MATCH_ODDS. Each member is read, bz2-decompressed,
    and CHEAPLY peeked (:func:`_peek_market_def`); members that are not the
    requested sport+market type are skipped before any full parse. Matching
    members are handed to :func:`parse_market_stream` verbatim. The tar is read
    member-by-member in streaming mode (``r|*``) so the 5 GB archive is never
    buffered whole; only the kept (soccer MATCH_ODDS) markets accumulate.

    An absent tar returns ``[]`` (the caller prints the operator instruction).
    Sorted by (kickoff, market_id) for determinism — matching ``load_betfair_dir``.
    """
    if not tar_path.is_file():
        return []
    markets: list[BetfairMarketClose] = []
    scanned = 0
    try:
        # Streaming mode (r|*): sequential read, no random seeking, low memory.
        with tarfile.open(tar_path, mode="r|*") as tar:
            for member in tar:
                scanned += 1
                if log_every and scanned % log_every == 0:
                    logger.info(
                        "betfair tar scan: %d members read, %d soccer MATCH_ODDS kept",
                        scanned,
                        len(markets),
                    )
                if not member.isfile() or not member.name.endswith(".bz2"):
                    continue
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                try:
                    raw = bz2.decompress(fobj.read())
                except (OSError, ValueError, EOFError):
                    continue
                lines = raw.decode("utf-8", errors="replace").splitlines()
                et, mt = _peek_market_def(lines)
                if et != event_type_id or mt != market_type:
                    continue  # skip cheaply — non-matching market never fully parsed
                market = parse_market_stream(lines)
                # BSP inventory 2026-07-03: the peek reads the FIRST definition
                # but the record carries the LAST — a mixed-definition stream
                # must be classified by its final type or it lands in the
                # wrong cache (match_odds cache measured only 76% pure).
                if market is not None and market.runners and market.market_type == market_type:
                    markets.append(market)
    except (tarfile.TarError, OSError) as exc:
        logger.warning("betfair tar read aborted after %d members: %s", scanned, type(exc).__name__)
    logger.info(
        "betfair tar done: %d members read, %d soccer MATCH_ODDS markets", scanned, len(markets)
    )
    markets.sort(key=lambda m: (m.kickoff_utc or datetime.min.replace(tzinfo=UTC), m.market_id))
    return markets


def load_betfair_tar_by_type(
    tar_path: Path,
    *,
    event_type_id: str = SOCCER_EVENT_TYPE_ID,
    market_types: tuple[str, ...] = ("MATCH_ODDS",),
    log_every: int = 50_000,
) -> dict[str, list[BetfairMarketClose]]:
    """Read-only: ONE streaming pass over a Betfair Basic ``.tar`` that buckets
    every soccer market whose ``marketType`` is in ``market_types`` into a
    ``{market_type: [BetfairMarketClose, ...]}`` dict.

    Generalises :func:`load_betfair_tar` (which keeps a single type): the same
    cheap :func:`_peek_market_def` skip drops the ~97% of members that are not
    the requested sport+types BEFORE any full parse, but MULTIPLE types are
    extracted in a single ~5 GB sequential read so OVER_UNDER and ASIAN_HANDICAP
    can be cached alongside MATCH_ODDS without re-scanning. Each bucket is sorted
    by (kickoff, market_id) for determinism. An absent tar returns a dict of
    empty buckets (one per requested type). Memory-safe: only kept markets
    accumulate; the archive is never buffered whole.
    """
    wanted = set(market_types)
    buckets: dict[str, list[BetfairMarketClose]] = {mt: [] for mt in market_types}
    if not tar_path.is_file():
        return buckets
    scanned = 0
    kept = 0
    try:
        with tarfile.open(tar_path, mode="r|*") as tar:
            for member in tar:
                scanned += 1
                if log_every and scanned % log_every == 0:
                    logger.info(
                        "betfair tar scan: %d members read, %d kept (%s)",
                        scanned,
                        kept,
                        ",".join(market_types),
                    )
                if not member.isfile() or not member.name.endswith(".bz2"):
                    continue
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                try:
                    raw = bz2.decompress(fobj.read())
                except (OSError, ValueError, EOFError):
                    continue
                lines = raw.decode("utf-8", errors="replace").splitlines()
                et, mt = _peek_market_def(lines)
                if et != event_type_id or mt is None or mt not in wanted:
                    continue  # skip cheaply — non-matching member never fully parsed
                market = parse_market_stream(lines)
                # Bucket by the record's FINAL market_type, not the first-seen
                # peek (see load_betfair_tar — same mixed-definition hazard).
                if market is not None and market.runners and market.market_type in wanted:
                    buckets[str(market.market_type)].append(market)
                    kept += 1
    except (tarfile.TarError, OSError) as exc:
        logger.warning("betfair tar read aborted after %d members: %s", scanned, type(exc).__name__)
    for mt in buckets:
        buckets[mt].sort(
            key=lambda m: (m.kickoff_utc or datetime.min.replace(tzinfo=UTC), m.market_id)
        )
    logger.info("betfair tar done: %d members read, %d kept across %s", scanned, kept, market_types)
    return buckets


def _market_to_dict(m: BetfairMarketClose) -> dict:
    """BetfairMarketClose -> JSON-safe dict (Decimal->str, datetime->ISO-8601)."""
    return {
        "market_id": m.market_id,
        "event_type_id": m.event_type_id,
        "event_name": m.event_name,
        "competition": m.competition,
        "market_type": m.market_type,
        "kickoff_utc": m.kickoff_utc.isoformat() if m.kickoff_utc else None,
        "in_play_utc": m.in_play_utc.isoformat() if m.in_play_utc else None,
        "settled": m.settled,
        "bsp_reconciled": m.bsp_reconciled,
        "event_id": m.event_id,
        "settled_time_utc": m.settled_time_utc.isoformat() if m.settled_time_utc else None,
        "runners": [
            {
                "selection_id": r.selection_id,
                "name": r.name,
                "sort_priority": r.sort_priority,
                "status": r.status,
                "close_price": str(r.close_price) if r.close_price is not None else None,
                "bsp": str(r.bsp) if r.bsp is not None else None,
                "won": r.won,
            }
            for r in m.runners
        ],
    }


def _dt_from_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    dt = datetime.fromisoformat(raw)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _market_from_dict(d: dict) -> BetfairMarketClose:
    """Inverse of :func:`_market_to_dict` — rebuild Decimal/UTC-typed objects."""
    runners = tuple(
        BetfairRunner(
            selection_id=int(r["selection_id"]),
            name=r.get("name"),
            sort_priority=r.get("sort_priority"),
            status=str(r.get("status") or ""),
            close_price=_to_decimal(r.get("close_price")),
            bsp=_to_decimal(r.get("bsp")),
            won=r.get("won"),
        )
        for r in d.get("runners", [])
    )
    return BetfairMarketClose(
        market_id=str(d.get("market_id") or ""),
        event_type_id=d.get("event_type_id"),
        event_name=d.get("event_name"),
        competition=d.get("competition"),
        market_type=d.get("market_type"),
        kickoff_utc=_dt_from_iso(d.get("kickoff_utc")),
        in_play_utc=_dt_from_iso(d.get("in_play_utc")),
        settled=bool(d.get("settled")),
        bsp_reconciled=bool(d.get("bsp_reconciled")),
        runners=runners,
        event_id=(str(d["event_id"]) if d.get("event_id") else None),
        settled_time_utc=_dt_from_iso(d.get("settled_time_utc")),
    )


def write_market_cache(path: Path, markets: Iterable[BetfairMarketClose]) -> int:
    """Write parsed soccer MATCH_ODDS closes to a gzip-compressed JSONL cache.

    A DERIVED artefact (one JSON market per line) so the ~90-minute tar scan
    runs once; subsequent backtests load the cache via :func:`read_market_cache`.
    Returns the number of markets written. Read-only w.r.t. the raw archive."""
    import gzip

    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for m in markets:
            fh.write(json.dumps(_market_to_dict(m), separators=(",", ":")))
            fh.write("\n")
            count += 1
    return count


def read_market_cache(path: Path) -> list[BetfairMarketClose]:
    """Load the gzip JSONL market cache written by :func:`write_market_cache`.

    An absent cache returns ``[]``. Unparseable lines are skipped with a log
    line. Sorted by (kickoff, market_id) to match the loaders' determinism."""
    import gzip

    if not path.is_file():
        return []
    markets: list[BetfairMarketClose] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                markets.append(_market_from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                logger.warning("skip betfair cache line: %s", type(exc).__name__)
    markets.sort(key=lambda m: (m.kickoff_utc or datetime.min.replace(tzinfo=UTC), m.market_id))
    return markets


@dataclass(frozen=True, slots=True)
class JoinStats:
    """Data-quality counters for the football-data <- Betfair-close join."""

    n_fd_rows: int
    n_markets: int
    n_joined: int
    n_unmatched: int
    n_result_conflict: int


def _fd_date_to_utc(raw: str) -> datetime | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def attach_betfair_close(
    fd_rows: list[dict],
    markets: list[BetfairMarketClose],
    *,
    aliases: AliasTable,
    max_day_drift: int = 1,
) -> tuple[list[dict], JoinStats]:
    """Join Betfair sharp closes onto football-data pre-match rows.

    For each fd row (HomeTeam/AwayTeam/Date + pre-match Max/PS odds) find the
    UNIQUE Betfair market with the same canonical teams within ``max_day_drift``
    days (``app/resolution/matching.match_event``, ordered, strict — never
    guesses), then OVERWRITE the closing slots (``PSCH/PSCD/PSCA`` and
    ``MaxCH/MaxCD/MaxCA``) with the Betfair close decimal odds. Pre-match columns
    are left untouched. A row is DROPPED (data-quality gate) when the Betfair
    settled result disagrees with the football-data ``FTR`` — a mismatch means a
    bad join or a bad result and must not silently corrupt CLV.

    Returns (joined_rows, stats). Only successfully-joined rows are returned, so
    every returned row carries a genuine sharp close.
    """
    from app.resolution.matching import EventCandidate, match_event

    # Index candidates by kickoff DATE. match_event already filters by
    # max_day_drift, but scanning all ~59k candidates for every fd row is
    # O(fd_rows x markets) (~386M comparisons -> the join hang that killed the
    # run). Pre-bucketing by date means each fd row only matches the handful of
    # candidates within +/- max_day_drift calendar days — identical result set,
    # match_event still applies the precise drift inside the bucket.
    by_ref: dict[str, BetfairMarketClose] = {}
    by_date: dict[date, list[EventCandidate]] = {}
    for m in markets:
        if m.kickoff_utc is None:
            continue
        # home/away = the two non-draw runners, ordered by sortPriority (Betfair
        # lists the home side first in soccer MATCH_ODDS).
        non_draw = [
            r for r in m.runners if r.selection_id != DRAW_SELECTION_ID and r.name is not None
        ]
        if len(non_draw) < 2:
            continue
        home_r, away_r = non_draw[0], non_draw[1]
        ref = m.market_id or f"mkt-{len(by_ref)}"
        by_ref[ref] = m
        cand = EventCandidate(
            ref=ref, home=home_r.name or "", away=away_r.name or "", kickoff=m.kickoff_utc
        )
        by_date.setdefault(m.kickoff_utc.date(), []).append(cand)

    joined: list[dict] = []
    n_unmatched = 0
    n_conflict = 0
    for row in fd_rows:
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        kickoff = _fd_date_to_utc(row.get("Date") or "")
        if not home or not away or kickoff is None:
            n_unmatched += 1
            continue
        kdate = kickoff.date()
        local: list[EventCandidate] = []
        for off in range(-max_day_drift, max_day_drift + 1):
            local.extend(by_date.get(kdate + timedelta(days=off), ()))
        match = match_event(
            home, away, kickoff, local, aliases=aliases, max_day_drift=max_day_drift
        )
        if match is None:
            n_unmatched += 1
            continue
        close = home_draw_away_close(by_ref[match.ref], home, away, aliases=aliases)
        if close is None or close.home_close is None or close.away_close is None:
            n_unmatched += 1
            continue
        # Data-quality gate: Betfair result must agree with football-data FTR.
        ftr = (row.get("FTR") or "").strip()
        if close.result is not None and ftr in ("H", "D", "A") and close.result != ftr:
            n_conflict += 1
            continue
        new_row = dict(row)
        new_row["PSCH"] = str(close.home_close)
        new_row["PSCA"] = str(close.away_close)
        new_row["MaxCH"] = str(close.home_close)
        new_row["MaxCA"] = str(close.away_close)
        if close.draw_close is not None:
            new_row["PSCD"] = str(close.draw_close)
            new_row["MaxCD"] = str(close.draw_close)
        new_row["BetfairMarketId"] = match.ref
        # Exact UTC kickoff from the Betfair market definition — provenance for
        # the VALIDATION-ONLY ARCADIA anchor join (fd Date alone is day-grain).
        matched_ko = by_ref[match.ref].kickoff_utc
        if matched_ko is not None:
            new_row["_betfair_kickoff_utc"] = matched_ko.isoformat()
        joined.append(new_row)

    return joined, JoinStats(
        n_fd_rows=len(fd_rows),
        n_markets=len(markets),
        n_joined=len(joined),
        n_unmatched=n_unmatched,
        n_result_conflict=n_conflict,
    )


def attach_betfair_ou_close(
    fd_rows: list[dict],
    markets: list[BetfairMarketClose],
    *,
    aliases: AliasTable,
    max_day_drift: int = 1,
) -> tuple[list[dict], JoinStats]:
    """Join Betfair OVER_UNDER_25 sharp closes onto football-data pre-match rows.

    Mirrors :func:`attach_betfair_close` for the totals market. The OU market's
    home/away come from the Betfair ``eventName`` ("Home v Away") since its
    runners are "Over/Under 2.5 Goals", not team names; the strict
    ``match_event`` then finds the UNIQUE same-fixture football-data row within
    ``max_day_drift`` days. On a match the closing OU slots (``PC>2.5``/``PC<2.5``
    and ``MaxC>2.5``/``MaxC<2.5``) are OVERWRITTEN with the Betfair Over/Under
    close decimal odds; pre-match OU columns are untouched. A row is DROPPED
    (data-quality gate) when the Betfair Over/Under settled result disagrees with
    the football-data total goals (FTHG+FTAG: Over iff total >= 3).

    Returns (joined_rows, stats); only successfully-joined rows are returned, so
    every returned row carries a genuine sharp OU close.
    """
    from app.resolution.matching import EventCandidate, match_event

    by_ref: dict[str, BetfairMarketClose] = {}
    by_date: dict[date, list[EventCandidate]] = {}
    for m in markets:
        if m.kickoff_utc is None:
            continue
        teams = event_name_home_away(m.event_name)
        if teams is None:
            continue
        home_name, away_name = teams
        ref = m.market_id or f"ou-{len(by_ref)}"
        by_ref[ref] = m
        by_date.setdefault(m.kickoff_utc.date(), []).append(
            EventCandidate(ref=ref, home=home_name, away=away_name, kickoff=m.kickoff_utc)
        )

    joined: list[dict] = []
    n_unmatched = 0
    n_conflict = 0
    for row in fd_rows:
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        kickoff = _fd_date_to_utc(row.get("Date") or "")
        if not home or not away or kickoff is None:
            n_unmatched += 1
            continue
        kdate = kickoff.date()
        local: list[EventCandidate] = []
        for off in range(-max_day_drift, max_day_drift + 1):
            local.extend(by_date.get(kdate + timedelta(days=off), ()))
        match = match_event(
            home, away, kickoff, local, aliases=aliases, max_day_drift=max_day_drift
        )
        if match is None:
            n_unmatched += 1
            continue
        ou = over_under_close(by_ref[match.ref])
        if ou is None or ou.over_close is None or ou.under_close is None:
            n_unmatched += 1
            continue
        # Data-quality gate: Betfair Over/Under result must agree with the
        # football-data total goals (Over iff FTHG+FTAG >= 3).
        total: int | None
        try:
            total = int(row["FTHG"]) + int(row["FTAG"])
        except (KeyError, TypeError, ValueError):
            total = None
        if ou.result is not None and total is not None:
            fd_over = total >= 3
            bf_over = ou.result == "O"
            if fd_over != bf_over:
                n_conflict += 1
                continue
        new_row = dict(row)
        new_row["PC>2.5"] = str(ou.over_close)
        new_row["PC<2.5"] = str(ou.under_close)
        new_row["MaxC>2.5"] = str(ou.over_close)
        new_row["MaxC<2.5"] = str(ou.under_close)
        new_row["BetfairOuMarketId"] = match.ref
        matched_ou_ko = by_ref[match.ref].kickoff_utc
        if matched_ou_ko is not None:
            new_row["_betfair_kickoff_utc"] = matched_ou_ko.isoformat()
        joined.append(new_row)

    return joined, JoinStats(
        n_fd_rows=len(fd_rows),
        n_markets=len(markets),
        n_joined=len(joined),
        n_unmatched=n_unmatched,
        n_result_conflict=n_conflict,
    )


__all__ = [
    "BASKETBALL_EVENT_TYPE_ID",
    "DRAW_SELECTION_ID",
    "OVER_25_SELECTION_ID",
    "SOCCER_EVENT_TYPE_ID",
    "UNDER_25_SELECTION_ID",
    "BetfairMarketClose",
    "BetfairRunner",
    "HdaClose",
    "JoinStats",
    "OverUnderClose",
    "attach_betfair_close",
    "attach_betfair_ou_close",
    "event_name_home_away",
    "home_draw_away_close",
    "load_betfair_dir",
    "load_betfair_tar",
    "load_betfair_tar_by_type",
    "over_under_close",
    "parse_market_stream",
    "read_market_cache",
    "write_market_cache",
]
