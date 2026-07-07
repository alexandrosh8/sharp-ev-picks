# Autoresearch program — capture-freshness (2026-07-07)

Human-owned instruction file (Karpathy `autoresearch` style). The agent MAY read
this during a run but MUST NOT edit it mid-run. Between runs, the human (GodFather)
owns and edits it. It defines the whole experiment contract.

---

## Run tag
`autoresearch/2026-07-07-capture-freshness`  (git branch of the same name)

## Objective metric (one number, higher is better)
`cycle_health_score`, emitted on stdout by the locked scorer `research/score.py`.
It is a deterministic, network-free replay of the OddsChecker **parse layer**
over the frozen corpus `research/corpus.py`, scoring every parser-controllable
dimension of capture health:

```
cycle_health_score =
      1000 * fresh_mintable_candidate_rate   # correct, live snapshots emitted (recall vs objective GT)
    +  500 * sharp_anchor_score              # OTHER-capture sharp-anchor decision correctness
    +  250 * matched_event_rate              # events registered with correct home/away (matcher-ready)
    - 1000 * stale_drop_ratio                # share of relevant emissions that are stale / wrong-game
    - 5000 * swap_count                      # orientation-swapped registration (plausible wrong game)
    - 5000 * crash_count                     # parser crashed on a payload it should handle
    - 10000 * safety_audit_fails             # scripts/safety_audit.sh non-zero
    - 10000 * gate_tests_fail                # parser / wrong-game / matcher contract regression
```

Baseline (HEAD of the branch at run start): **526.52**. Theoretical max with all
four headroom suboptimalities resolved and gates green: **1750**.

The weights mirror the operator's example scoring rule. `fresh_mintable`,
`sharp_anchor`, and `matched` map 1:1 onto the DB `cycle_health` components
(fresh-snapshot coverage, sharp-anchor share, matched-event rate) measured at the
SOURCE (the parser) — which is the only place a `oddschecker.py` edit can move,
deterministically. A read-only live-DB snapshot is recorded ALONGSIDE each kept
experiment (`research/db_context.py`) for monitoring — it is NOT part of the score
(a parser edit can't move live production state without a fresh scrape).

## Editable asset (exactly one)
`app/ingestion/oddschecker.py` — the OddsChecker parse layer.
No other production file may be edited during the run.

## Locked scorer
`research/score.py` (and its frozen corpus `research/corpus.py`). Run it, never
edit it. `bash scripts/safety_audit.sh` and the contract tests are folded INTO
the number, so an unsafe or regressing edit can never score higher.

## Success threshold
Stop when `cycle_health_score >= 1745` (all four headroom fixtures resolved,
gates green) **or** the human stops the run. Partial progress is kept along the
way (see keep/revert).

## Keep / revert rule
1. Make ONE small, GENERAL change to the editable asset.
2. Commit it on the run branch.
3. Run `research/score.py`.
4. KEEP the commit iff the number strictly increases (Δ > 1e-6) AND
   `safety_audit_fails == 0` AND `gate_tests_fail == 0` AND `swap_count == 0`
   AND `crash_count == 0`.
5. Otherwise `git revert`/reset the commit back to the prior baseline.
6. Log every attempt (kept or reverted) to `research/results.tsv`.
Changes MUST be general parser fixes, never corpus-specific special-casing
(hardcoding a fixture's subeventId / team strings is forbidden). Three
anti-overfitting defenses, most robust first:
1. Each headroom behavior appears in the corpus TWICE with different literals
   (`headroom_subN_*` + `headroom_subN_variant_*`), so a value-keyed hack fixes
   one instance and never reaches max — only a GENERAL fix does.
2. `scripts/safety_audit.sh` check #10 fails (hard gate in the score) if
   `app/` imports `research/` — the parser can never read the answer key.
3. The human reviews every kept diff for generality (attended-run guard).

## Forbidden files (never edit during a run)
- `research/program.md` (this file)
- `research/score.py`, `research/corpus.py`, `research/db_context.py` (locked scorer)
- `scripts/safety_audit.sh` and any safety flag in `app/config.py`
- the frozen contract tests used as gates
  (`tests/test_oddschecker.py`, `tests/test_wrong_game_audit.py`, `tests/test_resolution.py`)
- anything outside `app/ingestion/oddschecker.py`

## Safety constraints (doctrine — never override)
- Never weaken safety flags or `scripts/safety_audit.sh`; it is a hard gate in the score.
- This system never places bets; edits are read-only parse-layer improvements only.
- Never broaden the sharp-anchor book set beyond Betfair `OE` (SUB-4) — that is an
  operator-gated premium-scoping / shadow-first policy change, out of scope. The
  score deliberately does NOT reward it.
- No profit guarantees; no ROI-narrative optimization. Trusted CLV, freshness,
  sharp-anchor coverage, source agreement, settlement reliability, match precision only.

## Improvement backlog (headroom found by the 2026-07-07 parser audit)
Each maps to one headroom fixture; each is a SAFE, objectively-correct, general fix.
- **SUB-1** `parse_market_api_payloads` (primary path) keeps `expired`/`notExpired`
  odds that the bestOdds path drops -> stale leak. Apply the same drop.
- **SUB-3** `_odds_have_sharp_anchor` accepts a SUSPENDED Betfair OE quote ->
  require the OE anchor odd to be status==ACTIVE (and non-expired).
- **SUB-5** api team-split ignores structured `homeTeamName`/`awayTeamName` when
  `_split_match_name` fails -> read them (additive-safe; absent in prod = unchanged).
- **SUB-7** `_find_match_payload` picks the byte-largest bestOdds blob, not the
  URL's subevent -> select the blob whose subevent id/slug matches the URL.

## Loop cadence
Autonomous. One change per iteration; keep-or-revert per the rule; log each.
Checkpoint back to the human at each KEPT improvement; hard-stop on any safety
trip (`safety_audit_fails > 0`), swap, or crash. Human may say "stop" anytime.

## How to run
```bash
uv run python research/score.py                 # -> one number on stdout, breakdown on stderr
bash scripts/safety_audit.sh                    # must exit 0
uv run python research/db_context.py            # read-only live-DB monitor snapshot (context only)
```
