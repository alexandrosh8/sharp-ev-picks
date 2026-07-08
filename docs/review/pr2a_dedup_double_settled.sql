-- PR2a — neutralize ALREADY-double-settled cross-source duplicate picks.
--
-- Context: before the forward dedup resolver + settlement guard shipped, a
-- cross-source / [In Running]-fork duplicate minted the SAME bet on two event
-- rows. `uq_result_tracking_pick` is per pick_id, so BOTH settled and BOTH
-- count into pnl / ROI / CLV. Live audit (2026-07-08) found 7 such pairs:
--   * 1 pnl-distorting: FAA h2h (picks 3456 + 4528) both WON at +6.27 = a
--     phantom +6.27 double-counted into real-money ROI.
--   * 6 double-recorded VOIDs (pnl 0, but they double-count the settled/void
--     denominators, corrupting void-rate + CLV sample size).
--
-- Fix: keep the CANONICAL pick (lowest pick_id) with its result; for each
-- DUPLICATE, DROP its result_tracking row and mark the pick 'superseded' — the
-- exact terminal state the settlement guard now assigns to new duplicates, so
-- it leaves pnl / ROI / CLV entirely. Detection is DETERMINISTIC (same sport +
-- normalized unordered team pair + kickoff within the guard's per-sport
-- tolerance: tennis 6h, else 2h). Idempotent: re-running finds nothing.
--
-- HOW TO RUN (review context, e.g. psql, NOT auto-mode):
--   docker compose exec -T postgres psql -U betting_ai -d betting_ai \
--     -f docs/review/pr2a_dedup_double_settled.sql
-- The DRY-RUN SELECT prints first; review it, then the transaction applies.

\echo '==== DRY-RUN: duplicate pairs that will be de-duplicated ===='
WITH ev AS (
  SELECT e.id, e.sport_id, e.starts_at,
         regexp_replace(lower(regexp_replace(ht.name, '\s*\[[^\]]*\]\s*$', '', 'g')), '[^a-z0-9]', '', 'g') AS h,
         regexp_replace(lower(regexp_replace(at.name, '\s*\[[^\]]*\]\s*$', '', 'g')), '[^a-z0-9]', '', 'g') AS a
  FROM events e JOIN teams ht ON ht.id = e.home_team_id JOIN teams at ON at.id = e.away_team_id
),
pairs AS (
  SELECT p1.id AS keep_pick, p2.id AS drop_pick, p1.market, p1.selection,
         ra.outcome AS keep_oc, rb.outcome AS drop_oc, rb.pnl AS drop_pnl
  FROM picks p1
  JOIN ev e1 ON e1.id = p1.event_id
  JOIN result_tracking ra ON ra.pick_id = p1.id
  JOIN picks p2 ON p2.id > p1.id AND p2.event_id <> p1.event_id
       AND p2.market = p1.market AND p2.selection = p1.selection
       AND p2.model_version_id = p1.model_version_id
  JOIN result_tracking rb ON rb.pick_id = p2.id
  JOIN ev e2 ON e2.id = p2.event_id
  JOIN sports s ON s.id = e1.sport_id
  WHERE e1.sport_id = e2.sport_id
    AND LEAST(e1.h, e1.a) = LEAST(e2.h, e2.a)
    AND GREATEST(e1.h, e1.a) = GREATEST(e2.h, e2.a)
    AND abs(extract(epoch FROM (e1.starts_at - e2.starts_at)))
        <= (CASE WHEN s.key = 'tennis' THEN 21600 ELSE 7200 END)
)
SELECT keep_pick, drop_pick, market, selection, keep_oc, drop_oc, drop_pnl,
       'phantom pnl removed = ' || COALESCE(sum(drop_pnl) OVER (), 0) AS note
FROM pairs ORDER BY drop_pick;

\echo '==== APPLY ===='
BEGIN;
CREATE TEMP TABLE _pr2a_drop ON COMMIT DROP AS
WITH ev AS (
  SELECT e.id, e.sport_id, e.starts_at,
         regexp_replace(lower(regexp_replace(ht.name, '\s*\[[^\]]*\]\s*$', '', 'g')), '[^a-z0-9]', '', 'g') AS h,
         regexp_replace(lower(regexp_replace(at.name, '\s*\[[^\]]*\]\s*$', '', 'g')), '[^a-z0-9]', '', 'g') AS a
  FROM events e JOIN teams ht ON ht.id = e.home_team_id JOIN teams at ON at.id = e.away_team_id
)
SELECT DISTINCT p2.id AS drop_pick
FROM picks p1
JOIN ev e1 ON e1.id = p1.event_id
JOIN result_tracking ra ON ra.pick_id = p1.id
JOIN picks p2 ON p2.id > p1.id AND p2.event_id <> p1.event_id
     AND p2.market = p1.market AND p2.selection = p1.selection
     AND p2.model_version_id = p1.model_version_id
JOIN result_tracking rb ON rb.pick_id = p2.id
JOIN ev e2 ON e2.id = p2.event_id
JOIN sports s ON s.id = e1.sport_id
WHERE e1.sport_id = e2.sport_id
  AND LEAST(e1.h, e1.a) = LEAST(e2.h, e2.a)
  AND GREATEST(e1.h, e1.a) = GREATEST(e2.h, e2.a)
  AND abs(extract(epoch FROM (e1.starts_at - e2.starts_at)))
      <= (CASE WHEN s.key = 'tennis' THEN 21600 ELSE 7200 END);

UPDATE picks SET status = 'superseded' WHERE id IN (SELECT drop_pick FROM _pr2a_drop);
DELETE FROM result_tracking WHERE pick_id IN (SELECT drop_pick FROM _pr2a_drop);
COMMIT;

\echo '==== VERIFY (expect 0 remaining double-settled dup pairs) ===='
WITH ev AS (
  SELECT e.id, e.sport_id, e.starts_at,
         regexp_replace(lower(regexp_replace(ht.name, '\s*\[[^\]]*\]\s*$', '', 'g')), '[^a-z0-9]', '', 'g') AS h,
         regexp_replace(lower(regexp_replace(at.name, '\s*\[[^\]]*\]\s*$', '', 'g')), '[^a-z0-9]', '', 'g') AS a
  FROM events e JOIN teams ht ON ht.id = e.home_team_id JOIN teams at ON at.id = e.away_team_id
)
SELECT count(*) AS remaining_double_settled_pairs
FROM picks p1
JOIN ev e1 ON e1.id = p1.event_id
JOIN result_tracking ra ON ra.pick_id = p1.id
JOIN picks p2 ON p2.id > p1.id AND p2.event_id <> p1.event_id
     AND p2.market = p1.market AND p2.selection = p1.selection
     AND p2.model_version_id = p1.model_version_id
JOIN result_tracking rb ON rb.pick_id = p2.id
JOIN ev e2 ON e2.id = p2.event_id
JOIN sports s ON s.id = e1.sport_id
WHERE e1.sport_id = e2.sport_id
  AND LEAST(e1.h, e1.a) = LEAST(e2.h, e2.a)
  AND GREATEST(e1.h, e1.a) = GREATEST(e2.h, e2.a)
  AND abs(extract(epoch FROM (e1.starts_at - e2.starts_at)))
      <= (CASE WHEN s.key = 'tennis' THEN 21600 ELSE 7200 END);
