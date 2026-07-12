# ADR-0024 — Per-source sharp-close max-age gate (pre-registration, ACCEPTED)

- **Status:** ACCEPTED — the PRE-REGISTRATION is signed by the operator
  2026-07-12 (in-session "proceed with two remaining"). Signing here commits to
  the frozen adoption criterion below; it does NOT adopt the gate. The 60-minute
  cap stays SHADOW/report-only until the criterion is met on post-2026-07-12
  evidence AND a separate adoption sign-off is recorded (see "Adoption"). The
  thresholds are frozen as of pre-registration and may not be re-tuned on
  outcomes.
- **Context:** the CLV close-freshness study
  (`docs/research/2026-07-10-clv-close-freshness-study.md`, shadow harness
  `scripts/research/clv_close_freshness_study.py --max-sharp-close-age-minutes`)
  measured on 2026-07-11: uncapped trusted subset n=215 mean clv_log −0.0348
  ± 0.0207 SE; capped at 60 minutes n=52 mean **+0.0042 ± 0.0197**; the
  known-stale excluded mass mean −0.108. Stale sharp closes carry essentially
  all the negative CLV mass — a "close" hours before kickoff is not a close.
  M-clv-1338 documented the missing per-source freshness clock (stored sharp
  closes mean 329–491 min old by anchor type). The arcadia archive admission
  (operator-signed 2026-07-12) is expected to raise fresh-close coverage.

## Proposal (shadow now, definition change only on signature)

At settlement, a sharp close whose snapshot capture is older than
**CLOSE_MAX_AGE_MINUTES = 60** before kickoff is EXCLUDED from the trusted
sharp-close subset with `close_exclusion_reason='stale_close'` (the row keeps
its data; demote-not-drop for evidence). Report-only until signed: the shadow
harness re-reports the capped subset; no production predicate changes.

## Pre-registered adoption criterion (frozen 2026-07-12)

Adopt the 60-minute cap into `_settled_close_is_trusted` ONLY when ALL hold
on evidence accrued AFTER 2026-07-12 (the spent 2026-07-11 measurement that
motivated this ADR may not be reused as confirmation):

1. **Sample floor:** capped trusted subset n ≥ 100.
2. **Replication of direction:** capped-subset mean clv_log minus
   excluded-stale mean clv_log > 0 with the Welch 95% CI of the difference
   excluding 0 (the 2026-07-11 point estimate of this difference was ≈ +0.11).
3. **Coverage floor:** the cap retains ≥ 40% of the uncapped trusted n
   (a gate that starves the evidence base is worse than a stale one).
4. **No fabrication regression:** fabricated-positive and
   implausible-negative rates within the capped subset each ≤ 1%.
5. **Scorecard annotation:** adoption is a trusted-subset definition change
   and MUST be annotated on the scorecard with before/after n and mean,
   exactly like the 2026-07-12 archive-admission annotation.
6. **Rollback:** flag/config revert restores the prior definition; excluded
   rows are stamped, never mutated, so rollback is lossless.

Checkpoints: the standing T+1-week study re-run (~2026-07-18) and every
subsequent re-run until criterion 1 is met. The operator signs at adoption
time; amendments after signature require a new ADR.

## Adoption (SEPARATE from this pre-registration signature)

Pre-registration signed 2026-07-12 (below) locks the criterion. ADOPTION — the
actual definition change in `_settled_close_is_trusted` — happens only at a
future date when a study re-run shows all six conditions met, and requires its
OWN operator sign-off recorded here at that time. Until then the cap is
shadow/report-only via the study harness.

- Adopted: ______________________  Date: ____________

## Signature (pre-registration)

- Operator: **SIGNED — Alexis (operator), 2026-07-12**, via in-session
  directive "proceed with two remaining". This commits the frozen criterion;
  it does not adopt the gate.
