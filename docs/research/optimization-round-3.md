# Optimization Round 3 — Consensus Anchor, Asian Handicap, Live Evidence, Staking

- **Date:** 2026-06-12 (validated + finalized same day)
- **Tracks and verdicts:**

  | Track | Question                                                       | Verdict                                                                                        |
  | ----- | -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
  | A     | Is the consensus (soft-median) anchor good enough to trust?    | **STAGE** — fallback-only; live `anchor_type`-stratified CLV + season 2627 decide              |
  | B     | Does the Asian-handicap market clear the premium bar?          | **STAGE (tooling) / REJECT (premium eligibility on current evidence)** — one-shot UNDERPOWERED |
  | C     | Live-evidence instrumentation (stratified CLV, timing windows) | **ADOPT** — honest-n gates everywhere, hardened in this finalize pass                          |
  | D     | Does drawdown-constrained Kelly beat the deployed 0.25x + 2%?  | **ADOPT the KEEP-default verdict** — no variant passes the pre-registered criterion            |

- **Nothing changes live defaults.** The deployed premium tier
  (differential-margin devig, edge >= 0.03, odds >= 1.60) remains the
  alerting default; every new capability in this round is config-gated and
  default-off. Recommended stakes, edges, and EV are informational only —
  this platform never places bets and nothing here is a profit promise.
- **Code:** `scripts/ml/anchor_ah_backtest.py` (Tracks A+B),
  `app/backtesting/live_evidence.py` + `scripts/reports/timing_windows.py`
  (Track C), `scripts/ml/evaluate_staking.py` (Track D),
  `app/edge/value.py` (`anchor_type_for`, `min_acceptable_odds`),
  migration `alembic/versions/e9b3c7a1d5f2_picks_anchor_type.py`.
- **Artifacts (gitignored):** `data/ml/value_candidates_v3.parquet`
  (95,928 rows; v1/v2 parquets byte-identical after the rebuild),
  `data/ml/AH_ONESHOT_CONSUMED.json`,
  `data/ml/staking_evaluation.csv` (+ `_b10`/`_b50` sensitivity runs).

## 0. Spent-holdout ledger update (binding)

Contamination labels used below: **[TRAIN-ONLY]** = seasons <= 2324, the
development sandbox — never promotion evidence on its own;
**[FRESH-ONE-SHOT, CONSUMED]** = a pre-registered single look at a
never-consulted domain, now spent; **CONTAMINATED-REFERENCE** = any other
quantity touching seasons 2425/2526 — never a headline.

- Already consumed before this round: 18 baseline leagues + EC/SC1/SC2/SC3,
  markets 1x2+ou25, seasons 2425+2526 (4 looks), and the premium-tier-v2
  fresh slice of never-loaded divisions.
- **Consumed THIS round: the AH market, seasons 2425+2526** (one-shot
  executed 2026-06-12; §2). A power-gate implementation bug let a
  guaranteed-underpowered look run — honestly confessed in the script
  docstring, the memory ledger, and the consumption marker; the corrected
  gate plus an intent-first marker ordering are now in code with
  regression tests (`tests/test_anchor_ah_backtest.py`).
- The football-data "new leagues" country files (BRA/ARG/...) were verified
  2026-06-12 to carry **closing odds only** (every odds column is
  PSCH/MaxCH/AvgCH/B365CH-class) — no signal-time price exists, so they can
  never host the selection protocol for ANY track. They are not a usable
  fresh domain.
- **Remaining legitimate fresh domain: season 2627 alone.** Develop only on
  seasons <= 2324; pre-register every one-shot in code before running it.
  The binding verdict for every staged item below is live shadow CLV +
  season 2627.

## 1. Track A — consensus anchor (STAGE)

### Methodology

