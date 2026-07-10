# GitHub strategy sweep — implementable +EV ideas for the sharp-vs-soft platform (2026-07-10)

**Question.** What implementable, evidence-backed +EV strategy ideas exist in public
code/GitHub that we have NOT already tested, per sport (soccer live, basketball shadow,
tennis/NFL display-only), judged against the project doctrine: sharp-vs-soft line
shopping, trusted sharp-close CLV as the only success metric, walk-forward only,
picks-only (no autobet), free-first data.

**Method.** Local prior-art audit first (docs/research/, .claude/memory/decisions.md)
to exclude settled items; then GitHub REST API (repo metadata + raw file inspection —
the plugin GitHub MCP server was not exposed in this session and `gh` is not installed
here; unauthenticated `api.github.com`/`raw.githubusercontent.com` used read-only),
Tavily extract + WebSearch for non-GitHub sources. Every scored row below had files
opened (see "Files inspected"). Repos settled in prior sweeps (kyleskom, georgedouzas,
ProphitBet, NBA_Betting, nflverse verdicts, sbrscrape, cjbrant, NateDeMoro, mberk/shin,
hoopR, flumine, skelo, oliviersportsdata paid teaser, etc.) are **not** re-proposed —
see `docs/research/ev-strategy-repo-research-2026-06-24.md`,
`docs/research/multisport-modeling-2026-06-21.md`,
`docs/research/betting-repo-research.md`, `.claude/memory/decisions.md`.

**Nothing here is a guarantee of profit.** Every idea is a hypothesis to be
walk-forward tested against trusted sharp-close CLV; the prior sweeps' headline
("no public repo demonstrates a market-beating model validated by held-out CLV")
still stands after this sweep.

---

## Scored table

Evidence quality: 1 = anecdote/README claim … 5 = peer-reviewed or independently
replicated. Backtestability: 1 = needs data we don't have … 5 = runs on data we hold
today (BSP archive, football-data, Arcadia capture, warehouse, penaltyblog DC).

