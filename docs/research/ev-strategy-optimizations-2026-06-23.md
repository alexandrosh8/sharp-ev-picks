# +EV Strategy Optimizations — Prioritized, Sourced Research Brief

Date: 2026-06-23
Author: quant-sports-researcher
Status: ready to feed ADRs (devig-per-market ADR-0006, premium-gate ADR-0016,
CLV-anchor ADR-0014/0017, plus new staking + calibration ADRs)

Evidence tags used throughout: **[ESTABLISHED]** = peer-reviewed / canonical
primary or convergent multi-source; **[PRACTITIONER]** = credible industry
source, not peer-reviewed; **[FOLKLORE]** = widely repeated, weakly sourced —
do NOT present as fact; **[HYPOTHESIS]** = our inference, must be validated on
own data. Nothing here treats betting as guaranteed profit.

---

## Question

From ~1 week of live evidence, losses are concentrated in obscure low-liquidity
minor soccer leagues (soft closing line → unreliable CLV); the premium tier
shows stake-weighted CLV +11.9% but negative ROI over n=13; a low-edge volume
tier showed CLV ~0 and was disabled. What sourced optimizations, ranked by
(expected ROI impact x evidence strength) vs implementation effort, should we
make to the devig → fair-prob → edge/EV → fractional-Kelly → CLV/ROI pipeline?

## Method

Six parallel literature/market sweeps (liquidity filtering; devig-per-market;
edge/odds-band; CLV & sharp anchor; fractional-Kelly & correlation; calibration),
each required to cite every claim with a URL and to flag folklore vs established
result. Canonical papers (Štrumbelj 2014, Clarke 2017, Thorp 2006, Buchdahl
efficiency studies, Wilkens 2024/2026, Baker & McHale 2013, Kull 2017) verified
to primary where the PDF was machine-readable; abstract-level where blocked
(flagged inline).

---

## PRIORITIZED RECOMMENDATIONS

Ranking key: **Impact** = expected ROI effect x evidence strength;
**Effort** = implementation cost. Sorted by Impact/Effort.

### P1 — Liquidity gate before trusting CLV or booking an edge (HIGHEST impact, LOW effort)

**Impact: very high (directly targets the observed loss concentration).
Effort: low (a filter + a per-market allow-list).**

- **[ESTABLISHED]** Closing-line efficiency is *conditional on sharp volume*;
  the close is a true-probability anchor only in liquid, high-limit markets.
  Buchdahl: CLV is a valid EV proxy only "if the closing NVP is a reasonable
  reflection of the true price."
  https://www.pinnacleoddsdropper.com/blog/closing-line-value--clv-demystified-by-expert-joseph-buchdahl ·
  https://www.football-data.co.uk/blog/pinnacle_efficiency.php
- **[ESTABLISHED]** Efficiency demonstrated only for very popular markets (EPL,
  NFL, NBA-class). In immature/low-limit markets, positive CLV vs a soft early
  line reflects **line immaturity, not skill**.
  https://www.pinnacle.com/betting-resources/en/educational/efficient-market-hypothesis-in-sports-betting-why-early-bets-beat-the-market
- **[FOLKLORE — do NOT lean on]** "The edge is in obscure minor leagues." The
  rigorous *profitable* evidence (Constantinou & Fenton pi-rating; arbitrage
  study) is concentrated in **top** leagues, not minor ones.
  https://arxiv.org/pdf/1710.02824 · http://constantinou.info/downloads/papers/evidenceofinefficiency.pdf
- Concrete, sourced liquidity proxies and thresholds:
  - **Pinnacle limit + vig as a book-confidence gate**: higher limit / lower vig
    = sharper line; demand a margin of safety inversely proportional to market
    efficiency.
    https://www.pinnacleoddsdropper.com/guides/market-limits-and-vigs-what-are-they-and-why-are-they-important
  - **Betfair matched volume**: "good liquidity >= GBP 10,000 at start of match";
    Betaminic splits major/minor at **GBP 5,000** average matched ~1 min to KO
    (EPL ~GBP 592k ... down to <GBP 5k for 71 "minor" leagues); a market with
    only ~GBP 200 matched is "too thin to anchor a rating against."
    https://www.betaminic.com/betting-research/which-football-leagues-have-the-most-liquidity-on-betfair/
  - **Overround width**: sharp books ~2-3%, soft 8-12%; football 1X2 overround
    ~111-112% and "as high as 118% for less popular European leagues."
    https://help.outlier.bet/en/articles/9922960-how-sportsbooks-set-odds-soft-vs-sharp-books ·
    https://betting.football-data.co.uk/overround.php