`app/edge/value.py` falls back to a soft-book **median consensus** anchor
(>= 3 books) when no sharp book prices a market. Question: is selection
anchored on that consensus comparable to Pinnacle-anchored selection?
Dataset v3 adds consensus-anchored candidates
(`build_value_dataset.py --anchor-consensus --ah`); anchor construction was
independently verified **PS/BFE-free against the raw football-data CSVs
(40/40 spot checks exact)** — the consensus never peeks at the sharp or
closing columns. Protocol (the established one): one bet per
(match, market) = argmax edge, odds floor 1.60, dual CLV references
devig(Pinnacle close) and devig(Max close), thr=0 pool-null row (pool floor
0.005), bootstrap CIs clustered by match, seed 20260612, B=2000.
**[TRAIN-ONLY]** by construction — the script drops 2425+2526 for every
Track A computation, and no Track A fresh domain exists (§0).

### Results [TRAIN-ONLY, 1x2, maxavg era 1920–2324]

Consensus-anchored selection vs its own bet-everything null:

| thr   | n      | hit   | ROI [95% CI]          | CLVp    | CLVm    | incCLV_max [95% CI]        |
| ----- | ------ | ----- | --------------------- | ------- | ------- | -------------------------- |
| 0.000 | 10,400 | 38.6% | +0.78% [−1.9, +3.5]   | −0.0011 | −0.0005 | (null row)                 |
| 0.010 | 3,675  | 41.1% | +1.37% [−3.1, +5.8]   | +0.0113 | +0.0096 | +0.0101 [+0.0078, +0.0124] |
| 0.015 | 1,252  | 41.7% | +1.12% [−6.0, +8.6]   | +0.0285 | +0.0241 | +0.0246 [+0.0199, +0.0293] |
| 0.020 | 477    | 46.3% | +12.39% [+0.5, +24.6] | +0.0420 | +0.0342 | +0.0348 [+0.0262, +0.0443] |
| 0.030 | 119    | 48.7% | +16.34% [−6.8, +39.5] | +0.0578 | +0.0540 | +0.0546 [+0.0393, +0.0707] |

Pinnacle-anchored reference, same matches/protocol:

| thr   | n      | hit   | ROI [95% CI]          | CLVp    | CLVm    | incCLV_max [95% CI]        |
| ----- | ------ | ----- | --------------------- | ------- | ------- | -------------------------- |
| 0.000 | 12,361 | 38.5% | +2.16% [−0.4, +4.7]   | +0.0197 | +0.0138 | (null row)                 |
| 0.010 | 5,503  | 40.3% | +3.79% [−0.1, +7.4]   | +0.0284 | +0.0204 | +0.0066 [+0.0051, +0.0081] |
| 0.015 | 2,224  | 42.8% | +7.32% [+1.7, +13.1]  | +0.0379 | +0.0282 | +0.0144 [+0.0114, +0.0174] |
| 0.020 | 908    | 44.6% | +9.74% [+1.0, +18.7]  | +0.0488 | +0.0368 | +0.0231 [+0.0181, +0.0285] |
| 0.030 | 200    | 46.0% | +14.30% [−4.2, +32.7] | +0.0616 | +0.0464 | +0.0326 [+0.0202, +0.0461] |

Supplementary [TRAIN-ONLY, betbrain era — clv_pinn only, no Max close]:
consensus selection is also positive there (thr 0.03: n=432, ROI +13.80%
[+2.0, +26.5], incCLV_pinn +0.0654).

**Paired comparison on the SAME matches** (the decisive table — both
anchors bet the same (match, market)):

| thr   | both bet | same selection | paired dCLV_max (cons − pinn) [95% CI] | paired dCLV_pinn [95% CI]  |
| ----- | -------- | -------------- | -------------------------------------- | -------------------------- |
| 0.015 | 650      | 84.8%          | **−0.0071 [−0.0132, −0.0011]**         | −0.0091 [−0.0160, −0.0024] |
| 0.030 | 80       | 93.8%          | +0.0010 [−0.0117, +0.0144]             | −0.0012 [−0.0149, +0.0123] |

### Verdict: STAGE (fallback-only — do not promote)

