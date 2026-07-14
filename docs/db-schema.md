# Database Schema — PostgreSQL Warehouse

Source of truth: `app/storage/models.py` (SQLAlchemy 2.0 typed ORM), applied
through the current Alembic head (initial warehouse plus later migrations).
Conventions: snake_case; TIMESTAMPTZ for every timestamp; NUMERIC for odds
(10,4) / probabilities (8,6) / money (12,2) — never float; `created_at`
defaults `now()`; no credential-shaped columns anywhere.

## Reference tables

| Table     | Key columns                                                                             | Constraints                                                     |
| --------- | --------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `sports`  | key (e.g. `soccer`, `basketball_nba`), name                                             | UNIQUE(key)                                                     |
| `leagues` | sport_id FK, key (e.g. `soccer_epl`), name, country                                     | UNIQUE(sport_id, key, country)                                  |
| `teams`   | sport_id FK, league_id FK?, name, normalized_name                                       | UNIQUE(sport_id, normalized_name) — entity resolution anchor    |
| `events`  | sport_id, league_id, home/away_team_id FKs, external_ref, status, starts_at, updated_at | UNIQUE(external_ref); idx(starts_at), idx(league_id, starts_at) |

## Market data

| Table                              | Key columns                                                                                                               | Constraints                                                                                                                                                              |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `odds_snapshots` (**append-only**) | event_id FK, bookmaker, market, selection, decimal_odds, liquidity?, captured_at (provider time), ingested_at, is_closing | UNIQUE(event_id, bookmaker, market, selection, captured_at) — re-polls dedupe via ON CONFLICT DO NOTHING; idx(event_id, market, captured_at) for latest-snapshot queries |

## Modeling

| Table               | Key columns                                                                                                                             | Constraints                                                          |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `model_versions`    | name, version, sport_id, trained_at, training_window_start/end, features_hash, hyperparameters JSONB, calibration_method, metrics JSONB | UNIQUE(sport_id, name, version) — every served artifact registered first |
| `model_predictions` | event_id, model_version_id, market, selection, probability, confidence, predicted_at                                                    | UNIQUE(event_id, model_version_id, market, selection); idx(event_id) |

## Edge & picks

