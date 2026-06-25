# +EV / Value-Betting / Sharp-Line STRATEGY Repo Research (2026-06-24)

- **Goal:** double-check betting-ai's picks-only +EV doctrine (devig a SHARP
  closing line → fair prob → bet a soft book that beats fair → tier → judge by
  CLV) against the public GitHub state-of-the-art. Specifically: is there a
  premade +EV strategy more robust than ours worth adopting (with our
  sharp-anchor/CLV layer), or are we already ahead?
- **Method:** GitHub plugin MCP (`mcp__plugin_everything-claude-code_github__*`)
  + public API for license/stars. Search terms: positive expected value betting,
  value betting devig, vig removal, closing line value, pinnacle devig,
  sharp line betting, kelly bankroll. Every recommended/scored repo had its core
  files opened and a function quoted. Nothing cloned or executed.
- **Scope note:** ML winner/ATS predictors (kyleskom, georgedouzas, ProphitBet,
  NBA_*, wegerb) are the WRONG strategy shape (outcome prediction loses on CLV —
  our own backtests) and are already settled in decisions.md / betting-repo-research.md.
  Devig/Kelly libraries (penaltyblog, mberk/shin) are already settled as
  test-oracles. This pass hunted ONLY for a *strategy* better than ours.

## Headline verdict

**No adoptable premade strategy beats ours.** Every public "EV betting" repo is
one of: (a) a basic +EV/Kelly calculator, (b) a matched-betting/arb tool, (c) an
ML outcome-predictor (wrong shape), or (d) a sharp-anchor devig scanner that is a
SUBSET of what we already run — and where it adds anything, it also reintroduces
a pitfall we already fixed (consensus-as-fair fallback) or violates no-autobet.
**We are at or ahead of the public state-of-the-art** on the strategy itself.
Two genuine *ideas* are worth stealing (calibration-of-the-devigged-prob, and a
Shin/power devig pair for the tails) — both math-only, both from no-license repos
→ clean-room reimplementation only.

## Scoring table

| Repository | Category | Stars / activity | Core function | Code quality | Maint. | Reusable | Best file to adapt | Security | Autobet risk | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| cjbrant/probability-calibration-pipeline | Sharp-consensus EV + prob calibration | 0★, push 2026-03-18, **NO LICENSE** | Devig multi-book → sharp-consensus "true" prob → **Beta-cal + BBQ calibrate the devigged prob** → scan soft books for +EV; backtest 5yr NFL ML | 3/5 — clean typed numpy/scipy/sklearn; but consensus mixes Pinnacle+BetOnline (not pure sharp), backtest admits bet-stacking inflation, no tests | one-shot, abandoned | partial (math only) | `src/evbets/calibration.py` (`BBQCalibrator.fit` Bayesian-binning + `BetaCalibratorLib`) | None — read-only Odds API | **None** | **adapt-math (ideas-only)** — steal the calibrate-the-fair-prob step |
| NateDeMoro/prediction-market-ev-engine | Pinnacle-devig EV scanner + CLV (Kalshi/Polymarket) | 0★, push 2026-06-16, **NO LICENSE** | Pinnacle sharp devig (mult/power/Shin) → variance haircut → walk soft YES-ask ladder under fee model → EV/Kelly → **dual CLV (net_ev vs net_ev_close)** + decision-time sharp re-fetch | 4/5 — well-structured, config-driven, fee-aware, freshness-gated, append-only JSONL, has tests | active solo | **NO (code)** / partial (math) | `pmev/core/devig.py` (multiplicative + **power + Shin** bisection devig, + `synthesize_combined_american`) | **`real.py` + `kalshi_trade.py` + `polymarket_trade.py` = authed order placement** | **YES — real-money auto-placement throughout** (`REAL_TRADING_ENABLED`) | **reject (code) / adapt-math (devig only, clean-room)** |
| Sanju311/plusEV-odds-finder | Sharp-vs-soft EV scanner | 0★, push 2025-07-10, **NO LICENSE** | `getTrueOddsH2H` devigs Pinnacle then EV vs soft books; emails picks | 1/5 — **WRONG devig** (additive `v*(1-juice)`, no renorm); **consensus-average fallback when Pinnacle absent = OUR OLD PITFALL**; committed `__pycache__` + json fixtures, scaffold APIs commented out | dormant | no | — | None — read-only OddsJam | **None** | **reject** — anti-pattern catalogue (does exactly the consensus-as-fair mistake we fixed) |
| jjc256/devigger | Sharp-vs-soft devig tool | 2★, push 2025-03-02, **NO LICENSE** | `src/devig.py` devig + 70KB Selenium `scrape.py` (FanDuel/Pinnacle market types) → Google Sheets output | 2/5 — has a tests/ dir; but personal sheet-glue, heavy scrape | dormant | no | `src/devig.py` (correct shape) | None — read-only scrape | **None** (no order endpoints) | **reference-only** — right doctrine, nothing we lack |
| superkush06/kelly-bet | Kelly staking | 0★, push 2026-06-01, NO LICENSE | NumPy Kelly: fractional + **simultaneous/portfolio Kelly** (joint-outcome projected gradient ascent) + MC risk-of-ruin | 3/5 — clean numpy, no tests | dormant | partial (math) | portfolio-Kelly solver | None | **None** | **reference-only** — only relevant if we add correlated multi-bet sizing |
| ianalloway/kelly-js | Kelly + CLV (TS) | 0★, push 2026-06-12, NO LICENSE | Kelly calc + CLV tracking + odds conversion, zero-dep TS | 3/5 | dormant | no | — | None | **None** | **reject** — TS, trivial, nothing we lack |

## The two ideas worth STEALING (math-only, clean-room — all no-license)

