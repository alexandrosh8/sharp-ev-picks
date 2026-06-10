# ADR-0009: Gradient-Boosting Library — LightGBM Default, XGBoost Challenger

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

NBA models (ADR-0005) and the eventual football ensemble (ADR-0004 phase 6)
need a GBM library. Official-docs comparison in
`docs/research/lightgbm-vs-xgboost.md` (10 cited claims). Both libraries are
used DIRECTLY as dependencies (user direction 2026-06-10) — proven libraries
are not rebuilt.

## Decision

- **LightGBM** is the MVP default (`LGBMClassifier`; `binary` for NBA
  markets, `multiclass` for football 1X2).
- **XGBoost** (`tree_method="hist"`) is the standing challenger in every
  walk-forward evaluation; promotion requires beating LightGBM on
  **Brier/ECE** (never accuracy).

## Justification (doc-backed)

1. Native categorical handling is stable/first-class in LightGBM (Fisher
   optimal split, `min_data_per_group`/`cat_smooth` overfit controls) vs
   still-experimental in XGBoost's sklearn API — and team/league/referee/
   rest-bucket categoricals are core features here.
2. Histogram engine wall-clock advantage compounds over hundreds of
   walk-forward refits on CPU.
3. Both emit log-loss-trained probabilities and NEITHER ships calibration —
   the isotonic/beta layer (ADR-0005) is mandatory for both, so calibration
   is not a differentiator.
4. Monotonic constraints available in both (kept for odds-derived features).

## Consequences

- `lightgbm` + `xgboost` enter `pyproject.toml` as the `models` optional
  group; installed when phase 3/5 work starts.
- Every backtest report includes the challenger row; this ADR is revisited on
  evidence.
