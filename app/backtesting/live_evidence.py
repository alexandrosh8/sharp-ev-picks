"""Score-stratified live CLV/ROI evidence over settled picks. Pure module.

The instrument for the VALUE_ML_FILTER flip decision (and, later, the
consensus-anchor verdict): as settled + CLV-revalidated picks accumulate,
this stratifies their CLV and ROI by

  (a) ML value-filter score bucket — >= q* / < q* / unscored (q* is the
      manifest's frozen operating point; passed in by the caller, never
      read from disk here);
  (b) tier — premium (alerted) vs volume (shadow);
  (c) anchor_type — only when the caller's schema carries it (the column
      is being added by a separate migration; rows default to None and the
      grouping is omitted entirely until real values exist).

Honesty rules (binding, mirrored by the dashboard panel):
  - every stratum reports its n (and n_clv / n_roi denominators);
  - a stratum with n_clv < min_n is marked insufficient AND its point
    estimates are nulled at the source — no consumer of GET /performance
    can read noise-level numbers for an insufficient stratum, whether or
    not it honors the flag;
  - sufficiency is judged on n_clv (CLV is the evaluation currency); ROI
    can therefore render with a thinner pnl sample — consumers must
    eyeball n_roi before leaning on a stratum's ROI;
  - aggregates are evidence, never a profit promise.

Pure: stdlib/math plus the pure numeric helpers in app/backtesting/clv.py
(numpy/scipy) — DB reads live in app/storage/repositories.py and the
composition happens in the route (app/api/routes.py).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from app.backtesting.calibration import (
    CalibrationObservation,
    CalibrationReport,
    calibration_report,
)
from app.backtesting.clv import mean_significance, wilson_interval
from app.edge.value import is_legacy_product_mismatch
from app.edge.value_policy import normalize_league

#: Below this many CLV observations a stratum is "insufficient" — the
#: dashboard shows the state instead of point estimates.
MIN_STRATUM_N = 50

#: TASK TL (2026-07-26) league-tier MEASUREMENT dimension: the commented value
#: of the (deliberately DISABLED) VALUE_MAJOR_LEAGUES flag (.env.example /
#: app/config.py `value_major_leagues` — whitelist disabled 2026-06-28, premium
#: is scoped by DATA, restore ~Aug). Hardcoded HERE because the trusted-CLV
#: scorecard needs the major/non-major split as telemetry while the env flag
#: stays off; this constant NEVER gates, demotes, or promotes a pick.
#: Membership is judged on app/edge/value_policy.normalize_league — the exact
#: normal form is_major_league would use if the flag were re-enabled.
MAJOR_LEAGUES: tuple[str, ...] = (
    "Premier League",
    "LaLiga",
    "Serie A",
    "Bundesliga",
    "Ligue 1",
    "UEFA Champions League",
    "UEFA Europa League",
    "NBA",
    "EuroLeague",
)

_MAJOR_LEAGUES_NORMALIZED = frozenset(normalize_league(name) for name in MAJOR_LEAGUES)

#: Close anchors that make a close TRUSTABLE for honest CLV — a NAMED sharp
#: book priced it, not a soft-book consensus median. Mirrors the persisted
#: anchor_type values app/edge/value.anchor_type_for emits (pinnacle / sharp);
#: kept local so this module stays stdlib-pure. "consensus" is deliberately
#: excluded (a soft-book median close is not a sharp close).
_SHARP_CLOSE_ANCHORS = ("pinnacle", "sharp")

#: CLV TAUTOLOGY epsilon — mirrors #137 (app.edge.value.CLV_TAUTOLOGY_EPS) and the
#: headline path (app.storage.repositories.CLV_TAUTOLOGY_EPS), kept local so this
#: module stays stdlib-pure. When a settled pick's CLOSE fair equals its PICK-TIME
#: fair (the SAME archived sharp line reused at pick-time and close-time), clv_log =
#: ln(fill_eff * closing_fair) merely re-encodes the pick-time edge — a TAUTOLOGY,
#: not independent close evidence. The persisted close_independent_of_fill flag was
#: fill-book-only for legacy rows, so an unmoved close that just ECHOES the pick-time
#: anchor would read as independent here without this guard. 1e-3 = the 4-dp
#: archived-line resolution.
CLV_TAUTOLOGY_EPS = 1e-3

#: Physically-impossible-CLV (CLV-1) thresholds — mirror the headline guard
#: (app.storage.repositories._clv_row_is_fabricated / CLV_IMPLAUSIBLE_*), so a
#: fabricated close cannot leak into the live_evidence panel or the trusted subset.
CLV_IMPLAUSIBLE_CLOSE_EDGE = 0.20
CLV_IMPLAUSIBLE_LOG = 0.5

#: CLV->yield calibration: the public large-sample benchmark for how much of a
#: measured CLV edge survives as realized flat-stake yield. RebelBetting's
#: 373,654-bet month showed realized yield ~= 0.8 x measured CLV (+3.3% CLV ->
#: +2.7% yield) — docs/research/2026-07-10-whole-internet-research.md
#: (commercial lane). A benchmark for CONTEXT, never a profit promise.
CLV_YIELD_BENCHMARK = 0.8
CLV_YIELD_BENCHMARK_SOURCE = (
    "RebelBetting public benchmark ~0.8x realized yield per unit CLV "
    "(docs/research/2026-07-10-whole-internet-research.md, commercial lane)"
)

#: ADR-0022 crit 3/4 cohort boundary: the premium SELECTION-fix date. Criterion
#: 3 defines the "post-fix premium cohort" as picks minted AFTER 2026-07-07
#: (the odds-ceiling 4.0 + sharp-anchor selection fixes went live 2026-07-07);
#: criterion 4 requires the trusted-CLV scorecard to report the premium tier
#: SPLIT into pre-/post-fix cohorts so the kill criterion (post-fix trusted CLV
#: 95% CI < 0 at n >= 50) is readable directly off the dashboard. Minted exactly
#: at the boundary instant counts as post-fix (>=).
PREMIUM_SELECTION_FIX_AT = datetime(2026, 7, 7, tzinfo=UTC)

#: Monte Carlo zero-edge null resampler (Task 8 probe) — method after Joseph
#: Buchdahl, "Monte Carlo or Bust" (MCoB): resample the settled record under
#: the null that every pick had ZERO edge (true win prob = offered-implied
#: 1/odds) and ask how often pure luck does at least as well. Deterministic
#: seed so the report is reproducible run-to-run.
MC_NULL_SIMS = 10_000
MC_NULL_SEED = 20260711

#: ADR-0022 crit 3 kill/keep gate PROGRESS floor: the pre-/post-fix premium
#: cohort entries expose a PROGRESS 95% t-CI (progress_ci_low/high) from this
#: n — a readout of where the kill gate is heading, clearly labelled and
#: rendered as progress. It is NOT evidence: the headline mean/CI stay nulled
#: below MIN_STRATUM_N, and the kill criterion itself still requires n >= 50.
KILL_GATE_PROGRESS_MIN_N = 10

#: A trusted CLV this close to zero cannot anchor a yield ratio — the division
#: amplifies noise without bound (live example, 2026-07-10: fractional-mean
#: trusted CLV +0.0016 vs flat yield −0.094 rendered a meaningless −59.8x).
#: Below half the benchmark's reference CLV scale (~1%) the ratio is noise,
#: so it is nulled and the dashboard shows its not-computable state.
CLV_YIELD_MIN_ABS_CLV = 0.005


@dataclass(frozen=True)
class SettledPickRow:
    """One settled pick, already reduced to plain floats at the DB boundary."""

    tier: str
    value_filter_score: float | None
    clv_log: float | None  # None = never revalidated against a close
    beat_close: bool | None
    stake: float  # recommended stake (same weighting as performance_report)
    pnl: float | None  # None = outcome recorded without a pnl figure
    anchor_type: str | None = None  # CREATION anchor — None = column absent or value missing
    # Sport key of the pick (e.g. "soccer", "basketball_nba") for the per-sport
    # evidence split. None = dimension not joined (pre-feature row / pure-test
    # construction); the report then omits the by_sport grouping entirely, the
    # same feature-detected contract as anchor_type.
    sport: str | None = None
    # CLOSE-side provenance (the anchor that produced closing_fair / clv_log):
    closing_anchor_type: str | None = None  # pinnacle / sharp / consensus; None = unknown
    has_snapshot_close: bool = False  # closing_odds present => a true snapshot close,
    #                                   not a poll-time revalidation fallback
    # INDEPENDENCE provenance (P0-1/P0-3): True  = the close anchor book differs
    # from the fill book (genuine, independent close); False = the close was
    # anchored by the pick's OWN fill book (CIRCULAR — closing == fill,
    # |clv_log|~0 — the fake-CLV that masked the -EV); None = unknown
    # (pre-column row, feature-detected). Only a definite False excludes.
    close_independent_of_fill: bool | None = None
    # CLOSE-vs-PICK fair, for the TAUTOLOGY guard (mirrors #137 —
    # app.edge.value.close_moved_from_pick_fair / persisted_close_independent):
    # the persisted close_independent_of_fill flag was fill-BOOK-only for legacy
    # rows, so a close that merely ECHOES the pick-time sharp anchor (closing_fair
    # == model_probability, the SAME archived line at pick- and close-time) reads as
    # independent there even though its clv_log just re-encodes the pick-time edge.
    # Both are feature-detected (None = column absent / unknowable fair); a tautology
    # can only be PROVEN when BOTH are present, so None on either side is NEVER
    # treated as tautological (conservative, exactly like the persisted guard).
    closing_fair_probability: float | None = None
    model_probability: float | None = None  # the pick-time fair (1/fair_odds anchor)
    # Fill (bet) odds — input to the fabricated-CLV (CLV-1) guard below.
    decimal_odds: float | None = None
    # P2-2 devig-fallback provenance: did the configured devig method fall back to
    # multiplicative at MINT / at CLOSE? When they DISAGREE (exactly one fell back)
    # the mint and close fairs used different effective methods, so the CLV is a
    # devig-method artifact, not a real line move — excluded from the trusted
    # subset. Feature-detected: None on either side = symmetric (not excluded).
    mint_devig_fell_back: bool | None = None
    close_devig_fell_back: bool | None = None
    # Mint time (Pick.created_at) for the ADR-0022 pre-/post-selection-fix
    # cohort split. None = not joined (pure-test construction) — the row is
    # then excluded from BOTH cohorts (it cannot be assigned honestly).
    minted_at: datetime | None = None
    # TASK TL (2026-07-26): market key (Pick.market, e.g. "spreads") for the
    # per-(sport, market) trusted-close coverage counter. None = dimension not
    # joined (pure-test construction) — the (sport, market) cell grouping is
    # then omitted entirely, the same feature-detected contract as sport.
    market: str | None = None
    # Canonical mint-time line detail (Pick.market_detail) — the legacy EH/AH
    # product-mismatch cohort key (2026-08-02, with sport/market/minted_at).
    # None is BOTH "dimension not joined" and the genuine NULL-detail legacy
    # rows; the cohort predicate itself resolves the ambiguity via the other
    # dimensions (None sport/market/minted_at = unprovable, never excluded).
    market_detail: str | None = None
    # Scraped league display name (League.name) for the major/non-major
    # trusted-CLV split (MAJOR_LEAGUES). None = dimension not joined — the row
    # enters NEITHER league-tier bucket (cannot be classified honestly, the
    # same contract as minted_at); '' (source omitted the league) classifies
    # as non_major, mirroring is_major_league (an unconfirmable league is
    # never major).
    league_name: str | None = None

    @property
    def devig_fallback_asymmetric(self) -> bool:
        """True when exactly one of mint/close devig fell back to multiplicative.

        Conservative: a None on either side (provenance not recorded) is treated
        as SYMMETRIC, so historical rows are never excluded on this basis."""
        if self.mint_devig_fell_back is None or self.close_devig_fell_back is None:
            return False
        return self.mint_devig_fell_back != self.close_devig_fell_back

    @property
    def is_tautological_close(self) -> bool:
        """The CLOSE fair equals the PICK-TIME fair (the identical archived line).

        clv_log is then a TAUTOLOGY that re-encodes the pick-time edge, NOT
        independent close evidence — mirrors #137 (app.edge.value.
        close_moved_from_pick_fair, inverted) and the headline path
        (app.storage.repositories._clv_row_is_tautological). Only PROVABLE when a
        clv_log AND both fair probabilities are present; a None on either side
        (feature-detected absent / unknowable fair) is never treated as
        tautological. A row with no CLV (clv_log is None) carries no close to judge.
        """
        if self.clv_log is None:
            return False
        if self.closing_fair_probability is None or self.model_probability is None:
            return False
        return abs(self.closing_fair_probability - self.model_probability) <= CLV_TAUTOLOGY_EPS

    @property
    def is_fabricated(self) -> bool:
        """CLV is physically impossible (CLV-1 pollution) — mirrors the headline
        _clv_row_is_fabricated. When BOTH real inputs (decimal_odds + closing_fair_
        probability) are present the CLV is computed from them, so the close-implied
        edge (closing_fair - 1/odds) exceeding CLV_IMPLAUSIBLE_CLOSE_EDGE is the ONLY
        fabrication test — a genuine plausible-close longshot (modest edge yet
        |clv_log| > 0.5) is NOT fabricated. The |clv_log| magnitude cutoff is a
        FALLBACK, used ONLY when an input is absent (edge uncomputable). No clv_log
        => nothing to judge (not fabricated)."""
        if self.clv_log is None:
            return False
        if self.decimal_odds is not None and self.closing_fair_probability is not None:
            try:
                implied = 1.0 / float(self.decimal_odds)
            except (ZeroDivisionError, ValueError, TypeError):
                implied = None
            if implied is not None:
                # Both real inputs present: judge by the close-implied edge ONLY.
                return (self.closing_fair_probability - implied) > CLV_IMPLAUSIBLE_CLOSE_EDGE
        # Fallback (fair prob or odds absent / unusable): magnitude is the tripwire.
        return abs(self.clv_log) > CLV_IMPLAUSIBLE_LOG

    @property
    def is_legacy_product_mismatch(self) -> bool:
        """Legacy EH/AH product-mismatch cohort membership (2026-08-02) — the
        shared pure predicate over this row's own dimensions, so the verdict
        can never diverge from the headline gate's (see app.edge.value.
        is_legacy_product_mismatch for the cohort definition + rationale)."""
        return is_legacy_product_mismatch(
            sport=self.sport,
            market=self.market,
            market_detail=self.market_detail,
            minted_at=self.minted_at,
        )

    @property
    def sharp_close(self) -> bool:
        """A TRUSTED close for honest CLV: snapshot-sourced (not a poll-time
        revalidation fallback), anchored by a named sharp book (not a soft-book
        consensus median), independent of the fill book — the close anchor
        is NOT the pick's own fill book (a circular self-priced close is fake
        CLV, |clv_log|~0, and is what masked the -EV) — AND non-tautological:
        the close fair MOVED from the pick-time fair (an identical archived line
        re-encodes the pick-time edge — fake CLV #137). Independence must be
        EXACTLY True (2026-07-10 alignment): a NULL (unknown, pre-column) flag is
        NOT trusted here, mirroring the headline predicate app.storage.
        repositories._settled_close_is_trusted (``is True``) so the two
        trusted-n figures can never drift. A proven tautology also EXCLUDES;
        an unknowable fair (pre-column) stays not-proven-tautological. These
        are the closes whose CLV the platform can stand behind."""
        return (
            self.has_snapshot_close
            and self.closing_anchor_type in _SHARP_CLOSE_ANCHORS
            and self.close_independent_of_fill is True
            and not self.is_tautological_close
            # P2-2: an asymmetric mint/close devig fallback is a method artifact.
            and not self.devig_fallback_asymmetric
            # CLV-1: a physically-impossible (fabricated) close is never trusted.
            and not self.is_fabricated
            # 2026-08-02: the legacy EH/AH product-mismatch cohort's AH-only
            # close vs a possible EH fill is a vocabulary artifact — never
            # trusted (mirrors the headline gate in app/storage/repositories).
            and not self.is_legacy_product_mismatch
        )


def _stratum_stats(rows: Sequence[SettledPickRow], min_n: int) -> dict[str, Any]:
    """Aggregates for one stratum — every estimate rides with its n."""
    # CLV-2: a CIRCULAR close (close_independent_of_fill is False — the pick's own
    # fill book pricing its own close, |clv_log|~0 fake CLV) OR a TAUTOLOGICAL close
    # (is_tautological_close — the close fair equals the pick-time fair, the SAME
    # archived line re-encoding the pick-time edge, #137) must NOT enter the CLV or
    # beat-close samples of ANY stratum; either would drag a per-anchor mean toward a
    # mechanical zero (or a fabricated value). Independence must be EXACTLY True
    # (2026-07-11 alignment, completing the 2026-07-10 pass): a NULL (unknown,
    # pre-column) flag is NOT admitted here either, matching ``sharp_close`` and
    # the headline predicate app.storage.repositories._settled_close_is_trusted
    # (``is True``) — the old only-a-proven-False-excludes contract let unknown-
    # independence rows leak into per-stratum CLV samples. pnl_rows is left
    # untouched — realized P&L is real regardless of how the close was priced.
    clv_rows = [
        r
        for r in rows
        if r.clv_log is not None
        and r.close_independent_of_fill is True
        and not r.is_tautological_close
        and not r.is_fabricated
    ]
    pnl_rows = [r for r in rows if r.pnl is not None]
    beat_rows = [
        r
        for r in rows
        if r.beat_close is not None
        and r.close_independent_of_fill is True
        and not r.is_tautological_close
        and not r.is_fabricated
    ]

    mean_clv: float | None = None
    sw_clv: float | None = None
    if clv_rows:
        mean_clv = sum(r.clv_log for r in clv_rows if r.clv_log is not None) / len(clv_rows)
        stake_total = sum(r.stake for r in clv_rows)
        if stake_total > 0.0:
            sw_clv = (
                sum(r.stake * r.clv_log for r in clv_rows if r.clv_log is not None) / stake_total
            )
    roi: float | None = None
    staked = sum(r.stake for r in pnl_rows)
    if pnl_rows and staked > 0.0:
        roi = sum(r.pnl for r in pnl_rows if r.pnl is not None) / staked
    # Sufficiency is judged on the CLV sample — CLV is the evaluation
    # currency; ROI at these n is noise either way. (n_roi can still be
    # thinner than n_clv in a sufficient stratum — consumers eyeball it.)
    sufficient = len(clv_rows) >= min_n
    if not sufficient:
        # Insufficient stratum: estimates are nulled AT THE SOURCE so no
        # consumer can mistake noise for evidence — only the denominators
        # and the flag survive (the dashboard renders the state from those).
        mean_clv = sw_clv = roi = None
        beat_rate: float | None = None
    else:
        beat_rate = (
            sum(1 for r in beat_rows if r.beat_close) / len(beat_rows) if beat_rows else None
        )
    return {
        "n": len(rows),
        "n_clv": len(clv_rows),
        "n_roi": len(pnl_rows),
        "mean_clv_log": mean_clv,
        "stake_weighted_clv_log": sw_clv,
        "beat_close_rate": beat_rate,
        "roi": roi,
        "sufficient": sufficient,
    }


def _score_bucket(score: float | None, q_star: float | None) -> str:
    if score is None:
        return "unscored"
    if q_star is None:
        return "scored"  # no operating point known: one undivided bucket
    return "score_ge_q" if score >= q_star else "score_lt_q"


def meta_model_calibration(
    rows: Sequence[SettledPickRow], *, min_n: int = MIN_STRATUM_N
) -> CalibrationReport:
    """Is the value-filter meta-model's P(beats close) CALIBRATED in production?

    For every settled pick carrying BOTH a meta-model score and a realized
    beat-close label, score `value_filter_score` (predicted P beat-close) against
    the actual outcome. Well calibrated means a 0.7 reads as a 70% beat-close
    rate. Diagnostic only — held-out CLV stays the staking arbiter (ADR-0017);
    this closes with REAL production outcomes the loop the offline calibrator
    bake-off opened on the trainer's holdout.

    CAVEAT (measurement honesty): the score predicts P(beat the vig-free
    MAX-of-books close), but `beat_close` is realized against whatever
    closing_anchor_type each pick actually got (pinnacle / sharp / consensus) —
    so this OVERALL aggregate mixes the score's target (max close) with sharp/
    consensus closes and reads apples-to-oranges. Read the per-close-anchor
    stratification (`meta_model_calibration_by_close_anchor`) instead: the
    CONSENSUS stratum is the closest realized proxy to the max-of-books close the
    score was trained to predict. The overall is kept only for continuity."""
    return calibration_report(_score_observations(rows), min_n=min_n)


def _cal_dict(cal: CalibrationReport) -> dict[str, Any]:
    """Serialize a CalibrationReport to the GET /performance payload fields."""
    return {
        "n": cal.n,
        "insufficient": cal.insufficient,
        "log_loss": cal.log_loss,
        "brier": cal.brier,
        "ece": cal.ece,
        "base_rate": cal.base_rate,
        "mean_pred": cal.mean_pred,
    }


def _score_observations(rows: Sequence[SettledPickRow]) -> list[CalibrationObservation]:
    return [
        CalibrationObservation(fair_prob=r.value_filter_score, won=r.beat_close)
        for r in rows
        if r.value_filter_score is not None and r.beat_close is not None
    ]


def meta_model_calibration_by_close_anchor(
    rows: Sequence[SettledPickRow], *, min_n: int = MIN_STRATUM_N
) -> dict[str, CalibrationReport]:
    """Meta-model calibration STRATIFIED by the close anchor each pick got.

    The score predicts P(beat the max-of-books close); stratifying by
    closing_anchor_type keeps the score-vs-label comparison internally
    consistent within each stratum (a constant target per stratum), so the
    CONSENSUS stratum — the realized close nearest the max-of-books target — can
    be read as the score-aligned calibration instead of the apples-to-oranges
    overall. Picks with no recorded close anchor are grouped under "unknown"."""
    by_anchor: dict[str, list[SettledPickRow]] = {}
    for r in rows:
        if r.value_filter_score is None or r.beat_close is None:
            continue
        key = r.closing_anchor_type or "unknown"
        by_anchor.setdefault(key, []).append(r)
    return {
        k: calibration_report(_score_observations(v), min_n=min_n) for k, v in by_anchor.items()
    }


def _trusted_clv_ci_entry(
    rows: Sequence[SettledPickRow],
    min_n: int,
    *,
    progress_min_n: int | None = None,
) -> dict[str, Any]:
    """Trusted-CLV headline for one (sub)set of TRUSTED rows: mean clv_log with
    its 95% t-CI and n. Same honesty floor as every stratum: below ``min_n`` the
    point estimates are nulled at the source; only n and the flag survive.
    Statistics reuse the existing headline machinery (mean_significance) — no
    new estimators are invented here.

    ``progress_min_n`` (ADR-0022 crit 3, kill/keep gate — cohort entries only):
    when set, the entry additionally carries ``progress_ci_low/high`` — the
    same 95% t-CI exposed as a PROGRESS readout from that (lower) n while the
    headline estimates stay nulled below ``min_n``. Progress, never evidence:
    the kill criterion itself still requires the full ``min_n`` sample."""
    clv_vals = [r.clv_log for r in rows if r.clv_log is not None]
    entry: dict[str, Any] = {
        "n": len(clv_vals),
        "mean_clv_log": None,
        "ci_low": None,
        "ci_high": None,
        "significant": False,
        "sufficient": len(clv_vals) >= min_n,
    }
    if progress_min_n is not None:
        entry["progress_min_n"] = progress_min_n
        entry["progress_ci_low"] = None
        entry["progress_ci_high"] = None
        if len(clv_vals) >= progress_min_n:
            psig = mean_significance(clv_vals)
            if psig is not None:
                entry["progress_ci_low"] = psig.ci_low
                entry["progress_ci_high"] = psig.ci_high
    if not entry["sufficient"]:
        return entry
    sig = mean_significance(clv_vals)
    if sig is not None:
        entry["mean_clv_log"] = sig.mean
        entry["ci_low"] = sig.ci_low
        entry["ci_high"] = sig.ci_high
        entry["significant"] = sig.significant
    return entry


def _clv_yield_ratio(rows: Sequence[SettledPickRow], min_n: int) -> dict[str, Any]:
    """CLV->yield calibration on the SAME trusted subset: realized flat-stake
    yield divided by the mean fractional trusted CLV, displayed against the
    RebelBetting public 0.8x benchmark (CLV_YIELD_BENCHMARK_SOURCE).

    Units are aligned on the fractional scale: each clv_log becomes expm1(clv_log)
    (the same log->percent conversion the dashboard uses) and the yield is
    flat-stake (each pick weighted equally: mean of pnl/stake). Null semantics:
    either side below the ``min_n`` floor nulls that side AND the ratio; a
    trusted CLV within CLV_YIELD_MIN_ABS_CLV of zero nulls the ratio (a ~0
    denominator would amplify noise without bound). Denominators always survive.
    """
    clv_vals = [r.clv_log for r in rows if r.clv_log is not None]
    yield_vals = [r.pnl / r.stake for r in rows if r.pnl is not None and r.stake > 0.0]
    trusted_clv: float | None = None
    if len(clv_vals) >= min_n:
        trusted_clv = sum(math.expm1(v) for v in clv_vals) / len(clv_vals)
    flat_yield: float | None = None
    if len(yield_vals) >= min_n:
        flat_yield = sum(yield_vals) / len(yield_vals)
    ratio: float | None = None
    if (
        trusted_clv is not None
        and flat_yield is not None
        and abs(trusted_clv) >= CLV_YIELD_MIN_ABS_CLV
    ):
        ratio = flat_yield / trusted_clv
    return {
        "ratio": ratio,
        "flat_yield": flat_yield,
        "trusted_clv": trusted_clv,
        "n_clv": len(clv_vals),
        "n_yield": len(yield_vals),
        "benchmark": CLV_YIELD_BENCHMARK,
        "benchmark_source": CLV_YIELD_BENCHMARK_SOURCE,
    }


def _league_tier(league_name: str | None) -> str | None:
    """TASK TL league-tier of one row: 'major' / 'non_major' / None (unknown).

    Membership is exact on the normalize_league normal form of MAJOR_LEAGUES —
    the identical comparison is_major_league would apply if VALUE_MAJOR_LEAGUES
    were re-enabled. None (dimension not joined) classifies as NEITHER; a
    present-but-unlisted (or blank) name is non_major."""
    if league_name is None:
        return None
    return "major" if normalize_league(league_name) in _MAJOR_LEAGUES_NORMALIZED else "non_major"


def _trusted_close_gap_cells(
    by_sport_market: dict[tuple[str, str], list[SettledPickRow]], min_n: int
) -> list[dict[str, Any]]:
    """TASK TL per-(sport, market) trusted-close coverage counter cells.

    ``n_missing_trusted_close`` counts settled picks WITHOUT a trusted sharp
    close (``sharp_close`` is False) — the exact leak that keeps a promotion-
    critical cell (basketball spreads: needs n_trusted >= 100) from accruing
    evidence. Counts are denominators and always survive; the honesty flag
    ``sufficient`` (n_trusted_close >= min_n, the same evidence floor as every
    stratum) rides EVERY cell so a thin cell can never read as a bare number."""
    cells: list[dict[str, Any]] = []
    for (sport, market), cell_rows in sorted(by_sport_market.items()):
        n_trusted = sum(1 for r in cell_rows if r.sharp_close)
        cells.append(
            {
                "sport": sport,
                "market": market,
                "n_settled": len(cell_rows),
                "n_trusted_close": n_trusted,
                "n_missing_trusted_close": len(cell_rows) - n_trusted,
                "sufficient": n_trusted >= min_n,
            }
        )
    return cells


def _mint_cohort(minted_at: datetime | None) -> str | None:
    """ADR-0022 crit 3 cohort of one mint time: 'pre_fix' / 'post_fix' / None.

    None (mint time unknown) is assigned to NEITHER cohort. A naive datetime
    can only come from a caller bug or a fixture — the DB is TIMESTAMPTZ — and
    is read as UTC rather than raising inside a report."""
    if minted_at is None:
        return None
    m = minted_at if minted_at.tzinfo is not None else minted_at.replace(tzinfo=UTC)
    return "post_fix" if m >= PREMIUM_SELECTION_FIX_AT else "pre_fix"


def mc_null_record(
    rows: Sequence[SettledPickRow],
    *,
    min_n: int = MIN_STRATUM_N,
    sims: int = MC_NULL_SIMS,
    seed: int = MC_NULL_SEED,
) -> dict[str, Any]:
    """Monte Carlo LUCK probe on the settled trusted subset (Task 8).

    Method after Joseph Buchdahl, "Monte Carlo or Bust" (MCoB): each pick's
    flat-stake outcome is resampled under the NULL that its true win
    probability is its OFFERED-implied probability (1/odds — i.e. zero edge:
    win +(odds-1) units, lose -1 unit). ``p_luck`` is the fraction of null
    simulations whose total units are >= the observed total (pnl/stake summed
    over the sample) — a low value says the record is unlikely to be pure luck
    at the offered prices. A probe, never a profit promise. Deterministic
    (numpy Generator, fixed seed) and nulled below the ``min_n`` honesty floor;
    the denominators always survive. Pure: numpy + the row inputs only.
    """
    sample = [
        (float(r.decimal_odds), r.pnl / r.stake)
        for r in rows
        if r.pnl is not None
        and r.stake > 0.0
        and r.decimal_odds is not None
        and r.decimal_odds > 1.0
    ]
    n = len(sample)
    out: dict[str, Any] = {"n": n, "observed_units": None, "p_luck": None, "sims": sims}
    if n < min_n:
        return out
    odds = np.array([o for o, _u in sample], dtype=np.float64)
    observed = float(sum(u for _o, u in sample))
    rng = np.random.default_rng(seed)
    wins = rng.random((sims, n)) < (1.0 / odds)
    totals = wins @ (odds - 1.0) - (~wins).sum(axis=1)
    out["observed_units"] = observed
    out["p_luck"] = float(np.mean(totals >= observed))
    return out


def _evidence_verdict(rows: Sequence[SettledPickRow], min_n: int) -> str:
    """Plain-language verdict on the trusted subset, driven ONLY by the two
    EXISTING significance gates: the t-CI on mean clv_log excluding 0 (either
    side — a reliably negative CLV also judges), and the Wilson 95% lower
    bound on the beat-close rate clearing 0.5. No new statistics are invented;
    below the floor the verdict is honestly insufficient."""
    clv_vals = [r.clv_log for r in rows if r.clv_log is not None]
    n = len(clv_vals)
    if n < min_n:
        return (
            "evidence insufficient to judge profitability at current n "
            f"(n_trusted={n} below the {min_n} floor)"
        )
    sig = mean_significance(clv_vals)
    ci_excludes_zero = sig is not None and sig.std > 0.0 and (sig.ci_low > 0.0 or sig.ci_high < 0.0)
    beat_known = [r.beat_close for r in rows if r.beat_close is not None]
    wilson = (
        wilson_interval(sum(1 for b in beat_known if b), len(beat_known)) if beat_known else None
    )
    wilson_clears = wilson is not None and wilson[0] > 0.5
    if ci_excludes_zero or wilson_clears:
        gates = []
        if ci_excludes_zero:
            gates.append("trusted-CLV 95% CI excludes 0")
        if wilson_clears:
            gates.append("beat-close Wilson lower bound > 0.5")
        return (
            "evidence sufficient to judge profitability at current n "
            f"(n_trusted={n}; {'; '.join(gates)})"
        )
    return (
        "evidence insufficient to judge profitability at current n "
        f"(n_trusted={n}; trusted-CLV 95% CI straddles 0 and the beat-close "
        "Wilson lower bound does not clear 0.5)"
    )


def live_evidence_report(
    rows: Sequence[SettledPickRow],
    *,
    ml_threshold: float | None,
    min_n: int = MIN_STRATUM_N,
) -> dict[str, Any]:
    """Stratified live evidence over settled picks (see module docstring).

    `ml_threshold` is the manifest's frozen operating point q* (None when no
    artifact is configured: scores then fall into one "scored" bucket).
    `by_anchor` is None — meaning "dimension not available" — until at least
    one row carries an anchor_type value; consumers must distinguish that
    from an empty grouping.
    """
    if ml_threshold is not None and not math.isfinite(ml_threshold):
        raise ValueError(f"ml_threshold must be finite, got {ml_threshold}")

    by_score: dict[str, list[SettledPickRow]] = {}
    by_tier: dict[str, list[SettledPickRow]] = {}
    by_anchor: dict[str, list[SettledPickRow]] = {}
    by_close_anchor: dict[str, list[SettledPickRow]] = {}
    by_sport: dict[str, list[SettledPickRow]] = {}
    by_sport_market: dict[tuple[str, str], list[SettledPickRow]] = {}
    for row in rows:
        by_score.setdefault(_score_bucket(row.value_filter_score, ml_threshold), []).append(row)
        by_tier.setdefault(row.tier, []).append(row)
        if row.anchor_type is not None:
            by_anchor.setdefault(row.anchor_type, []).append(row)
        if row.closing_anchor_type is not None:
            by_close_anchor.setdefault(row.closing_anchor_type, []).append(row)
        if row.sport is not None:
            by_sport.setdefault(row.sport, []).append(row)
        # TASK TL: the trusted-close coverage counter needs BOTH keys — a row
        # missing either cannot be placed in a (sport, market) cell honestly.
        if row.sport is not None and row.market is not None:
            by_sport_market.setdefault((row.sport, row.market), []).append(row)
    # The TRUSTED subset: closes the platform can stand behind for honest CLV
    # (a genuine sharp snapshot close, not a consensus median or a poll-time
    # revalidation fallback). Always reported — n=0 honestly says "none yet".
    sharp_rows = [r for r in rows if r.sharp_close]
    trusted_by_tier: dict[str, list[SettledPickRow]] = {}
    for r in sharp_rows:
        trusted_by_tier.setdefault(r.tier, []).append(r)
    # ADR-0022 crit 3/4: the PREMIUM trusted subset split into pre-/post-
    # selection-fix mint cohorts (boundary PREMIUM_SELECTION_FIX_AT). Rows with
    # an unknown mint time enter NEITHER cohort.
    premium_cohorts: dict[str, list[SettledPickRow]] = {"pre_fix": [], "post_fix": []}
    for r in trusted_by_tier.get("premium", []):
        cohort = _mint_cohort(r.minted_at)
        if cohort is not None:
            premium_cohorts[cohort].append(r)
    # TASK TL: major/non-major league-tier split of the SAME trusted subset
    # (MAJOR_LEAGUES — the commented VALUE_MAJOR_LEAGUES value; measurement
    # only, never a gate). Both buckets always present, like premium_cohorts;
    # rows with an unknown (None) league name enter NEITHER.
    trusted_by_league_tier: dict[str, list[SettledPickRow]] = {"major": [], "non_major": []}
    for r in sharp_rows:
        tier_key = _league_tier(r.league_name)
        if tier_key is not None:
            trusted_by_league_tier[tier_key].append(r)

    cal = meta_model_calibration(rows, min_n=min_n)
    return {
        "n_settled": len(rows),
        "q_star": ml_threshold,
        "min_n": min_n,
        # Is the meta-model's P(beats close) calibrated against realized outcomes?
        # The OVERALL is apples-to-oranges (score targets the max-of-books close;
        # beat_close is vs the realized anchor close) — read by_close_anchor, whose
        # CONSENSUS stratum is the score-aligned read. Overall kept for continuity.
        "meta_model_calibration": {
            **_cal_dict(cal),
            "by_close_anchor": {
                k: _cal_dict(v)
                for k, v in sorted(
                    meta_model_calibration_by_close_anchor(rows, min_n=min_n).items()
                )
            },
        },
        "by_score": {k: _stratum_stats(v, min_n) for k, v in sorted(by_score.items())},
        "by_tier": {k: _stratum_stats(v, min_n) for k, v in sorted(by_tier.items())},
        # by_anchor stratifies on the CREATION anchor (the consensus-fallback
        # forward test); by_close_anchor stratifies on the anchor that produced
        # each CLOSE — the anchor CLV is actually measured against, so a
        # pinnacle-created/consensus-closed pick lands in the consensus stratum.
        "by_anchor": (
            {k: _stratum_stats(v, min_n) for k, v in sorted(by_anchor.items())}
            if by_anchor
            else None
        ),
        "by_close_anchor": (
            {k: _stratum_stats(v, min_n) for k, v in sorted(by_close_anchor.items())}
            if by_close_anchor
            else None
        ),
        # PER-SPORT evidence (Batch 3): each sport accumulates its OWN CLV/ROI on
        # its OWN n — a thin/experimental sport (e.g. basketball, shadow-only) can
        # never borrow another sport's sufficiency (min-n suppression is per
        # stratum). None = no row carries a sport key (feature-detected, mirrors
        # by_anchor), distinct from an empty grouping.
        "by_sport": (
            {k: _stratum_stats(v, min_n) for k, v in sorted(by_sport.items())} if by_sport else None
        ),
        # TASK TL: per-(sport, market) trusted-close coverage counter — how many
        # settled picks are NOT accruing trusted CLV evidence in each cell (the
        # SportMarketClvGate denominator leak; basketball spreads is the
        # promotion-critical cell). Feature-detected like by_sport: None until a
        # row carries BOTH sport and market keys.
        "trusted_close_gap_by_sport_market": (
            _trusted_close_gap_cells(by_sport_market, min_n) if by_sport_market else None
        ),
        "sharp_close": _stratum_stats(sharp_rows, min_n),
        # Task 4 (2026-07-10) — trusted-CLV-first operator report. All three
        # ride the SAME trusted subset (sharp_rows) and the SAME min_n floor;
        # estimates are nulled at the source below it, like every stratum.
        "trusted_clv_ci": {
            "overall": _trusted_clv_ci_entry(sharp_rows, min_n),
            "by_tier": {
                k: _trusted_clv_ci_entry(v, min_n) for k, v in sorted(trusted_by_tier.items())
            },
            # ADR-0022 crit 3/4: the premium tier split into pre-/post-
            # selection-fix mint cohorts — same entry shape and min_n floor,
            # PLUS the kill/keep-gate PROGRESS 95% CI from n >= 10 (crit 3
            # readout; the headline estimates stay nulled below min_n).
            "premium_cohorts": {
                k: _trusted_clv_ci_entry(v, min_n, progress_min_n=KILL_GATE_PROGRESS_MIN_N)
                for k, v in sorted(premium_cohorts.items())
            },
            # TASK TL: the SAME trusted subset split major/non-major on
            # MAJOR_LEAGUES (the commented VALUE_MAJOR_LEAGUES value —
            # measurement only, never a gate). Same entry shape and min_n
            # floor; unknown-league rows enter neither bucket.
            "by_league_tier": {
                k: _trusted_clv_ci_entry(v, min_n)
                for k, v in sorted(trusted_by_league_tier.items())
            },
        },
        "clv_yield_ratio": _clv_yield_ratio(sharp_rows, min_n),
        "evidence_verdict": _evidence_verdict(sharp_rows, min_n),
        # Task 8 probe: is the trusted flat-stake record distinguishable from
        # zero-edge luck at the offered prices? (Buchdahl MCoB resampler.)
        "mc_null": mc_null_record(sharp_rows, min_n=min_n),
    }


@dataclass(frozen=True)
class SportMarketClvGate:
    """Per-(sport, market) CLV-READINESS gate — a DOCUMENTED policy scaffold that
    is default-OFF and shadow-only. It NEVER promotes anything on its own.

    A (sport, market) is promotion-READY (eligible to leave the experimental
    shadow tier and earn alerts) ONLY when, on its OWN trusted sharp-close sample:

      - ``n_sharp_close >= min_n_sharp_close`` — enough genuine, independent sharp
        closes to measure CLV at all;
      - the sport-scoped sharp stake-weighted CLV is positive by more than
        ``min_clv_sigma`` standard errors (the held-out > 2 SE doctrine bar,
        measured PER sport/market — never borrowed from football);
      - the beat-close rate's CI lower bound exceeds ``min_beat_close_ci_lower``
        (a coin-flip beat rate is no edge).

    ``enabled`` defaults False: this is reporting-only scaffolding. No code path
    flips ``enabled`` or auto-promotes a sport; an operator must both enable the
    gate AND the evidence must clear every bar. Promotion stays a deliberate,
    human, ADR-logged act. Pure: stdlib only, no env/DB/HTTP — policy enters as a
    frozen dataclass from the composition root, like every other gate here.
    """

    enabled: bool = False
    min_n_sharp_close: int = 500
    min_clv_sigma: float = 2.0
    min_beat_close_ci_lower: float = 0.5

    def is_ready(
        self,
        *,
        n_sharp_close: int,
        sharp_clv_mean: float | None,
        sharp_clv_se: float | None,
        beat_close_ci_lower: float | None,
    ) -> bool:
        """True ONLY if the gate is enabled AND every readiness bar is cleared.

        Disabled (the default) always returns False — the scaffold cannot promote.
        A missing/degenerate input (None mean/SE, non-positive SE, None CI bound)
        is treated as NOT ready: the gate fails closed, never open.
        """
        if not self.enabled:
            return False
        if n_sharp_close < self.min_n_sharp_close:
            return False
        if sharp_clv_se is None or sharp_clv_se <= 0.0:
            return False
        if sharp_clv_mean is None or sharp_clv_mean <= self.min_clv_sigma * sharp_clv_se:
            return False
        return beat_close_ci_lower is not None and (
            beat_close_ci_lower > self.min_beat_close_ci_lower
        )