1. **Calibrate the devigged "fair" probability before computing edge**
   (cjbrant). We currently treat the Pinnacle no-vig prob as truth. cjbrant's
   key empirical finding — even on efficient NFL ML, the devigged prob is
   biased at the **tails** (underdogs overpriced, heavy favorites slightly
   underpriced), and a learned recalibration (Beta-cal or **BBQ = Bayesian
   Binning into Quantiles**, ensembled over bin counts with Bayesian-evidence
   weights, `src/evbets/calibration.py::BBQCalibrator.fit`) tightens exactly
   those tails. This is a real *strategy* improvement orthogonal to our
   sharp-anchor fix: it would shrink edge on longshots (where we likely book
   false +EV) and is testable on our own settled history. **Caveat:** it needs
   a labelled training set and risks overfitting; do it walk-forward, per-market,
   and treat the calibrator output as a haircut, not a new "truth." Maps to a
   new calibration ADR + the existing `calibration-eval`/`kestrel-calibration-pool`
   skills.

2. **A clean power + Shin devig pair with a synthetic-2-way collapse for the
   tails** (NateDeMoro `pmev/core/devig.py`). We have multiplicative/Shin via
   penaltyblog; what's notable here is (a) `devig_power` (Σpᵢᵏ=1 by bisection)
   as a middle-aggressiveness favorite-longshot correction sitting between
   multiplicative and Shin, and (b) `synthesize_combined_american` — flatten a
   3-way market's other legs into a synthetic NO so 2-way devig on [yes, synth]
   equals full 3-way devig on the YES leg. Useful if/when we devig 1X2 to a
   single-outcome fair without distorting the favorite-longshot curve. Pure
   stdlib, ~150 lines, trivially clean-roomed.

   NateDeMoro also independently validates two things WE already do right and
   the public mostly doesn't: **decision-time sharp re-fetch** (never let a stale
   snapshot mint fake edge) and **dual EV basis** (realized `net_ev` vs
   close-basis `net_ev_close` over the same closed-bet subset) — i.e. our
   CLV-vs-fill discipline. Convergent independent confirmation our `kestrel-clv-correctness`
   doctrine is the right one.

## Pitfall audit — do these repos avoid what we hit?

| Pitfall we fixed | Public repos' behavior |
|---|---|
| **Consensus-as-fair** (anchoring on soft-book consensus) | cjbrant uses a *sharp* consensus (Pinnacle+BetOnline median) — better than pure-soft but still dilutes the sharp with a semi-soft. **Sanju311 falls straight into it** (averages all soft books when Pinnacle is missing). NateDeMoro is pure-Pinnacle (correct). |
| **Fake CLV** (circular / zero-movement close) | Only NateDeMoro captures a real independent Pinnacle close at kickoff (correct). cjbrant has no CLV at all (backtests on outcomes). The rest: no CLV. |
| **Uncalibrated probs** | **cjbrant is the ONLY repo that calibrates** — and that's its whole contribution (the steal). Everyone else trusts the devigged prob as-is, like our current pipeline. |

So the public field is *worse* than us on consensus-vs-sharp and CLV, *equal*
on most devig math, and *ahead of us on exactly one axis*: probability
calibration of the fair price (cjbrant only).

## Files inspected (one quoted function each = inspection proof)

- cjbrant `README.md` (methodology + key findings), `src/evbets/calibration.py`
  (`BBQCalibrator.fit` Bayesian-binning posterior + `BetaCalibratorLib`,
  `evaluate_probs` Brier/log-loss/ECE), `src/evbets/consensus.py`
  (`compute_sharp_consensus` = median no-vig per outcome over sharp books).
- NateDeMoro `README.md` (full layout incl. `execution/real.py`,
  `*_trade.py`), `pmev/core/devig.py` (`devig_multiplicative` / `devig_power`
  bisection on Σpᵏ=1 / `devig_shin` / `synthesize_combined_american`),
  `pmev/adapters/` listing (confirmed `kalshi_trade.py` + `polymarket_trade.py`
  authed order clients), `pmev/execution/` listing (`real.py` 47KB order placer).
- Sanju311 `main.py` (`getTrueOddsH2H` additive-juice devig + `findValue`
  consensus fallback), repo listing (committed `__pycache__`, json fixtures).
- jjc256 repo + `src/` listing (`src/devig.py`, 70KB Selenium `scrape.py`,
  Google-Sheets glue).
- superkush06, ianalloway — README/description level (Kelly-only, below the bar).

## Surfaced but NOT inspected (below the inspect-worthy gate, not recommended)

jbram22/ev_sports_betting, albertlockett/sports-betting, HannibalLP33/Postive...,
whodeanie/live-odds-aggregator, acsqlworks/-EV-Sports-Betting-Engine (SQL only),
matthewaberdeen11/World-Cup-Value-Betting-Model, Tw1chet20/SportsBetting,
ethangu16/nba-betting-ev-model, hahalaa/tennis-ev-trading-system (ML predictors),
alecurtu/clv-calculator, the devtry8/* cluster (SEO-spam, content-less — flagged),
tdfarrell/betting-stake-optimizer, ShubhamNakhod/..., Pogz0r/bankroll-manager,
romeomircea98/betting_simulator, VinayJogani14/KellyBet (all basic Kelly/sim).

## Bottom line for the user

Our pipeline (pure-Pinnacle/Betfair anchor + require-sharp-anchor gate + real
CLV-vs-close + fractional Kelly + tiering) is **more complete and more correct
than every public +EV strategy repo inspected**. The only thing the public field
does that we don't is **cjbrant's calibration of the devigged fair prob to fix
tail bias** — that's the single upgrade worth piloting, clean-room, walk-forward.
