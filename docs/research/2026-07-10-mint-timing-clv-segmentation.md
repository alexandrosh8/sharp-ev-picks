# Mint-timing CLV segmentation (Simon 2024, litx sweep idea #2)

**Date:** 2026-07-10 · **Phase:** 4 (measurement-first backtest) · **Status:** measured, no policy change
**Hypothesis:** trusted CLV varies systematically with mint-to-kickoff lead time
(`picks.created_at → events.starts_at`); market forecasts do not improve monotonically
toward the close (Simon, *Management Science* 70(12) 2024), so some mint-timing buckets
may carry systematically negative CLV.

**Verdict: MEASURABLE-EFFECT (fragile, clustered).** The `>24h` bucket is significantly
negative at 95% (n=98, mean clv_log −0.0777, CI [−0.1538, −0.0016]) — but the effect is
carried entirely by h2h picks (h2h-only −0.1634, CI [−0.2889, −0.0378]; non-h2h in the
same bucket is significantly POSITIVE +0.0414, CI [+0.0084, +0.0744]), and it loses
significance when the single dominant kickoff day (2026-07-03, 38/98 rows) is removed
(−0.0317, CI [−0.0897, +0.0263]). No suppression policy is justified by this sample;
forward pre-registered evidence is required (see §6).

---

## 1. Population and honesty caveats

| Stage | n |
|---|---|
| Settled picks (`picks ⋈ result_tracking`, all tiers) | **1,194** |
| … with a clv_log | 1,159 |
| **Trusted sharp subset (this measurement)** | **232** (19.4%) |

Sequential exclusion funnel (each row counted at its first failing gate):
`tautological 401 · no_snapshot_close 343 · non_sharp_anchor 167 · no_clv 35 · fabricated 13 · not_independent 3`.

- **Observational sample**: these are post-gate picks (odds-age, sharp-anchor, edge-floor,
  odds-ceiling gates all applied at mint). Any timing effect is CONDITIONAL on the
  current gate stack, not a property of the market.
- **Short, regime-crossed window**: trusted picks minted 2026-06-26 → 2026-07-09
  (13 days), spanning the 2026-07-07 odds-ceiling 5.0→4.0 + sharp-anchor changes.
- **Tier split is uninformative**: trusted premium n=8 (volume n=224) — all premium
  cells are below any honesty floor; the headline is effectively a volume-tier result.
- **Lead time is bounded at ~50h** (scrape horizon): ">24h" means 24–50h, not multi-day.
- Bucket `<2h` has n=23 (<30), so the headline table also reports the merged `<6h`.

## 2. Methodology — exact trust gates (mirrors `/performance` code)

Row source (read-only prod, betting_ai DB):

```sql
SELECT p.id, p.event_id, p.tier, p.market, s.key, l.name,
       p.created_at, p.revalidated_at, e.starts_at,
       p.clv_log, p.decimal_odds, p.closing_fair_probability, p.model_probability,
       p.has_snapshot_close, p.closing_anchor_type, p.close_independent_of_fill,
       p.mint_devig_fell_back, p.close_devig_fell_back, rt.settled_at, rt.outcome
FROM picks p
JOIN result_tracking rt ON rt.pick_id = p.id
JOIN events e  ON e.id = p.event_id
JOIN sports s  ON s.id = e.sport_id
JOIN leagues l ON l.id = e.league_id;
```

Trust gates applied per row, byte-for-byte the logic of
`app/storage/repositories.py::_aggregate_settled` (and its guards):

1. `clv_log IS NOT NULL`.
2. NOT fabricated — `_clv_row_is_fabricated`: when `decimal_odds` AND
   `closing_fair_probability` are both present, exclude iff close-implied edge
   `closing_fair − 1/decimal_odds > 0.20` (CLV_IMPLAUSIBLE_CLOSE_EDGE); the
   `|clv_log| > 0.5` cutoff is the FALLBACK used only when an input is absent
   (post-fix conditional form, deep-audit 2026-07-08 item resolved).
3. NOT tautological — `_clv_row_is_tautological`:
   `|closing_fair_probability − model_probability| <= 1e-3` (CLV_TAUTOLOGY_EPS)
   with both present.
4. `has_snapshot_close IS TRUE` (NULL = poll-time fallback close → excluded).
5. `closing_anchor_type IN ('pinnacle','sharp')` (`_SHARP_CLOSE_ANCHORS`).
6. `close_independent_of_fill IS TRUE` (NULL is NOT trusted).
7. Symmetric devig — exclude iff `mint_devig_fell_back` and `close_devig_fell_back`
   are both non-NULL and differ (`_devig_fallback_asymmetric`).

