"""Settle open picks from a ScoreBook — the IO half of settlement.

Invariants (kestrel-settlement discipline):
- Refuse silent-empty: an empty score book settles NOTHING and logs loudly.
- Atomic per run: result_tracking insert + pick.status flip happen in the
  caller's transaction; the insert is idempotent (uq_result_tracking_pick).
- Never guess: missing scores, ambiguous team matches, and unparseable
  selections leave the pick open (manual settlement via the API still works).
- Settling flips status away from 'alerted', which freezes the pick's CLV
  (app/clv_trueup.py only touches alerted rows).
"""

import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.edge.value import effective_odds
from app.edge.value_policy import ValuePolicy
from app.probabilities.devig import DevigMethod
from app.resolution.matching import fixture_pair_key, normalize_name, strip_live_status
from app.schemas.base import Outcome
from app.settlement.outcomes import (
    TENNIS_SETTLEMENT_CONVENTION,
    pick_pnl,
    pick_roi,
    settle_selection,
    settle_selection_retired,
    tennis_set_score_ungradeable,
)
from app.settlement.results import Completion, FinalScore, ScoreBook, load_scores
from app.storage.models import Event, League, ManualBetLog, Pick, ResultTracking, Sport, Team

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import Settings

logger = logging.getLogger(__name__)

# How far back the score book reaches. Anything older than this with no
# score available needs manual settlement anyway.
SCORE_WINDOW = timedelta(days=14)

# Settlement dedup guard: two same-sport events with the same UNORDERED
# normalized team pair whose kickoffs fall within this bound are treated as the
# SAME real fixture (cross-source duplicate event rows). 2h is a hard physical
# invariant for TEAM sports — two teams cannot start a second meeting within 2h
# (a game runs ~2h) — so a genuinely distinct fixture (home/away leg reversal,
# multi-day rematch, doubleheader, playoff G1/G2) can NEVER fall inside it. The
# guard can therefore only ever skip a true duplicate, never suppress a
# legitimate distinct pick.
DEDUP_FIXTURE_TOLERANCE = timedelta(hours=2)

# TENNIS (and any 1v1 sport) needs a WIDER window: a given player pair meets at
# most once per day, so there is no same-day distinct rematch to false-merge —
# yet an in-running fork's captured start can drift by hours (the live
# Lehecka/Zverev fork sat 2h47m off its clean twin, past the 2h team bound).
# Widen tennis so those forks still dedup; team sports keep the tight bound.
_DEDUP_TOLERANCE_BY_SPORT: dict[str, timedelta] = {"tennis": timedelta(hours=6)}


def _dedup_tolerance(sport_key: str | None) -> timedelta:
    """Same-fixture kickoff tolerance for the settlement dedup guard, per sport
    (tennis wider — see _DEDUP_TOLERANCE_BY_SPORT; team sports keep 2h)."""
    return _DEDUP_TOLERANCE_BY_SPORT.get(sport_key or "", DEDUP_FIXTURE_TOLERANCE)


# Full time + stoppage + a buffer for the results CSVs to update. Scores are
# matched by date anyway; the delay just avoids settling in-play fixtures.
SETTLE_DELAY = timedelta(hours=2)

# Per-sport settle-eligibility floors past kickoff (mirrors app/clv_trueup.py's
# _FINISHED_FLOOR, keyed by Sport.key). The generic 2h delay is soccer-sized;
# an NBA back-to-back's game-2 can still be IN PLAY at kickoff+2h while
# yesterday's same-pairing final sits in the score book — the ±1-day lookup
# tolerance would then settle game-2 with game-1's score. Holding basketball
# to >=4h means the exact-date final exists by eligibility time and
# ScoreBook.lookup prefers it. Sports not listed keep the caller's base delay.
_SPORT_SETTLE_DELAY: dict[str, timedelta] = {
    "basketball": timedelta(hours=4),
    "american_football": timedelta(hours=4, minutes=30),
    "tennis": timedelta(hours=6),
}


def settle_delay_for(sport_key: str, base: timedelta = SETTLE_DELAY) -> timedelta:
    """The settle-eligibility delay for a sport: the sport floor, never below
    the caller's base delay (a longer caller-supplied delay always holds)."""
    return max(base, _SPORT_SETTLE_DELAY.get(sport_key, base))


# Shared frozen no-op policy for the default-OFF path (ruff B008: no call in a
# function default). ValuePolicy is immutable, so one instance is safe to share.
_EMPTY_VALUE_POLICY = ValuePolicy()

# Picks on events whose kickoff was NEVER reported (starts_at NULL, "TBD")
# can neither auto-settle (settle_open_picks filters NULL out) nor stop
# revalidating — without a deadline they consume off-window scrape slots
# forever. After this age (from pick creation) the off-window selector stops
# re-pricing them (app/clv_trueup.py) and void_stale_null_kickoff_picks
# below closes them out.
STALE_NULL_KICKOFF_AGE = timedelta(days=14)


