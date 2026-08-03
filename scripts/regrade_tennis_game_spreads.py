"""Re-grade the re-opened tennis GAME-handicap spreads from GAMES-level scores.

Follow-up to ``scripts/restate_tennis_spread_grades.py`` (settlement audit
2026-08-02): that script re-nulled the tennis spreads that had been wrongly
graded from SET scores (reason ``set_score_axis_mislabel`` in the
``settlement_restatements`` audit table) and left them ``status='alerted'``.
This script settles them HONESTLY from real games-level results:

* Source: tennis-data.co.uk season workbooks (``app/ingestion/tennis_data.py``)
  — ATP + WTA main tour only (no Challengers/ITF/doubles), per-set game scores
  (W1..W5/L1..L5) summed to total games per player. Coverage gaps are expected
  and REPORTED, never guessed around.
* Matching: exact canonical linkage only — ``canonical_tennis_name`` on both
  players (the same "surname f" ground the live Pinnacle-close re-key uses,
  app/storage/repositories.py), both players must agree, degenerate pairs
  refuse, and the ScoreBook date discipline applies (exact kickoff date first;
  a ±1-day hit only when unique; hits on BOTH adjacent dates are ambiguous ->
  refuse). No fuzzy matching, ever. Unmatched picks are left alone + reported.
* Grading: through the ENGINE's own code path — ``_settle_one`` (which calls
  ``settle_selection`` -> ``_settle_spreads`` with ``sport_key='tennis'``:
  half lines win/lose, integer lines PUSH per the two-way handicap convention,
  quarter lines split; P&L via ``pick_pnl``/``pick_roi`` with the same
  commission-netted effective odds) plus the same settled-sibling dedup guard
  ``settle_event_picks`` uses. No grading math is re-implemented here.
* Retirement rule (TENNIS_SETTLEMENT_CONVENTION "pinnacle_one_set"):
  tennis-data's ``Comment`` classifies completion. "Completed" grades from
  games; "Walkover" -> completion="void"; any other abnormal completion
  (Retired/Awarded/...) -> completion="retired" — the engine grades h2h to the
  advancing player and VOIDS spreads. Every target here is a spread, so both
  abnormal paths yield VOID (stake returned), exactly the convention.
* CLV: ``devig_method=None`` — the picks keep whatever close the live pipeline
  already stored; this script never fabricates or recomputes a close.
* Audit: every settled pick writes one ``settlement_restatements`` row per
  changed column with reason ``games_level_regrade``, and the result row is
  stamped ``note='games_level_regrade:tennis_data'``.

INTERLOCK — why the live axis guard can never re-touch a pick graded here:
1. ``_settle_one`` flips ``picks.status`` to 'settled'; every automatic
   settlement query (``settle_open_picks``/``settle_event_picks``/scraped
   finals) selects ``Pick.status == 'alerted'`` only, so a graded pick is
   invisible to the guard's candidate loop.
2. ``uq_result_tracking_pick`` + the engine's ``on_conflict_do_nothing`` make
   any concurrent/duplicate settle a no-op — one result row, ever.
3. The games score itself passes ``tennis_set_score_ungradeable`` honestly: a
   completed match's games sum is >= 12 > TENNIS_MAX_SET_SUM (5), so the
   set-score guard does not even fire on this score shape.

Usage (dry-run is the DEFAULT — everything runs in one transaction and ROLLS
BACK, printing the full report):

    uv run --extra backtest python scripts/regrade_tennis_game_spreads.py
    uv run --extra backtest python scripts/regrade_tennis_game_spreads.py --apply
    uv run --extra backtest python scripts/regrade_tennis_game_spreads.py --fetch

``--fetch`` downloads the needed season workbooks (read-only GET, ATP + WTA)
into ``--data-dir`` first. Run with the scheduler stopped. Decision-support
data hygiene only — this script never touches betting execution (none exists).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.ingestion.tennis_data import TennisMatchRow, fetch_season, load_tennis_dir
from app.resolution.matching import fixture_pair_key
from app.resolution.tennis_names import canonical_tennis_name
from app.settlement.engine import (
    _lock_settlement_instrument,
    _settle_one,
    _settled_sibling_exists,
)
from app.settlement.outcomes import is_tennis_sets_spread_detail
from app.storage.models import Event, Pick, ResultTracking, Sport, Team

logger = logging.getLogger(__name__)

REGRADE_REASON: Final[str] = "games_level_regrade"
RESULT_NOTE: Final[str] = "games_level_regrade:tennis_data"
SOURCE_REASON: Final[str] = "set_score_axis_mislabel"
AUDIT_TABLE: Final[str] = "settlement_restatements"

#: Minimum games in a COMPLETED match (best-of-3 floor: 6-0 6-0). A "Completed"
#: row below this is a source anomaly -> refuse, never grade.
_MIN_COMPLETED_GAMES: Final[int] = 12

_DEFAULT_DATA_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "data" / "tennis"

# Same DDL as restate_tennis_spread_grades.py — idempotent, so either script
# may run first.
_CREATE_AUDIT: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
    id           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    pick_id      BIGINT       NOT NULL,
    column_name  VARCHAR(64)  NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    reason       VARCHAR(128) NOT NULL,
    restated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
)
"""

