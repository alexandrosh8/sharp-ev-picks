# Sharp-vs-soft value backtest — held-out validation (2026-06-21)

`uv run python scripts/value_backtest.py` on football-data.co.uk historical odds
(up to 18 leagues — E0–E3, SC0, D1/D2, I1/I2, SP1/SP2, F1/F2, N1, B1, P1, T1, G1). Backtests the ACTUAL doctrine — no goals model:
**fair = devig(Pinnacle pre-match); bet the best soft price when it beats fair by
≥ threshold; one bet per match (highest-edge selection).** Thresholds swept on
TRAIN seasons; the best train threshold evaluated **once** on held-out TEST.
Decision-support only — places no bets.

## Held-out TEST (single-shot, never tuned)

Threshold chosen on TRAIN = shin devig, edge ≥ 0.03.

| Selection                      | n    | Hit   | ROI         | CLV vs Pinnacle close      | CLV vs Max-of-books |
| ------------------------------ | ---- | ----- | ----------- | -------------------------- | ------------------- |
| Bet everything (baseline null) | 5271 | 38.5% | −1.59%      | +0.0061                    | −0.0006             |
| **Edge ≥ 0.03 (strategy)**     | 62   | 50.0% | **+22.39%** | **+0.1127 ±0.039 (>2 SE)** | +0.0829             |
| → 1X2                          | 34   | 41.2% | +26.47%     | +0.1101                    | +0.0924             |
| → Over/Under 2.5               | 28   | 60.7% | +17.43%     | +0.1159                    | +0.0713             |

**Computed verdict: POSITIVE selection skill** — incremental CLV +0.1066 (>2 SE),
ROI +22.39%, and it beats even the **Max-of-books** close (strips the mechanical
best-price premium → the stricter test of selection skill).

## TRAIN sweep — the edge is in the SELECTION, not the market

Across all six devig methods the pattern is identical: edge concentrates with the
threshold; "bet everything" is ≈0.

| devig=shin       | n     | Hit   | ROI     | CLV vs Pinnacle |
| ---------------- | ----- | ----- | ------- | --------------- |
| thr 0.000 (null) | 33610 | 43.1% | +0.99%  | +0.0113         |
| thr 0.010        | 7338  | 45.5% | +3.45%  | +0.0294         |
| thr 0.015        | 2867  | 47.6% | +6.17%  | +0.0389         |
| thr 0.020        | 1146  | 50.2% | +9.89%  | +0.0523         |
| thr 0.030        | 267   | 51.7% | +17.10% | +0.0738         |

## Honest read

- **CLV is the trustworthy number; ROI is directional.** +0.11 CLV at >2 SE is the
  low-variance proof of skill. +22% ROI is on n=62 — wide variance; read as
  "consistent with a strong edge," not an expected yield.
- **Selective by design:** at 0.03 the held-out test surfaced only n=62 across all
  18 leagues over the test seasons (tens of bets/yr). 0.015 keeps CLV
  > 2 SE at far higher volume (thousands of bets) for ~6% ROI.
- **Live frictions the backtest can't model:** the Max-of-books CLV assumes you
  line-shop every book at snapshot time, and **soft books limit/cut winners**.
  Realized live ROI will be lower than this frictionless backtest.
- Scope: FOOTBALL only (the sport with deep free CLOSING-odds history). NBA is the
  other validated sport; NFL/tennis lack a free sharp close (see
  multisport-modeling-2026-06-21.md).

## Takeaway

The core strategy shows **genuine, out-of-sample selection skill** (CLV >2 SE +
positive ROI, beating the Max-of-books close) on deep historical data — the
doctrine holds where it can be tested. The live system's job is to surface these
same high-threshold edges; realized ROI hinges on actually getting the soft price
before it moves and not being limited.