Stats: per-bucket unweighted mean clv_log, ddof=1 SE, two-sided t-based 95% CI
(matches `mean_significance`). "SIG" = CI excludes 0. Clustering reported as distinct
events and the top kickoff-day share per bucket. Script:
`analyze.py` in the session scratchpad; buckets on `starts_at − created_at`.

## 3. Results — trusted CLV by mint lead time

### 3.1 All trusted (n=232; lead_h median 19.5h, p25 6.6, p75 32.5)

| Bucket | n | mean clv_log | SE | 95% CI | events | top day (share) | sig |
|---|---|---|---|---|---|---|---|
| <2h | 23 | −0.0191 | 0.0391 | [−0.1002, +0.0620] | 21 | 07-06 (7/23) | no |
| 2–6h | 33 | +0.0198 | 0.0307 | [−0.0427, +0.0824] | 29 | 07-02 (8/33) | no |
| **<6h merged** | 56 | +0.0039 | 0.0241 | [−0.0445, +0.0522] | 46 | 07-02 (11/56) | no |
| 6–24h | 78 | +0.0052 | 0.0253 | [−0.0453, +0.0557] | 55 | 07-02 (21/78) | no |
| **>24h** | **98** | **−0.0777** | 0.0384 | **[−0.1538, −0.0016]** | 73 | **07-03 (38/98)** ⚠ clustered | **SIG−** |

⚠ `>24h` is dominated by one kickoff day (2026-07-03 = 39% of rows, mostly Club
Friendly / ATP Wimbledon slates). Excluding that day: n=60, −0.0317,
CI [−0.0897, +0.0263] — NOT significant. The bucket-level significance is fragile.

### 3.2 By sport (soccer vs rest)

| Sport | Bucket | n | mean | 95% CI | events | sig |
|---|---|---|---|---|---|---|
| soccer | <2h | 12 | +0.0308 | [−0.1016, +0.1632] | 11 | no |
| soccer | 2–6h | 17 | −0.0089 | [−0.0724, +0.0545] | 16 | no |
| soccer | 6–24h | 38 | −0.0599 | [−0.1491, +0.0292] | 34 | no |
| soccer | >24h | 62 | −0.0953 | [−0.1995, +0.0090] | 42 | no (borderline) |
| rest | <2h | 11 | −0.0735 | [−0.1763, +0.0293] | 10 | no |
| rest | 2–6h | 16 | +0.0504 | [−0.0665, +0.1674] | 13 | no |
| rest | 6–24h | 40 | +0.0671 | [+0.0208, +0.1134] | **21** ⚠ WC 16/40 | **SIG+** ⚠ clustered |
| rest | >24h | 36 | −0.0474 | [−0.1567, +0.0619] | 31 | no |

⚠ rest/6–24h SIG+ is 40 rows over only 21 events, 16/40 from one competition
(basketball World Cup) — treat as clustered, not independent evidence. Non-soccer
sports are shadow/unvalidated (shadow-first mandate); their positives here are
consistent with the existing basketball-promotion watch, nothing more.

### 3.3 By tier

Premium trusted n=8 total — INSUFFICIENT-SAMPLE in every bucket (the nominal
"SIG−" at 2–6h is n=2 and meaningless). Volume (n=224) reproduces the headline:
>24h n=98 −0.0777 CI [−0.1538, −0.0016] SIG−; all other buckets straddle 0.

### 3.4 By market family (families with n≥30)

| Family | Bucket | n | mean | 95% CI | sig |
|---|---|---|---|---|---|
| h2h (108) | <2h | 12 | −0.0102 | [−0.1232, +0.1029] | no |
| h2h | 2–6h | 14 | +0.0330 | [−0.1135, +0.1796] | no |
| h2h | 6–24h | 25 | −0.0292 | [−0.1655, +0.1072] | no |
| **h2h** | **>24h** | **57** | **−0.1634** | **[−0.2889, −0.0378]** | **SIG−** ⚠ 07-03 = 25/57 |
| spreads (70) | 6–24h | 30 | +0.0520 | [−0.0047, +0.1087] | no (borderline) |
| spreads | >24h | 23 | +0.0835 | [+0.0448, +0.1223] | SIG+ ⚠ n<30, NBL-tilted |
| totals (46) | all buckets | ≤19 | — | CIs straddle 0 | no |

