# Research Memory — Ultimate Betting Strategy Synthesis (2026-07-10)

Living memory for the composite-strategy research task. Categorized findings; every
entry cites its evidence source. Entries marked **[PRIOR]** come from this repo's
already-file-inspected research (docs/research/, .claude/memory/decisions.md) and are
settled — do not re-evaluate. Entries marked **[NEW]** come from this session's sweep.

## Doctrine (evidence-backed frame everything is judged against)

- **[PRIOR]** No public repo demonstrates a market-beating outcome-prediction model
  validated by held-out CLV. ML winner/ATS predictors are the wrong shape; the durable
  edge shape is **sharp-vs-soft line shopping + CLV measurement** (multiple sweeps:
  ev-strategy-repo-research-2026-06-24.md, 2026-07-10-github-strategy-sweep.md).
- **[PRIOR]** THE data gate: a market is only backtestable if a **free sharp anchor
  (Pinnacle/exchange) AND closing line** exist. Football clears it
  (football-data.co.uk PSH/PSCH); NBA/NFL/tennis do not on any free source (proven by
  fetching: decisions.md 2026-06-16/06-21).
- **[PRIOR]** Held-out validated edge (football): edge≥3% vs devigged sharp anchor →
  n=62 ROI +22.4%, incremental CLV +0.107 (>2SE) on held-out seasons; thr=0 baseline
  −1.6% — the edge is all in the selection gate (decisions.md 2026-06-19).

## Probability / devig models

- **[PRIOR] penaltyblog (MIT)** — Dixon-Coles + 7 devig methods, matches reference
  values to 1e-8; the adopted football pricing engine. Deep-read
  penaltyblog-deep-read-2026-06-18.md.
- **[PRIOR] goto_conversion (MIT, 112★)** — favourite-longshot-aware devig; SHIPPED
  as 8th devig after single-shot walk-forward + ADR-0021 (memory 2026-07-10).
- **[PRIOR] mberk/shin** — Shin devig cross-check reference only.
- **[PRIOR] cjbrant/probability-calibration-pipeline (NO LICENSE)** — Beta-cal/BBQ
  calibration of devigged probs = the one genuine public idea; clean-room only.
  Investigated: calibration haircut NOT warranted on our data (2026-06-24).
- **[PRIOR]** Dixon-Coles > raw Poisson for football; skellam/raw-Poisson repos are
  subsets of penaltyblog.

## Bankroll / Kelly

- **[PRIOR]** Own fractional-Kelly ladder + exposure caps validated; staking-variant
  study found no variant beats proportional fractional Kelly (optimization-round-3).
- **[PRIOR] superkush06/kelly-bet** — reference for simultaneous/portfolio Kelly.
- **[PRIOR] deltaray-io/kelly-criterion, pybettor, ianalloway/kelly-js** — trivial or
  reject.
- **[NEW]** keeks + broader Kelly-library sweep: see sweep results below (pending).

## Odds data / ingestion

- **[PRIOR]** OddsHarvester (OddsPortal scrape), Pinnacle Arcadia guest JSON (the only
  free live sharp anchor; GET-only client shipped), Betfair Exchange read-only +
  BSP archive, The Odds API (key rotation), oddschecker via proxy pool. ESPN public
  scoreboard for free settlement scores (adopt-pattern from sportsdataverse-py, MIT).
- **[PRIOR]** No free historical sharp open+close exists beyond football — every
  vendor dataset checked is consensus/soft or paid teaser (2026-06-21 dataset scan).

## Backtesting / validation

- **[PRIOR]** Walk-forward only; no closing odds in features; spent-holdout ledger
  (2425+2526 consumed; only season 2627 fresh); pre-registered single-shot protocol
  for any new idea. betcode-org/flumine (MIT) PIQ queue-aware fill model =
  adopt-pattern for realistic exchange fills.
- **[PRIOR]** Leakage patterns catalogued from rejected repos: shuffled k-fold,
  closing-odds-in-features, SMOTE-before-calibration, outcome-ordered rows
  (ProphitBet, tennis winner-first CSVs).