| Table                | Key columns                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Constraints                                                                                              |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `detected_edges`     | event_id, model_prediction_id, odds_snapshot_id FKs, devig_method, fair_probability, edge, ev, accepted bool, reject_reasons JSONB, detected_at                                                                                                                                                                                                                                                                                                                                                                                                                | idx(event_id, detected_at). RESERVED (audit #12) — unwritten (mandatory model_prediction_id/odds_snapshot_id FKs, model-strategy shaped); the live value-pipeline audit trail is `candidate_evaluations`             |
| `candidate_evaluations` | event_id FK, sport_key, market, market_detail (NOT NULL, `''` default), selection, tier (`premium` kept / `volume` demoted), reasons JSONB `{"reasons":[…]}`, anchor_book?, anchor_type?, anchor_age_seconds?, anchor_liquidity?, best_book?, best_odds?, edge?, fair_probability?, evaluated_at (cycle ts), created_at | UNIQUE(event_id, market, market_detail, selection, evaluated_at) — idempotent per candidate per cycle (ON CONFLICT DO NOTHING); idx(event_id, evaluated_at). Per-candidate/rejection AUDIT TRAIL (external-audit #3) — pure measurement, never gates minting. Standalone tap (no model_versions/model_predictions FK). Reason slugs: visibility_only, odds_ceiling, non_major_league, no_sharp_anchor, experimental_sport, ml_filter, steam, structural_sanity, stale (freshness_drop), off_band, thin_books (book_miss), ah_implausible, dc_implausible |
| `picks`              | event_id, model_version_id, detected_edge_id FKs, market, market_detail?, selection, bookmaker, decimal_odds, model/fair_probability, edge, ev, confidence, recommended_stake_fraction/amount, stake_breakdown JSONB, reason_summary, status (`pending→alerted→settled/void`), tier (`premium`=alerted+exposure-capped, `volume`=CLV-evidence shadow: never alerted, never on the exposure ledger; key collisions across tiers resolve premium-first — a volume re-detection of a premium key is a no-op, a premium detection of an open volume key UPGRADES the row in place) | UNIQUE NULLS NOT DISTINCT(event_id, market, market_detail, selection, model_version_id) — no duplicate picks; idx(created_at), idx(status) |
| picks **CLV fields** | closing_odds?, closing_fair_probability?, clv_log?, beat_close?, closing_anchor_type?, close_independent_of_fill?, **has_snapshot_close?** (True whenever finalize anchored a snapshot close fair — independent of whether a SOFT book priced closing_odds; the explicit flag avoids the `closing_odds IS NOT NULL` false-negative, finding clv-1)                                                                                                                                                                                                                       | filled at settlement true-up (ADR-0010)                                                                  |
| picks **anchor**     | anchor_type? (`pinnacle`/`sharp`/`consensus`), **anchor_book?** (pick-time sharp anchor BOOK name behind anchor_type — keeps the concrete book for per-book anchor analysis, finding CLV-3), **anchor_match_confidence?** (matcher min-side Jaro-Winkler in [0,1] for the anchor's cross-source match), **anchor_match_method?** (`exact_canonical`/`jw_two_tier`/`slug_*`/`inline_betfair_canonical`/`unscored`)                                                                                                                                                       | additive nullable; NULL on model-strategy / consensus / pre-column rows                                  |

## Cross-source resolution observability

| Table                | Key columns                                                                                                                                                                                                                                                                 | Constraints                                                                                                                                                                                    |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `event_source_links` | canonical_event_id FK, source (`pinnacle_arcadia`/`betfair_api`), source_event_id (per-source stable id), source_market_id?, confidence_score, match_method, matched_at, active (soft-retire), raw_sport/league/home/away?, raw_start_time_utc?, evidence_json?             | UNIQUE(source, source_event_id, canonical_event_id) — upsert-in-place on re-confirm; idx(canonical_event_id). OBSERVABILITY ONLY — the matcher stays authoritative, never gated by this table |
| `match_review_queue` | source, source_event_id, source_market_id?, candidate_canonical_event_id FK?, confidence_score, reason (`jw_below_accept`/`token_sort_below_accept`/`ambiguity_margin`/`kickoff_drift`/`same_teams_kickoff_split`), evidence_json?, reviewed_at?, review_status (`pending`) | UNIQUE(source, source_event_id, candidate_canonical_event_id, reason) — idempotent re-runs (ON CONFLICT DO NOTHING). A TAP on the matcher's silently-discarded borderline bands, never a gate  |

## Manual tracking & accounting

| Table                | Key columns                                                                                                                                         | Constraints                                                                               |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `manual_bet_logs`    | pick_id FK, bet_placed bool, actual_stake?, actual_odds?, bookmaker_used?, placed_at?, notes                                                        | idx(pick_id). **User-entered facts only — never credentials/cookies/sessions (ADR-0002)** |
| `result_tracking`    | pick_id FK, outcome (`won/lost/void/push`), pnl?, roi?, settled_at                                                                                  | UNIQUE(pick_id)                                                                           |
| `bankroll_snapshots` | snapshot_date, balance, note                                                                                                                        | UNIQUE(snapshot_date)                                                                     |
| `alerts`             | pick_id FK, channel (`telegram/webhook`), dedupe_key, status (`sent/failed/skipped`), sent_at                                                       | UNIQUE(dedupe_key) — DB-level idempotency behind the Redis gate; idx(pick_id)             |
| `backtest_runs`      | name, model_version_id?, window_start/end, gate_policy JSONB, cost_assumptions JSONB, n_picks, roi, clv_log_mean, max_drawdown, metrics JSONB, seed | reproducibility record per run                                                            |

## odds_snapshots — write policy, growth, retention

Writes are **change-only** (`app/pipeline.py::_persist_snapshots` →
`app/storage/repositories.py::persist_odds_snapshots`): a process-local
last-seen cache keyed on (event*ref, bookmaker, line-qualified market,
selection) suppresses rows whose decimal odds did not move since the last
write. The `market` column stores the provider submarket key
(`market_detail`, e.g. `asian_handicap*-1_5`) when present — distinct lines
stay distinct observations. `captured_at` is the scrape observation time
(provider-reported), never the insert time.

Cache semantics (accepted trade-offs, asserted by
`tests/test_odds_snapshot_persistence.py`):

- **Restart = cold cache**: the first cycle after a restart re-writes one
  unchanged row per live key; only same-`captured_at` re-writes dedupe on
  `uq_odds_snapshot_observation`.
- **Bounded**: above 100k entries, keys unseen for 3 days are swept, then
  oldest-seen down to the cap (`ODDS_SEEN_TTL` / `ODDS_SEEN_MAX`). An
  evicted live key costs one extra row — eviction is never lossy.
- A failed batch is NOT cached, so it retries next cycle; persistence
  failure never breaks pick generation (WARNING log, cycle continues).

**Growth expectation**: expanded markets scrape ~5–20k observations per
cycle, back-to-back (raw appends would be tens of millions of rows/month).
With change-only writes, expect roughly low single-digit % of observations
per cycle once warm → order of 0.5–2M rows/month at current breadth
(~100 bytes/row + 2 indexes). Verify against
`LAST_POLL[*].snapshots_persisted` (also on the dashboard ingestion strip).

**Suggested retention (NOT implemented)**: keep ~90 days of raw rows;
before deleting older rows, archive or aggregate them (e.g. per-day
open/close/min/max per key) for long-horizon line-movement features. Pick
CLV survives any pruning — closing fields live on `picks`. When
implemented, this should be a maintenance job with batched deletes, not a
migration.

## Migration discipline

Every change ships as an Alembic migration with a working downgrade;
autogenerate output is hand-reviewed (server defaults/constraint names
drift). Applied migrations are never edited. Destructive migrations require a
git checkpoint commit first.
