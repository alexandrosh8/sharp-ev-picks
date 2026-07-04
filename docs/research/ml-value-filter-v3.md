# ML Value Filter v3 — Drop `book_count` (Round A9)

**Date:** 2026-07-04 · **Verdict: CANDIDATE (shadow)** — by construction; the
trainer cannot emit ADOPT. Binding verdict remains live shadow CLV + the
fresh 2627 season, exactly as for v2.

## 1. Scope — one change

Remove `book_count` from the feature set and retrain under the EXACT v2
protocol (`scripts/ml/train_value_filter_v2.py` docstring; digest in
`docs/research/premium-tier-v2.md`). Motivation: v2's final model gave
`book_count` **zero gain and zero splits** (v2 manifest importances), and its
maxavg-era semantics are fragile (`docs/research/ml-value-filter.md` §8).

The 37 dataset-v2 features (rolling form, xG, devig deltas, odds_band) were a
REAL NULL in v2 (`feature_lift_v1_to_v2_logloss = -0.00261`) and were **not
retried** — the v3 trainer has no V2-feature arm by construction and asserts
none of them enter the feature set.

## 2. Protocol fidelity (nothing re-tuned)

- Trainer: `scripts/ml/train_value_filter_v3.py` — thin wrapper importing all
  machinery from the v2 script (loader, folds, ES fitters, calibration,
  sweep sampler, operating-point criterion). Checkpoint/resume was added as
  pure compute plumbing (per-draw metrics JSONL; the selected draw is refit
  and its checkpointed log-loss asserted reproduced to <1e-9).
- Identical rows: `data/ml/value_candidates_v2.parquet`, sha256 asserted
  equal to the v2 manifest pin (`b5d5f701…`).
- Identical folds (the SAME season-blocked expanding walk-forward v2 used),
  identical seeds (20260612 / sweep 20260712), identical 100-draw sweep
  sequence — draw 81 is asserted byte-identical to the v2 winner's params.
- **Spent-holdout discipline held:** seasons 2425/2526 are filtered out at
  load by the reused v2 loader (asserted); NO number in this run touches
  them. The EC/SC1/SC2/SC3 fresh pool was a pre-registered ONE-SHOT consumed
  by v2 and was **not re-consulted** (recorded in the manifest).
- Grounding parity (harness at parity with the v2 protocol, drift = 0.0):
  v1 winner reproduces OOF log-loss 0.651751143; v2's selected draw-81
  params reproduce 0.649683881.

## 3. Result — pooled OOF on the same folds (never accuracy)

| arm                          | features | log-loss    | brier   | ece    |
| ---------------------------- | -------- | ----------- | ------- | ------ |
| v1_grounding (v1 winner)     | 14       | 0.65175     | 0.22883 | 0.0225 |
| v2 selected (draw 81, refit) | 14       | 0.64968     | 0.22873 | 0.0217 |
| **v3 selected (draw 73)**    | **13**   | **0.64932** | 0.22874 | 0.0246 |

- **Delta (v3 − v2 selected): −0.00036 log-loss** — dropping `book_count`
  costs nothing; the best-of-100 on 13 features is marginally better than
  v2's best-of-100 on 14. (The v2 winner's own params on the 13-feature set,
  draw 81, score 0.64949 — also ≤ v2.) The margin is noise-level; the honest
  claim is "no OOF cost", not "lift".
- Feature count 14 → 13 (10 numeric + 3 categorical); `book_count` absent
  from the model and its importances (asserted).
- Operating point (v1 criterion, train-OOF only): **q\* = 0.725** (unchanged
  from v1/v2), n = 556, ROI +6.05% [−6.4%, +16.3%], incCLV_max +0.0285 ±
  0.0047 (2SE), match-clustered bootstrap.
- Final model: LightGBM, fit 1920–2223, early-stop + isotonic on 2324,
  best_iteration 246. Artifacts: `data/ml/value_filter_model_v3.txt`,
  `value_filter_manifest_v3.json`, `value_filter_v3_sweep.csv` (same
  locations/naming scheme as v2; deployed v1/v2 files untouched).

**Explicitly: the 2425/2526 holdout was NOT touched; the fresh one-shot pool
was NOT re-consulted. Every number above is train-OOF on seasons ≤ 2324.**

## 4. Status and how to apply

- Manifest verdict is `CANDIDATE` — the live loader refuses it without
  `VALUE_ML_MANIFEST_ALLOW_SHADOW`, and a shadow manifest can never demote.
  The live value_filter stays on whatever manifest it currently uses; no
  flag was flipped in this round.
- Promotion path unchanged (v2 digest §5): score-stratified live shadow CLV
  plus the never-consulted 2627 season.

## 5. Decision-support only

Scores, edges, and EV are informational. This system never places bets
(ADR-0002); nothing in this round changes that.
