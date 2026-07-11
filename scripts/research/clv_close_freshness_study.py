"""CLV close-freshness shadow study — stored close vs the Pinnacle ARCADIA archive close.

CONTEXT (remaining-work queue #1, 2026-07-10). Settled picks' closing snapshots run
median 139-196 minutes pre-kickoff (the main scrape cadence), while the arcadia
dedicated capture holds rows median ~13 min pre-kickoff. A stale "close" distorts
CLV. The consume machinery already exists and is GATED OFF:
``finalize_closing_from_snapshots(use_pinnacle_archive=...)`` <-
``Settings.clv_use_pinnacle_archive`` (default False). This script measures, in
SHADOW, what flipping that flag would have done to every settled pick:

  (a) MATCH RATE — fraction of settled picks whose fixture strictly matches an
      arcadia archive event AND yields a devig-anchorable close for the pick's own
      market/line/selection (split by sport, market, tier). Uses the REAL consume
      path (``app.clv_trueup._pinnacle_archive_close`` ->
      ``repositories.resolve_pinnacle_close_snaps``), never a reimplementation.
  (b) CLOSE-AGE DELTA — minutes-before-kickoff distribution of the CURRENT stored
      close (close_snapshot_captured_at) vs the arcadia anchor rows, on the
      matched subset.
  (c) CLV DELTA — per-pick clv_log recomputed with the arcadia close (same devig
      method + value policy + effective-odds netting as finalize) vs the stored
      clv_log, on the TRUSTED subset (all clv-evidence-reviewer gates applied:
      fabrication, tautology, circularity/independence, devig symmetry,
      has_snapshot_close, sharp closing anchor). Mean/SE of the delta, beat-close
      sign flips, and the aggregate trusted CLV under each close source.
  (d) GUARD CHECKS — how many arcadia-close rows would trip the fabricated-close
      bounds, including the SYMMETRIC implausible-NEGATIVE counter (M349 design:
      close-implied edge < -CLV_IMPLAUSIBLE_CLOSE_EDGE) and the |clv_log|>0.5
      fallback bound.

READ-ONLY BY CONSTRUCTION. This script never commits: the shared session is rolled
back on exit, and ``repositories._record_pinnacle_link_observability`` (the resolve
path's best-effort observability write) is monkeypatched to a no-op for the run, so
not even savepoint-scoped rows are flushed. No config flip, no pick mutation, no bet.

    uv run python scripts/research/clv_close_freshness_study.py
    uv run python scripts/research/clv_close_freshness_study.py --limit 50   # smoke
    # SHADOW per-source sharp-close freshness re-report (report-only; no gate):
    uv run python scripts/research/clv_close_freshness_study.py \
        --max-sharp-close-age-minutes 60 120 240
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

# --------------------------------------------------------------------------- #
# Reused production machinery (imported, never reimplemented)
# --------------------------------------------------------------------------- #
from app.backtesting.clv import clv_log
from app.clv_trueup import (
    _anchor_capture_time,
    _detail_matched_books,
    _exact_group_books,
    _merge_vocabulary_groups,
    _pinnacle_archive_close,
    _settleable_groups,
)
from app.edge.value import effective_odds
from app.pipeline import event_fair_probs, group_market_prices
from app.probabilities.devig import DevigMethod
from app.storage.repositories import (
    _SHARP_CLOSE_ANCHORS,
    CLV_IMPLAUSIBLE_CLOSE_EDGE,
    CLV_IMPLAUSIBLE_LOG,
    CLV_TAUTOLOGY_EPS,
    _clv_row_is_fabricated,
    _clv_row_is_tautological,
    _devig_fallback_asymmetric,
)

logging.basicConfig(level=logging.WARNING)  # silence per-pick INFO chatter
logger = logging.getLogger("clv_close_freshness_study")


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _percentiles(values: list[float]) -> tuple[float, float, float] | None:
    """(p25, median, p75) by nearest-rank; None on empty."""
    if not values:
        return None
    ordered = sorted(values)

    def _p(q: float) -> float:
        idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
        return ordered[idx]

    return _p(0.25), _p(0.50), _p(0.75)


def _mean_se(xs: list[float]) -> tuple[float, float]:
    m = sum(xs) / len(xs)
    if len(xs) < 2:
        return m, float("nan")
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)  # ddof=1 (honest SE)
    return m, math.sqrt(var / len(xs))


# --------------------------------------------------------------------------- #
# Per-pick shadow record
# --------------------------------------------------------------------------- #
@dataclass
class Row:
    pick_id: int
    sport: str
    market: str
    tier: str
    fill_book: str
    decimal_odds: float
    fill_eff: float
    model_probability: float | None
    clv_stored: float | None
    closing_fair_stored: float | None
    closing_anchor_type: str | None
    close_independent: bool | None
    has_snapshot_close: bool | None
    mint_fell_back: bool | None
    close_fell_back_stored: bool | None
    stored_close_age_min: float | None  # kickoff - close_snapshot_captured_at
    # arcadia shadow results
    arc_matched: bool = False  # archive event matched, >=1 close snap returned
    arc_fair: float | None = None  # anchored devigged fair for the pick's selection
    arc_fell_back: bool | None = None
    arc_close_age_min: float | None = None
    arc_clv: float | None = None
    arc_refusal: str | None = None  # why no usable arc close (funnel diagnostics)

    # ---- stored-side trust gates (clv-evidence-reviewer) ------------------- #
    def stored_fabricated(self) -> bool:
        return _clv_row_is_fabricated(self.clv_stored, self.decimal_odds, self.closing_fair_stored)

    def stored_tautological(self) -> bool:
        return _clv_row_is_tautological(
            self.clv_stored, self.closing_fair_stored, self.model_probability
        )

    def stored_trusted(self) -> bool:
        return (
            self.clv_stored is not None
            and not self.stored_fabricated()
            and not self.stored_tautological()
            and bool(self.has_snapshot_close)
            and self.closing_anchor_type in _SHARP_CLOSE_ANCHORS
            and self.close_independent is True
            and not _devig_fallback_asymmetric(self.mint_fell_back, self.close_fell_back_stored)
        )

    # ---- arcadia-side trust gates (same rules re-applied to the new close) - #
    def arc_close_edge(self) -> float | None:
        if self.arc_fair is None:
            return None
        return self.arc_fair - 1.0 / self.decimal_odds  # RAW odds: mirrors read-side guard

    def arc_fabricated(self) -> bool:
        """Mirror of _clv_row_is_fabricated on the arcadia close (POSITIVE bound)."""
        edge = self.arc_close_edge()
        if edge is not None:
            return edge > CLV_IMPLAUSIBLE_CLOSE_EDGE
        return self.arc_clv is not None and abs(self.arc_clv) > CLV_IMPLAUSIBLE_LOG

    def arc_implausible_negative(self) -> bool:
        """M349 symmetric counter: close-implied edge below -bound (mis-oriented
        close biasing trusted CLV DOWN). Reported, never silently excluded."""
        edge = self.arc_close_edge()
        return edge is not None and edge < -CLV_IMPLAUSIBLE_CLOSE_EDGE

    def arc_tautological(self) -> bool:
        return _clv_row_is_tautological(self.arc_clv, self.arc_fair, self.model_probability)

    def arc_trusted(self) -> bool:
        """Would this row enter the trusted sharp subset under the arcadia close?
        Anchor is 'Pinnacle' by construction (closing_anchor_type='pinnacle');
        independence = fill book is not the anchor book AND the fair moved
        (persisted_close_independent's two tests, applied to the shadow close)."""
        if self.arc_clv is None or self.arc_fair is None:
            return False
        fill_is_anchor = self.fill_book.strip().lower() == "pinnacle"
        fair_moved = (
            self.model_probability is not None
            and abs(self.arc_fair - self.model_probability) > CLV_TAUTOLOGY_EPS
        )
        return (
            not self.arc_fabricated()
            and not self.arc_tautological()
            and not fill_is_anchor
            and fair_moved
            and not _devig_fallback_asymmetric(self.mint_fell_back, self.arc_fell_back)
        )


# --------------------------------------------------------------------------- #
# Arcadia fair extraction — mirrors finalize_closing_from_snapshots' fair logic
# --------------------------------------------------------------------------- #
def _arc_fair_for_pick(
    pick: Any,
    snaps: list[Any],
    devig_method: DevigMethod,
    value_policy: Any,
) -> tuple[float, bool | None, datetime | None] | None:
    """(fair, fell_back, anchor_capture_ts) for the pick's own market/line/selection
    from the arcadia close snaps, using the SAME grouping/anchoring chokepoints
    finalize uses (settleable groups, vocabulary merge, exact-detail vs line-blind
    with fail-closed ambiguity). None = no anchorable fair (fail-closed)."""
    grouped = _merge_vocabulary_groups(_settleable_groups(group_market_prices(snaps)))
    fair_by_key: dict[tuple[str, str], float] = {}
    fell_by_key: dict[tuple[str, str], bool] = {}
    detail_by_key: dict[tuple[str, str], str | None] = {}
    ambiguous: set[tuple[str, str]] = set()
    fair_by_exact: dict[tuple[str, str, str], float] = {}
    fell_by_exact: dict[tuple[str, str, str], bool] = {}
    fell_back_by_market: dict[tuple[str, Any, str | None], bool] = {}
    for (_ev, market, _detail), (anchor, fair_by_sel) in event_fair_probs(
        grouped, devig_method, value_policy, fell_back_out=fell_back_by_market
    ).items():
        fb = fell_back_by_market.get((_ev, market, _detail), False)
        for sel, p in fair_by_sel.items():
            key = (str(market), sel)
            if _detail is not None:
                fair_by_exact[(str(market), sel, _detail)] = p
                fell_by_exact[(str(market), sel, _detail)] = fb
            if key in detail_by_key and detail_by_key[key] != _detail:
                ambiguous.add(key)
            detail_by_key[key] = _detail
            fair_by_key[key] = p
            fell_by_key[key] = fb
        del anchor  # anchor book is 'Pinnacle' by construction here
    if pick.market_detail is not None:
        stamped = (pick.market, pick.selection, pick.market_detail)
        fair = fair_by_exact.get(stamped)
        fell = fell_by_exact.get(stamped)
        books_cap = _exact_group_books(grouped, pick.market, pick.selection, pick.market_detail)
    else:
        if (pick.market, pick.selection) in ambiguous:
            return None
        fair = fair_by_key.get((pick.market, pick.selection))
        fell = fell_by_key.get((pick.market, pick.selection))
        books_cap = _detail_matched_books(
            grouped,
            pick.market,
            pick.selection,
            detail_by_key.get((pick.market, pick.selection)),
        )
    if fair is None or not 0.0 < fair < 1.0:
        return None
    _books, captured_map = books_cap
    capture_ts = _anchor_capture_time(captured_map, "Pinnacle")
    return fair, fell, capture_ts


# --------------------------------------------------------------------------- #
# Main study
# --------------------------------------------------------------------------- #
async def run_study(  # noqa: PLR0912, PLR0915
    limit: int | None, freshness_caps: list[float] | None = None
) -> str:
    from app import storage
    from app.config import get_settings, value_policy
    from app.database import create_engine, create_session_factory
    from app.storage import repositories
    from app.storage.models import Event, Pick, ResultTracking, Sport

    settings = get_settings()
    devig_method = DevigMethod(settings.value_devig)
    policy = value_policy(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    # READ-ONLY hardening: resolve_pinnacle_close_snaps makes best-effort
    # observability writes (event_source_links / match_review_queue) inside
    # savepoints. Neuter them for this shadow run so the study cannot write a
    # single row even before the final rollback.
    async def _noop_observability(*_args: Any, **_kwargs: Any) -> None:
        return None

    repositories._record_pinnacle_link_observability = _noop_observability  # type: ignore[assignment]
    del storage  # imported only to make the patch target explicit

    rows: list[Row] = []
    n_superseded = 0
    n_no_kickoff = 0

    try:
        async with session_factory() as session:
            q = (
                select(
                    Pick,
                    Event.external_ref,
                    Event.starts_at,
                    Sport.key,
                )
                .join(Event, Pick.event_id == Event.id)
                .join(Sport, Event.sport_id == Sport.id)
                .join(ResultTracking, ResultTracking.pick_id == Pick.id)
                .order_by(Pick.id)
            )
            if limit is not None:
                q = q.limit(limit)
            settled = (await session.execute(q)).all()

            for pick, external_ref, kickoff_raw, sport_key in settled:
                if pick.status == "superseded":
                    n_superseded += 1  # dedup twin — never double-count evidence
                    continue
                if kickoff_raw is None:
                    n_no_kickoff += 1
                    continue
                kickoff = _utc(kickoff_raw)
                assert kickoff is not None
                base_sport = sport_key.split("_")[0]
                stored_ts = _utc(pick.close_snapshot_captured_at)
                row = Row(
                    pick_id=pick.id,
                    sport=base_sport,
                    market=pick.market,
                    tier=pick.tier or "?",
                    fill_book=pick.bookmaker,
                    decimal_odds=float(pick.decimal_odds),
                    fill_eff=effective_odds(pick.bookmaker, float(pick.decimal_odds)),
                    model_probability=(
                        float(pick.model_probability)
                        if pick.model_probability is not None
                        else None
                    ),
                    clv_stored=float(pick.clv_log) if pick.clv_log is not None else None,
                    closing_fair_stored=(
                        float(pick.closing_fair_probability)
                        if pick.closing_fair_probability is not None
                        else None
                    ),
                    closing_anchor_type=pick.closing_anchor_type,
                    close_independent=pick.close_independent_of_fill,
                    has_snapshot_close=pick.has_snapshot_close,
                    mint_fell_back=pick.mint_devig_fell_back,
                    close_fell_back_stored=pick.close_devig_fell_back,
                    stored_close_age_min=(
                        (kickoff - stored_ts).total_seconds() / 60.0
                        if stored_ts is not None
                        else None
                    ),
                )
                # ---- arcadia archive close via the REAL consume path -------- #
                try:
                    snaps = await _pinnacle_archive_close(session, pick, external_ref, kickoff_raw)
                except Exception as exc:  # never let one fixture kill the study
                    logger.warning("pick %d resolve failed: %s", pick.id, type(exc).__name__)
                    snaps = []
                if snaps:
                    row.arc_matched = True
                    res = _arc_fair_for_pick(pick, snaps, devig_method, policy)
                    if res is None:
                        row.arc_refusal = "no_anchorable_fair"
                    else:
                        fair, fell, cap_ts = res
                        row.arc_fair = fair
                        row.arc_fell_back = fell
                        cap_ts = _utc(cap_ts)
                        if cap_ts is not None:
                            row.arc_close_age_min = (kickoff - cap_ts).total_seconds() / 60.0
                        if row.fill_eff > 1.0 and 0.0 < fair < 1.0:
                            row.arc_clv = clv_log(row.fill_eff, fair)
                else:
                    row.arc_refusal = "no_archive_match"
                rows.append(row)
            # READ-ONLY: nothing above may persist. Roll back explicitly.
            await session.rollback()
    finally:
        await engine.dispose()

    return _report(rows, n_superseded, n_no_kickoff, devig_method, freshness_caps)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _fmt_pct(num: int, den: int) -> str:
    return f"{num}/{den} ({num / den * 100:.1f}%)" if den else "0/0 (n/a)"


def _fmt_p(p: tuple[float, float, float] | None) -> str:
    if p is None:
        return "n/a"
    return f"median {p[1]:.0f}m  [p25 {p[0]:.0f}m, p75 {p[2]:.0f}m]"


def _freshness_section(rows: list[Row], caps: list[float]) -> list[str]:
    """(e) SHADOW per-source sharp-close freshness re-report (report-only).

    Re-reports the STORED-close trusted subset (the exact ``stored_trusted``
    gate set used in section (c)) excluding rows whose stored sharp close is
    OLDER than each cap — i.e. ``kickoff - close_snapshot_captured_at`` above
    ``cap`` minutes. Shadow-first mandate: this is a REPORT ONLY — no
    production gate, no config flag; nothing here changes which rows enter the
    live trusted subset."""
    out: list[str] = []
    w = out.append
    trusted = [r for r in rows if r.stored_trusted()]
    w("--- (e) STORED SHARP-CLOSE FRESHNESS SHADOW (report-only; no gate, no flag) ---")
    w(f"  trusted subset today (stored close): n={len(trusted)}")
    if trusted:
        m, se = _mean_se([r.clv_stored for r in trusted])  # type: ignore[misc]
        w(f"    baseline trusted CLV (no age cap)          : {m:+.5f} ± {se:.5f} SE")
    unknown_age = [r for r in trusted if r.stored_close_age_min is None]
    w(f"  trusted rows with UNKNOWN close age (always excluded under a cap): {len(unknown_age)}")
    w("")
    w("  cap_min |    n | mean_clv |      se | excluded_stale (their mean clv)")
    for cap in caps:
        fresh = [
            r
            for r in trusted
            if r.stored_close_age_min is not None and r.stored_close_age_min <= cap
        ]
        stale = [
            r
            for r in trusted
            if r.stored_close_age_min is not None and r.stored_close_age_min > cap
        ]
        stale_note = "n/a"
        if stale:
            sm, _ = _mean_se([r.clv_stored for r in stale])  # type: ignore[misc]
            stale_note = f"{sm:+.5f}"
        if fresh:
            fm, fse = _mean_se([r.clv_stored for r in fresh])  # type: ignore[misc]
            w(
                f"  {cap:>7.0f} | {len(fresh):>4} | {fm:+.5f} | {fse:.5f} | "
                f"{len(stale) + len(unknown_age):>3} ({stale_note})"
            )
        else:
            w(
                f"  {cap:>7.0f} | {len(fresh):>4} |      n/a |     n/a | "
                f"{len(stale) + len(unknown_age):>3} ({stale_note})"
            )
        # per-source (closing_anchor_type) breakdown under this cap
        by_src: dict[str, list[Row]] = defaultdict(list)
        for r in trusted:
            by_src[r.closing_anchor_type or "?"].append(r)
        for src in sorted(by_src, key=lambda k: -len(by_src[k])):
            g = by_src[src]
            g_fresh = [
                r for r in g if r.stored_close_age_min is not None and r.stored_close_age_min <= cap
            ]
            if g_fresh:
                gm, gse = _mean_se([r.clv_stored for r in g_fresh])  # type: ignore[misc]
                w(
                    f"          |      |          |         |   {src}: n={len(g_fresh)}/{len(g)}"
                    f"  mean {gm:+.5f} ± {gse:.5f} SE"
                )
            else:
                w(f"          |      |          |         |   {src}: n=0/{len(g)}  mean n/a")
    w("")
    return out


def _report(  # noqa: PLR0912, PLR0915
    rows: list[Row],
    n_superseded: int,
    n_no_kickoff: int,
    devig_method: DevigMethod,
    freshness_caps: list[float] | None = None,
) -> str:
    out: list[str] = []
    w = out.append
    n = len(rows)
    w("=" * 78)
    w("CLV CLOSE-FRESHNESS SHADOW STUDY — stored close vs Pinnacle ARCADIA archive")
    w(f"generated {datetime.now(tz=UTC).isoformat(timespec='seconds')}  devig={devig_method.value}")
    w("READ-ONLY shadow run: CLV_USE_PINNACLE_ARCHIVE stays OFF; nothing persisted.")
    w("=" * 78)
    w("")
    w(
        f"population: {n} settled picks (superseded excluded: {n_superseded}, "
        f"kickoff unknown: {n_no_kickoff})"
    )
    w("")

    # ---- (a) match rate ----------------------------------------------------- #
    matched = [r for r in rows if r.arc_matched]
    usable = [r for r in rows if r.arc_clv is not None]
    w("--- (a) ARCADIA MATCH RATE ---")
    w(f"  archive event matched (>=1 close snap) : {_fmt_pct(len(matched), n)}")
    w(f"  USABLE close (anchorable fair + CLV)   : {_fmt_pct(len(usable), n)}")
    for dim, keyf in (
        ("sport", lambda r: r.sport),
        ("market", lambda r: r.market),
        ("tier", lambda r: r.tier),
    ):
        w(f"  by {dim}:")
        groups: dict[str, list[Row]] = defaultdict(list)
        for r in rows:
            groups[keyf(r)].append(r)
        for key in sorted(groups, key=lambda k: -len(groups[k])):
            g = groups[key]
            gu = sum(1 for r in g if r.arc_clv is not None)
            gm = sum(1 for r in g if r.arc_matched)
            w(
                f"    {key:<22} n={len(g):>4}  matched={_fmt_pct(gm, len(g)):>16}  "
                f"usable={_fmt_pct(gu, len(g)):>16}"
            )
    refusals = Counter(r.arc_refusal for r in rows if r.arc_refusal is not None)
    w(f"  refusal funnel: {dict(refusals)}")
    w("")

    # ---- (b) close-age delta ------------------------------------------------ #
    both_age = [
        r for r in rows if r.arc_close_age_min is not None and r.stored_close_age_min is not None
    ]
    w("--- (b) CLOSE AGE BEFORE KICKOFF (matched subset, minutes) ---")
    w(f"  rows with BOTH ages: {len(both_age)}")
    paired_stored_ages = [
        r.stored_close_age_min for r in both_age if r.stored_close_age_min is not None
    ]
    paired_arc_ages = [r.arc_close_age_min for r in both_age if r.arc_close_age_min is not None]
    w(f"  current stored close : {_fmt_p(_percentiles(paired_stored_ages))}")
    w(f"  arcadia close        : {_fmt_p(_percentiles(paired_arc_ages))}")
    all_stored = [r.stored_close_age_min for r in rows if r.stored_close_age_min is not None]
    all_arc = [r.arc_close_age_min for r in rows if r.arc_close_age_min is not None]
    w(f"  (all rows) stored close age n={len(all_stored)}: {_fmt_p(_percentiles(all_stored))}")
    w(f"  (all rows) arcadia close age n={len(all_arc)}: {_fmt_p(_percentiles(all_arc))}")
    w("")

    # ---- (c) CLV delta on the trusted subset --------------------------------- #
    trusted_now = [r for r in rows if r.stored_trusted()]
    paired = [r for r in trusted_now if r.arc_clv is not None and r.arc_trusted()]
    w("--- (c) CLV DELTA (trust gates applied) ---")
    w(f"  trusted subset today (stored close)         : n={len(trusted_now)}")
    if trusted_now:
        m, se = _mean_se([r.clv_stored for r in trusted_now])  # type: ignore[misc]
        w(f"    aggregate trusted CLV (stored close)      : {m:+.5f} ± {se:.5f} SE")
    w(f"  paired subset (trusted now AND arc-trusted)  : n={len(paired)}")
    if paired:
        stored_vals = [r.clv_stored for r in paired]  # type: ignore[misc]
        arc_vals = [r.arc_clv for r in paired]  # type: ignore[misc]
        deltas = [a - s for a, s in zip(arc_vals, stored_vals, strict=True)]
        ms, _ = _mean_se(stored_vals)
        ma, _ = _mean_se(arc_vals)
        md, sed = _mean_se(deltas)
        flips_pos_to_neg = sum(1 for r in paired if r.clv_stored > 0 >= r.arc_clv)  # type: ignore[operator]
        flips_neg_to_pos = sum(1 for r in paired if r.clv_stored <= 0 < r.arc_clv)  # type: ignore[operator]
        w(f"    mean CLV, stored close                    : {ms:+.5f}")
        w(f"    mean CLV, arcadia close                   : {ma:+.5f}")
        w(f"    per-pick delta (arc - stored)             : {md:+.5f} ± {sed:.5f} SE")
        w(
            f"    beat-close sign flips                     : +->- {flips_pos_to_neg}, "
            f"-->+ {flips_neg_to_pos}"
        )
    # hypothetical trusted set under the flag: arc close where usable+clean,
    # stored trusted rows otherwise.
    hyp: list[float] = []
    hyp_from_arc = 0
    for r in rows:
        if r.arc_clv is not None and r.arc_trusted():
            hyp.append(r.arc_clv)
            hyp_from_arc += 1
        elif r.stored_trusted():
            hyp.append(r.clv_stored)  # type: ignore[arg-type]
    w(
        f"  HYPOTHETICAL trusted set with flag ON        : n={len(hyp)} "
        f"({hyp_from_arc} arcadia-closed, {len(hyp) - hyp_from_arc} kept stored)"
    )
    if hyp:
        mh, seh = _mean_se(hyp)
        w(f"    aggregate trusted CLV (flag ON, shadow)   : {mh:+.5f} ± {seh:.5f} SE")
    # newly-trusted rows (not trusted today, trusted under arcadia) per sport
    newly = [
        r for r in rows if not r.stored_trusted() and r.arc_clv is not None and r.arc_trusted()
    ]
    w(
        f"  rows NEWLY entering the trusted subset       : n={len(newly)} "
        f"(by sport: {dict(Counter(r.sport for r in newly))})"
    )
    w("")

    # ---- (d) guard trips under the arcadia close ----------------------------- #
    arc_rows = [r for r in rows if r.arc_fair is not None]
    w("--- (d) GUARD TRIPS UNDER THE ARCADIA CLOSE ---")
    w(f"  rows with an arcadia fair                    : n={len(arc_rows)}")
    w(
        f"  fabricated POSITIVE (close edge > +{CLV_IMPLAUSIBLE_CLOSE_EDGE:.2f})     : "
        f"{sum(1 for r in arc_rows if r.arc_fabricated())}"
    )
    w(
        f"  implausible NEGATIVE (close edge < -{CLV_IMPLAUSIBLE_CLOSE_EDGE:.2f})    : "
        f"{sum(1 for r in arc_rows if r.arc_implausible_negative())}   (M349 symmetric counter)"
    )
    n_big_log = sum(
        1 for r in arc_rows if r.arc_clv is not None and abs(r.arc_clv) > CLV_IMPLAUSIBLE_LOG
    )
    w(f"  |clv_log| > {CLV_IMPLAUSIBLE_LOG:.1f} magnitude bound                 : {n_big_log}")
    w(
        f"  tautological vs pick-time fair (eps={CLV_TAUTOLOGY_EPS})   : "
        f"{sum(1 for r in arc_rows if r.arc_tautological())}"
    )
    n_asym = sum(
        1 for r in arc_rows if _devig_fallback_asymmetric(r.mint_fell_back, r.arc_fell_back)
    )
    w(f"  devig-fallback asymmetric (mint vs arc close): {n_asym}")
    n_stored_neg = sum(
        1
        for r in rows
        if r.closing_fair_stored is not None
        and r.clv_stored is not None
        and (r.closing_fair_stored - 1.0 / r.decimal_odds) < -CLV_IMPLAUSIBLE_CLOSE_EDGE
    )
    w("  reference, STORED close on the same rows:")
    w(
        f"    fabricated: {sum(1 for r in rows if r.stored_fabricated())}   "
        f"tautological: {sum(1 for r in rows if r.stored_tautological())}   "
        f"stored implausible-NEGATIVE edge: {n_stored_neg}"
    )
    w("")

    # ---- (e) stored sharp-close freshness shadow (opt-in) -------------------- #
    if freshness_caps:
        out.extend(_freshness_section(rows, freshness_caps))

    w("=" * 78)
    w("SHADOW ONLY — no flag flipped, no rows written. Operator signs any flip.")
    w("=" * 78)
    return "\n".join(out)


async def _main() -> None:
    ap = argparse.ArgumentParser(description="CLV close-freshness shadow study (read-only).")
    ap.add_argument("--limit", type=int, default=None, help="cap settled picks (smoke run)")
    ap.add_argument(
        "--max-sharp-close-age-minutes",
        type=float,
        nargs="+",
        default=None,
        metavar="MIN",
        help=(
            "SHADOW re-report of the stored-close trusted subset excluding sharp "
            "closes older than each cap (minutes before kickoff). Report-only: "
            "no production gate, no config flag."
        ),
    )
    args = ap.parse_args()
    print(await run_study(args.limit, args.max_sharp_close_age_minutes))


if __name__ == "__main__":
    asyncio.run(_main())
