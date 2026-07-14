---
name: sports-modeling
description: "Routing layer for football/NBA modeling decisions. Use when building features, training models, or evaluating probabilities for any sport."
allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
---

# Sports Modeling

## Purpose

Route modeling work to the right method with the project's non-negotiables:
calibration-first, walk-forward-only, leakage-free.

## Procedure

1. Football MVP: Dixon-Coles (ADR-0004) — time-decayed attack/defence +
   home advantage + low-score ρ correction; markets derived from the score
   matrix (1X2 partitions, totals/AH sums, BTTS complement).
2. NBA MVP: gradient boosting (ADR-0005/0009) with rest/B2B/travel/pace/
   ratings/injury features; isotonic calibration on temporal folds.
3. Features use ONLY signal-time information: rolling stats are shifted;
   lineups/injuries carry an as-of timestamp; closing odds never appear as
   features.
4. Evaluate with Brier, log-loss, ECE + reliability diagrams per
   league/market — full derivations in global skills `calibration-eval`
   and `betting-feature-engineering` (use them; don't re-derive here).
5. Blend with market prior where it measurably improves calibration;
   document the blend weight per league.
6. Register every artifact in `model_versions` before its predictions
   flow anywhere.

## Checklist

- [ ] Temporal split only; embargo when rolling windows overlap folds
- [ ] Calibration measured before any league/market goes live
- [ ] Feature list documented with as-of semantics
- [ ] Eval results written to docs/backtesting/

## Gotchas

- **Season-to-date stats including the current game** is the most common
  leak — always shift by one game.
- **Dixon-Coles ρ only adjusts {0-0, 1-0, 0-1, 1-1}** — applying the
  correction to the whole matrix is a known implementation bug.
- **Calibrating on the training window** silently overfits — the
  calibration fit needs its own temporal slice.
- **NBA garbage time** distorts pace/rating features — prefer
  non-garbage-time ratings where the source provides them.

## Forbidden mistakes

- Random k-fold CV on time-ordered matches.
- Headline accuracy instead of Brier/log-loss/ECE.
- Shipping probabilities for an uncalibrated league/market.
