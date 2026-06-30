# ROI + Pick-Quality Review (multi-agent, 2026-06-30)

Comprehensive review (20 agents: 10 subsystem audits → ranked → 8 adversarially
verified → synthesis) toward one goal: **increase real ROI and produce
top-quality picks.** Adversarial verification overturned the earlier soccerdata
"USE" and rejected two levers that fail held-out CLV.

## Headline

The codebase is disciplined (devig math, CLV honesty guards, walk-forward, the
fail-closed ML adopt gate) — preserve it. The biggest finding: **the captured
sharp data (Pinnacle Arcadia) is mostly disconnected from live pick anchoring.**
A live pick only gets a sharp anchor when the soft OddsPortal scrape happens to
list a sharp book (~1%); `VALUE_SHARP_ANCHOR_FROM_ARCHIVES` defaults off and the
Arcadia archive starved (no proxies). So the highest-ROI work is **plumbing +
measurement honesty, not modeling.** No new predictive model raises held-out
CLV; several would destroy it via overfit/leakage.

## Top levers (survived adversarial verification)

1. **Provision rotating proxies** (SCRAPER_PROXY_POOL + ARCADIA_PROXY_URLS) to
   un-starve the Pinnacle Arcadia sharp archive. Precondition for all sharp-anchor
   ROI. `config.py:779,1121-1133`. *(DONE on operator side 2026-06-30: rotation +
   static Betfair API.)*
2. **Shadow-validate → enable `VALUE_SHARP_ANCHOR_FROM_ARCHIVES`** so bulk-captured
   Pinnacle/Betfair prices anchor LIVE picks. Gate on held-out CLV on the loader's
   exact 4h-fresh pick-time anchor (NOT the BSP backtest); add a tighter live
   freshness gate (~30-60min) + pre-kickoff assertion. `config.py:882`,
   `clv_trueup.py:1070-1149`.
3. **Edge-rank the daily-exposure ledger by raw_kelly** before reserving, so the
   5% cap funds highest-growth picks first. No leakage, no cap increase.
   `pipeline.py` (`_reserve_for_outcome`, ledger.reserve).
4. **Measurement honesty**: bootstrap ROI CIs + ddof=1 CLV SE; re-anchor
   expectations off the inflated +0.107/n=61 figure to the honest ~+0.02
   independent-sharp-close number. `scripts/value_backtest.py`, `clv.py`.
5. **Fix the meta-model calibration monitor** (apples-to-oranges) to unblock the
   `VALUE_ML_FILTER` decision honestly; add an ECE ceiling. `live_evidence.py:234`.
6. **Grow trusted sharp-close n**: feed more soccer Betfair-BSP archives (re-verify
   CI under the corrected bootstrap). `betfair_bsp.py`.
7. **Accrue genuine NBA Pinnacle moneyline closes** toward the per-sport promotion
   gate (multi-month, operator-gated, default-off).

## soccerdata → REJECT

Overturns the earlier single-agent "USE". football-data.co.uk is already loaded
directly (with its full Pinnacle reservoir); Understat xG is already reachable via
penaltyblog. soccerdata adds only FBref scraping (rate-limited, ToS-sensitive) for
data we don't price against = zero incremental sharp price = zero incremental CLV.
Its xG/ELO features are the "beat-the-close" predictors the doctrine rejects. If
ever trialed: ClubElo only, strictly point-in-time, as input to a better pricing
model that clears a FRESH-season holdout — never a standalone predictor.

## OddsHarvester → HOLD

The 4-5-day-old commits are PR #70 (`02fb70de1`, `4e743b176`) on per-match
Playwright market navigation — which our live curl_cffi JSON feed bypasses AND our
8 monkeypatches already rewrite — scoped to localized mirrors we never hit → zero
live pick-volume, zero CLV, and trips our version guard. One commit adds a bad
integer-AH line that breaks direct devig. Wait for the next TAG, then full
patch-revalidation via `upgrade_deps.sh`.

## Sequenced plan

1. Proxies (done) → 2. Measurement honesty (parallel, no downside) → 3. Edge-rank
the ledger (pure growth) → 4. Archive→live anchors in SHADOW (promote only if OOS
CLV CI excludes 0) → 5. Grow sharp-close n (soccer BSP + NBA close accrual) →
6. Only after a FRESH unspent season: re-run the value-filter protocol, then
consider flipping `VALUE_ML_FILTER`.

## DO NOT DO (the traps that quietly wreck ROI)

- Don't re-consult the SPENT 2425/2526 holdout to re-tune anything.
- Don't plan/stake on +0.107/+22% (n=61, Pinnacle's own close) — use ~+0.02
  (BSP +0.0229±0.0167, volume +0.019).
- Don't build the NBA GBM / rest-B2B-pace "beat-the-close" features — they lose to
  the close in the repo's own backtests.
- Don't enable the equal-weight logit sharp pool (walk-forward dCLV −0.008, dilutes
  Pinnacle, demotes picks out of premium).
- Don't count consensus/SBR/BeatTheBookie as sharp closes; never leak consensus
  into `_SHARP_CLOSE_ANCHORS`.
- Don't broaden `_NOISE_TOKENS` / add fuzzy matching (wrong-game-unsafe).
- Don't read stake-weighted CLV as the go/no-go (biased by the edge under test) —
  use the unweighted ddof=1 t-CI.
- Don't apply a realizable-price haircut to odds/edge/EV/tier-gating (only to the
  stake arg), and don't add a calibration haircut to the devigged fair (identity
  wins OOS; a drift detector already monitors it).
- Don't build full simultaneous-bet portfolio Kelly (unestimable correlation
  matrix = overfit surface; the per-event 4% + daily 5% caps already bound ruin).
- Don't cherry-pick OddsHarvester PR #70 / the integer-AH commit.

## Implementation status (this session)

- **#4 ddof=1 CLV SE** — DONE (`value_backtest._mean_se` + `optimize_thresholds._mean_se`
  → sample variance; n<2 → None). The ROI bootstrap CI is a small follow-up
  (perf-sensitive inside the 46k-match sweep).
- **#5 calibration monitor** — DONE (`meta_model_calibration_by_close_anchor` stratifies
  by closing_anchor_type; consensus stratum is the score-aligned read).
- **#3 edge-rank ledger** — implemented via the risk-kelly specialist (deferred,
  raw_kelly-sorted reserve pass).
- **#1 proxies** — done operator-side. **#2 / #6 / #7** — operator/data-gated.