- **Action:** per-market allow-list (top divisions + NBA/major soccer); **refuse
  to compute CLV/edge** when Pinnacle limit/vig is outside the confident band OR
  Betfair matched volume is below the GBP 5k major/minor line (prefer >= GBP 10k
  at KO). This is the same direction as ADR-0016 (major-league premium gate) —
  extend it from a league list to a *measured liquidity gate*.
- **[HYPOTHESIS / GAP]** No sourced numeric cutoff ($ limit / GBP volume / bet
  count) below which to discard the close exists in the literature — qualitative
  only. Tune the threshold empirically and label it self-imposed.

### P2 — Minimum-edge floor anchored to estimation error + odds-band ceiling (HIGH impact, LOW effort)

**Impact: high. Effort: low (two config constants + a band filter).**

- **[ESTABLISHED]** Kelly is acutely input-sensitive: a 3pp probability error can
  double/triple recommended stake; edges of 2-3% can fall within the estimation
  noise floor. A too-low edge threshold systematically bets noise.
  https://marketmath.io/blog/kelly-criterion-guide ·
  https://predictionengine.app/learn/kelly-criterion-sports-betting
- **[PRACTITIONER]** Value services commonly gate at a **minimum 1-3% edge** over
  the no-vig fair price. https://www.rebelbetting.com/faq/expected-value-and-variance
- **[HYPOTHESIS, well-grounded]** Set the minimum edge **above your measured
  devig/model estimation error** (not an arbitrary 1%). With ~1-2% typical error,
  a **2-3% floor** is principled; <1% is almost certainly noise. This directly
  explains why the low-edge volume tier showed CLV ~0 (correctly disabled).
- **[ESTABLISHED]** Longshots carry a *double penalty*: Kelly already shrinks the
  fraction as odds lengthen (f = (bp-q)/b), AND fair-price estimation error is
  largest there. Naive football returns: ~break-even shorter than ~1.50, ~-20%
  or worse beyond 5.00 (Buchdahl); horse racing extreme longshots ~-61%
  (Snowberg & Wolfers). https://en.wikipedia.org/wiki/Kelly_criterion ·
  https://www.football-data.co.uk/blog/favourite_longshot_bias_revisited.php ·
  https://www.nber.org/system/files/working_papers/w15923/w15923.pdf
- **CAUTION — [ESTABLISHED debate]** FLB is largely *already priced into a sharp
  Pinnacle close*; on the sharpest books "no specific sign of any longshot bias."
  Do NOT manufacture a structural longshot edge from FLB folklore — edge must
  come from beating the anchor.
  https://www.researchgate.net/publication/351985837_Favorite-Longshot_Bias_and_Market_Efficiency_in_the_Soccer_Betting_Market ·
  https://datagolf.com/fav-longshot-not-a-bias
- **Action:** minimum-edge floor = max(2%, measured devig error band); add an
  odds-band ceiling (e.g. suppress or higher-threshold picks beyond ~5.00 / a
  higher edge bar on long odds). Mark the band-conditional threshold a hypothesis
  and A/B it.

### P3 — Devig method per market (HIGH impact, LOW-MEDIUM effort; partly already ADR-0006)

**Impact: high (method choice can flip a marginal pick in/out of +EV).
Effort: low-medium (drop a redundant method, set per-market policy).**

- **[ESTABLISHED — math identity]** On any **2-way** market (totals, spreads/AH),
  **additive and Shin are EXACTLY equivalent** — running both is redundant. The
  four-method panel collapses to three distinct outputs on 2-way markets
  {multiplicative, power, additive≡Shin}.
  https://cran.r-project.org/web/packages/implied/vignettes/introduction.html ·
  https://github.com/mberk/shin
- **[ESTABLISHED]** Across the two leading peer-reviewed studies, **power and
  Shin beat multiplicative/basic normalization** at recovering true
  probabilities. Štrumbelj (2014, *IJF*): Shin most accurate (best in 217/412
  book-competition pairs, 37 competitions / 5 sports).
  https://www.sciencedirect.com/science/article/abs/pii/S0169207014000533 ·
  https://docslib.org/doc/1515964/on-determining-probability-forecasts-from-betting-odds
  Clarke, Kovalchik & Ingram (2017): **power universally beats multiplicative,
  beats or matches Shin**, and uniquely stays in [0,1] while correcting FLB.
  https://outlier.bet/wp-content/uploads/2023/08/2017-clarke-adjusting_bookmakers_odds.pdf
