# The Ultimate Betting Strategy — A Synthesis of the Best Existing Components

**Date:** 2026-07-10 · **Method:** multi-agent GitHub MCP/API sweep (6 agents, ~50 repos
file-inspected this session on top of ~80 settled in prior file-inspected sweeps) +
Tavily/exa web research (papers, practitioner sources, forums). Living notes:
`research_memory.md`. Nothing here is invented: every component is an existing,
inspected repo, library, dataset, or peer-reviewed method, cited inline.

**Nothing in this document presents betting as guaranteed profit. This is a
decision-support synthesis; the system it describes never places bets.**

---

## 1. Executive Summary

**What the research found:** after file-inspecting ~130 repositories across two
research campaigns and cross-checking the academic and practitioner literature, one
strategy shape stands alone as publicly validated with real money: **sharp-vs-soft
line shopping** — take the sharp market's (Pinnacle/exchange) devigged price as fair,
find soft bookmakers quoting materially better odds, bet the discrepancy, and measure
skill by **Closing Line Value (CLV)** against the devigged sharp close. It is the
method of the only real-money-verified public result (Kaunitz et al. 2017: +3.5% ROI
over 56,435 simulated bets, +8.5% over 5 live months — ended by account limiting, not by
the math failing), it is what the sharp bookmaker itself endorses as the skill measure
(Pinnacle/Buchdahl), and it is the shape every credible public +EV repo converges on.
**Zero public outcome-prediction ML repos clear the held-out vs-closing-line bar** —
the top-starred recent one (299★) has shuffled-CV leakage verified in its code.

**The synthesized ultimate strategy** is therefore a composite of the best existing
pieces, each selected on inspected evidence:

- **Fair price:** devigged sharp anchor — `penaltyblog` (MIT; 7 devig methods matching
  reference values to 1e-8) + `goto_conversion` (MIT; favourite-longshot-aware devig)
  + Dixon-Coles as a sanity prior, with `bpl-next` (MIT, numpyro) as the Bayesian
  cross-check.
- **Edge detection:** best-price line shopping vs the devigged anchor with
  microstructure quality gates (market width / vig% / min-books — `oddsapi_ev`
  pattern) and an odds ceiling (favourite-longshot bias, Snowberg & Wolfers 2010).
- **Sizing:** fractional Kelly ¼–½ (MacLean/Thorp/Ziemba 2010) + uncertainty
  shrinkage (Bayesian-Kelly pattern) + simultaneous-bet QP (KellyPortfolio pattern)
  + optional risk-constrained Kelly (Busseti/Ryu/Boyd RCK, re-derived).
- **Backtesting:** walk-forward only, vig-in, decision-time odds, match-level
  bootstrap CIs, cohort windows, chained bankroll (`R1ch1k/betting-backtester`
  patterns + Bailey et al. PBO discipline + Wunderlich & Memmert model selection).
- **Tracking:** CLV vs devigged sharp close with close-provenance trust rules —
  **no public component exists for this; it must be in-house** (confirmed twice
  independently this sweep) — plus a null-hypothesis Monte Carlo baseline
  (`paper-betting-tracker` idea).
- **Data:** football-data.co.uk (free Pinnacle open+close), Pinnacle guest JSON
  (free live sharp anchor), Betfair historic/BSP via `betfairutil` +
  `betfair-database` (both MIT), The Odds API. (BeatTheBookie's closing-odds
  corpus was re-checked 2026-07-10: hosting links are dead — see §2.1.)

**The most load-bearing realism finding:** the binding constraint on this strategy is
not the signal but the venue — soft books limit winning accounts within months
(0.64% of Massachusetts accounts per the MGC study; operators say "well under 1%" —
i.e. precisely the sharp ones — Washington Post 2022, MGC 2025, Kaunitz aftermath).
Realistic sustained ROI is low single digits on turnover; double-digit backtest ROI is
a red flag for stale odds or leakage (r/algobetting consensus + Zimmermann 2024).

---

## 2. Research Summary

### 2.1 Top GitHub findings this session (all file-inspected; verdicts per project gates)