Composition check inside `>24h`: h2h-only −0.1634 SIG−, non-h2h +0.0414
CI [+0.0084, +0.0744] SIG+. The bucket-level negative is an h2h phenomenon
(early-minted moneylines vs the sharp close), not a uniform timing effect —
consistent with the 2026-07-07 finding that negative CLV is a SELECTION problem
concentrated in the H2H longshot/consensus segment.

btts: 0 trusted rows (structural — no sharp close source), as expected.

## 4. Revalidation freshness (kickoff − revalidated_at)

222/232 trusted rows have `revalidated_at` (SUCCESS-only stamp). Distribution is
extremely skewed — 188/222 (85%) were last revalidated <30m before kickoff, so the
buckets are not comparable-size and this cut is close to unmeasurable:

| Bucket | n | mean | 95% CI | sig |
|---|---|---|---|---|
| <30m | 188 | −0.0421 | [−0.0880, +0.0039] | no (borderline −) |
| 0.5–2h | 16 | +0.0606 | [+0.0067, +0.1145] | SIG+ ⚠ 7 events, Match Coupon 9/16 — clustered |
| 2–6h | 9 | +0.0617 | [−0.0187, +0.1421] | no |
| >6h | 7 | +0.0295 | [−0.1151, +0.1741] | no |
| post-KO | 2 | — | — | insufficient |

Verdict for §3 of the task: NO usable revalidation-freshness effect — the only
significant cell (0.5–2h) is 16 rows over 7 events dominated by one league-coupon
slate. Note also `revalidated_at` is success-only, so "stale" partially proxies
"book dropped the line", a selection artifact.

## 5. Verdict

- **MEASURABLE-EFFECT, direction negative for early mints, h2h-specific**:
  `>24h` mint lead is significantly negative at 95% overall
  (n=98, −0.0777, CI [−0.1538, −0.0016]) and more strongly for h2h
  (n=57, −0.1634, CI [−0.2889, −0.0378]).
- **Fragility flags**: the overall bucket loses significance without the 2026-07-03
  slate (38/98 rows); the window is 13 days; picks-per-event clustering (98 rows /
  73 events) means the naive SE is somewhat anti-conservative; ~all evidence is
  volume-tier.
- **Countervailing cells** (both clustered): rest/6–24h +0.0671 SIG+ (World-Cup-heavy),
  spreads/>24h +0.0835 SIG+ (n=23). The effect is NOT "early is uniformly bad";
  it is "early h2h is bad in this sample".
- Consistent with Simon (2024): forecast quality is not monotone in time-to-event,
  and with the 2026-07-07 selection-problem diagnosis (h2h premium/longshot band).
- **No threshold selection and no policy change is made from this sample.**

## 6. Forward evidence that would justify a shadow-only timing policy

Pre-registration style (single-shot, no re-tuning on this spent sample):

1. **Register now, before the data exists**: hypothesis "h2h picks minted ≥24h
   before kickoff carry negative trusted CLV" — bucket boundary FIXED at 24h
   (the task's pre-specified grid, not tuned), market scope FIXED at h2h.
2. **Shadow telemetry only**: tag (do not suppress) every new pick with its mint
   lead bucket; no selection, stake, or alert change.
3. **Pass criteria on FRESH data** (picks minted after 2026-07-10):
   trusted-sharp n ≥ 100 in the h2h/≥24h cell, spanning ≥ 21 distinct kickoff
   days and ≥ 70 distinct events with no single day > 20% of rows;
   mean clv_log t-CI excludes 0 on the negative side; effect persists in a
   leave-one-day-out sweep (every day-excluded CI still < 0 at 90%).
4. **Control cell**: the same window's h2h/<24h trusted CLV must be
   distinguishably better (Welch CI on the difference excludes 0), otherwise the
   finding is "h2h is bad", not "early h2h is bad" — a different (selection) fix.
5. Only then propose a shadow-only suppression trial, per the shadow-first
   promotion policy (operator mandate 2026-07-04).

## Reproduction

- Extraction SQL: §2 (read-only COPY against compose Postgres).
- Analysis: t-based CIs (scipy), ddof=1; script archived in session scratchpad
  (`analyze.py`); trust gates cross-checked against
  `app/storage/repositories.py` lines ~1126–1502 as of commit 62abb95.
