# LightGBM vs XGBoost for Tabular Sports Prediction

- **Date:** 2026-06-10
- **Method:** official documentation only (10 cited claims captured in the
  research workflow output); no repo dives. Target workload: binary/3-way
  probabilities (football 1X2, NBA moneyline/spread/totals), 10⁴–10⁵ rows,
  CPU training on Mac→Ubuntu, walk-forward refits, calibration-first
  evaluation.

## Verdict (feeds ADR-0009)

- **MVP default: LightGBM** (`LGBMClassifier`, `objective="binary"` for NBA
  markets, `"multiclass"` num_class=3 for football 1X2).
- **Benchmark challenger: XGBoost** (`XGBClassifier`,
  `tree_method="hist"`, `enable_categorical=True`) — run side-by-side in every
  walk-forward evaluation; promotion requires beating LightGBM on Brier/ECE,
  not accuracy.

## Doc-backed comparison

| Dimension                    | LightGBM                                                                                                                                                                                                                                       | XGBoost                                                                                                                |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Native categoricals          | First-class, stable: pandas `category` auto-detected; Fisher (1958) optimal split over integer codes; overfit controls `min_data_per_group`, `cat_smooth` — important for sparse categories (referee, rare opponents) on 10⁴-row football data | `enable_categorical` still flagged **experimental** in the sklearn-API reference; unsupported by the exact tree method |
| Probability output           | `predict_proba` from proper log-loss objectives                                                                                                                                                                                                | same (binary:logistic / multi:softprob)                                                                                |
| Post-hoc calibration         | **Neither library ships calibration utilities or guarantees** — both need our isotonic/beta layer on temporally separated folds (ADR-0005; calibration-eval skill)                                                                             | same                                                                                                                   |
| CPU speed                    | Histogram engine with explicit small-machine guidance (`force_col_wise`/`force_row_wise` auto-benchmarked); typically shorter wall-clock for hundreds of walk-forward refits                                                                   | `hist` competitive; both train seconds-per-fit at our scale                                                            |
| Early stopping / sklearn API | callbacks + eval_set, mature                                                                                                                                                                                                                   | mature                                                                                                                 |
| Monotonic constraints        | supported (`monotone_constraints`) — lets us force edge monotone in odds-derived features                                                                                                                                                      | supported                                                                                                              |

## Consequences

1. Both engines' raw probabilities are treated as **uncalibrated** until the
   calibration layer (isotonic/beta, fitted on a temporally disjoint slice)
   passes reliability checks — Brier/log-loss/ECE are the only promotion
   metrics.
2. Categorical features (team, league, referee, rest-bucket) go to LightGBM
   as pandas `category` dtype; no one-hot explosion.
3. The challenger run is a standing fixture of every backtest report in
   `docs/backtesting/` — model choice stays evidence-based, revisitable at
   ADR-0009.