| Repository | Category | Stars / activity | License | Verdict | The extractable piece |
|---|---|---|---|---|---|
| [wdm0006/keeks](https://github.com/wdm0006/keeks) | Kelly/bankroll lib | 6★, pushed 2026-07-09 | MIT | reference-only | Strategy-class + Monte-Carlo simulator + RuinError bankroll test-harness shape. **Trap found in code:** default `min_probability=0.5` silently zeroes every +EV underdog stake |
| [thk3421-models/KellyPortfolio](https://github.com/thk3421-models/KellyPortfolio) | Simultaneous Kelly | 96★, 2025-06 | MIT | adopt-pattern | Kelly as QP (max F′M − ½F′CF) with per-position caps, Ledoit-Wolf shrinkage, fraction applied **after** optimization, `kelly_implied()` inversion diagnostic |
| [cvxgrp/kelly_code](https://github.com/cvxgrp/kelly_code) | Drawdown-constrained Kelly | 29★, 2020 | GPL-3 | adapt-math | Risk-Constrained Kelly (Busseti/Ryu/Boyd 2016): `logsumexp(log π − λ log r·b) ≤ 0`, λ = log β/log α — bounds P(bankroll ever < α) ≤ β; dominates the fractional-Kelly frontier. Re-derive from paper (GPL + dead Py2 code) |
| [sergeisukhovmkt/Bayesian-Kelly…](https://github.com/sergeisukhovmkt/Bayesian-Kelly-Criterion-with-Parameter-Uncertainty) | Uncertainty-aware Kelly | 5★, 2026-06 | MIT | adapt-math | Multiplicative shrink φ = n_eff/(n_eff+κ) on the Kelly stake; exposes posterior/CI diagnostics alongside the stake |
| [DeliciousPipe1326/edge-scanner](https://github.com/DeliciousPipe1326/edge-scanner) | +EV / arb / middles scanner | 42★, 2025-12 | MIT | adapt-math | The only credible public **middles** implementation (NFL key-number table 3:15%, 7:9%, 10:6% + gap detection + middle-EV + stake split); independently confirms the sharp-priority (Pinnacle→exchange, commission-netted) pipeline. Devig is multiplicative-only — re-base on power/Shin before any use |
| [roman-smith/oddsapi_ev](https://github.com/roman-smith/oddsapi_ev) | +EV pipeline | 20★, 2022 (stale — pattern-only) | MIT | adopt-pattern | Dual-reference EV (vs consensus AND vs Pinnacle side-by-side) + pick-quality gates: max market width, max vig%, min books quoting the line |
| [pretrehr/Sports-betting](https://github.com/pretrehr/Sports-betting) | Matched-betting/promo math | 525★, dormant 2023 | MIT | adapt-math | Deepest public promo-EV math: closed-form optimal stakes for freebet conversion, refund-if-lose, odds boosts, combos |
| [R1ch1k/betting-backtester](https://github.com/R1ch1k/betting-backtester) | Backtesting framework | 0★, 2026-04 | MIT | adopt-pattern | Cohort test-windows (odds AND settlement inside window, boundary exclusions counted); match-level bootstrap yield CIs; chained bankroll across walk-forward windows; per-market commission netting. (Re-verified 2026-07-10: patterns confirmed verbatim in code; 559 test functions — not ~800 — and NO CI or mypy-strict config exists) |
| [mberk/betfairutil](https://github.com/mberk/betfairutil) | Exchange historic parsing | 37★, 2025-05 | MIT | adopt | `get_last_pre_event_market_book_from_prices_file()` = the exchange close for CLV; BSP, winners (settlement truth), pre-event traded volume (liquidity floor), book% (overround). Caveat: transitive dep `betfairlightweight` contains order endpoints → safety-audit whitelist |
| [mzaja/betfair-database](https://github.com/mzaja/betfair-database) | Historic data management | 12★, 2025-10 | MIT | adopt-pattern | SQLite index-in-place over folders of Betfair market files; SQL select returns file paths, no data duplication |
| [Lisandro79/BeatTheBookie](https://github.com/Lisandro79/BeatTheBookie) | Strategy + dataset (Kaunitz 2017) | 651★, 2021 | GPL-3 | adopt-pattern (strategy) | The consensus-deviation strategy validated in a paper + real money. Dataset: README documents 880,494 matches of per-book closing odds 2000–2015 (the paper analyzed a 479,440-game subset), but re-verified 2026-07-10: **all Dropbox links dead** (HTTP-200 error pages) and the Google Drive mirror is sign-in-gated — corpus effectively unavailable |
| [anguswilliams91/bpl-next](https://github.com/anguswilliams91/bpl-next) | Bayesian Dixon-Coles | 5★, 2026-06 | MIT | adapt-math | Only maintained Python Bayesian DC (numpyro NUTS; dynamic/time-varying + covariate variants); posterior uncertainty for free; Turing-Institute-backed via AIrsenal |
| [vivekjoshy/openskill.py](https://github.com/vivekjoshy/openskill.py) | Rating library | 359★, 2026-05 | MIT | adopt (if ratings needed) | Patent-clean Weng-Lin Bayesian ratings (5 models), draws/margins native, JOSS-reviewed |
| [LeoEgidi/footBayes](https://github.com/LeoEgidi/footBayes) | Model zoo (R/Stan) | 57★, 2026-06 | GPL-2 | reference-only | Authoritative Stan specs for bivariate Poisson, Skellam, zero/diag-inflated, dynamic variants — math specs to re-implement |
| [aqsmith02/paper-betting-tracker](https://github.com/aqsmith02/paper-betting-tracker) | Honest tracking | 6★, pushed 2026-07-10 | none | reference-only (idea) | `NullHypothesisSimulator`: Monte-Carlo your actual slate under a −5%-EV null; test observed P&L against that distribution |

**Named rejects worth recording** (verified in code, not suspected):
[mhaythornthwaite/Football_Prediction_Project](https://github.com/mhaythornthwaite/Football_Prediction_Project)
(299★, MIT) — `shuffle=True` StratifiedKFold + random `train_test_split` over
temporally-ordered fixtures = leakage; [HintikkaKimmo/surebet](https://github.com/HintikkaKimmo/surebet)
(84★) — the arb module is literally one comment line, vaporware;
[PySBR](https://github.com/jemorriso/PySBR) (80★) — SBR GraphQL endpoint live-probed
2026-07-10 → dead; [danielcardeenas/surebet](https://github.com/danielcardeenas/surebet),
ratsam3474/autoarbitrage + ≥4 more — **confirmed autobet** (order placement /
plaintext bookmaker credentials): ideas-only, code never liftable.

### 2.2 Prior settled components (file-inspected in earlier campaigns, reused here)

| Component | Source | Verdict (evidence) |
|---|---|---|
| Dixon-Coles + 7 devig methods + Kelly | [penaltyblog](https://github.com/martineastwood/penaltyblog) (MIT) | adopted; devig matches reference values to 1e-8 |
| Favourite-longshot-aware devig | [goto_conversion](https://github.com/gotoConversion/goto_conversion) (MIT, 112★) | adopted after single-shot walk-forward validation |
| Realistic exchange fills | [betcode-org/flumine](https://github.com/betcode-org/flumine) (MIT) | adopt-pattern: PIQ queue-aware fill model (never the live-order stack) |
| Cross-source event matching | [glass_onion](https://github.com/USSoccerFederation/glass_onion) (BSD-3) + [soccerdata](https://github.com/probberechts/soccerdata) (Apache-2) + reep (CC0) | deterministic join + alias pattern + alias data; fuzzy matching forbidden (wrong-game risk proven) |
| Free live sharp anchor | Pinnacle guest JSON (`guest.api.arcadia.pinnacle.com`) | verified live, GET-only; the only free live Pinnacle source |
| Free sharp open+close (historical) | football-data.co.uk PSH/PSCH columns | the ONLY free sharp open+close, football-only (re-verified this sweep: SBR dead, all vendor datasets consensus/soft/paid) |
| Settlement scores | ESPN public scoreboard (pattern from sportsdataverse-py, MIT) | free, key-less, GET-only |
| Calibration of devigged probs | cjbrant/probability-calibration-pipeline (no license) | the one genuine public modeling idea; clean-room only |

### 2.3 Key non-GitHub sources

| Source | Type | Load-bearing claim |
|---|---|---|
| [Kaunitz, Zhong & Kreiner 2017](https://arxiv.org/abs/1710.02824) | paper (press-verified real money) | Consensus-deviation beats books: +3.5% over 56,435 bets (closing-odds sim; a continuous-odds sim gave +9.9% over 6,994) / +8.5% real over 265 bets in 5 months; accounts then limited. Critiques: stale max-odds, feed errors (home/away switched), exchange odds contaminating the consensus mean; authors concede limits created uncorrected sampling bias; never journal-published |
| [MacLean, Thorp & Ziemba 2010](https://www.tandfonline.com/doi/pdf/10.1080/14697688.2010.506108) | peer-reviewed | Fractional Kelly ¼–½ is the professional standard; 2× Kelly → growth zero — and edge overestimation silently produces that multiplier |
| [Hubáček, Šourek & Železný 2019](http://ida.felk.cvut.cz/zelezny/pubs/ijf.2019.pdf) | peer-reviewed (IJF) | Model-market correlation is detrimental; decorrelating from bookmaker odds beat accuracy-maximizing training |
| [Wunderlich & Memmert 2020](https://ideas.repec.org/a/eee/intfor/v36y2020i2p713-722.html) | peer-reviewed (IJF) | Backtest ROI is a low-power selection criterion; select on calibration/CLV |
| [Bailey, Borwein, López de Prado & Zhu](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) | peer-reviewed | Probability of Backtest Overfitting: every re-tune spends the holdout |
| [Snowberg & Wolfers 2010](https://www.nber.org/papers/w15923) | peer-reviewed, replicated | Favourite-longshot bias is robust and misperception-driven → odds ceilings justified |
| [Pinnacle: What is CLV](https://www.pinnacle.com/betting-resources/en/educational/what-is-closing-line-value-clv-in-sports-betting) + [Buchdahl closing-line skill test](https://www.pinnacle.com/betting-resources/en/betting-strategy/using-closing-line-to-test-betting-skill/7e6jwjm5ykejuwkq) | industry (the sharp book itself) | CLV vs devigged close is the skill measure; taken/close ratio predicts realized profit |
| [Washington Post 2022](https://www.washingtonpost.com/sports/2022/11/17/betting-limits-draft-kings-betmgm-caesars-circa) + iGaming Business + MGC study | investigative press + regulator data | Books limit "well under 1%" of accounts (operators to the Massachusetts commission; BetMGM ~1%; MGC study 0.64%) — conditional on sustained edge it is near-certain; durable venues = exchanges, sharp books, brokers |
| Hegarty & Whelan 2024/2025 (IJF / Rev. Behav. Fin., via [karlwhelan.com](https://www.karlwhelan.com/sports-betting-research)) | peer-reviewed | Soccer 1X2 is biased; Asian Handicap on the same fixtures is not — AH is the better fair anchor in the tails |
| [Simon 2024, Management Science 70(12)](https://aura.american.edu/articles/journal_contribution/Inefficient_Forecasts_at_the_Sportsbook_An_Analysis_of_Real-Time_Betting_Line_Movement/30546293) | peer-reviewed | Line accuracy does NOT improve monotonically to the close → time-to-kickoff segmentation matters |
| r/algobetting (variance/ROI threads, e.g. [this](https://www.reddit.com/r/algobetting/comments/1rrytx5/determiningdealing_with_variance)) | forum consensus | −12% over 100 bets is normal variance at a 1.5% edge; realistic elite ROI = low single digits on turnover |

---

## 3. Best Components Identified

**Probability / fair price.** Best-in-class is the *market itself*: a devigged sharp
price. Best devig stack (all inspected): `penaltyblog` (7 methods, power default) +
`goto_conversion` (FLB-aware) + Shin as cross-check (`mberk/shin`). Best model-side
prior: Dixon-Coles (`penaltyblog` MLE; `bpl-next` Bayesian cross-check; `footBayes`
Stan files as specs for richer variants). No public ML predictor clears the
closing-line bar — verified again this sweep.

**Bankroll / sizing.** No wholesale-adoptable library exists (the niche is genuinely
empty — confirmed by inspecting keeks, the category's only active package). The best
composite: `penaltyblog` per-bet Kelly → × fraction (¼–½, MacLean/Thorp/Ziemba) →
× uncertainty shrink φ = n_eff/(n_eff+κ) (Bayesian-Kelly pattern) → capped;
simultaneous bets via the KellyPortfolio QP pattern; drawdown control via re-derived
RCK (Boyd et al.) if principled ruin bounds are wanted.

**Odds data.** Free spine: OddsPortal scraping (OddsHarvester), Pinnacle guest JSON
(live sharp anchor), oddschecker via proxies, The Odds API (keyed fallback),
football-data.co.uk (historical sharp open+close, football only), Betfair historic
files (`betfairutil` + `betfair-database`, both MIT) for exchange close/BSP/liquidity.
BeatTheBookie's 880k-match dump WAS the best free multi-book closing-odds corpus,
but its hosting is dead as of 2026-07-10. Everything else checked is consensus-only,
soft-only, dead (SBR), or paid — fresh self-capture remains the only current source.

**Arbitrage / value logic.** Arb math is a commodity (`sum(1/best_odds) < 1`, stakes
∝ 1/odds — daankoning/ArbitrageFinder is the clean reference). The differentiators
that matter, per inspection: which fair reference (sharp > consensus; use both
side-by-side per `oddsapi_ev`), commission netting on exchange anchors
(edge-scanner), and microstructure quality gates (width / vig% / min-books). Middles:
edge-scanner's key-number module is the only credible public implementation — usable
only after re-basing its devig and fitting push probabilities from real margin data.

**Backtesting.** Best public framework is a 0-star repo (`R1ch1k/betting-backtester`)
— in this niche stars anti-correlate with rigor. Its cohort windows, match-level
bootstrap, and chained bankroll patterns + flumine's queue-aware fills + the PBO/
pre-registration discipline form the complete validation kit.

**Tracking / CLV.** **The one component with no public peer.** Every public "CLV
tracker" found is a stub, vaporware, or unlicensed AI boilerplate (checked across
two sweeps). Must be built: log-ratio CLV vs devigged sharp close, close-provenance
trust rules (snapshot-sourced + named-sharp vs consensus/fallback), settlement from
independent score feeds, plus the null-simulator idea for thin-coverage slates.

---

## 4. The Ultimate Strategy (the bound-together system)

**Doctrine (one sentence):** *Buy prices, not predictions — treat the devigged sharp
market as fair value, bet soft books only when they misprice against it beyond a
validated threshold, size with shrunk fractional Kelly, and let CLV against the
devigged sharp close be the sole arbiter of whether it is working.*

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. ODDS INGESTION (read-only, GET-only)                                │
│    soft books: OddsPortal/oddschecker scrape · The Odds API            │
│    sharp anchor: Pinnacle guest JSON · Betfair exchange (commission-   │
│    netted) — freshness + liquidity gated                               │
└──────────────┬─────────────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. EVENT IDENTITY (deterministic, never fuzzy)                         │
│    glass_onion join pattern + soccerdata alias tables + dedup-by-      │
│    nearest-kickoff; ambiguous → no match (wrong close corrupts CLV)    │
└──────────────┬─────────────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. FAIR PRICE  = devig(sharp anchor)                                   │
│    power devig default · goto_conversion + Shin in the bake-off        │
│    harness · Dixon-Coles (penaltyblog / bpl-next) as sanity prior only │
│    — model may veto, never generate, picks (Hubáček: agreement with    │
│    the market is worthless; the market IS the model here)              │
└──────────────┬─────────────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 4. SELECTION GATES (each threshold walk-forward validated)             │
│    edge = p_fair·odds_soft − 1 ≥ ~3% · odds ceiling ≈ 4.0 (FLB)        │
│    sharp-anchor-required · market width / vig% / min-books quality     │
│    gates · freshness window · league scope by data quality             │
│    (arbitrage & middles: detect + log as separate informational        │
│    streams; same math, no model needed)                                │
└──────────────┬─────────────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 5. SIZING (informational)                                              │
│    stake = cap( ¼–½ · kelly(p_fair, odds_net_commission) · φ )         │
│    φ = n_eff/(n_eff+κ) uncertainty shrink · simultaneous bets → QP     │
│    (KellyPortfolio pattern) · optional RCK ruin bound (Boyd)           │
│    per-bet + daily exposure caps                                       │
└──────────────┬─────────────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 6. ALERT + LOG (immutably, at decision time)                           │
│    odds taken, book, timestamp, anchor + provenance, edge, stake —     │
│    the audit trail IS the product                                      │
└──────────────┬─────────────────────────────────────────────────────────┘
               ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 7. SETTLEMENT + CLV (the feedback loop)                                │
│    close capture (Pinnacle/exchange; betfairutil last-pre-event book)  │
│    CLV = log(odds_taken / odds_close_devigged), trusted only when      │
│    snapshot-sourced + named-sharp · scores from independent feed       │
│    (ESPN pattern) · ROI/W-L secondary; CLV primary                     │
│    + null-simulator baseline when trusted-close coverage is thin       │
└──────────────┬─────────────────────────────────────────────────────────┘
               ▼
        gates 4–5 re-validated ONLY via pre-registered single-shot
        tests on fresh data (PBO discipline) — never holdout re-tuning
```

Why each binding was chosen over alternatives, in one line each:

- **Sharp anchor over model** — the only real-money-validated public edge (Kaunitz)
  is market-relative; every model repo inspected fails the closing-line bar.
- **Power/goto/Shin devig over multiplicative** — multiplicative devig mis-handles
  the favourite-longshot tail that FLB research says is exactly where margin hides.
- **Deterministic matching over fuzzy** — a wrong-game close silently corrupts CLV,
  the system's only truth signal; proven failure mode in prior evaluations.
- **Fractional-shrunk Kelly over full Kelly or flat stakes** — settled math
  (MacLean/Thorp/Ziemba) plus the empirical fact that model edges are overestimated.
- **CLV over ROI as the metric** — ROI is too noisy to select on (Wunderlich &
  Memmert); CLV is the earliest statistically meaningful skill evidence, endorsed by
  the sharp book itself.
- **Pre-registration over iteration** — Bailey et al.: an iterated holdout is spent;
  this is the clinical-trial discipline betting backtests need.

---

## 5. Implementation Blueprint

Ordered so each step is independently useful. Where a step exists in this repo
already (this platform implements most of the architecture), it's marked ✅.

1. **Data spine** ✅ — OddsPortal/oddschecker scrapers + The Odds API client +
   Pinnacle guest JSON capture + Betfair read-only. New build: none needed; for a
   greenfield clone, start from OddsHarvester + a 50-line httpx Arcadia client.
2. **Historical corpus** — football-data.co.uk CSVs (PSH/PSCH sharp open+close);
   add BeatTheBookie's dump for multi-book consensus replay; index Betfair historic
   tars with `betfair-database`; parse closes/BSP/liquidity with `betfairutil`
   (pip-installable, MIT; whitelist its transitive `betfairlightweight` in the
   safety audit).
3. **Fair-price engine** ✅ — `pip install penaltyblog`; port `goto_conversion`
   (~20 lines, oracle-tested against upstream values); Shin cross-check.
4. **Selection gates** ✅ — edge ≥3%, odds ≤4.0, sharp-anchor-required. New: add the
   `oddsapi_ev` microstructure trio (width / vig% / min-books) as telemetry first,
   gate later if walk-forward supports it.
5. **Sizing** ✅ (fractional Kelly ladder + caps). New, in priority order:
   (a) uncertainty shrink φ keyed to settled-sample size per strategy/market
   (~10 lines); (b) simultaneous-bet QP re-implemented from the KellyPortfolio
   pattern in numpy/scipy (~40 lines); (c) RCK re-derived from Busseti/Ryu/Boyd
   only if principled ruin bounds are wanted.
6. **Backtester** ✅ (walk-forward + CLV harness). New: adopt the three
   `betting-backtester` patterns — cohort windows with counted boundary exclusions,
   match-level bootstrap CIs, chained bankroll — as upgrades to the existing
   harness.
7. **CLV tracking** ✅ — in-house by necessity (no public peer): log-ratio CLV,
   provenance-trusted closes, sharp-close stratum reporting. New: the −5%-EV
   null-hypothesis Monte Carlo as a companion report.
8. **Optional informational modules** — arb detector (commodity math), middles
   (edge-scanner pattern re-based on power devig + push probabilities fitted from
   own settled margins), promo-EV math (pretrehr patterns) — all shadow/log-only
   first.

**Safety invariants throughout:** GET-only integrations, no order code anywhere, a
CI grep (safety audit) that fails the build on any placement path, credentials only
in `.env`. Six of twenty-four arb repos inspected this sweep contained autobet code
or stored bookmaker passwords — inspection before any lift is not optional.

## 6. Backtesting & Validation Approach

Distilled from the strongest sources found (Bailey PBO; Wunderlich & Memmert;
Zimmermann 2024; Kaunitz replication critiques; betting-backtester; this project's
own protocol):

1. **Walk-forward only** — no shuffled CV ever (the 299★ negative exemplar has it).
2. **Decision-time odds only** — no closing odds in features; beware "historical max
   odds" that were never fillable (the main sim-vs-real gap in Kaunitz replications).
3. **Vig and commission in** — evaluate at net odds; exchange anchors
   commission-netted.
4. **Cohort windows** — an event belongs to a test window only if both its odds and
   settlement fall inside; count boundary exclusions.
5. **Match-level bootstrap CIs** on ROI and CLV — never trust point estimates;
   correlated bets within a match resample together.
6. **Select on calibration + incremental CLV vs the devigged sharp close** — ROI is a
   diagnostic, never the selection criterion.
7. **Pre-register + single-shot** — declare hypothesis, threshold, and metric in
   writing before touching fresh data; a re-tuned holdout is spent forever.
8. **Null baselines** — compare against random-bet ROI (≈ −vig) and the −5%-EV
   Monte Carlo distribution of the same slate.

## 7. Risk Management & Realism Notes

- **Account limiting is the strategy's true ceiling.** The one publicly verified
  edge died in months to ~$1 limits, not to math (Kaunitz; WaPo 2022). Soft-book
  accounts are depleting assets; durable venues are exchanges (commission but no
  limiting), Pinnacle-class books, and brokers. Plan the venue before scaling stakes.
- **Expect low single-digit ROI on turnover.** 3–6% sustained is elite; a
  double-digit backtest is evidence of stale odds or leakage until proven otherwise.
- **Variance will impersonate failure.** At a true 1.5% edge, −12% over 100 bets is
  unremarkable; judge on CLV samples with CIs, never short P&L windows.
- **Never full Kelly.** Edges are overestimated; nominal full Kelly is often
  effectively >1× where growth goes to zero. Fraction + shrink + caps.
- **Respect the favourite-longshot bias.** Margin concentrates in the tails; odds
  ceilings are evidence-aligned, not timidity.
- **Guard the truth signal.** CLV is only as honest as its close: provenance rules,
  deterministic event matching, and independent settlement scores are risk controls,
  not bookkeeping.
- **This system informs; a human decides and places (or doesn't).** No component in
  this synthesis places bets, and several inspected repos were rejected precisely
  because they do.

## 8. Sources & References

**GitHub (this session, file-inspected):** keeks · KellyPortfolio · cvxgrp/kelly_code ·
Bayesian-Kelly-Criterion-with-Parameter-Uncertainty · edge-scanner · oddsapi_ev ·
pretrehr/Sports-betting · daankoning/ArbitrageFinder · R1ch1k/betting-backtester ·
mberk/betfairutil · mzaja/betfair-database · tarb/betfair_data · PySBR (dead-probe) ·
aqsmith02/paper-betting-tracker · bpl-next · footBayes · openskill.py ·
sublee/glicko2 · regista · octopy · BeatTheBookie · Football_Prediction_Project
(negative exemplar) · HintikkaKimmo/surebet (vaporware exemplar) · autobet rejects
(danielcardeenas/surebet, ratsam3474/autoarbitrage et al.). Links in §2.1.

**GitHub (prior settled, reused):** penaltyblog · goto_conversion · mberk/shin ·
flumine · glass_onion · soccerdata · reep · sportsdataverse-py · OddsHarvester ·
cjbrant/probability-calibration-pipeline. Links in §2.2.

**Papers & industry:** Kaunitz et al. 2017 (arXiv:1710.02824) · MacLean/Thorp/Ziemba
2010 · Hubáček et al. 2019 · Wunderlich & Memmert 2020 · Bailey et al. (PBO) ·
Snowberg & Wolfers 2010 · Shin/Whelan (SJPE) · Hegarty & Whelan 2024/25 · Simon 2024
(Mgmt Sci) · Zimmermann et al. 2024 · Pinnacle/Buchdahl CLV resources · Washington
Post 2022 · r/algobetting threads. Links in §2.3.

**Working notes:** `research_memory.md` (categorized findings + lessons, maintained
through this session). Full lane outputs with per-repo quoted code evidence are in
the session workflow journal.