The consensus anchor selects bets with **real incremental CLV vs its own
null** (every thr >= 0.01 CI excludes zero), so the existing fallback is
defensible. But where both anchors price the same match it is **measurably
weaker than Pinnacle at moderate thresholds** (paired dCLV_max −0.0071,
CI excluding zero, at thr 0.015; no separation at 0.03 where n=80 is
thin). Consensus therefore stays exactly what it is today: the fallback
when no sharp book prices a market — never the preferred anchor. There is
no legitimate fresh domain for a Track A one-shot (§0), so the **binding
verdict is live `anchor_type`-stratified CLV + season 2627**. Live
instrumentation shipped this round: every pick persists
`picks.anchor_type` (`pinnacle` / `sharp` / `consensus`; migration
`e9b3c7a1d5f2`), the dashboard shows the anchor chip, and
`GET /performance` stratifies live CLV by anchor (§5).

## 2. Track B — Asian handicap (STAGE tooling / REJECT premium eligibility)

### Methodology and pre-registration

The AH market was the last never-consulted market domain (the spent-holdout
ledger covered 1x2+ou25 only). Dataset rows are **half-lines only** (integer
and quarter lines carry pushes and are devig-unsound here); CLV labels exist
only where the closing line equals the pre-match line (`AHCh == AHh` — a
moved close prices a different bet; 0 moved-line label leaks across all 838
labeled rows, independently verified). Scope facts: half-lines = 23.1% of
AH-priced matches; close line == pre-match line on 60.3% of half-line
matches. The pre-registration was **frozen in code before the test slice
was ever read** (`anchor_ah_backtest.py` docstring + module constants):

- `thr_rule`: thr\* = argmax train ROI s.t. train n >= 150 and
  train (incCLV_max − 2SE) > 0; no qualifier → one-shot cancelled unconsumed.
- `pass_rule`: PASS iff test n_labeled >= 100 and test (incCLV_max − 2SE) > 0
  and test ROI > 0; n_labeled < 100 → UNDERPOWERED; else FAIL.

### Train sweep [TRAIN-ONLY, AH half-lines, 1920–2324]

| thr   | n     | n_labeled_max | hit   | ROI [95% CI]          | incCLV_max [95% CI]        |
| ----- | ----- | ------------- | ----- | --------------------- | -------------------------- |
| 0.000 | 1,449 | 756           | 52.4% | +4.02% [−0.9, +9.1]   | (null row)                 |
| 0.010 | 543   | 265           | 55.6% | +10.60% [+2.3, +18.7] | +0.0070 [+0.0025, +0.0114] |
| 0.015 | 223   | 104           | 56.5% | +12.22% [−1.3, +24.8] | +0.0096 [+0.0010, +0.0184] |
| 0.020 | 103   | 51            | 55.3% | +10.22% [−9.3, +28.8] | +0.0137 [+0.0019, +0.0258] |
| 0.030 | 26    | 12            | 50.0% | +2.85% [−37.6, +43.2] | +0.0486 [+0.0317, +0.0648] |

`thr_rule` chose **thr\* = 0.015** (reproduced exactly in this finalize
pass). **Denominator warning (validator-confirmed): the train evidence at
the operating point is thin — n=223 selected bets, only 104 CLV-labeled.
Never quote the positive train sweep without those denominators.**

### The one-shot [FRESH-ONE-SHOT, CONSUMED 2026-06-12]

`--oneshot-ah` executed 2026-06-12 and **permanently consumed the AH
2425+2526 domain** (marker `data/ml/AH_ONESHOT_CONSUMED.json`):

| quantity              | value                                              |
| --------------------- | -------------------------------------------------- |
| thr\* (frozen)        | 0.015                                              |
| n selected            | 27                                                 |
| n_labeled (max)       | **10** (< 100 floor)                               |
| ROI                   | −10.96% [boot CI −48.1%, +28.1%]                   |
| incCLV_max            | +0.0330 [CI −0.0019, +0.0687] (SE 0.0180)          |
| verdict (frozen rule) | **UNDERPOWERED — defer to live shadow CLV + 2627** |