- **[ESTABLISHED]** FLB is **strong in 3-way 1X2 but absent in the 2-way Asian
  handicap** for the same matches (Hegarty & Whelan 2025, *IJF*) — so
  FLB-correcting devig (Shin/power) matters most on 1X2; the 2-way AH side is the
  better raw probability anchor.
  https://ideas.repec.org/a/eee/intfor/v41y2025i2p803-820.html ·
  https://www.karlwhelan.com/sports-betting-asian-handicap-vs-1x2/
- **[ESTABLISHED / HYPOTHESIS]** Method divergence is bounded by margin: ~1-2pp on
  lopsided/high-margin lines (can flip a marginal +EV call), sub-pp on sharp
  near-even lines. https://betherosports.com/blog/devigging-methods-explained
- **Recommended policy (updates ADR-0006):**
  - **Soccer 1X2 (3-way):** Shin or power (avoid plain multiplicative; they
    genuinely differ at 3 outcomes — carry both, FLB-correct).
  - **Totals & spreads/AH (2-way):** power default; additive≡Shin as the
    FLB-correcting alternative; **drop the duplicate Shin run on 2-way**.
  - Multiplicative acceptable only as a low-margin sharp-line approximation — the
    "good enough" claim is **folklore**, not proven.
- **[FOLKLORE — reject]** Bet Hero's "the draw isn't a longshot so Shin is less
  applicable to 1X2" contradicts Štrumbelj; do not adopt it.

### P4 — Calibration on log-loss with forward-in-time fit (HIGH impact, MEDIUM effort)

**Impact: high (miscalibration manufactures phantom edges → oversized Kelly).
Effort: medium (calibration layer + per-fold discipline).**

- **[ESTABLISHED — most on-point citation]** Wilkens (NBA): selecting models by
  **calibration** gave **+34.69% avg ROI** vs **-35.17%** by accuracy; calibration
  selection profitable in all tested cases. Directly applies to the LightGBM leg.
  https://arxiv.org/abs/2303.06021 ·
  https://www.sciencedirect.com/science/article/pii/S266682702400015X
- **[ESTABLISHED]** Mechanism: EV = p_model·odds - 1, so a 2pp overconfidence bias
  inflates computed edge and oversizes stakes via Kelly — exactly the phantom-edge
  failure mode. https://www.sports-ai.dev/blog/ai-model-calibration-brier-score
- **[ESTABLISHED]** GBMs (LightGBM) push probabilities toward a sigmoid distortion;
  Platt/sigmoid is the natural inverse. Isotonic needs **>~1000 samples** or it
  overfits — NBA per-market settled samples are often below that, so prefer
  **Platt or beta**. Beta calibration contains the identity (won't *un*-calibrate
  an already-good model) and handles skewed scores — strongest single default.
  https://scikit-learn.org/stable/modules/calibration.html · https://betacal.github.io/ ·
  https://transferlab.ai/refs/niculescu-mizil_predicting_2005/
- **[ESTABLISHED]** Select/monitor on **log-loss** (harsher on overconfidence than
  Brier) + reliability diagram + ECE; require **calibration-in-the-small** within
  the probability band you actually bet (e.g. 55-65% favorites).
  https://journals.sagepub.com/doi/10.1177/22150218261416681 ·
  https://www.statstest.com/calibration-checks-brier-score-reliability-diagrams
- **[ESTABLISHED]** Fit the calibrator on a **forward-in-time held-out window, per
  fold, never in-sample** — an in-sample map biases toward 0/1 and lies. Wire into
  existing calibration-eval / walkforward-backtest / leakage-auditor tooling.
  https://scikit-learn.org/stable/modules/calibration.html ·
  https://www.emergentmind.com/topics/walk-forward-validation-strategy
- **Honest caveat (soccer):** in Wilkens' Bundesliga xG study the bookmaker's odds
  were *better* calibrated than the model — edge comes from signal the market
  misses, not from out-calibrating the market.
  https://journals.sagepub.com/doi/10.1177/22150218261416681

### P5 — Sharp-anchor selection per market + dual-anchor logging (MEDIUM-HIGH impact, MEDIUM effort)

**Impact: medium-high. Effort: medium (anchor routing + logging both anchors).**

- **[ESTABLISHED]** Default anchor = **devigged Pinnacle close**; cleanest CLV→EV
  quantification is Buchdahl's slope≈1.00 over 87,960 pairs, and R²=0.997 over
  397,935 Pinnacle football games. DataGolf weights 95-100% to Pinnacle's close
  on liquid markets. https://www.football-data.co.uk/blog/pinnacle_efficiency.php ·
  https://www.reading.ac.uk/web/files/economics/emdp201910.pdf ·
  https://datagolf.com/how-sharp-are-bookmakers
