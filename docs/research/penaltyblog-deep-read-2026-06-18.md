# penaltyblog deep-read — latest docs + blog + Colab (2026-06-18)

**Question (user):** "something is missing — read every penaltyblog doc + the
Colab + the blog and optimise the code (football, tennis, basketball, NFL)."

**Method:** 4-agent read of the _current_ penaltyblog readthedocs, the
~95-post pena.lt/y blog, the user-supplied Colab notebook, and our own
penaltyblog call sites — framed strictly by the doctrine: the only validated
edge is **sharp-vs-soft line shopping + CLV**; outcome/goal **prediction**
backtested NEGATIVE CLV and is rejected. A penaltyblog feature is "worth
adding" only if it improves **devig / fair-pricing / CLV / market coverage**,
not standalone prediction. Extends [[data-source-feature-audit]].

## Verdict: nothing edge-relevant is missing

1. **The Colab notebook is a 9-cell devig tutorial** ("Implied Probabilities").
   It does exactly one thing — `pb.implied.calculate_implied(odds)` to strip
   the overround into fair probabilities — with **no model, no data, no
   backtest, no prediction**. We already own this natively:
   `app/probabilities/devig.py` implements all **7** methods
   (multiplicative, additive, power, shin, odds_ratio, **logarithmic**,
   differential_margin — enum at `devig.py:22-30`, dispatch `:48-60`) and is
   **parity-tested to 1e-8 against `penaltyblog.implied` in CI**
   (`tests/test_parity_penaltyblog.py`). (A research agent wrongly claimed we
   lacked `logarithmic`; it is present at `devig.py:29,58,127`.)

2. **No new release.** Latest penaltyblog is **1.11.0** (2026-06-02) — exactly
   what we ship (`uv.lock`, `pyproject` `penaltyblog>=1.11`). The sole 1.11.0
   change is a goals-prediction `neutral_venue` tweak (out of doctrine).
   `app/upstream_watch.py` already watches PyPI for bumps.

3. **The blog (~95 posts)** is ~85 prediction/ratings tutorials (Poisson,
   Dixon-Coles, Elo/Massey/PageRank/Pi, xG, xT, Bayesian goal models) — the
   machinery the doctrine rejects — and ~6 odds/devig/CLV posts that **confirm
   what we already do** and contain no new technique.

4. **"penaltyblog for tennis / NFL / NBA" is a category error.** penaltyblog's
   match models are football **goals**-based (Dixon-Coles / Poisson over a
   score grid). They cannot represent tennis sets/games or NFL/NBA points. The
   _only_ sport-portable pieces are devig (we own it) and `metrics`
   (calibration scorers). penaltyblog adds **zero** to those sports.

## What we use vs what penaltyblog offers

| penaltyblog surface                                                                  | Status                                                                                                                                    | Doctrine                                                             |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `implied` (7 devig methods)                                                          | **re-implemented natively**, 1e-8 parity-gated                                                                                            | positive — owned                                                     |
| `models` Dixon-Coles + grid + `goal_expectancy_extended` + `create_dixon_coles_grid` | **used, football only** (`football_dc.py`, `ah_bridge.py`) — as a _pricer_ to devig against / bridge 1X2+OU→AH, never as a live predictor | positive (pricing)                                                   |
| `scrapers.Understat`                                                                 | used **offline only** for ML-filter xG features (`build_value_dataset.py:900`)                                                            | n/a (dataset)                                                        |
| `metrics` (RPS / Brier / Ignorance / log-loss)                                       | **not imported**                                                                                                                          | positive but optional (see below)                                    |
| `models` Bayesian/Bivariate/ZIP/NegBin/Weibull-Copula                                | unused                                                                                                                                    | out — fancier goals predictors; DC goals already backtested NEGATIVE |
| `ratings` Elo/Massey/Colley/Pi, `xt`, `fpl`, `matchflow`, `viz`, `betting`           | unused                                                                                                                                    | out — prediction/rating/fantasy/presentation/paid (Opta)             |

## External validation of our thesis (worth recording)