**Process-flaw confession (validator-verified honest):** a row-count power
gate added to cancel guaranteed-underpowered looks compared the total test
pool (140 rows) to the 100 floor instead of the selectable matches (27), so
the look executed when it should have been cancelled unconsumed. This is a
process flaw, not a reporting flaw — the result was only ever reported as
UNDERPOWERED. Hardening now in code with regression tests
(`tests/test_anchor_ah_backtest.py`): the corrected gate counts selectable
matches and cancels without touching a single label column, and the
consumption marker is written **intent-first** (before the first
label/outcome read) so a crash mid-computation can never permit a second
look — the 2627 one-shot inherits both protections.

### Verdict: STAGE (tooling) / REJECT (premium eligibility on current evidence)

AH does **not** meet the premium bar: the only fresh look is underpowered
with a negative ROI point estimate, and the positive train sweep is thin at
the operating point. The dataset/backtest tooling stays (it is exactly what
the 2627 one-shot will reuse), AH knobs stay config-gated default-off, and
AH picks keep flowing through the live pipeline at the **global** premium
floor (edge >= 0.03) like every other devig-sound market — accumulating the
live CLV evidence that, together with the 2627 one-shot, is the binding
verdict.

### Exact .env lines IF the user later chooses to enable AH at thr\*

**Not recommended on current evidence** — recorded so the option is exact.
Lowering the AH premium floor to the train-chosen operating point uses the
per-market override knob (line-qualified keys, the six scraped half-lines;
`spreads:0.015` would also catch European handicap and is NOT what was
evaluated):

```dotenv
VALUE_MIN_EDGE_PER_MARKET=asian_handicap_-3_5:0.015,asian_handicap_-2_5:0.015,asian_handicap_-1_5:0.015,asian_handicap_-0_5:0.015,asian_handicap_+0_5:0.015,asian_handicap_+1_5:0.015
```

(Settings validates every override >= `VALUE_VOLUME_MIN_EDGE`, currently
0.015 — the line above sits exactly at that floor.)

## 3. Track C — live-evidence instrumentation (ADOPT)

### What shipped

- **`app/backtesting/live_evidence.py`** (pure module): stratifies settled
  - CLV-revalidated picks by ML score bucket (>= q\* / < q\* / unscored),
    tier (premium/volume), and — feature-detected, only once real values
    exist — `anchor_type`. Every stratum carries `n`, `n_clv`, `n_roi`;
    a stratum with `n_clv < 50` is `sufficient: false` and (hardened in this
    finalize pass) its point estimates are **nulled at the source**, so no
    consumer of `GET /performance` can read noise-level numbers whether or
    not it honors the flag. Sufficiency is judged on `n_clv` (CLV is the
    evaluation currency); consumers must still eyeball `n_roi` before
    leaning on a sufficient stratum's ROI.
- **Dashboard panel** ("Live evidence"): renders per-stratum estimates or
  the explicit `insufficient data (n<50)` state; hidden until served.
- **`scripts/reports/timing_windows.py`**: weekly analyzer over
  `odds_snapshots` — per hours-to-kickoff bucket (>48h, 24–48h, 5–24h,
  1–5h, <1h) it reports price drift toward the close (ln(close/then)) and
  replays the live value scan at each bucket's latest observation to score
  hypothetical edge-if-detected-then against realized CLV. **Withholds all
  estimates below 30 qualified events** (>= 2 distinct pre-kickoff snapshot
  times AND a settled close within 4h of kickoff) — counts only behind an
  INSUFFICIENT banner. `odds_snapshots` started filling 2026-06-11; expect
  weeks of counts-only output first.
- **Execution helper**: `min_acceptable_odds` (pure, commission-aware)
  drives the "still +EV down to X.XX" line on alerts and the dashboard —
  the price floor at which a pick stops clearing the premium edge.

### Verdict: ADOPT

30 tests cover the honesty gates (insufficient flags, withheld estimates,
feature-detected anchor dimension, helper math). Validation confirmed the
gates render states, not estimates. The two hardening items found by
validation are fixed in this pass (§6).

## 4. Track D — staking policy evaluation (ADOPT the KEEP-default verdict)