async def void_stale_null_kickoff_picks(
    session: AsyncSession,
    now: datetime,
    max_age: timedelta = STALE_NULL_KICKOFF_AGE,
) -> int:
    """Void alerted picks whose event STILL has no kickoff after `max_age`.

    Terminal-state convention (same shape as score settlement): an idempotent
    result_tracking row — outcome 'void', stake returned, pnl 0 — plus the
    status flip to 'settled', which freezes CLV and drops the pick from
    revalidation. /performance already counts 'void' outcomes; no new
    vocabulary. Returns the number of picks voided. Caller owns the
    transaction.
    """
    cutoff = now - max_age
    rows = (
        (
            await session.execute(
                select(Pick)
                .join(Event, Pick.event_id == Event.id)
                .where(
                    Pick.status == "alerted",
                    Event.starts_at.is_(None),
                    Pick.created_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    voided = 0
    for pick in rows:
        stake, odds, payout_bookmaker = await _stake_and_odds(session, pick)
        pnl = pick_pnl(Outcome.VOID, stake, odds)  # stake returned -> 0.00
        inserted = await session.execute(
            pg_insert(ResultTracking)
            .values(
                pick_id=pick.id,
                outcome=str(Outcome.VOID),
                pnl=pnl,
                roi=pick_roi(pnl, stake),
                settled_stake_amount=stake,
                settled_effective_odds=_effective_settlement_odds(odds, payout_bookmaker),
                settled_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_result_tracking_pick")
            .returning(ResultTracking.id)
        )
        if inserted.scalar_one_or_none() is None:
            continue  # already settled by a concurrent/manual path
        pick.status = "settled"
        logger.info(
            "voided pick %d (%s %s): kickoff still unknown %d days after pick "
            "creation — stake treated as returned",
            pick.id,
            pick.market,
            pick.selection,
            max_age.days,
        )
        voided += 1
    if voided:
        await session.flush()
        logger.info("settlement cycle: %d stale TBD picks voided", voided)
    return voided


#: result_tracking.note stamped on every no-result policy void (the bounded
#: expiry below AND the 15d scrape-window void), so provider-gap voids stay
#: distinguishable from score-based results in later audits.
EXPIRED_NO_RESULT_NOTE = "expired_no_result_source"


#: A KNOWN-kickoff pick this old with NO captured score can never settle: the
#: free results feed (SCORE_WINDOW) AND the finished-score scrape
#: (RESULTS_SCRAPE_WINDOW, 14d) have both stopped covering it. Without a void
#: path such a pick sits "awaiting result" forever (the class the prior results
#: commits fought). 15d = just past the 14d scrape window, so a still-scrapeable
#: pick is never voided early.
STALE_UNSETTLEABLE_AGE = timedelta(days=15)


async def void_unsettleable_known_kickoff_picks(
    session: AsyncSession,
    now: datetime,
    max_age: timedelta = STALE_UNSETTLEABLE_AGE,
) -> int:
    """Void alerted KNOWN-kickoff picks older than `max_age` with NO scraped
    score — feed + scrape windows are both exhausted, so they can never settle;
    voiding bounds the awaiting-result tail. Same idempotent terminal shape as
    void_stale_null_kickoff_picks (result row outcome='void', status 'settled').
    A pick still inside the scrape window, or one that already carries a scraped
    score (it settles by score), is left alone. Caller owns the transaction."""
    cutoff = now - max_age
    home, away = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(
                Pick,
                home.name,
                away.name,
                Event.starts_at,
                Event.sport_id,
                Sport.key,
            )
            .join(Event, Pick.event_id == Event.id)
            .join(home, Event.home_team_id == home.id)
            .join(away, Event.away_team_id == away.id)
            .join(Sport, Event.sport_id == Sport.id)
            .where(
                Pick.status == "alerted",
                Event.starts_at.is_not(None),
                Event.starts_at < cutoff,
                Event.scraped_home_score.is_(None),
            )
        )
    ).all()
    voided = 0
    superseded = 0
    for pick, home_name, away_name, starts_at, sport_id, sport_key in rows:
        pair = fixture_pair_key(home_name, away_name)
        if pair is not None:
            await _lock_settlement_instrument(
                session,
                sport_id=sport_id,
                market=pick.market,
                market_detail=pick.market_detail,
                selection=pick.selection,
                model_version_id=pick.model_version_id,
                target_pair=pair,
            )
        if pair is not None and await _settled_sibling_exists(
            session,
            pick_id=pick.id,
            event_id=pick.event_id,
            sport_id=sport_id,
            starts_at=starts_at,
            market=pick.market,
            market_detail=pick.market_detail,
            selection=pick.selection,
            model_version_id=pick.model_version_id,
            target_pair=pair,
            sport_key=sport_key,
        ):
            pick.status = "superseded"
            superseded += 1
            logger.info(
                "stale-known settlement: superseded duplicate pick %d (%s %s)",
                pick.id,
                pick.market,
                pick.selection,
            )
            continue
        stake, odds, payout_bookmaker = await _stake_and_odds(session, pick)
        pnl = pick_pnl(Outcome.VOID, stake, odds)  # stake returned -> 0.00
        inserted = await session.execute(
            pg_insert(ResultTracking)
            .values(
                pick_id=pick.id,
                outcome=str(Outcome.VOID),
                pnl=pnl,
                roi=pick_roi(pnl, stake),
                settled_stake_amount=stake,
                settled_effective_odds=_effective_settlement_odds(odds, payout_bookmaker),
                note=EXPIRED_NO_RESULT_NOTE,
                settled_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_result_tracking_pick")
            .returning(ResultTracking.id)
        )
        if inserted.scalar_one_or_none() is None:
            continue  # already settled by a concurrent/manual path
        pick.status = "settled"
        logger.info(
            "voided pick %d (%s %s): no result %d days after kickoff, past the "
            "scrape window — stake treated as returned",
            pick.id,
            pick.market,
            pick.selection,
            max_age.days,
        )
        voided += 1
    if voided or superseded:
        await session.flush()
        logger.info(
            "settlement cycle: %d unsettleable known-kickoff picks voided, "
            "%d duplicates superseded",
            voided,
            superseded,
        )
    return voided


async def report_and_expire_no_result_picks(
    session: AsyncSession,
    book: ScoreBook,
    now: datetime,
    *,
    delay: timedelta = SETTLE_DELAY,
    expire_after: timedelta | None,
) -> int:
    """Provider-gap visibility + bounded expiry for NO-candidate-result picks.

    ``book`` must be the UNION of every result source the cycle consulted
    (feed + ESPN + scraped): a pick counts as a provider gap only when that
    union holds NO candidate score for its fixture. Two actions:

    1. REPORT — one INFO line per cycle aggregating the gap backlog by
       (sport, league), top-10 by count, so leagues no provider covers are
       visible instead of silently skewing open-exposure views.
    2. EXPIRE — with ``expire_after`` set (settlement_expire_days > 0), a gap
       pick whose kickoff is older than that bound is settled as VOID with
       ``result_tracking.note=EXPIRED_NO_RESULT_NOTE`` (stake returned,
       pnl 0), terminally exiting the open set. ``None`` disables expiry;
       the report still runs.

    Safety gates (mirror of the engine's no-result vs not-settleable split):
    - A pick with ANY candidate score — even one that grades to a refusal
      (unparseable selection, tennis set-score game line) — is NEVER touched
      here: a pending/ambiguous result belongs to manual settlement.
    - An EMPTY book is a provider outage, not a quiet day (silent-empty
      guard): nothing is reported or expired.
    - The cross-source dedup guard applies before any void write, so an
      expiring twin of an already-settled fixture supersedes instead of
      minting a second P&L row.

    Returns the number of picks expired. The caller owns the transaction.
    """
    if len(book) == 0:
        return 0
    home, away = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(
                Pick,
                home.name,
                away.name,
                Event.starts_at,
                Event.sport_id,
                Sport.key,
                League.name,
            )
            .join(Event, Pick.event_id == Event.id)
            .join(home, Event.home_team_id == home.id)
            .join(away, Event.away_team_id == away.id)
            .join(Sport, Event.sport_id == Sport.id)
            .join(League, Event.league_id == League.id)
            # NULL starts_at is excluded by SQL three-valued logic — the
            # TBD-kickoff class has its own policy (void_stale_null_kickoff).
            .where(Pick.status == "alerted", Event.starts_at <= now - delay)
        )
    ).all()
    gap_counts: Counter[tuple[str, str]] = Counter()
    gap_rows = []
    for pick, home_name, away_name, starts_at, sport_id, sport_key, league_name in rows:
        if starts_at > now - settle_delay_for(sport_key, delay):
            continue  # sport floor not reached — the game may still be in play
        if book.lookup(home_name, away_name, starts_at) is not None:
            continue  # candidate result exists -> the grading paths own it
        gap_counts[(sport_key, league_name)] += 1
        gap_rows.append((pick, home_name, away_name, starts_at, sport_id, sport_key))
    if gap_counts:
        top = ", ".join(
            f"{sport}/{league} {count}" for (sport, league), count in gap_counts.most_common(10)
        )
        logger.info(
            "settlement cycle: %d picks past kickoff with no result source (top: %s)",
            sum(gap_counts.values()),
            top,
        )
    if expire_after is None:
        return 0
    cutoff = now - expire_after
    expired = 0
    superseded = 0
    for pick, home_name, away_name, starts_at, sport_id, sport_key in gap_rows:
        if starts_at >= cutoff:
            continue
        pair = fixture_pair_key(home_name, away_name)
        if pair is not None:
            await _lock_settlement_instrument(
                session,
                sport_id=sport_id,
                market=pick.market,
                market_detail=pick.market_detail,
                selection=pick.selection,
                model_version_id=pick.model_version_id,
                target_pair=pair,
            )
            if await _settled_sibling_exists(
                session,
                pick_id=pick.id,
                event_id=pick.event_id,
                sport_id=sport_id,
                starts_at=starts_at,
                market=pick.market,
                market_detail=pick.market_detail,
                selection=pick.selection,
                model_version_id=pick.model_version_id,
                target_pair=pair,
                sport_key=sport_key,
            ):
                pick.status = "superseded"
                superseded += 1
                logger.info(
                    "no-result expiry: superseded duplicate pick %d (%s %s)",
                    pick.id,
                    pick.market,
                    pick.selection,
                )
                continue
        stake, odds, payout_bookmaker = await _stake_and_odds(session, pick)
        pnl = pick_pnl(Outcome.VOID, stake, odds)  # stake returned -> 0.00
        inserted = await session.execute(
            pg_insert(ResultTracking)
            .values(
                pick_id=pick.id,
                outcome=str(Outcome.VOID),
                pnl=pnl,
                roi=pick_roi(pnl, stake),
                settled_stake_amount=stake,
                settled_effective_odds=_effective_settlement_odds(odds, payout_bookmaker),
                note=EXPIRED_NO_RESULT_NOTE,
                settled_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_result_tracking_pick")
            .returning(ResultTracking.id)
        )
        if inserted.scalar_one_or_none() is None:
            continue  # already settled by a concurrent/manual path
        pick.status = "settled"
        logger.info(
            "expired pick %d (%s %s): no result source %d days after kickoff — "
            "voided, stake treated as returned (note=%s)",
            pick.id,
            pick.market,
            pick.selection,
            expire_after.days,
            EXPIRED_NO_RESULT_NOTE,
        )
        expired += 1
    if expired or superseded:
        await session.flush()
        logger.info(
            "settlement cycle: %d no-result picks expired (voided), %d duplicates superseded",
            expired,
            superseded,
        )
    return expired


# Per-pick "not settleable" warning dedup (audit S 2026-07-26): the 30s cycle
# re-warned EVERY unsettleable pick EVERY cycle (~167k warnings/6h). First
# sighting of a (pick id, reason) warns; repeats stay silent until the REASON
# changes or the pick grades (its entry is then dropped, so a later regression
# re-warns). In-memory only — a restart re-warns each pick once, which is
# acceptable; no schema change.
_UNSETTLEABLE_WARNED: dict[int, str] = {}


def reset_unsettleable_warning_state() -> None:
    """Forget which unsettleable picks were already warned about (tests +
    operator tooling)."""
    _UNSETTLEABLE_WARNED.clear()


def _unsettleable_summary(counts: Counter[str]) -> str:
    """The per-cycle summary line body: 'N picks unsettleable (M btts, ...)'."""
    total = sum(counts.values())
    breakdown = ", ".join(f"{n} {market}" for market, n in sorted(counts.items()))
    return f"{total} picks unsettleable ({breakdown})"


# Trailing signed handicap token of a spreads selection ("Alpha FC -1.5").
# Mirror of outcomes._SIGNED_LINE_RE (kept here so the dedup key needs no
# settlement import).
_SELECTION_LINE_RE = re.compile(r"[+-]\d+(?:\.\d+)?")


def _selection_dedup_key(selection: str) -> str:
    """Spelling-insensitive, LINE-PRESERVING equivalence key for one selection.

    Fixture identity in the dedup guard is normalized (fixture_pair_key), but
    Pick.selection embeds the SOURCE'S team spelling verbatim ("Arsenal" vs
    "Arsenal FC", diacritics, a legacy "[In Running]" suffix) — raw string
    equality is blind exactly where cross-source spellings diverge, so both
    twins settle and pnl/ROI/CLV double-count. Fold team-named selections with
    the SAME normalizer the resolution layer uses, while keeping every
    line/number token VERBATIM (normalize_name strips signs and punctuation,
    so it must never see a line — merging "+1.5" with "-1.5" would supersede a
    genuinely distinct bet):

    - "Team -1.5" (spreads): normalized team + the verbatim signed line;
    - "A or B" / "A or Draw" (double_chance): normalized parts, order-free;
    - anything else carrying a digit (totals "Over 2.5", EH "Draw (+1)"):
      kept verbatim — the digits ARE the bet;
    - plain team/word selections (h2h, dnb, BTTS): normalized.

    A part that normalizes to "" falls back to the raw string — fail toward
    NOT merging, the same behavior as the old exact comparison.
    """
    base = strip_live_status(selection)
    team, _, raw_line = base.rpartition(" ")
    if team and _SELECTION_LINE_RE.fullmatch(raw_line):
        norm = normalize_name(strip_live_status(team))
        return f"{norm} {raw_line}" if norm else base
    if " or " in base:
        parts = [normalize_name(part) for part in base.split(" or ")]
        if all(parts):
            return " or ".join(sorted(parts))
        return base
    if any(ch.isdigit() for ch in base):
        return base
    return normalize_name(base) or base


@dataclass(frozen=True, slots=True)
class _SettlementCandidate:
    """One score-aware open-pick row prepared for the settlement decision."""

    pick: Pick
    home_name: str
    away_name: str
    starts_at: datetime
    external_ref: str
    sport_key: str
    sport_id: int
    pair: frozenset[str] | None
    score: FinalScore | None
    tennis_game_line_set_score: bool


_SETTLED_SIBLING_PREFETCH_CHUNK_SIZE = 1_000

type _SettlementFingerprint = tuple[int, str, str | None, int, str, frozenset[str]]


def _settlement_fingerprint(
    *,
    sport_id: int,
    market: str,
    market_detail: str | None,
    model_version_id: int,
    selection: str,
    pair: frozenset[str],
) -> _SettlementFingerprint:
    return (
        sport_id,
        market,
        market_detail,
        model_version_id,
        _selection_dedup_key(selection),
        pair,
    )


async def _prefetch_settled_sibling_candidate_ids(
    session: AsyncSession,
    candidates: Sequence[_SettlementCandidate],
) -> set[int]:
    """Bulk hint for candidates that may already have a settled source twin.

    One bounded settled-results query per candidate chunk replaces the old two
    round trips per open pick. SQL constrains sport, kickoff range, market,
    market_detail, and model; Python then applies the exact spelling-insensitive
    selection and fixture fingerprints used by the authoritative check. No
    candidate×settled SQL join is emitted, so query output grows with settled
    rows rather than their Cartesian product with a large open backlog.

    This is deliberately only a hint: every positive is rechecked while holding
    the advisory lock before a status change, and every score-bearing automatic
    settlement lock/rechecks independently of this snapshot. Chunking bounds
    query parameters and in-memory maps for an unexpectedly large backlog.
    """
    eligible = [candidate for candidate in candidates if candidate.pair is not None]
    possible: set[int] = set()
    for offset in range(0, len(eligible), _SETTLED_SIBLING_PREFETCH_CHUNK_SIZE):
        chunk = eligible[offset : offset + _SETTLED_SIBLING_PREFETCH_CHUNK_SIZE]
        windows = [
            (
                candidate.starts_at - _dedup_tolerance(candidate.sport_key),
                candidate.starts_at + _dedup_tolerance(candidate.sport_key),
            )
            for candidate in chunk
        ]
        market_details = {candidate.pick.market_detail for candidate in chunk}
        non_null_details = sorted(detail for detail in market_details if detail is not None)
        detail_predicates = []
        if non_null_details:
            detail_predicates.append(Pick.market_detail.in_(non_null_details))
        if None in market_details:
            detail_predicates.append(Pick.market_detail.is_(None))

        home_t, away_t = aliased(Team), aliased(Team)
        rows = (
            await session.execute(
                select(
                    Pick.id,
                    Pick.event_id,
                    Event.sport_id,
                    Pick.market,
                    Pick.market_detail,
                    Pick.model_version_id,
                    Pick.selection,
                    Event.starts_at,
                    home_t.name,
                    away_t.name,
                )
                .select_from(ResultTracking)
                .join(Pick, ResultTracking.pick_id == Pick.id)
                .join(Event, Pick.event_id == Event.id)
                .join(home_t, Event.home_team_id == home_t.id)
                .join(away_t, Event.away_team_id == away_t.id)
                .where(
                    Event.sport_id.in_(sorted({candidate.sport_id for candidate in chunk})),
                    Event.starts_at.is_not(None),
                    Event.starts_at >= min(window_start for window_start, _ in windows),
                    Event.starts_at <= max(window_end for _, window_end in windows),
                    Pick.market.in_(sorted({candidate.pick.market for candidate in chunk})),
                    or_(*detail_predicates),
                    Pick.model_version_id.in_(
                        sorted({candidate.pick.model_version_id for candidate in chunk})
                    ),
                )
            )
        ).all()

        settled_by_fingerprint: dict[_SettlementFingerprint, list[tuple[int, int, datetime]]] = {}
        for (
            sibling_pick_id,
            sibling_event_id,
            sibling_sport_id,
            sibling_market,
            sibling_market_detail,
            sibling_model_version_id,
            sibling_selection,
            sibling_starts_at,
            sibling_home,
            sibling_away,
        ) in rows:
            sibling_pair = fixture_pair_key(sibling_home, sibling_away)
            if sibling_pair is None:
                continue
            fingerprint = _settlement_fingerprint(
                sport_id=sibling_sport_id,
                market=sibling_market,
                market_detail=sibling_market_detail,
                model_version_id=sibling_model_version_id,
                selection=sibling_selection,
                pair=sibling_pair,
            )
            settled_by_fingerprint.setdefault(fingerprint, []).append(
                (sibling_pick_id, sibling_event_id, sibling_starts_at)
            )

        for candidate in chunk:
            assert candidate.pair is not None  # eligible partition invariant
            fingerprint = _settlement_fingerprint(
                sport_id=candidate.sport_id,
                market=candidate.pick.market,
                market_detail=candidate.pick.market_detail,
                model_version_id=candidate.pick.model_version_id,
                selection=candidate.pick.selection,
                pair=candidate.pair,
            )
            tolerance = _dedup_tolerance(candidate.sport_key)
            if any(
                sibling_pick_id != candidate.pick.id
                and sibling_event_id != candidate.pick.event_id
                and abs(sibling_starts_at - candidate.starts_at) <= tolerance
                for sibling_pick_id, sibling_event_id, sibling_starts_at in (
                    settled_by_fingerprint.get(fingerprint, ())
                )
            ):
                possible.add(candidate.pick.id)
    return possible


async def _settled_sibling_exists(
    session: AsyncSession,
    *,
    pick_id: int,
    event_id: int,
    sport_id: int,
    starts_at: datetime,
    market: str,
    market_detail: str | None,
    selection: str,
    model_version_id: int,
    target_pair: frozenset[str],
    sport_key: str | None = None,
) -> bool:
    """True when an equivalent pick (same instrument+model_version) on a
    DIFFERENT event of the SAME real fixture is ALREADY settled — so settling
    this pick again would double-count real-money pnl/ROI/CLV.

    "Same real fixture" = same sport, kickoff within the per-sport
    _dedup_tolerance (tennis wider — a 1v1 pair meets once/day; team sports keep
    the tight 2h), and the same UNORDERED fixture_pair_key (which folds a
    ``[In Running]`` live-fork onto its clean twin and preserves women's/youth
    markers). ``market_detail`` is compared NULL-safely because it identifies
    the canonical submarket/line; two selections with the same display label
    but different details are distinct instruments. Selections are compared
    via _selection_dedup_key, NOT raw string equality — a cross-source twin
    spells the same team differently ("Arsenal" vs "Arsenal FC"), while
    distinct lines/handicaps stay distinct. Only rows that already carry a
    result_tracking row are considered (the settled sibling). Fail-safe: the
    same-teams + bounded-time match cannot hit a genuinely distinct fixture,
    so a match is only ever a cross-source duplicate."""
    tol = _dedup_tolerance(sport_key)
    home_t, away_t = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(Pick.selection, home_t.name, away_t.name)
            .select_from(Pick)
            .join(ResultTracking, ResultTracking.pick_id == Pick.id)
            .join(Event, Pick.event_id == Event.id)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .where(
                Pick.id != pick_id,
                Pick.event_id != event_id,
                Pick.market == market,
                Pick.market_detail.is_not_distinct_from(market_detail),
                Pick.model_version_id == model_version_id,
                Event.sport_id == sport_id,
                Event.starts_at.is_not(None),
                Event.starts_at >= starts_at - tol,
                Event.starts_at <= starts_at + tol,
            )
        )
    ).all()
    sel_key = _selection_dedup_key(selection)
    return any(
        _selection_dedup_key(sib_sel) == sel_key and fixture_pair_key(h, a) == target_pair
        for sib_sel, h, a in rows
    )


async def _lock_settlement_instrument(
    session: AsyncSession,
    *,
    sport_id: int,
    market: str,
    market_detail: str | None,
    selection: str,
    model_version_id: int,
    target_pair: frozenset[str],
) -> None:
    """Serialize settlement of one canonical cross-source instrument.

    The settled-sibling check and result insert must be one atomic decision.
    A row lock cannot provide that invariant because sibling picks live on
    different rows, so concurrent workers could both observe "no result" and
    insert. A PostgreSQL transaction-scoped advisory lock gives every source
    twin the same mutex until commit; the second worker then observes the first
    worker's committed result and supersedes its duplicate.

    Kickoff is deliberately absent from the key: source forks can disagree on
    kickoff while still describing the same fixture. The bounded kickoff test
    remains in ``_settled_sibling_exists`` and prevents rematches from merging;
    omitting it here can only over-serialize unrelated rematches briefly.
    """
    identity = repr(
        (
            "settlement-instrument-v1",
            sport_id,
            tuple(sorted(target_pair)),
            market,
            market_detail,
            _selection_dedup_key(selection),
            model_version_id,
        )
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": identity},
    )


async def _supersede_if_settled_sibling(
    session: AsyncSession, candidate: _SettlementCandidate
) -> bool:
    """Lock+authoritatively recheck one candidate; supersede only on proof."""
    if candidate.pair is None:
        return False
    pick = candidate.pick
    await _lock_settlement_instrument(
        session,
        sport_id=candidate.sport_id,
        market=pick.market,
        market_detail=pick.market_detail,
        selection=pick.selection,
        model_version_id=pick.model_version_id,
        target_pair=candidate.pair,
    )
    if not await _settled_sibling_exists(
        session,
        pick_id=pick.id,
        event_id=pick.event_id,
        sport_id=candidate.sport_id,
        starts_at=candidate.starts_at,
        market=pick.market,
        market_detail=pick.market_detail,
        selection=pick.selection,
        model_version_id=pick.model_version_id,
        target_pair=candidate.pair,
        sport_key=candidate.sport_key,
    ):
        return False
    # Terminal duplicate: no result row, so it never enters pnl/ROI/CLV and
    # never appears pending again.
    pick.status = "superseded"
    logger.info(
        "settlement: superseded duplicate pick %d (%s %s) — a sibling of "
        "the same fixture is already settled (cross-source event dedup)",
        pick.id,
        pick.market,
        pick.selection,
    )
    return True


async def settle_open_picks(
    session: AsyncSession,
    book: ScoreBook,
    now: datetime,
    delay: timedelta = SETTLE_DELAY,
    devig_method: DevigMethod | None = None,
    use_pinnacle_archive: bool = False,
    use_betfair_exchange: bool = False,
    sharp_close_echo_gate: bool = True,
    value_policy: ValuePolicy = _EMPTY_VALUE_POLICY,
) -> int:
    """Settle every alerted pick whose event finished and has a known score.

    Returns the number of picks settled. The caller owns the transaction.

    Closing-line source preference: when `devig_method` is given, every pick
    that settles gets its closing fair/CLV recomputed from our OWN
    odds_snapshots change-only history (finalize_closing_from_snapshots) —
    same devig, same anchoring rules, effective odds both sides. When that
    finds no coverage (event not scraped near kickoff, no anchorable close
    set — the common case until snapshots accumulate), the pick KEEPS the
    close the live/match-page re-scrape revalidation last wrote: the
    fallback. `devig_method=None` skips the snapshot path entirely.
    """
    if len(book) == 0:
        logger.error("settlement: empty score book — refusing to settle (silent-empty guard)")
        return 0
    # Lazy import: app.clv_trueup imports STALE_NULL_KICKOFF_AGE from this
    # module at import time — a top-level import here would be circular.
    from app.clv_trueup import finalize_closing_from_snapshots

    home, away = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(
                Pick,
                home.name,
                away.name,
                Event.starts_at,
                Event.external_ref,
                Sport.key,
                Event.sport_id,
            )
            .join(Event, Pick.event_id == Event.id)
            .join(home, Event.home_team_id == home.id)
            .join(away, Event.away_team_id == away.id)
            .join(Sport, Event.sport_id == Sport.id)
            # NULL starts_at (kickoff unknown) is filtered out here by SQL
            # three-valued logic — correct: never auto-settle a game we
            # cannot prove has finished. Manual settlement stays available.
            .where(Pick.status == "alerted", Event.starts_at <= now - delay)
        )
    ).all()

    # Resolve scores once, before deciding whether a row needs the expensive
    # advisory-lock + authoritative sibling query. Rows with no score — and
    # tennis game lines for which the available final is only a set score —
    # cannot write P&L, so a negative bulk sibling snapshot safely skips both
    # per-row round trips. A concurrently-settled twin is picked up next cycle;
    # the manual path itself still lock/rechecks before writing any result.
    candidates: list[_SettlementCandidate] = []
    for pick, home_name, away_name, starts_at, external_ref, sport_key, sport_id in rows:
        if starts_at > now - settle_delay_for(sport_key, delay):
            continue  # sport floor not reached — the game may still be in play
        score = book.lookup(home_name, away_name, starts_at)
        tennis_game_line_set_score = (
            score is not None
            and score.completion == "full"
            and sport_key == "tennis"
            and tennis_set_score_ungradeable(
                pick.market,
                pick.selection,
                score.home_score,
                score.away_score,
            )
        )
        candidates.append(
            _SettlementCandidate(
                pick=pick,
                home_name=home_name,
                away_name=away_name,
                starts_at=starts_at,
                external_ref=external_ref,
                sport_key=sport_key,
                sport_id=sport_id,
                pair=fixture_pair_key(home_name, away_name),
                score=score,
                tennis_game_line_set_score=tennis_game_line_set_score,
            )
        )

    gradeable = [
        candidate
        for candidate in candidates
        if candidate.score is not None and not candidate.tennis_game_line_set_score
    ]
    deferred = [
        candidate
        for candidate in candidates
        if candidate.score is None or candidate.tennis_game_line_set_score
    ]

    settled = 0
    superseded = 0
    tennis_manual_ids: list[int] = []
    unsettleable_counts: Counter[str] = Counter()

    # Phase 1: every row capable of writing a result ALWAYS lock+rechecks,
    # independent of any snapshot. Doing real work first also preserves the old
    # same-pass behavior when only one cross-source twin has a matched score:
    # its scoreless twin is visible to the deferred bulk snapshot below.
    for candidate in gradeable:
        pick = candidate.pick
        score = candidate.score
        assert score is not None  # gradeable partition invariant
        if await _supersede_if_settled_sibling(session, candidate):
            superseded += 1
            continue
        if await _settle_one(
            session,
            pick,
            candidate.home_name,
            candidate.away_name,
            score.home_score,
            score.away_score,
            now,
            completion=score.completion,
            winner_side=score.winner_side,
            sport_key=candidate.sport_key,
            unsettleable_counts=unsettleable_counts,
        ):
            settled += 1
            # Snapshot close AFTER the status flip, same transaction: the
            # pick is now frozen for revalidation, so what we write here is
            # final. A False return keeps the re-scrape close untouched.
            # Walkover/abandonment voids skip it: a market that never played
            # out has no legitimate close (prices drift toward suspension).
            if devig_method is not None and score.completion != "void":
                await finalize_closing_from_snapshots(
                    session,
                    pick,
                    candidate.external_ref,
                    candidate.starts_at,
                    devig_method,
                    use_pinnacle_archive=use_pinnacle_archive,
                    use_betfair_exchange=use_betfair_exchange,
                    sharp_close_echo_gate=sharp_close_echo_gate,
                    value_policy=value_policy,
                )

    # Make phase-1 result/status writes visible before taking the bulk snapshot.
    # ResultTracking inserts execute immediately, but the explicit flush makes
    # this sequencing invariant independent of future _settle_one internals.
    if settled or superseded:
        await session.flush()

    # Phase 2: rows that cannot write automatic P&L only lock+exact-recheck when
    # the bounded bulk hint found a possible settled sibling. A negative hint is
    # safe: a concurrent manual settlement is discovered next cycle, while this
    # row writes no result in the meantime.
    possible_sibling_ids = await _prefetch_settled_sibling_candidate_ids(session, deferred)
    for candidate in deferred:
        pick = candidate.pick
        if pick.id in possible_sibling_ids and await _supersede_if_settled_sibling(
            session, candidate
        ):
            superseded += 1
            continue
        if candidate.tennis_game_line_set_score:
            # The automatic caller aggregates these deterministic refusals.
            # _settle_one retains its per-request guard/log for manual/direct
            # callers, preserving the manual safety path.
            tennis_manual_ids.append(pick.id)

    if tennis_manual_ids:
        logger.info(
            "settlement refusal summary: reason=tennis_game_line_set_score "
            "count=%d sample_pick_ids=%s — left open for manual settlement",
            len(tennis_manual_ids),
            sorted(tennis_manual_ids)[:3],
        )
    if unsettleable_counts:
        # One line per cycle replaces the old per-pick re-warns (dedup above).
        logger.warning("settlement cycle: %s", _unsettleable_summary(unsettleable_counts))
    if settled or superseded:
        await session.flush()  # status flips visible to the caller's transaction
        logger.info(
            "settlement cycle: %d picks settled, %d duplicates superseded",
            settled,
            superseded,
        )
    return settled