| # | Idea | Sport(s) | Source | Evid. | Backtest. | Integration risk | Verdict |
|---|------|----------|--------|-------|-----------|------------------|---------|
| 1 | **goto_conversion devig** — shrink all inverse odds by equal units of implied standard error (favourite-longshot-aware margin removal); also ships analytic Shin (`efficient_shin_conversion`, Štrumbelj-2014 closed form) | Soccer, NBA (evidence is basketball-flavoured) | [gotoConversion/goto_conversion](https://github.com/gotoConversion/goto_conversion) — MIT, 112★, pushed 2026-06-13; core inspected: `goto_conversion/__init__.py` (`efficient_shin_conversion` quoted below) | 3 — repeated Kaggle March-Madness medal evidence (Brier on outcomes, NOT CLV; self-reported but with linked third-party winner write-ups) | 5 — drops straight into the existing devig bake-off harness (`scripts/value_backtest.py`, `devig_comparison_2026-07-04.*`) | LOW — pure math, ~20-line clean-room port into `app/probabilities/`; oracle-test against upstream values like the mberk/shin pattern. CAUTION: same hypothesis family as the pre-registered Shin tail-devig (ADR-0019) — must join that single-shot protocol, never a spent-holdout re-tune | **USE-CANDIDATE** — the one public devig we don't ship, with real (if metric-mismatched) empirical wins, targeting exactly our documented longshot-band CLV weakness |
| 2 | **'bb' balanced-books devig** (Fingleton & Waldron 1999, Shin variant) and **'jsd'** (C. D. Long, Jensen-Shannon-distance devig) | Soccer 1X2 | [opisthokonta/implied](https://github.com/opisthokonta/implied) — GPL-3, 9★, pushed 2026-05; inspected `DESCRIPTION` + `R/implied_probabilities.R` (method docs enumerate basic/wpo/or/power/shin/bb/jsd/ooepc) | 2 — implemented and documented, but no published OOS comparison beating the standard set; our own bake-off found the 7 shipped methods within ~0.0002 RPS on 1X2 (decisions.md 2026-06-24 devig note) | 5 — same harness as #1 | LOW math / MEDIUM legal — GPL-3 code, so formulas-only clean-room from the cited papers | **WATCH** — add to the same pre-registered bake-off as #1 only if #1 shows signal; expected payoff small given prior within-noise result |
| 3 | **BSP-stream pre-off drift/velocity study** — use the full Betfair stream archive (message-level `ltp`/`batb` paths, not just the close) to test at n≈123k whether last-hours exchange drift/velocity predicts CLV of a T−60m price vs BSP (steam-family retest at scale; our live steam gate was tested OFF at n=1) | Soccer (1X2, OU), Basketball | Our own archive via `app/ingestion/betfair_bsp.py` (parses the documented stream `mcm` messages; header block lists fields). Public-code precedent: [betfair-datascientists/predictive-models](https://github.com/betfair-datascientists/predictive-models) (119★, no license, stale 2022) + the MIT tutorial site [betfair-datascientists.github.io](https://github.com/betfair-datascientists/betfair-datascientists.github.io) (pushed 2026-07-06) — patterns only | 3 — market-microstructure momentum in exchanges is studied but not settled; the decisive evidence would be our own archive | 5 — data fully in hand; extends the loader to emit a T−60m/T−6h/T−24h price path instead of only the close | MEDIUM — loader change + new study script; NO live gate shipped until walk-forward passes (steam gate stays OFF per gates-validated-keep-off-2026-06-28) | **USE-CANDIDATE (as a backtest study, not a feature)** — the only pick-time CLV-predictor idea whose deciding dataset we already hold |
| 4 | **nfelo-style market-regression NFL model** — Elo + QB adjustment, then explicitly regress the model line toward the market open/close (`regress_to_market` in `nfelo/Model/Nfelo.py`; grader vs market in `nfelo/Performance/NfeloGrader.py`) | NFL | [greerreNFL/nfelo](https://github.com/greerreNFL/nfelo) — 53★, **NO LICENSE**, actively rebuilt (pushed 2026-07-05); files inspected: README, tree, `Model/Nfelo.py`, `Performance/` listing | 3 — long-running public model with published weekly performance vs market on nfeloapp.com; not CLV-audited by us | 2 — blocked on data: we capture no NFL sharp close (Arcadia capture filters to soccer/basketball/tennis; decisions.md 2026-06-18) and no free NFL sharp close exists (row 5) | MEDIUM — no license ⇒ ideas-only clean-room (already the settled 2026-06-21 verdict; NEW fact: repo is active again and the market-regression + grader structure is now concrete and inspectable) | **WATCH → USE-CANDIDATE once Arcadia NFL close capture exists** — the credible path from display-only to shadow |
| 5 | **aussportsbetting.com free Excel (NFL/NBL/A-League/etc.) as a sharp open+close dataset** | NFL, basketball | [aussportsbetting.com/data](https://www.aussportsbetting.com/data/) (fetched via Tavily; direct fetch is Cloudflare-gated); bookmaker provenance per their own forum ([thread 10643](https://forum.aussportsbetting.com/forum/aussportsbetting-com/data-sets/10643-historical-nfl-odds-data)): odds were **bet365, forced switch to betr for 2025 (patchy)** | 3 — data exists and is free with open+close columns | 2 — carries open+close but from a single SOFT book → fails THE data gate (no sharp anchor, no sharp close); cannot measure incremental CLV vs a sharp close | LOW to ingest, but pointless for the gate | **REJECT for CLV validation / WATCH as results-and-consensus backfill only** — same failure class as SBR/nflverse consensus closes |
| 6 | **TML-Database — live-updated ATP results in the Sackmann schema** (per-year CSVs 1968-2026; `score` column carries retirement/walkover markers) | Tennis | [Tennismylife/TML-Database](https://github.com/Tennismylife/TML-Database) — 73★, **NO LICENSE**, pushed 2026-01-27; inspected `2025.csv` header (winner/loser, surface, `score`, serve stats). Relevant because the canonical **JeffSackmann/tennis_atp is gone from GitHub (404, re-confirmed this sweep; first noted in repo-sweep-2026-06-16)** | 2 — community-maintained results DB, no model claims | 4 — directly usable for settlement edge-case fixtures (RET/W-O grading tests) and as the results spine for the skelo display-only Elo screen | MEDIUM — no license + Sackmann-derived schema (original data was CC-BY-NC) ⇒ research/fixtures only, never redistributed | **USE-CANDIDATE (narrow)** — as settlement edge-case test data + display-screen input; tennis stays display-only (no free sharp close, re-confirmed) |
| 7 | **Kovalchik-style tennis Elo benchmark** (surface-weighted Elo beats rankings/point models pre-match) | Tennis | [skoval/deuce](https://github.com/skoval/deuce) — 94★, **NO LICENSE**, R, pushed 2025-08; paper: Kovalchik (2016), *J. Quant. Anal. Sports* "Searching for the GOAT of tennis win prediction" | 4 — peer-reviewed benchmark | 3 — reproducible on TML/Sackmann-schema data, but with no sharp close the output can only ever be a display screen | LOW-MEDIUM — no license ⇒ method-only; `mbhynes/skelo` (settled 2026-06-21) remains the bindable lib | **WATCH** — only if the operator wants a better tennis display screen; zero CLV upside until a tennis close exists |
| 8 | **NBA player-prop projection repos** (rolling-average projections from nba_api) | NBA props | [parlayparlor/nba-prop-prediction-model](https://github.com/parlayparlor/nba-prop-prediction-model) — MIT, 31★; README inspected: last-N-game averages of PTS/REB/AST vs prop lines; also surfaced (uninspected beyond metadata): kpundhir/Prop-Model (5★, no license), ejjlittle/ev-betting-model (5★, MIT) | 1 — no market validation anywhere in the class; naive averages | 1 — no free sharp prop closes exist at all (data gate fails harder than team markets) | HIGH relative to value | **REJECT** — props class fails both evidence and the data gate |
| 9 | **CLV-predictive pick filters from public code** (consensus-deviation à la Kaunitz 2017, reverse-line-movement repos, "beat the bookies" searches) | All | GitHub searches `closing line value betting`, `beat the bookies odds` returned Bee-Movie-script spam and 0-star stubs (this sweep); Kaunitz-style consensus deviation ≈ our logit-pool consensus anchor, already walk-forward tested OFF (gates-validated-keep-off-2026-06-28) | 1 (public code) | — | — | **REJECT (category exhausted publicly)** — nothing implementable found beyond what we already tested; #3 above is the remaining untested signal and it is powered by our own data |

Surfaced but NOT file-inspected (metadata only — do not adopt without inspection):
kpundhir/Prop-Model, ejjlittle/ev-betting-model, serve-and-volley/atp-world-tour-tennis-data
(219★, stale 2023, scraper of atptour.com), betfair-datascientists/predictive-models
(listed above as pattern precedent only), K3val17/deuce (Kalshi tennis engine, 0★ —
also flagged: prediction-market execution shape, ignore).

---

## Top-3 shortlist with concrete backtest designs

### 1. goto_conversion as the 8th devig (soccer 1X2/OU; NBA secondary)

- **What ships first:** nothing live. A clean-room `goto` method in
  `app/probabilities/` (TDD, oracle values from the upstream repo like the
  mberk/shin pattern), registered in the devig bake-off harness.
- **Dataset:** football-data.co.uk 2015/16–2023/24 (train folds) with the
  Betfair BSP stream archive close as the trusted sharp close; NBA on the
  warehouse + Arcadia capture forward sample.
- **Split:** walk-forward season folds for development; the **decisive test is
  single-shot on the fresh 2026 BSP tar**, amended into the ADR-0019
  pre-registration alongside Shin tail-devig (same hypothesis family:
  favourite-longshot tail handling). No spent-holdout (2425+2526) re-tuning.
- **Metric:** trusted sharp-close CLV of the value pipeline with goto as the
  fair-prob devig, vs the shipped power devig baseline; RPS as secondary.
- **Success bar:** CLV improvement over power with a bootstrap CI excluding 0 on
  the single-shot set, AND no degradation of the H2H odds-ceiling band metrics.
  Anything less → record and reject (prior devig deltas were ~0.0002 RPS noise).

### 2. BSP-stream pre-off drift study (steam-family retest at n≈123k)

- **What ships first:** nothing live — a study script + a loader extension so
  `parse_market_stream` can emit the best-back price path at T−24h/T−6h/T−60m,
  not only the pre-in-play close (all fields already documented in
  `app/ingestion/betfair_bsp.py`).
- **Dataset:** the full ~123k-market stream archive (soccer MATCH_ODDS + OU;
  basketball where present), through 2025-12.
- **Split:** walk-forward by month; features strictly from messages before the
  synthetic pick time (T−60m); label = log(price@T−60m / BSP) per runner —
  leakage-audited (kickoff = `marketTime`, in-play flip is the hard cutoff).
- **Metric:** does drift/velocity over the prior window predict the label's sign
  (AUC, and CLV uplift when used as a pick filter in replay against the
  warehouse's dual-provider snapshots)?
- **Success bar:** AUC ≥ 0.55 stable across ≥ 6 monthly folds AND positive
  filtered-CLV delta with CI excluding 0 in replay. Below that, the steam gate
  stays OFF (its live walk-forward verdict, n=1, is currently the ruling
  evidence) and the study is filed as closure.

### 3. NFL: Arcadia close capture first, then a clean-room nfelo-style shadow

- **What ships first:** extend `app/ingestion/pinnacle_arcadia.py` to include the
  NFL sport id in AVAILABLE GAMES (capture-only, isolated namespace, mints
  nothing) — that is the data-gate unblock; the aussportsbetting Excel is
  confirmed soft-book (bet365→betr) and cannot substitute.
- **Model (later, shadow-only per the shadow-first mandate):** clean-room
  regularized Elo + QB adjustment + explicit regression toward the devigged
  market open — the structure now concretely inspectable in
  `nfelo/Model/Nfelo.py::regress_to_market` and graded in
  `nfelo/Performance/NfeloGrader.py` (NO LICENSE ⇒ ideas only, zero code lift).
- **Dataset/split:** nflreadpy features (settled ADOPT, MIT) + our own accrued
  Arcadia NFL closes; walk-forward by week across the 2026 season; no closing
  odds in features.
- **Metric & bar:** shadow picks' trusted sharp-close CLV vs our Pinnacle NFL
  close, promotion per the shadow-first policy (n + CI, source agreement,
  freshness, coverage) — never small-sample ROI. Expect a full season of
  capture before any promotion question is even askable.

---

## Implications / recommended decision

1. Adopt the two USE-CANDIDATE studies (#1 devig, #2 BSP drift) as offline,
   pre-registered experiments; neither touches the live pipeline until its bar
   is met. #1 belongs in the ADR-0019 amendment; #2 is a new study ADR.
2. Approve the NFL Arcadia capture extension as groundwork (read-only, mints
   nothing) — it is the only path that ever converts NFL from display to shadow.
3. Take TML-Database retirement/walkover rows as settlement-test fixtures for
   tennis (display-only stance unchanged).
4. Close the props and public-CLV-filter categories as exhausted (this sweep +
   2026-06-24 sweep agree); revisit only if a free sharp prop/close source appears.

## Open questions

- Does the free/Basic Betfair stream tier consistently carry enough pre-off
  message density at T−24h for study #2's early windows (spot-check before
  committing the loader extension)?
- goto_conversion's evidence is Brier-on-outcomes; the transfer to CLV via
  fair-prob devigging is our hypothesis, not the author's claim.
- nfelo's rebuild is undocumented ("README will be updated"); re-inspect the
  `Optimizer/` and `Data/` layers before writing the clean-room spec.
- JeffSackmann/tennis_atp's removal leaves attribution/provenance of all
  Sackmann-schema mirrors (incl. TML) murkier — keep them out of anything
  redistributed.

## Files inspected (proof-of-inspection list)

- gotoConversion/goto_conversion: `README.md`, `goto_conversion/__init__.py`
  (quoted: `efficient_shin_conversion` — `listOfZ = ((beta - 1.0) * (listOfComplementPies ** 2.0 - beta)) / (beta * (listOfComplementPies ** 2.0 - 1.0))`), tree, LICENSE presence (MIT).
- opisthokonta/implied: `DESCRIPTION` (GPL-3), `R/implied_probabilities.R`
  (method roxygen block enumerating bb/jsd/ooepc with citations), tree.
- greerreNFL/nfelo: `README.md`, root + `nfelo/` + `Model/` + `Performance/` +
  `Data/` trees, `nfelo/Model/Nfelo.py` (quoted: `mr_regression_close = regress_to_market(... row['market_elo_dif_close'], row['nfelo_home_line_close'...`), `config.json`.
- Tennismylife/TML-Database: root tree, `2025.csv` header + first row.
- parlayparlor/nba-prop-prediction-model: `README.md` (rolling-average method).
- betfair-datascientists org: repo listing (metadata).
- skoval/deuce, mbhynes/skelo, JeffSackmann/tennis_atp (404): metadata via API.
- aussportsbetting.com: `/data/` page (Tavily), forum thread 10643 via WebSearch
  (bookmaker provenance quote: 2025 season "patchy due to a forced switch from
  bet365 to betr").
- Local prior art: `docs/research/ev-strategy-repo-research-2026-06-24.md`,
  `multisport-modeling-2026-06-21.md`, `betting-repo-research.md`,
  `repo-sweep-2026-06-16.md`, `.claude/memory/decisions.md`,
  `app/ingestion/betfair_bsp.py`.

*Author: quant-sports-researcher, 2026-07-10. Decision-support only; no idea in
this brief implies guaranteed profit, and nothing here authorizes any bet
placement code.*
