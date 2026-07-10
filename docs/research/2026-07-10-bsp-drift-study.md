# BSP-Stream Pre-Off Drift Study — the Definitive Steam-Family Retest

**Date:** 2026-07-10 · **Script:** `scripts/research/bsp_drift_study.py` ·
**Protocol:** idea #2 of `docs/research/2026-07-10-github-strategy-sweep.md`

## VERDICT: STEAM-STAYS-OFF (permanent, per the pre-set bar)

The frozen success bar — **AUC >= 0.55 across >= 6 consecutive monthly folds
AND a positive filtered-CLV delta with CI excluding 0** — is **NOT met**:

- **AUC leg: FAIL, decisively.** Soccer MATCH_ODDS pooled sign-AUC = **0.466**
  (day-clustered 95% CI [0.462, 0.470]) over 49,542 markets / 122,534 runner
  rows across 23 monthly folds — **zero folds** reach 0.55 (best single fold
  0.506); best consecutive streak **0 of the required 6**. OVER_UNDER_25:
  pooled AUC **0.495** (CI [0.487, 0.503]), zero passing folds. The direction
  is not merely "no momentum" — it is mild **mean-reversion**: prices that
  shortened over [T-24h, T-60m] tend to drift back OUT between T-60m and the
  close, and vice versa (pooled Spearman −0.075 on 1X2).
- **CLV-delta leg: passes numerically but is mechanically confounded** (see
  §5) — and the bar is an AND, so it cannot rescue the verdict.

This closes the steam family at archive scale (n ≈ 68.5k usable markets),
confirming the 2026-06-28 walk-forward KEEP-OFF verdict that was previously
resting on n=1 live evidence. `VALUE_STEAM_GATE_ENABLED` stays OFF; the
`docs/research/2026-07-10-github-strategy-sweep.md` idea #2 is **closed as
filed**.

## 1. Framing and honesty (read before citing)

- **Exploratory closure study, NOT a pre-registered confirmatory test.** Per
  ADR-0019 the 2025 BSP tars are registered SPENT (consumed by the 2026-07-02
  single-shot) and the 2026-H1 slice was consumed for the AH one-shot
  question. This study CONSULTS that data anyway because steam's fate is a
  **KEEP-OFF-or-retest decision on an already-OFF gate** — a closure readout,
  not new live-gate tuning. **No threshold, cutoff, or filter parameter from
  this study may be lifted into the live pipeline.** Had the verdict been
  RETEST-JUSTIFIED, it would have required its own pre-registered forward
  test on unspent data; it was not.
- The success bar was frozen before any data was read (sweep doc §2 +
  the study brief) and was not moved.
- The readout is **fit-free** (rank correlation, decile table, Mann-Whitney
  sign-AUC): nothing is trained, so monthly folds are pure stability checks,
  not train/test splits — there is nothing to leak *between* folds. Leakage
  *within* a market is excluded by construction (§3).

## 2. Data and coverage (silent truncation forbidden — all counts)

Source: the operator-placed Betfair Basic historical STREAM archive,
`data/betfair/bsp/{data.tar, data (1).tar, data (2).tar}` (the
`combined_train2025_holdout2026.tar` and the `incoming/data2026.tar` symlink
are re-packs of these three and were skipped; market_id dedupe guards
overlap).

| stage | count |
|---|---|
| tar members scanned (3 tars, streaming, never extracted) | 3,477,901 |
| soccer MATCH_ODDS / OVER_UNDER_25 markets kept by the extractor | 252,940 |
| duplicate market_ids across tars (dropped, first wins) | 3,751 |
| distinct markets in | 249,189 |
| excluded: kickoff outside 2024-01..2026-12 sanity band | 415 |
| excluded: kickoff moved > 30 min after first definition | 8,825 |
| excluded: in-play flip BEFORE the T-60m cutoff | 118 |
| runner rows dropped: no close price | 31,476 |
| runner rows dropped: no price at T-60m | 58,849 |
| runner rows dropped: no message at all before T-24h | 333,012 |
| runner rows dropped: T-24h snapshot staler than 12h | 20,161 |
| runner rows dropped: T-60m snapshot staler than 3h | 2,089 |
| runner rows dropped: non-finite / sub-1.01 price | 12 |
| **runner rows kept** | **153,760** |
| **markets kept (>=1 usable runner)** | **68,505** (49,542 1X2 + 18,963 OU) |

