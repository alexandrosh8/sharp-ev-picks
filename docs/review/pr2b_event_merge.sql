-- PR2b — MERGE legacy duplicate EVENT rows into one canonical event (ONLINE).
--
-- Context: before the forward mint-time resolver (PR1a, commit 88c3a0c) shipped,
-- OddsChecker / OddsPortal / Pinnacle / the [In Running] fork minted SEPARATE
-- `events` rows for the SAME real fixture. The settlement guard + PR2a already
-- neutralised the MONEY double-count (superseded double-settled picks); this
-- migration removes the leftover redundant event rows so each fixture has ONE
-- canonical event — de-duplicating the operator's live alert view and
-- consolidating odds/anchors under a single event for settlement + CLV.
--
-- SCOPE — deterministic, wrong-merge-SAFE subset only:
--   A duplicate group = events sharing the EXACT oriented key
--   (sport_id, home_team_id, away_team_id) whose kickoffs cluster within the
--   per-sport tolerance (tennis 6h, else 2h) via gap-and-island. Same key the
--   forward resolver uses, so it CANNOT mis-merge: leg reversals (orientation
--   differs), same-day rematches (kickoffs > tolerance apart) and abbrev-vs-full
--   name-fork entities (different team_ids -> PR1b territory) are all excluded.
--
-- ONLINE / NO-PAUSE DESIGN (the whole point of this rewrite):
--   Phase 1 commits ALL small metadata work (map, pick canonicalisation, edges,
--   evals, links) in a SUB-SECOND transaction so pick-row locks release at once
--   — the picks finder is never blocked. After Phase 1 every source ref resolves
--   to the keep event (event_source_links repointed + resolver keys the lowest
--   id), so NO new odds land on fold events. Phase 3 then repoints the ~3.5M
--   historical odds_snapshots in small AUTOCOMMITTED batches (row-level locks
--   only, on a table the finder does not write in its revalidation path), so it
--   never holds a long lock. `lock_timeout` keeps any single batch from waiting.
--
-- SAFETY (verified against the live DB 2026-07-08):
--   * bankroll_ledger and manual_bet_logs are EMPTY — NO operator-placed bet
--     exists; Phase 1 ASSERTS this and ABORTS if ever untrue.
--   * fold-event picks have zero alerts / pick_line_drift children; only
--     result_tracking children exist (cleared for dropped duplicates).
--
-- REVERSIBILITY: pr2b_event_merge_map (persistent) records every (fold -> keep)
--   with applied_at. Redundant duplicate child rows (exact uq-tuple twins already
--   present on the keep event) are DELETED not archived (an identical row
--   survives on keep). Full row-level restore of deleted fold event rows is NOT
--   possible; the map is the audit trail. Legacy hygiene, not a money fix.
--
-- HOW TO RUN (review context — psql, NOT auto-mode; from /workspace):
--   docker compose exec -T postgres psql -U betting_ai -d betting_ai \
--     < docs/review/pr2b_event_merge.sql
--   Idempotent: re-running with no duplicates left is a no-op.

\set ON_ERROR_STOP on
SET lock_timeout = '5s';   -- never wait long on a lock behind the live finder

-- ============================================================================
-- Shared merge-map (gap-and-island time-clustering on the oriented key).
-- Session-scoped TEMP tables (survive the Phase-1 COMMIT — no ON COMMIT DROP).
-- ============================================================================
CREATE TEMP TABLE _clustered AS
WITH ordered AS (
  SELECT e.id, e.sport_id, s.key AS sport, e.starts_at, e.home_team_id, e.away_team_id,
         lag(e.starts_at) OVER (
           PARTITION BY e.sport_id, e.home_team_id, e.away_team_id ORDER BY e.starts_at
         ) AS prev_start
  FROM events e JOIN sports s ON s.id = e.sport_id
),
flagged AS (
  SELECT *,
         CASE WHEN prev_start IS NULL
                OR abs(extract(epoch FROM (starts_at - prev_start)))
                   > (CASE WHEN sport = 'tennis' THEN 21600 ELSE 7200 END)
              THEN 1 ELSE 0 END AS newcluster
  FROM ordered
)
SELECT id, sport_id, home_team_id, away_team_id, starts_at,
       sum(newcluster) OVER (
         PARTITION BY sport_id, home_team_id, away_team_id
         ORDER BY starts_at ROWS UNBOUNDED PRECEDING
       ) AS cl
