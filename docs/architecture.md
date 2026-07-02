# Architecture — Manual-Betting +EV Picks Platform

> Picks-only decision support. No component places bets; no execution code
> path exists (ADR-0002). All market-data integrations are read-only
> (GET-only). Stakes, edges and EV are informational — the operator reviews
> every pick and places any bet personally.

## What it is

The system scrapes multi-book odds, anchors a fair price on a sharp book
(exchange/Pinnacle) or the soft-book consensus, devigs it, and flags soft
books quoting a better price than fair (the backtested "value" strategy,
`PICK_STRATEGY=value` — `app/config.py`, `docs/backtesting/value-findings.md`).
No prediction model is in the live path; the Dixon-Coles goals model
(`app/models/football_dc.py`) only runs when `PICK_STRATEGY=model`
(negative backtested CLV — screens only; `app/scheduler.py`).

**Fail-closed doctrine:** `app/config.py::_enforce_picks_only` raises at
startup if `PICKS_ONLY` / `MANUAL_BETTING_ONLY` / `READ_ONLY_MARKET_DATA`
are not true or `AUTO_BETTING` / `BET_EXECUTION_ENABLED` are not false.
There is deliberately no flag that enables betting. `scripts/safety_audit.sh`
greps the tree for bet-placement code paths and runs in CI; any hit is a
build-breaking defect.

## Data sources & roles

| Source | Module | Role |
| ------ | ------ | ---- |
| OddsPortal JSON feed | `app/ingestion/oddsportal_json.py` (+ `oddsportal.py` vocabulary/listing) | **Primary odds source** (`ODDS_SOURCE=oddsportal` default). curl_cffi GET of the public encrypted feed; yields soft-book odds **plus the inline Betfair Exchange row (provider id 44)** on the canonical event. Playwright Chromium is the listing/fallback path. |
| Betfair Exchange capture | `app/ingestion/betfair_exchange.py` | Dedicated read-only BACK-odds + matched-`volume` liquidity capture from the same feed. Persists **inline onto existing canonical events** (`attach_only_to_existing=True`, never mints events) so the sharp anchor and CLV close resolve without matching (ADR-0015 v2). Liquidity-floor gated. |
| Pinnacle ARCADIA | `app/ingestion/pinnacle_arcadia.py` | Independent read-only guest-JSON capture of Pinnacle lines into the isolated `pinnacle_<sport>` namespace — a free sharp **close archive** (ADR-0013). Mints no picks; **cross-matched to canonical events at consumption time** by the hardened matcher (ADR-0014). |
| Betfair read-only API | `app/ingestion/betfair_api.py` | **Shadow/validator only.** Authenticated GET/JSON-RPC restricted to a runtime allowlist of read operations (asserted by `safety_audit.sh`). Rows persist under `betfair exchange (api-shadow)` — a name deliberately outside `SHARP_BOOKS`, so a shadow row can never become the sharp anchor. Compared tick-by-tick against the scraped Betfair row. |
| Results feeds | `app/settlement/results.py`, `app/clv_trueup.py::capture_finished_scores`, `app/ingestion/espn_scores.py`, `football_data.py` | Finished scores for auto-settlement (OddsPortal match pages; ESPN for basketball/NFL/tennis; football-data.co.uk historical). |

API-Football is suspended and never called. All credentials live only in
`.env` (0600, gitignored).

## The pick pipeline (poll → devig → anchor → gate → stake → alert)

One cycle of `app/pipeline.py::run_value_pipeline`, invoked per sport by the
`poll_odds` job:

1. **Poll** — `loader.fetch_odds(sport_key)`; snapshots persist change-only
   to `odds_snapshots` (append-only, dedupe cache — see `docs/db-schema.md`).
   Visibility-only sports (tennis, NFL) stop here: slate published
   `unvalidated=true`, no picks, no alerts, no exposure.
2. **Sharp-anchor injection** — `app/clv_trueup.py::build_sharp_anchor_loader`
   merges captured Betfair (exact canonical ref) and Pinnacle ARCADIA
   (hardened-matcher, strict) snapshots into the anchoring set only,
   freshness-gated per source (`max_age_seconds`). Match provenance
   (`anchor_match_confidence`, `anchor_match_method`) is recorded per pick.
3. **Devig → fair** — `group_market_prices` + `event_fair_probs` anchor fair
   probabilities on a `SHARP_BOOKS` member (Pinnacle / Betfair Exchange /
   Smarkets) or the consensus median (`app/edge/value.py`). Default devig is
   `power`; multiplicative is rejected at config load (ADR-0006, ADR-0019).
