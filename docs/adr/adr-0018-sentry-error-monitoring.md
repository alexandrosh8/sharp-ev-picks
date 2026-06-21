# ADR-0018 — Sentry error monitoring (opt-in, secret-scrubbed)

- **Status:** Accepted (2026-06-22)
- **Relates to:** the logging-hygiene rules in CLAUDE.md (never log URLs /
  stringified HTTP exceptions whose query strings carry API keys) and the
  no-secrets-in-memory/docs rule. Code: `app/observability.py`.

## Context

Scheduler jobs (Betfair exchange reads, OddsPortal scrapes, Pinnacle ARCADIA,
settlement) currently fail **log-only** — there is no aggregation, so recurring
runtime errors are easy to miss, especially on the Stage-2 VPS. The user asked
to integrate Sentry.

The hard constraint: Sentry **ships error data to a third party**. This app
holds odds-API keys (carried in URL query strings), a Betfair/Sentry DSN, and
dashboard credentials. Sentry's defaults are dangerous here — it captures
**stack-frame locals**, **source-context lines around each frame**, request
URLs, headers, and bodies. Any of those can contain a raw secret.

## Decision

Integrate `sentry-sdk` **opt-in and DISABLED unless `SENTRY_DSN` is set** (in
`.env` only; `init_sentry` is a no-op without it). Every event is scrubbed
before send, and the SDK is hardened so secrets are not captured in the first
place:

- **`scrub_event` (before_send)** redacts: dict values by secret KEY; `secret=value`
  pairs inside strings (URLs incl. odds query keys, exception messages); bare
  secret SHAPES (`Bearer …`, `sk_(live|test)_…`, JWTs); `bytes` and unknown-object
  `repr()`s; and `[secret_key, value]` list pairs. It drops benign shutdown
  exceptions (`CancelledError`/`KeyboardInterrupt`).
- **init hardening:** `send_default_pii=False`, `include_local_variables=False`,
  `include_source_context=False`, `max_request_body_size="never"`, static
  `server_name`.

The DSN is a credential: `.env` (0600, gitignored) only; `.env.example` ships it
blank with a "never commit" note.

The integration was **adversarially security-reviewed**. The review found one
BLOCKER — `include_source_context` (on by default) ships the source lines around
a failing frame, so a secret literal near the error would bypass the scrubber —
now closed, along with embedded-`cookie=`/`dsn=`, `bytes`, and list-pair gaps.

## Consequences

- Runtime errors become visible/aggregated once a DSN is set; default behaviour
  (no DSN) is unchanged.
- **Residual risk (documented):** a bare secret _value_ under a non-denylist key
  with no recognizable shape (e.g. a raw token stuffed into `extra`/`tags`) is
  not caught. Rule: never put raw secrets in Sentry `extra`/`tags`/`contexts`.
- Connecting the **Sentry MCP** (to query issues from tooling) is a separate,
  interactive user step (`claude mcp add` + OAuth) — not part of this change.
- New dep `sentry-sdk` (core); review: official SDK, MIT, no install scripts.