### Methodology

`scripts/ml/evaluate_staking.py`: premium stream regenerated with the
deployed selection config (construction parity-locked against
`scripts/value_backtest.py` by test) on **[TRAIN-ONLY] seasons 1920–2324**
(`assert_train_only` hard-fails on the spent holdout — verified by test).
Stakes route through `app.risk.staking.recommended_stake` — the same code
path live picks use. Circular block bootstrap of the chronological stream
(10,000 paths, block=20, seed 20260612; within-block order preserved —
code + test), all policies paired on the same resampled paths. **Criterion
pre-registered in code before the first run:** switch only if a variant
(A) improves median log-growth per bet, or (B) cuts P95 max-drawdown >= 20%
relative while costing <= 10% of median growth.

### Results [TRAIN-ONLY; n=249 picks, 2019-08-03…2024-06-02]

Headline context: hit 49.0%, flat ROI +19.5%/bet — **in-sample for the
deployed selection config; the level is optimistic and is NOT evidence.
Only the paired policy comparison below is evidential.**

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

### Verdict: KEEP the deployed default (no variant passes)

Validation re-ran the script **byte-identical** to the stored CSV (diff
exit 0) and independently re-applied the criterion to the block-size
sensitivity artifacts (b10/b20/b50): **no variant passes at any block
size**. P(50% drawdown) is already 0.00% at the deployed default on this
stream — the constraint has nothing to rescue and only taxes growth.
Structural caveat (validator-confirmed): under proportional Kelly scaling
the drawdown cut tracks the growth cut almost one-for-one, so criterion (B)
is **near-unsatisfiable by construction** — the test structurally favors
the status quo. That is consistent with the no-default-changes doctrine,
but it is not a neutral test; treat "KEEP" as "no evidence to switch",
not "evidence that switching is harmful".

### Exact .env lines IF the user wants the tightest defensible variant

**No default changes; personal risk preference only** (informational
recommended-stake shaping; the nearest-miss variant, multiplier 0.182):

```dotenv
STAKE_MAX_DRAWDOWN=0.5
STAKE_MAX_DRAWDOWN_PROBABILITY=0.001
```

## 5. Live-evidence playbook (the round's binding instrument)

### What the dashboard panel shows (`GET /performance` → `live_evidence`)

Per stratum (score bucket × tier × anchor): `n`, `n_clv`, `n_roi`,
`mean_clv_log`, `stake_weighted_clv_log`, `beat_close_rate`, `roi`,
`sufficient`. A stratum below 50 CLV observations renders
`insufficient data (n<50)` — its estimates are null in the payload itself.
`by_anchor` is absent (null) until picks with a persisted `anchor_type`
exist, then appears automatically.

### What the timing report shows (`uv run python scripts/reports/timing_windows.py`)

Per hours-to-kickoff bucket: observation count, mean drift ln(close/then)
(negative = prices shorten toward kickoff → earlier detection got better
prices), hypothetical pick count, mean edge-if-detected-then, mean realized
clv_log, beat-close %. All estimates withheld until >= 30 qualified events.

### Decision triggers (pre-stated so the numbers are read, not fished)

- **ML flip** (`VALUE_ML_FILTER=true`, v1 ADOPT artifact): flip only when
  the `score_ge_q` stratum is sufficient AND, per the pre-registered v1/v2
  protocol, >= 300 scored settled picks exist with the >= q\* stratum
  showing **positive incremental CLV vs the unfiltered premium tier at
  2SE**. Consistency check: the v1 one-shot evidence was +0.0357 mean CLV —
  a live `mean_clv_log` for `score_ge_q` materially below `score_lt_q`, or
  a CI containing zero at n_clv >= 300, means do NOT flip (and revisit the
  artifact). The same panel with the v2 shadow annotating
  (`VALUE_ML_MANIFEST_ALLOW_SHADOW=true`) feeds v2's gate (1) in
  `docs/research/premium-tier-v2.md` §5.
