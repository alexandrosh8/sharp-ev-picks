# Diagnosis: Why Strategy Shows Negative CLV (2026-07-07)

## Verdict

The negative CLV is a **SELECTION** problem — not math, not money management.

## Ruled Out

- **Math**: All formulas verified (devig, edge, EV, CLV). CLV recomputed from raw
  data matches stored values to <1e-4.
- **Kelly staking**: Textbook-correct. 0.25x fraction + 2% cap compresses the
  flat-vs-Kelly ROI difference to <2.6pp on every bucket, and on the *losing*
  buckets staking is neutral-to-slightly-better than flat. `corr(edge, realized
  ROI) ~ 0` — the claimed edge has no predictive power on outcomes, so staking is
  neutral: it cannot be the cause.

## Root Cause

Two overlapping slices carry essentially all the negative CLV:

1. **Consensus-anchored picks** (CLV ~ -9%) — the soft-median (consensus) anchor
   carries zero sharp information; `edge = fair - best_soft_price` then just
   selects the *stalest soft quote*, which reverts to the sharp close (winner's
   curse). Sharp-anchored picks (Pinnacle / Betfair) are ~break-even/positive by
   contrast. Measured: soccer consensus-mint CLV -8.77% vs sharp +0.40%; the same
   pattern repeats across basketball and tennis. The consensus fair value drifts
   3.79pp on average vs the Pinnacle close (4.54pp in the critical 3-5% edge
   bucket) — a drift larger than the claimed edge.
2. **Longshots (odds >= 4.0)** (CLV ~ -30%) — classic favourite-longshot bias
   (FLB); the model overstates longshot probabilities exactly where the market is
   most efficient. Favourites/mids are flat-to-positive. The two slices overlap:
   consensus-anchored picks concentrate in the longshot tail.

## Corrections to Earlier Framing

- "Premium worse than shadow = adverse selection" was **wrong** — that was
  small-sample ROI noise (n ~ 39). CLV (the reliable signal) says the
  sharp-anchored premium tier is *better* than the consensus-heavy shadow.
- "Biggest edge = biggest error" was **wrong** — the largest edges are the only
  positive ones. The axis that matters is anchor quality + odds tail, not edge
  size (corr(edge, CLV) ~ +0.11).

## Ranked Fixes

1. `VALUE_MONEYLINE_MAX_ODDS = 4.0` (was 5.0) — already wired via
   `ValuePolicy.moneyline_max_odds`; the pipeline DEMOTES a premium H2H candidate
   above the ceiling to the volume (shadow) tier (persisted + CLV-tracked, never
   alerted). Tightening is shadow-first by construction — the [4.0, 5.0) band
   moves alerted -> shadow, never the reverse. Biggest, cheapest win. Scoped to
   H2H (`pipeline.py`, `market is Market.H2H`); OU/AH/totals untouched.
2. `VALUE_REQUIRE_SHARP_ANCHOR = True` — already wired via
   `ValuePolicy.require_sharp_anchor`; demotes consensus-anchored premium
   candidates to shadow (never dropped). Live via `.env` since 2026-06-26; now the
   code default too. Sharp-anchored CLV ~ break-even/+.
3. Promote basketball ONLY if its trusted CLV survives the shadow-first checklist
   (n + CI, source agreement, freshness, coverage) — NOT on small-sample ROI. The
   two CLV cuts disagree on basketball's sign/n (one subset showed +7.0%, t=3.33,
   n~114; another showed -3.0% shadow ROI on a different subset). Reconcile the
   subset definitions and re-measure trusted CLV before any promotion.
4. Consider Shin devig for the H2H tail (tail-LOCAL miscalibration hypothesis —
   NOTE: the prior global "calibration-haircut-not-warranted" finding was global;
   this is a distinct, still-open tail-restricted hypothesis). Requires nested-CV
   / fresh-domain evidence before shipping — do NOT tune on spent holdouts.
5. Edge-uncertainty shrink (`StakePolicy.edge_uncertainty_coef`, Baker-McHale) —
   second-order; can only reduce stakes on noisy edges, never create profit from a
   negative-CLV book. Implement only after fixes #1 and #2 show positive trusted
   CLV lift.

## Implementation Rule

All changes go shadow-first (walk-forward on fresh settled data). Holdout seasons
2425 + 2526 are SPENT — do not backtest on them. Fixes #1 and #2 are conservative
by construction (they only move picks *out* of the alerted tier into shadow, never
the reverse), so they are safe to deploy immediately; #3-#5 need forward evidence.

## Sources

Kaunitz et al. 2017 (beating soft bookmakers, not the sharp close); Pinnacle
closing-line efficiency (close ~ outcome, r^2 ~ 0.997); favourite-longshot-bias
literature; Shin (1993) / Strumbelj (2014) devig for FLB correction. Complements
ADR-0019 (H1 odds ceiling) and the `flip-require-sharp-anchor-gate-august` memory.
