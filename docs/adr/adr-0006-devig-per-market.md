# ADR-0006: Devig Method per Market Type

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

Edge = p_model − p_fair requires stripping the bookmaker margin. Methods
implemented in `app/probabilities/devig.py` (clean-room, oracle-validated):
multiplicative, additive, power, Shin. References: mberk/shin (test oracle —
our Shin matches its reference values to 1e-6), penaltyblog implied.py
(documents 7 methods; the extra three are candidates for a future revision),
clv-evaluation skill.

## Decision

| Market situation                                                            | Method             | Why                                                                                                                        |
| --------------------------------------------------------------------------- | ------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| 2-way markets (totals, spreads, BTTS, ML 2-way)                             | **power**          | Removes margin proportionally more from longshots; on 2-way books behaves nearly identically to Shin at lower solver cost  |
| 3-way 1X2                                                                   | **power**          | Longshot-bias correction matters most with a draw leg; power is robust and fast                                            |
| Suspected insider/longshot-heavy books (big outsiders, early markets, cups) | **Shin**           | Models insider proportion z explicitly (Shin 1993); the right tool when the margin is information-driven, not symmetric    |
| Universal fallback (solver failure, pathological books, one-sided input)    | **multiplicative** | Always defined; implementations fall back automatically and log it                                                         |
| Additive                                                                    | never default      | Can produce negative probabilities on longshot-heavy books (auto-falls back); kept for analysis parity only                |
| Exchange back/lay midpoint                                                  | deferred           | Adopted if/when Betfair read-only data lands (phase 6+); midpoint of back/lay devigs "for free" with commission adjustment |

Selection is an explicit `DevigMethod` enum parameter throughout — never
hardcoded inside pipeline code. The pipeline default is `POWER`.

## Hard rules

1. **CLV consistency:** fill-side and close-side probabilities in any CLV
   computation use the SAME method.
2. **Stale/illiquid books are rejected, not devigged into picks**: staleness
   and liquidity gates run regardless of method.
3. Method used is persisted on every `detected_edges` row for auditability.

## Alternatives considered

- Multiplicative-everywhere — rejected as default: understates favourites on
  longshot-biased books, inflating apparent edges on outsiders (the classic
  false-value trap).
- Shin-everywhere — rejected: solver cost and instability on near-fair books
  for no benefit where insider share is negligible.
- Odds-ratio / logarithmic / margin-weighting (penaltyblog) — not implemented
  yet; revisit with measured CLV data, plus goto_conversion (surfaced,
  uninspected) as a candidate.

## Consequences

- `detected_edges.devig_method` column records the method per evaluation.
- Backtests must sweep methods per market type once real data flows;
  this ADR is revisited with evidence after phase 4 (CLV loop live).