**Pre-off depth answer (the design pre-check):** T-60m prices exist for the
overwhelming majority of runners; the binding constraint is **T-24h depth —
roughly half of runner rows have no fresh price 24h out** (Basic-tier files
often open recording < 24h before kickoff). Coverage of the drift feature is
therefore ~27% of all extracted markets — reported, not hidden. Only 5,271
kept markets (2.1%) are BSP-reconciled; the close used is the repo's canonical
`close_price` convention (reconciled BSP else last pre-in-play best-back —
the same price live CLV is scored against). BSP-only sensitivity in §4.

Months covered: 2024-08 .. 2026-06 (23 eligible monthly folds per market
type; folds with < 200 markets are shown but ineligible for the streak).

## 3. Method (leakage rules)

- **Feature:** d24 = ln(price@T-60m / price@T-24h) per runner (best-back
  ladder, `bdatb`/`batb` level-0 with `ltp` fallback — same ladder logic as
  `app/ingestion/betfair_bsp.py`). Short-window variant d3 = ln(p@T-60m /
  p@T-3h). Velocity (d24 per observed hour) computed but adds nothing.
- **Target:** y = ln(close / price@T-60m) — the remaining move.
- **Leakage:** cutoffs are per-market (T-24h/T-3h/T-60m before the FIRST
  `marketTime`); each snapshot is taken BEFORE applying the first message
  whose publish-time crosses the cutoff, so features never see a post-cutoff
  message. Markets in-play before T-60m or with a moved kickoff are excluded
  and counted. Snapshot staleness is bounded (12h / 3h) and violations
  counted.
- **Inference:** tie-aware Mann-Whitney AUC of d24 for sign(y); Spearman rank
  correlation; decile table with ddof=1 SEs; pooled AUC CI via
  market-day-clustered bootstrap (B=2000, seed 20260710).

## 4. Per-fold AUC table (frozen question readout)

AUC > 0.5 = momentum (drift continues); < 0.5 = reversal. Bar: >= 0.55 in
>= 6 consecutive eligible folds.

### Soccer MATCH_ODDS (1X2) — pooled AUC 0.466 [0.462, 0.470], streak 0/6

| month | n mkts | AUC d24 | Spearman | AUC d3 | pass |
|---|---|---|---|---|---|
| 2024-08 | 3,209 | 0.473 | −0.061 | 0.451 | no |
| 2024-09 | 2,852 | 0.471 | −0.066 | 0.448 | no |
| 2024-10 | 3,060 | 0.442 | −0.107 | 0.441 | no |
| 2024-11 | 2,287 | 0.473 | −0.051 | 0.446 | no |
| 2024-12 | 1,849 | 0.444 | −0.127 | 0.442 | no |
| 2025-01 | 1,948 | 0.443 | −0.121 | 0.440 | no |
| 2025-02 | 2,836 | 0.439 | −0.127 | 0.431 | no |
| 2025-03 | 494 | 0.446 | −0.108 | 0.426 | no |
| 2025-04 | 3,148 | 0.442 | −0.121 | 0.429 | no |
| 2025-05 | 2,927 | 0.449 | −0.111 | 0.455 | no |
| 2025-06 | 1,410 | 0.462 | −0.087 | 0.452 | no |
| 2025-07 | 1,575 | 0.450 | −0.107 | 0.423 | no |
| 2025-08 | 3,243 | 0.475 | −0.053 | 0.460 | no |
| 2025-09 | 2,110 | 0.473 | −0.051 | 0.463 | no |
| 2025-10 | 2,286 | 0.482 | −0.046 | 0.472 | no |
| 2025-11 | 2,112 | 0.495 | −0.012 | 0.470 | no |
| 2025-12 | 1,527 | 0.476 | −0.070 | 0.436 | no |
| 2026-01 | 1,731 | 0.467 | −0.073 | 0.447 | no |
| 2026-02 | 2,064 | 0.480 | −0.036 | 0.457 | no |
| 2026-03 | 2,191 | 0.484 | −0.056 | 0.450 | no |
| 2026-04 | 2,166 | 0.490 | −0.023 | 0.454 | no |
| 2026-05 | 1,940 | 0.506 | −0.016 | 0.464 | no |
| 2026-06 | 567 | 0.495 | −0.044 | 0.462 | no |

### Soccer OVER_UNDER_25 — pooled AUC 0.495 [0.487, 0.503], streak 0/6

23 folds, AUC range 0.441 (2025-01) .. 0.533 (2024-08); no fold reaches 0.55,
no consecutive streak. Full table in the JSON artifact (§7).

### Decile table (1X2, pooled): monotone REVERSAL, not momentum