4. **Edge → gates** — `find_value_bets_with_fair` flags soft prices above
   fair. Gates (defaults in `app/config.py`): edge ≥ 0.03 premium /
   ≥ 0.015 volume, edge ≤ 0.20 (implausibility ceiling), odds ≥ 1.30,
   moneyline odds ≤ 5.0 (`VALUE_MONEYLINE_MAX_ODDS`, ADR-0019),
   Asian-handicap plausibility (`ah_candidate_plausible`), odds age ≤ 300 s,
   in-play veto (kicked-off events never mint/upgrade), exchange
   liquidity floor (`VALUE_EXCHANGE_MIN_LIQUIDITY=50` on known-liquidity
   Betfair rows), optional sharp-anchor requirement
   (`VALUE_REQUIRE_SHARP_ANCHOR`), optional steam gate (built, default off).
5. **Tiers** — `premium` (alerted, exposure-capped) vs `volume` (shadow:
   persisted + CLV-tracked, never alerted, zero exposure). Unvalidated
   sports and gated demotions land in volume.
6. **Stake (informational)** — `app/risk/staking.py`: Kelly × 0.25
   fractional, 2 % per-bet cap, 4 % per-event correlation cap, 5 % daily
   exposure ledger (`app/risk/exposure.py`). Premium reservation is deferred
   and ranked by raw Kelly per cycle so high-growth picks fund first.
7. **Alert** — Redis-idempotent dispatch (7-day dedupe TTL keyed on price)
   to Telegram/webhook; alerts are withheld fail-closed if pick persistence
   failed.

## Matching & observability

Cross-source resolution (Pinnacle/ESPN names ↔ canonical OddsPortal events)
runs through the hardened matcher in `app/resolution/matching.py`:
normalization, distinguishing markers (women/youth/reserve/B never merge
with the first team), two-tier Jaro-Winkler + token-sort with ambiguity
margins and kickoff windows. Settlement name matching
(`app/settlement/results.py`) is never weaker than the close matcher.

Observability (additive, never a gate — the matcher stays authoritative):

- `event_source_links` — every accepted cross-source match with confidence,
  method and evidence; upsert-in-place per (source, source_event_id).
- `match_review_queue` — a tap on silently-discarded borderline candidates
  (below-accept scores, ambiguity splits, kickoff drift), idempotent.
- Per-pick `anchor_match_confidence` / `anchor_match_method`, serialized on
  `GET /picks`.
- `GET /resolution/match-rate` (`app/api/routes.py`) — match-rate outcomes
  plus a `links` object (auto_linked, review_queued, rejected_observed,
  by_source, weak_links).

## CLV + settlement evidence loop

- **In-cycle revalidation / CLV true-up** — every poll cycle re-prices open
  picks from the same anchored snapshot set used at mint
  (`revalidate_open_picks`), and directly scrapes match pages for picks
  outside the dated window (`revalidate_offwindow_picks`). No separate
  fetch job (`app/pipeline.py`, `app/clv_trueup.py`).
- **Close finalization** — at settlement,
  `finalize_closing_from_snapshots` anchors the closing fair from the
  change-only snapshot history, preferring the Pinnacle archive / captured
  Betfair close (`CLV_USE_PINNACLE_ARCHIVE`, `CLV_USE_BETFAIR_EXCHANGE`).
- **Close independence guard** —
  `app/edge/value.py::close_is_independent_of_fill` /
  `persisted_close_independent` stamp whether the close anchor venue is
  independent of the pick's fill book, so tautological CLV never counts as
  evidence (ADR-0017, ADR-0020). `app/backtesting/live_evidence.py`
  additionally excludes fabricated/implausible closes and asymmetric devig
  fallbacks from the "genuine sharp close" stratum.
- **Auto-settlement** — `capture_finished_scores` (dedicated light job)
  commits final scores per link under timeouts + a cycle budget;
  `settle_results` consumes them from the DB
  (`app/settlement/engine.py::run_settlement_cycle`, results feeds cached
  under a TTL — `SETTLE_FEED_TTL_SECONDS=1800`). Ambiguity vetoes
  (`app/settlement/results.py::ScoreBook`): adjacent-date double matches or
  marker conflicts leave the pick open for manual settlement; an empty
  score book settles nothing, loudly.

## Sport promotion status

Doctrine (`app/backtesting/live_evidence.py::SportMarketClvGate`): a
(sport, market) leaves the experimental/shadow tier only with enough genuine
sharp-close samples, sharp stake-weighted CLV > 2 SE, and a beat-close CI
lower bound > 0.5 — then a deliberate, ADR-logged flip, never automatic.

