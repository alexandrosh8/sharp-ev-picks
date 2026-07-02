# Decision: Betfair `volume["44"]` semantics — store-not-gate (2026-07-02)

**Context.** The dedicated OddsPortal Betfair capture writes the feed's
`volume["44"]` per-outcome value into `odds_snapshots.liquidity`
(`app/ingestion/betfair_exchange.py`). The name suggested "matched volume";
live measurement (2026-07-02) says otherwise:

- It is the **displayed available £ at the price** — a best-back-depth proxy,
  NOT matched (traded) volume.
- It is **hours stale** by the time picks read it (the inline scrape anchor
  runs 2.3–9.1 h behind the Betfair API; `api_fresher=100%` in the COMPARE
  logs).
- 14-day prod distribution: 21,291 Betfair rows, 1,512 with liquidity set;
  median £44.5; 45–73% below £50 depending on market — i.e. the median gated
  row sits BELOW the £50 anchor floor.

## Recommendation matrix (adopted)

| Option | Verdict | Rationale |
|---|---|---|
| **Store-not-gate** (keep writing `volume["44"]` to `liquidity`; keep the £10 capture dust-gate; do NOT feed it into new gates) | **ADOPT (status quo, made explicit)** | It is honest display/diagnostic data with the wrong name; the semantics ("available at price", hours-stale) are too weak to gate picks. Docstrings renamed: "matched volume" → "displayed available £ (best-back-depth proxy)". |
| Display (dashboard / pick payload) | **ADOPT** | Already flows into `PickOut.liquidity` (`app/pipeline.py` ~582); label it "displayed £ @ price (stale)". |
| Lower the £50 anchor floor (`VALUE_EXCHANGE_MIN_LIQUIDITY`) to ~£25 to admit median inline rows | **REJECT** | The floor's job is rejecting KNOWN-thin; the inline number is stale available-depth, not firmness. Lowering it to fit a stale proxy optimizes pick volume — forbidden. |
| Require API validation for gated liquidity | **ADOPT as the ONLY promotion path** | Betfair's own `availableToBack.size` (`app/ingestion/betfair_api.py:283-302`) is the real, fresh liquidity unit — the same unit the £50 floor was calibrated for (`app/config.py` `value_exchange_min_liquidity` comment says exactly this). More gated-Betfair coverage comes from `VALUE_BETFAIR_API_PROMOTE` (evidence-gated), never from reinterpreting feed volume. |

## Constraints that stand

- **NULL liquidity ≠ thin.** Main-scrape Betfair rows carry
  `liquidity IS NULL` and supply ~59/62 Betfair-anchored events (memory:
  do-not-remove-main-scrape-betfair). Any change that makes NULL
  non-anchor-grade guts Betfair coverage. The WP5 floor rejects only
  KNOWN-thin; unknown stays eligible.
- **Two units share one column.** `odds_snapshots.liquidity` holds the
  OddsPortal `volume["44"]` display value (dedicated capture) OR the API's
  `availableToBack.size` (promoted rows). Split by provenance before any
  aggregation; never calibrate one floor against the other's distribution.
  The `betfair_anchor_verdicts.api_best_back_size` column (staleness-guard
  package) now records the fresh API size per compared selection — the
  empirical cross-check for this decision.
- **The £10 capture dust-gate stays.** It only drops £0/dust rows at
  ingestion; it never touched anchoring.

## What changed in code (this package)

Comment-only: `app/ingestion/betfair_exchange.py` docstrings no longer call
`volume["44"]` "matched volume"; it is described as "displayed available GBP
at the price (best-back-depth proxy)". No behavior change; no gate touched.

## Explicitly NOT now (P4 scope cut)

Wide per-odds-row provenance on `odds_snapshots` (source job id, feed URL
hash, capture channel, session id, …) is REJECTED for now: the table is
1,430,361 rows / 366 MB (live count 2026-07-02) and append-only; capture
channel is already derivable (`bookmaker` + `liquidity IS NULL` = main scrape;
NOT NULL = dedicated; `"betfair exchange (api-shadow)"` = API shadow), and
cross-source identity persists in `event_source_links`. If verdict-table
usage proves a need, write "ADR-0021: odds-row provenance" first — the only
column that plausibly earns its bytes is a compact `source_channel SMALLINT`.
This matches the 2026-07-01 deep audit's rejection of a raw-source-table
rebuild.