async def settle_event_picks(
    session: AsyncSession,
    event_id: int,
    home_score: int,
    away_score: int,
    now: datetime,
    *,
    devig_method: DevigMethod | None = None,
    use_pinnacle_archive: bool = False,
    use_betfair_exchange: bool = False,
    sharp_close_echo_gate: bool = True,
    value_policy: ValuePolicy = _EMPTY_VALUE_POLICY,
) -> tuple[int, int]:
    """Settle every open pick of one event from a user-entered final score
    (the manual path for leagues without a free results feed).

    When `devig_method` is given, each settled pick ALSO gets its closing
    fair/CLV finalized from our own odds_snapshots — a mirror of the auto path
    (settle_open_picks). Without it the snapshot close is skipped, so a
    manually-settled pick would never enter the sharp-CLV subset (audit #4).

    Applies the SAME settled-sibling dedup guard as the auto path: a pick whose
    cross-source twin already settled is flipped to 'superseded' (counted into
    `skipped`), never hand-settled into a second result_tracking row.

    Returns (settled, skipped). The caller owns the transaction.
    """
    from app.clv_trueup import finalize_closing_from_snapshots  # lazy: circular

    home, away = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(
                Pick,
                home.name,
                away.name,
                Event.external_ref,
                Event.starts_at,
                Event.sport_id,
                Sport.key,
            )
            .join(Event, Pick.event_id == Event.id)
            .join(home, Event.home_team_id == home.id)
            .join(away, Event.away_team_id == away.id)
            .join(Sport, Event.sport_id == Sport.id)
            .where(Pick.status == "alerted", Pick.event_id == event_id)
        )
    ).all()
    settled = skipped = superseded = 0
    for pick, home_name, away_name, external_ref, starts_at, sport_id, sport_key in rows:
        # DEDUP GUARD — mirror of settle_open_picks: in the pre-supersede window
        # (full time until the auto pass reaches kickoff+delay) the dashboard
        # shows BOTH duplicate events as pending, so an operator can hand-settle
        # the twin of an already-settled pick and double-count one physical bet.
        # NULL starts_at cannot bound the fixture window -> guard skipped (the
        # manual path stays available for TBD-kickoff events, as before).
        pair = fixture_pair_key(home_name, away_name)
        if pair is not None and starts_at is not None:
            await _lock_settlement_instrument(
                session,
                sport_id=sport_id,
                market=pick.market,
                market_detail=pick.market_detail,
                selection=pick.selection,
                model_version_id=pick.model_version_id,
                target_pair=pair,
            )
        if (
            pair is not None
            and starts_at is not None
            and await _settled_sibling_exists(
                session,
                pick_id=pick.id,
                event_id=pick.event_id,
                sport_id=sport_id,
                starts_at=starts_at,
                market=pick.market,
                market_detail=pick.market_detail,
                selection=pick.selection,
                model_version_id=pick.model_version_id,
                target_pair=pair,
                sport_key=sport_key,
            )
        ):
            # Same terminal shape as the auto pass: 'superseded' writes NO
            # result_tracking row, so it never enters pnl/ROI/CLV.
            pick.status = "superseded"
            superseded += 1
            skipped += 1  # not settled — the caller's (settled, skipped) contract
            logger.info(
                "manual settlement: superseded duplicate pick %d (%s %s) — a "
                "sibling of the same fixture is already settled (cross-source "
                "event dedup)",
                pick.id,
                pick.market,
                pick.selection,
            )
            continue
        if await _settle_one(
            session,
            pick,
            home_name,
            away_name,
            home_score,
            away_score,
            now,
            sport_key=sport_key,
        ):
            settled += 1
            # Snapshot close AFTER the status flip, same transaction — mirror of
            # the auto path so manual settles also enter the CLV subset (audit #4).
            # starts_at None (kickoff unknown) has no freshness anchor -> skip.
            if devig_method is not None and starts_at is not None:
                await finalize_closing_from_snapshots(
                    session,
                    pick,
                    external_ref,
                    starts_at,
                    devig_method,
                    use_pinnacle_archive=use_pinnacle_archive,
                    use_betfair_exchange=use_betfair_exchange,
                    sharp_close_echo_gate=sharp_close_echo_gate,
                    value_policy=value_policy,
                )
        else:
            skipped += 1
    if settled or superseded:
        await session.flush()
    return settled, skipped