FROM flagged;

CREATE TEMP TABLE _merge_map AS
WITH grp AS (
  SELECT sport_id, home_team_id, away_team_id, cl,
         min(id) AS keep_id, array_agg(id ORDER BY id) AS ev_ids
  FROM _clustered
  GROUP BY sport_id, home_team_id, away_team_id, cl
  HAVING count(*) > 1
)
SELECT unnest(ev_ids) AS fold_id, keep_id FROM grp;
DELETE FROM _merge_map WHERE fold_id = keep_id;  -- keep maps to itself; drop it

CREATE TEMP TABLE _ev2keep AS
SELECT fold_id AS event_id, keep_id FROM _merge_map
UNION
SELECT keep_id  AS event_id, keep_id FROM _merge_map;

-- ============================================================================
\echo '==== DRY-RUN: merge groups + impact ===='
-- ============================================================================
SELECT
  (SELECT count(*) FROM (SELECT DISTINCT keep_id FROM _merge_map) x) AS merge_groups,
  (SELECT count(*) FROM _merge_map)                                  AS fold_events_to_delete,
  (SELECT count(*) FROM picks p JOIN _merge_map m ON m.fold_id = p.event_id)          AS picks_on_folds,
  (SELECT count(*) FROM odds_snapshots o JOIN _merge_map m ON m.fold_id = o.event_id) AS odds_on_folds;

\echo '---- duplicate picks that will be dropped (canonical twin survives) ----'
WITH ranked AS (
  SELECT (rt.pick_id IS NOT NULL) AS has_result,
         row_number() OVER (
           PARTITION BY k.keep_id, p.market, p.selection, p.model_version_id
           ORDER BY (rt.pick_id IS NOT NULL) DESC,
                    (CASE p.status WHEN 'settled' THEN 0 WHEN 'alerted' THEN 1
                                   WHEN 'void' THEN 2 WHEN 'superseded' THEN 3 ELSE 4 END) ASC,
                    p.id ASC) AS rn
  FROM picks p JOIN _ev2keep k ON k.event_id = p.event_id
  LEFT JOIN result_tracking rt ON rt.pick_id = p.id
)
SELECT count(*) AS dup_picks_dropped, count(*) FILTER (WHERE has_result) AS with_result_tracking
FROM ranked WHERE rn > 1;

-- ============================================================================
\echo '==== PHASE 1: fast metadata txn (sub-second pick-lock hold) ===='
-- ============================================================================
BEGIN;

