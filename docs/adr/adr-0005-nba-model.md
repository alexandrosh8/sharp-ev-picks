# ADR-0005: NBA Probability Engine — LightGBM with Isotonic Calibration

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

NBA markets: moneyline, spread cover, totals (team totals later; player props
optional future phase). NBA specifics: high game-to-game variance, heavy
schedule effects (rest, back-to-backs, travel), late scratches, pace dictating
totals. Candidates: Elo, logistic regression, GBMs (LightGBM/XGBoost/
CatBoost), neural nets, Bayesian hierarchical models. Research grounding:
official LightGBM/XGBoost docs comparison (docs/research/lightgbm-vs-xgboost.md),
nba_api (3.7k★, adopted for ingestion), inspected NBA repos (kyleskom — Kelly
test oracle; nealmick — cautionary feature gaps).

## Decision

**LightGBM classifier per market family** (ADR-0009 fixes the library), with
an **isotonic calibration layer** fitted on temporally disjoint folds, serving
probabilities for moneyline (binary), spread cover (binary vs the quoted
line), and totals (binary over/under the quoted line). XGBoost runs as the
standing challenger in every walk-forward evaluation.

## Feature set (leakage-safe; all as-of pre-tipoff)

- **Schedule:** rest days each side, back-to-back flags, 3-in-4 flags,
  travel (km since last game, home stand/road trip length).
- **Team form:** rolling-window (5/10/20, shift-1) offensive/defensive
  ratings, pace, eFG%, TOV%, ORB%, FT rate (Four Factors), garbage-time
  filtered where the source provides it.
- **Market context:** the quoted line/total being priced (the model predicts
  cover/over probability for that line — never sees closing odds of the
  target game as a feature).
- **Availability:** injury/lineup snapshot with as-of timestamp; minutes-
  weighted on/off adjustments in a later iteration.
- Categoricals (team, opponent, rest-bucket) passed natively as pandas
  `category` (LightGBM strength).

## Variance handling

1. Bet markets, not winners: outputs are market probabilities (cover/over),
   which strips most blowout noise.
2. Edges are shrunk by measured calibration error: effective edge =
   max(0, edge − ECE_market) before gating — high-variance markets must clear
   a higher bar.
3. Late scratches: lineup-data freshness rides on the alert
   (odds-age + availability-snapshot age); stale availability fails the gate.

## Calibration & validation

- Isotonic regression (beta calibration as fallback at small n) fitted on a
  validation slice temporally disjoint from both training and test.
- Headline metrics: Brier, log-loss, ECE + reliability diagrams per market;
  accuracy is never a promotion criterion.
- Walk-forward by season-chunks with embargo equal to the longest rolling
  window.

## Why it beats simple Elo

Elo compresses everything into one rating: no rest/travel/lineup capacity, no
totals pricing (pace ignored), no line-conditional probabilities. LightGBM
consumes Elo-style strength as ONE feature among schedule/pace/availability
features and prices the actual quoted line. Logistic regression is the
degenerate case (kept as a sanity baseline in backtests); neural nets and
Bayesian hierarchies are rejected for MVP on data-efficiency and operational
complexity at 10⁴-row scale.

## Consequences

- Phase 5 implements: nba_api ingestion, feature builders, LightGBM training
  - isotonic layer, walk-forward harness, model registration.
- Historical odds base: sportsbookreview bundled 2011–2021 dataset + self-
  captured closes going forward (ADR-0010).
