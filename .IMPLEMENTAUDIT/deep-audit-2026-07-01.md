# Deep Audit — sharp-ev-picks / betting-ai — 2026-07-01

Method: session-run checks (safety audit, ruff, mypy, pytest 2,080 tests, alembic heads) + 19-agent multi-pass audit
(9 dimensions, ~2.3M tokens read), with every critical/high finding independently adversarially verified by a
fresh agent instructed to refute it. Backtest command deliberately NOT run (network fetches + spent-holdout
discipline). Alembic upgrade NOT run (needs DB); `alembic heads` static check only.

## Section 1 — Executive verdict

**Overall: PARTIALLY SAFE. Football premium picks: defensible. Basketball/tennis/cup-competition settled
evidence: contaminated at the source and should not be trusted as-is. The +22.4% ROI headline: optimistic,
not live-parity.**

The core is much better than typical for this domain — verified directly, not assumed: all 8 devig methods
executed in-venv sum to 1.0, preserve order, converge; EV/Kelly formulas correct with commission netting on
the bet side only; CLV is log-ratio with identical devig chokepoint for mint and close; fake-CLV guards
(circular fill-book, tautology, fabrication bounds, asymmetric devig fallback) enforced at write AND read;
settlement is idempotent with a silent-empty-feed refusal; UTC-aware datetimes throughout; APScheduler jobs
uniformly max_instances=1+coalesce; safety audit + config tripwires + CI genuinely fail-closed for the paths
that exist today; secret hygiene verified (type-only exception logging, SecretStr, pinned httpx loggers).

**Top 5 risks (all verified):**
1. **Wrong-settlement contamination** — three confirmed classes silently poison the ROI/CLV evidence the
   platform's promotion decisions depend on: (a) ScoreBook ±1-day tolerance settles NBA back-to-backs with the
   previous game's score; (b) tennis retirements/walkovers settle as normal results, with a leading *partial*
   set counted as won; (c) extra-time goals contaminate 90-minute markets from the martj42 feed and scraped
   cup finals. All three concentrate in exactly the segments (basketball/tennis/cups) whose forward evidence
   drives go/no-go.
2. **Daily exposure cap is bypassable** — a premium pick denied capacity is persisted at full stake and
   alerts one poll cycle later via the duplicate path with zero reservation; during a DB outage the cap is
   entirely unenforced while alerts still flow.
3. **Backtest ≠ live** — the validated ROI fills at football-data's gross *Max across all books* (incl.
   exchanges) while live fills only at soft books net of commission; the live config cell (power devig,
   min-odds 1.30, 5.0 ceiling, 0.20 max-edge) was never itself holdout-scored; the >2SE CLV verdict uses an
   i.i.d. SE over correlated same-match 1X2+OU pairs (n=62).
4. **Promotion gates are config-flips, not evidence gates** — `NBA_EXPERIMENTAL=false` (or
   `ODDS_SOURCE=odds_api`, which never populates experimental_sports) promotes basketball with no CLV
   evidence check; exchange liquidity is captured but never enforced at anchor-use time, so a NULL-liquidity
   Betfair inline row can anchor a premium pick.
5. **Monitoring fails open exactly when needed** — dead-man's-switch/self-audit alerts are marked delivered
   before delivery; `settle_results` re-downloads ~36 CSVs + 28 ESPN feeds every 30s (ban → settlement
   degradation); Redis client has no socket timeout (wedge risk); SIGTERM tears clients down under in-flight
   jobs.

**Top 5 fixes:** (1) settlement score-semantics hardening (ambiguity veto on ±1-day, tennis retirement
quarantine, ET filtering); (2) close the exposure-cap duplicate-dispatch hole + fail closed on DB outage;
(3) soft-book-net backtest variant and re-anchor the headline; (4) runtime read-only allowlist in Betfair
`_rpc` + wire the liquidity floor + evidence-checked promotion; (5) split the 30s settle loop from the feed
fetch, confirm-before-consume alerting.

## Section 2 — Real production path (verified against live .env)

Live strategy: `PICK_STRATEGY=value` + `ODDS_SOURCE=oddsportal` → **`run_value_pipeline`** (app/pipeline.py:843)
is the live path. `run_pick_pipeline` (Dixon-Coles model join) + refit jobs are dead in prod.

