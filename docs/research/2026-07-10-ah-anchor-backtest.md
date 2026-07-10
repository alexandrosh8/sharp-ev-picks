# AH-Anchored Soccer Fair vs Power-Devig 1X2 Fair — Walk-Forward CLV Study

**Date:** 2026-07-10  ·  **Author:** quant-backtest-engineer (agent)
**Script:** `scripts/research/ah_anchor_backtest.py` (seed 20260710, B=2000)
**Design source:** docs/research/2026-07-10-litx-strategy-sweep.md, idea #1
(Hegarty & Whelan — 1X2 odds carry favourite-longshot bias, AH odds do not).

## Status — exploratory, NOT confirmatory

The 1920-2526 football-data seasons have been consulted by earlier studies of
other hypotheses (value_backtest sweeps; the AH-market *betting* one-shot
consumed 2425+2526 for that separate question), and the BSP 2025 holdout is
spent for the ADR-0019 hypotheses. This study asks a NEW question (anchor
replacement for 1X2 pick selection) but runs on previously-seen data, so the
strongest verdict available here is PROMISING-PREREGISTER. No thresholds were
tuned: the selection rule is the deployed premium doctrine, frozen up front
(edge >= 0.03, odds in [1.6, 4.0], one pick per match,
power devig everywhere). The only fitted parameter (Dixon-Coles rho) is fitted
per season on strictly earlier seasons (expanding window back to 1213).

## Method

- **v1 (baseline) anchor:** power-devig(PSH, PSD, PSA) — deployed behaviour.
- **v2 (candidate) anchor:** power-devig 2-way (PAHH, PAHA) = fair AH cover
  probability at line AHh; power-devig(P>2.5, P<2.5) = fair P(total >= 3).
  Solve independent-Poisson + Dixon-Coles-tau for (lambda_h, lambda_a)
  matching both fair probabilities (quarter lines split into two half-stakes;
  the 2-way devig recovers exactly the push-adjusted W/(W+L) the model
  matches). 1X2 fair read off the score matrix (goals 0..15, renormalised).
- **Universe:** fixtures where BOTH anchors exist (coverage reported below);
  22 football-data leagues, seasons 1920-2526. All anchor inputs pre-match;
  closing columns score CLV only.
- **Selection (both arms, identical):** H/D/A at the pre-match Max price,
  edge = fair x price - 1 >= 0.03, price in [1.6, 4.0], argmax-edge one pick
  per match.
- **CLV:** clv = ln(price x p_close_fair) (app/backtesting/clv.py). Primary
  close = Betfair exchange close where the strict canonical join matches,
  else power-devig Pinnacle close; each leg also reported separately. The
  Pinnacle-close leg is not independent of the hypothesis: if the 1X2 close
  retains the favourite-longshot bias, it penalises the arm correcting it.
- **Inference:** per-season and pooled delta (v2 - v1) with a match-day
  (date-)clustered bootstrap 95% CI; date-clustered SEs (cl2SE) per arm;
  flat-stake ROI at the Max price as the secondary truth check.

## Data honesty — n up front

- Fixtures loaded (seasons 1920-2526, 22 leagues): 53329
- v1-anchorable (pre-match Pinnacle 1X2 + Max prices): 48407
- AH+OU pre-match present: 48292; solver converged: 48292
- **Anchor coverage: 99.8%** (success bar >= 80%)
- Betfair close joined: 8283/48292 common fixtures
- Degenerate closes dropped (devig underflow/NaN, per source): pinnacle=0, bsp=0 (a degenerate close falls back to the other close source for the primary metric; it is never averaged as -inf/NaN)
- 2026-01-15 Pinnacle blackout: 3648 rows on/after the blackout have
  no pre-match Pinnacle columns and drop out of BOTH arms (the 2526 season
  is effectively its first half only — reported, not imputed).
- Dixon-Coles rho (walk-forward, fitted on strictly earlier seasons):
  1920=-0.0459, 2021=-0.0456, 2122=-0.0445, 2223=-0.0455, 2324=-0.0429, 2425=-0.0442, 2526=-0.0433

## Primary CLV (BSP close where joined, else Pinnacle close)

