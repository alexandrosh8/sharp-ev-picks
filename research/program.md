# Autoresearch program — evidence-flow (2026-07-07)

Human-owned instruction file. Read-only during a run.

## Run tag
`autoresearch/2026-07-07-evidence-flow` (branched off main after the saturated
`cycle_health` parser run; that harness lives in main's git history).

## Objective metric (one number, higher is better)
`evidence_flow_score`, emitted by `research/score.py`. Maximize fresh, independent,
sharp-anchored evidence flow WITHOUT weakening safety or increasing
wrong-game/circular-close risk. Deterministic replay of the parse layer over the
frozen corpus. Formula + weights are in `score.py` (max **2550**).

## Editable asset group (exactly one)
**C — Bookmaker normalization.** Only the bookmaker-naming code in
`app/ingestion/oddschecker.py` (`_bookmaker_name`, `_BOOKMAKER_FALLBACKS`, and how
the all-odds / legacy parse paths pass bookmaker entities). Everything else is
read-only unless a gate failure needs a minimal obvious fix the program permits.

## Baseline / target
Baseline = **2150** (off-map books emit raw 2-letter codes). Target = **2470**
(max reachable via bookmaker normalization: all emitted books canonical, dup 0,
guards held). The corpus's legacy fixture carries a permanent −80
unknown-timestamp penalty (legacy grid has no provider time) that a FUTURE asset
D run would address, not C. Stop at target — do not continue on this scorer;
author a new corpus + program/score for the next run.

## Keep / revert rule
One small change to the asset group → commit → `uv run python research/score.py`
→ gates → keep iff score strictly increases AND `safety_audit_fail==0` AND
`gate_tests_fail==0` AND `wrong_game_count==0` AND `crash_count==0`; else reset.
Never keep a fixture-shaped hack (e.g. hardcoding a corpus code); never keep a
change that improves score by hiding stale/unknown/ambiguous data.

## Forbidden files (never edit during a run)
`research/*` (program/score/corpus/run/db_context), `.env`/secrets/credentials,
`scripts/safety_audit.sh` + safety flags in `app/config.py`, the gate tests
(`tests/test_oddschecker.py`, `tests/test_wrong_game_audit.py`,
`tests/test_resolution.py`), deployment files, and anything outside the
bookmaker-normalization code in `app/ingestion/oddschecker.py`.

## Safety constraints (doctrine)
Never place bets / weaken safety_audit / store betting credentials / imply
guaranteed profit. `safety_audit.sh` (incl. check #10: app must not import
`research/`) is a hard gate folded into the score. Preserve the manual-review,
evidence-first model.

## Known headroom (asset C)
SUB-6: `parse_market_api_payloads` (L1154) and `parse_legacy_match_page` (L1231)
call `_bookmaker_name(code, {})` with EMPTY entities, so a book whose code is not
in the 16-entry `_BOOKMAKER_FALLBACKS` (e.g. Smarkets "SM", Spreadex "SX") emits
the RAW CODE instead of the feed's canonical name — a split identity vs bestOdds.
Additive-safe fix: thread the payload's `bookmakers.entities` map into the
all-odds path (absent → unchanged; no new invented codes → not fixture-shaped).

## Loop cadence
Autonomous short experiments; keep winners, reset losers; log every attempt to
`research/results.tsv`. On reaching max, STOP and open a PR (do not deploy from
this branch).

## Run
```bash
uv run python research/score.py                 # one number on stdout, breakdown on stderr
uv run python research/run.py --iter N --status attempt --desc "..."   # score + log a row
```