| Stage | Where |
|---|---|
| Scheduler | app/scheduler.py:257 `build_scheduler`. Jobs: poll_odds 60s (live .env; default 300); settle_results 30s; capture_finished_scores 60s; self_audit 600s; calibration_drift cron 05:30; capture_pinnacle_arcadia 120s; capture_betfair_exchange 300s (live on); capture_betfair_api_shadow 300s (live on, promote OFF); snapshot_bankroll = stub |
| Ingestion | OddsPortalLoader JSON feed (oddsportal_json.py, curl_cffi); sharp archives: pinnacle_arcadia.py, betfair_exchange.py, betfair_api.py (anchor+CLV only, no picks). odds_api branch inert. bsp/beatthebookie/tennis_data/nfl_data/sbr_nba = backtest-only, never scheduled |
| Normalize/persist | EventDirectory (ingestion/base.py) → _persist_snapshots (pipeline.py:382) → persist_odds_snapshots (repositories.py:1427). Tables: sports/leagues/teams/events/odds_snapshots |
| Matching | resolution/matching.py — match_event_hardened:521 (Pinnacle archive, Betfair API shadow), match_event:389 (betfair back-snaps). League deliberately None; identity = normalized name + alias table + marker vetoes + kickoff bound |
| Devig/fair | edge/value.py — event_fair_probs → anchor_fair_probs_with_provenance:349; VALUE_DEVIG=power; sharp anchor _named_sharp_anchor:598 else consensus median; archive injection ON (VALUE_SHARP_ANCHOR_FROM_ARCHIVES=true, ≤4h fresh) |
| Gates | pipeline.py:1023-1369: in-play drop → find_value_bets_with_fair (min_odds 1.30, max-edge cap) → freshness ≤300s → tier (premium ≥3%, volume ≥1.5%) → moneyline ceiling 5.0 → require-sharp-anchor ON → experimental-sport demotion → steam gate ENFORCING live |
| Stake | risk/staking.py:129 — Kelly ×0.25, 2% per-bet cap; DailyExposureLedger 5% daily / 4% per-event, premium only, seeded on restart |
| Alerts | notifications/dispatcher.py — Telegram+webhook, Redis dedupe TTL 7d. Premium only |
| Settlement | settlement/engine.py:427 run_settlement_cycle — void stale → football-data CSV + ESPN + scraped finals → settle_open_picks → result_tracking (idempotent) |
| CLV | clv_trueup.py — revalidate_open_picks in every poll cycle; final close at settlement via finalize_closing_from_snapshots with Pinnacle/Betfair archive closes ON live |
| Dashboard | api/routes.py — /health:652, /picks, /performance:829, /resolution/match-rate, manual result endpoints |

**Architecture-doc drift:** docs/architecture.md still describes The Odds API as primary, the model-join
pipeline, 300s poll, 24h dedupe, detected_edges auditing — all wrong vs live. Tables detected_edges,
model_predictions, model_versions, bankroll_snapshots, backtest_runs are never written on the live path.
GatePolicy fields MIN_EDGE/MIN_EV/MIN_CONFIDENCE/MIN_LIQUIDITY are injected but only max_odds_age_seconds is
read by the value path — MIN_LIQUIDITY=0 in .env is a silent no-op.

## Section 3 — Critical bugs (verified high; nothing rated critical survived verification)

**3.1 Daily exposure cap bypass — HIGH, confirmed**
app/pipeline.py:1379 (`run_value_pipeline`/`_reserve_for_outcome`/`_maybe_persist`), repositories.py:2608.
Persist-first ordering writes the full-stake premium row; zero-grant only skips dispatch; next cycle the same
candidate returns 'duplicate', reserves nothing, and is unconditionally dispatched — dedupe can't stop it
because the key was never claimed. Same shape in run_pick_pipeline:559-574. Effect: the daily cap delays
alerts by one poll interval instead of blocking; daily premium exposure is unbounded. Also: during a DB
outage every pick is 'unpersisted' → alerts flow with zero ledger accounting and no DB row (invisible to
settlement/CLV). Fix: on zero grant, demote the persisted row (stake 0 / tier volume, 'daily-cap' marker)
and suppress duplicate dispatch of zero-stake rows; attempt ledger.reserve before dispatching a 'duplicate';
fail closed (no alert) on 'unpersisted'.