async def _settle_one(
    session: AsyncSession,
    pick: Pick,
    home_name: str,
    away_name: str,
    home_score: int,
    away_score: int,
    now: datetime,
    *,
    completion: Completion = "full",
    winner_side: str | None = None,
    sport_key: str | None = None,
    unsettleable_counts: Counter[str] | None = None,
) -> bool:
    """Atomic single-pick settlement: result row + status flip. False = skipped.

    `completion` implements TENNIS_SETTLEMENT_CONVENTION ("pinnacle_one_set",
    app/settlement/outcomes.py): "void" (walkover / abandoned before one
    completed set) voids every market; "retired" (>=1 completed set, a player
    advanced) grades h2h to `winner_side` and voids the rest; "full" — the
    only value non-tennis providers emit — is the unchanged score path.

    `sport_key` enables sport-convention grading (a tied final on a 2-way
    moneyline pushes — see outcomes._TWO_WAY_H2H_SPORTS); None keeps the
    3-way default.
    """
    if (
        completion == "full"
        and sport_key == "tennis"
        and tennis_set_score_ungradeable(pick.market, pick.selection, home_score, away_score)
    ):
        # SET-SCORE GUARD (2026-07-10, 106 mis-graded picks): scraped tennis
        # results are SET scores, so a GAME-line totals/spreads pick ("Over
        # 22.5", "-4.5") must never be graded from one (2-1 would read as
        # "3 total, margin 1"). Mirror the unclassifiable-selection skip:
        # leave the pick OPEN for manual result entry — never void, never
        # guess. (The retirement/walkover paths above are untouched: those
        # VOID by book convention regardless of line.)
        logger.info(
            "pick %d not settled: tennis %s %r is a game line but %d-%d is a set "
            "score — left open for manual settlement",
            pick.id,
            pick.market,
            pick.selection,
            home_score,
            away_score,
        )
        return False
    try:
        if completion == "void":
            outcome = Outcome.VOID
        elif completion == "retired":
            outcome = settle_selection_retired(
                pick.market, pick.selection, home_name, away_name, winner_side
            )
        else:
            outcome = settle_selection(
                pick.market,
                pick.selection,
                home_name,
                away_name,
                home_score,
                away_score,
                sport_key=sport_key,
            )
    except ValueError as exc:
        reason = str(exc)
        if unsettleable_counts is not None:
            unsettleable_counts[pick.market] += 1
        if _UNSETTLEABLE_WARNED.get(pick.id) != reason:
            # First sighting of this (pick, reason) — or the reason changed.
            # Repeats stay silent; the caller's per-cycle summary carries the
            # ongoing count instead (warning-dedup, audit S 2026-07-26).
            _UNSETTLEABLE_WARNED[pick.id] = reason
            logger.warning("pick %d not settleable: %s", pick.id, exc)
        return False
    # The pick grades now — clear any stale unsettleable state so a future
    # regression on this pick warns again.
    _UNSETTLEABLE_WARNED.pop(pick.id, None)

    stake, odds, payout_bookmaker = await _stake_and_odds(session, pick)
    pnl = pick_pnl(outcome, stake, odds, bookmaker=payout_bookmaker)
    inserted = await session.execute(
        pg_insert(ResultTracking)
        .values(
            pick_id=pick.id,
            outcome=str(outcome),
            pnl=pnl,
            roi=pick_roi(pnl, stake),
            settled_stake_amount=stake,
            settled_effective_odds=_effective_settlement_odds(odds, payout_bookmaker),
            # A walkover/abandonment has no meaningful score — leave NULL
            # (same shape as the stale-void paths) rather than persist 0-0.
            home_score=None if completion == "void" else home_score,
            away_score=None if completion == "void" else away_score,
            settled_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_result_tracking_pick")
        .returning(ResultTracking.id)
    )
    if inserted.scalar_one_or_none() is None:
        return False  # already settled by a concurrent/manual path
    pick.status = "settled"
    if completion == "void":
        # Walkover/abandonment: the match was never (fully) played — mirror
        # the other VOID paths, which deliberately leave Event.status alone.
        logger.info(
            "settled pick %d: %s %s -> void (%s — tennis convention %s)",
            pick.id,
            pick.market,
            pick.selection,
            "walkover/abandoned before one completed set",
            TENNIS_SETTLEMENT_CONVENTION,
        )
        return True
    # Issue 2 (2026-06-24): Event.status was only ever the 'scheduled' server
    # default — nothing transitioned it, so a finished, settled game stayed
    # 'scheduled' forever. A pick settling from a REAL final score is the
    # canonical "event is over" signal, so flip the event here (idempotent, gated
    # to a real-score settle — the VOID paths deliberately leave status alone, an
    # abandoned/TBD pick is not a finished game). Lifecycle-only column; no logic
    # reads it, so this cannot affect settlement/edge math.
    await session.execute(
        update(Event)
        .where(Event.id == pick.event_id, Event.status != "finished")
        .values(status="finished")
    )
    logger.info(
        "settled pick %d: %s %s -> %s (%d-%d)",
        pick.id,
        pick.market,
        pick.selection,
        outcome,
        home_score,
        away_score,
    )
    return True


async def _load_scraped_finals(session: AsyncSession, now: datetime) -> list[FinalScore]:
    """FinalScore rows from EVENTS that carry an OddsPortal-scraped final score,
    for still-open picks whose match has kicked off. Lets leagues with no free
    results feed AUTO-settle (no manual entry) from the score already fetched at
    scrape time. The score is on the pick's OWN event, so the ScoreBook matches
    it exactly by the same team names — no cross-source name risk."""
    home_t, away_t = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(
                home_t.name,
                away_t.name,
                Event.starts_at,
                Event.scraped_home_score,
                Event.scraped_away_score,
            )
            .join(Pick, Pick.event_id == Event.id)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .where(
                Pick.status == "alerted",
                Event.scraped_home_score.is_not(None),
                Event.scraped_away_score.is_not(None),
                Event.starts_at.is_not(None),
                Event.starts_at < now,
            )
            .distinct()
        )
    ).all()
    return [
        FinalScore(
            home_team=h,
            away_team=a,
            match_date=ko.date(),
            home_score=int(hs),
            away_score=int(as_),
        )
        for h, a, ko, hs, as_ in rows
    ]


