# Decisions Log

- 2026-06-11 (later) — **No-league-filter mode SHIPPED (user decision)**:
  ODDSPORTAL_FOOTBALL/BASKETBALL_LEAGUES="all" -> league-less dated daily
  pages /matches/{sport}/{YYYYMMDD}/ covering EVERY league, today+tomorrow
  (ODDSPORTAL_DAYS_AHEAD=1; "all" requires dated mode, enforced).
  Settlement "all" expands to every known results source. Far-future
  fixtures no longer scraped — by design; cycle time scales with the daily
  slate (watch busy weekends). days_ahead dates are %Y%m%d (CLI-validated
  - live-tested; dashed format 404s). Live verify 2026-06-11: today's
    Mexico-South Africa 19:00 UTC + Jun-12 games confirmed correct, 17
    bookmakers x 7 markets per game (bookies_filter defaults ALL upstream),
    628 snapshots, no picks past edge>=0.03 (opener efficiently priced).
    Pipeline LAST_POLL liveness -> /health "polls" + dashboard stale-engine
    banner + per-pick "picked Xh ago" age. NL/BE registered into the
    OddsHarvester registry (register_extra_leagues; turkey/greece were
    already upstream). App runs via nohup uvicorn on :8000 (pid changes;
    restart after env changes).

- 2026-06-11 — **League coverage + only-ML diagnosis**. "Only world-cup
  picks" root causes: config had 2 football leagues; Euro big-5 OFF-SEASON
  until mid-Aug, euroleague until Oct, Brazil/Argentina/Mexico pause during
  WC2026, NBA=Finals only — seasonality, not bugs. .env now carries 9
  football slugs (registry-verified; Argentina is argentina-liga-profesional;
  OddsHarvester has NO MLS/Netherlands/Belgium/Turkey/Greece/EuroBasket).
  Settlement \_SLUG_SOURCES corrected to real registry keys + regression test
  pinning every key to the installed registry. "Only ML picks": OddsPortal
  market-tab scraping is DOM-flaky upstream (selector timeouts; secondary
  markets intermittently empty while 1x2 succeeds) — loader now logs
  per-market snapshot counts + missing markets each cycle. MAX_ODDS_AGE
  300→1800s (multi-league cycle takes 10-20 min; picks evaluated after the
  full fetch — 300s discarded early-scraped matches). **NBA repos** (docs/
  research/nba-repo-evaluations.md): kyleskom = reference-only (NO license,
  same-day leakage, closing OU as feature, accuracy-only); NBA-Betting/
  NBA_Betting = mine-for-parts (MIT, archived) — point-in-time nba_api
  snapshot fetcher + merge_asof(+1day) join + model-cutoff rule for phase 5;
  successor NBA-Betting/NBA_AI not yet inspected.

- 2026-06-10 (evening) — **Upstream check + backtest re-verify + quarter-AH
  bridge**. Upstream: penaltyblog 1.11.0 and oddsharvester 0.3.0 are BOTH the
  latest releases (verified PyPI+GitHub 2026-06-10) — no upgrade exists;
  matchflow = nested event-JSON query engine, REJECTED as orthogonal to our
  odds pipeline. OddsHarvester issue #69 (1x2 arrays empty since 2026-05-28)
  does NOT reproduce for us — monitor. Unreleased upstream commit 9975ca4
  independently validates our browser_timezone_id="UTC" fix. Backtest re-run
  (46,220 matches): verdict REPRODUCED — holdout n=62 ROI +22.4%, incCLV
  +0.1066 >2SE, beats Max-of-books close; plain `value_backtest.py` runs
  min_odds=1.0 (v4 config needs `--min-odds 1.6`; script now prints a note).
  **odds_ratio ≡ logarithmic devig is a mathematical identity** (constant
  OR-scaling = constant logit shift) — locked by test, identical sweep rows
  are NOT a bug. Shin underround fallback demoted warning→debug (154k-line
  backtest log flood). **Quarter-line AH bridge BUILT**:
  `app/models/ah_bridge.py` (goal_expectancy_from_market →
  create_dixon_coles_grid → asian_handicap_price; EV = win·(o−1) − lose with
  stake-weighted win/push/lose; sign: line = handicap the side RECEIVES) +
  split-stake settlement (Outcome.HALF_WON/HALF_LOST; quarter components
  push on adjusted tie, whole-line selections stay EH-semantics). Loader
  still REJECTS quarter keys — enable only after the pipeline EV path is
  wired and backtest-validated (next step).

