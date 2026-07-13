---
name: sharp-soft-market-analysis
description: Audit and implement sharp-versus-soft sportsbook pricing for this project. Use when classifying books as sharp or soft, selecting Pinnacle/Betfair Exchange/Smarkets anchors, comparing a sharp fair probability with a soft-book fill, handling exchange commission/liquidity/staleness, detecting steam or disagreement, validating sharp coverage, or evaluating sharp-vs-soft CLV evidence.
---

# Sharp vs Soft Market Analysis

Treat "sharp" as a property of a specific source observation with valid provenance,
market alignment, freshness, and liquidity—not as a permanent brand label.

## Source of truth

Read these before changing behavior:

- `app/edge/value.py`: `SHARP_BOOKS`, `_named_sharp_anchor`,
  `_best_other_book`, `anchor_type_for`, edge/EV calculations.
- `app/pipeline.py`: premium versus volume routing and live gates.
- `app/config.py`: active policy defaults and promotion flags.
- `app/clv_trueup.py`: archive injection and closing-line provenance.
- `app/resolution/`: Pinnacle-to-canonical event matching.
- `docs/architecture.md`: deployed data flow and source roles.

Load related skills when needed:

- `sharp-anchor-auditor` for freshness, liquidity, provenance, and demotion.
- `canonical-matcher-verifier` for cross-source event alignment.
- `odds-math` for devig, implied probability, edge, and EV.
- `clv-evidence-reviewer` for independent closing-line evidence.

## Classification contract

1. Use the normalized `SHARP_BOOKS` membership in `app/edge/value.py` as the
   executable allowlist. Do not infer sharpness from low overround alone.
2. Recognize only the exact exchange source as sharp. Betfair Sportsbook,
   generic/unknown Betfair labels, and API-shadow rows are not live sharp
   anchors unless explicitly promoted through tested configuration.
3. Prefer Pinnacle, then eligible exchange anchors in configured priority.
   Require a complete market and plausible overround.
4. Demote known-thin or positively stale exchange observations to the next
   sharp source, then consensus. Preserve the current explicit treatment of
   unknown liquidity; do not silently convert unknown into known-good.
5. Treat `consensus(median)` as a fallback estimator, never as trusted sharp
   evidence. Keep its results separately stratified.
6. Select the actionable price only from eligible soft books. Exclude the
   anchor and every normalized sharp book even if a fill allowlist includes it.

## Comparison workflow

For each candidate:

1. Align the same canonical event, sport, league, market, line, selection, and
   pre-kickoff time window. Reject ambiguous or cross-game matches.
2. Retain raw source name, canonical name, capture time, external reference,
   matcher method/confidence, liquidity, and commission provenance.
3. Build the full sharp-market odds vector. Reject incomplete or implausible
   anchors; never construct fair probability from one isolated selection.
4. Devig the displayed sharp odds using the configured method. Exchange
   commission is a payout cost: keep gross odds for fair-probability devig and
   use effective/net odds for payout, overround eligibility, edge, and EV where
   the existing implementation specifies.
5. Find the best available effective soft price after normalization,
   sharp-book exclusion, regional availability, and operator fill allowlist.
6. Calculate consistently:

   ```text
   soft_implied = 1 / soft_effective_odds
   edge = sharp_fair_probability - soft_implied
   ev = sharp_fair_probability * soft_effective_odds - 1
   ```

7. Apply freshness, in-play, min/max edge, odds-band, liquidity, market-specific
   plausibility, correlation, and sharp-anchor gates from `ValuePolicy`.
8. Route uncertain or unvalidated observations to the zero-exposure volume
   tier. Never upgrade missing evidence into premium eligibility.

## Movement and disagreement

Separate these cases:

- **Soft converging toward sharp:** expected price discovery; recompute edge
  from the latest synchronized observations.
- **Soft moving away from sharp:** flag for review; distinguish stale anchor,
  news latency, mapping error, and real disagreement.
- **Sharp source disagreement:** compare source timestamps, market definitions,
  exchange liquidity, and commission before combining sources.
- **Extreme sharp/soft ratio:** treat as a likely mapping, line, or stale-price
  defect until market-specific plausibility checks pass.

Never call a one-snapshot gap "steam" or evidence of durable edge. Require a
chronological sequence and preserve capture timestamps.

## CLV evidence

A sharp-vs-soft pick is validated only by a later, independently captured close:

- Exclude the fill book's own close.
- Exclude an unchanged copy of the pick-time anchor.
- Require the close to move beyond the repository tautology epsilon.
- Report `n`, coverage denominator, anchor stratum, freshness, confidence
  interval, and exclusion counts with every headline.
- Keep Pinnacle, exchange, and consensus closes separate before aggregation.

## Required review output

Return one row per candidate or source:

| Field | Required value |
|---|---|
| Event/market/selection | Canonical aligned identifiers |
| Sharp source | Raw + normalized name and anchor type |
| Soft fill | Raw + normalized name and effective odds |
| Capture ages | Sharp and soft ages at decision time |
| Provenance | Exact-ref or matcher method/confidence |
| Market quality | Completeness, overround, liquidity, commission |
| Math | Fair probability, implied probability, edge, EV |
| Decision | eligible / demote / volume-only / reject |
| Reason | Named gate or evidence defect |

## Validation

Use focused tests before the full suite:

```bash
.venv/bin/python -m pytest tests/test_value.py tests/test_close_evidence.py tests/test_clv_trueup.py tests/test_betfair_staleness_guard.py tests/test_resolution_hardened.py -q
bash scripts/safety_audit.sh
```

Add regression coverage for every new alias, source classification, matcher
rule, commission rule, or gate. Keep all integrations read-only and never add
order-placement paths.
