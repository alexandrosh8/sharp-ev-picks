# Dashboard frontend QA checklist

Repeatable validation for `app/api/dashboard.html` served at `/`.

## 0. Automated gates

- [ ] `.venv/bin/python -m pytest tests/test_dashboard_contract.py -q`
- [ ] `.venv/bin/python -m ruff check scripts/dashboard_qa.py`
- [ ] `.venv/bin/python -m py_compile scripts/dashboard_qa.py`
- [ ] Mock-only browser suite when local Chromium is installed:

  ```bash
  DASHQA_MOCK_ONLY=1 \
  DASHQA_HTML=/absolute/path/to/sharp-ev-picks/app/api/dashboard.html \
  DASHQA_OUT=/tmp/sharp-dashboard-qa \
  .venv/bin/python scripts/dashboard_qa.py
  ```

- [ ] Live + mocked container sweep: `bash scripts/dashboard_qa.sh`
- [ ] `bash scripts/safety_audit.sh`

## 1. Setup and shell

- [ ] Start dependencies, then `.venv/bin/python -m uvicorn app.main:app --reload`.
- [ ] Console is clean: no errors or unhandled promise rejections.
- [ ] With dashboard auth enabled: anonymous `/` redirects to `/login`; a valid session opens `/`.
- [ ] `/manifest.webmanifest` loads and `/sw.js` registers without console noise.
- [ ] Dashboard HTML returns `Cache-Control: no-store` after a deployment.

## 2. Responsive structure and accessibility

Check **360 / 390 / 430 / 768 / 1024 / 1440px**:

- [ ] No document-level horizontal overflow; wide tables scroll only in their own containers.
- [ ] Desktop rail is visible above 1080px; five-item bottom dock is visible at and below 1080px.
- [ ] A visible `h1` identifies `sharp-ev-picks` on desktop and mobile.
- [ ] Mobile text inputs/selects compute to at least 16px, so iOS does not zoom on focus.
- [ ] Every mobile button, select, text input, summary, and row-button target is at least 44px high.
- [ ] Event and evidence text wraps or ellipsizes without covering adjacent controls.
- [ ] Keyboard focus is visible throughout; reduced-motion mode suppresses slide/shimmer/pulse movement.
- [ ] Heading order, live regions, alert/status roles, and labels are coherent in an accessibility tree.

## 3. Five-view routing

- [ ] Today / Edges / Radar / Lab / Sources work from both rail and dock.
- [ ] Reloading each hash (`#/today`, `#/edges`, `#/radar`, `#/lab`, `#/sources`) restores that view.
- [ ] A cold load of `#/edges/<valid-id>` opens the matching drawer only after core data resolves.
- [ ] `#/edges/<missing-id>` does not freeze body scrolling or open an empty drawer.
- [ ] A missing ID is normalized only after both pick tiers loaded successfully; a partial outage does not discard it.

## 4. Edge drawer modal behavior

- [ ] Open drawer exposes `role=dialog`, `aria-modal=true`, a title reference, and `aria-hidden=false`.
- [ ] Backdrop is visible and background body scrolling is disabled while open.
- [ ] Initial focus moves to Back; Tab/Shift+Tab cycle inside the drawer.
- [ ] Escape, Back, and backdrop click close the drawer, set `aria-hidden=true`, and restore focus to the invoking row or stable Edges search fallback.
- [ ] Periodic rendering preserves focus/selection on keyed rows and controls.

## 5. Qualification honesty

Use at least seven qualifying Premium rows plus these counterexamples:

- [ ] The Qualified KPI reports the full qualifying count; the Today list remains capped at five.
- [ ] A row cannot qualify without `structural_sane === true`.
- [ ] A row cannot qualify without a sharp/Pinnacle anchor, bounded confidence, or match method.
- [ ] A started, missing, or invalid kickoff cannot qualify.
- [ ] A missing/invalid offered price or `current_edge` below its tier floor cannot qualify.
- [ ] A missing, invalid, stale, or future `revalidated_at` cannot qualify.
- [ ] Premium endpoint failure changes the KPI to `—`; retained Premium cache is explicitly non-actionable.
- [ ] Volume-only failure retains valid Premium qualification but raises the global degraded state.

## 6. Health, transport, and schema failures

Exercise each endpoint with DevTools interception or the mocked QA harness:

- [ ] Request deadline covers headers and the complete body; a body stalled beyond 15s becomes a timeout state.
- [ ] Empty, malformed, wrong-shape, and over-4MiB JSON fail closed without a console exception.
- [ ] `/health` accepts only HTTP 200 + `status=ok` or HTTP 503 + `status=degraded`.
- [ ] Wrong HTTP/body status pairing is rejected as unknown health.
- [ ] Missing picks-only health detail, invalid ages/windows, or future poll completion fails closed.
- [ ] Cold start (`polls={}`, no completed poll) displays the unverified message and Qualified `—`.
- [ ] Stale/degraded health keeps cached displays visible but never marks them Verified or actionable.
- [ ] A 401 shows Authentication required and redirects to `/login` without a TypeError.

## 7. Partial failure and last-good data

- [ ] Premium and Volume retain independent last-good rows and timestamps.
- [ ] Games, Performance, and Health retain last-good payloads after refresh failure.
- [ ] The red global banner enumerates each failed source and says when cached data is shown.
- [ ] Today/Edges/Radar/Lab/Sources label cached counts or panels; none silently resemble fresh data.
- [ ] Review queue, promotion distance, bankroll, match ceiling, and match-rate panels display explicit refresh-failed/cache copy.
- [ ] A first-load failure with no cache uses a true unavailable/empty state, not `0`.

## 8. Refresh lifecycle

- [ ] Normal visible operation refreshes countdown rendering every 30s and core data every 60s.
- [ ] No countdown/core polling fires while the document is hidden.
- [ ] Returning to a visible tab immediately re-renders countdowns and requests current data.
- [ ] A focused or dirty result form is never replaced by periodic refresh.
- [ ] A non-dirty form may refresh normally.

## 9. Result entry

For a closed unsettled pick with `scraped_score="2-1"`:

- [ ] Home/Away inputs are prefilled `2` and `1` and have unique IDs/labels.
- [ ] Enter from either score input submits the native form; click submission also works.
- [ ] Missing home/away and non-integer/out-of-range values identify and focus the exact invalid field.
- [ ] Success reads `Result recorded — N picks settled.` before refresh.
- [ ] HTTP failure includes `(HTTP N)`; timeout says `No answer within 15s`; network and invalid-response failures are distinct.
- [ ] The JSON response is schema checked and body consumption stays inside the request deadline.

## 10. CSV export

- [ ] Export follows the active search/tier/status/sort filters.
- [ ] Commas, quotes, CR, and LF produce valid RFC 4180 cells and CRLF rows.
- [ ] Cells beginning with optional whitespace/control characters followed by `=`, `+`, `-`, or `@` receive a leading apostrophe.
- [ ] Test event names and selections such as `=2+2`, `+cmd`, `-1+2`, and `@SUM(A1:A2)`.

## 11. Language and safety

- [ ] No promotional claims (`sure bet`, `easy money`, `best bet`, `risk-free`, guaranteed, or lock language).
- [ ] Picks-only/informational/never-places-bets framing remains visible.
- [ ] Untrusted, stale, inconsistent, and Shadow rows remain clearly non-actionable without relying on color alone.