| season | n v1 | n v2 | mean CLV v1 | mean CLV v2 | delta v2-v1 | 95% CI (date-boot) |
|---|---|---|---|---|---|---|
| 1920 | 844 | 1274 | +0.0352 (cl2SE 0.0046) | +0.0120 (cl2SE 0.0056) | -0.0232 | [-0.0293, -0.0173] **excludes 0** |
| 2021 | 890 | 1342 | +0.0222 (cl2SE 0.0055) | +0.0118 (cl2SE 0.0059) | -0.0104 | [-0.0169, -0.0035] **excludes 0** |
| 2122 | 1162 | 1300 | +0.0306 (cl2SE 0.0047) | +0.0054 (cl2SE 0.0058) | -0.0252 | [-0.0315, -0.0193] **excludes 0** |
| 2223 | 651 | 744 | +0.0313 (cl2SE 0.0066) | +0.0069 (cl2SE 0.0082) | -0.0244 | [-0.0317, -0.0170] **excludes 0** |
| 2324 | 468 | 557 | +0.0291 (cl2SE 0.0058) | -0.0080 (cl2SE 0.0082) | -0.0372 | [-0.0457, -0.0287] **excludes 0** |
| 2425 | 471 | 521 | +0.0279 (cl2SE 0.0078) | -0.0138 (cl2SE 0.0108) | -0.0417 | [-0.0530, -0.0300] **excludes 0** |
| 2526 | 133 | 165 | +0.0203 (cl2SE 0.0218) | +0.0023 (cl2SE 0.0236) | -0.0180 | [-0.0327, -0.0006] **excludes 0** |
| POOLED | 4619 | 5903 | +0.0292 (cl2SE 0.0024) | +0.0054 (cl2SE 0.0029) | -0.0238 | [-0.0269, -0.0208] **excludes 0** |

## Betfair-close leg only (independent close)

| season | n v1 | n v2 | mean CLV v1 | mean CLV v2 | delta v2-v1 | 95% CI (date-boot) |
|---|---|---|---|---|---|---|
| 1920 | 0 | 0 | n=0 | n=0 | n/a | n/a |
| 2021 | 0 | 0 | n=0 | n=0 | n/a | n/a |
| 2122 | 0 | 0 | n=0 | n=0 | n/a | n/a |
| 2223 | 0 | 0 | n=0 | n=0 | n/a | n/a |
| 2324 | 0 | 0 | n=0 | n=0 | n/a | n/a |
| 2425 | 362 | 409 | +0.0281 (cl2SE 0.0094) | -0.0164 (cl2SE 0.0124) | -0.0445 | [-0.0587, -0.0306] **excludes 0** |
| 2526 | 107 | 135 | +0.0200 (cl2SE 0.0265) | +0.0035 (cl2SE 0.0287) | -0.0165 | [-0.0337, +0.0020] |
| POOLED | 469 | 544 | +0.0263 (cl2SE 0.0095) | -0.0115 (cl2SE 0.0116) | -0.0377 | [-0.0488, -0.0266] **excludes 0** |

## Pinnacle-close leg only (NOT hypothesis-independent)

| season | n v1 | n v2 | mean CLV v1 | mean CLV v2 | delta v2-v1 | 95% CI (date-boot) |
|---|---|---|---|---|---|---|
| 1920 | 844 | 1274 | +0.0352 (cl2SE 0.0046) | +0.0120 (cl2SE 0.0056) | -0.0232 | [-0.0291, -0.0172] **excludes 0** |
| 2021 | 890 | 1342 | +0.0222 (cl2SE 0.0055) | +0.0118 (cl2SE 0.0059) | -0.0104 | [-0.0169, -0.0035] **excludes 0** |
| 2122 | 1162 | 1300 | +0.0306 (cl2SE 0.0047) | +0.0054 (cl2SE 0.0058) | -0.0252 | [-0.0315, -0.0189] **excludes 0** |
| 2223 | 651 | 744 | +0.0313 (cl2SE 0.0066) | +0.0069 (cl2SE 0.0082) | -0.0244 | [-0.0319, -0.0173] **excludes 0** |
| 2324 | 468 | 557 | +0.0291 (cl2SE 0.0058) | -0.0080 (cl2SE 0.0082) | -0.0372 | [-0.0459, -0.0289] **excludes 0** |
| 2425 | 471 | 521 | +0.0277 (cl2SE 0.0079) | -0.0128 (cl2SE 0.0106) | -0.0405 | [-0.0523, -0.0290] **excludes 0** |
| 2526 | 133 | 165 | +0.0299 (cl2SE 0.0203) | +0.0177 (cl2SE 0.0196) | -0.0122 | [-0.0247, -0.0002] **excludes 0** |
| POOLED | 4619 | 5903 | +0.0295 (cl2SE 0.0023) | +0.0059 (cl2SE 0.0028) | -0.0235 | [-0.0265, -0.0205] **excludes 0** |

## Flat-stake ROI (profit units per pick, secondary)

