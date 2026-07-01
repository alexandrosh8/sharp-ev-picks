# ADR-0020 — CLV anchor↔close-venue independence (stratify by anchor source)

- **Status:** Proposed (2026-07-01)
- **Relates to:** ADR-0015 (Betfair exchange capture), ADR-0017 (CLV close-anchor
  provenance), ADR-0019 (sharp-vs-soft pre-registration). Arises from the
  Betfair-vs-Pinnacle "one vs both" review (2026-07-01).

## Context

The platform anchors a pick's fair value on a sharp book (`SHARP_BOOKS =
(pinnacle, pinnacle sports, betfair exchange, smarkets)`, Pinnacle preferred) and
grades the pick's Closing Line Value (CLV) against a sharp CLOSE. The backtest
truth metric for CLV is the **Betfair BSP close** (ADR-0019).

The review surfaced a subtle correlation trap:

- A pick **anchored on Betfair Exchange** and then **graded against the Betfair
  BSP close** compares the **same venue** at two times. That is not a clean
  sharp-vs-soft CLV — it partly measures Betfair's own exchange→BSP drift, a
  partial tautology that inflates apparent CLV.
- A pick anchored on **Pinnacle** and graded against Betfair BSP is a genuinely
  **independent-source** comparison — the clean signal.

The live path already enforces a related independence (`value.py:106-204`:
`close_is_independent_of_fill` + the value-delta gate) — but those guard the
injected LIVE close, not the BSP **backtest metric**. So BSP grading of a
Betfair-anchored pick is currently unguarded.

Both sources stay CAPTURED (they cover complementary slates — Betfair = liquid
majors, Pinnacle = broad; dropping either loses coverage). This ADR is about the
CLV **evaluation**, not capture.

## Decision

**A pick's CLV close must come from a different venue family than its creation
anchor.** Concretely, when evaluating CLV (backtest or forward):

1. **Stratify CLV by `anchor_type`.** Report Pinnacle-anchored, Betfair-anchored,
   and consensus-anchored CLV separately — never a single blended headline.
2. **Betfair-anchored picks graded against a Betfair-family close (BSP or
   exchange) are a LOW-TRUST / EXCLUDED stratum.** Either exclude them from the
   headline CLV, or grade those specific picks against the **Pinnacle** close
   instead (independent source).
3. Extend the existing `close_is_independent_of_fill` notion to also require
   **anchor≠close-venue-family** for the BSP metric, mirroring what it already
   does for the fill book.

Pinnacle-anchored is the clean default; the priority order (`SHARP_BOOKS`) already
maximises Pinnacle anchoring, so the affected stratum is small — but it is exactly
the stratum that would be tautological against BSP.

## Consequences

- CLV reports gain an `anchor_type` breakdown; the headline number excludes (or
  re-grades) the Betfair-anchored-vs-Betfair-close stratum — a more honest,
  slightly smaller headline.
- No capture change; both sharp feeds stay on (coverage is complementary).
- Implementation is deferred until scoped against the CLV/settlement owners
  (`app/backtesting/clv.py`, `app/backtesting/live_evidence.py`, the
  `run-betfair-bsp` path) — it touches CLV semantics and must not silently
  rewrite historical numbers. This ADR is the pre-registered rationale.