The blog's empirical study **"How Accurate Are Soccer Odds? (250M lines)"**
(<https://pena.lt/y/2025/07/16/how-accurate-are-soccer-odds/>) independently
corroborates two pillars of the line-shopping doctrine with hard numbers:

- **Margin structure = the gap we monetise.** Pinnacle overrounds average
  **2.39%–3.00%** vs mainstream books (bet365/bwin/William Hill) at **5–7%**
  (their Table 2). This _is_ the sharp-vs-soft spread our edge captures.
- **Bookmaker 1X2 odds are well-calibrated** (predicted-prob buckets track the
  diagonal). This justifies treating a **devigged sharp anchor as a fair
  probability** — the premise the whole strategy rests on.

## Honest caveats (genuine guardrails, also from the 250M-line study)

- **Pinnacle is not a better _forecaster_.** "Despite having the lowest
  overrounds, Pinnacle does not clearly outperform higher-margin books like
  William Hill or bet365 in terms of RPS." So our edge is the **price/margin we
  beat**, _not_ Pinnacle being a better predictor — the devigged-Pinnacle
  anchor is a fair-prob reference, not an oracle. Consistent with our
  consensus-anchor being only mildly weaker than Pinnacle.
- **Obscure leagues ≠ soft/beatable.** Overrounds rise in lower divisions /
  obscure leagues (League One/Two & Serie B 7%+, Mongolia ~12%) but **RPS
  stays decent** — "the more obscure the league, the higher the margin, not
  necessarily the worse the forecast." Do not assume a high-margin obscure
  league is mispriced; the margin can be padding around an accurate line.
- **No upside to changing our devig.** The blog's EPL bake-off
  (<https://pena.lt/y/2025/09/14/from-biased-odds-to-fair-probabilities/>)
  ranks the 7 methods within ~0.0002 RPS of each other (multiplicative best
  0.19724, power worst 0.19739) and concludes "several different models … can
  produce similarly accurate probabilities." Switching default devig or adding
  solver complexity for 1X2 buys nothing. (We keep differential_margin for
  value + shin for CLV true-up, both validated on our own holdout.)
- **Scoring rule:** the author now argues RPS is non-local/inefficient and
  prefers **log loss** (strictly proper, local) with Brier close behind
  (<https://pena.lt/y/2025/05/01/>). Relevant only if we add anchor-calibration
  grading (below).

## Deferred (optional, NOT an edge change)

A standalone **anchor-calibration report** — log-loss / Brier / reliability
diagram of our _devigged sharp-anchor_ fair probabilities vs realised
outcomes, per sport/league — does not exist in `app/backtesting/` (we have
ML-filter calibration in `scripts/ml/train_value_filter*.py`, not an anchor
diagnostic). It would be a doctrine-positive _diagnostic_, but: (a) it changes
no picks and no edge, and (b) the 250M-line study already shows bookmaker odds
are calibrated, so the premise holds. Recorded here as a possible future
diagnostic, not built.

## Bottom line

penaltyblog has already been fully harvested for everything the doctrine can
use (devig, owned natively; the DC pricer, used for football). The Colab adds
nothing we don't have; the latest docs are unchanged from 1.11.0; and
penaltyblog structurally cannot price tennis/NFL/NBA. The genuine value in this
read was **external corroboration** of why line-shopping works (margin, not
forecast skill) and two **caveats** (Pinnacle isn't a better forecaster;
obscure ≠ soft) now captured above.

### Sources

- <https://penaltyblog.readthedocs.io/en/latest/> ; `/changelog/` (top = v1.11.0, 2026-06-02) ; `/implied/` ; `/metrics/` ; `/scrapers/` ; `/backtest/`
- <https://pena.lt/y/blog/> — devig, 250M-lines, better-metrics, goal-expectancy, Kelly posts (URLs inline above)
- Colab "Implied Probabilities" (user-supplied) — `pb.implied.calculate_implied` + `ImpliedMethod` enum only
- Code: `app/probabilities/devig.py:22-127` ; `tests/test_parity_penaltyblog.py` ; `app/models/football_dc.py:135` ; `app/models/ah_bridge.py:27` ; `scripts/ml/build_value_dataset.py:900` ; `app/upstream_watch.py`
