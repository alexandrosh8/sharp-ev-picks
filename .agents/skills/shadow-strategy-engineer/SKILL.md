---
name: shadow-strategy-engineer
description: "Build shadow-only/validation-only strategy modules safely. Use when implementing any new gate, sport module, telemetry, or strategy variant — especially basketball/NBA/NFL/tennis (operator mandate: shadow-first)."
allowed_tools:
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - Bash
---

# Shadow-Strategy Engineer

## When to use
Implementing any strategy-adjacent code: gates, eligibility bounds,
telemetry, sport modules, backtest variants.

## Required inputs
ADR-0019 (frozen hypotheses + amendments), the shadow-first sport policy
memory, and the target's blast radius (live pipeline vs offline harness).

## Procedure
1. Prefer ZERO-live-path designs: retrospective replay over warehouse data
   and frozen-eval backtest rows deliver shadow evidence without touching
   app/pipeline.py / app/edge/value.py / app/scheduler.py at all.
2. If live recording is unavoidable: off-by-default env flag (read only in
   config.py), verdict-table pattern (betfair_anchor_verdicts precedent),
   never a drop path — demote/record only.
3. Closed rejection vocabularies; fail-closed on missing references
   (excluded AND counted, never passed).
4. Label every data readout EXPLORATORY/SPENT for selection; thresholds
   frozen from first principles BEFORE reading stratified outcomes.
5. Pre-registration text goes in the ADR in the same commit as the code.

## Forbidden shortcuts
No silent row drops; no tolerance/threshold picked from the readout it will
gate; no promotion/staking/alerting wiring "for later"; no all-row CLV as
evidence.

## Verification checklist (all mandatory)
- [ ] no-live-import test (pipeline/value/scheduler never import the module)
- [ ] off-by-default proven by test if any flag exists
- [ ] pass/fail/excluded reasons recorded and summed = total (no silent loss)
- [ ] sample sizes printed everywhere; n<30 marked insufficient
- [ ] full pytest + ruff + format + mypy + safety_audit green

## Gotchas
- **The H6 tolerance lesson (2026-07-04):** an ADR citing "the value in the
  research log" is worthless if nobody wrote it down — record the number in
  the ADR itself, same commit.
- **Replay beats live shadow wiring** for evidence speed: history is already
  there; live shadow accrues at real-time speed only.
- **A gate that would have kept the lower-CLV group** (H6 soccer replay) is
  exactly why gates ship OFF — expect anti-confirmations at small n.

## Output format
Files + tests list, gate results, exploratory findings with n, the ADR
wording added, and what sign-off is still required.
