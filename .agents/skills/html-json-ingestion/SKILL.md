---
name: html-json-ingestion
description: Design, optimize, and review this project's read-only HTML and JSON scraper/fetcher pipelines. Use for curl_cffi/httpx sessions, OddsPortal HTML bootstrap extraction and encrypted JSON feeds, OddsChecker embedded application/json payloads, JSON endpoint parsing, Playwright fallbacks, concurrency, retry/backoff, proxy pools, schema drift, completeness gates, parser fixtures, or fetch/decrypt/parse performance.
---

# HTML and JSON Ingestion

Optimize the pipeline as independent stages:

```text
GET transport → response classification → HTML/JSON extraction → decode/decrypt
→ schema validation → normalization → OddsSnapshotIn emission → completeness gate
```

Never merge transport, parsing, normalization, persistence, and scheduler policy
into one untestable function.

## Read first

- `app/ingestion/oddsportal.py`: loader, listing/browser fallback, source routing.
- `app/ingestion/oddsportal_json.py`: bootstrap, feed URL, decrypt, parse, per-match GETs.
- `app/ingestion/oddsportal_json_session.py`: session reuse, retry, concurrency, completeness.
- `app/ingestion/oddschecker.py`: HTML, Hypernova JSON, endpoint, proxy-session flow.
- `app/ingestion/pinnacle_arcadia.py`: typed JSON API failure handling.
- `app/ingestion/base.py` and `app/schemas/odds.py`: loader and output contracts.
- `async-ingestion` and `security-review` skills for project-wide IO/security rules.

## Transport contract

1. Expose only the smallest injected async GET protocol required by the adapter.
   Market-data fetchers remain structurally incapable of POST/PUT/PATCH/DELETE.
2. Reuse one bounded session per appropriate scope. Align connection-pool capacity
   with the concurrency semaphore; do not create a session per request.
3. Set connect/read/total timeouts explicitly. Cap attempts and total elapsed time.
4. Classify retryability from status codes and typed/numeric transport errors,
   never exception-string matching. Retry transport failures, 429, and selected
   5xx responses; honor bounded `Retry-After`; never retry ordinary 4xx.
5. Acquire the semaphore per attempt and release it before backoff sleep.
6. Use `gather(..., return_exceptions=True)` when one match must not cancel the slate.
7. Sanitize logs: status, source, stage, and exception type only. Never log a
   credential-bearing URL, proxy string, response body, or stringified HTTP exception.

## Response classification

Classify before parsing:

- HTTP success versus transient/permanent failure.
- Expected content type/body shape versus HTML interstitial returned to a JSON request.
- Challenge/interstitial versus legitimate page text.
- Empty/off-window data versus parser/schema drift.
- Decode/decrypt-key rotation versus ordinary missing market.

Return or record a named gap reason. An empty list without provenance must not be
indistinguishable from a healthy empty slate.

## HTML extraction

Prefer structured state already embedded in the document:

1. `script[type="application/json"]`, JSON-LD, data attributes, or SSR bootstrap nodes.
2. Pure extraction into Python mappings/lists.
3. Schema-based payload selection when several script blocks exist.
4. DOM/table parsing only as a tested fallback.
5. Browser rendering only when required for discovery or a proven dynamic path;
   avoid per-match Playwright work when the same data is available through GETs.

Keep BeautifulSoup/selectors inside extraction functions. Do not let DOM objects
escape into normalization or storage. Treat a missing required bootstrap field as
schema drift with a fixture-backed regression test.

## JSON/decrypt pipeline

- Parse/decrypt once per unique response body.
- Validate container type and required keys before indexing nested values.
- Keep feed URL construction, timestamp coercion, bookmaker mapping, and selection
  normalization as pure functions.
- Group markets sharing one feed URL so it is fetched once per match.
- Cache immutable process-level derivations separately from cycle-scoped registries
  whose bundle/version may rotate.
- Preserve raw capture time and provenance; never invent kickoff, bookmaker, line,
  selection, or odds values.
- Unknown bookmaker IDs and invalid odds become explicit gaps, not numeric labels.

## Optimization workflow

Measure before changing code:

| Metric | Required |
|---|---|
| Cycle duration | p50/p95 and maximum |
| Request volume | listing, HTML, JSON/feed GETs per match/cycle |
| Concurrency | configured and observed peak |
| Pool behavior | connections/clients, reuse, queue wait |
| Payload | bytes by endpoint/stage |
| Outcomes | rows, events, market coverage, named gap reasons |
| Reliability | retries, 429/5xx, challenge, decrypt, schema errors |

Optimize in this order:

1. Remove duplicate GETs and browser renders.
2. Reuse sessions/connections and cache correct-scope metadata.
3. Bound parallelism to the source and local parser capacity.
4. Move CPU-heavy decrypt/parse work off the event loop only after profiling.
5. Reduce parsing passes and object churn.
6. Re-measure coverage and output parity; speed never justifies silent row loss.

## Refactoring boundaries

For large modules, split by responsibility without changing public loader contracts:

```text
source_transport.py       # sessions, GET, status/challenge classification
source_extract.py         # HTML/embedded JSON extraction
source_decode.py          # decrypt/decompress/envelope handling
source_parse.py           # pure schema → normalized domain rows
source_loader.py          # orchestration, concurrency, completeness
```

Checkpoint before a large refactor. Move one boundary at a time with characterization
tests, then delete the old path only after output parity.

## Test matrix

Cover:

- GET-only request recording.
- 200 JSON, 200 HTML interstitial, 204/empty, 403, 429 with Retry-After, 5xx,
  timeout, TLS transient/permanent errors, malformed JSON, wrong container type.
- Multiple embedded JSON scripts, comments around JSON, missing bootstrap fields,
  bundle/key rotation, unknown bookmaker IDs, invalid odds/timestamps.
- Semaphore bound, session reuse, one failed sibling, shared-feed dedupe.
- Previous-cycle collapse and missing-market completeness failures.
- Snapshot parity against the established browser/source contract.

Run focused gates:

```bash
.venv/bin/python -m pytest tests/test_oddsportal_json.py tests/test_oddsportal_json_session.py tests/test_oddsportal_json_loader.py tests/test_oddschecker.py tests/test_pinnacle_arcadia.py -q
bash scripts/safety_audit.sh
```