## CLV / tracking

- **[PRIOR]** CLV = log-ratio vs devigged sharp close; trusted-close provenance
  (snapshot-sourced + named-sharp) vs fallback split shipped (ADR-0017).
  neeljshah/clvtrack = vaporware (22-byte stub).
- **[PRIOR]** Negative CLV episodes were SELECTION problems (odds>4.0 tail,
  consensus-anchored premium), fixed by gates, not math (memory 2026-07-07).

## Matching / infrastructure

- **[PRIOR]** glass_onion (BSD-3) deterministic event join + soccerdata (Apache-2.0)
  alias pattern + reep (CC0) alias data → the cross-source matcher. Fuzzy matching
  forbidden (wrong-game risk proven).

## Web / literature knowledge

- **[PRIOR]** Hegarty & Whelan 2024/2025 (Int. J. Forecasting / Rev. Behav. Finance):
  soccer 1X2 odds systematically biased (favourite-longshot); Asian Handicap odds on
  the same fixtures are NOT — AH-derived fair prob is the better anchor in the tails.
- **[PRIOR]** Simon 2024 (Management Science 70(12)): sportsbook forecasts do NOT
  improve monotonically to the close — time-to-kickoff segments carry stale windows;
  motivates CLV segmentation by mint-timing (we pre-registered h2h>24h neg-CLV).
- **[PRIOR]** Favourite-longshot bias = the most robust anomaly (Whelan Economica
  2024; reproduced on Kalshi, Bürgi/Deng/Whelan 2026). Supports hard odds ceilings.
- **[PRIOR]** Tennis: models at best MATCH the market (MDPI Analytics 2026 benchmark:
  tuned Elo 65.9% vs best hybrid 67.5%; bookmaker Brier beats/ties best RF —
  Dryja 2024; Kovalchik 2016). Don't build outcome models; line-shop instead.
- **[PRIOR]** Kaunitz-style consensus deviation ≈ logit-pool anchor — tested, kept OFF.
  Steam/momentum: REJECTED at n≈68.5k (AUC 0.466, drift mean-reverts).
- **[PRIOR]** arXiv 2604.17194 (2026): booksum (overround) does not correlate with
  odds accuracy under Shin variants — tempers Shin-tail-devig expectations.
- **[PRIOR]** Tennis retirement settlement rules differ per book (bet365 void vs
  one-set rules) — cross-book EV mis-statement risk for line shopping.
- **[NEW]** This session's web sweep (papers/forums/pitfalls): pending below.

## Session sweep results (2026-07-10, this task — 6-agent workflow, all file-inspected unless noted)

### Kelly/bankroll lane
- **keeks (wdm0006/keeks, MIT, 6★, pushed 2026-07-09)** — REFERENCE-ONLY. Widest
  strategy zoo (Kelly/fractional/CPPI/OptimalF/Merton + MC simulators + RuinError
  bankroll harness), but betting-hostile default `min_probability=0.5` zeroes every
  +EV underdog; dead statement in core kelly.py; odds frozen at construction; no
  simultaneous Kelly/uncertainty shrinkage. Value = the test-harness SHAPE.
- **KellyPortfolio (thk3421-models, MIT, 96★)** — ADOPT-PATTERN. Simultaneous Kelly
  as QP (max F'M − ½F'CF) with box caps, Ledoit-Wolf shrinkage, fraction applied
  AFTER optimization, kelly_implied() inversion diagnostic. CLI not library.
- **cvxgrp/kelly_code (GPL-3, Boyd et al. RCK)** — ADAPT-MATH (re-derive from paper;
  GPL + dead py2 cvxpy). The only principled drawdown-probability-constrained Kelly:
  logsumexp(log π − λ log r·b) ≤ 0, λ = log β/log α; dominates fractional-Kelly
  frontier. NOTE: math-to-rebuild, no adoptable code exists publicly.
