# Decisions Log

- 2026-06-10 — Project is a **manual-betting +EV picks decision-support
  platform** (never an auto-betting bot, never "paper trading" by default).
  Enforcement layers: ADR-0002.
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
