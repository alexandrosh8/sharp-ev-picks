# Settlement Engine (Roadmap Phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Settle alerted picks automatically from free results sources (and manually from the dashboard), producing ROI + stake-weighted log-CLV performance reports.

**Architecture:** A pure outcome-mapping module (`app/settlement/outcomes.py`, stdlib only — same pure-math boundary as `app/probabilities/`) maps `(market, selection, final score)` → `Outcome`. A results layer (`app/settlement/results.py`) loads final scores from the already-integrated free sources (martj42 international CSV for World Cup, football-data.co.uk new-league/season CSVs for clubs) into a `ScoreBook` keyed by normalized team names + date. An IO engine (`app/settlement/engine.py`) joins open picks to scores and writes `result_tracking` rows idempotently. The stubbed `settle_results` scheduler job goes real; the API gains event-level manual settlement plus `GET /performance`; the dashboard gains a performance card and a settle control.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 async, FastAPI, pytest (existing compose-Postgres fixture pattern for DB tests).

**Invariants (kestrel-settlement skill, adapted):**

- Fake/in-memory score data for tests; no network in tests.
- Settler refuses silent-empty results: providers returning zero scores logs a loud error and writes nothing.
- Atomic per-pick settlement: `result_tracking` row (outcome, pnl, roi, settled_at) + `picks.status='settled'` in one transaction; insert is idempotent via `uq_result_tracking_pick`.
- Settling freezes CLV automatically (true-up only touches `status == 'alerted'`).
- Picks whose scores are unavailable stay open ("close_pending" analog) — never guessed.

**Selection vocabulary being settled (from `app/ingestion/oddsportal.py::_selections`):**

- `h2h`: `{home}` | `Draw` | `{away}` (basketball home_away: no Draw)
- `totals`: `Over {line}` | `Under {line}`
- `btts`: `BTTS Yes` | `BTTS No`
- `dnb`: `{home}` | `{away}` (draw → push)
- `double_chance`: `{home} or Draw` | `{home} or {away}` | `Draw or {away}`
- `spreads`: `{team} -1.5` (Asian, half-lines only — pushes impossible by loader config) and `{team} -1` / `Draw (-1)` (European 3-way; adjusted draw LOSES for team legs, wins only the Draw leg)

---

### Task 1: Pure outcome mapping — `app/settlement/outcomes.py`

**Files:**

- Create: `app/settlement/__init__.py`
- Create: `app/settlement/outcomes.py`
- Test: `tests/test_settlement_outcomes.py`

- [ ] **Step 1: Write failing tests** covering every market: h2h home/draw/away ×win/lose, totals over/under win/lose/push-on-integer-line, btts yes/no, dnb win/push/lose, all three double-chance legs, AH half-line home/away, EH team leg adjusted-draw-loses, EH draw leg wins/loses, unknown selection raises ValueError, pnl/roi math (won/lost/void/push).
- [ ] **Step 2: Run, verify fail** (`uv run pytest tests/test_settlement_outcomes.py -q` → import error).
- [ ] **Step 3: Implement** `settle_selection(market, selection, home, away, home_score, away_score) -> Outcome`, `pick_pnl(outcome, stake, decimal_odds) -> Decimal`, `pick_roi(pnl, stake) -> Decimal | None`. Stdlib only (Decimal allowed). Spreads parser: `Draw ({line})` → EH draw leg (wins iff home_score+line == away_score); `{team} {signed line}` via rsplit, team must equal home or away exactly; margin = (team_score+line) − opp_score; >0 won, <0 lost, ==0 → integer line means EH team leg → LOST (AH push-lines rejected upstream; half-line ties impossible). Totals: `==` line → PUSH.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** `feat(settlement): pure outcome mapping for all live markets`.

### Task 2: Results layer — `app/settlement/results.py`

**Files:**

- Create: `app/settlement/results.py`
- Test: `tests/test_settlement_results.py`

- [ ] **Step 1: Failing tests**: `FinalScore` lookup by exact normalized names at kickoff date; ±1-day tolerance; unique-containment fallback (`flamengo` vs `flamengo rj`); ambiguous containment returns None; `scores_from_match_rows` and `scores_from_international` adapters; `league_score_sources("world-cup,brazil-serie-a")` maps slugs → source descriptors and logs/skips unknown slugs (nba).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement**: `normalize_team()` (casefold, strip accents via unicodedata, alnum+space only, collapse spaces), `FinalScore` frozen dataclass, `ScoreBook.lookup(home, away, kickoff_utc)`, adapters from `MatchRow`/`InternationalMatch`, slug map `{"world-cup": international, "brazil-serie-a": BRA, "england-premier-league": E0, ...}` reusing `NEW_LEAGUES`/`LEAGUES` codes, and `async load_scores(client, slugs, seasons, on_or_after) -> list[FinalScore]` calling the existing fetchers (httpx errors logged per-source, never fatal).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** `feat(settlement): score book + free results sources mapped from league slugs`.

