# Dashboard manual QA checklist

Repeatable pass for `app/api/dashboard.html` (served at `/`). Run after any
dashboard change, on top of the automated gates (`uv run pytest -q`,
`bash scripts/safety_audit.sh`). Use a real browser; DevTools device mode is
fine for the phone widths.

## 0. Setup

- [ ] `docker compose up -d postgres redis`, `uv run uvicorn app.main:app --reload`, open `/`.
- [ ] Console clean on load (no errors, no unhandled promise rejections).
- [ ] With `DASHBOARD_AUTH_ENABLED=true`: anonymous `/` → 303 to `/login`; after login → dashboard.

## 1. Viewports (no horizontal scroll for pick info)

Check at **360 / 390 / 430 / 768 / 1024 px**:

- [ ] Top status bar wraps cleanly; summary chips (Source Freshness / Premium / Shadow / Open / ROI / CLV) visible, no overlap.
- [ ] ≤430px: chips collapse to ONE non-wrapping horizontally swipeable strip (no wrap, momentum scroll, no visible scrollbar); total bar height ~90–100px at 360px; health dot + freshness stamp still visible on the top row; brand tagline hidden.
- [ ] ≤430px: auto-hide still works (scroll down hides, up restores) and the chip strip scrolls horizontally without moving the page.
- [ ] ≤980px: bottom section nav (PICKS/GAMES/PERF/DIAG) fixed, 44px+ targets; desktop top nav hidden.
- [ ] Pick cards: no horizontal scroll; event names ellipsize; drift bar fits.
- [ ] Focusing search/sort/tier on iOS does NOT zoom the page (16px inputs at ≤980px).
- [ ] Wide tables (Games, Diagnostics, evidence) scroll inside their own `.tscroll` wrapper only.
- [ ] Scrolling down hides the header (mobile); scrolling up restores it; never stuck hidden at ≥981px.

## 2. Sections & navigation

- [ ] Picks / Games / Performance / Diagnostics switch via top nav AND bottom bar; active state on both.
- [ ] Section choice persists across reload (localStorage `pt_view`); invalid stored value falls back to Picks.
- [ ] Entering Games/Diagnostics auto-expands their primary panel.

## 3. Picks states (each must be visually distinct, never color-only)

- [ ] Premium card: solid PREMIUM badge; Shadow card: dashed SHADOW badge + muted/hatched card + "tracked — not actionable" footer.
- [ ] ALL TIERS: Premium group first, then "Shadow — tracked, not actionable" group header.
- [ ] State badges: ● LIVE / ○ UNVERIFIED / ■ CLOSED / ▣ SETTLED (glyph + word, not just color).
- [ ] Risk row: ◈ SHARP / ◇ CONSENSUS / △ MISSING ANCHOR always present (absence is marked).
- [ ] Freshness: ● Last Updated Nm ago vs ○ STALE; display-only sports carry ▲ DISPLAY-ONLY.
- [ ] Value-gone pick: faded + "◌ no value now", still listed (no survivorship pruning).
- [ ] CLV chip states: pending / self-priced / n/a (fabricated or tautological) / dim indicative "consensus close" / trusted green-red; "provisional" tag on open picks with live CLV.
- [ ] Match Confidence: ≥0.95 renders quiet "Match Confidence: X.XX" stat in the risk row (no warning); <0.95 renders dashed △ WEAK MATCH badge whose tooltip carries the value + method; null confidence renders nothing extra (never "undefined").
- [ ] Settled: score, ✓ WON / ✕ LOST / ▣ PUSH / ∅ VOID glyph badges, Closing Line, p&l.
- [ ] CLOSED card only: small dashed "✎ Record result" control (44px+ on mobile); expanding reveals labeled Home/Away score inputs pre-filled from the scraped score; LIVE/UNVERIFIED cards never show it.
- [ ] Record result submit: success shows "Result recorded — N picks settled." then the card re-renders settled after the auto re-fetch; server error shows inline "Could not record result. (HTTP …)"; killed network shows the timeout variant; expired session redirects to /login.
- [ ] Record result is keyboard-operable: Tab to the toggle, Enter expands, Tab through inputs, Enter submits.
- [ ] Missing numeric fields render "—", never NaN/undefined (test a pick with null `decimal_odds` via devtools override).

## 4. Empty & error states (exact copy)

- [ ] Premium empty (live tab): "Nothing currently passes Premium gates. Shadow candidates may still be tracked."
- [ ] Volume filter empty: "No shadow candidates currently tracked."
- [ ] Stale poll (stop the scheduler / age the poll): amber banner "Odds data is stale. Picks should not be treated as current."
- [ ] Stop postgres: red banner (SERVER ERROR/UNRESPONSIVE/OFFLINE distinct), tape shows "Could not load picks.", ledger cells flagged FROZEN/STALE, stamp shows frozen time.
- [ ] Kill one tier's endpoint only (devtools request blocking): other tier still renders + amber "Could not refresh … picks" note.
- [ ] 401 (delete session cookie, wait for refresh): "Authentication required." then redirect to /login — no console TypeError.
- [ ] /health 503-degraded: engine label DEGRADED, chips still populated from the body (not "health: unavailable").
- [ ] Coverage fetch failing: "Sharp anchor coverage is unavailable or insufficient." in the panel.

## 5. Data honesty spot-checks

- [ ] Hero below 50 sharp closes: "n / 50" accruing state, never a blended CLV in its place.
- [ ] All-closes CLV tile dim + "Indicative only" tooltip.
- [ ] Tier switch (PREMIUM→ALL) immediately re-renders the beat-close distribution.
- [ ] Evidence strata under min-n read "insufficient data (n<50)", never point estimates.
- [ ] All times in the BROWSER timezone; change OS timezone → kickoffs shift accordingly; countdown agrees with wall clock.
- [ ] Signed percentages (+4.8%), decimal odds 2dp, CLV as % from log-ratio.

## 6. Accessibility

- [ ] Tab through header → nav → filters → tabs → cards: visible focus ring everywhere; logout reachable.
- [ ] Headings outline sane (h1 brand, h2 per section/panel) — check with an outline tool.
- [ ] Pick tabs: `role=tab` + `aria-selected` + `aria-controls="cards"`; banner has `role=alert`; stale banner `role=status`; stamp region `aria-live`.
- [ ] Table headers carry `scope="col"`.
- [ ] `prefers-reduced-motion`: no pulse/shimmer/slide animations.
- [ ] Contrast spot-check (DevTools): chip text on chip bg, faint text on cards ≥ 4.5:1.

## 7. Language / safety

- [ ] No "lock(ed)", "guaranteed", "sure bet", "easy money" anywhere (also enforced by
      `test_dashboard_avoids_promotional_language`).
- [ ] "This system never places bets" + "not a profit guarantee" visible (legend + performance safety note).
- [ ] Suggested Stake reads as informational fraction of bankroll with the never-a-guarantee tooltip.

## 8. PWA / caching

- [ ] `/manifest.webmanifest` loads; SW registers without console noise; dashboard HTML `Cache-Control: no-store` (fresh shell after deploy).
