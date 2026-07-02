# Plan — multi-sport high-EV picks, auto-settlement, fixed anchors, pro dashboard (2026-06-20)

Goal: solid high-EV picks across football/basketball/tennis/NFL with auto-result

- auto-ROI, fixed Betfair/Pinnacle, a professional mobile dashboard, full
  backtest/calibrate/verify, and open follow-ups closed. Built honestly.

## Honest constraints (non-negotiable, from research 2026-06-20)

- **Football**: validated edge (holdout n=62, +22.4% ROI, +0.107 incremental
  CLV > 2 SE). Real picks. _(Audit 2026-07-01: that headline is max-book
  GROSS fill with i.i.d. SEs — an upper bound; see README caveats and
  `--fill-universe soft`.)_
- **Basketball**: already a pick source; validation accrues forward via Pinnacle
  ARCADIA (no free historical close exists).
- **Tennis**: NO free closing line exists anywhere → CLV is structurally
  unmeasurable on free data. Picks can be SURFACED but never claimed as a
  validated edge. EXPERIMENTAL/UNVALIDATED tier only.
- **NFL/American football**: Pinnacle forward-capture only; no retroactive
  backtest. EXPERIMENTAL now; can graduate after a season of forward CLV.
- penaltyblog is football-goals-only (Poisson/DC/Bayesian + grid). The shared
  Colab link is a `penaltyblog.viz.Pitch` tutorial — ZERO betting content
  (verified) → no action for the +EV core.
- Doctrine stays: picks-only, read-only GET, never loosen the strict matcher,
  never present betting as guaranteed profit.

## Phases

### A. Auto-settlement + auto-ROI for ALL sports (highest value, fully achievable)

The settlement math (`settle_selection`, `pick_pnl`) is already sport-agnostic;
only a results LOADER is missing. Build a clean-room ESPN scoreboard client
(`app/ingestion/espn_scores.py`, free key-less GET
`site.api.espn.com/.../scoreboard?dates=YYYYMMDD`) emitting `FinalScore`, +
EuroLeague official API branch, + nflverse CSV NFL fallback. Tennis: derive a
pseudo-score from ESPN winner flag + per-set linescores. Wire into `load_scores`
via a per-sport source map; reuse `ScoreBook`/`settle_open_picks` unchanged;
strict matcher for name alignment (UNIQUE match or skip). Result: CLOSED tab
auto-shows result + auto-ROI, no manual entry — for every sport.

### B. Multi-sport picks + honest EXPERIMENTAL tier

Flip tennis/NFL to mint picks behind an honest `ENABLE_UNVALIDATED_PICKS` gate
(default off in committed config, on in .env), tagged `validation_state =
validated | accruing | experimental`. Validated/accruing (football/basketball)
unchanged; experimental (tennis/NFL) surfaced + tracked + clearly labeled,
never alerted as proven, never claiming "big ROI". Auto-settlement (Phase A)
builds their real forward evidence.

### C. Fix Betfair + Pinnacle anchors

Canonicalize `betfair_ex_uk/eu` -> "betfair exchange" in the odds_api parser +
make `ODDS_API_REGIONS` configurable (uk,eu) so a free Odds API key yields
Betfair Exchange AND Pinnacle as live anchors (no scrape, no name-match gap).
Keep the free OddsPortal Betfair reader + Pinnacle ARCADIA as the no-key path;
verify CLV flags enabled.

### D. penaltyblog adoption (calibration + markets)

Add `rps_average` + `ignorance_score` beside Brier in
`app/backtesting/calibration.py` (sport-agnostic proper scoring). Emit
`double_chance` / `draw_no_bet` football markets from the existing grid. (Elo/Pi
screen-only deferred — lower value.)

### E. Dashboard: professional + mobile + auto-result + multi-sport

CLOSED-tab auto-result+ROI (POST returns `{status,outcome,pnl,roi,score}`, JS
binds it — drop manual prompt where a feed exists); mobile breakpoints (768px
tablet, 375px phone); Sport column/filter on picks; `sharp_close` /
`closing_anchor_type` chip in the CLV breakdown; clear UNVALIDATED labeling for
experimental sports. Keep the textContent contract + self-contained file.

### F. Backtest / calibrate / verify

Re-run + verify the football value backtest (>2 SE holdout) and the calibration
report with the new RPS/ignorance metrics; run the tennis backtest (reports
VISIBILITY-ONLY honestly); document what needs forward accrual. No holdout
re-spend (2425+2526 are spent).

### G. Fetch today + tomorrow games -> live picks

Window is already today+tomorrow (`oddsportal_days_ahead=1`). Run the app /
pick path for all sports; surface the picks.

### H. Finish open follow-ups

Merge the two stacked branches (feat/major-league-premium-gate,
feat/clv-close-provenance) into local main (squash); apply migration; note push
as the user's call.

## Method

TDD for every code change; ultracode workflows for fan-out (per-sport loaders,
dashboard dimensions, verification); full suite + ruff + mypy + safety green at
each step; commit per concern.
