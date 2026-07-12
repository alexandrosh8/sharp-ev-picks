# Full audit 2026-07-09 — findings ledger

Multi-agent workflow audit (8 reviewers, adversarial verify): 33 raw findings →
31 CONFIRMED (3 high / 10 medium / 18 low), 2 refuted. Baseline (Smoke A):
suite exit 0, safety_audit PASSED, autoresearch scorer 2470/2470.

## Implemented (30 of 31)

api/routes.py: superseded-pick 409 guard on POST /picks/{id}/result;
body/path pick_id 422; correction clears ResultTracking scores; match-rate
report queries run concurrently (own session each, sequential fallback).

api/dashboard.html: clvIsFabricated mirrors backend fallback-only cutoff;
structural_sane gates actionability + danger tag (+ dead .stars-row CSS
removed); one-sided tier fetch keeps last rows + banner; /health failure
fails closed; trusted-CLV tag requires measured clv_log + no exclusion;
recent-settled rows use selLabel; ticker stale count shares edgeGroupOf.

settlement: dedup guard folds selection spelling via resolution normalizer
(line-preserving); manual settle_event_picks applies the same guard; 2-way
h2h tie now PUSH (american_football only, sport-keyed).

clv_trueup: line-ambiguity guard extended to unanchored groups
(_collect_group_prices, skip-not-overwrite); _detail_matched_books merges
full-match vocabularies (soft '1x2' vs detail-less sharp h2h) while excluding
non-settleable period/corner/card groups — fixed the initial exact-detail
regression caught by test_betfair_clv_consumption.

pipeline: persist+reserve cancellation-atomic (asyncio.shield); model
freshness gate fails closed on future captured_at; kickoff refresh before the
post-kickoff guard.

storage: upgrade path refreshes mint_devig_fell_back + clears close-side
provenance; idx_event_source_links_source_event_id (migration a2f7d4c9e1b8,
new alembic head — 3 pinned-head tests updated); pick-path league
get-or-create keys on (key, country).

edge/probabilities: model-strategy EV/Kelly on commission-netted effective
odds (policy via composition root); devig.py logging removed — fallback
reason returned as data (DevigFallbackReason), logged at IO call site.

ingestion: betfair change-gate rolls back on persist failure; oddschecker
fetch_match_odds markets override honored (scorer held 2470); oddsportal_json
error logs exception-type-only; JSON bootstrap applies ET/OT/pens score veto.

resolution: ambiguity margin measured against first DISTINCT fixture;
AliasTable conflicting claims quarantined (resolve to neither) + seed
tripwire pinning the 2 known conflicts (america mineiro, drogheda united).

## Deferred (OWNER DECISION)

1. RESOLVED (operator-approved 2026-07-09, commit 165176d): value.py consensus
   anchors now devig GROSS odds; netted prices retained for dedupe order and
   the overround gate only. Watch consensus-anchored pick CLV after deploy.
2. Seed merge of the 2 quarantined alias conflicts — route through the alias
   review process (wrong-game risk; America MG genuinely ambiguous).
3. provisional_result (display-only CLOSED-tab grading) still grades 2-way
   ties sportlessly until settlement runs (needs sport threading through
   repositories.py:634).
3b. Sinner/Djokovic twin events (11866 keep / 11900 pickless) are DUPLICATE
   TEAM ENTITIES (identical names, team_ids 9161/9222 vs 6584/6577) — event
   merge correctly refuses (different team_ids); needs the team-dedup
   migration (42-team backlog class). 2026-07-10: one true twin folded
   (11722->11715) via pr2b_event_merge_2026_07_10.sql (mrq twin-row fix).
4. Optional data migration merging existing country='' league twins (needs
   an exactly-one-twin guard; 'Division 1' matches Iceland AND Ireland).
5. dashboard structural_sane finding 5 residual: has_snapshot_close /
   devig-fallback flags not serialized on /picks rows — full client mirror
   needs a server verdict field.

## Gates (Smoke B)

ruff check + format --check: clean (232 files). mypy app: clean (91 files).
mypy tests: 2 pre-existing errors remain (test_value.py:873,
test_oddschecker.py unused-ignore) — untouched files, present at HEAD.
safety_audit.sh: PASSED. research/score.py: 2470.0000 (all gate metrics
unchanged). Full suite: see smokeB3 (DB-backed tests run via bridge-IP
pytest plugin from the session scratchpad — never committed).