# In-process TTL cache for the RESULT FEEDS (ops audit WP7): the settle job
# runs every ~30s but football-data CSVs / ESPN scoreboards update far slower —
# uncached, each cycle re-hits ~36 CSVs + ~28 ESPN endpoints (~2,880x/day each),
# a self-inflicted ban risk on free feeds. Keyed by the full feed config (slugs,
# seasons, date window, ESPN settings) so a config change never serves a stale
# entry. Values: (fetched_at, scores). EMPTY results are never cached — the
# silent-empty refusal in run_settlement_cycle must keep seeing live outages.
_FEED_CACHE: dict[tuple[object, ...], tuple[datetime, list[FinalScore]]] = {}


def clear_feed_cache() -> None:
    """Drop all cached feed results (tests + operator tooling)."""
    _FEED_CACHE.clear()


async def _load_feed_scores(
    client: httpx.AsyncClient,
    slugs: Sequence[str],
    seasons: Sequence[str],
    now: datetime,
    settings: "Settings",
) -> list[FinalScore]:
    """Load football-data CSV + ESPN scores, served from the TTL cache when a
    fresh-enough fetch with the IDENTICAL feed config exists. Cache expiry is
    judged against the caller's `now` (UTC-aware), matching the cycle clock."""
    on_or_after = (now - SCORE_WINDOW).date()
    key: tuple[object, ...] = (
        tuple(slugs),
        tuple(seasons),
        on_or_after,  # rolls with now.date(): a date rollover splits the key
        settings.espn_settle_enabled,
        settings.espn_settle_sports,
        settings.espn_settle_days,
    )
    ttl = settings.settle_feed_ttl_seconds
    cached = _FEED_CACHE.get(key)
    if ttl > 0 and cached is not None:
        fetched_at, scores = cached
        if (now - fetched_at).total_seconds() < ttl:
            return list(scores)
    scores = await load_scores(client, slugs, seasons, on_or_after=on_or_after)
    # ESPN free scores add basketball / NFL / tennis auto-settlement (soccer
    # already uses the football-data CSV feeds above). Read-only SCORES only —
    # ESPN odds are soft and are NEVER used as a close.
    if settings.espn_settle_enabled:
        from app.ingestion.espn_scores import load_espn_scores

        espn_sports = [s.strip() for s in settings.espn_settle_sports.split(",") if s.strip()]
        espn_dates = [now.date() - timedelta(days=i) for i in range(settings.espn_settle_days)]
        scores = [*scores, *await load_espn_scores(client, espn_sports, espn_dates)]
    if ttl > 0 and scores:  # empty == outage: never cached, re-probed next cycle
        # Drop expired entries so stale keys (e.g. yesterday's date window)
        # never accumulate across long uptimes.
        for stale_key in [
            k for k, (at, _) in _FEED_CACHE.items() if (now - at).total_seconds() >= ttl
        ]:
            _FEED_CACHE.pop(stale_key, None)
        _FEED_CACHE[key] = (now, list(scores))
    return scores