- 2026-06-10 — **Settlement engine shipped (phase 4)**, `app/settlement/`:
  outcomes.py is pure stdlib (same boundary as app/probabilities). Key
  semantics: INTEGER-line spreads selections are European handicap legs —
  adjusted draw LOSES for team legs ("Draw (line)" wins it); AH is half-line
  only (push lines rejected upstream); totals push on exact integer line;
  DNB draw = push. Free results: league slug -> source map (world-cup ->
  martj42 intl CSV; brazil-serie-a -> football-data new/BRA.csv; European
  slugs -> mmz4281 season CSVs); nba/euroleague have NO free feed -> manual
  `POST /events/{id}/result` + dashboard settle button. Matching: normalized
  names ±1 day, unique-containment fallback, ambiguity refuses. Settler
  refuses empty score book (feed outage ≠ quiet day); idempotent via
  uq_result_tracking_pick; 2h post-kickoff delay; pnl uses manual_bet_logs
  stake/odds when logged, else recommendation; settling freezes CLV (true-up
  touches only status='alerted'). Report: GET /performance — ROI +
  recommended-stake-weighted clv_log. Branch feat/settlement-engine.

- 2026-06-10 (END OF SESSION, commit 5cc61d6) — **v4 config live**:
  VALUE_DEVIG=differential_margin_weighting (7-method train sweep with 1.60
  floor; holdout n=61 ROI +21.1% incCLV +0.1058 >2SE; shin indistinguishable;
  holdout consulted 3x — trust CLV not ROI). 7 devig methods parity-tested
  vs penaltyblog. **Markets live**: football 1x2/OU2.5/BTTS/DNB/DC(derived)
  /AH-1.5(half-lines only — push lines rejected)/EH-1; basketball home_away
  - totals band 215.5/220.5/225.5 (nba,euroleague). market_detail keys each
    line's devig group. **Critical fix: browser_timezone_id="UTC" on ALL
    run_scraper calls** — oddsportal epochs inherit browser tz (+3h on Cyprus
    Mac); verified vs published WC2026 kickoffs. Dashboard: Cyprus time
    display, supersede dedupe (version bump), kickoff refresh every cycle,
    CLV card shows pending count, demo picks purged, safety note in footer.
    Scheduler: misfire_grace_time=None (run on Mac wake). 199 tests.
    **Next**: settlement engine (phase 4); optional dashboard settle button;
    quarter-line AH needs penaltyblog grid bridge (researched, not built).

- 2026-06-10 — Project is a **manual-betting +EV picks decision-support
  platform** (never an auto-betting bot, never "paper trading" by default).
  Enforcement layers: ADR-0002.