### Task 3: Settlement engine — `app/settlement/engine.py`

**Files:**

- Create: `app/settlement/engine.py`
- Test: `tests/test_settlement_engine.py` (compose-Postgres fixture pattern from `tests/test_persistence.py`, skip when DB absent)

- [ ] **Step 1: Failing tests**: settles a past-kickoff alerted pick (result_tracking row with outcome/pnl/roi/settled_at; status→settled); idempotent re-run (no duplicate row); uses ManualBetLog actual stake/odds when present; leaves future-kickoff and score-missing picks open; empty ScoreBook → returns 0, writes nothing, logs error.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `settle_open_picks(session_factory, book, now) -> int`: select alerted picks joined to event + team names where `starts_at <= now - 2h`; lookup score; `settle_selection` (ValueError → warn + skip); stake/odds from latest ManualBetLog with `bet_placed and actual_stake` else recommended; `pg_insert(ResultTracking).on_conflict_do_nothing(constraint="uq_result_tracking_pick")`; status='settled'; single commit.
- [ ] **Step 4: Run, verify pass** (and against compose Postgres).
- [ ] **Step 5: Commit** `feat(settlement): engine settles open picks from score book, idempotent`.

### Task 4: Real `settle_results` job — `app/scheduler.py`

**Files:**

- Modify: `app/scheduler.py:233-241`
- Test: extend `tests/test_settlement_engine.py` (job builder unit test with fake loader funcs — no network)

- [ ] Replace the stub: when `session_factory` is set and `odds_source == "oddsportal"`, the hourly `:15` job loads scores for `oddsportal_football_leagues` slugs (last 14 days) via `load_scores`, builds `ScoreBook`, calls `settle_open_picks`; zero scores → loud `logger.error`, no settle call. Keep `misfire_grace_time=None`, `coalesce=True`, `max_instances=1`.
- [ ] Run full suite; commit `feat(scheduler): settle_results goes real (phase 4)`.

### Task 5: Manual event settlement + performance API

**Files:**

- Modify: `app/schemas/events.py` (add `EventResultIn(home_score: int ≥0, away_score: int ≥0)`)
- Modify: `app/api/routes.py` (POST `/events/{event_id}/result`, GET `/performance`)
- Create: repository fn `performance_report(session)` in `app/storage/repositories.py`
- Test: `tests/test_api.py` (validation), `tests/test_settlement_engine.py` (report math vs seeded rows)

- [ ] POST `/events/{event_id}/result`: settles ALL open picks of the event via `settle_selection` with the submitted score (works for basketball too); 404 unknown event; returns `{settled: n, skipped: m}`.
- [ ] GET `/performance`: from `result_tracking ⨝ picks`: n_settled, won/lost/void/push counts, total_staked, total_pnl, roi (pnl/staked), stake-weighted mean `clv_log`, beat_close rate, n_pending (alerted). All Decimal-as-string.
- [ ] Run suite; commit `feat(api): event-level manual settlement + performance report`.

### Task 6: Dashboard — settle control + performance card

**Files:**

- Modify: `app/api/dashboard.html`

- [ ] Performance card fetches `/performance` (ROI, P&L, record W-L-P, stake-weighted CLV, settled/pending counts).
- [ ] Each past-kickoff unsettled event row gets a "Settle" button → `prompt()` for `home-away` score → POST `/events/{id}/result` → refresh. No innerHTML (test enforces), Cyprus-time display preserved, safety footer untouched.
- [ ] Run `tests/test_api.py`; commit `feat(dashboard): settle button + performance card`.

### Task 7: Docs + memory

- [ ] `docs/roadmap.md`: Phase 4 → ✅ with delivered summary; README status line.
- [ ] `.claude/memory/decisions.md`: settlement design decisions (EH adjusted-draw rule, slug→source map, silent-empty refusal, 2h settle delay).
- [ ] Run `bash scripts/safety_audit.sh`, `uvx ruff check .`, `uv run mypy app tests`, full pytest; commit `docs: phase 4 settlement shipped`.

## Self-review notes

- Spec coverage: results loader ✓ (Task 2), outcome mapping ✓ (Task 1), settle_results real ✓ (Task 4), ROI + stake-weighted log-CLV report ✓ (Task 5/6), "user-recorded results produce reports" ✓ (existing /picks/{id}/result + new event-level endpoint feed the same tables).
- Football CLV true-up from PSC\* closing columns already exists live (`app/clv_trueup.py`); not duplicated here.
- Basketball has no free results source — manual event settlement covers it (documented).