**3.2 Wrong-game settlement via ±1-day score tolerance — HIGH, confirmed**
app/settlement/results.py:130-137 `ScoreBook.lookup` returns the first exact-key hit at dates (0,−1,+1) with
no ambiguity check. NBA/WNBA back-to-back same-pairing: game-1 (D−1) is in the book while game-2 is in play;
picks are settle-eligible at kickoff+2h and settlement runs every 30s with ESPN merged — the wrong score
settles deterministically. Fix: accept an adjacent-date hit only when no same-pairing fixture exists on the
pick's own kickoff date; raise the basketball settle floor to ≥4h.

**3.3 Tennis retirements/walkovers settle as normal — HIGH, confirmed**
app/ingestion/espn_scores.py:53-54 gates only on `status.type.completed` (no RETIRED/WALKOVER detail check);
`_sets_won`:89-97 counts a leading partial set as won. Live: ENABLE_UNVALIDATED_PICKS=true mints tennis
shadow picks that auto-settle via ESPN. The only retirement quarantine in the repo is in the offline
tennis_data.py loader. Fix: skip competitions with retirement/walkover status detail; only count completed
sets (≥6 games etc.); require a complete Bo3/Bo5 pattern before emitting FinalScore, else leave pending.

**3.4 Extra-time contamination of 90-minute markets — HIGH, confirmed**
app/ingestion/international_results.py:65-93: martj42 scores are documented as *including extra time*;
ingested verbatim, settled by outcomes.py as 90' 1X2/totals. Scraped cup finals equally ET-blind. Fix: leave
knockout-stage internationals pending/manual; capture OddsPortal's ET marker in the scraped-score path and
refuse ET-decided finals.

**3.5 settle_results hammers free feeds every 30s — HIGH, confirmed**
app/scheduler.py:671-683 + settlement/engine.py:470-479: each 30s run does ~36 football-data.co.uk CSV GETs +
28 ESPN GETs, uncached (~2,880 cycles/day) — the job's own comment claims "cheap, DB-only". Ban risk on the
settlement path. Fix: 30s loop consumes DB scraped scores only; move feed fetch to hourly; or 30–60min TTL
cache keyed by URL.

## Section 4 — High-priority issues (verified severities)

- **Betfair read-only is textual, not structural** (betfair_api.py:602, adjusted MEDIUM but top-value fix):
  `_rpc(op, params)` is a generic dispatcher POSTing to the same JSON-RPC endpoint that serves placeOrders
  with a full-capability session (Betfair has no read-only scope). All enforcement is literal-string greps
  (safety_audit.sh check 1 + module-scan test) — a concat/getattr-spelled op bypasses both. No current caller
  is non-constant, so not exploitable today. Fix: `_ALLOWED_OPS = frozenset({listMarketCatalogue, listMarketBook})`,
  raise before any HTTP; test with MockTransport that fails on contact; audit presence-check for the allowlist.
- **Basketball promotion bypass** (scheduler.py:393): NBA_EXPERIMENTAL=false is a bare flag flip; the
  odds_api branch never populates experimental_sports at all. The CLV-readiness gate is reporting-only.
  Fix: promotion requires SportMarketClvGate pass in code, not env.
- **Exchange liquidity unenforced at anchor-use** (value.py:648): exchange_min_liquidity gate unwired (no
  Settings field feeds it); dominant Betfair inline rows carry NULL liquidity. A thin/ghost Betfair line can
  anchor premium under require-sharp-anchor. Fix: wire the floor; treat NULL-liquidity exchange rows as
  non-anchor-grade (demote to consensus/shadow).
- **Pinnacle AH key mismatch** (pinnacle_arcadia.py:516): `asian_handicap_+1_5` never matches the OddsPortal
  key `asian_handicap_1_5` → Pinnacle anchor silently absent for all positive home AH lines, biasing the AH
  visibility evidence one-sided. Fix: normalize '+' out of the signed-token key.
- **Tennis fuzzy accepts wrong sibling** (matching.py:504): 'cerundolo f' vs 'cerundolo j' passes JW 0.964 /
  token_sort 90.9 — wrong-player close attach when the true fixture is uncaptured. Fix: hard veto on
  first-initial mismatch for tennis canonical names.
- **Backtest fill-price universe** (value_backtest.py:283, adjusted MEDIUM): headline earned at gross Max
  incl. exchanges; live = best soft book net of commission. Divergence documented but the README headline
  isn't re-anchored. Fix: soft-only netted backtest variant; publish that number.
- **CLV significance on correlated pairs** (value_backtest.py:1170): same-match 1X2+OU pairs treated i.i.d.;
  SE understated on n=62. Fix: cluster-by-match SE (or drop to one bet/match for the verdict).
