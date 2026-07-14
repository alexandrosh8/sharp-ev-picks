---
name: vanilla-dashboard-architecture
description: Structure, refactor, build, and performance-tune this project's self-contained FastAPI dashboard. Use when editing app/api/dashboard.html, login/setup HTML, CSS tokens, vanilla JavaScript renderers, DOM construction, Fetch API orchestration, responsive layout, PWA shell, accessibility, dashboard build tooling, or browser performance without introducing React, npm, a CDN, or unsafe innerHTML.
---

# Vanilla Dashboard Architecture

Preserve the deployed contract while making the authoring structure modular,
deterministic, measurable, and safe.

## Read first

- `docs/design/DESIGN.md`: locked data, safety, and single-file constraints.
- `docs/frontend-qa-checklist.md`: required manual behavior.
- `app/api/dashboard.html`: deployed dashboard artifact.
- `app/api/routes.py`: shell loading, login/setup HTML, API routes, PWA assets.
- `tests/test_dashboard_contract.py` and `tests/test_api.py`: executable UI contract.
- `scripts/dashboard_qa.py` and `scripts/dashboard_qa.sh`: browser sweep.

Use the repository `webapp-testing` skill plus
`docs/frontend-qa-checklist.md` for WCAG, viewport, console, network, and
interaction verification.

## Locked output contract

The served result remains one self-contained `dashboard.html`:

- One inline `<style>` and one inline `<script>` using `"use strict"`.
- Vanilla JavaScript only; no runtime framework, npm dependency, CDN, or network asset.
- API paths, query parameters, field semantics, safety copy, and tier/CLV states stay compatible.
- Untrusted values enter the DOM through `textContent`, text nodes, attributes
  validated against allowlists, or DOM property assignment. Never use `innerHTML`.
- Preserve `Cache-Control: no-store`, offline behavior, PWA routes, local time
  semantics required by the design contract, and WCAG-AA behavior.

## Authoring/build architecture

Do not make another manual copy of the entire HTML document. For a substantial
refactor, propose a deterministic standard-library Python builder and record the
decision in an ADR before changing the source of truth.

Recommended authoring layout:

```text
app/api/dashboard_src/
  shell.html
  styles.css
  app.js
  views/
  components/
tools/build_dashboard.py
app/api/dashboard.html          # generated, committed runtime artifact
```

The builder must:

1. Use absolute resolved paths internally and no network access.
2. Assemble fragments in an explicit fixed order.
3. Reject missing placeholders, duplicate IDs, multiple `<style>/<script>` tags,
   `innerHTML`, external URLs/assets, and unreplaced template markers.
4. Produce byte-identical output on two consecutive runs.
5. Keep the generated artifact readable; minify only after measured proof.
6. Emit size totals for HTML, CSS, JavaScript, fonts, and other data URIs.
7. Be covered by a generation-parity test and a clean-tree regeneration check.

If a build step is not approved, refactor inside the single file using the same
section boundaries and invariants.

## HTML structure

- Use semantic landmarks: header/nav/main/section/article/table/form.
- Keep one stable root per view and explicit `data-view-key`/`data-testid` contracts.
- Use `<template>` only for trusted static skeletons; clone nodes, then fill API
  values with `textContent`.
- Keep heading order, labels, `aria-live`, focus restoration, keyboard sorting,
  reduced motion, and 24px+ targets testable.
- Avoid decorative wrappers that add DOM depth without layout or semantic value.

## CSS structure and budget

Order the style block: tokens → reset/base → layout → components → utilities →
states → responsive/reduced-motion rules.

- Reuse tokens for spacing, type, surfaces, and semantic states.
- Consolidate repeated selectors and media-query overrides.
- Use semantic classes instead of per-element inline styles.
- Treat embedded fonts and data URIs as bundle bytes. Deduplicate identical font
  payloads across weights; prefer one variable/subset font or the approved local
  system stacks when visual parity permits.
- Record before/after document bytes, CSS bytes, font bytes, DOM nodes, and load/
  parse metrics. Do not call a change an optimization without measurements.

## JavaScript structure

Keep a single script in the output but organize it as ordered internal modules:

1. constants and selectors
2. typed-by-convention state and wire normalizers
3. HTTP client/error types
4. formatters and predicates
5. DOM factories/components
6. view renderers
7. loaders/cache policy
8. event binding and bootstrap

Rules:

- One state owner; renderers receive data and return/update nodes predictably.
- Prefer small DOM factories and `DocumentFragment` for batch insertion.
- Centralize API-number coercion and missing-value rendering.
- Attach listeners once or use constrained event delegation.
- Keep stale-response sequence guards and avoid overlapping refresh cycles.
- Avoid repeated full-array scans inside row render loops; pre-index derived data.

## Fetch layer

Use one guarded client around `fetch`:

- AbortController timeout and explicit HTTP/auth/error classification.
- Parse JSON once; validate required top-level shape before state mutation.
- Use `Promise.allSettled` when partial dashboard data should remain visible.
- Preserve last-good data and distinguish frozen, stale, timeout, auth, and server states.
- Cache expensive lazy panels with explicit TTLs and invalidate intentionally.
- Discard responses from superseded refresh generations.
- Never log or render raw response bodies containing sensitive fields.

## Validation

```bash
.venv/bin/python -m pytest tests/test_dashboard_contract.py tests/test_api.py -q
bash scripts/safety_audit.sh
bash scripts/dashboard_qa.sh /absolute/output/path
```

Also run a JavaScript syntax check on the extracted inline script, inspect console
and failed requests, and verify 360/390/430/768/1024/1440 widths. Compare bundle
and runtime measurements against the recorded baseline.
