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

> **Backtest-honesty caveats (audit 2026-07-01) — read every number above as
> an upper bound.** (1) _Fill universe:_ these fills are football-data's
> **gross Max across ALL books** (exchanges included, gross of commission),
> while live fills at the best soft book or an exchange net of commission —
> optimistic vs live. `scripts/value_backtest.py --fill-universe soft` now
> fills at the best NAMED soft book only (Pinnacle never a fill; Betfair
> Exchange prices only net of commission). (2) _Correlation:_ the "> 2SE"
> here treated same-match 1X2 + OU2.5 picks as i.i.d.; they are correlated,
> so that SE was understated. The script's verdict now gates on a
> cluster-robust (by-match) SE, printed next to the i.i.d. one. No
> re-anchored number is quoted: the 2025 holdout is **spent** (ADR-0019) —
> the defensible soft-net, clustered figure awaits a single-shot run on
> fresh 2026 data.

These are the live defaults (`app/config.py`): `PICK_STRATEGY=value`,
`VALUE_DEVIG=shin`, `VALUE_MIN_EDGE=0.03` → ~120 high-conviction picks/year
across 18 leagues (≈2-3/week). For more volume at a thinner edge, set
`VALUE_MIN_EDGE=0.015` (the v2-validated tier below: n=379, ROI +2.5%,
incremental CLV +0.019). **ROI at n=62 is noise-dominated — the number to
trust is the incremental CLV; plan around CLV, not the +22% point estimate.**

**User odds floor (`VALUE_MIN_ODDS=1.60`):** re-running the full procedure
with `--min-odds 1.6` leaves the train choice unchanged (shin/0.03, train
ROI +18.8% n=234) and the holdout intact: **n=58, ROI +21.1%, incremental
CLV +0.1082 (> 2SE), CLV vs Max close +0.0826.** The floor costs almost
nothing — picks at this edge threshold rarely sit below 1.60 anyway.