- **[ESTABLISHED]** Soccer: anchor to the **Asian totals/handicap** close (more
  efficient than 1X2; Whelan & Hegarty) and **derive 1X2 from it**.
  https://mpra.ub.uni-muenchen.de/116925/1/MPRA_paper_116925.pdf ·
  https://www.pinnacle.com/betting-resources/en/betting-strategy/how-good-are-pinnacles-asian-handicap-markets/zm62p4gdr62ngx8n
- **[ESTABLISHED]** Betfair is bias-free and beats margined books in deep liquid
  markets, but **degrades / reverses in smaller markets** (Štrumbelj). Use it as
  anchor **only above a liquidity gate**, on a spread-corrected, commission-adjusted
  **mid** — never last-traded. Effective back after commission =
  1 + (odds-1)(1-c). https://www.sciencedirect.com/science/article/abs/pii/S0169207010000105 ·
  https://www.sciencedirect.com/science/article/abs/pii/S0169207014000533
- **[ESTABLISHED / HYPOTHESIS]** Consensus is best as a **fallback/coverage layer**
  (when Pinnacle is absent/thin), built **sharp-weighted, devig-then-median** — an
  equal-weight average is dominated by devigged Pinnacle on liquid markets; "books
  differ in accuracy" is established, the weighting specifics are folklore.
  https://ideas.repec.org/a/eee/intfor/v26yi3p482-488.html ·
  https://www.football-data.co.uk/The_Wisdom_of_the_Crowd_updated.pdf
- **[ESTABLISHED]** Anchor the fair price on consensus/Pinnacle but **bet the best
  available price** (Direr: 4.45% return on best odds vs 2.78% on mean).
  https://ideas.repec.org/a/taf/applec/v45y2013i3p343-356.html
- **NBA:** [ESTABLISHED] benchmark = the closing number (Levitt); exclude/de-weight
  **early-season totals** (documented UNDER bias ~56-58% Week 1).
  http://pricetheory.uchicago.edu/levitt/Papers/LevittWhyAreGamblingMarkets2004.pdf ·
  https://www.sciencedirect.com/science/article/abs/pii/S1544612307000177
- **Action:** route the anchor per market as above; **log Pinnacle-close AND
  Betfair-mid-close AND consensus per pick** to settle the (literature-unsettled)
  three-way sharpness ranking on own data per league/market. Keep the CLV
  denominator on a single efficient close — do not switch it to a noisy consensus.
- **[GAP]** No peer-reviewed head-to-head of Pinnacle vs Betfair vs consensus on
  identical fixtures with CLV; no NBA-specific CLV→ROI magnitude (Buchdahl is
  soccer-only). Generate both in-house.

### P6 — Fractional-Kelly fraction + correlated-bet handling (MEDIUM impact, LOW-MEDIUM effort)