_ANCHOR_IDS: Final[str] = f"""
SELECT DISTINCT pick_id FROM {AUDIT_TABLE} WHERE reason = :reason
"""  # noqa: S608 - table name is an internal literal

_INSERT_AUDIT_ROW: Final[str] = f"""
INSERT INTO {AUDIT_TABLE} (pick_id, column_name, old_value, new_value, reason)
VALUES (:pick_id, :column_name, :old_value, :new_value, :reason)
"""  # noqa: S608 - table name is an internal literal


# ---------------------------------------------------------------------------
# tennis-data result index + exact-canonical resolution
# ---------------------------------------------------------------------------


def build_result_index(
    rows: list[TennisMatchRow],
) -> dict[tuple[str, str], dict[date, list[TennisMatchRow]]]:
    """Index rows by (sorted canonical player pair) -> match date -> rows.

    Rows whose players don't both canonicalize (or collide into one canonical
    name) are dropped here — they could never be accepted downstream anyway."""
    index: dict[tuple[str, str], dict[date, list[TennisMatchRow]]] = {}
    for row in rows:
        cw, cl = canonical_tennis_name(row.winner), canonical_tennis_name(row.loser)
        if not cw or not cl or cw == cl:
            continue
        pair = (cw, cl) if cw < cl else (cl, cw)
        index.setdefault(pair, {}).setdefault(row.match_date.date(), []).append(row)
    return index


def resolve_match(
    index: dict[tuple[str, str], dict[date, list[TennisMatchRow]]],
    home: str,
    away: str,
    kickoff_date: date,
) -> tuple[TennisMatchRow | None, str]:
    """Exact-canonical, date-disciplined lookup. Returns (row, reason);
    row is None unless reason == 'matched'.

    Mirrors ScoreBook.lookup's wrong-game discipline: the pick's own kickoff
    date wins; a ±1-day hit is accepted only when the exact date has nothing
    AND exactly one adjacent date matches; both adjacent dates -> ambiguous."""
    ch, ca = canonical_tennis_name(home), canonical_tennis_name(away)
    if not ch or not ca or ch == ca:
        return None, "degenerate_canonical_names"
    pair = (ch, ca) if ch < ca else (ca, ch)
    by_date = index.get(pair)
    if not by_date:
        return None, "no_counterpart_in_tennis_data"
    exact = by_date.get(kickoff_date, [])
    if exact:
        return _unique_row(exact)
    prev = by_date.get(kickoff_date - timedelta(days=1), [])
    nxt = by_date.get(kickoff_date + timedelta(days=1), [])
    if prev and nxt:
        return None, "ambiguous_adjacent_dates"
    adjacent = prev or nxt
    if adjacent:
        return _unique_row(adjacent)
    return None, "no_row_within_one_day"