- **Bayesian-Kelly (sergeisukhovmkt, MIT, 5★)** — ADAPT-MATH: multiplicative
  uncertainty shrink φ = n_eff/(n_eff+κ) on Kelly stake; cleanest code in lane but
  shrinks on sample-size of a shared win-rate (category error for per-pick model p).
- Lesson: 'kelly criterion' namespace now dominated by Polymarket/Kalshi AUTOBET
  bots; stars anti-correlate with usability. No wholesale-adoptable staking lib
  exists; penaltyblog remains best simple per-bet Kelly.

### Value/arb lane
- **edge-scanner (DeliciousPipe1326, MIT, 42★, 2025-12)** — ADAPT-MATH. Cleanest
  public doctrine pipeline (Pinnacle→exchange sharp priority w/ commission netting →
  devig → edge → ¼-Kelly). Unique piece: MIDDLES module w/ NFL key-number table
  (3:15%, 7:9%, 10:6%) + gap detection + middle-EV. Caveats: multiplicative devig
  only (re-base on power/Shin before use), hand-set constants, no backtest/CLV.
- **oddsapi_ev (roman-smith, MIT, 20★, 2022 — stale, pattern-only via unique
  capability)** — dual-reference EV (vs consensus AND vs Pinnacle side-by-side) +
  microstructure pick-quality gates: max market width, max vig%, min books quoting.
- **pretrehr/Sports-betting (MIT, 525★, dormant 2023)** — ADAPT-MATH: deepest
  matched-betting/promo math (freebet conversion, refund-if-lose, boosts). FR books.
- **AUTOBET-dense lane**: ≥6 of 24 shortlisted place bets/store credentials
  (danielcardeenas/surebet placeArb(), ratsam3474/autoarbitrage plaintext passwords).
  HintikkaKimmo/surebet (84★) = VAPORWARE (empty arb module — stars≠code).
- Lesson: nothing found this sweep beats devig-sharp→compare-soft; arb math is
  commodity (sum 1/best_odds < 1); hard problems (event identity, staleness) are
  where public repos are weakest. No public scanner tracks CLV or settles picks.

### Data/backtest/CLV lane
- **R1ch1k/betting-backtester (MIT, 0★, 2026-04)** — ADOPT-PATTERN (agent-reported:
  ~800 tests, mypy strict — verify before adoption). 3 liftable patterns: cohort
  test-windows (odds AND settlement inside window, boundary exclusions counted),
  match-level bootstrap for yield CIs, chained bankroll across walk-forward windows.
  Honest README (its own xG example loses money). 1X2-only, no CLV metric.
- **mberk/betfairutil (MIT, 37★)** — ADOPT. get_last_pre_event_market_book (exchange
  close for CLV), get_bsp_/get_winners_from_prices_file, pre-event volume (liquidity
  floor), book %. Same author as adopted shin lib. Transitive dep betfairlightweight
  has order endpoints → safety_audit whitelist needed.
- **mzaja/betfair-database (MIT, 12★)** — ADOPT-PATTERN: SQLite index-in-place over
  historic market-file folders.
- **PySBR (80★)** — REJECT: SBR GraphQL endpoint live-probed 2026-07-10 → 301/HTML,
  dead. Confirms: free sharp open+close remains FOOTBALL-ONLY.
- **aqsmith02/paper-betting-tracker (NO LICENSE, pushed today)** — idea-only:
  NullHypothesisSimulator — MC the actual bet slate under −5%-EV null, test observed
  P&L vs that distribution. Complements CLV when trusted-close coverage thin.
- Lesson: 'CLV tracker' category = zero-star unlicensed AI boilerplate; our in-house
  trusted-close/provenance CLV layer has NO public peer (none found in sweep).

### Models lane
- **bpl-next (anguswilliams91, MIT, numpyro)** — ADAPT-MATH: only maintained Python
  Bayesian DC alternative (posterior uncertainty, dynamic/time-varying variants);
  Turing-Institute-backed via AIrsenal. Heavy jax dep; no closing-line eval.
