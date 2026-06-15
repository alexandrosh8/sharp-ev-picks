---
name: odds-math
description: "Odds, vig, EV, and edge mathematics. Use when implementing or reviewing devig methods, implied probabilities, EV/edge formulas, or CLV math."
allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
---

# Odds Math

## Purpose

One canonical formula set for probabilities, vig removal, EV, edge, and CLV —
with invariants every implementation must satisfy.

## Procedure

1. Implied probability: `q_i = 1 / d_i` (decimal odds `d_i > 1.0`).
   Overround: `B = Σ q_i − 1` (book margin).
2. Devig methods (`app/probabilities/devig.py`):
   - multiplicative: `p_i = q_i / Σ q_j` (universal fallback)
   - additive: `p_i = q_i − B/n` (reject if any `p_i ≤ 0` → fallback)
   - power: solve `Σ q_i^k = 1` for `k` (brentq), `p_i = q_i^k`
   - Shin: solve for insider fraction `z`; handles longshot bias
     Method per market type follows ADR-0006; choose via enum, never hardcode.
3. Edge: `edge = p_model − p_fair` where `p_fair` is the DEVIGGED market
   probability.
4. EV per unit stake: `EV = p_model · (d − 1) − (1 − p_model)`.
5. Pick gates: `EV > 0 ∧ edge ≥ MIN_EDGE ∧ confidence ≥ MIN_CONFIDENCE ∧
odds_age ≤ MAX_ODDS_AGE_SECONDS ∧ liquidity ≥ MIN_LIQUIDITY`.
6. CLV: `clv_log = ln(d_fill / d_close_fair)` with `d_close_fair` from the
   SAME devig method as fill-side analysis; aggregate stake-weighted.

## Checklist

- [ ] Every devig output sums to 1.0 (±1e-9), order-preserving
- [ ] `d ≤ 1.0` or `p ∉ [0,1]` raises ValueError at the boundary
- [ ] Pathological power/Shin solves fall back to multiplicative (logged)
- [ ] Same devig method on both sides of any CLV comparison

## Gotchas

- **Edge and EV are not interchangeable** — positive edge with EV below
  MIN_EV (or vice versa near longshots) must fail the gate; test both.
- **Multiplicative devig understates favourites on longshot-biased books**
  — that's why power/Shin are the 3-way defaults (ADR-0006).
- **Shin's z-solve can fail to bracket on near-fair books** — fall back to
  multiplicative, never raise mid-pipeline.
- **Floating-point sums**: normalize with `p / p.sum()` as the last step;
  asserting `== 1.0` without tolerance will flake.

## Forbidden mistakes

- Computing edge from vig-inclusive implied probabilities.
- Mixing devig methods between fill and close in CLV.
- Calling any of this "guaranteed profit" anywhere.