def _unique_row(rows: list[TennisMatchRow]) -> tuple[TennisMatchRow | None, str]:
    if len(rows) == 1:
        return rows[0], "matched"
    # Two rows for the same canonical pair on one date: only acceptable when
    # they are literally the same result (duplicate capture); else refuse.
    if all(r == rows[0] for r in rows[1:]):
        return rows[0], "matched"
    return None, "ambiguous_same_date"


def classify_completion(
    row: TennisMatchRow,
) -> tuple[Literal["full", "retired", "void"] | None, str]:
    """(completion, reason) for a matched row under the pinnacle_one_set
    convention. None completion means the row cannot classify -> leave open."""
    comment = (row.comment or "").casefold()
    if row.completed:
        if row.winner_games is None or row.loser_games is None:
            return None, "missing_set_game_scores"
        if (
            row.winner_sets is not None
            and row.loser_sets is not None
            and row.winner_sets <= row.loser_sets
        ):
            return None, "inconsistent_sets_columns"
        if row.winner_games + row.loser_games < _MIN_COMPLETED_GAMES:
            return None, "implausible_games_total"
        return "full", "matched"
    if "walkover" in comment:
        return "void", "matched"
    if comment:
        # Retired/Awarded/etc. after play began. The engine grades h2h to the
        # advancing player and VOIDS every other market — for the spreads-only
        # target family both this and "void" grade VOID (stake returned).
        return "retired", "matched"
    return None, "no_completion_info"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass
class RegradeReport:
    targets: int = 0
    settled: int = 0
    superseded: int = 0
    engine_skipped: int = 0
    unmatched: Counter[str] = field(default_factory=Counter)
    outcomes: Counter[str] = field(default_factory=Counter)
    # market_detail family -> outcome -> count
    families: dict[str, Counter[str]] = field(default_factory=dict)
    fair_prob_sum: Decimal = Decimal("0")
    fair_prob_n: int = 0
    pnl_sum: Decimal = Decimal("0")

    @property
    def decided(self) -> int:
        return sum(self.outcomes[o] for o in ("won", "lost", "half_won", "half_lost"))

    @property
    def wins(self) -> int:
        return self.outcomes["won"] + self.outcomes["half_won"]

    def lines(self) -> list[str]:
        out = [
            f"target picks (re-opened {SOURCE_REASON} family, status='alerted'): {self.targets}",
            f"  settled via engine path:      {self.settled}",
            f"  superseded (settled sibling): {self.superseded}",
            f"  engine skipped (unsettleable/conflict): {self.engine_skipped}",
            f"  unmatched/deferred (left 'alerted'):    {sum(self.unmatched.values())}",
        ]
        for reason, n in self.unmatched.most_common():
            out.append(f"    {reason:<32} {n}")
        out.append("  outcome counts:")
        for outcome in ("won", "half_won", "lost", "half_lost", "push", "void"):
            if self.outcomes[outcome]:
                out.append(f"    {outcome:<10} {self.outcomes[outcome]}")
        out.append("  per-family breakdown (market_detail -> outcomes):")
        for family in sorted(self.families):
            parts = ", ".join(f"{k}={v}" for k, v in sorted(self.families[family].items()))
            out.append(f"    {family:<24} {parts}")
        if self.decided:
            rate = self.wins / self.decided
            expected = (
                float(self.fair_prob_sum) / self.fair_prob_n if self.fair_prob_n else float("nan")
            )
            out.append(
                f"  decided win-rate: {self.wins}/{self.decided} = {rate:.3f} "
                f"(fair-prob expectation ~{expected:.3f})"
            )
        out.append(f"  settled P&L sum: {self.pnl_sum}")
        return out


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------

_RT_AUDIT_COLUMNS: Final[tuple[str, ...]] = (
    "outcome",
    "pnl",
    "roi",
    "settled_stake_amount",
    "settled_effective_odds",
    "home_score",
    "away_score",
    "note",
    "settled_at",
)


