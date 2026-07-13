# ADR-0025 — Deterministic dashboard and authentication HTML builds

- **Status:** Accepted
- **Date:** 2026-07-13

## Context

The dashboard and authentication pages are currently embedded as large HTML
strings. Their duplicated base64 webfonts account for most of the response and
source-file size, while the single-file dashboard mixes markup, CSS, and
JavaScript in a form that is difficult to lint, test, or review independently.
Manual edits can also leave the served artifact out of sync with its intended
source.

The application deliberately has no Node runtime requirement in production and
must remain deployable as a small FastAPI service with no external frontend
asset CDN.

## Decision

1. Author the dashboard as three reviewable source files: shell HTML, CSS, and
   JavaScript.
2. Generate the committed `app/api/dashboard.html` with a standard-library
   Python builder that uses a fixed input order and byte-stable output.
3. Make CI run the builder in check mode and fail when the committed artifact
   differs from a clean rebuild.
4. Validate the generated artifact for unresolved placeholders, duplicate DOM
   IDs, unexpected external resources, and extra executable/style blocks.
5. Keep the served page self-contained, but use system font stacks instead of
   embedded base64 fonts. HTTP compression is applied by the ASGI application.
6. Store login and first-run setup pages as normal HTML template files loaded
   with `pathlib`; they are not runtime-generated and contain no credentials.
7. Keep browser behavior dependency-free. Node and Playwright are development
   validation tools only, not production dependencies.

## Consequences

- Source reviews and JavaScript syntax checks operate on focused files rather
  than a generated monolith.
- Builds are reproducible and drift is detected before merge.
- Initial HTML transfer and Python import/source size drop substantially by
  removing duplicated fonts and enabling gzip.
- `dashboard.html` remains committed so runtime startup never depends on a
  frontend toolchain.
- Any generated-file edit must be made in the source files and rebuilt.
