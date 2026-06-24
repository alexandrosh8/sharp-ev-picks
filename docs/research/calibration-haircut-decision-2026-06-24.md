# Calibration "tail-bias haircut" — Decision Doc (2026-06-24)

Decision-support only. This system never places bets. Edges/ROI/log-loss are
informational.

## Question

A standing follow-up asked us to **calibrate the devigged fair probability
(`fair_prob`) with a "tail-bias haircut"** — "the single axis the field beats us
on" — validated walk-forward. The hypothesis: the devigged sharp anchor is
systematically biased at the probability tails (or optimistic on the bets we
actually take), and a monotone correction would improve log-loss / reduce false
+EV.

## Verdict — **NOT WARRANTED. Do not ship a haircut.**

The devigged Pinnacle `fair_prob` is **already calibrated**. No leakage-free,
walk-forward-fit recalibration beats the identity out-of-sample; the gains are
noise, and applying one would *degrade* log-loss and *demote genuine +EV picks*.
We let the data decide, and the data says the upgrade is not real.

This was confirmed by **four independent in-house cuts plus a six-agent
adversarial verification** (four refute lenses + two methodology audits) — every
one agreed.

## Evidence

Datasets (the v3 maximal-data backtest, no closing odds used as a feature):
`data/ml/value_pool_full.parquet` (210,288 selections — the **unbiased** full
pool) and `data/ml/value_candidates_v3.parquet` (95,928 — the selection-biased
bet set). Walk-forward = fit on seasons strictly before each test season.

### In-house cuts

1. **Unconditional reliability** (full pool, n=210,288): `mean_pred == base_rate
   == 0.39992` to 5 dp; decile residuals within Wilson 95% noise.
2. **Walk-forward logit-shrinkage** toward the per-selection-type base rate:
   optimal `λ* = 0.00` in **every** test fold; pooled OOS log-loss improvement
   `= 0.000000`.
3. **Edge-conditional bet set** (argmax-edge picks): residual (observed − claimed)
   is **positive** (`+0.0125` at edge ≥ 0.03, observed 0.5466 > claimed 0.5341) —
   value picks win **more** than claimed, not less, consistent with their rising
   positive CLV (+0.011 → +0.130 as edge grows). There is **no over-optimism to
   haircut**. A walk-forward edge-conditional haircut *degraded* pooled OOS
   log-loss (−0.00034) and would demote dozens of +EV picks per season.
4. **Walk-forward beta/Platt recalibration** `p' = σ(a·logit(p) + b)`: converges
   to the **identity** (slope a∈[1.00,1.08], intercept b≈0); pooled OOS
   improvement **+0.002%** (all), +0.002% (1x2), +0.004% (ou25, sign flips across
   folds). Noise.

### Adversarial verification (6 agents, all `claim_holds` / no blocking flaw)

| Lens | Pooled OOS log-loss gain | Transfers? | Harm |
| --- | --- | --- | --- |
| Walk-forward **isotonic** | −0.071% | no (1/4 folds, only "wins" by shrinking to identity) | demotes +EV picks |
| **Segment-conditional** (market×sel-type, odds-band) | −0.005% | no (signs flip fold-to-fold) | demotes 53k/82k picks |
| **Favourite-longshot tempering** (`p^T`) | −0.003% (T*≈1) | no (oracle ceiling +0.003% = noise) | worsens bet-set CLV |
| **Edge-conditional, CLV-constrained** | −0.069% (isotonic variant −11%, overfit) | no | degrades CLV |
| Audit — **leakage / look-ahead** | — | — | none blocking (fair_prob is the signal anchor, not the close: corr 0.98 but only 1.1% identical) |
| Audit — **masked subpopulation bias** | — | — | none (the longshot (0,0.15] hint fails Bonferroni — multiple-comparisons false positive) |

## Why the field's edge isn't ours to capture here

A sharp Pinnacle close already prices the favourite-longshot bias away (folklore
"FLB lives in the tails" does not apply to a sharp devig). The only place a
correction *could* matter — the bets we actually take — shows the residual in the
**conservative** direction, and that signal does not transfer across folds. Any
transform with enough freedom to "fix" the tails (isotonic, segment Platt) adds
variance without correcting real bias, so it strictly hurts held-out log-loss —
the textbook signature of an already-calibrated probability.

## What we shipped instead

Not a haircut — a **standing detector** so the door stays open without bolting on
dead, noise-fitting code:

- `app/backtesting/calibration.walk_forward_beta_gain(...)` — pure, unit-tested
  (`tests/test_calibration_walkforward.py`). Fits beta on cumulative past
  periods, scores the next, and returns `warrants_recalibration` only when the
  pooled OOS gain clears a threshold **and** every fold improves. Says "no" on
  calibrated data; flags a real, stable bias if one ever appears.
- `scripts/ml/calibration_haircut_probe.py` — the reproducible re-check
  (`uv run --extra ml python scripts/ml/calibration_haircut_probe.py`).
  Current output: FULL POOL OOS gain **−0.0008%**, BET SET **−0.0047%** →
  **NOT WARRANTED**.

## Revisit trigger

Rerun the probe as live settled volume accumulates (especially once European
majors return ~Aug 2026 and the premium tier resolves real picks). **Revisit the
haircut only if the probe flips to `WARRANTED`** — i.e. a pooled out-of-sample
log-loss gain ≥ 0.5% that is positive in every fold, on the unbiased pool, with
no CLV degradation on the bet set. Until then, `fair_prob` ships uncorrected.

## Related

- `app/backtesting/calibration.py` — `bet_band_reliability` (report-only drift
  monitor) is the companion: it *shows* calibration drift; `walk_forward_beta_gain`
  decides whether *correcting* it transfers out-of-sample.
- `docs/research/strategy-optimization-decision-2026-06-23.md` §3 P4 — ranked
  calibration low-impact; this confirms it empirically as a no-op.
