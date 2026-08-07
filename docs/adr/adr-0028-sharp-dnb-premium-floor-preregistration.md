# ADR-0028 — Sharp-anchored soccer DNB premium floor 2.0% (pre-registration, PROPOSED)

- **Status:** PROPOSED — operator signature PENDING. Nothing changes at any
  gate until this pre-registration is signed AND, later, the separate adoption
  criterion below is met on post-2026-08-07 evidence with its own adoption
  sign-off. What ships NOW is only the shadow-cohort MARKER (see "Evidence
  accrual"), which is measurement telemetry with zero behavior change to
  tiers, alerts, stakes, or exposure.
- **Context:** the 2026-08-07 diagnosis measured the sharp-anchored soccer DNB
  cell at trusted CLV **+5.78% ± 2.20% (n=54)** — the strongest positive
  trusted cell on the book. Its candidates are currently premium only at
  edge ≥ `VALUE_MIN_EDGE` = 3.0%; the [1.5%, 3.0%) band mints at the volume
  (shadow) tier and is never alerted. If the cell's positive CLV extends into
  that band, the 3.0% floor is leaving alerted +EV volume on the table — but
  the band's OWN CLV is unmeasured and the +5.78% figure comes from the
  ≥ 3.0% cell, so promoting on it would be evidence laundering.

## Proposal (dormant until adoption)

Lower the PREMIUM floor from 3.0% to **2.0%** for candidates that are ALL of:

- sport `soccer*`;
- market `dnb` (draw no bet) exactly — no other market, no derived legs;
- STRICTLY sharp-anchored (`app/edge/value.is_sharp_anchored` — Pinnacle or a
  genuine exchange anchor; the consensus fallback NEVER qualifies).

Every other gate (freshness, odds ceilings, structural sanity, exposure caps,
require-sharp-anchor, draw demotion, etc.) stays exactly as-is; this is a
floor change for one sharp-anchored market cell only.

## Evidence accrual (ships NOW, shadow-only)

So the promotion decision can be made without peeking, the pipeline MARKS —
but does not reprioritize, alert, or restake — every sharp-anchored soccer DNB
candidate whose edge sits in **[1.5%, premium-floor)** and that therefore
failed ONLY the premium floor:

- reason slug **`premium_floor_shadow_dnb`** in `candidate_evaluations.reasons`
  (the existing gate-reason mechanism; surfaced by `/lab/gate-reasons`);
- the same marker as a `reason_summary` note on the minted volume pick.

Band floor 1.5% is frozen at pre-registration
(`app/edge/value.PREMIUM_FLOOR_SHADOW_DNB_MIN_EDGE`) — it is deliberately NOT
a config knob, so the cohort cannot be re-tuned on outcomes. Picks in the band
mint at the volume tier exactly as before the marker existed (tier, stake 0
exposure, no alert — bit-identical behavior).

## Pre-registered adoption criterion (frozen 2026-08-07)

Adopt the 2.0% floor ONLY when ALL hold, evaluated exclusively on closes
captured AFTER 2026-08-07 (the diagnosis sample that motivated this ADR is
SPENT and may not be reused as confirmation):

1. **Sample floor:** the marked [1.5%, 3.0%) cohort's TRUSTED-CLV subset
   (the standing trusted-close definition, ADR-0017/0020/0024 — same devig
   method mint and close) reaches **n ≥ 30**.
2. **The band's OWN CLV:** the cohort's mean trusted clv_log is positive with
   its 95% CI **not below 0** (CI lower bound ≥ 0). The ≥ 3.0% cell's CLV is
   context, never a substitute — the decision statistic is the band's own.
3. **No composition drift:** the cohort remains strictly sharp-anchored soccer
   DNB (any marked row failing that scope is a marker bug to fix first).
4. **Rollback:** adoption is a config floor change
   (`VALUE_MIN_EDGE_PER_MARKET`-style per-market override at the composition
   root, per ADR-0006's policy-not-code principle); reverting the config
   restores the 3.0% floor losslessly.

## Data-peeking prohibition

Until this pre-registration is SIGNED, no one — operator or agent — evaluates,
plots, or summarizes the marked cohort's CLV. The marker accrues silently.
After signature, the cohort may be measured only against the frozen criterion
above; interim peeks that motivate re-tuning the band or the floor invalidate
the pre-registration and require a fresh ADR with a fresh cohort.

## Adoption (SEPARATE from this pre-registration signature)

The pre-registration signature (pending below) locks the criterion. ADOPTION —
the actual 2.0% floor — happens only when a post-signature evaluation shows
all four conditions met, and requires its OWN operator sign-off recorded here
at that time. Amendments after signature require a new ADR.

## Signature

- Pre-registration: PENDING (operator).
- Adoption: N/A (pre-registration not yet signed).
