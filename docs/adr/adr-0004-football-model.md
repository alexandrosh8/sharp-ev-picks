# ADR-0004: Football Probability Engine — Dixon-Coles First

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

The platform needs calibrated probabilities for football 1X2, double chance,
totals, Asian handicap, BTTS, and (later) correct score/team totals.
Candidates compared: basic Poisson, Dixon-Coles, bivariate Poisson, Elo,
xG-only models, gradient-boosting ensembles, hybrids. Research grounding:
penaltyblog (inspected 2026-06-09 push; its `models/` implements exactly this
family and serves as our parity oracle), plus the calibration-eval and
betting-feature-engineering skills.

## Decision

**Dixon-Coles is the MVP engine** (roadmap phase 3): independent Poisson
goals with team attack/defence strengths and home advantage, plus the
Dixon-Coles low-score dependence correction and exponential time decay.
xG-blended ratings enter as inputs where Understat coverage exists. A
LightGBM ensemble layered on DC outputs is deferred to roadmap phase 6.

## Justification (why it beats simple Poisson)

Goal counts: home `x ~ Poisson(λ)`, away `y ~ Poisson(μ)` with
`λ = exp(α_home_att − β_away_def + γ_home)`, `μ = exp(α_away_att − β_home_def)`.

1. **Low-score dependence.** Independent Poisson systematically misprices
   draws and low-score outcomes. Dixon-Coles multiplies the joint pmf by
   `τ(x, y)` ONLY on {0-0, 1-0, 0-1, 1-1}:
   `τ(0,0)=1−λμρ, τ(0,1)=1+λρ, τ(1,0)=1+μρ, τ(1,1)=1−ρ` — one parameter ρ
   fitted by maximum likelihood corrects exactly where the independence
   assumption fails. (Applying τ outside those four cells is a known
   implementation bug — encoded in the sports-modeling skill.)
2. **Time decay.** Match log-likelihoods are weighted `exp(−ξ·t)` (t in days
   since the match). Static fits over-weight stale form; ξ is selected by
   maximizing out-of-sample predictive likelihood (typical half-life around
   one season, ξ ≈ 0.0018/day as the starting grid point).
3. **All markets from one score matrix.** Truncate the corrected joint pmf at
   10 goals → matrix M. 1X2 = lower/diagonal/upper partition sums; totals
   O/U-k = sums over x+y ⋚ k (quarter lines split stakes across adjacent
   half-lines); AH derived from the same partitions with push handling; BTTS
   `= 1 − P(x=0) − P(y=0) + P(0,0)`; team totals = marginal tail sums.
   One model, consistent prices everywhere — an ML classifier per market
   cannot guarantee that consistency.
4. **xG blending.** Where Understat data exists, attack/defence ratings are
   fitted on a goals/xG blend (weight fitted per league, not guessed) —
   xG de-noises finishing variance in small samples.
5. **Interpretability + small data.** DC fits stably on one-season samples
   (~380 rows) where GBMs overfit; parameters are auditable per team.

## Calibration & validation

- Walk-forward only; features/ratings use strictly pre-kickoff information;
  closing odds never appear as features (leakage rules).
- Per-league Brier score, log-loss, and reliability diagrams gate go-live;
  uncalibrated leagues stay off.
- Parity checks against penaltyblog's DC implementation (test oracle) before
  first deployment; market-prior blending (shrink model probs toward devigged
  market) evaluated per league and adopted only if it improves Brier/ECE.

## Alternatives considered

- **Basic Poisson** — rejected: mispriced draws/low scores (the exact gap DC
  closes with one parameter).
- **Bivariate Poisson** — rejected for MVP: extra shared-component parameter
  mainly improves draw modeling, at materially higher fitting complexity;
  revisit in phase 6 if DC calibration shows draw bias.
- **Elo** — rejected as core: outputs match-result strength, not a score
  distribution; cannot price totals/AH/BTTS consistently. Kept as a feature.
- **GBM-as-core** — rejected for MVP: needs per-market models (consistency
  loss), more data, and gave no calibration advantage in inspected projects
  (ProphitBet's accuracy-driven GBM tuning is the cautionary example).
- **Hybrid now** — deferred: ensemble after DC is calibrated (phase 6), so
  the ensemble has a sound base learner.

## Consequences

- Phase 3 implements: DC likelihood with τ correction + decay, score-matrix
  market derivation, per-league calibration reports, penaltyblog parity tests.
- The `ProbabilityModel` protocol already in `app/models/base.py` is the
  serving interface; no pipeline changes needed when DC lands.