async def run_settlement_cycle(
    client: httpx.AsyncClient,
    session_factory: "async_sessionmaker",
    slugs: Sequence[str],
    seasons: Sequence[str],
    now: datetime | None = None,
    devig_method: DevigMethod | None = None,
    use_pinnacle_archive: bool = False,
    use_betfair_exchange: bool = False,
) -> int:
    """One scheduler cycle: fetch scores for the configured leagues, settle.

    Refuses to settle when the providers return nothing (a feed outage must
    look like an outage, not like a quiet day).

    `devig_method` prices the snapshot-sourced closing line for the picks
    this cycle settles (see settle_open_picks). None — the scheduler's call —
    resolves to the SAME method the pick pipeline runs with, mirroring how
    app/scheduler.py builds deps.devig_method: live CLV, backtest CLV, and
    the settlement-time snapshot close must all speak one devig.
    """
    now = now or datetime.now(tz=UTC)
    from app.config import get_settings  # composition-root parity, lazy

    settings = get_settings()
    if devig_method is None:
        devig_method = (
            DevigMethod(settings.value_devig)
            if settings.pick_strategy == "value"
            else DevigMethod.POWER
        )
    # Same composition-root policy the pick pipeline uses (per-market devig +
    # logit-pool consensus), so the snapshot close is devigged with the
    # IDENTICAL per-market method as the fill — never a CLV method mismatch.
    from app.config import value_policy as build_value_policy

    settlement_value_policy = build_value_policy(settings)
    # Stale-TBD voiding runs FIRST and independently of the score feed: a
    # feed outage must not keep dead picks burning revalidation slots.
    async with session_factory() as session:
        await void_stale_null_kickoff_picks(session, now)
        await void_unsettleable_known_kickoff_picks(session, now)
        await session.commit()
    # TTL-cached feed load (ops audit WP7): repeat 30s cycles inside the TTL
    # reuse the last non-empty fetch instead of re-hammering the free feeds.
    scores = await _load_feed_scores(client, slugs, seasons, now, settings)
    # FEED/ESPN scores are authoritative + clean -> settle FIRST. Scraped final
    # scores (DOM-fragile) then settle ONLY the picks no feed reached, in an
    # idempotent SECOND pass, so a scrape can never override a feed result
    # (review 2026-06-21 — earlier the merged ScoreBook let scraped win on the
    # pick's exact-name key).
    feed_scores = scores
    scraped: list[FinalScore] = []
    if settings.settle_from_scraped_scores:
        async with session_factory() as session:
            scraped = await _load_scraped_finals(session, now)
    if not feed_scores and not scraped:
        logger.error("settle_results: no scores from any source — nothing settled")
        return 0
    if not feed_scores and scraped:
        logger.warning(
            "settle_results: result feeds returned nothing — settling %d game(s) "
            "from scraped final scores",
            len(scraped),
        )
    settled = 0
    async with session_factory() as session:
        if feed_scores:
            settled += await settle_open_picks(
                session,
                ScoreBook(feed_scores),
                now,
                devig_method=devig_method,
                use_pinnacle_archive=use_pinnacle_archive,
                use_betfair_exchange=use_betfair_exchange,
                # D2 echo gate — composition-root parity (same lazy Settings read
                # as the devig/value-policy resolution above).
                sharp_close_echo_gate=settings.clv_sharp_close_echo_gate,
                value_policy=settlement_value_policy,
            )
        if scraped:  # second pass: only feed-missed picks remain open (idempotent)
            settled += await settle_open_picks(
                session,
                ScoreBook(scraped),
                now,
                devig_method=devig_method,
                use_pinnacle_archive=use_pinnacle_archive,
                use_betfair_exchange=use_betfair_exchange,
                # D2 echo gate — composition-root parity (same lazy Settings read
                # as the devig/value-policy resolution above).
                sharp_close_echo_gate=settings.clv_sharp_close_echo_gate,
                value_policy=settlement_value_policy,
            )
        # Provider-gap report + bounded no-result expiry, AFTER both settle
        # passes so only picks NO source could settle remain alerted. The union
        # book is judgment-only here (lookup-is-None), so merge order is moot.
        await report_and_expire_no_result_picks(
            session,
            ScoreBook([*feed_scores, *scraped]),
            now,
            expire_after=(
                timedelta(days=settings.settlement_expire_days)
                if settings.settlement_expire_days > 0
                else None
            ),
        )
        await session.commit()
    return settled