**Impact: medium (variance/drawdown + correlation; protects, doesn't add edge).
Effort: low for caps, medium for joint Kelly.**

- **[ESTABLISHED]** Half-Kelly ≈ **75% of growth at 50% of std-dev (= 25% of
  variance)** — NOTE the common "1/2 variance" line is folklore; correct figure is
  1/4 variance (Thorp §7). P(ever halving): ~50% full vs ~12.5% half.
  https://gwern.net/doc/statistics/decision/2006-thorp.pdf
- **[ESTABLISHED]** Fractional Kelly also hedges **estimation error** — the cost is
  asymmetric: half-Kelly costs ~25% of growth but *survives* a 2× edge overestimate
  that *ruins* full Kelly; beyond 2× full Kelly long-run growth is negative.
  Baker & McHale (2013, INFORMS *Decision Analysis* — NOT the IMA Journal) prove
  bet size should shrink under parameter uncertainty.
  https://gwern.net/doc/statistics/decision/2006-thorp.pdf ·
  https://pubsonline.informs.org/doi/abs/10.1287/deca.2013.0271
- **[PRACTITIONER]** Standard fraction is **1/4 to 1/2 Kelly**; lean lower given
  genuine model uncertainty. Per-bet cap 1-5% (pros <=2.5%); daily/simultaneous
  cap ~5-10% — the cap *numbers* are folklore (make them configurable policies),
  but the *existence* of a simultaneous cap is rigorously motivated.
  https://betstamp.com/education/kelly-criterion · https://betresearcher.com/guides/sports-betting-bankroll-management/
- **[ESTABLISHED]** Scalar Kelly has no covariance term and over-sizes simultaneous
  correlated bets; correct joint allocation is *smaller* than the sum of singles.
  Rigorous fix = multivariate Kelly (Whitrow 2007, *JRSS-C*; Thorp QP over the
  covariance/joint distribution). Cheap guardrail = treat a correlated cluster
  (same game multi-market, same-day shared-factor) as one bet under the exposure
  cap. https://rss.onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-9876.2007.00594.x ·
  https://en.wikipedia.org/wiki/Kelly_criterion · https://arxiv.org/pdf/0803.1364
- **[ESTABLISHED]** Validate staking changes by **Monte Carlo bankroll simulation**
  (>=10,000 paths) reporting expected return, max drawdown, risk of ruin.
  https://www.stat.berkeley.edu/~aldous/157/Papers/Good_Bad_Kelly.pdf ·
  https://www.thestakingmachine.com/monte-carlo-simulations/

### P7 (cross-cutting) — Use CLV as the primary KPI, but only where the anchor is liquid

**Impact: high diagnostic value. Effort: low (already tracked; gate it).**

- **[ESTABLISHED]** CLV reaches significance ~10× faster than P&L (SD ≈ 0.1 vs
  ~1.0) — possibly ~50 bets vs several thousand. Long-run yield ≈ the % you beat
  the close by (Buchdahl ~1:1: realized 3.4% vs 4.0% expected over ~20k bets).
  https://www.pinnacleoddsdropper.com/blog/closing-line-value--clv-demystified-by-expert-joseph-buchdahl
- This **vindicates the premium-tier read**: +11.9% stake-weighted CLV over n=13
  is a legitimate *leading* indicator; negative ROI at n=13 is pure variance, not
  a falsification. Keep the tier, grow n, judge on CLV — **but only on liquid
  markets where the close is real** (P1). CLV measured in soft minor leagues is
  meaningless and is the mechanism behind the loss concentration.
- **[FOLKLORE — reject]** "Positive CLV → 2-3× ROI" and "2-5% CLV → 15-25% annual
  ROI" appear on SEO sites and contradict Buchdahl's ~1:1 mapping. Realistic
  sustainable yield is low single digits to ~6%.

---

## Implications for this project

1. The observed loss concentration is the **predicted failure mode** of measuring
   EV against a soft minor-league close — not a signal to mine those leagues. P1
   (liquidity gate) is the single highest-leverage, lowest-effort fix and extends
   ADR-0016 from a league list to a measured gate.
2. The disabled low-edge volume tier was the **correct** call (sub-noise edges =
   CLV ~0). Formalize it as a measured minimum-edge floor (P2).
3. ADR-0006 (devig-per-market) should be updated: drop the redundant Shin run on
   2-way markets; 1X2 → Shin/power; totals/AH → power (P3).
4. Calibration (P4) is the highest-evidence ROI lever for the LightGBM NBA leg
   (Wilkens NBA result) and needs forward-in-time, per-fold fit to avoid phantom
   edges — wire into existing calibration-eval/leakage-auditor tooling.
5. Anchor routing (P5) should prefer devigged Pinnacle (AH-derived for soccer),
   gate Betfair by liquidity, and **log all three anchors** to settle the
   unsettled sharpness ranking on own data.
6. Staking (P6): default to 0.25-0.5 Kelly leaning lower; add correlated-cluster
   exposure caps; validate by Monte Carlo before any change.

## Recommended decision

Adopt P1 + P2 + P3 immediately (low effort, high impact, all extend existing
ADRs). Schedule P4 (calibration) and P5 (anchor routing + dual logging) as the
next medium-effort work. Treat P6 as a guardrail hardening pass. Track everything
on CLV-on-liquid-markets (P7) as the primary KPI; do not judge tiers on ROI at
small n.

## Open questions (literature gaps — validate on own data, do not fabricate)

- No sourced numeric liquidity/limit threshold for discarding a close — tune
  empirically.
- No clean published backtest of edge-threshold vs realized ROI; none isolating
  where *anchor-relative* +EV concentrates by odds band.
- No peer-reviewed Pinnacle vs Betfair vs consensus head-to-head; no NBA-specific
  CLV→ROI magnitude.
- No LightGBM-specific calibration-drift quantification; no direct "calibration
  reduced phantom edges by X%" statistic — label any such number as own result.

## Honesty flags carried from sources

- "Half-Kelly = 1/2 variance" is folklore — correct is 1/4 variance (Thorp).
- Baker & McHale venue is INFORMS *Decision Analysis* (2013), not the IMA Journal.
- "FLB is an exploitable edge" is contested — largely priced into a sharp close.
- Several Pinnacle article bodies are JS-gated; titles/URLs verified, bodies not
  read verbatim. Štrumbelj/Clarke per-table numerics were abstract-verified where
  the PDF was blocked — re-read primaries before quoting exact deltas in an ADR.