async def _write_audit(session: AsyncSession, pick: Pick, rt: ResultTracking) -> None:
    """One settlement_restatements row per changed column (old -> new), the
    mirror image of the re-null audit written by restate_tennis_spread_grades."""
    rows: list[dict[str, str | int | None]] = [
        {
            "pick_id": pick.id,
            "column_name": "picks.status",
            "old_value": "alerted",
            "new_value": "settled",
            "reason": REGRADE_REASON,
        }
    ]
    for column in _RT_AUDIT_COLUMNS:
        value = getattr(rt, column)
        rows.append(
            {
                "pick_id": pick.id,
                "column_name": f"result_tracking.{column}",
                "old_value": None,
                "new_value": None if value is None else str(value),
                "reason": REGRADE_REASON,
            }
        )
    for params in rows:
        await session.execute(text(_INSERT_AUDIT_ROW), params)


async def regrade(
    session: AsyncSession,
    tennis_rows: list[TennisMatchRow],
    now: datetime,
) -> RegradeReport:
    """Re-grade the target family on an ALREADY-BEGUN transaction (caller owns
    commit/rollback — the CLI wrapper and the test suite both drive this)."""
    report = RegradeReport()
    await session.execute(text(_CREATE_AUDIT))
    anchor_ids = {
        int(pick_id)
        for (pick_id,) in (
            await session.execute(text(_ANCHOR_IDS), {"reason": SOURCE_REASON})
        ).all()
    }
    if not anchor_ids:
        logger.warning("no %s rows in %s — nothing to re-grade", SOURCE_REASON, AUDIT_TABLE)
        return report

    home_t, away_t = aliased(Team), aliased(Team)
    rows = (
        await session.execute(
            select(
                Pick,
                home_t.name,
                away_t.name,
                Event.starts_at,
                Event.sport_id,
                Sport.key,
            )
            .join(Event, Pick.event_id == Event.id)
            .join(home_t, Event.home_team_id == home_t.id)
            .join(away_t, Event.away_team_id == away_t.id)
            .join(Sport, Event.sport_id == Sport.id)
            .where(
                Pick.id.in_(anchor_ids),
                Pick.status == "alerted",
                Pick.market == "spreads",
                Sport.key == "tennis",
            )
            .order_by(Pick.id)
        )
    ).all()

    index = build_result_index(tennis_rows)

    for pick, home_name, away_name, starts_at, sport_id, sport_key in rows:
        if is_tennis_sets_spread_detail(pick.market_detail):
            continue  # legitimately sets-axis — never part of the defective family
        report.targets += 1
        if starts_at is None:
            report.unmatched["no_kickoff_date"] += 1
            continue
        row, reason = resolve_match(index, home_name, away_name, starts_at.date())
        if row is None:
            report.unmatched[reason] += 1
            continue
        completion, completion_reason = classify_completion(row)
        if completion is None:
            report.unmatched[completion_reason] += 1
            continue

        # Orientation: map the winner-first tennis-data games onto the pick's
        # own home/away axis by canonical NAME, never by position.
        winner_is_home = canonical_tennis_name(row.winner) == canonical_tennis_name(home_name)
        winner_side: Literal["home", "away"] = "home" if winner_is_home else "away"
        if completion == "full":
            assert row.winner_games is not None and row.loser_games is not None
            home_games, away_games = (
                (row.winner_games, row.loser_games)
                if winner_is_home
                else (row.loser_games, row.winner_games)
            )
        else:
            # VOID/retired: the engine ignores (void) or voids the spread
            # (retired) — pass partial games when parseable, else zeros that
            # the void path never persists. Spreads-only family by selection.
            assert pick.market == "spreads"
            if row.winner_games is not None and row.loser_games is not None:
                home_games, away_games = (
                    (row.winner_games, row.loser_games)
                    if winner_is_home
                    else (row.loser_games, row.winner_games)
                )
            elif completion == "retired":
                report.unmatched["retired_without_game_scores"] += 1
                continue
            else:
                home_games = away_games = 0  # walkover: _settle_one stores NULL scores

        # Same settled-sibling dedup guard as settle_event_picks — a
        # cross-source twin that already settled supersedes this pick instead
        # of double-counting one physical bet.
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
                report.superseded += 1
                continue

        settled = await _settle_one(
            session,
            pick,
            home_name,
            away_name,
            home_games,
            away_games,
            now,
            completion=completion,
            winner_side=winner_side,
            sport_key=sport_key,
        )
        if not settled:
            report.engine_skipped += 1
            continue
        await session.flush()
        rt = await session.scalar(select(ResultTracking).where(ResultTracking.pick_id == pick.id))
        assert rt is not None  # _settle_one returned True -> the row exists
        rt.note = RESULT_NOTE
        await session.flush()
        await _write_audit(session, pick, rt)

        report.settled += 1
        report.outcomes[rt.outcome] += 1
        family = pick.market_detail or "<NULL>"
        report.families.setdefault(family, Counter())[rt.outcome] += 1
        if rt.pnl is not None:
            report.pnl_sum += rt.pnl
        if rt.outcome in ("won", "lost", "half_won", "half_lost"):
            report.fair_prob_sum += pick.fair_probability
            report.fair_prob_n += 1
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _fetch_workbooks(data_dir: Path, years: list[int]) -> None:
    """Read-only GET of the ATP + WTA season workbooks into data_dir."""
    data_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient() as client:
        for year in years:
            for tour in ("atp", "wta"):
                target = data_dir / f"{tour}_{year}.xlsx"
                try:
                    payload = await fetch_season(client, tour, year)
                except httpx.HTTPError as exc:
                    logger.warning("fetch %s %d failed: %s", tour, year, type(exc).__name__)
                    continue
                target.write_bytes(payload)
                print(f"fetched {tour} {year}: {len(payload)} bytes -> {target}")