| season | n v1 | n v2 | mean CLV v1 | mean CLV v2 | delta v2-v1 | 95% CI (date-boot) |
|---|---|---|---|---|---|---|
| 1920 | 844 | 1274 | -0.0277 (cl2SE 0.1077) | +0.0163 (cl2SE 0.0813) | +0.0440 | [-0.0651, +0.1530] |
| 2021 | 890 | 1342 | +0.0443 (cl2SE 0.1016) | -0.0894 (cl2SE 0.0795) | -0.1337 | [-0.2502, -0.0088] **excludes 0** |
| 2122 | 1162 | 1300 | +0.0812 (cl2SE 0.0845) | +0.0023 (cl2SE 0.0829) | -0.0788 | [-0.1711, +0.0155] |
| 2223 | 651 | 744 | +0.0959 (cl2SE 0.1088) | +0.0593 (cl2SE 0.1158) | -0.0365 | [-0.1734, +0.0998] |
| 2324 | 468 | 557 | +0.0750 (cl2SE 0.1356) | -0.0092 (cl2SE 0.1250) | -0.0843 | [-0.2411, +0.0767] |
| 2425 | 471 | 521 | -0.0185 (cl2SE 0.1339) | +0.0116 (cl2SE 0.1368) | +0.0301 | [-0.1307, +0.2080] |
| 2526 | 133 | 165 | -0.0238 (cl2SE 0.2946) | +0.1332 (cl2SE 0.2216) | +0.1570 | [-0.1842, +0.4569] |
| POOLED | 4619 | 5903 | +0.0424 (cl2SE 0.0437) | -0.0049 (cl2SE 0.0389) | -0.0474 | [-0.0990, +0.0025] |

## Pooled by odds band (primary close)

| band | n v1 | n v2 | mean CLV v1 | mean CLV v2 | delta | 95% CI |
|---|---|---|---|---|---|---|
| [1.6,2.0) | 527 | 191 | +0.0377 (cl2SE 0.0060) | +0.0417 (cl2SE 0.0107) | +0.0040 | [-0.0045, +0.0129] |
| [2.0,2.5) | 623 | 586 | +0.0346 (cl2SE 0.0076) | +0.0323 (cl2SE 0.0077) | -0.0023 | [-0.0075, +0.0027] |
| [2.5,3.0) | 414 | 1058 | +0.0423 (cl2SE 0.0083) | +0.0218 (cl2SE 0.0055) | -0.0205 | [-0.0279, -0.0129] |
| [3.0,4.0) | 2974 | 3809 | +0.0253 (cl2SE 0.0027) | -0.0049 (cl2SE 0.0036) | -0.0303 | [-0.0339, -0.0265] |

## Selection agreement

- fixtures where both arms pick: 2565; same selection: 1634 (63.7%)
- primary-close source split: bsp=1013, pinnacle=9509, unscored=0

## Interpretation (analyst, written against the 2026-07-10 run — a script re-run regenerates tables only)

The result is unambiguous and the opposite of the hypothesis:

- The delta is significantly negative in EVERY season and pooled, and — the
  decisive check — on the **independent Betfair-close leg** as well
  (-0.0377 [-0.0488, -0.0266]), so this is not an artifact of scoring against
  a 1X2 close that retains the favourite-longshot bias.
- The damage concentrates exactly where the sweep predicted the AH anchor
  would help most: the 3.0-4.0 odds band (delta -0.0303 [-0.0339, -0.0265]),
  and v2 issues 28% more picks (5,903 vs 4,619) — the AH-derived fair is MORE
  optimistic in the tails, not less.
- Mechanism (diagnostic on all 48,292 common fixtures): realized outcome
  rates H/D/A = 0.4286/0.2627/0.3087 vs v1 fair 0.4334/0.2578/0.3088 and
  v2 fair 0.4325/0.2532/0.3143. The devigged Pinnacle 1X2 is already
  essentially calibrated at this granularity (consistent with the 2026-06-24
  calibration-haircut finding), while the Poisson/Dixon-Coles reconstruction
  UNDER-prices draws (-1.0pp vs realized) and OVER-prices away sides
  (+0.6pp) even with the walk-forward-fitted rho (~-0.044, interior to its
  bounds). Those distortions mint fake edges on draw/away longshots.
- Flat-stake ROI agrees directionally (pooled v2-v1 = -0.047
  [-0.099, +0.003]).

Conclusion: Hegarty & Whelan's two-market result (AH odds unbiased *for AH
bets*) does not survive the model-mediated reconstruction into 1X2
probabilities — the Skellam/DC layer introduces a draw-deficit/away-surplus
bias larger than any favourite-longshot bias it removes. Do NOT pre-register
this; do not spend the fresh 2026-27 season on it. The shipped
blunt-instrument mitigations (odds ceiling 4.0, power devig) remain the
better treatment of 1X2 tail bias.

## Verdict

**NO-EFFECT** — pooled primary-close CLV delta (v2 - v1) = -0.0238
[-0.0269, -0.0208] (date-clustered bootstrap, B=2000); anchor
coverage 99.8%. pooled delta 95% CI excludes 0 on the NEGATIVE side — the AH anchor is significantly WORSE than the deployed 1X2 anchor on this close.

If PROMISING-PREREGISTER: next step is an operator-signed pre-registration
(fresh 2026-27 season + live shadow, per the shadow-first mandate) BEFORE any
gate change; nothing in this study alters production behaviour.

_Runtime 216s. Decision-support only — no bets placed._