-- HARD SAFETY ASSERT: refuse if any real placed-bet footprint exists in a component.
DO $$
DECLARE n_money int;
BEGIN
  SELECT count(*) INTO n_money
  FROM picks p JOIN _ev2keep k ON k.event_id = p.event_id
  WHERE EXISTS (SELECT 1 FROM bankroll_ledger b WHERE b.pick_id = p.id)
     OR EXISTS (SELECT 1 FROM manual_bet_logs mb WHERE mb.pick_id = p.id);
  IF n_money > 0 THEN
    RAISE EXCEPTION 'PR2b ABORT: % pick(s) in a merge component have a bankroll/manual-bet footprint; refusing to merge', n_money;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS pr2b_event_merge_map (
  fold_id integer NOT NULL, keep_id integer NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO pr2b_event_merge_map (fold_id, keep_id) SELECT fold_id, keep_id FROM _merge_map;

-- (1) PICKS — one canonical pick per (keep, market, selection, model); drop the rest.
CREATE TEMP TABLE _pick_drop ON COMMIT DROP AS
WITH ranked AS (
  SELECT p.id AS pick_id,
         row_number() OVER (
           PARTITION BY k.keep_id, p.market, p.selection, p.model_version_id
           ORDER BY (rt.pick_id IS NOT NULL) DESC,
                    (CASE p.status WHEN 'settled' THEN 0 WHEN 'alerted' THEN 1
                                   WHEN 'void' THEN 2 WHEN 'superseded' THEN 3 ELSE 4 END) ASC,
                    p.id ASC) AS rn
  FROM picks p JOIN _ev2keep k ON k.event_id = p.event_id
  LEFT JOIN result_tracking rt ON rt.pick_id = p.id
)
SELECT pick_id FROM ranked WHERE rn > 1;

DELETE FROM result_tracking WHERE pick_id IN (SELECT pick_id FROM _pick_drop);
DELETE FROM pick_line_drift  WHERE pick_id IN (SELECT pick_id FROM _pick_drop);
DELETE FROM alerts           WHERE pick_id IN (SELECT pick_id FROM _pick_drop);
DELETE FROM picks            WHERE id      IN (SELECT pick_id FROM _pick_drop);
UPDATE picks p SET event_id = m.keep_id FROM _merge_map m WHERE p.event_id = m.fold_id;

-- (3) MODEL_PREDICTIONS — uq (event_id, model_version_id, market, selection).
DELETE FROM model_predictions WHERE id IN (
  SELECT id FROM (SELECT mp.id, row_number() OVER (
             PARTITION BY k.keep_id, mp.model_version_id, mp.market, mp.selection
             ORDER BY (mp.event_id = k.keep_id) DESC, mp.id ASC) AS rn
    FROM model_predictions mp JOIN _ev2keep k ON k.event_id = mp.event_id) r WHERE r.rn > 1);
UPDATE model_predictions mp SET event_id = m.keep_id FROM _merge_map m WHERE mp.event_id = m.fold_id;

-- (4) CANDIDATE_EVALUATIONS — uq (event_id, market, market_detail, selection, evaluated_at).
DELETE FROM candidate_evaluations WHERE id IN (
  SELECT id FROM (SELECT ce.id, row_number() OVER (
             PARTITION BY k.keep_id, ce.market, ce.market_detail, ce.selection, ce.evaluated_at
             ORDER BY (ce.event_id = k.keep_id) DESC, ce.id ASC) AS rn
    FROM candidate_evaluations ce JOIN _ev2keep k ON k.event_id = ce.event_id) r WHERE r.rn > 1);
UPDATE candidate_evaluations ce SET event_id = m.keep_id FROM _merge_map m WHERE ce.event_id = m.fold_id;

-- (5) EVENT_SOURCE_LINKS — uq (source, source_event_id, canonical_event_id).
--     Repointing these here is what makes future ingestion resolve to the keep
--     event, so NO new odds land on fold events during Phase 3.
DELETE FROM event_source_links WHERE id IN (
  SELECT id FROM (SELECT l.id, row_number() OVER (
             PARTITION BY k.keep_id, l.source, l.source_event_id
             ORDER BY (l.canonical_event_id = k.keep_id) DESC, l.id ASC) AS rn
    FROM event_source_links l JOIN _ev2keep k ON k.event_id = l.canonical_event_id) r WHERE r.rn > 1);
UPDATE event_source_links l SET canonical_event_id = m.keep_id FROM _merge_map m WHERE l.canonical_event_id = m.fold_id;

-- (6) DETECTED_EDGES — no uq beyond pk: plain repoint.
UPDATE detected_edges e SET event_id = m.keep_id FROM _merge_map m WHERE e.event_id = m.fold_id;
-- (7) MATCH_REVIEW_QUEUE — plain repoint.
UPDATE match_review_queue q SET candidate_canonical_event_id = m.keep_id FROM _merge_map m WHERE q.candidate_canonical_event_id = m.fold_id;

COMMIT;

-- ============================================================================
\echo '==== PHASE 2: one-time component-wide odds twin dedupe (read-heavy, small delete) ===='
-- ============================================================================
-- Removes exact uq-tuple twins (keep-vs-fold AND fold-vs-fold) leaving <=1 row
-- per (keep, bookmaker, market, selection, captured_at) so the batched repoint
-- below is collision-free. Autocommitted single statement.
DELETE FROM odds_snapshots WHERE id IN (
  SELECT id FROM (
    SELECT o.id, row_number() OVER (
             PARTITION BY k.keep_id, o.bookmaker, o.market, o.selection, o.captured_at
             ORDER BY (o.event_id = k.keep_id) DESC, o.id ASC) AS rn
    FROM odds_snapshots o JOIN _ev2keep k ON k.event_id = o.event_id
  ) r WHERE r.rn > 1
);

-- ============================================================================
\echo '==== PHASE 3: batched ONLINE odds repoint (fold -> keep, per-batch COMMIT) ===='
-- ============================================================================
CREATE OR REPLACE PROCEDURE pr2b_repoint_odds_batched(_batch int DEFAULT 25000)
LANGUAGE plpgsql AS $$
DECLARE _n int;
BEGIN
  LOOP
    DROP TABLE IF EXISTS _pr2b_batch;
    CREATE TEMP TABLE _pr2b_batch AS
      SELECT o.id AS oid, m.keep_id
      FROM odds_snapshots o JOIN pr2b_event_merge_map m ON m.fold_id = o.event_id
      LIMIT _batch;
    SELECT count(*) INTO _n FROM _pr2b_batch;
    EXIT WHEN _n = 0;
    -- defensive: drop any straggler that would collide with a keep-side snapshot
    DELETE FROM odds_snapshots o USING _pr2b_batch b
    WHERE o.id = b.oid
      AND EXISTS (SELECT 1 FROM odds_snapshots k
                  WHERE k.event_id = b.keep_id AND k.id <> o.id
                    AND k.bookmaker = o.bookmaker AND k.market = o.market
                    AND k.selection = o.selection AND k.captured_at = o.captured_at);
    UPDATE odds_snapshots o SET event_id = b.keep_id
    FROM _pr2b_batch b WHERE o.id = b.oid AND o.event_id <> b.keep_id;
    COMMIT;
  END LOOP;
END $$;

CALL pr2b_repoint_odds_batched(25000);
DROP PROCEDURE pr2b_repoint_odds_batched(int);

-- ============================================================================
\echo '==== PHASE 4: delete drained fold events (fast txn, race-robust) ===='
-- ============================================================================
-- The long Phase 3 window lets the LIVE scraper attach fresh rows to a fold
-- event of a currently-in-play fixture (stale dup source-link -> new odds). So
-- Phase 4 must (a) re-repoint EVERY FK table, not just odds, and (b) delete only
-- fold events that are now REFERENCE-FREE. An actively-scraped live fixture may
-- out-race this delete; that is fine — it is left as an empty shell (its picks
-- and odds already live on the keep event, so no duplicate alert shows) and the
-- NEXT run of this script sweeps it once the match ends (its map row still maps
-- fold -> keep). NEVER force-delete a referenced fold event.
BEGIN;
-- drop stale duplicate fold links so the scraper resolves to the keep event
DELETE FROM event_source_links l USING pr2b_event_merge_map m
WHERE l.canonical_event_id = m.fold_id;
-- re-repoint any straggler rows that raced onto a fold event during Phase 3
DELETE FROM odds_snapshots o USING pr2b_event_merge_map m
WHERE o.event_id = m.fold_id
  AND EXISTS (SELECT 1 FROM odds_snapshots k WHERE k.event_id = m.keep_id AND k.id <> o.id
              AND k.bookmaker = o.bookmaker AND k.market = o.market
              AND k.selection = o.selection AND k.captured_at = o.captured_at);
UPDATE odds_snapshots o     SET event_id = m.keep_id                    FROM pr2b_event_merge_map m WHERE o.event_id = m.fold_id;
UPDATE model_predictions x  SET event_id = m.keep_id                    FROM pr2b_event_merge_map m WHERE x.event_id = m.fold_id;
UPDATE candidate_evaluations c SET event_id = m.keep_id                 FROM pr2b_event_merge_map m WHERE c.event_id = m.fold_id;
UPDATE detected_edges e     SET event_id = m.keep_id                    FROM pr2b_event_merge_map m WHERE e.event_id = m.fold_id;
UPDATE match_review_queue q SET candidate_canonical_event_id = m.keep_id FROM pr2b_event_merge_map m WHERE q.candidate_canonical_event_id = m.fold_id;
-- delete only fold events now clear of ALL references (skips actively-live ones)
DELETE FROM events e USING pr2b_event_merge_map m
WHERE e.id = m.fold_id
  AND NOT EXISTS (SELECT 1 FROM picks p                 WHERE p.event_id = e.id)
  AND NOT EXISTS (SELECT 1 FROM odds_snapshots o        WHERE o.event_id = e.id)
  AND NOT EXISTS (SELECT 1 FROM model_predictions x     WHERE x.event_id = e.id)
  AND NOT EXISTS (SELECT 1 FROM candidate_evaluations c WHERE c.event_id = e.id)
  AND NOT EXISTS (SELECT 1 FROM detected_edges d        WHERE d.event_id = e.id)
  AND NOT EXISTS (SELECT 1 FROM event_source_links l    WHERE l.canonical_event_id = e.id)
  AND NOT EXISTS (SELECT 1 FROM match_review_queue q    WHERE q.candidate_canonical_event_id = e.id);
COMMIT;
\echo '-- fold events left (actively-live; re-run after they finish to sweep) --'
SELECT count(*) AS fold_events_left FROM events e JOIN pr2b_event_merge_map m ON m.fold_id = e.id;

-- ============================================================================
\echo '==== VERIFY (expect 0 remaining dup groups, 0 FK orphans) ===='
-- ============================================================================
WITH ordered AS (
  SELECT e.id, e.sport_id, s.key AS sport, e.starts_at, e.home_team_id, e.away_team_id,
         lag(e.starts_at) OVER (PARTITION BY e.sport_id, e.home_team_id, e.away_team_id ORDER BY e.starts_at) AS prev_start
  FROM events e JOIN sports s ON s.id = e.sport_id
),
flagged AS (
  SELECT *, CASE WHEN prev_start IS NULL
                 OR abs(extract(epoch FROM (starts_at - prev_start))) > (CASE WHEN sport='tennis' THEN 21600 ELSE 7200 END)
                 THEN 1 ELSE 0 END AS newcluster FROM ordered
),
clustered AS (
  SELECT sport_id, home_team_id, away_team_id,
         sum(newcluster) OVER (PARTITION BY sport_id, home_team_id, away_team_id ORDER BY starts_at ROWS UNBOUNDED PRECEDING) AS cl
  FROM flagged
)
SELECT count(*) AS remaining_dup_groups
FROM (SELECT 1 FROM clustered GROUP BY sport_id, home_team_id, away_team_id, cl HAVING count(*) > 1) g;

SELECT
  (SELECT count(*) FROM picks p                 WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.id = p.event_id))            AS orphan_picks,
  (SELECT count(*) FROM odds_snapshots o        WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.id = o.event_id))            AS orphan_odds,
  (SELECT count(*) FROM model_predictions mp    WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.id = mp.event_id))           AS orphan_model_predictions,
  (SELECT count(*) FROM candidate_evaluations c WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.id = c.event_id))            AS orphan_candidate_evals,
  (SELECT count(*) FROM detected_edges d        WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.id = d.event_id))            AS orphan_edges,
  (SELECT count(*) FROM event_source_links l    WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.id = l.canonical_event_id)) AS orphan_source_links;
