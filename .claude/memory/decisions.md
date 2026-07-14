# Decisions Log

- 2026-06-24 (+EV/value/sharp-line STRATEGY repo sweep — repo-researcher,
  file-inspected; log: docs/research/ev-strategy-repo-research-2026-06-24.md) —
  hunted GitHub for a premade +EV *strategy* more robust than ours (sharp-anchor
  devig → fair → soft-book edge → tier → CLV). VERDICT: **no adopt; ours is at/
  ahead of public SOTA.** Every hit is a basic +EV/Kelly calc, matched-bet/arb,
  ML predictor (wrong shape), or a sharp-devig scanner that is a SUBSET of ours.
  Settled (don't re-eval): **cjbrant/probability-calibration-pipeline** (NO LICENSE;
  the ONE genuine idea — Beta-cal + **BBQ** calibrate the *devigged* prob to fix
  tail bias, underdogs overpriced/favorites underpriced → adapt-math clean-room,
  walk-forward); **NateDeMoro/prediction-market-ev-engine** (NO LICENSE; **REJECT
  code — real-money autobet** `execution/real.py` + `adapters/*_trade.py`; but
  `pmev/core/devig.py` power+Shin+`synthesize_combined_american` = adapt-math
  clean-room; independently confirms our decision-time sharp re-fetch + dual
  net_ev/net_ev_close CLV); **Sanju311/plusEV-odds-finder** (REJECT — wrong
  additive devig + consensus-avg fallback = OUR OLD PITFALL); **jjc256/devigger**
  (reference-only, right shape, sheet-glue); **superkush06/kelly-bet** (reference
  — portfolio/simultaneous Kelly if we ever size correlated bets); ianalloway/
  kelly-js (reject, TS trivial). devtry8/* CLV repos = SEO-spam, content-less.
  Net new action: pilot calibration-of-fair-prob (the only axis the public beats us).

- 2026-06-21 (multi-sport historical-ODDS dataset deep-scan — repo-researcher,
  file-inspected) — hunted GitHub+Kaggle for repos/datasets that VENDOR historical
  odds CSV/parquet (not scrapers) carrying odds + results, judged vs the sharp-vs-soft
  CLV backtest gate. VERDICT: **no new ADOPT**. Best FREE+permissive finds are
  results-rich but SOFT/consensus-close-only -> fail the incremental-CLV-vs-Pinnacle
  gate (same failure as nflverse/SBR): (a) **cviaxmiwnptr/nba-betting-data** (Kaggle,
  CC0, 24.4k NBA games 2007-2026, ML/spread/total + scores + cover/total result flags,
  free 2.49MB CSV, src SportsbookReviewsOnline consensus) -> REFERENCE/backfill for NBA
  only; (b) **jonathanncoletti/nhl-historical-game-data** (Kaggle, CC0, 60k NHL rows
  2004-2026, ESPN-sourced soft odds) -> reference; (c) **ArnavSaraogi/mlb-odds-scraper**
  (GH, NO LICENSE, 76MB JSON release MLB 2021-2025, per-book OPEN+CLOSE FanDuel/DK/Bet365
  - scores, scraper inspected = GET-only no-autobet) -> reference, unliftable+soft.
    The ONLY true sharp Pinnacle OPEN+CLOSE+novig+movement dataset found
    (**oliviersportsdata/tennis-grand-slam-pinnacle-pure**, ATP/WTA Grand Slams 2016-2026,
    113 cols Blocks A-H w/ CLV signals) is a 257-match Kaggle TEASER; full 10,266-match set
    is PAID on Gumroad (CC-BY-NC sample only) -> REJECT (paid), schema REFERENCE only.
    REJECTS: ovignez-hash/\* + oliviersportsdata "US Sports Master 9 sports" = same Gumroad
    vendor, 50-row Kaggle samples, closing-only single consensus, paid full; tayfunakcay/
    football-data-collection = empty README pointer to football-data.co.uk (already used).
    Confirms the standing gap: NO free historical Pinnacle open+close beyond football
    (football-data.co.uk PSH/PSCH). marcoblume/pinnacle.data re-confirmed MLB+election only.

- 2026-06-20 (RESULTS-tab merge + auto-settlement + live sharp-anchor verified +
  goal audit — local main, 974 green) — (1) **Sharp anchor ENABLED + live-proven**:
  set VALUE*SHARP_ANCHOR_FROM_ARCHIVES=true in .env; live run logged "merged 93
  free sharp-anchor snapshot(s)" -> "3 premium picks, 2 volume" — picks now anchor
  on the free Betfair/Pinnacle sharp price, not consensus. (2) **CLOSED+SETTLED ->
  one RESULTS tab** (user chose merge): inResultsTab = kicked-off OR settled;
  reuses the settled column layout (settledView = STATUS_TAB==="results"); cellResult/
  Score/Pnl fall back to provisional*\*; cellStatus drops the CLOSED badge + the
  manual settle button. Scorecard = W-L-P + %beat-close + mean CLV (ROI stays in
  the header; stake never surfaced — test_dashboard_renders_confidence_stars
  forbids recommended_stake_amount in the page). (3) **Auto-settlement, no manual**:
  outcomes.provisional_result() grades a kicked-off pick from the SCRAPED score
  (read-time, RESULTS tab); engine.\_load_scraped_finals + settle_from_scraped_scores
  (default ON) RECORDS it — minor leagues with no feed auto-settle from the score
  on the pick's OWN event (exact name match, no cross-source risk). Feed/ESPN take
  precedence; feed outage still warns. (4) **arcadia NFL warning** -> warn-once per
  sport (off-season spam killed). (5) **calibration**: added ignorance score (bits
  = log_loss/ln2, Good 1952) beside Brier; RPS deliberately skipped for the binary
  pick-conditional report (=Brier for 2 outcomes). (6) **10-agent goal audit**:
  9/10 DONE + 1 PARTIAL (calibration metrics) NOW CLOSED -> all original-goal
  components verified DONE with file/commit evidence; the "OFF by default" notes are
  intended conservative defaults, not gaps. SCREENSHOT-VERIFIED RESULTS tab. 19
  commits ahead of origin, NOT pushed.

- 2026-06-20 (/goal correctness review + sharp-anchor-at-pick-time — local main,
  968 green) — 5-agent review (Explore x4 + research) answered the user's audit:
  (1) **Picks were CONSENSUS-anchored, NOT sharp** — run_value_pipeline only saw
  OddsPortal soft books (Pinnacle absent there, Betfair in a skipped section);
  Betfair/Pinnacle archives were consumed ONLY at settlement for CLV. So live
  picks ran a WEAKER variant than the validated Pinnacle-anchored backtest. FIX:
  `VALUE_SHARP_ANCHOR_FROM_ARCHIVES` (default OFF) — PipelineDeps.sharp_anchor_loader
  merges the captured free Betfair (EXACT ref) + Pinnacle ARCADIA (STRICT match),
  re-keyed to each scraped event, into the anchor set at pick time (anchor only;
  the scrape is what's persisted). clv_trueup.build_sharp_anchor_loader +
  resolve_betfair_back_snaps extracted; reuses the SAME resolve path as the
  settlement close (no new false-match surface); isolated try/except so picking
  never breaks. Tested (soft->consensus vs +Betfair->sharp). (2) **Team matching
  is SOUND** — soccer/basketball false-negatives only (the ~37% structural alias
  gap, not a bug); Betfair EXACT-ref safe; tennis Pinnacle resolve is ordered=True
  (conservative — misses reversed-order tennis, but NO false-positive; tennis CLV
  is unvalidatable anyway so low impact; the reviewer's "false-positive" framing
  was inaccurate for the current code). (3) **Markets** — all configured reach
  picks EXCEPT Asian Handicap (scraped but deliberately not wired; ah_bridge.py
  complete but needs a backtest first); no silent drops. (4) **Runtime CLEAN** —
  app live-verified (only the intentional xgboost upstream notice; 0 JS console
  errors; OddsHarvester 0.3.0 patch-guarded; httpx silenced). (5) **Free
  historical data CONFIRMED ABSENT** for NBA/tennis/NFL with Pinnacle open+close
  — football works only via football-data.co.uk's PSH+PSCH pair; the repo the user
  recalled is marcoblume/pinnacle.data (real Pinnacle but MLB+election ONLY).
  Dashboard: mobile+PC+tablet screenshot-verified (720->860 card breakpoint).

- 2026-06-20 (/goal multi-sport high-EV optimization — IMPLEMENTED, branch
  feat/clv-close-provenance, all local) — realized the ESPN auto-settlement
  decision below + more, full suite 967 green, ruff/mypy/safety clean each step:
  (A) **ESPN free auto-settlement** — clean-room app/ingestion/espn*scores.py
  (key-less GET site.api.espn.com scoreboard; team parser + tennis SET-score
  parser) wired into run_settlement_cycle so basketball/NFL/tennis auto-settle +
  auto-ROI through the existing ScoreBook (ESPN_SETTLE*\* config; SCORES only,
  never odds). (B) **Experimental picks** for unvalidated sports — opt-in
  ENABLE_UNVALIDATED_PICKS routes tennis/american_football to
  PipelineDeps.experimental_sports; run_value_pipeline FORCES them to volume
  (shadow) tier: surfaced + CLV-tracked + auto-settled, NEVER alerted/sized.
  (C) **odds_api Betfair/Pinnacle** — \_BOOK_CANONICAL folds betfair_ex_uk/eu ->
  "betfair exchange" + ODDS_API_REGIONS config (keyed path). (E) **dashboard** —
  /picks now exposes sport/sport_label/closing_anchor_type; picks table badges
  non-soccer picks, tennis/NFL get a warn EXPERIMENTAL chip. **penaltyblog
  audit: LOW value** — devig parity + Dixon-Coles already integrated; calibration
  already has log_loss (= ignorance score) + Brier/ECE/MCE; models are
  football-goals-only (screens-only for tennis/NFL/NBA); the user's Colab is a
  penaltyblog.viz.Pitch tutorial with ZERO betting content → no code change.
  **FREE Betfair/Pinnacle VERIFIED (see pitfalls.md):** Pinnacle is ABSENT from
  OddsPortal in every region → arcadia guest JSON is the ONLY free source (live:
  584 matchups); Betfair = OddsPortal betting-exchanges-section (selectors
  correct, UK/EU proxy + liquid major required); roundproxies blog re-confirmed
  off-doctrine; NO new free source exists. .env confirms BETFAIR_EXCHANGE_ENABLED
  - ARCADIA_ENABLED + both CLV flags = true; 15 UK proxies wired. **Football edge
    RE-VERIFIED** (held-out: edge>=0.03 n=62 ROI +22.4% incCLV +0.107 >2SE). Live
    World Cup value picks demonstrated (3 picks, consensus-anchored from host IP).
    REMAINING: full dashboard mobile-polish redesign (sport badges + cards exist;
    visual device-test pass deferred); merge stack to main; push (user's call).

- 2026-06-20 (repo sweep — AUTO-SETTLEMENT gap; ESPN public API) — multi-sport
  repo discovery for the open gaps (results/scores feeds for NBA/tennis/NFL
  settlement + fixtures; calibration/Kelly/CLV were re-confirmed SETTLED — only
  bee-movie/SEO spam surfaced, no gap). DECISIVE FINDING: the free, no-auth,
  GET-only **ESPN public scoreboard** (`site.api.espn.com/apis/site/v2/sports/
{sport}/{league}/scoreboard?dates=YYYYMMDD`) live-verified to carry SETTLEMENT
  primitives (home/away `score`, `winner`, `status.type.completed`) AND
  pre-match FIXTURES for NBA(1)/NFL(16)/soccer eng.1(1)/ATP tennis(4)/nbl(2)/
  fiba(2)/mens-college-basketball(1) — the exact data the project lacks today
  (`app/settlement/results.py` \_SLUG_SOURCES has NO source for nba/euroleague/
  tennis → manual POST only). `sportsdataverse/sportsdataverse-py` (MIT, 104★,
  push 2026-06-19, v0.0.67, PEP621 setuptools — NO custom/postinstall hooks)
  is the clean reference: `nba_schedule.espn_nba_schedule` + GET-only
  `dl_utils.download` (requests.Session.get, no auth, Retry-After clamped 120s,
  404-as-no-data) → VERDICT **adopt-pattern** (clean-room an async httpx ESPN
  client `app/ingestion/espn_scores.py`; do NOT add the package — heavy
  xgboost/polars/pyreadr/matplotlib deps orthogonal to FastAPI/asyncpg). MIT =
  liftable with attribution. CAVEATS: (a) ESPN has NO `euroleague` slug → that
  league stays manual; (b) tennis needs match-level `summary?event=` parsing
  (tournament-scoped IDs, not the clean team-sport shape) — extra work vs
  NBA/NFL; (c) ESPN "Betting & Odds" endpoints are read-only SOFT books
  (Caesars/FanDuel/DK, NO Pinnacle) → NOT a CLV close anchor, scores-only.
  REJECTS: `pseudo-r/Public-ESPN-API` (endpoint DOC only, reference-only, no
  license); `msolonskyi/ManTennisData` (201-byte stub README, 5★, reject);
  flashscore/sofascore scrapers (ToS-grey, redundant with ESPN's no-auth JSON);
  all tennis-elo + "settlement" search hits = outcome-prediction or SEO/bee-movie
  spam. NO autobet risk in any inspected repo (sportsdataverse code-search 0 hits
  for place_order/betslip). Full report in this session's research output.

- 2026-06-20 (CLV close-anchor provenance — ADR-0017) — an adversarial review
  found the CLV TRUST METRIC was partly contaminated: a pick's `anchor_type` is
  the CREATION anchor, but the CLOSE (computed later by clv_trueup) can be
  anchored by a soft-book CONSENSUS median, or be a poll-time REVALIDATION
  FALLBACK (`closing_odds` NULL) — and both were reported under the creation
  anchor and counted in the headline stake-weighted CLV with NO provenance
  filter. Fix (additive, feature-detected like anchor_type): new
  `Pick.closing_anchor_type` (migration d3b8f1c7a9e2) set by BOTH write paths
  (revalidate_open_picks + finalize_closing_from_snapshots) from the anchor
  event_fair_probs already returns (was discarded). A close is TRUSTED ("sharp
  close") iff snapshot-sourced (closing_odds NOT NULL) AND named-sharp-anchored
  (pinnacle/sharp, NOT consensus). live_evidence_report gains a `sharp_close`
  stratum + `by_close_anchor`; performance_report/\_aggregate_settled gain
  `n_sharp_close`/`sharp_stake_weighted_clv_log`/`sharp_beat_close_rate` —
  alongside (not replacing) the blended headline, so the operator SEES how much
  CLV is genuine sharp evidence. `by_anchor` keeps its CREATION-anchor contract
  (the deliberate consensus-creation forward test). 957 tests green, ruff/mypy/
  safety clean, migration up/down verified. NOTE: contested review finding —
  soft-book consensus circular edge reaching premium — was NOT treated as a bug
  (the team's pre-registered validation shows consensus is +CLV at the premium
  threshold and is deliberately tracked forward via anchor_type). odds_api-only
  anchor-freshness gap deferred (default oddsportal shares one captured_at).

- 2026-06-20 (major-league PREMIUM gate — ADR-0016) — the honest-high-ROI lever
  is scoping the ALERTING tier to leagues with real sharp coverage, NOT looser
  matching. New `VALUE_MAJOR_LEAGUES` (csv of scraped `league_name`) → frozen
  `ValuePolicy.major_leagues`; a premium candidate outside the set is DEMOTED to
  the volume/shadow tier (still CLV-tracked, never alerted, no exposure),
  mirroring the `VALUE_ML_FILTER` demotion in `run_value_pipeline`. Exact-on-
  normalized match (NFKD/casefold/space) — never fuzzy (can't falsely promote a
  minor league); blank league with gate on ⇒ not major. DEFAULT OFF (empty =
  every league premium-eligible, non-breaking); `.env.example` ships a commented
  majors seed. Curate the set from the existing `/resolution/match-rate`
  `by_league` report (same `league_name` key). DEMOTE not drop = the evidence
  trail stays auditable. New pure helpers `normalize_league`/`is_major_league`
  in `app/edge/value_policy.py`; 9 new tests; full suite + ruff/mypy/safety
  green. Also fixed a pre-existing local-only test fragility:
  `test_persistence.py` warehouse-fallback tests used `limit=200` against the
  SHARED dev DB (3172 events, 612 before now+3h) and got crowded out — raised to
  `limit=5000` to match the already-hardened soccer sibling (NOT pollution; only
  0 test-pattern refs exist). See [[gateguard-write-pattern]].

- 2026-06-19 (5-stream ultracode research sweep — see
  docs/research/betting-repo-research.md) — VERDICT: nothing new to build.
  Free live Pinnacle (`guest.api.arcadia.pinnacle.com`) is REAL, GET-only, and
  ALREADY fully ours (`pinnacle_arcadia.py` extracts moneyline+totals+spreads —
  the "discards totals/spreads" research claim was FALSE, caught on grounding).
  Live `/resolution/match-rate` = 28/98 (28.6%): ~35% `no_archive_candidates`
  (Pinnacle doesn't cover obscure off-season leagues — unfixable) + ~37%
  `unmatched_with_candidates` (alias gap, fixable but for teams we won't pick
  in-season). NBA/tennis stay visibility-only until in-season Pinnacle coverage
  - a held-out forward-CLV >2SE flips `CLV_USE_PINNACLE_ARCHIVE`. Alt scrapers
    (whodeanie/live-odds-aggregator, aqsmith02/paper-betting-tracker) REFUTED by
    the verifier (regions=us, no Pinnacle, averages all books). roundproxies blog
    = no new safe source (off-limits: 2Captcha/stealth/login/proxy-evasion). Top
    mobile skills (ce-frontend-design, bencium-impact-designer, ui-design-system)
    already in ~/.claude/skills/. Do not re-run these searches.
    FOLLOW-UP PROBE (scripts/research/probe_arcadia_match.py): the 36
    `unmatched_with_candidates` are NOT pure coverage noise — filtering
    candidates to ones sharing a team-name token shows **32/36 are real
    ALIAS/dedup gaps**, 3 systematic patterns: (1) DUPLICATE Pinnacle archive
    captures of one fixture → the matcher's "multiple candidates => no match"
    rejects an EXACT-name match (e.g. Perry Lakes Hawks vs Willetton Tigers) —
    a dedup bug, the biggest lever; (2) unstripped suffix tokens (Besiktas vs
    "Besiktas JK" — generalizes to in-season majors); (3) women-team naming
    ("Cairns W" vs "Cairns Dolphins"). Fixes (matcher is CLV-critical, needs
    careful TDD): dedup candidates by (norm_home,norm_away,date) before the
    uniqueness check; extend \_NOISE_TOKENS / aliases_seed.json; women aliases
    without conflating M/W.
- 2026-06-19 (ROI goal — backtest + calibrate + doctrine-safe matcher fix).
  BACKTEST re-confirmed the validated edge: held-out 2425+2526 thr=0.03 n=62
  ROI +22.39%, incCLV +0.1066 (>2SE), beats Max-close; thr=0 baseline -1.59%
  (edge is all in the selection gate). Calibration INSUFFICIENT (0 settled).
  AUDIT (16 agents + data-snooping skeptic) verdict: strategy at its VALIDATED
  CEILING — NO parameter is tunable without data-snooping (2425/2526 burned).
  Skeptic REJECTED the "commission-netting bug" (it's the Dixon-Coles
  goals-model path, NOT the value-pick path). Only implement-now safe levers =
  matcher correctness fixes. IMPLEMENTED (TDD, CLV-critical, SHADOW-only so
  zero pick-ROI risk): match_event now picks the NEAREST-kickoff capture among
  same-canonical duplicates (was "len!=1 -> None"; recovered the exact-name
  rejects like Perry Lakes) + added "jk" to \_NOISE_TOKENS (Besiktas JK). Live
  matched 28->32. Women/reserve-team aliases STILL deferred (risky M/W
  conflation). Everything else (ML v2, AH, consensus demotion) gated on FRESH
  season 2627 — do NOT touch 2425/2526.

- 2026-06-19 (penaltyblog + OddsHarvester unused-feature scan — see
  docs/research/betting-repo-research.md) — penaltyblog has NOTHING new for the
  picks (no odds/Pinnacle source; unused modules are wrong-shape goal/rating
  models or redundant devig/Kelly; only `metrics.rps_array` + Dixon-Coles score
  heatmap are dashboard-diagnostic candy, deferred). OddsHarvester's ONE real
  unused feature = HISTORIC mode + `--odds-history` (free per-book OPENING +
  CLOSING odds for past matches) — USE, but gate a backfill on a probe that
  confirms OddsPortal historic exposes **Pinnacle** open+close (else it's a real
  close but not a sharp anchor). Built instead: dashboard **CLOSED tab** (4th
  tab, kicked-off picks = proof-of-edge ledger) + **CLV scorecard** (% beat
  close, mean CLV). Do not re-scan these two repos.
  PROBE RAN (scripts/research/probe_historic_odds.py, EPL 2023-24, 1 page):
  HISTORIC works — 50 matches, per-book CLOSING 1X2 odds — but **PINNACLE
  ABSENT** (8 soft books: 1xBet/22Bet/888sport/BetInAsia/Betsson/GGBET/N1Bet/
  bet365). Data gate NOT cleared: free soft-book closes (better than our
  re-price proxy for grading) but no free sharp anchor. Historic backfill NOT
  worth building for edge — DECIDED, do not re-run unless probing a per-match
  detail page / target_bookmaker=Pinnacle (residual uncertainty only).

- 2026-06-18 (scrape-gap log fix — 'period target element not found' downgraded;
  basketball was a RED HERRING) — live monitoring flagged 8/hr
  `ERROR:SelectionManager:period target element not found for: Full Time`.
  Systematic-debugging traced it through installed oddsharvester 0.3.0
  (selection.py:86, sport_period_registry.py, validate_and_convert_period):
  the period IS resolved per-sport correctly — basketball O/U uses
  `FullIncludingOT`/"FT including OT" (806 ok), so basketball is NOT broken; its
  low O/U coverage (1 event vs 219 home_away) is OFF-SEASON liquidity, not this
  error. The "Full Time" errors are FOOTBALL double_chance pages (8 of 1415
  Full-Time period-sets = 0.5%) where the period div isn't present/ready within
  timeout — an EXPECTED, gracefully-handled scrape gap (market skipped, no
  crash, picks unaffected). ROOT CAUSE of the NOISE: app/ingestion/oddsportal.py
  `_ScrapeGapDowngradeFilter` already downgrades expected scrape-gap messages on
  the SelectionManager logger, but its `_NEEDLES` omitted
  "period target element not found", so that one leaked at ERROR (and inflated
  the monitor's error count = false alarm). FIX: added that needle (TDD
  RED→GREEN; SCOPED to "period ..." so a bookies-filter target miss stays at
  ERROR). NO scrape-behavior change. The needle is message-text-coupled to
  oddsharvester 0.3.0 like the other patches — re-verify on bump (pitfalls.md).

- 2026-06-18 (external-AI findings cross-checked — NO config change) — an
  outside review flagged a football "artifact mismatch": live devig is
  `differential_margin_weighting` (config.py:211) while `scripts/value_backtest.py`
  at `--min-odds 1.30` selects `shin`, and recommended regenerating the
  threshold artifact + promoting "best held-out CLV". REJECTED the action: that
  would RE-SELECT on the SPENT holdout (2425+2526) = data-snooping, which the
  doctrine forbids. The live config is the VALIDATED one (set by the stronger
  threshold-control process, not value_backtest.py's train-ROI sweep with
  n>=150 + analytic SE). value_backtest.py ITSELF notes (line 231) that below a
  1.6 floor "the sweep may pick a different (EQUIVALENT) devig" — and the
  penaltyblog 250M-line study + our own bake-off show the 7 devig methods sit
  within ~0.0002 RPS for 1X2, so shin vs differential_margin is noise. The 1.30
  floor is already held-out-validated (PR #13, barely binds at thr=0.03);
  "audit expects 1.60" is stale. Findings #2 (tennis stays visibility-only) and
  #3 (prefer threshold-control artifact over value_backtest.py) just CONFIRM
  current behavior. The repo-research half (nba_api / nflverse-data / nflreadpy /
  soccerdata-ClubElo as FUTURE feature sources for NBA/NFL; JeffSackmann tennis
  is CC BY-NC-SA = research-only) is a roadmap for sports that stay
  visibility/shadow-only and gated on forward CLV — not actionable now. Any
  real future devig/threshold change needs NEW data (season 2627), never a
  spent-holdout re-tune. The two stray `docs/research/*multisport*.md` files
  (agent side-effect of the multisport workflow, unreviewed, overlapping
  committed docs #21/#23) were deleted. Anchor-calibration diagnostic (PR #25)
  ran LIVE against the warehouse: 0 settled binary picks → INSUFFICIENT (clean
  honesty gate; SQL + model_probability column confirmed against the real
  schema). OPERATIONAL note: the compose `app` IMAGE was 29h stale (pre-PR#15)
  and crash-looped on `alembic upgrade head` ("Can't locate revision
  c3d8f1a6b240") because the DB is already at that head — NOT a code bug;
  rebuild the image before deploy (`docker compose up -d --build app`).

- 2026-06-18 (Pinnacle arcadia: capture totals + spreads, not just moneyline)
  — the arcadia straight feed is fetched with `primaryOnly=false` (full market
  set already on the wire) but previously extracted ONLY period-0 moneyline
  (`s;0;m`). Now `extract_market_quotes` ALSO captures the MAIN-line total
  (`s;0;ou;<line>` → Market.TOTALS, "Over 2.5"/market*detail "over_under_2_5")
  and spread/AH (`s;0;s;<line>` → Market.SPREADS, "{home} -1.5"/market_detail
  "asian_handicap*-1*5", keyed on the home handicap). `isAlternate` lines and
  period≥1 variants are excluded (main line = the sharp anchor). MoneylineQuote
  → MarketQuote (+market_key); the version change-gate is now keyed by
  (sport, event, market_key) so each line gates independently. Zero new
  requests (data was already fetched-and-discarded). Read-only, mints nothing,
  isolated `pinnacle*<sport>`namespace. **This is GROUNDWORK** — it accrues the
sharp OU/AH closing archive. Using it for CLV still needs (a) extending`resolve_pinnacle_close_snaps` to re-key OU/AH selections (today it re-keys
  only home/away/Draw → OU/AH closes are dropped at the re-key) + line-matching
  to the pick, and (b) flipping CLV_USE_PINNACLE_ARCHIVE (still false). Decided
  after a feature audit (OddsHarvester HISTORIC unused = future CLV-backtest
  gap; georgedouzas/sports-betting + kochlisGit/ProphitBet both rejected as
  off-doctrine outcome predictors). Markets config UNCHANGED (operator kept
  leagues=all + 4 core markets — all-leagues + all-markets would starve the
  slate via the odds-age gate, ~73s/match×18 tabs = multi-hour cycles).

- 2026-06-18 (odds floor 1.60 → 1.30, evidence-backed) — held-out floor sweep
  via `scripts/value_backtest.py --min-odds {1.60,1.30,1.01}` (train-sweep →
  single-shot test, the existing methodology). At the production edge threshold
  (thr=0.03) the floor is NEARLY NON-BINDING: 1.60→1.30→1.01 gives test n =
  61→62→62, ROI +21.1%→+22.4%→+22.4%, incCLV +0.106 throughout — high-edge
  sub-1.60 value bets barely exist (favorites priced efficiently). At the
  no-threshold baseline the floor DOES matter: dropping it pulls in ~440 extra
  sub-1.60 picks at NEGATIVE ROI (−1.37%→−1.59%). So 1.30 (the engine default)
  captures 100% of premium upside while guarding the noisy short-odds region;
  1.01 adds zero upside and only volume-tier downside → STOPPED at 1.30, not
  removed entirely. value_min_odds default 1.60→1.30 (config.py), tests +
  .env.example updated. NOTE: 2425+2526 is the spent holdout, so this is
  descriptive confirmation of a STRUCTURAL fact (the floor barely binds), not a
  fresh validation — a true protocol change would need live CLV / season 2627.
  Conclusion stands because the finding is structural, not a tuned parameter.

- 2026-06-18 (config defaults → VPS/local parity — DONE) — the committed
  `Settings` defaults now match the reference `.env` so a fresh deploy is wide
  out of the box (the local-vs-VPS divergence was pure per-`.env` config, not a
  bug): `oddsportal_football_leagues` + `oddsportal_basketball_leagues` →
  `"all"` (worldwide daily page; off-season yields nothing) with their market
  lists trimmed to the 4-key budget (`_enforce_all_leagues_market_budget`, so
  the worldwide scrape stays sub-hour); `oddsportal_tennis_leagues` → the
  in-season grass slugs (VISIBILITY-ONLY — still mints NO picks/alerts) with
  `oddsportal_tennis_markets="match_winner"`; `arcadia_enabled` → `True`
  (capture is GET-only and mints nothing, so on-by-default is safe — operator
  confirmed Cloudflare no longer blocks the VPS). `CLV_USE_PINNACLE_ARCHIVE`
  STAYS `False` (still gated on cross-source match-rate validation). Tennis
  grass slugs are SEASONAL — rotate in `.env` as the tour moves. Supersedes the
  "ARCADIA_ENABLED OFF by default" wording in the 2026-06-16 entries below and
  in ADR-0013. Tests updated (test_config, test_sports_enablement); `.env.example`
  - free-odds-sources doc updated; safety audit exit 0.

- 2026-06-16 (cross-source CLV matcher — BUILT, ADR-0014) — the deferred
  ADR-0013 step is SHIPPED: a PURE `app/resolution/` strict matcher attaches the
  Pinnacle ARCHIVE close to the matching OddsPortal pick. `match_event` =
  exact-normalized names (+ alias table) AND kickoff within a small day window,
  UNIQUE-or-None (NO fuzzy/containment/best-available; ambiguous->None;
  women/youth markers PRESERVED so "Arsenal Women" never matches "Arsenal";
  ordered=True rejects home/away swap; ordered=False for tennis). Clean-room
  from glass_onion (join) + soccerdata (alias pattern) + reep CC0 (alias data) —
  patterns/data only, ZERO code. `repositories.resolve_pinnacle_close_snaps`
  re-keys the matched archive close to the pick's event_id + selection
  vocabulary; `clv_trueup.finalize_closing_from_snapshots` injects it behind
  `CLV_USE_PINNACLE_ARCHIVE` (DEFAULT OFF — changes anchor_type/CLV for matched
  picks, evidence-gated; byte-identical when off). 25 tests (20 pure + 5 DB);
  ruff/mypy/safety green. 3-agent adversarial review (clv-auditor + integration
  - clean-room): integration/clean-room PASS; strictness found ONE MAJOR —
    the close cutoff used the PICK's kickoff not the matched ARCADIA event's, so a
    ±1-day-earlier arcadia fixture could admit a post-kickoff in-play price as the
    close (cardinal sin) — FIXED (cutoff = min(pick_ko, arcadia_ko)) + regression
    test; plus an unordered-degenerate guard made unconditional and a seed
    no-collision test. NEXT: validate the soccer match rate on live data before
    flipping the flag; then tennis (name-order) + NBA. v1 = soccer, moneyline.

- 2026-06-16 (repo sweep #2 — "best repos for the project"; full report
  `docs/research/repo-sweep-2026-06-16.md`) — 4-agent gated sweep, settled
  repos excluded. NO new runtime dependency, but 3 clean-room takes that
  DE-RISK the deferred cross-source CLV join. **CROSS-SOURCE MATCHER (the
  actionable win):** USSoccerFederation/glass_onion (BSD-3 — deterministic
  event JOIN: exact date+team_ids merge + ±3-day tolerance + matchday fallback;
  SKIP its TF-IDF cosine fuzzy passes, forbidden), probberechts/soccerdata
  (Apache-2.0, 1759★ — the `{alias→canonical}` dict + bidirectional
  add_alt_team_names/add_standardized_team_name PATTERN), withqwerty/reep
  (CC0 — 488K-people/45K-team alias DATA to seed it) → build a PURE
  `app/resolution/` module (port the algorithm/data, do NOT pip-install; extend
  app/settlement/results.py::normalize_team). **BACKTEST:** betcode-org/flumine
  (MIT) PIQ queue-aware fill model = adopt-pattern clean-room into
  app/backtesting/ (do NOT add flumine/betfairlightweight — live order
  placement). **ARCADIA ROBUSTNESS:** pinnapi/pinnapi (MIT) typed
  AuthError/RateLimitError + retry_after pattern for pinnacle_arcadia.py.
  **DEVIG/CLV: NO gap** — penaltyblog already has all 7 methods; mberk/shin =
  cross-check ref only; neeljshah/clvtrack = VAPORWARE (22-byte stub). **DATA
  GATE unchanged:** still NO free sharp+close for tennis/NBA (all paid; hoopR is
  ESPN-soft, no Pinnacle) → forward self-capture (shipped, ADR-0013) is the only
  path. AUTOBET REJECTS: rozzac90/pinnacle, chrisgillam/polymarket_gambot. (See
  also the detailed "GitHub discovery #2" Pinnacle-clients entry below.) DO NOT
  re-evaluate: glass_onion, soccerdata, reep, flumine, pinnapi, mberk/shin,
  clvtrack, p2w-math, pybettor, deltaray-io/kelly-criterion,
  prediction-market-backtesting, polymarket_gambot, hoopR.

- 2026-06-16 (Pinnacle arcadia capture — BUILT, ADR-0013) — the recommended
  clean-room job below is now SHIPPED: `app/ingestion/pinnacle_arcadia.py`
  (GET-only client + pure `parse_matchups`/`extract_moneyline_quotes` +
  `PinnacleArcadiaCapture`), wired as an INDEPENDENT scheduler job
  (`ARCADIA_ENABLED`, OFF by default) that runs ALONGSIDE the active
  `ODDS_SOURCE` and mints no picks. Took only the unlicensed repo's API FACTS
  (endpoints, sport ids 29/33/4/15, `s;0;m` period-0 moneyline key,
  American→decimal), ZERO code. Persists `bookmaker="Pinnacle"` period-0
  moneyline closes under an ISOLATED `pinnacle_<sport>` warehouse namespace
  (chosen because `ODDS_SOURCE` is single-select — a real source would replace
  OddsPortal — and AVAILABLE GAMES filters to soccer/basketball/tennis, so the
  archive can't pollute the dashboard/pick path). Change-gated on Pinnacle's
  per-market `version` int; the latest pre-kickoff row IS the close via the
  existing `closing_odds_from_snapshots` (no `is_closing` write — it's dead
  code). Guest `x-api-key` is OPTIONAL/empty (the 2 endpoints used need none) →
  no secret committed, gitleaks-clean. Verified LIVE (tennis/soccer/basketball
  245/101/28 quotes; soccer 303=101×3 confirms draw); 16 tests, ruff/mypy/
  safety green. NOT YET validation: turning the archive into NBA/tennis CLV
  needs (a) STRICT cross-source event resolution to attach closes to OddsPortal
  picks (fuzzy joins FORBIDDEN — wrong close = corrupted CLV) and (b) pick
  generation for those sports — both deferred. v1 = moneyline only.

- 2026-06-16 (tennis backtest OUTCOME-LEAK fixed — Codex review of PR #4) —
  scripts/sports/tennis_backtest.py loaded PSW/PSL + MaxW/MaxL with a FIXED
  `winner_idx=0` (the source lists odds winner-first), so the eventual winner
  was ALWAYS side 0 at selection time → any side-0 pick settled as a guaranteed
  win → held-out ROI was OUTCOME-LEAKED. FIX: new pure `assign_sides()` +
  a seeded per-tour-year coin in `_load_year` randomize which side the winner
  sits on, so the selector sees an order uncorrelated with the result
  (3 regression tests, incl. "side 0 is no longer a guaranteed win"). Leak-free
  re-run (train 2021-23 / test 2024-25, power devig thr 0.01): ATP held-out
  n=1073 ROI +4.9% [-1.4%,+10.5%], WTA n=1193 ROI +3.1% [-2.7%,+8.9%] — both
  CIs CROSS 0 (not conclusively profitable). VERDICT UNCHANGED: tennis has no
  closing line → CLV gate unevaluable → VISIBILITY-ONLY / 0 picks regardless of
  ROI; the leak only inflated the reported ROI, never the operational decision.
  Also fixed in the same PR: tennis_backtest now declared under a `backtest`
  extra (pandas + openpyxl for .xlsx); `POST /login` offloads the 600k-iter
  PBKDF2 to a worker thread (asyncio.to_thread) so a login burst can't stall
  the event loop + scheduler (Codex PR #3).

- 2026-06-16 (GitHub discovery — devig/Pinnacle repos) — **POTENTIAL
  GAP-CLOSER found: a FREE, accountless, PRE-MATCH Pinnacle feed exists** via
  the unofficial JSON API `guest.api.arcadia.pinnacle.com/0.1` (bulk
  `/sports/{id}/markets/straight?primaryOnly=false`). Verified in two repos'
  code. This is the first concrete artifact touching our documented biggest
  gap (a free live Pinnacle sharp anchor) — it could populate
  picks.anchor_type='pinnacle' (today mostly 'consensus') and enable FORWARD
  CLV against a true sharp line. CAVEAT: direct Pinnacle scraping = high ToS
  risk + DOM/endpoint fragility (same class as OddsPortal), guest x-api-key
  rotates. RECOMMENDATION (not yet built): clean-room a GET-only, rate-gated
  app/ingestion/pinnacle_arcadia.py forward-capture job (read-only,
  ToS-risk-accepted). **All 3 repos are UNLICENSED → idea-only, NO code may
  be copied:** ACHBIDHAN/Pinnacle_Football_Odds_Scraper (idea-only, top pick,
  safety-clean GET-only — the Pinnacle mechanism); NateDeMoro/
  prediction-market-ev-engine (**HARD REJECT bind — auto-places real bets,
  RSA-signed orders, credential storage**; read-only refs only: its Shin/
  bisection devig, the bulk arcadia endpoint shape, a calibration-haircut +
  live-refetch-before-decision pattern); jjc256/devigger (reject — crude
  devig, not an oracle). The free-Pinnacle endpoint logged in
  free-odds-sources.md.

- 2026-06-16 (NFL data gate + GitHub discovery) — **NFL = REJECT, now PROVEN
  by fetching the nflverse data (not just asserted).** nflverse games.csv
  (7.5k rows 1999-2026) has spread/total/ML odds but only ONE snapshot per
  market (no open/close columns), the source is unlabeled CONSENSUS (no
  Pinnacle/sharp), closing_lines.csv ends 2018 with no book id, the only
  opening file (initial_lines.csv) is 2021-only / single Australian book /
  price-less, and ESPN's API is all soft books, current-only. So NFL clears
  NEITHER gate condition (sharp anchor + close) — no backtest possible; only
  forward self-capture of Pinnacle (regions=eu) would work, same as
  NBA/tennis. nflfastR pbp is a rich FEATURE source but only for an
  outcome-prediction model (out of our line-shopping/CLV doctrine). Other
  named repos: Public-ESPN-API idea-only (soft, current-only), yfpy reject
  (fantasy, no odds), unravelsports reject (tracking GNN, no odds), nflow
  reject (unrelated workflow engine). GITHUB DISCOVERY (read-only sweep):
  nothing bindable — golden-mane-labs/Sports-Betting-Demo (partial: an
  odds-history open->close modal-extraction technique, mirrors OddsHarvester
  scrape_odds_history), ianalloway/awesome-sports-betting (idea-only: a
  free-data vetting checklist), iliyasone/ps3838api (reject-bind: Pinnacle V4
  JSON-shape reference for a future forward-capture client); all others
  reject. RECURRING CONCLUSION: the free historical sharp-anchor+CLOSE gap
  cannot be closed by any repo; the only doctrine path for new sports is
  prospective self-captured Pinnacle snapshots.

- 2026-06-16 (repo-bind + NBA-backtestability re-check, 2nd ask) — **DO NOT
  re-evaluate these 4 repos again; verdicts unchanged from 2026-06-11.**
  kyleskom/NBA-ML = REJECT (still NO LICENSE; only a single SOFT book via
  sbrscrape, no Pinnacle/close). georgedouzas/sports-betting = idea-only
  (market-avg only, no Pinnacle/close). kochlisGit/ProphitBet = REJECT
  (leakage 3 ways). NBA-Betting/NBA_Betting = idea-only/pattern (archived;
  only nba_api point-in-time snapshot + merge_asof(+1d) patterns liftable).
  None supplies a free historical SHARP-anchor+CLOSE feed; 3 are winner/ATS
  ML (wrong shape for line-shopping/CLV). **NBA is NOT historically
  backtestable for our CLV doctrine on any FREE data (verified by fetching):
  the flancast90 SBR archive (13,903 games 2011-2021, MIT) has only a single
  CONSENSUS close — no Pinnacle, no opening ML, no per-book; sbrscrape has
  Pinnacle but is LIVE-only (no archive); Kaggle dumps are SBR-consensus/ESPN
  soft, login-gated; The Odds API historical is paid/credit-spending.** So
  NBA = forward-only/visibility-only like tennis; the only doctrine path is
  prospectively SELF-CAPTURING near-tipoff Pinnacle (regions=eu) snapshots.
  Full evidence: docs/research/nba-repo-evaluations.md + free-odds-sources.md.

- 2026-06-12 (optimization round 3 FINALIZED — validated verdicts + hardening;
  full digest: `docs/research/optimization-round-3.md`) — validation upheld:
  **Track A consensus anchor STAGE** (train evidence reproduced exactly;
  anchor verified PS/BFE-free 40/40 vs raw CSVs; weaker than Pinnacle on
  shared matches → fallback-only; binding = live `anchor_type`-stratified
  CLV + 2627); **Track B AH STAGE-tooling / REJECT premium eligibility**
  (one-shot UNDERPOWERED n_labeled=10, dataset verified 40/40 + 0 moved-line
  label leaks; knobs stay default-off); **Track C live-evidence tooling
  ADOPT** (honest-n gates verified, 30+ tests); **Track D staking ADOPT the
  KEEP-default verdict** (byte-identical re-run, no variant passes at block
  10/20/50; criterion (B) is structurally near-unsatisfiable under
  proportional Kelly — "KEEP" means "no evidence to switch"). **No live
  defaults changed.** Validator fixes landed: (1) `run_ah_oneshot` now
  writes an INTENT marker before the first label/outcome read — a crash can
  no longer permit a second look (the 2627 one-shot inherits this); (2) the
  corrected power gate (selectable matches, not pool rows) is
  regression-tested label-blind (`tests/test_anchor_ah_backtest.py`);
  (3) `live_evidence_report` NULLS point estimates for insufficient strata
  at the source — no `/performance` consumer can read noise-level numbers
  (`app/backtesting/live_evidence.py`; n_roi must still be eyeballed on
  sufficient strata). **SPENT-HOLDOUT LEDGER (restated, binding):**
  consumed = 18 baseline leagues + EC/SC1/SC2/SC3 (1x2+ou25, 2425+2526,
  4 looks), the v2 fresh slice (never-loaded divisions), and — NEW this
  round — **the AH market 2425+2526** (one-shot 2026-06-12, marker
  `data/ml/AH_ONESHOT_CONSUMED.json`). Football-data "new leagues"
  (BRA/ARG/…) carry closing odds only → unusable for the protocol.
  **Remaining legitimate fresh domain: season 2627 ALONE.** Develop only on
  <=2324; pre-register every one-shot in code; 2425/2526 numbers anywhere =
  CONTAMINATED-REFERENCE.

- 2026-06-12 (AH 2425+2526 fresh domain CONSUMED — one-shot UNDERPOWERED;
  consensus anchor validated on train) — the pre-registered Asian-handicap
  one-shot (`scripts/ml/anchor_ah_backtest.py --oneshot-ah`, criterion
  frozen in code: thr\*=0.015 train-chosen, pass iff n_labeled>=100 and
  incCLV_max−2SE>0 and ROI>0) **executed 2026-06-12 and consumed the AH
  2425+2526 domain** (marker `data/ml/AH_ONESHOT_CONSUMED.json`). Result:
  n=27, n_labeled=10, ROI −10.96% [boot CI −48.1%, +28.1%], incCLV_max
  +0.0330 [CI −0.0019, +0.0687] → **verdict UNDERPOWERED — AH does NOT meet
  the premium bar**; a row-count power gate intended to cancel the look had
  a bound bug (compared pool rows 140 vs floor 100) — honest execution
  record in the script docstring. Binding AH verdict now = live shadow CLV
  - season **2627 alone**. AH scope facts: half-lines = 23.1% of AH-priced
    matches; close line == pre-match line on 60.3% of half-line matches (CLV
    labels only there). Track A (consensus anchor, TRAIN <=2324 only, maxavg
    1x2): consensus-anchored selection shows real incremental CLV vs its own
    null (thr 0.02: n=477, ROI +12.4% [+0.5,+24.6], incCLV_max +0.0348
    [+0.0262,+0.0443]) but is WEAKER than the Pinnacle anchor on the same
    matches at moderate thresholds (paired dCLV_max −0.0071 [−0.0132,−0.0011]
    at thr 0.015; no separation at 0.03) → consensus stays the FALLBACK, now
    trackable live via `picks.anchor_type` (pinnacle/sharp/consensus). The
    football-data "new leagues" feed (BRA/ARG/…) was verified 2026-06-12 to
    carry CLOSING odds only → no Track A one-shot exists; binding consensus
    verdict = live anchor-stratified CLV + 2627. Dataset v3
    (`--anchor-consensus --ah`, `value_candidates_v3.parquet`, 95,928 rows)
    is additive; v1/v2 artifacts byte-identical (proven by full rebuild).

- 2026-06-12 (value filter v2: SHADOW-CANDIDATE, spent-holdout kept) —
  **v2 retrain ships annotation-only; verdict stage-v2-shadow** (full
  digest + numbers: `docs/research/premium-tier-v2.md`). Discipline held:
  2425+2526 NEVER loaded (trainer filters + asserts at load); every number
  is train-OOF (<=2324) or the pre-registered FRESH one-shot
  (EC/SC1/SC2/SC3). Selected model `lgbm_v1feat_sweep_draw81`: pooled OOF
  log-loss 0.64968 vs v1's 0.65175 — **hyperparameter lift only; the 37
  new features (rolling form, Understat xG, devig deltas, odds_band) gave
  NO lift** (honest negative, recorded in the manifest); XGB challenger
  refused by the pre-registered rule. Fresh one-shot: META transports
  (incCLV_max +0.0338 vs null) but does not separate from plain
  edge>=0.03 (overlapping CIs). **Manifest verdict is `CANDIDATE` — the
  trainer can never emit ADOPT; binding verdict = live shadow CLV + the
  one-shot fresh 2627 season.** Wiring: loader gained
  `VALUE_ML_MANIFEST_ALLOW_SHADOW` (+ filename overrides) — a non-ADOPT
  manifest loads ONLY with that flag, is marked `shadow=True`, and can
  never demote (pipeline branch + composition root both refuse;
  enforcement requires a true ADOPT manifest). Config defaults still point
  at v1 ADOPT artifacts. **How to apply:** to shadow v2 live set the three
  env overrides in `.env.example`; flip verdict to ADOPT only with §5
  evidence of the digest attached; 2425/2526 numbers anywhere =
  CONTAMINATED-REFERENCE, never headlines.

- 2026-06-12 (ML value filter: ADOPT, shadow-first) — **meta-labeling
  SECONDARY model over the value signal adopted; enforcement OFF by
  default** (full evidence + protocol: `docs/research/ml-value-filter.md`;
  artifacts gitignored in `data/ml/`). One-shot holdout 2425+2526
  (consultation #4, declared final — binding metric incCLV vs Max close,
  NOT ROI): META q>=0.725 n=396, ROI +12.0% [CI −1.6,+26.7], incCLV_max
  +0.0357 ± 0.0075 (2SE) — beats thr=0 null, the volume baseline (+0.0138),
  and the per-cell threshold control (+0.0082); all four pre-registered
  gates passed. **Doctrine intact:** this scores value CANDIDATES (P(beats
  the vig-free Max close)), never match outcomes — winner-prediction ML
  remains forbidden. **Wiring:** `app/models/value_filter.py` (ADOPT-only
  manifest gate, lazy ML imports, numpy calibrator replay);
  `run_value_pipeline` scores AFTER the edge gate; scope = 1x2/ou25, 18
  trained leagues, named sharp anchor, odds >= 1.6 — out-of-scope is
  unscored, never vetoed. `VALUE_ML_FILTER=false` (default) annotates
  scores only (`picks.value_filter_score`, dashboard "ML 0.xx");
  `true` demotes sub-q\* premium picks to the volume shadow tier. **How to
  apply:** keep flag OFF until score-stratified LIVE CLV confirms; retrain
  when `odds_snapshots` reaches scale (true intraday distribution); any
  protocol change needs the fresh 2627 holdout — 2425+2526 is spent.

- 2026-06-11 (Wave-4 repo verdicts) — **six user-mandated repos evaluated,
  NOTHING adoptable as a dependency** (full tables:
  docs/research/betting-repo-research.md Wave 4 + nba-repo-evaluations.md).
  ProphitBet REJECT 1/10 (leakage-inflated eval: shuffled k-fold +
  closing-odds features + SMOTE-before-isotonic — never cite its numbers);
  AlphaPy IDEA-ONLY 2/10 (abandoned for paid fork, zero tests, random-KFold
  core); georgedouzas/sports-betting IDEA-ONLY 2.5/10 — **DOWNGRADES the
  Wave-3 'walk-forward harness pattern' note**: its backtester settles at
  decision odds, weaker than our walkforward-backtest skill; only the
  TimeSeriesSplit-or-TypeError guard pattern survives;
  GastonDeMichele/Polymarket-Sports-Bot REJECT 0/10 — **repo does not exist**
  (404; SEO-spam cluster, see pitfalls.md); kyleskom + NBA_Betting verdicts
  re-confirmed (see nba-repo-evaluations.md). Survivors for phase 5:
  NBA_AI injury-PDF loader + ESPN lines client patterns, feature checklists,
  and `sbrscrape` (needs its own evaluation before any use).

- 2026-06-11 (NBA_AI repo verdict) — **NBA-Betting/NBA_AI = PARTIAL: mine
  data loaders, reject modeling core** (full evaluation:
  docs/research/nba-repo-evaluations.md, score 4/10, MIT, de facto frozen
  since 2026-04-14 "stable release, no active development"). Safety clean
  (GET-only; zero placement/login code). Leakage discipline is GOOD (strict
  pre-game prior-states cutoff, no lines in features) — better than both
  sibling repos. Adopt patterns for phase 5: (1) official NBA injury-report
  PDF loader (nba_official_injuries.py — free official source, granular
  status/body-part, URL-format + 403 quirks solved); (2) ESPN
  scoreboard/summary free NBA lines client with lines-lock-at-tipoff caching
  (betting.py); (3) rest/B2B/game-frequency + time-decay feature cross-check
  (features.py). REJECT: ML spread-prediction core (our backtests show the
  approach loses), zero calibration (hardcoded logistic win-prob, no
  isotonic/Brier/CLV/devig), PyTorch Phase3/Phase5 stacks (GPU, no
  checkpoints, orthogonal to LightGBM-first ADR-0005), Covers.com scraper
  (ToS-grey, UA spoofing — reference-only).

- 2026-06-11 (latest) — **Live pick revalidation SHIPPED**: every poll
  re-prices ALL open picks. In-window picks from the cycle's own snapshots
  (revalidate_open_picks — replaces the 30-min clv_trueup job, which was
  REMOVED as redundant double-scraping); off-window picks (taken weeks
  ahead) via direct match-page scrapes (fetch_match_odds + match_links,
  external_ref IS the oddsportal URL; cap 25/cycle, sport-segment filter).
  New picks columns current_odds/current_edge/revalidated_at (migration
  a3c9d1e7b2f4, APPLIED to dev DB). current_edge = fresh_fair − 1/current
  price at the pick's own book (best book fallback). Dashboard odds cell:
  "now X.XX — still value/thin/edge gone". RESTART the app after pulling.

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

- 2026-06-16 (GitHub discovery #2 — maintained Pinnacle clients + free
  sharp+close tennis/NBA sweep) — NOTHING bindable; standing conclusions hold.
  ROBUSTNESS REFERENCE for app/ingestion/pinnacle_arcadia.py:
  **pinnapi/pinnapi** (MIT, pushed 2026-06-11, python/src/pinnapi/client.py)
  = ADOPT-PATTERN (read-only REST/SSE client, NO autobet). Liftable ideas:
  (a) typed error hierarchy PinnapiError->AuthError/RateLimitError carrying
  HTTP status + parsed payload + retry_after (we currently raise a single
  PinnacleArcadiaError on any non-200 — could split 401/403 vs 429 vs 5xx);
  (b) explicit 429 handling that reads retry_after_ms and surfaces seconds-to-
  reset (our tenacity backoff is blind to Retry-After); (c) SSE reconnect loop
  that re-raises auth/plan errors immediately but backs off on transient
  ConnectionError/Timeout/ChunkedEncodingError (only relevant if we ever add a
  streaming feed). The pinnapi SERVICE itself FAILS the data gate (paid proxy,
  $99/mo for streams, free tier 100 req/day, NO historical) — pattern-only.
  Its README confirms Pinnacle CLOSED its public developer API on 2025-07-23;
  our guest.api.arcadia.pinnacle.com path is the separate web-client backend.
  REJECT (autobet): **rozzac90/pinnacle** (56*, MIT, pushed 2022-10-27) — the
  most-starred Pinnacle client but endpoints/betting.py has place_bet()/
  place_special_bet() POSTing /v2/bets/straight orders -> autobet_risk reject;
  its marketdata/referencedata endpoints are the DEAD V1 API anyway.
  REFERENCE-ONLY (fail data gate, not deps): cengizmandros/odds-arb-scanner
  (no-license, the-odds-api wrapper, current-snapshot soft aggregator, no
  Pinnacle-direct, no close/historical — roadmap lists CLV as TODO; redundant
  with our odds_api.py); Danymcflyy/OddsTracker (MIT, Next.js/Supabase
  closing-odds dashboard built on the PAID OddsPapi aggregator — a capture-
  scheduling pattern at most, fails free+sharp gate). FREE sharp+close for
  tennis/NBA: STILL NONE (exa sweep). Every source carrying BOTH a Pinnacle
  anchor AND close is PAID: bettingiscool/PinBook/prop-line/SharpAPI (price-
  redacted free tiers), Odds Warehouse ($79 one-time, consensus open/close not
  Pinnacle). ParlayAPI's free 'pinnacle' rows are its OWN forward daily self-
  capture (== what we already built), not a historical archive. hoopR
  (sportsdataverse/hoopR, 137*, NOASSERTION license, pushed 2026-06-13)
  espn_basketball_event_betting_helpers.R DOES expose home/away_team_odds_open
  - \_close per provider, but the provider set is ESPN's SOFT books (no
    Pinnacle) -> fails data-gate condition 1 (sharp anchor); same class as
    nflverse consensus. CONCLUSION UNCHANGED: the only doctrine path for NBA/
    tennis remains prospective self-captured Pinnacle arcadia snapshots (already
    shipped). DO NOT re-evaluate: pinnapi, rozzac90/pinnacle, odds-arb-scanner,
    Danymcflyy/OddsTracker, sportsdataverse/hoopR.

2026-06-17 — SHADOW match-rate harness (ADR-0014 validation precondition) BUILT
on branch feat/resolution-match-rate-shadow. Pure aggregator
app/resolution/shadow.py (ShadowOutcome / GroupRate / MatchRateReport +
summarize*match_rate; numpy/stdlib only, inside the pure-math boundary) +
impure DB reader repositories.shadow_match_rate_outcomes, which runs the SAME
strict matcher app.clv_trueup uses at settlement over picks with a known
kickoff (Event.starts_at NOT NULL, optional `since`), writes NOTHING and
attaches no close — it only records matched + candidates_in_window. Bound to
the app two ways: GET /resolution/match-rate (auth, read-only, ?days=N) and
scripts/reports/resolution_match_rate.py (--days / --json). DRY: the
pinnacle*<base> namespace logic now lives once in shadow.arcadia*base_sport
(clv_trueup imports it; its private \_ARCADIA_SPORTS removed). Diagnostic split
is the point — no_archive_candidates = COVERAGE gap, unmatched_with_candidates
= ALIAS gap (extend aliases_seed.json). LIVE RESULT 2026-06-17: 56 picks, 0
matched, ALL 56 = no_archive_candidates — the pinnacle*<sport> archive is
EMPTY (ARCADIA_ENABLED=false, never captured). So the blocker before flipping
CLV_USE_PINNACLE_ARCHIVE is COVERAGE, not aliasing: enable ARCADIA_ENABLED,
let it capture a slate, then re-run to read the real match rate. 9 pure + 2 DB
tests; ruff/mypy/full-pytest/safety all green. NOT committed (left on the
branch for review).

2026-06-17 — OPTIMIZATION PASS (branch feat/resolution-match-rate-shadow,
committed). Goal: "train/backtest/optimize to best results + friendlier
dashboard." A 4-agent audit (dashboard/code/modeling/safety) returned the
decisive verdict, NOW THE STANDING RULE: **the value strategy is at its
validated statistical CEILING — there is NO honest modeling slack left.** The
production config (differential-margin devig, edge>=0.03, odds>=1.60) is
conclusively +CLV (holdout n=61, incCLV +0.106 >2SE, beats Max close, monotone
across 7 devig methods); the asymptotic CLV (~0.11 premium / ~0.02 volume per
bet) is set by immovable factors (best-of-N premium already stripped vs Max
close, the exogenous Pinnacle-to-best gap, the win rate at that edge — not
improvable without outcome prediction, which is forbidden + backtested
negative). Seasons 2425+2526 are SPENT (consulted 4x); 2627 lands ~Jun 2027.
=> The ONLY doctrine-safe frontier is COVERAGE, OBSERVABILITY, EXECUTION
fidelity, and dashboard COMPREHENSION — none statistical. Reproduced this
session: value backtest n=62 ROI +22.4% incCLV +0.1066; ML value-filter retrain
VERDICT ADOPT (4 criteria, ECE 0.014); tennis ATP+WTA visibility-only (no
closing line -> CLV gate unevaluable). SHIPPED (all gated/tested): dashboard
archive-coverage panel (lazy GET /resolution/match-rate), onboarding CLV
explainer banner (dismissible/localStorage), colorblind-safe badge glyphs +
--dim/--faint contrast lift, and tests/test_value_no_closing_leak.py (locks the
no-closing-into-decision invariant on bets_for). DO-NOT-DO (reaffirmed): no
re-tuning VALUE_MIN_EDGE/DEVIG/ODDS/features on the spent holdout (p-hacking);
no ML-v2 CANDIDATE->ADOPT promotion (shadow only); no outcome/goal model; no
index migration on Event.external_ref (already unique); no ROI-delta-justified
changes on small n; no pure-math-boundary churn. DEFERRED (available, lower
value): pipeline \_fair_probabilities fusion (~5-10% cycle time, touches the
validated hot path), ETag/304 on /picks+/performance, live-CLV drift ALERT
gates (need live data, alert-only never auto-tune), more aliases as Arcadia
coverage accrues.

- 2026-06-21 (basketball research sweep — NBA/EuroLeague/WNBA/NBL, NEW repos +
  literature; quant-sports-researcher) — extends nba-repo-evaluations.md (kyleskom/
  NBA_AI/NBA_Betting already settled). NEW VERDICTS: (1) **genehuh39/sports-projection**
  (MIT, 0★, push 2026-04-30) = best new NBA repo, ON-DOCTRINE — shift(1).rolling +
  sorted walk-forward CV (strict train<test windows, explicit leakage comment),
  PlattCalibrator (sigmoid>isotonic on small sets), Brier/log_loss, Kalshi+Polymarket
  READ-ONLY price compare, PAPER-TRADE only (no autobet). CAVEAT: value_engine uses
  raw single-side implied (NO devig — we have penaltyblog); 0★ unproven. VERDICT
  reference (mine walk-forward+calibration patterns, don't depend). (2)
  **ianalloway/nba-ratings** (pkg nba-edge, MIT, push 2026-06-18) = tiny clean Elo+
  logistic+Kelly primitives lib w/ CI+tests; sibling nba-edge (archived,1★,"top
  contributor: claude") has SUSPICIOUSLY-CLEAN backtest table (55%+,+6-7%ROI,
  +1.8-2.1%CLV) = AI portfolio project, DO NOT TRUST numbers. Kelly hardcaps 0.25.
  VERDICT reference-only (we have kelly-bankroll+odds-math). (3) **conorwalsh99/
  ml-for-sports-betting** (MIT, archived 2025, 2★) = VERIFIED reproduction code of
  Walsh&Joshi 2024 (calibration vs accuracy); has model_selection.py+simulate.py+8
  test files. VERDICT reference (cite the paper, mine the sim harness). (4)
  **giasemidis/euroleague_api** (86★, push 2026-04, GPLv3/NOASSERTION) = canonical
  EuroLeague+EuroCup wrapper (PBP/boxscore/shots) BUT COPYLEFT -> reference/clean-
  room only, NOT adoptable. (5) **sfendourakis/py-euroleague** (MIT, 1★, push
  2026-01) = real async EuroLeague v1/v2/v3+live client, MIT-liftable but unproven.
  (6) **sportsdataverse/wehoop** (R, NOASSERTION, 45★) WNBA/WBB ESPN data — R not
  our stack; sportsdataverse-py already settled as adopt-pattern for ESPN. REJECTS:
  davideganna/NBA_Bet (RF+Elo, MIT, reaches bookmaker for ODDS only=GET, no autobet
  found, but 7★ toy 2024); whisdev/NBA-prediction-sports-betting (TS, ONNX, accuracy-
  led); IdoBelyaev/UndrOdds.nba (0★ "place bets one click"=journaling UI, reject);
  paulkellar2023/\* + dozens of "nba-betting-model" = low-quality/SEO. LITERATURE
  (load-bearing): Walsh&Joshi 2024 MLWA 16:100539 (calibration sel +34.69% vs
  accuracy -35.17% ROI, NBA, CC-BY) = our calibration-first backbone; Wang 2026
  MDPI Information 17(1):56 (uncertainty-aware RNN+MC-dropout, strict chrono split
  +SHAP leakage check, 0.3-Kelly sim -> EDGE IN MONEYLINES, spreads/totals near-
  efficient) = validates model-as-screen + market efficiency; Paul&Weinbach / early-
  season totals bias (56.72% vs close, 20yr) = practitioner-known soft inefficiency.
  DATA GATE for new basketball leagues: OddsPortal covers EuroLeague (2003/04+) so
  OddsHarvester extends free (no Pinnacle there as always); Odds API has
  basketball_euroleague(2020)/wnba(2022)/nbl(2024)/nba w/ Pinnacle on PAID; arcadia
  Pinnacle = free live sharp anchor for NBA going forward (EuroLeague/WNBA/NBL arcadia
  coverage = verify in-app, not researchable). NO free historical Pinnacle open+close
  for any basketball league (unchanged gate). See research output for full brief.

- **OddsPortal lighter-scrape (non-Playwright) repo eval (2026-06-23):** Goal was
  a proven repo/technique to replace OddsHarvester full-Chromium render with a
  curl_cffi/httpx -> fb.oddsportal.com .dat JSON-feed client. VERDICT: no repo
  reliably solves the CURRENT feed. The exact feed+token technique is implemented
  only by **jckkrr/Unlayering_Oddsportal** (2023, unlicensed, reference-only) and
  **borewicz/oddsportal** (2016, unlicensed, reference-only) — both on the OLD
  PLAINTEXT feed. OddsPortal changed the feed ~late-2024 to
  /feed/match-event/...dat returning base64+AES-256-CBC (PBKDF2-HMAC-SHA256, 1000
  iters; password+salt in app.js). The only public working decrypt is SO #79241543
  (CC-BY-SA, not a repo). Token (xhash) is NOT computed — it is scraped from the
  match-page PageEvent(...) script and urllib.unquote'd (rotates ~minutes, re-derive
  per match). All actively-maintained 2026 repos are browser-based: OddsHarvester
  (Playwright, our current), Mg30 (Playwright/Node), djibril-marega (Playwright),
  gingeleski/BeatTheBookie/pretrehr (Selenium). SAFETY FLAG: AinaRazafinjato/value-
  bets-scraper stores ODDPORTAL_USERNAME/PASSWORD — do NOT copy. DECISION: build our
  own client (curl_cffi + Referer + x-requested-with), learn from jckkrr/borewicz +
  the SO AES recipe; keep Playwright fallback. Brittleness moves to app.js password
  rotation, not removed. Full brief: docs/research/oddsportal-lightweight-scrape-2026-06-23.md

- **OddsPortal JSON-feed cut-over hardening — NO Playwright fallback (2026-06-24):**
  Supersedes the "keep Playwright fallback" note above for the ODDS path (operator
  instruction). With `oddsportal_use_json_feed=true`: (1) per-match odds come ONLY
  from curl_cffi; the dated LISTING runs with `markets=[]`, so OddsHarvester does
  NOT do the per-match Playwright odds extraction (`_scrape_match_data` only scrapes
  markets `if markets:`) — the CPU savings are REAL, not cancelled by a paid-for
  fallback. (2) A per-match JSON failure/empty LOGS (type only) + SKIPS the match (a
  scrape gap, like a benign DOM miss) — no `_convert_match` fallback. (3) A
  key/bundle rotation fails CLOSED with a LOUD WARNING in `fetch_match_feed` (the
  RuntimeError version guard is caught + logged, not swallowed at INFO), since a
  JSON-wide failure is now invisible without a fallback. (4) BLOCKER fix: the feed
  keys odds by NUMERIC provider IDs; they are mapped to canonical bookmaker NAMES
  via a GET-only, per-cycle-cached registry parsed from OddsPortal's `bookmakersData`
  JS bundle (`/res/x/bookies-*.js`, `WebName` field; URL read from page HTML, map is
  static-ish). An UNKNOWN id is SKIPPED, NEVER persisted numeric (a numeric bookmaker
  silently breaks sharp/soft classification, CLV join, devig grouping — value.py
  SHARP_BOOKS keys on names like "Pinnacle"/"Betfair Exchange"/"bet365"). NOTE: the
  exact id->name spellings for high IDs (707/263/841/44) are read live from WebName,
  not hardcoded (21 is Betfred, NOT Betfair — do not guess). (5) captured_at now
  sources the feed `d.time-base` (provider observation time), matching the Playwright
  `scraped_date` semantics. score_only (finished-score) stays Playwright header-only.
  Still OFF by default; READ-ONLY GET-only. Files: app/ingestion/oddsportal_json.py,
  app/ingestion/oddsportal.py, app/config.py, app/scheduler.py.