- **footBayes (GPL-2, R/Stan, 57★)** — REFERENCE: richest model zoo (bivariate
  Poisson, Skellam, zero/diag-inflated, dynamic) as authoritative math specs.
- **openskill.py (MIT, 359★, JOSS-reviewed)** — ADOPT if rating features ever needed
  (patent-clean TrueSkill-class, draws/margins native).
- **BeatTheBookie (Lisandro79, GPL-3, 651★)** — ADOPT-PATTERN: the Kaunitz 2017 repo;
  ONLY surveyed repo evaluating vs closing odds; ~479k-match per-book closing-odds
  dump (avg+max+bookie) on Dropbox = free consensus-deviation/CLV replay corpus
  (ends ~2015). Real-money +8.5%/5mo then account-limited.
- **Football_Prediction_Project (299★, MIT)** — REJECT, verified leakage in code:
  shuffle=True StratifiedKFold over temporal fixtures. Negative exemplar.
- Lesson: ZERO public model repos clear the vs-closing-line bar; 2026 WC slop wave
  pollutes search; leakage is the norm (verified, not suspected).

### Web/literature lane (all sourced, see final doc for URLs)
- Kaunitz 2017: consensus-deviation edge real (+3.5% sim, +8.5% real-money 5mo) but
  killed by account limiting in months — venue, not signal, is the bottleneck.
  Replication critique: stale max-odds inflate backtest ROI.
- MacLean/Thorp/Ziemba 2010: fractional Kelly ¼–½ is the professional standard;
  2× Kelly → growth zero; edge overestimation silently produces that multiplier.
- Hubáček 2019 (IJF): decorrelate model from bookmaker odds — accuracy correlated
  with market = no bets; profit needs disagreement + being right.
- Wunderlich & Memmert 2020 (IJF): backtest ROI is a low-power selection criterion;
  select on calibration/CLV, never ROI leaderboards.
- Bailey/López de Prado PBO: iterating on a holdout spends it (matches our
  pre-registration doctrine).
- Snowberg & Wolfers 2010: FLB robust, misperception-driven → odds ceilings aligned.
- Pinnacle/Buchdahl: CLV vs devigged sharp close = skill measure (the book itself
  says so); taken/close ratio predicts realized profit.
- WaPo 2022 + iGaming: US books limit ~0.5% of accounts = the sharp ones; durable
  venues = exchanges (commission, no limiting), Pinnacle-class books, brokers.
- Line shopping worth ~2-3% ROI (arithmetic, undisputed mechanism).
- r/algobetting: variance underestimation is the top killer (−12% over 100 bets is
  normal at 1.5% edge); realistic elite ROI = low single digits on turnover;
  double-digit backtest ROI = red flag.

## Deep verification + knowledge round (2026-07-10, second workflow — 7 agents)

Full report: docs/research/2026-07-10-deep-verification-and-knowledge.md.
Scorecard: 17 claims re-checked → 12 CONFIRMED, 4 PARTIAL (corrected in the
synthesis doc), 1 UNVERIFIABLE. Corrections:
- R1ch1k/betting-backtester: 559 tests (not ~800), NO CI, no mypy-strict config —
  patterns confirmed verbatim; strictly a pattern donor.
- BeatTheBookie corpus: 880,494 matches (479,440 = paper subset); ALL Dropbox links
  DEAD 2026-07-10 (Dropbox serves HTTP-200 error pages — check content, not status);
  GDrive mirror sign-in-gated. Corpus effectively unavailable.