- **AH enable** (the §2 .env lines): requires the **2627 one-shot PASS**
  under the frozen `pass_rule` (n*labeled >= 100, incCLV_max − 2SE > 0,
  ROI > 0). Live support signal on the way there: AH picks (family
  `spreads`, the `asian_handicap*\*`market chips) showing positive`mean_clv_log` in a sufficient stratum at the current 0.03 floor.
  Live CLV alone does NOT trigger the enable — the one-shot is the gate;
  live evidence can only veto (clearly negative live AH CLV at
  n_clv >= 50 → don't enable regardless of 2627).
- **Anchor confidence** (`by_anchor` strata): once the `consensus` stratum
  is sufficient (n_clv >= 50), compare its `stake_weighted_clv_log` to the
  `pinnacle` stratum. Within noise (CIs overlap / difference smaller than
  the train paired deficit of ~0.007) → keep the fallback as-is. Clearly
  below (a deficit >= 0.01 persisting at n_clv >= 100) → restrict
  consensus-anchored candidates to the volume tier pending 2627 (a
  config-gated change to propose separately; nothing automatic). Clearly
  comparable at scale → consensus fallback is vindicated; consider widening
  consensus-anchored coverage in a future round.
- **Timing/cadence**: once the timing report unlocks (>= 30 qualified
  events), a strongly negative drift in early buckets (>48h / 24–48h) with
  hypothetical early picks whose realized CLV beats late buckets justifies
  raising scrape cadence / earlier alerting; flat drift means cadence is
  not the constraint. No threshold pre-committed — first unlock is
  descriptive, any cadence change ships as its own reviewed change.
- **Staking**: no live trigger — the criterion failed. Re-run
  `evaluate_staking.py` when the train-season premium stream materially
  grows (>= 500 picks) or after 2627 lands.

## 6. Validator-confirmed issues → resolutions (finalize pass)

| #   | Issue (validation round)                                                                                                 | Resolution                                                                                                                                                             |
| --- | ------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | AH power-gate bound bug let an underpowered one-shot execute (domain consumed)                                           | Corrected gate (counts selectable matches) was already in code; now regression-tested: cancel path provably reads no label column (`tests/test_anchor_ah_backtest.py`) |
| 2   | `run_ah_oneshot` wrote the consumption marker AFTER computing — a crash window permitting a second look                  | **Fixed:** intent marker written before the first label/outcome read; results appended after; crash-replay regression test                                             |
| 3   | `live_evidence_report` carried point estimates for `sufficient: false` strata — non-dashboard consumers could read noise | **Fixed:** estimates nulled at the source for insufficient strata; end-to-end test asserts nulls in the `/performance` payload                                         |
| 4   | Stratum sufficiency judged on `n_clv` only; ROI can render on thin `n_roi`                                               | Documented (module docstring + §3/§5): defensible — CLV is the currency — consumers eyeball `n_roi`                                                                    |
| 5   | Track D headline flat ROI +19.5%/bet is in-sample; criterion (B) structurally favors the status quo                      | Documented (§4): only the paired comparison is evidential; "KEEP" = "no evidence to switch"                                                                            |
| 6   | AH train sweep positive but thin at thr\* (n=223, n_labeled_max=104)                                                     | Documented (§2 denominator warning): never quote the sweep without denominators                                                                                        |

## 7. Reproduction

```bash
# Tracks A+B (train-only; the one-shot refuses — domain consumed)
uv run python scripts/ml/anchor_ah_backtest.py
# Track C timing report (counts-only until >= 30 qualified events)
uv run python scripts/reports/timing_windows.py
# Track D (byte-identical re-run verified against data/ml/staking_evaluation.csv)
uv run python scripts/ml/evaluate_staking.py
# Regression tests for this round's gates
uv run pytest -q tests/test_anchor_ah_backtest.py tests/test_live_evidence.py \
  tests/test_timing_windows.py tests/test_evaluate_staking.py
```

## 8. Decision-support only

This platform never places bets. Edges, CLV, ROI, and recommended stakes
are informational evidence for a human who reviews every pick — never a
guarantee of profit.