| d24 decile | n | mean y (ddof=1 SE) | frac y>0 |
|---|---|---|---|
| D1 (strongest shorteners, d24 ≤ −0.112) | 12,251 | **+0.0428** (0.0018) | 0.550 |
| D2..D5 (mild shorteners) | 42,107 | +0.002..+0.010 | 0.44–0.50 |
| D6..D9 (mild drifters-out) | 55,912 | +0.001..+0.005 | 0.44–0.45 |
| D10 (strongest drifters, d24 ≥ +0.143) | 12,264 | **−0.0115** (0.0017) | 0.428 |

Runners that steamed IN hardest bounce back OUT the most (+4.3% log-price),
and the biggest drifters come back in (−1.2%). The short-window variant (d3)
is even more reversal-shaped (pooled AUC ~0.45). The BSP-only sensitivity
subset (15,677 1X2 runner rows scored vs reconciled BSP) flips mildly
positive at 0.523 — still nowhere near the 0.55 bar, on 10x less data, and
consistent with BSP's known last-seconds idiosyncrasy rather than a
tradeable pre-off signal.

## 5. Frozen-rule CLV replay (second leg of the bar)

Frozen rule reused verbatim from `scripts/research/ah_anchor_backtest.py`
(power-devig Pinnacle 1X2 fair, edge >= 3% at the football-data Max price,
odds [1.6, 4.0], one pick per match; CLV = ln(price × devigged Betfair
close), seasons 2425+2526 across 22 leagues):

| readout | value |
|---|---|
| fixtures / BSP-joined / rule picks / picks with CLV | 10,601 / 8,317 / 605 / 469 |
| picks with a usable drift feature (base universe) | 437 |
| kept by the drift filter (d24 < 0, market moved toward pick) | 221 |
| mean CLV base / kept | +0.0255 / +0.0551 |
| **delta (kept − base), day-clustered 95% CI** | **+0.0296 [+0.0212, +0.0390]** |
| flat-stake ROI base / kept (secondary, small-n) | −0.9% / +10.5% |

**Why this leg cannot rescue the verdict (beyond the AND):** the delta is
mechanically confounded. The fill is a pre-match price captured before most
of the drift window, so a pick whose price shortened by T-60m has already
banked that move into its final CLV *whatever happens after T-60m*.
Filtering on d24 < 0 therefore selects on a component of the outcome
variable. The clean test of the frozen question — does drift predict the
REMAINING move — is the AUC leg, and it says the remaining move mildly
*reverses* (0.466). The AND structure of the pre-set bar exists precisely to
prevent this confound from producing a false pass.

## 6. Relation to prior steam evidence

- 2026-06-28: steam gate walk-forward tested on football-data, kept OFF
  (live shadow n=1; memory `gates-validated-keep-off-2026-06-28`).
- This study: n ≈ 68.5k exchange markets, 23 monthly folds, message-level
  pre-off paths — same conclusion, stronger form: the pre-off exchange move
  does not continue; it slightly reverses. The steam family needs no further
  retests unless a fundamentally different signal definition (e.g.
  cross-book sharp-vs-soft divergence rather than same-market drift) is
  proposed with its own pre-registration.

## 7. Reproduction

```
.venv/bin/python scripts/research/bsp_drift_study.py extract \
    --tar "data/betfair/bsp/data.tar"     --out CKPT_A.jsonl.gz
.venv/bin/python scripts/research/bsp_drift_study.py extract \
    --tar "data/betfair/bsp/data (1).tar" --out CKPT_B.jsonl.gz
.venv/bin/python scripts/research/bsp_drift_study.py extract \
    --tar "data/betfair/bsp/data (2).tar" --out CKPT_C.jsonl.gz
.venv/bin/python scripts/research/bsp_drift_study.py analyze \
    --ckpt CKPT_A.jsonl.gz CKPT_B.jsonl.gz CKPT_C.jsonl.gz --out-json analysis.json
.venv/bin/python scripts/research/bsp_drift_study.py replay \
    --ckpt CKPT_A.jsonl.gz CKPT_B.jsonl.gz CKPT_C.jsonl.gz --out-json replay.json
```

Extraction ≈ 1h/tar (streaming, ~3.5M members total); analyze + replay are
minutes. Full JSON artifacts from this run (fold tables, deciles, replay)
were checkpointed in the session scratchpad (`bsp_drift/full_analysis.json`,
`bsp_drift/full_replay.json`); the tables above are copied from them
verbatim.

Decision-support only — nothing here places bets.
