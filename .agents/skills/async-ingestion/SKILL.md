---
name: async-ingestion
description: "Read-only async data ingestion patterns. Use when writing or reviewing any client/loader in app/ingestion/ — rate limits, retries, dedupe, staleness, secret scrubbing."
allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
---

# Async Ingestion (Read-Only)

## Purpose

Ingest odds and stats reliably without ever writing to a provider, leaking a
key, or storing duplicate/stale snapshots.

## Procedure

1. **GET-only** — no POST/PUT/DELETE to any bookmaker, exchange, or odds
   provider. This is a hard safety boundary, not a style rule.
2. Client shape: `httpx.AsyncClient` with explicit `timeout`, tenacity
   retry (exponential backoff + jitter) on transport errors ONLY — 4xx
   never retries; 429 rotates key (if multi-key) and backs off.
3. Respect documented rate budgets per source; centralize the budget in the
   client config, not call sites.
4. Snapshot writes are append-only with the uniqueness key
   (event, bookmaker, market, selection, captured_at); duplicate polls
   no-op via ON CONFLICT DO NOTHING.
5. Staleness: compare provider timestamp age to MAX_ODDS_AGE_SECONDS at
   ingestion; mark stale rows — the edge engine rejects them.
6. Scrub secrets before stringification: error messages and logs must never
   contain the request URL (query strings carry `apiKey=`).
7. Tests use `httpx.MockTransport`; never live networks.

## Checklist

- [ ] Timeout + retry policy on every request path
- [ ] Key rotation handles 401/429 without logging the key
- [ ] Uniqueness key enforced; re-runs idempotent
- [ ] All timestamps UTC-aware before persistence

## Gotchas

- **`exc.request.url` leaks API keys** — log `type(exc).__name__` + status
  code only.
- **Retrying 4xx burns rate credits for nothing** — only transport-level
  errors (timeouts, connection resets) are retryable.
- **Provider clocks drift** — staleness uses provider-reported timestamps
  when present, but bounds them by our own captured_at.
- **API-Football is SUSPENDED** — it must not appear in code or config;
  use approved sources from docs/research/free-odds-sources.md.

## Forbidden mistakes

- Any write request to a betting provider.
- Scraping behind logins or bypassing anti-bot protections.
- Silent empty results — a provider returning nothing must flag/raise.