| Sport | Status | Mechanism |
| ----- | ------ | --------- |
| Football (soccer) | **Validated, live** — premium + volume tiers | default `sport_keys`; backtest evidence in `docs/backtesting/value-findings.md`, ADR-0019 |
| Basketball | **Experimental shadow** — minted, persisted, CLV-tracked, auto-settled, dashboarded; forced to volume tier, never alerted | `NBA_EXPERIMENTAL=true`; promotion additionally requires `NBA_PROMOTION_ACKNOWLEDGE_EVIDENCE` — a bare flag flip is refused (fail-closed) |
| Tennis, NFL | **Display-only** — slates shown `unvalidated=true`, no picks | `visibility_only_sports` in `app/scheduler.py`; opt-in `ENABLE_UNVALIDATED_PICKS` mints shadow-tier-only picks (tennis has no free closing line, so it can never clear the CLV gate) |

## Dashboard & diagnostics

`app/api/dashboard.html` — a single self-contained page with four sections
(Picks / Games / Performance / Diagnostics) behind optional PBKDF2 session
auth (`/health` stays public). Status chips: source freshness, live premium
count, shadow count, open picks, ROI (sample-suppressed), stake-weighted
sharp CLV. Diagnostics surface per-sport poll state, ingestion counters,
match-rate/link observability and self-audit anomalies. Runtime watchdogs:
`app/maintenance/self_audit.py` (read-only DB anomaly checks + dead-man's
switch, alerting via the same sinks), `calibration_drift`, and
`upstream_watch` (PyPI release notices; never auto-installs).

## Scheduler jobs (app/scheduler.py; defaults from app/config.py)

| Job id | Cadence (default) | Purpose |
| ------ | ----------------- | ------- |
| `poll_odds` | every `POLL_INTERVAL_SECONDS` = 300 s | scrape slate per sport, run the value pipeline, revalidate open picks (in-cycle CLV true-up) |
| `capture_finished_scores` | every `RESULTS_SCRAPE_INTERVAL_SECONDS` = 60 s (+ startup run) | scrape + commit final scores for finished fixtures (budgeted, per-link timeout) |
| `settle_results` | every `SETTLE_INTERVAL_SECONDS` = 30 s | settle open picks from committed scores; finalize CLV closes |
| `capture_pinnacle_arcadia` | every `ARCADIA_POLL_INTERVAL_SECONDS` = 120 s | read-only Pinnacle sharp-line archive (change-gated) |
| `capture_betfair_exchange` | every `BETFAIR_EXCHANGE_POLL_INTERVAL_SECONDS` = 300 s | dedicated Betfair BACK odds + liquidity capture (change-gated) |
| `capture_betfair_api_shadow` | every `BETFAIR_API_POLL_INTERVAL_SECONDS` = 300 s | read-only Betfair API shadow capture + scrape-vs-API comparison |
| `self_audit` | every `SELF_AUDIT_INTERVAL_SECONDS` = 600 s | read-only anomaly checks, dead-man's switch, alert dispatch |
| `refit_football_model` | daily 04:10 UTC (+ startup; only when `PICK_STRATEGY=model`) | Dixon-Coles refit — not in the validated value path |
| `calibration_drift` | daily 05:30 UTC | fair-probability calibration drift check + alert |
| `upstream_watch` | daily 08:05 UTC (+ startup) | PyPI release watch for bound engines (penaltyblog, oddsharvester) |
| `snapshot_bankroll` | daily 00:30 UTC | placeholder (bankroll tracking is roadmap phase 6) |

Continuous-poll jobs run `max_instances=1`, coalesced, with hang escalation
on repeated skips.

## Deployment shape

- **Local:** `docker compose up -d postgres redis` (host ports 5433/6380);
  app on host via `uv run uvicorn app.main:app`.
- **Production (Ubuntu VPS):** full compose with the `app` service (profile
  `prod`), `restart: unless-stopped`, `.env` on host (0600), stdout logging.
  The container entrypoint (`scripts/docker_entrypoint.sh`, see `Dockerfile`)
  runs `alembic upgrade head` (idempotent) then execs uvicorn; the compose
  healthcheck covers the migration window. Runbooks: `docs/deployment/`.

Decision history: `docs/adr/` (ADR-0002 picks-only; ADR-0006 devig;
ADR-0010/0012 free-first sources + engine binding; ADR-0013/0014/0015
Pinnacle archive, cross-source resolution, Betfair capture; ADR-0017/0020
CLV close provenance + venue independence; ADR-0019 pre-registered
sharp-vs-soft hypotheses). Current stats and status live in `README.md` —
this document intentionally repeats none of them.