**v4 — seven devig methods (odds_ratio, logarithmic,
differential_margin_weighting added, parity-tested vs penaltyblog 1e-8):**
with the 1.60 floor the train sweep now chooses **differential-margin /
0.03** (train ROI +19.5%, n=249, ahead of shin's +18.8%); one-shot holdout:
**n=61, ROI +21.1%, incremental CLV +0.1058 (> 2SE), beats the Max close.**
These are the live defaults. Caveats: (1) shin/0.03 is statistically
indistinguishable on holdout (+0.1082 vs +0.1058, n≈60) — the conclusion is
method-robust; (2) the holdout has now been consulted by three sweep rounds
(v3, min-odds, v4), so treat the ROI point estimates with extra humility —
the incremental-CLV signal, stable at +0.10–0.11 across every round, is the
number to trust. odds_ratio and logarithmic produce IDENTICAL picks here
(both are monotone margin reallocations that agree at these overrounds).

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

## Track D — staking-policy evaluation (2026-06-12): drawdown-constrained Kelly vs the deployed 0.25x + 2% cap

- **Question:** as a _recommended-stake_ policy (informational only — this
  platform never places bets), does the drawdown-constrained fractional-Kelly
  variant (`app/risk/staking.py`, `STAKE_MAX_DRAWDOWN` /
  `STAKE_MAX_DRAWDOWN_PROBABILITY`, default OFF) beat the deployed default
  (0.25x Kelly + 2% per-bet cap) or flat staking?
- **Harness:** `scripts/ml/evaluate_staking.py` (tests:
  `tests/test_evaluate_staking.py`). Premium stream regenerated with the
  deployed selection config (differential-margin devig, edge ≥ 0.03, odds ≥
  1.60, 1x2+ou25, one bet per match-market — construction parity-locked
  against `scripts/value_backtest.py` by test) on **TRAIN seasons 1920–2324
  ONLY** — staking is strategy-adjacent, so the spent 2425+2526 holdout was
  not consulted (`assert_train_only` hard-fails on it). Stakes route through
  `app.risk.staking.recommended_stake` — the same code path live picks use.
- **Method:** circular block bootstrap of the chronological stream (10,000
  paths, block = 20 picks, seed 20260612), all policies paired on the same
  resampled paths; chronological sanity pass alongside. **The decision
  criterion was pre-registered in code before the first run** (constants at
  the top of the script): switch only if a variant (A) improves median
  log-growth per bet, or (B) cuts P95 max-drawdown ≥ 20% relative while
  costing ≤ 10% of median growth.
- **Stream:** n = 249 picks, 2019-08-03…2024-06-02, hit 49.0%, flat ROI
  +19.5%/bet (in-sample for the selection config — levels are optimistic;
  the _paired policy comparison_ is the result here, not the level).

| policy (multiplier)                 | medT      | P5        | P95       | g/bet         | medDD     | P95DD     | P(ruin −50%) |
| ----------------------------------- | --------- | --------- | --------- | ------------- | --------- | --------- | ------------ |
| flat 1u of 100 (context)            | 1.478     | 1.189     | 1.795     | +0.001570     | 7.4%      | 12.1%     | 0.00%        |
| **kelly 0.25x + 2% cap (deployed)** | **1.945** | **1.263** | **3.097** | **+0.002673** | **14.3%** | **22.0%** | **0.00%**    |
| dd 0.50/0.010 (0.250 — no-op)       | 1.945     | 1.263     | 3.097     | +0.002673     | 14.3%     | 22.0%     | 0.00%        |
| dd 0.50/0.005 (0.231)               | 1.901     | 1.258     | 2.960     | +0.002579     | 13.6%     | 20.9%     | 0.00%        |
| dd 0.50/0.001 (0.182)               | 1.773     | 1.249     | 2.607     | +0.002301     | 11.3%     | 17.2%     | 0.00%        |
| dd 0.30/0.050 (0.213)               | 1.857     | 1.256     | 2.838     | +0.002486     | 12.8%     | 19.7%     | 0.00%        |
| dd 0.30/0.010 (0.144)               | 1.638     | 1.224     | 2.253     | +0.001981     | 9.2%      | 14.0%     | 0.00%        |
| dd 0.20/0.050 (0.139)               | 1.621     | 1.220     | 2.211     | +0.001939     | 9.0%      | 13.5%     | 0.00%        |
| dd 0.20/0.010 (0.092)               | 1.435     | 1.166     | 1.786     | +0.001452     | 6.4%      | 9.7%      | 0.00%        |

- **Computed verdict (pre-registered criterion): KEEP the deployed default.**
  No variant passes: every multiplier-tightening cuts growth roughly in
  proportion to drawdown. The nearest miss, dd 0.50/0.001, cuts P95
  max-drawdown 21.8% but costs 13.9% of median growth (budget: ≤ 10%);
  dd 0.30/0.010 cuts the tail 36% but costs 25.9%. Crucially **P(50%
  drawdown from peak) is already 0.00% at the deployed default** on this
  stream — the capped quarter-Kelly sits well below the dangerous zone, so
  the drawdown constraint has nothing to rescue and only taxes growth.
  Verdict is stable under block-size sensitivity (10/20/50; criterion was
  fixed at 20 before running). Flat 1u grows materially slower (medT 1.478
  vs 1.945) at lower drawdown — context only, per the pre-registration.
- **No defaults change.** If a user nevertheless wants the most defensible
  tightening for personal risk preference, the exact .env lines are e.g.
  `STAKE_MAX_DRAWDOWN=0.5` + `STAKE_MAX_DRAWDOWN_PROBABILITY=0.001`
  (multiplier 0.182) — informational recommended-stake shaping only, not
  betting advice, no guarantee of profit.
- **Limitations:** train slice is in-sample for selection (paired comparison
  unaffected); daily-exposure ledger (5%) not simulated (per-bet 2% cap
  dominates at ~50 picks/season); bootstrap treats 20-pick blocks as
  exchangeable. Full tables: `data/ml/staking_evaluation*.csv` (gitignored).
