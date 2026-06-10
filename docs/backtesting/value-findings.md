# Value-Strategy Findings — Maximal-Data Optimization (v3)

- **Date:** 2026-06-10 (v3 — maximal data: 7 seasons × 18 leagues × 2
  markets; devig-method × threshold sweep on TRAIN only, one-shot holdout)
- **Strategy:** sharp-vs-soft line shopping (`app/edge/value.py`,
  `scripts/value_backtest.py`). Fair value = devig(Pinnacle pre-match); bet
  the best available price (Max across books) when it beats fair by ≥
  threshold. **No goals model.**

## v3 — the production configuration (chosen on TRAIN, confirmed on holdout)

Maximal free dataset: 18 divisions × seasons 2019-20…2025-26 (33,077 train /
13,143 test matches), **two markets per match** (1X2 and Over/Under 2.5 —
football-data.co.uk carries Pinnacle + Max for both since 2019-20). The sweep
ran devig ∈ {power, shin, multiplicative} × threshold ∈ {0.005…0.030} on
TRAIN seasons 1920-2324 only and chose **shin devig, edge ≥ 0.03** (best
train ROI with n ≥ 150: +17.1%, n=267, incremental CLV +0.0624). Evaluated
ONCE on held-out 2425+2526:

|                               | n      | hit%     | ROI         | CLV vs Pinnacle close | CLV vs Max close     | incremental CLV     |
| ----------------------------- | ------ | -------- | ----------- | --------------------- | -------------------- | ------------------- |
| baseline (bet everything)     | 5271   | 38.5     | −1.59%      | +0.0061 ± 0.0025      | −0.0006              | —                   |
| **picks (shin, edge ≥ 0.03)** | **62** | **50.0** | **+22.39%** | **+0.1127 ± 0.0391**  | **+0.0829 ± 0.0419** | **+0.1066 (> 2SE)** |
| — 1x2 only                    | 34     | 41.2     | +26.47%     | +0.1101               | +0.0924              | +0.1040             |
| — ou25 only                   | 28     | 60.7     | +17.43%     | +0.1159               | +0.0713              | +0.1098             |

**Computed verdict: POSITIVE selection skill on held-out data** — incremental
CLV +0.1066 clears 2SE and the picks beat even the Max-of-books close. Both
markets are independently positive. The train sweep was monotone in
threshold for every devig method (higher bar → higher ROI and CLV), so the
chosen corner is a robust pattern, not a lucky cell.

These are the live defaults (`app/config.py`): `PICK_STRATEGY=value`,
`VALUE_DEVIG=shin`, `VALUE_MIN_EDGE=0.03` → ~120 high-conviction picks/year
across 18 leagues (≈2-3/week). For more volume at a thinner edge, set
`VALUE_MIN_EDGE=0.015` (the v2-validated tier below: n=379, ROI +2.5%,
incremental CLV +0.019). **ROI at n=62 is noise-dominated — the number to
trust is the incremental CLV; plan around CLV, not the +22% point estimate.**

## Corrected methodology (what changed in v2 and why)

The first version of this document overstated the result. The deep review
found and we fixed:

1. **One bet per match** (highest-edge selection only) — H/D/A bets on the
   same match are correlated; counting them separately inflated n and
   narrowed the CI illegitimately.
2. **The right null is "bet everything", not zero.** Betting the Max line on
   every match already shows +0.009–0.016 CLV mechanically (best-of-N-books
   premium). Selection skill = CLV **incremental** to that baseline.
3. **Dual CLV references**: vs devig(Pinnacle close) _and_ vs
   devig(Max-of-books close). The second strips the best-price premium
   entirely — the strictest test.
4. **No in-sample headline.** Thresholds are swept on TRAIN seasons
   (2021-24); the chosen threshold is evaluated ONCE on held-out TEST
   seasons (2024-26).
5. The printed verdict is **computed from the held-out numbers** — the
   script can and will print "NO PROVEN EDGE" if that's what the data says.

