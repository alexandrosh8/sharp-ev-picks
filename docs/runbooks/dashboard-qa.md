# Runbook — Dashboard QA harness

Automated Playwright sweep of the SignalDesk dashboard (`app/api/dashboard.html`,
served at `GET /`). Read-only: the browser only issues GETs, so it is safe to
run against the live app while it serves traffic.

Files:

- `scripts/dashboard_qa.sh` — runner (throwaway Playwright container on the
  compose network)
- `scripts/dashboard_qa.py` — the sweep itself (runs inside that container)

## When to run

- After **any change to `app/api/dashboard.html`**, before deploying.
- After **every deploy** of the app container (smoke check on the live UI).
- When investigating a dashboard report ("blank tab", layout broken on mobile,
  console noise).

## What it checks

For each tab (Today, Edges, Radar, Lab, Sources):

1. Clicks the rail nav button (`data-testid="rail-nav-<key>"`), waits for the
   panel `#view-<key>` to become visible, and screenshots it.
2. Asserts the tab's root panel is **non-empty** (`inner_text` after trim) —
   a blank render fails even if the click "worked".

Plus, across the whole run:

- Console errors and `pageerror`s → **fail**.
- Horizontal overflow at 390px viewport (Today tab, mobile dock nav) → **fail**.
- Failed network requests → recorded in the report, **informational only**.
- `GET /health` status + JSON body → recorded in the report for context.

Exit code is nonzero on any failure above, so it can gate a deploy step.

## Prerequisites

- Docker CLI access to the daemon that runs the app
  (`betting-ai-app-1` on network `betting-ai_default`).
- Image `mcr.microsoft.com/playwright/python:v1.49.0-noble` pullable
  (~2 GB on first pull; cached afterwards).
- The app up and serving `GET /` and `GET /health`.

## Usage

```bash
bash scripts/dashboard_qa.sh                 # artifacts -> ./dashboard-qa-out
bash scripts/dashboard_qa.sh "/path/out dir" # custom output dir (quote paths)
```

Overrides (env vars):

- `DASHQA_BASE_URL` — app URL as seen from the compose network
  (default `http://app:8000`).
- `DASHQA_APP_CONTAINER` — container to auto-detect the network from
  (default `betting-ai-app-1`; falls back to `betting-ai_default`).

## Reading the report

`<out-dir>/report.txt`, plain text, also printed to stdout:

- `health: HTTP 200 {...}` — app health at sweep time.
- `tab <key>: OK (panel text N chars)` — rendered, non-empty. A suspiciously
  small `N` on a data-heavy tab is worth an eyeball even though it passes.
- `tab <key>: FAIL ...` — click/visibility failure or empty panel; see
  `tab_<key>_fail.png`.
- `mobile_overflow_390px: YES` — horizontal scroll on mobile; see
  `tab_today_mobile.png`.
- `console_errors / pageerrors` — each listed; any count > 0 fails the run.
- `request_failures (informational)` — transient upstream/API failures land
  here; they do NOT fail the run but explain sparse panels.
- `RESULT: PASS|FAIL` plus one `failure:` line per cause.

Screenshots: `tab_<key>.png` per tab (1440x900) and `tab_today_mobile.png`
(390x844).

## Environment quirk (dev container)

The dev container **cannot run browsers directly** (missing system libs) and
**cannot bind-mount its own paths** into new containers — the docker daemon is
host-side, so a `-v` from this container's filesystem points at nonexistent
host paths. The harness therefore:

1. `docker run -d --name <qa> --network <net> <image> sleep 600`
2. `docker exec <qa> pip install -q playwright==1.49.0`
3. `docker exec -i <qa> python - < scripts/dashboard_qa.py` (sweep over stdin)
4. `docker cp <qa>:/out <out-dir>` then `docker rm -f <qa>`

The QA container self-expires after 600 s (`sleep 600`) even if cleanup is
skipped; the script also force-removes it on exit.

## CI note

**Not wired into the PR gate, deliberately** — the sweep needs a running app +
Postgres + Redis with real data, which the PR pipeline does not provision.
Recommended shape: a **manual (`workflow_dispatch`) and/or nightly (`schedule`)
GitHub Actions job**. A future job would run the same two scripts against a
compose-provisioned app; the service snippet it would need:

```yaml
# Illustrative only — do NOT add to the PR gate.
jobs:
  dashboard-qa:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Start app stack
        run: docker compose --profile prod up -d --build
      - name: Run dashboard QA
        run: bash scripts/dashboard_qa.sh "dashboard-qa-out"
      - name: Upload artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: dashboard-qa
          path: dashboard-qa-out/
```

Caveat for CI: a fresh compose stack has an empty warehouse, so data-heavy
panels may legitimately render sparse; the non-empty-panel assertion still
passes on headers/empty-state copy, but review screenshots rather than trusting
green blindly on a cold database.
