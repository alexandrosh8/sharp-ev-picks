# Literature + X Strategy Sweep — Implementable +EV Ideas for sharp-ev-picks

**Date:** 2026-07-10
**Author:** quant-sports-researcher (agent)
**Question:** What implementable +EV strategy ideas from 2023-2026 academic literature,
X (Twitter), and sports-analytics communities are worth a walk-forward backtest on this
platform's data (BSP archive to 2025-12, ARCADIA Pinnacle capture, football-data,
settled-picks warehouse)? Success metric: trusted sharp-close CLV only.

**Excluded by prior decisions (not re-proposed):** per-market devig, logit-pool anchor,
steam detection (tested, kept off); calibration haircut (rejected); odds ceiling 4.0 +
power devig + sharp-anchor gate (shipped); Shin tail-devig + basketball promotion
(pre-registered ADR-0019, no re-tuning on spent data).

## Method

- Skimmed the two June corpora (`docs/research/external/2026-06-27-tavily-corpus-1092.json`,
  `-x-corpus-318.json`) first: they covered devig/Kelly/CLV mechanics, steam, model
  libraries, and a first pass of X accounts (BettingIsCool, nishikoripicks, PickTheOdd,
  ClosingDime, teddy_covers, etc.). This sweep targeted only new ground.
- Tavily MCP was reachable in this run; 12 targeted searches across the 7 hunt areas
  (2026-07-10). Claims below cite the source found; anything not independently
  verifiable at fetch time is marked.
- **X-discourse caveat:** direct X searches now return mostly SEO/content-marketing and
  coaching-funnel material. No new named accounts with verifiable track records
  surfaced beyond the June X corpus. Practitioner signal below is therefore drawn from
  identifiable practitioner outlets (Unabated, Establish The Run, Pinnacle's own
  research blog, r/algobetting, r/EVbetting) and is labelled folklore where it is.

## Findings

### 1. Market efficiency / CLV literature 2023-2026

**F1 — 1X2 vs Asian Handicap: a two-market bias (established result).**
Hegarty & Whelan, *"Forecasting Soccer Matches With Betting Odds: A Tale of Two
Markets"*, International Journal of Forecasting (2025), and *"Returns on Complex Bets:
Evidence From Asian Handicap Betting on Soccer"*, Review of Behavioral Finance (2024):
soccer 1X2 odds are systematically biased (favourite-longshot pattern); Asian Handicap
odds on the same fixtures are not, and the AH market priced the COVID home-advantage
drop essentially perfectly (*"The Wisdom of No Crowds"*, J. Prediction Markets, 2023).
Source: https://www.karlwhelan.com/sports-betting-research (author page, accessed
2026-07-10). Implication: an AH-derived fair probability is a better anchor than a
devigged 1X2 line, especially in the tails — directly relevant to a sharp-vs-soft
platform whose shipped mitigations (odds ceiling 4.0) are blunt-instrument fixes for
exactly this bias.

**F2 — Bet-timing: market forecasts do NOT improve monotonically to the close
(established result, MLB).** Simon, J. (2024), *"Inefficient Forecasts at the
Sportsbook: An Analysis of Real-Time Betting Line Movement"*, Management Science
70(12), 8583-8611. Full opening-to-close movement for 3,681 MLB games across 4 books:
forecasts are mostly reliable, but accuracy does not improve monotonically as kickoff
approaches — e.g. weekend day-game prices at start time are significantly *worse* than
90 minutes earlier, and simple strategies on these windows were profitable in-sample.
Source: https://aura.american.edu/articles/journal_contribution/Inefficient_Forecasts_at_the_Sportsbook_An_Analysis_of_Real-Time_Betting_Line_Movement/30546293.
Implication: "the close is the best price" is an approximation; time-of-day /
time-to-kickoff segments can carry exploitable stale windows even at the close. This
motivates segmenting our own trusted CLV by time-to-kickoff bucket and choosing alert
timing per segment, rather than one freshness policy.

**F3 — Favourite-longshot bias remains the most robust anomaly, including on new
venues.** Whelan, *"Risk Aversion and Favourite-Longshot Bias"*, Economica (2024)
(mechanism: bookmaker risk aversion, not just bettor bias); Bürgi, Deng & Whelan,
*"Makers and Takers: The Economics of the Kalshi Prediction Market"* (working paper,
Jan 2026): low-priced Kalshi contracts produce large losses — FLB reproduced on a
CFTC-regulated prediction market. Source: karlwhelan.com (above). Implication: our
shipped odds-ceiling is well supported; prediction-market prices (Kalshi/Polymarket)
should NOT be treated as unbiased anchors in the tails if ever ingested.

