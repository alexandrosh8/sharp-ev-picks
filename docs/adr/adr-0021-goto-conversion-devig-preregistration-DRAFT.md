# ADR-0021 — Pre-registration of the goto_conversion devig hypothesis (DRAFT)

- **Status:** DRAFT — AWAITING OPERATOR SIGNATURE. Nothing in this ADR is in
  force until the operator signs; the method ships REGISTERED-ONLY (selectable,
  never a default) in the meantime.
- **Relates to:** ADR-0006 (devig method policy), ADR-0019 (sharp-vs-soft
  pre-registration — same hypothesis family and the same single-shot
  discipline), ADR-0017 (CLV close-anchor provenance). Operationalizes idea #1
  of `docs/research/2026-07-10-github-strategy-sweep.md`.

## Context

The 2026-07-10 GitHub strategy sweep scored
[gotoConversion/goto_conversion](https://github.com/gotoConversion/goto_conversion)
(MIT) the top USE-CANDIDATE: a devig that shrinks all inverse odds by **equal
units of implied standard error** (se_i = sqrt(1 − 1/odds_i)) rather than
proportionally, so longshots — which carry proportionately wider implied
standard errors — surrender more absolute probability. This targets exactly our
documented weakness: the CLV-negative longshot band that motivated the H2H
odds ceiling (ADR-0019 H1) and the multiplicative ban (ADR-0019 H4).

Its public evidence is Brier-on-outcomes (Kaggle March-Madness); **the transfer
to CLV via fair-prob devigging is OUR hypothesis, not the author's claim**
(sweep report, Open questions). It sits in the same favourite-longshot-tail
hypothesis family as the pre-registered Shin tail-devig, so it must join the
same single-shot protocol — never a spent-holdout re-tune.

A clean-room implementation (`DevigMethod.GOTO`, `app/probabilities/devig.py`,
TDD with hand-derived golden vectors, no upstream code vendored, no new
dependency) is registered as the 9th selectable method. **No default changed;
no live gate or pipeline behavior changed.** Fat-margin longshot books where
the equal-SE shrink drives a tail probability non-positive fall back to
multiplicative and report `DevigFallbackReason.GOTO_NON_POSITIVE` (expected,
debug-grade), per the module's diagnostics-as-data doctrine.

## Decision (pre-registered, frozen before the decision dataset exists)

### Hypothesis

**H-GOTO:** using goto_conversion as the fair-probability devig produces
better trusted sharp-close CLV than the shipped POWER devig baseline **on the
CLV-negative longshot band** (and no worse pooled), because the equal-SE-units
shrink removes the favourite-longshot bias that a proportional or power shrink
under-corrects in the tail.

### Decision dataset — single-shot

- The **fresh 2026 Betfair-BSP tar** (the operator's next uncorrupted,
  never-examined slate), exactly as prescribed by ADR-0019.
- Walk-forward season folds on football-data + the existing BSP archive MAY be
  used for development/debugging of the harness, but **the decisive evaluation
  is one run, once, on the fresh tar**.
- **No spent holdout may be consulted**: the 2024-07..2025-12 BSP sample and
  the 2425+2526 football-data holdouts are SPENT (read across PRs #156-160 and
  the ML value-filter one-shot). No parameter of this method or its evaluation
  may be tuned on them, and no "winner" may be read off their sweeps.

### Success bar

Adopt goto (per market family, as a candidate default subject to ADR-0006
policy) ONLY if, on the single-shot fresh slate:

1. **CLV delta vs the POWER devig baseline** (mean trusted sharp-close
   `clv_log`, identical pick set and anchors, only the devig method varied) has
   a **95% CI excluding 0** in goto's favour on the longshot band, AND
2. pooled CLV is **not degraded** (goto − power pooled delta CI does not
   exclude 0 on the negative side), AND
3. **no degradation of the H2H odds-ceiling band metrics** (ADR-0019 H1 must
   still hold with goto), AND
4. n is sufficient per market family (>= 150, per the ADR-0019 criterion),
   with the goto fallback rate on the evaluated books reported alongside.

**Frozen evaluation parameters (operator-review amendment 2026-07-10 —
pinned here so no evaluation-time choice remains):**

- **Longshot band:** fill-side decimal odds in **[3.25, 4.0]** for 1X2/h2h
  (the CLV-negative tail identified in the 2026-07-07 selection analysis,
  bounded above by the shipped VALUE_MONEYLINE_MAX_ODDS=4.0 ceiling so the
  band exists inside the live-representative pick set); for OU/totals the
  band is fill odds **>= 2.20** (the away-from-even tail; OU has no shipped
  ceiling). Bands are judged separately per family; neither may be moved,
  split, or pooled after the tar is opened.
- **Market families in scope:** 1X2 (h2h) and OU 2.5 (totals) ONLY — the two
  families with n >= 150 plausibly reachable on one slate. Other families are
  observational-only in the report.
- **Pick-set rule (identical in both arms):** the frozen selection rule of
  `scripts/research/ah_anchor_backtest.py` — edge >= 3%, fill odds
  [1.6, 4.0] (1X2) / no ceiling (OU), one pick per market, selection computed
  under the POWER fair in BOTH arms so the pick set is identical; only the
  fair used for CLV scoring varies (power vs goto).
- **Statistics:** mean `clv_log` deltas with **ddof=1 SEs, 95% t-based CIs,
  clustered by match-day** (the repo's standard, per the 2026-06-30 SE
  correction); no other CI construction may be substituted.
- **Fallback handling:** rows where goto falls back (GOTO_NON_POSITIVE)
  score under the fallback (multiplicative) fair in the goto arm — that IS
  the method as shipped; the fallback share per family is reported and, if it
  exceeds 20% in a family, that family's verdict is INSUFFICIENT rather than
  pass/fail.
- **Insufficient-n rule:** n < 150 in a family after the single shot →
  verdict INSUFFICIENT for that family (not reject, not retry on the same
  tar); the hypothesis may be re-registered for the following slate.

Anything less → record the result and reject (prior devig deltas were ~0.0002
RPS noise; the default expectation is rejection).

### What is explicitly NOT decided here

- No default change: `VALUE_DEVIG` stays `power`; `VALUE_DEVIG_PER_MARKET`
  stays empty.
- No live tier, gate, alerting, or CLV true-up behavior changes.
- No adoption based on Brier/RPS alone — CLV is the decision metric, RPS
  secondary.

## Consequences

- The method is selectable everywhere method names are validated (config
  validates against the `DevigMethod` enum; the bake-off harness
  `scripts/research/devig_comparison.py` enumerates the enum), so the eventual
  single-shot run needs no further registration code.
- Running goto in any research script before the fresh tar exists is
  permitted ONLY on training folds — any read of a spent holdout under goto
  voids this pre-registration and requires a new one on a newer slate.
- This ADR must be countersigned by the operator BEFORE the fresh 2026 tar is
  opened; evaluating first and signing after is a protocol violation.

---

*Drafted 2026-07-10 by the implementing agent. DRAFT — AWAITING OPERATOR
SIGNATURE; deliberately not self-signed.*