async def run(*, apply: bool, data_dir: Path, fetch: bool, years: list[int]) -> None:
    from app.config import get_settings
    from app.database import create_engine

    if fetch:
        await _fetch_workbooks(data_dir, years)
    tennis_rows = load_tennis_dir(data_dir)
    if not tennis_rows:
        raise SystemExit(
            f"no tennis-data season files in {data_dir} — place the .xlsx/.csv "
            "workbooks there (or pass --fetch) before running"
        )
    print(f"tennis-data rows loaded: {len(tennis_rows)} from {data_dir}")

    engine = create_engine(get_settings())
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            session = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                report = await regrade(session, tennis_rows, datetime.now(tz=UTC))
                await session.flush()
            finally:
                await session.close()
            mode = "APPLY" if apply else "DRY-RUN (rolled back)"
            print(f"\n=== tennis games-level spread re-grade — {mode} ===")
            print(f"reason: {REGRADE_REASON}\n")
            for line in report.lines():
                print(f"  {line}")
            if apply:
                await trans.commit()
                print(f"\nCOMMITTED. Audit rows in {AUDIT_TABLE} (reason={REGRADE_REASON}).")
            else:
                await trans.rollback()
                print("\nROLLED BACK (dry run). Re-run with --apply to execute.")
    finally:
        await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute for real (default is dry-run: run everything, print the report, roll back).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_DEFAULT_DATA_DIR,
        help=f"Directory of tennis-data season workbooks (default {_DEFAULT_DATA_DIR}).",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Download the ATP+WTA workbooks for --years into --data-dir first (read-only GET).",
    )
    parser.add_argument(
        "--years",
        type=int,
        nargs="+",
        default=[datetime.now(tz=UTC).year],
        help="Season years for --fetch (default: current year).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()
    asyncio.run(run(apply=args.apply, data_dir=args.data_dir, fetch=args.fetch, years=args.years))