**F4 — Shin/booksum null (caution flag, not a proposal).** arXiv 2604.17194 (2026),
*"Forecast Sports Outcomes under Efficient Market Hypothesis"*: no significant
correlation found between a market's booksum (overround) and the accuracy of its odds,
under either Shin variant (Štrumbelj 2014 numerical; Kizildemir et al. 2025
analytical). Source: https://arxiv.org/html/2604.17194v1. Relevance: tempers
expectations for the pre-registered ADR-0019 Shin tail-devig test — do not raise its
prior; the pre-registration stands as written.

### 2. X / practitioner discourse 2025-2026

- **Quality finding: the open-web X signal has degraded.** Searches for "where edges
  persist 2025/2026" return coaching funnels and affiliate SEO. No new
  track-record-credible named accounts beyond the June corpus. (Own observation from
  this sweep, 2026-07-10.)
- **Player props at soft books remain the practitioner-consensus edge, with limits as
  the binding constraint** (folklore, consistent across sources): Establish The Run,
  "How to Beat NFL Player Props in 2025" (YouTube, 2025-09-04,
  https://www.youtube.com/watch?v=oi5v8ilc8So) — books adapting, unders + post-move
  timing discussed; Unabated, "The 4 Commandments of Injury News in Sports Betting"
  (YouTube, 2025-11-13, https://www.youtube.com/watch?v=lTl9px_ntv4) — news-driven
  prop betting profitable but latency-critical.
- **The NBA "beat the tweet" injury edge is now an automation race** (folklore):
  r/EVbetting thread "NBA injury betting used to be easy and now it's a race"
  (https://www.reddit.com/r/EVbetting/comments/1qqbu3t/, 2025-2026, exact date
  unverifiable). Human-in-the-loop picks platforms cannot win a sub-second race;
  the viable variant is stale-soft-line detection *after* the sharp anchor has moved —
  which is our existing architecture, applied to a tighter pre-tip window.
- **Sharp-accessible venues shifting to exchanges/prediction markets** (folklore):
  Circa, Novig, ProphetX named as 2026 sharp-friendly venues
  (https://xclsvmedia.com/best-sharp-friendly-sportsbooks-2026-where-bet-when-limited,
  2026); CFTC drafting prediction-market rules (Insurance Journal, 2026-06-11).
  Decision-support relevance: none immediate (picks-only, EU-facing books).

### 3. Tennis (display-only)

- **Model-vs-market benchmarks: headroom above Elo is narrow and the market wins on
  calibration.** MDPI Analytics 5(3):22 (2026), *"A Unified Benchmark of Machine
  Learning and Deep Neural Networks for Tennis Match Prediction"*
  (https://www.mdpi.com/2813-2203/5/3/22): tuned Elo 65.87% accuracy; best classical
  ML 66.30%; DNNs cluster 66.15-66.22%; hybrid Elo-ML 67.52%. Dryja BSc thesis (VU
  Amsterdam, Grand Slams 2010-2024,
  https://www.cs.vu.nl/~wanf/theses/dryja-bscthesis.pdf): bookmaker log loss 0.4888 /
  Brier 0.1608 vs best model (random forest) 0.4876 / 0.1600 — models at best *match*
  the market, in-sample-adjacent. Consistent with Kovalchik (2016) and Tennis
  Abstract's 2017 comparison where the Pinnacle-based model beat all Elo variants
  (https://www.tennisabstract.com/blog/2017/01/15/measuring-the-performance-of-tennis-prediction-models).
  **Implication: do not build a tennis model; sharp-vs-soft line shopping is the right
  architecture for tennis. Confirms current display-only stance.**
- **Tennis FLB is stable, 2025 was an anomaly year.** Pinnacle Betting Resources,
  *"The Favorite-Longshot Bias in ATP Tennis: What 40,000 Matches Reveal"*
  (late 2025/early 2026,
  https://www.pinnacle.com/betting-resources/en/educational/the-favorite-longshot-bias-in-atp-tennis-what-40000-matches-reveal;
  mirrored at https://tennisedge.io/favorite-longshot-bias-tennis-betting): ATP main
  draw 2010-2025 on Pinnacle closes, flat stakes — favourites structurally outperform
  longshots; 2025 alone reversed (favourites -3.1%, dogs -0.85%). Academic support:
  Abinzano, Bonilla & Muga (2025), *"On the Longshot Bias in Tennis Betting Markets:
  The Casco Normalization"*, Journal of Mathematical Economics (via
  https://ideas.repec.org/p/pra/mprapa/47905.html citation listing). Implication: when
  tennis is promoted beyond display, apply an odds ceiling analogous to soccer's 4.0
  from day one.
- **Retirement/walkover settlement is NOT uniform across books — a real cross-book
  risk for a line-shopping platform.** bet365: disqualification/retirement → all bets
  "no action" unless already determined (https://help.bet365.com/s/en-us/sportsrules/tennis,
  accessed 2026-07-10), plus a promotional "Tennis Retirement Guarantee" paying the
  non-retiring side as a winner in some jurisdictions
  (https://www.bet365.com/promos/en-us/home/tennis-retirement-guarantee). Other books
  use one-set-completed or ball-served rules — comparative guides: OddsMonkey
  (2026-01-02, https://www.oddsmonkey.com/blog/matched-betting/tennis-betting-retirement-rules)
  and Outplayed (2025-09-03, https://outplayed.com/blog/tennis-retirement-rules).
  **Our `pinnacle_one_set` settlement convention will diverge from void-on-retirement
  books on every retirement: a "won" pick per our settlement may be voided at the soft
  book the user actually bets, and vice versa.** Before any tennis promotion, the
  per-book settlement rule must be attached to the pick metadata or the EV is
  mis-stated for retirement scenarios. (Retirement base-rate not verified in this
  sweep — flagged as open question.)

### 4. NFL (display-only)

- **Odds-movement predictability + home-favourite overvaluation** (weak evidence:
  undergraduate thesis, but uses 2020-2024 high-frequency data): Costa, E. (2025),
  *"NFL Moneyline Market Efficiency"*, CMC Senior Theses
  (https://scholarship.claremont.edu/cgi/viewcontent.cgi?article=5145&context=cmc_theses):
  direction of intra-week odds movement is predictable (books adjusting to sharp
  action); home-field advantage overpriced in forecast-close games; profitable
  away-side system in the 0.3-0.7 win-probability band (in-sample; no true holdout
  discipline evident).
- **Look-ahead lines:** practitioner guidance only (ESPN 2025 betting guide,
  https://www.espn.com/espn/betting/story/_/id/46038649/, and PFF
  https://www.pff.com/news/bet-why-betting-early-critical-beating-nfl-markets):
  early-week/look-ahead numbers are softer but injury-risky. No rigorous 2023-2026
  study found. Our capture does not include look-ahead lines → not backtestable today.
- **Free sharp-close data (NEW, usable now):** the nflverse ecosystem
  (https://github.com/nflverse/nflverse-data; Lee Sharpe's nfldata `games` release,
  loadable via `nflreadpy`) ships per-game spread/total/moneyline lines for free,
  maintained by GitHub Actions. **Caveat (unverified): whether those lines are true
  closes vs consensus snapshots must be confirmed at the field level before using as a
  CLV close.** Also confirmed: The Odds API historical snapshots at 10-minute
  intervals since 2020-06 (props since 2023-05), paid credits
  (https://the-odds-api.com/historical-odds-data); new-entrant comparison of six odds
  APIs at https://oddspapi.io/blog/best-odds-apis-2026-comparison (2026); TXODDS "Tx
  LAB" historical archive is enterprise-priced (https://txodds.net/our-products/tx-lab).

### 5. Basketball (shadow)

- No published 2023-2026 walk-forward benchmark of a public NBA model against sharp
  closes surfaced — the space is dominated by promotional material; r/algobetting
  claims (e.g. "60-65% win rate" threads, 2025-26 season) are unverifiable and almost
  certainly selection-biased. Treat as absence of evidence.
- Rest/travel/B2B factors are priced quickly per practitioner consensus; the residual
  edge claimed by practitioners is concentrated in (a) the ~90-minute pre-tip window
  around lineup confirmation (oddsindex.com trend piece + Unabated above — folklore)
  and (b) derivative/prop markets that lag the mainline move. For our shadow NBA
  strategy the transferable, testable idea is the same as F2: time-to-tip CLV
  segmentation, not new features.

### 6. Soccer — anything beating a Pinnacle-anchored sharp-vs-soft baseline?

- Nothing found in 2023-2026 that beats a sharp-close-anchored baseline in a published
  walk-forward test. The systematic review (arXiv 2410.21484, Oct 2024,
  https://arxiv.org/html/2410.21484v1) catalogues ML pipelines but its profitable
  entries are in-sample or bet-vs-soft-books results; SSRN 5381388 (xG simple models)
  reports signals, not sharp-close outperformance. This is consistent with our own
  finding that goals models do not beat the anchor. The only structural result that
  *changes the anchor itself* is F1 (AH unbiased vs 1X2 biased) — which is why it
  heads the shortlist.

### 7. New data sources 2025-2026 (summary)

| Source | What | Cost | Status |
|---|---|---|---|
| nflverse / Lee Sharpe nfldata (github.com/nflverse) | NFL per-game lines w/ schedules, automated | Free | USE-CANDIDATE (verify close-vs-consensus) |
| The Odds API historical (the-odds-api.com/historical-odds-data) | 10-min snapshots since 2020-06; props since 2023-05 | Paid credits | WATCH (we already hold keys; historical credits pricey) |
| OddsPapi (oddspapi.io, 2026) | New odds-API entrant, comparison blog | Freemium | WATCH (unproven; file-level eval via repo-researcher before use) |
| TXODDS Tx LAB (txodds.net) | 800-book, decades-deep archive | Enterprise | REJECT (cost) |
| Kalshi/Polymarket prices | Prediction-market sports prices | Free-ish | REJECT as anchor (F3: FLB present); possible display cross-check only |

## Scored table

Scales: Evidence quality 1-5 (5 = top peer-reviewed journal, replicated); Backtestability
with our data 1-5 (5 = runnable this week on existing warehouse).

| # | Idea | Sport(s) | Source (URL, date) | Ev. | Btest. | Risk | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | AH-derived fair probability as the soccer anchor (1X2 biased, AH not) | Soccer | Hegarty & Whelan IJF 2025; RBF 2024 — karlwhelan.com/sports-betting-research | 5 | 4 | Low — anchor-side change, shadow-first | **USE-CANDIDATE** |
| 2 | Time-to-kickoff CLV segmentation → per-segment alert-timing policy | Soccer, NBA | Simon, Mgmt Sci 70(12) 2024 — aura.american.edu/.../30546293 | 4 | 5 | Low — measurement first, policy later | **USE-CANDIDATE** |
| 3 | nflverse free NFL line archive as display-tier CLV benchmark | NFL | github.com/nflverse/nflverse-data (live, 2026) | 3 | 4 | Low — data enablement; close-vs-consensus unverified | **USE-CANDIDATE** |
| 4 | Tennis odds ceiling at promotion time (FLB stable on Pinnacle closes) | Tennis | Pinnacle Betting Resources 2025/26; Abinzano et al. JME 2025 | 4 | 3 | Low | WATCH (pre-condition for promotion) |
| 5 | Per-book tennis retirement-rule metadata (settlement divergence vs pinnacle_one_set) | Tennis | bet365 rules; OddsMonkey 2026-01-02; Outplayed 2025-09-03 | 4 | 2 | Medium — mis-stated EV if ignored | WATCH (blocker for tennis promotion) |
| 6 | NBA pre-tip (≤90min) stale-soft-line window | NBA | Unabated 2025-11-13; r/EVbetting 2025 (folklore) | 2 | 4 | Medium — latency; folds into idea 2 | WATCH |
| 7 | NFL away-side in close games / HFA overpricing | NFL | Costa CMC thesis 2025 | 2 | 2 | High — thesis-grade, in-sample | WATCH |
| 8 | NFL look-ahead line capture | NFL | ESPN 2025, PFF (folklore) | 1 | 1 | — no capture exists | REJECT (for now) |
| 9 | Soft-book player-prop line shopping (NBA/NFL props) | NBA, NFL | ETR 2025-09-04; Unabated (folklore) | 2 | 1 | High — no prop sharp anchor in our capture | REJECT (data first) |
| 10 | Prediction-market prices as anchors | All | Bürgi, Deng & Whelan wp 2026-01 | 4 (against) | — | — | REJECT (FLB present) |
| 11 | Booksum/Shin over-tuning | All | arXiv 2604.17194 (2026) | 3 | — | — | Note only — supports ADR-0019 discipline |

## Top-3 shortlist with walk-forward backtest designs

### 1. AH-anchored soccer fair probabilities (idea 1)

- **Hypothesis:** replacing (or logit-blending is FORBIDDEN by prior decision — strictly
  replacing) the devigged 1X2 sharp anchor with an AH+O/U-derived fair probability
  improves trusted sharp-close CLV of value picks, most in the odds 2.5-4.0 band.
- **Dataset:** football-data.co.uk historical files (AH closing odds + 1X2 closes,
  2019-20 through 2025-26 seasons; note Pinnacle columns dead after 2026-01-15 —
  use the pre-anomaly window for the archive leg) + BSP archive to 2025-12 as the
  independent close for CLV scoring; forward leg on ARCADIA capture.
- **Split:** walk-forward by season; train nothing (this is an anchor transform, not a
  model) — the walk-forward element is threshold selection (edge ≥3% gate) fitted on
  season N, evaluated on season N+1. Forward shadow on live picks ≥4 weeks.
- **Metric:** trusted sharp-close CLV (log-ratio, per clv-evaluation skill), overall and
  per odds band; pick counts per band.
- **Success bar:** held-out CLV delta vs current power-devig 1X2 anchor > 0 with 95% CI
  excluding 0 on the archive leg, AND no coverage collapse (≥80% of current pick volume
  still anchorable — AH lines are missing for some fixtures/leagues).
- **Leakage guards:** anchor built strictly from pre-kickoff snapshots; CLV close never
  enters the anchor (kestrel-clv-correctness); AH line matched to fixture by
  canonical-event id, not name-form slug (marker-loss pitfall).

### 2. Time-to-kickoff CLV segmentation and alert-timing policy (idea 2)

- **Hypothesis:** our trusted CLV differs materially by time-to-kickoff bucket at pick
  issuance (e.g. T-24h+, T-24h..T-6h, T-6h..T-1h, T-<1h) and by kickoff local-time
  slot (Simon 2024's weekend-day-game effect), so per-segment issuance windows beat the
  single freshness gate.
- **Dataset:** settled-picks warehouse + ARCADIA/odds_snapshots timelines (we already
  persist multi-snapshot histories per event). No external data needed.
- **Split:** phase 1 is pure measurement (no fitting): estimate per-bucket trusted CLV
  with block-bootstrap CIs on all settled picks to date. Phase 2: any policy (e.g.
  suppress or delay alerts in negative buckets) is chosen on data through month M and
  evaluated shadow-only on months M+1..M+2, per shadow-first mandate.
- **Metric:** trusted sharp-close CLV per bucket; policy evaluated on aggregate trusted
  CLV of surviving picks vs baseline all-picks CLV.
- **Success bar:** at least one bucket with CI-separated negative CLV (justifying
  suppression), and shadow-period aggregate CLV improvement with CI excluding 0 before
  any live gate change.
- **Leakage guards:** bucket assignment uses pick-issuance timestamp only; no
  post-kickoff snapshots (existing guard); policy fit/eval months disjoint;
  do not reuse the spent 2025 BSP holdout for this (ADR-0019 discipline).

### 3. Free NFL sharp-close benchmark via nflverse (idea 3)

- **Hypothesis (enablement, not strategy):** nflverse/Lee Sharpe game lines give a
  free, maintained close-side benchmark good enough to score display-tier NFL picks'
  CLV, unblocking the operator's standing "calibrate NFL later" intent without new
  spend.
- **Dataset:** nflverse `games` (via `nflreadpy`), seasons 2020-2025, joined to our
  ARCADIA NFL capture by canonical event.
- **Design:** (a) delegate file-level repo inspection to repo-researcher (per policy —
  no adoption without inspection); (b) verify at field level whether spread/total/
  moneyline are closes, opens, or consensus snapshots (compare a sample against The
  Odds API historical snapshots for the same games); (c) if closes: agreement audit vs
  ARCADIA Pinnacle closes where both exist (correlation, mean abs diff in implied
  prob); (d) only then wire as a *display-tier* CLV close source, never as a trusted
  gate close.
- **Metric:** coverage %, implied-prob agreement vs ARCADIA close; downstream: NFL
  display CLV becomes reportable.
- **Success bar:** ≥95% event match on in-scope games, mean |Δ implied prob| vs
  Pinnacle close ≤ 1.5pp on moneylines; else mark the source consensus-grade and keep
  NFL CLV untrusted.
- **Leakage guards:** display-only labelling enforced (clv-evidence-reviewer trusted
  subset untouched); no nflverse-derived numbers feed pick generation.

## Recommended decision

Proceed in order 2 → 1 → 3: idea 2 is free (own data, measurement-first), idea 1 is the
only literature result that structurally improves a sharp-vs-soft anchor, idea 3 is
cheap enablement for a display tier. Tennis items 4-5 are pre-conditions to file
against any future tennis promotion ADR, not current work.

## Open questions

- Are nflverse game lines closes or consensus? (Blocking check for idea 3.)
- ATP/WTA retirement base rate by tour level — needed to size the settlement-divergence
  EV distortion (no citable figure found this sweep; do not assume one).
- Does the 2025 ATP FLB reversal (favourites -3.1%) persist into 2026, or was it the
  one-off Pinnacle's analysis suggests? Re-check after 2026 season data.
- Whether Simon (2024)'s MLB non-monotonicity generalizes to soccer close formation —
  our idea-2 measurement answers this on our own data.
