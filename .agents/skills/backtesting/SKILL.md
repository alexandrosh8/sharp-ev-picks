---
name: backtesting
description: "Leakage-free walk-forward backtesting. Use when building or reviewing any backtest, settlement replay, or strategy evaluation."
allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
---

# Backtesting

## Purpose

Validate strategies with realistic execution assumptions and zero temporal
leakage — full methodology lives in the global `walkforward-backtest` skill;
this encodes the project's specifics.

## Procedure

1. Walk-forward only: train on `[t0, t1)`, evaluate on `[t1, t2)`, roll
   forward. No information from the evaluation window touches training or
   calibration.
2. Prices: bets are evaluated at **signal-time odds** (the snapshot that
   triggered the pick), never closing or best-available-later prices.
3. Costs: apply realistic slippage and (for exchange odds) commission;
   document assumptions per run in `backtest_runs`.
4. Bankroll path: simulate sequential stakes with the SAME caps as live
   (fractional Kelly, per-bet, daily exposure) — report max drawdown,
   not just ROI.
5. Metrics per run: ROI, CLV (log-ratio, stake-weighted), Brier/log-loss
   of underlying probabilities, n picks, drawdown. Persist to
   `backtest_runs`; summaries to `docs/backtesting/`.
6. Every run records: model_version, data window, gate thresholds, seed.

## Checklist

- [ ] No closing odds in features or pick decisions
- [ ] Settlement uses real results, joined by event id (not name fuzzing)
- [ ] Costs/slippage stated explicitly (zero is a stated assumption)
- [ ] Confidence intervals via bootstrap when comparing strategies

## Gotchas

- **Backtests that pick the best odds across books at signal time**
  overstate ROI unless the live system polls those same books at the same
  cadence — match the live polling reality.
- **Survivorship in odds snapshots**: missing snapshots near kickoff bias
  toward stale favorable prices; require odds-age gates in the replay too.
- **A profitable backtest with negative CLV is a red flag** — it usually
  means leakage or luck; CLV is the more stable signal at small n.
- **Reusing the calibration fit across folds** leaks — refit per fold.

## Forbidden mistakes

- Random shuffles/k-fold on time-ordered data.
- Reporting in-sample results as backtest performance.
- Tuning thresholds on the same window used for the headline result.