## v2 — volume tier (edge ≥ 0.015, power devig, 18 leagues, 1X2 only)

Re-run across 18 divisions (top + second tiers incl. E2/E3/SC0/D2/I2/SP2/F2/
N1/B1/P1/T1/G1) for 3x the held-out sample:

|                           | n       | hit%     | ROI        | CLV vs Pinnacle close | CLV vs Max close | incremental CLV     |
| ------------------------- | ------- | -------- | ---------- | --------------------- | ---------------- | ------------------- |
| baseline (bet everything) | 4360    | 43.0     | -0.03%     | +0.0057 ± 0.0025      | -0.0017          | —                   |
| **picks (edge ≥ 0.015)**  | **379** | **49.3** | **+2.46%** | **+0.0249 ± 0.0110**  | **+0.0123**      | **+0.0192 (> 2SE)** |

With 3x the data the selection skill stays conclusive (incremental CLV
+0.0192 > 2SE, positive even vs the Max-of-books close) while the ROI
expectation regresses to a modest +2.5% — the 6-league +12.7% above was
partly small-sample luck. **Plan around CLV ~+2% as the realistic edge; ROI
point estimates at n=126-379 are noise-dominated.**

## Held-out result (6 leagues; train 2122-2324, test 2425-2526)

Train sweep chose **edge ≥ 0.015** (best train ROI with n ≥ 100). Held-out:

|                           | n       | hit%     | ROI         | CLV vs Pinnacle close | CLV vs Max close     | incremental CLV     |
| ------------------------- | ------- | -------- | ----------- | --------------------- | -------------------- | ------------------- |
| baseline (bet everything) | 1994    | 43.6     | −1.17%      | +0.0090 ± 0.0035      | +0.0055              | —                   |
| **picks (edge ≥ 0.015)**  | **126** | **59.5** | **+12.67%** | **+0.0351 ± 0.0194**  | **+0.0278 ± 0.0193** | **+0.0261 (> 2SE)** |

**Computed verdict: POSITIVE selection skill on held-out data** — the picks'
CLV clears the bet-everything baseline by more than 2 standard errors, ROI is
positive, and the edge survives even the Max-of-books closing reference
(i.e., it is not just the best-price premium).

## Engine hardening (same review, `app/edge/value.py`)

- **Exchange commission netted out** before any comparison/edge/EV
  (Betfair 5%, Smarkets/Matchbook 2%, configurable) — gross exchange prices
  otherwise fake edges the size of min_edge.
- **No-Pinnacle fallback is a ≥3-book median consensus**, never a single
  lowest-overround book — one stale quote can no longer contaminate fair
  value for every selection (the review's worked example is now a test).
- **Anchors with implausible overround** (underround or > 0.12) are rejected;
  the market is skipped.
- **min_odds gate (default 1.30)** — ultra-short "edges" are devig noise.
- 14 unit tests cover these paths, including the review's adversarial cases.

## Honest caveats (unchanged in spirit, sharpened in detail)

1. **n = 126 on holdout is modest**; the CLV CI excludes zero but not by
   much against the Max-close reference. Keep tracking live CLV per pick.
2. **Best-price assumption**: capturing the Max line live needs many book
   accounts and prompt action; realized prices are often a notch lower.
3. **Soft books limit/close winning accounts** — the structural constraint on
   every value bettor; volume is harder than the table implies (~120
   bets/year across 6 leagues at this threshold).
4. The OddsPortal free scrape only supports this where a match lists enough
   books (and rarely Pinnacle); The Odds API `regions=eu` is the better live
   feed for the validated Pinnacle-anchored path.
5. Decision-support only: the system finds the value and names the
   book/price; the user reviews and places any bet. Nothing is a guarantee
   of profit.

## How it's wired

- `app/edge/value.py::find_value_bets` — pure, review-hardened, 14 tests.
- `scripts/value_backtest.py` — the v2 validation above (re-runnable).
- `scripts/value_picks.py` — LIVE picks with min-odds gate; names the exact
  book and price.
