---
name: sharp-anchor-auditor
description: "Audit the sharp-anchor chain (freshness, liquidity, provenance, close-independence) for the value pipeline. Use when reviewing/changing _named_sharp_anchor, exchange liquidity floors, Betfair anchor sources, anchor demotion logic, or CLV close provenance."
allowed_tools:
  - Read
  - Grep
  - Glob
  - Bash
---

# Sharp-Anchor Auditor

Audits the chain: odds_snapshots → `app/edge/value.py:_named_sharp_anchor` (line ~598)
→ premium tier (`VALUE_REQUIRE_SHARP_ANCHOR`, live since 2026-06-26) → CLV close.

## The real thresholds (do not confuse them)

| Knob | Value | Where | Meaning |
|---|---|---|---|
| `BETFAIR_EXCHANGE_MIN_LIQUIDITY` | £10 | config.py ~846 | CAPTURE-time dust gate in `parse_betfair_feed` (betfair_exchange.py:193) |
| `VALUE_EXCHANGE_MIN_LIQUIDITY` | £50 | config.py ~524 | ANCHOR-time floor in `_named_sharp_anchor` (value.py:653-667). KNOWN-thin rejected; NULL stays eligible |
| `SHARP_BOOKS` | pinnacle, pinnacle sports, betfair exchange, smarkets | value.py:29 | Order = anchor preference |
| Exchange commission | 5% ("betfair exchange") | value.py:37 | NET for overround gate + bet-side EV only; devig runs on GROSS (P2-1) |

## Invariants to check on any change

- **NULL liquidity ≠ thin.** Main-scrape provider-44 rows carry `liquidity IS NULL` and
  supply ~59/62 Betfair-anchored events; the dedicated capture ~3 (memory:
  do-not-remove-main-scrape-betfair). Any change that makes NULL non-anchor-grade guts coverage.
- **Fail-closed demotion pattern:** a disqualified sharp book `continue`s to the next
  `SHARP_BOOKS` member, then `_consensus_anchor` — never a hard pick drop, never a silent pass.
- **Close-independence:** the CLV close must not be the mint anchor row re-read
  (ADR-0017 provenance, ADR-0020 venue independence). Never consensus-as-fill / model-as-close.
- **Provenance lands per pick:** `picks.anchor_match_confidence` / `anchor_match_method`
  (pipeline.py:275-310, observability only), `event_source_links`, `match_review_queue`,
  `GET /resolution/match-rate`.

## SQL probes (read-only prod: `docker exec betting-ai-postgres-1 psql -U betting_ai -d betting_ai -c ...`)

```sql
-- Source split: main scrape (NULL) vs gated capture (set) — expect NULL to dominate
SELECT liquidity IS NULL AS main_scrape, count(*), count(DISTINCT event_id)
FROM odds_snapshots WHERE bookmaker='Betfair Exchange'
  AND captured_at >= now()-interval '7 days' GROUP BY 1;

-- Would the £50 floor reject gated rows? (2026-07-02: 45-73% below £50 by market, median £44.5)
SELECT market, count(*), count(*) FILTER (WHERE liquidity < 50) AS below_50,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY liquidity) AS median
FROM odds_snapshots WHERE bookmaker='Betfair Exchange' AND liquidity IS NOT NULL
  AND captured_at >= now()-interval '14 days' GROUP BY market;

-- Anchor freshness at mint: age of the latest Betfair row per upcoming event
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM now()-captured_at)/3600) AS med_age_h
FROM (SELECT DISTINCT ON (event_id) event_id, captured_at FROM odds_snapshots
      WHERE bookmaker='Betfair Exchange' ORDER BY event_id, captured_at DESC) t;
```

Log probe: `docker compose --profile prod logs app --since 24h | grep "betfair api SHADOW"` —
read `mean|delta|`, `within1tick`, `api_fresher` before trusting any inline-anchor claim.

## Gotchas

- **`odds_snapshots.liquidity` holds TWO different units.** Dedicated OddsPortal capture writes
  feed `volume["44"]` (OddsPortal's displayed per-outcome £ at the Betfair price); promoted API rows
  write `availableToBack[best].size`. Same column, different semantics — split by provenance
  before aggregating, and never calibrate one floor against the other's distribution.
- **The dedicated capture is FEED-VOLUME-BOUND, not floor-bound.** ~22 liquid markets/day carry
  `volume["44"]`; the rest have a price but no volume → gated out at ingestion. Raising
  `BETFAIR_EXCHANGE_MAX_TARGETS_PER_CYCLE` or lowering the £10 floor changes nothing.
- **Median gated liquidity (£44.5) sits BELOW the £50 anchor floor.** Naively feeding inline
  volume into `VALUE_EXCHANGE_MIN_LIQUIDITY` flips ~half the known-liquidity rows to known-thin.
  Any liquidity-wiring change needs the "pct below_50" probe run FIRST and a shadow rollout.
- **Inline Betfair anchors are hours stale.** Live COMPARE (2026-07-01/02): `api_fresher=100%`,
  freshness gaps 2-9 h, only ~60% within one tick, mean |delta| ~0.49. Do not treat an inline
  exchange price as "current" in any freshness argument; a staleness guard must compare against
  a FRESH API verdict with its own TTL (stale verdict ⇒ no-op, never demote).
- **Overround gate runs on NET odds, devig on GROSS.** Netting commission before the devig biases
  asymmetric markets — keep membership (net) and magnitude (gross) separate when touching anchors.