- Kaunitz: +3.5% over 56,435 bets (44.4% was ACCURACY — source of the "~44k" error);
  +9.9%/6,994 on continuous odds; α bias correction 0.034/0.057/0.037 (H/D/A);
  authors concede limits created uncorrected sampling bias; never journal-published;
  own-repo critiques: feed home/away switches (#1), Betfair contaminating consensus (#6).
- Limiting prevalence: "well under 1%" (operators→MGC), BetMGM ~1%, MGC study 0.64%
  (58% of limited cut to 1–24% of default stake); no sourced 0.5% exists.

New load-bearing knowledge (sourced in the report):
- Devig ordering (replicated): power ≥ Shin ≫ multiplicative (Clarke/Kovalchik/
  Ingram 2017); Buchdahl's data best fit by odds-ratio; additive can go NEGATIVE;
  devigged log-CLV = the EV estimate; never mix devig methods bet-vs-close.
- Closing line = best cheap benchmark, NOT an oracle: Simon 2024 (non-monotone),
  Angelini & De Angelis 2019 (per-book efficiency heterogeneity).
- Kelly: P(ever reach fraction x of bankroll) = x^(2/c−1) — full Kelly halves w.p.
  1/2; half-Kelly 12.5%; quarter ~0.8%. Baker & McHale 2013: fractional Kelly IS
  shrinkage under estimation error. Uhrín et al. 2021: raw plug-in Kelly on real
  data → 100% ruin rate vs ~10× median for fractional/robust. RCK constraint
  E[(r·b)^−λ]≤1, λ=logβ/logα is the certified drawdown bound.
- Backtest: E[max SR|null] ~ √(2 ln N) — LOG EVERY TRIAL or deflation is impossible;
  sample-size at even odds: 2% edge → ~19.6k bets, 3% → ~8.7k, 4% → ~4.9k for
  significance; CLV cuts required n by 2-3 orders of magnitude; Dwork 2015 reusable
  holdout = the theory behind single-shot pre-registration; Harvey-Liu haircut/BHY
  FDR for league×market scans.
- Venues 2026: limiting keys on BET-SHAPE not P&L (Fanatics: ~half of limited were
  net LOSERS); MA 48h individualized limit notices live 2026-06-01 (free diagnostic
  labels); Betfair Premium Charge → "Expert Fee" Jan 2025 (20%/40% above £25k/£100k
  rolling profit); Pinnacle winners-welcome current but 2025 reports of per-account
  price reaction; prediction markets now the largest no-limit US venue (Kalshi ~87%
  of $39.7B trailing volume = sports; CFTC NPRM 2026-06-10 would clear game-outcome
  contracts, comments close ~2026-07-25); Buchdahl's own consensus system
  "flatlined for 3 seasons" (Jun 2025) — consensus-proxy edges decay.

## Whole-internet sweep (2026-07-10, third workflow — 6 web lanes + 1 X lane)

Full report: docs/research/2026-07-10-whole-internet-research.md. Headlines:
- **Execution is the moat** — every documented pro (Spanky/Walters/Bloom/Benham/
  Voulgaris) built beard/broker networks; limit cascade quantified: MGM 2wk, FanDuel
  4wk, Caesars 2mo vs Pinnacle-class 6+mo; Fanatics on record: detection = bet-shape
  not P&L (~half of limited were net losers); MA public comment names CLV as trigger.
- **Sharp-anchor architecture independently converged on** by Kaggle 2026 winners
  (3rd place = 90% market weight; 1st's top regret = no market odds), Circles Off
  taxonomy (origination = syndicate-scale; top-down = realistic solo path), and
  Unabated's product (devigged per-sport weighted sharp blend).
- **CLV domain-of-validity doctrine**: valid only with a real market-maker, devigged,
  move-timing-aware; "CLV doesn't mean anything in props" (Andrews). RebelBetting
  373,654-bet month: realized yield ≈ 0.8× CLV (+3.3%→+2.7%) — public calibration
  factor for our CLV→ROI mapping.
- **Fresh academia (Whelan/UCD corpus + 2025-26 arXiv)**: Betfair makers +0.6…+2.0%
  vs takers −2.2…−2.5% (~3M bets) — never cross the spread, esp. longshots; Shin-z
  measures margin NOT insiders (2 independent results); ALL FLB devigs underestimate
  1X2 draws (QC probe idea); in-play absorbs goals ≤2min, no price-leak before goals;
  Polymarket NBA top-of-book HFT-efficient, combinatorial mispricings ~2/game;
  Kalshi FLB reproduced (low-priced contracts big losers).
- **X lane**: Pinnacle killed PUBLIC real-time API 2025-07-23 (Buchdahl; Pinnacle
  confirmed) → aggregator "Pinnacle" prices delayed; our own Arcadia capture posture
  is correct. BettingIsCool publicly called out betstamp for non-devigged CLV.
  Named CLV-skeptic debate (Shipper/Pads/Kirk); Andrews: "the golden goose is EV
  without CLV" (untracked edge = unlimited account). Polymarket officially recruits
  sharps ("No limit. No bans." Jan 2026); Peabody: taker fees bad → make markets.
  US OBBBA 90% loss-deduction = structural pro headwind. NFL/MLB/NBA prop
  limits tightening post-scandals (menu shrinking at exploitable end).
- **Edges persist 2025-26** per practitioners: prop/niche origination, live (BetBurger
  live tier = 3.5× prematch price), cross-venue prediction-market divergence,
  maker-side exchange, speed-to-news. Died: naive scanner EV (commoditized), Wong
  teaser (repriced), novelty props, Betfair liquidity (Premium Charge→Expert Fee
  20/40% over £25k/£100k), free Pinnacle API.
- **Anchor-quality heuristic (arbusers)**: where Pinnacle's own limit is unusually
  low, "edge vs Pinnacle" is usually fake; early lower-league Pinnacle lines are NOT
  sharp — time-gate them. Broker-tier Pinnacle (PS3838 white-labels) = half limits.
- **Books canon distilled**: Buchdahl (t-test/MC record simulation; wisdom-of-crowd),
  Miller/Davidow (synthetic hold; derivative-seam angles; break-even camouflage
  bets on "unbeatable" products for account longevity), Walters (1-3% edge-scaled
  stakes, HFA≈3pts, injury≈1pt), Wong (key numbers 3/7; classic teaser now repriced
  dead), Poundstone/Samuelson (full-Kelly refuted in practice), Mack (Bayesian
  origination textbooks), Buchalter "Theory of Everything" (fractional Kelly +
  regression-to-market + CLV as one uncertainty story).
- **Kaggle lessons**: shallow models + expanded windows win; isotonic calibration
  worth ~2% Brier (quantified); leaderboards gamed via champion-overrides — mine
  base models, ignore headline scores; Buchalter bet-volume smoke detector.
- **Scam forensics**: Blogabet odds-delay exploit (platform-confirmed); Tipstrr
  soft-vs-Pinnacle ROI flip (+25%→−18.5%) = template for auditing any tipster record.

### Critic corrections applied
- CLV tracking + risk-controls = in-house/math-to-rebuild; no public components.
- odds-api.net = uninspected bookmark only. oddsapi_ev = pattern-only (stale).
- edge-scanner middles must be re-based on power/Shin devig (multiplicative-never).
- betfair-historical downloader needs full Betfair creds (no read-only scope) —
  conflicts with read-only rule; pattern-only.
- Universal negatives softened to "none found in this sweep".

### Verify-targets from direct Tavily cross-check (inline, 2026-07-10)

Surfaced via github.com/topics through Tavily — metadata only, NOT yet inspected:
- pretrehr/Sports-betting — multi-bookmaker (FR books + Pinnacle/Betfair) arb/decision toolkit
- personal-coding/Live-Sports-Arbitrage-Bet-Finder — live arb across FanDuel/DK/WH; autobet-risk check needed
- unnamed "python analytics sqlite clv sports-betting kelly-criterion" topic repo — possible CLV tracker (lane C target)
- odds-api/odds-api — OpenAPI odds API w/ MCP, arbitrage + positive-EV endpoints
- SharpAPI TS client — real-time odds incl. Pinnacle, arbitrage/EV tagging