- **Live config cell never holdout-scored** (config.py:362): production runs power/1.30/ceiling-5.0/max-edge-0.20 —
  a cell no holdout evaluation scored. Fix: single-shot validate the live cell on the fresh 2026 tar (per
  ADR-0019), don't touch the spent holdout.
- **BSP mixed-source close vector** (betfair_bsp.py:798): missing Betfair draw close silently retains the
  football-data draw → devig blends two books' vig. Fix: require complete Betfair vector or skip the row.
- **Consensus anchors devig netted prices** (value.py:691): contradicts the P2-1 gross-devig doctrine for
  named sharps; taints the volume-tier consensus CLV evidence. Fix: devig gross in consensus paths too.
- **Security cluster (public Traefik exposure)**: /setup loopback guard keys off host-port binding that
  Traefik bypasses (config.py:1090); /health leaks dependency versions + strategy thresholds publicly
  (routes.py:652); /login has no rate limit and each attempt burns a 600k-iter PBKDF2 on a 2-CPU box
  (routes.py:332); /docs + /openapi.json public. Fixes: gate /setup on credentials-not-provisioned instead of
  bind address; auth or strip /health detail; small in-memory login throttle; disable docs in production.
- **Alert-loss seams**: dead-man's-switch consumed before delivery confirmed (self_audit.py:254); SIGTERM
  closes clients under in-flight dispatch (main.py:93-99), can strand a claimed-undelivered alert behind the
  7-day dedupe TTL; Redis client lacks socket timeouts (main.py:62) — a blackholed Redis wedges poll_odds
  outside the watchdog with no auto-restart. Fixes: flip one-shots only after a delivered DispatchResult;
  scheduler.shutdown(wait=True) with a bounded grace; socket_timeout/socket_connect_timeout on Redis.
- **ROI accounting bias** (repositories.py:1142, routes.py:960, engine.py:131): recommended-stake turnover
  mixed with actual-stake P&L; bet_placed=false outcomes counted at full recommended stake with NULL pnl;
  15-day voids stay in the denominator. Fix: settle the denominator convention (recommended-only, consistently)
  and exclude unplaced/void from turnover.

## Section 5 — Medium/low

In-play gate fails open on unknown kickoff (pipeline.py:937); transient snapshot-insert failure poisons the
change-only cache → lost price moves bias snapshot close (pipeline.py:438); volume→premium upgrade lacks a
status guard vs concurrent settlement (repositories.py:2543); bfapi_http client never closed + shadow job
registered even when capture builder is None (scheduler.py:957); hardcoded exchange commissions (value.py:37);
'pinnacle' substring anchor-grade match (value.py:84); one-sided implausible-close write guard
(clv_trueup.py:177); strict matcher lacks tight kickoff accept bound (matching.py:440); odds_api
market_detail vocabulary incompatible with arcadia keys (odds_api.py:147); league-season CSV silently skipped
after 4 failed attempts (value_backtest.py:309); no BeatTheBookie duplicate-match dedupe (value_backtest.py:433);
market-axis-blind settlement latent for tennis games-markets (outcomes.py:24); unparseable-selection picks
stuck 'alerted' forever (engine.py:153); non-ASCII username → 500 (auth.py:190); vendored Playwright cleanup
not exception-safe → Chromium leak after renderer OOM (vendor/.../playwright_manager.py:121); single-instance
assumption comment-only (no PG advisory lock); no retention job for append-only tables (unbounded pgdata);
JSON-cycle INCOMPLETE verdict is log-only, picks still mint from a degraded slate (oddsportal.py:1282);
safety_audit.sh scope/robustness gaps (app/-only scope, case-sensitive check 1, betfairlightweight-only lib
ban, comment-satisfiable check 6, stale check-4 patterns); stale docs (architecture.md, results.py:209
"hourly", clv_trueup.py netting docstring).

## Section 6 — Betfair/Pinnacle/OddsAPI matching plan

**Current state (verified):** identity spine = `events.external_ref` (globally UNIQUE) with per-source
namespaces; Betfair scrape/inline joins by EXACT external_ref (safest join present); Pinnacle arcadia rows
join at read time via match_event_hardened — normalized name (narrow _NOISE_TOKENS, post-CD-Nacional) +
~880-team collision-guarded alias seed + women/youth/reserve marker veto + disambiguating-token blocklist +
two-tier fuzzy (JW≥0.92 AND token_sort≥90; the 0.84–0.92 band is never auto-accepted) + 6h kickoff accept
bound + ambiguity-margin reject; ordered sports never flip home/away; tennis unordered via surname+initial
with name-based selection re-keying. Market matching is EXACT on (event, market, market_detail) with the line
in key and selection; only full-time markets captured; in-play close capped at kickoff. Betfair API shadow
uses catalogue runners by sortPriority/selectionId 58805, market window from=now (no in-play), best-back size
as liquidity. This is genuinely strong — most of the proposed architecture's *intent* already exists.