- 2026-06-10 (markets + dashboard, commit 0c0954a) — **PICKS TERMINAL
  dashboard at GET /** (self-contained HTML, textContent-only XSS-safe,
  test-enforced). **Markets live**: football 1x2/OU2.5/BTTS/DNB/double-chance
  - basketball home_away (nba,euroleague; OddsHarvester maps NO EuroBasket).
    Double-chance fair is DERIVED from the 1X2 anchor (pairwise sums —
    `double_chance_fair`); direct DC devig is invalid (quotes sum ~200%).
    Handicap market keys are REJECTED by the loader (per-line submarkets +
    push outcomes break naive devig); the researched path is penaltyblog
    v1.9+ `goal_expectancy_extended` + `create_dixon_coles_grid` (build score
    grid from sharp 1X2+OU, price AH/EH off it) — NOT yet built. **User
    policy VALUE_MIN_ODDS=1.60** re-validated (--min-odds 1.6: train choice
    unchanged shin/0.03; holdout n=58, ROI +21.1%, incCLV +0.1082 >2SE).
    /picks payload no longer carries manual_betting_reminder (alerts+banner
    keep it; audit check 8 targets app/schemas/picks.py). penaltyblog 1.11.0
    notes: extra devig methods (odds_ratio/logarithmic/diff-margin),
    predict_many(), per-match neutral_venue — candidates, not adopted.
    WagerBrain re-rejected with fresh source evidence (Kelly p/q swap).
- 2026-06-10 (v3 FINAL, maximal-data optimization) — **Production config =
  shin devig, edge ≥ 0.03** (`VALUE_DEVIG=shin`, `VALUE_MIN_EDGE=0.03`),
  chosen by sweeping devig×threshold on TRAIN seasons 1920-2324 only across
  18 leagues × 2 markets (1X2 + OU2.5, 33k train matches) and confirmed
  ONE-SHOT on holdout 2425-2526: n=62, ROI +22.4%, **incremental CLV +0.1066
  (>2SE)**, beats Max-of-books close, both markets independently positive.
  ~120 picks/yr (high conviction). Volume tier VALUE_MIN_EDGE=0.015 stays
  validated (n=379, CLV +0.019). CLV true-up uses the SAME devig so live CLV
  is backtest-comparable. Trust CLV, not small-n ROI.
  `docs/backtesting/value-findings.md`, `docs/HOW_TO_RUN.md`.
- 2026-06-10 (final repo sweep) — **No repo qualifies for binding**
  (`docs/research/value-platform-repo-research.md`): all 5 inspected are
  reference-only; no free Pinnacle feed exists on GitHub (PS3838 needs a
  funded account + has NO read-only auth scope → never bind, hard rule 3);
  multi-book "datasets" all dropped the PSH/PSC columns we already get from
  football-data.co.uk. Noted for later: goto_conversion (devig alternative),
  RapidAPI pinnacle-odds proxy (unverified ToS/limits).
- 2026-06-10 — Clean-room core: `app/` code written fresh from researched
  repos/literature; sibling projects (kestrel, Betting Picks) are NOT ported.
- 2026-06-10 (later, user direction) — **Proven libraries used DIRECTLY**:
  penaltyblog, lightgbm/xgboost, nba_api, OddsHarvester (backfills) as
  dependencies — ADR-0011. Exceptions (evidence-based): WagerBrain (Kelly
  p/q-swap bug) and betfairlightweight (ships bet execution) stay out.
  Existing pure-math core stays; parity-tested against penaltyblog (1e-8).
- 2026-06-10 (/goal — master app) — **Bound the proven engines** as the live
  spine (ADR-0012): OddsHarvester→`app/ingestion/oddsportal.py` (free
  OddsPortal odds), penaltyblog Dixon-Coles→`app/models/football_dc.py`,
  wired in `app/scheduler.py` via ODDS_SOURCE. Verified live: 760 EPL matches
  fitted, 150 Brazil Serie A snapshots scraped. `scripts/master_demo.py` is
  the proof. Needs `playwright install chromium` for live scraping.
- 2026-06-10 — **Fixed .gitignore bug**: `models/` (unanchored) was ignoring
  the whole `app/models/` source package — every fresh clone was broken.
  Anchored to `/models/`; verified via throwaway clone. [[gitignore-models-trap]]
- 2026-06-10 (v3, platform) — **Value strategy is now THE app pipeline**
  (PICK_STRATEGY=value default): `run_value_pipeline` polls -> anchors fair on
  the sharpest book -> persists + alerts; `app/clv_trueup.py` job (30 min)
  refreshes closing-fair/clv_log/beat_close on open picks — the live edge
  discipline. **18-league holdout** (n=379): ROI +2.46%, incremental CLV
  +0.0192 (>2SE), positive vs Max close — plan around CLV ~+2%; the 6-league
  +12.7% ROI was partly small-sample luck. DC refit jobs only run for
  PICK_STRATEGY=model. 171 tests.
- 2026-06-10 (v2, post-review) — **Deep review confirmed 23 findings; all
  fixed.** Key: exchange commission now netted (value.py), no-Pinnacle
  fallback = ≥3-book median consensus (one bad quote can't fake edges),
  resolve_team requires unique-longest match (no wrong-team pricing), alias
  values normalized, oddsportal timestamps/dedupe fixed, new-league closing
  odds no longer mislabeled as pre-match. **Backtest v2** (one bet/match,
  train 2122-2324 / holdout 2425-2526, incremental-CLV null, computed
  verdict): holdout edge>=0.015 → n=126, ROI +12.67%, incremental CLV +0.0261
  (>2SE), positive vs Max-of-books close. Strategy SURVIVES the stricter
  test. Caveat: modest holdout n; track live CLV.
- 2026-06-10 — **THE solid pick finder = sharp-vs-soft line shopping**
  (`app/edge/value.py`, `docs/backtesting/value-findings.md`). NOT a goals
  model. Fair value from the sharpest book (Pinnacle pref / lowest-overround
  fallback); pick = another book beating it. Backtested CONCLUSIVE POSITIVE
  CLV: edge>=0.015 → +9.25% ROI, CLV +0.043 (95% CI excludes 0), beats close
  77% over 11,667 matches / 6 leagues / 5 seasons. Live demo: 12 sane WC value
  picks. **Why:** this is the only approach that beat the market in backtest.
  **How to apply:** `scripts/value_picks.py` for live; best data is The Odds
  API regions=eu (has Pinnacle); OddsPortal free scrape works where it lists
  enough books. Caveat: real CLV lower (soft books limit winners). The goals
  model below is kept for context but is NOT the pick strategy.
- 2026-06-10 — **BACKTEST PROVES goals model has no edge** (`docs/backtesting/findings.md`):
  walk-forward Dixon-Coles vs Bet365, CLV vs Pinnacle close. EPL ROI −3.4%,
  CLV −0.075; Championship ROI −9.1%, CLV −0.072 — both conclusive negative.
  The naive goals-only model does NOT beat the market; threshold/devig/blend
  tuning can't fix it (it's an information problem). A "solid pick finder"
  needs xG/injuries + proven positive CLV. **Why:** so we never claim edge we
  can't prove. **How to apply:** track clv_log on every pick; only trust a
  model version that shows persistent positive CLV in scripts/backtest.py.
- 2026-06-10 — **Repo discovery** (`docs/research/pickbot-repo-discovery.md`):
  evaluated Elo/xG/injury/backtest repos. xG (StatsBomb) license-blocked for
  commercial use; injuries (EasySoccerData MIT/GPL conflict, transfermarkt/FIFA
  no-license) rejected for binding per "no unclear/unsafe repos" rule. Only
  martj42 international_results (CC0) bound — World Cup model. Bound: backtest
  engine, intl loader, neutral-venue DC, WC picks script.
- 2026-06-10 — **App runs fully on live in-season data**: added
  football-data "new leagues" loader (BRA/ARG/...), pick DB persistence
  (`app/storage/repositories.py`, get-or-create entities + ON CONFLICT dedupe),
  readable "Home vs Away" labels, scheduler FOOTBALLDATA_NEW_LEAGUE_CODE.
  Verified live: 5496 Brazil matches fitted → 150 odds scraped → 6 picks
  persisted → served via HTTP /picks → manual result recorded (ROI 3.10).
  Installed penaltyblog skill (built from installed package — upstream's
  .claude/skills file is gitignored in their repo, not published).
- 2026-06-10 — Free-first odds ingestion; paid Odds API keys optional
  (ADR-0010 when research completes).
- 2026-06-10 — Hooks design accepted: ADR-0003.
- 2026-06-10 — Memory system: project-local markdown (this directory) +
  docs/adr/; external memory tools rejected — ADR-0001.