def _recommended_settlement_basis(pick: Pick) -> tuple[Decimal, Decimal, str | None]:
    """Cap-adjusted recommended stake and its blended executable price.

    The accumulated effective-odds term is already commission-netted, so its
    bookmaker is ``None``: passing a book to ``pick_pnl`` would charge exchange
    commission twice. Legacy/unmigrated zero-basis rows retain the old latest-
    recommendation fallback.
    """
    stake = pick.settlement_stake_amount or Decimal("0")
    effective_term = pick.settlement_effective_odds_stake or Decimal("0")
    if stake > 0 and effective_term > 0:
        return stake, effective_term / stake, None
    return pick.recommended_stake_amount, pick.decimal_odds, pick.bookmaker


def _effective_settlement_odds(odds: Decimal, bookmaker: str | None) -> Decimal:
    """Return the exact commission-net price represented by a settlement row.

    Blended recommendation prices already carry exchange commission and signal
    that with ``bookmaker=None``. Explicit/manual raw fills carry their book and
    are netted once here, matching ``pick_pnl``'s winning-return calculation.
    """
    if bookmaker is None:
        return odds
    return Decimal(str(effective_odds(bookmaker, float(odds))))


async def _stake_and_odds(session: AsyncSession, pick: Pick) -> tuple[Decimal, Decimal, str | None]:
    """The user's actual stake/odds when they logged the bet, else the
    recommendation — result_tracking.pnl is 'vs actual or recommended stake'."""
    log = await session.scalar(
        select(ManualBetLog)
        .where(
            ManualBetLog.pick_id == pick.id,
            ManualBetLog.bet_placed.is_(True),
            ManualBetLog.actual_stake.is_not(None),
        )
        .order_by(ManualBetLog.id.desc())
        .limit(1)
    )
    if log is not None and log.actual_stake is not None:
        if log.actual_odds is not None:
            return log.actual_stake, log.actual_odds, log.bookmaker_used or pick.bookmaker
        _, blended_odds, blended_book = _recommended_settlement_basis(pick)
        return log.actual_stake, blended_odds, blended_book
    return _recommended_settlement_basis(pick)