**Real gaps → the delta to build (in order):**
1. **Persist match links** — new table `event_source_links` (canonical event_id, source, source_event_id,
   confidence tier, match_method, evidence snapshot: names/kickoffs compared, matched_at, active). Today an
   accepted match leaves no queryable audit trail and can flip cycle-to-cycle. One migration; write on every
   hardened-match accept.
2. **Persist the review band** — `match_review_queue` for the 0.84–0.92 tier that is currently discarded
   silently. This is the sanctioned non-fuzzy recovery lever for the capture-bound match ceiling: operator
   approves → per-club alias row, exactly the workflow project memory prescribes.
3. **Fix the two concrete key bugs**: Pinnacle '+' AH token and tennis first-initial veto.
4. **Wire the liquidity floor** at anchor-use time; NULL-liquidity exchange rows are not anchor-grade.
5. **Metrics** (extend /resolution/match-rate): match/auto-link/review/reject rate by source, Betfair
   liquidity pass rate, anchor coverage by sport/league/market, picks demoted for weak match confidence.
6. **Tests to add** (much of the wishlist is already covered — verify then fill): same-pairing twice in a
   week (strict matcher path), postponed+shifted start, tennis sibling initials, Pinnacle +AH key, duplicate
   source events → one canonical, one source event → two canonicals (ambiguity reject).

Explicitly NOT recommended: a full raw-source-table + canonical_events rebuild. The append-only
odds_snapshots + namespaced external_ref spine already provides raw provenance; a parallel canonical schema
would be a large migration with wrong-game risk during cutover, for information the link table (1) captures
incrementally. Do not broaden _NOISE_TOKENS or fuzzy thresholds (CD Nacional lesson stands).

## Section 7 — Quant integrity

Verified clean by execution or full trace: devig (8 methods, sum-to-1, order-preserving, convergent, flagged
fallback), EV `p·(d−1)−(1−p)` on commission-netted effective price, Kelly `((d−1)p−(1−p))/(d−1)` clipped ≥0,
×0.25 fractional, 2% per-bet cap, CLV log-ratio with identical mint/close devig chokepoint, moneyline ceiling
as a deliberate shadow-cap (ADR-0019 self-validation), sharp-vs-consensus separation by exact SHARP_BOOKS
membership, fake-CLV guards mirrored write/read/live_evidence. Broken or biased: exposure-ledger enforcement
(3.1), consensus-netted devig asymmetry, hardcoded commissions, i.i.d. CLV SE, backtest fill universe,
unscored live config cell, mixed-source BSP close. Promotion gates: tennis/NFL blocked by code (forced
visibility/shadow); basketball blocked only by env flag — the one real gate hole.

## Section 8 — Safety

Picks-only confirmed for all existing paths: no order/cancel/replace/account method anywhere; betfair_api.py
calls only listMarketCatalogue/listMarketBook via constants; credentials .env→SecretStr→memory; config
validator fail-closed against kwarg and env tampering with CI-run tests; ci.yml runs safety_audit.sh as a
build-failing step; vendor/OddsHarvester is an unimported reference copy. Gaps (make it structural, not
textual): runtime `_ALLOWED_OPS` allowlist in `_rpc`; extend audit scope to scripts/; case-insensitive
check 1; ban flumine/betconnect/betdaq + scan uv.lock; replace comment-satisfiable check 6 with a real check
for the actual validator; update check 4's patterns to the real credential field names.

## Section 9 — Security

Solid: PBKDF2-600k, HMAC compare, httponly/samesite cookies, auth on all data routes, no CORS middleware
(fail-closed), non-root container, non-superuser alembic, 127.0.0.1 host binds, verified no-secret logging.
Fix: /setup exposure guard unsound behind Traefik; /health public detail leak; no login rate limit (brute
force + PBKDF2 CPU DoS); public /docs; non-ASCII username 500.

## Section 10 — Recommended patches (minimal diffs)

1. pipeline.py — zero-grant ⇒ demote persisted row + suppress zero-stake duplicate dispatch; 'unpersisted' ⇒
   no alert. Tests: cap-exhausted premium never alerts across two cycles; DB-down cycle alerts nothing.
2. settlement/results.py — adjacent-date accept only when the pick's own date has no same-pairing fixture;
   basketball settle floor ≥4h. Test: NBA back-to-back fixture pair.
3. espn_scores.py — retirement/walkover status veto + completed-set counting + full Bo3/Bo5 pattern required.
   Tests: retired-mid-set, walkover, partial-leading-set.
4. international_results.py + scraped-final path — ET veto (pending/manual). Test: KO-round ET score not settled.
5. scheduler.py/engine.py — split 30s DB-only settle from hourly feed fetch (or TTL cache). Test: two 30s
   cycles produce one upstream fetch.
6. betfair_api.py — `_ALLOWED_OPS` frozenset raise-before-HTTP + MockTransport test + audit presence check.
7. value.py — wire exchange liquidity floor; NULL-liquidity exchange ≠ anchor-grade. Test: thin Betfair row
   demotes to consensus/shadow.
8. pinnacle_arcadia.py — strip '+' in _signed_token. Test: +1.5 AH key equality both sources.
9. matching.py — tennis first-initial hard veto. Test: cerundolo f vs j rejected.
10. scheduler.py — basketball promotion requires SportMarketClvGate pass; odds_api branch populates
    experimental_sports. Test: NBA_EXPERIMENTAL=false without gate-pass still demotes.
11. self_audit.py — flip one-shots only after delivered DispatchResult. Test: failing sink ⇒ re-fire next cycle.
12. main.py — Redis socket timeouts; scheduler.shutdown(wait=True, bounded). config.py — /setup guard on
    credentials-provisioned; routes.py — login throttle; app docs_url=None in production.
13. value_backtest.py — soft-book-only netted fill variant + cluster-by-match SE; re-anchor README headline.

## Section 11 — Test plan

Unit: devig property tests exist (keep); add allowlist-op, liquidity-floor, signed-token, tennis-initial,
retirement-parse, ET-veto, adjacent-date-ambiguity. Integration: two-cycle cap-exhaustion no-alert; DB-outage
fail-closed; duplicate-dispatch suppression; promotion-gate bypass attempts (env matrix). Regression: NBA
back-to-back settlement; cup-final ET; walkover; postponed+shifted kickoff; same-pairing twice-a-week.
Backtest validation: soft-net variant reproduces sign; clustered SE; live-cell single-shot on fresh 2026 tar
only. Safety: non-constant _rpc op ⇒ raise, zero HTTP; scripts/-scoped audit greps. Smoke: SIGTERM mid-dispatch
alert not stranded; Redis blackhole ⇒ poll cycle bounded.

## Section 12 — Honest solid picks (ranked)

1. **Settlement score semantics (patches 2–4).** Highest evidence-integrity payoff. Every contaminated
   settlement permanently poisons the CLV/ROI record that decides promotions. Reduces settled volume in
   tennis/cups — correctly.
2. **Exposure-cap bypass + DB-outage fail-open (patch 1).** The one place the money math is actually wrong
   on the alerted tier.
3. **Backtest honesty (patch 13).** Stop quoting +22.4%. The defensible number is the soft-net, independent-
   close, clustered-SE figure — likely much smaller and possibly CI-straddling-zero. Better to know.
4. **Betfair allowlist + audit hardening (patch 6 + Section 8).** Not exploitable today; cheap; converts the
   platform's core promise from grep-enforced to structural.
5. **Anchor quality: liquidity floor + Pinnacle AH key + tennis initials (patches 7–9).** Fewer, cleaner
   anchored picks; unblocks trustworthy AH/tennis evidence.
6. **Promotion gate in code (patch 10).** One env flip should never promote a sport.
7. **Ops seams (patches 5, 11, 12).** Ban-risk removal and alert-delivery confirmation; cheap insurance.
8. **Matching link table + review queue (Section 6).** Medium effort, compounding value; not urgent — the
   matcher itself is sound.
9. Everything in Section 5 afterwards. docs/architecture.md rewrite is worth an hour — it currently describes
   a different system.

What does NOT matter much: more leagues, more picks, broader fuzzy matching, a canonical-schema rewrite,
volume-tier alerting. The system's edge is credibility of its evidence loop; every top fix above buys
credibility at the cost of volume, which is the right trade.
