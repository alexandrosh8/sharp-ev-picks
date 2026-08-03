# ADR-0027: Consensus-anchored picks are informational-only — affirm and pin the never-actionable rule

- **Status:** Accepted (operator "Proceed with all", 2026-08-03)
- **Date:** 2026-08-03
- **Deciders:** operator (GodFather) + Claude session

## Context

The 2026-08-02 post-restatement trusted-CLV scorecard (computed strictly on
the trusted sharp subset — snapshot close, close-independent, sharp anchor,
tautology/fabrication/circularity guards; same source as ADR-0026) splits
live CLV by anchor type:

| Cell | n | mean clv_log | 95% CI |
| --- | --- | --- | --- |
| consensus-anchored (all) | 153 | **−0.0705** | **[−0.1230, −0.0180]** |
| genuine Betfair-Exchange-anchored | 58 | +0.0535 | [+0.0121, +0.0948] |
| Pinnacle-anchored | 19 | +0.0635 | [+0.0333, +0.0938] |

Consensus-anchored picks carry significantly negative trusted CLV (CI
excludes 0 from below), while both genuinely sharp-anchored cells are
positive with CIs excluding 0. The evidence cleanly separates on anchor
provenance, not on sport or market.

### What the code already enforces (verified 2026-08-03)

This ADR ratifies an existing posture rather than introducing new code:

- **Dashboard actionability.** `isActionable`
  (`app/api/dashboard_src/app.js:556-572`) requires
  `p.anchor_type === "pinnacle" || p.anchor_type === "sharp"` (line 564) —
  a consensus-anchored pick can never enter the Actionable group
  (`edgeGroupOf`, line 604) or the qualified-premium count (line 1383).
  Consensus rows additionally render as untrusted (`lacksSharpAnchor`,
  line 2259), show the neutral "Consensus Anchor" label (lines 612-617,
  2418), the hollow trust glyph "○" (lines 1737-1741), and their suggested
  stake is gated to "informational only — not applicable" (lines 2325-2335).
- **Mint-time tiering.** With `value_require_sharp_anchor = True`
  (`app/config.py:558`, code default; live via `.env` since 2026-06-26), a
  premium candidate whose fair value came from the soft consensus median is
  demoted to the volume (shadow) tier at mint
  (`app/pipeline.py:2533-2550`); for sports in
  `sharp_anchor_only_sports` (tennis, per the −37.9% consensus cell,
  2026-07-26) it is hard-dropped with named reason
  `consensus_anchor_dropped` instead.
- **Alerts.** Alert dispatch is premium-only — the volume branch persists
  and CLV-tracks but never dispatches (`app/pipeline.py:2915-2926`; volume
  alerting was trialed 2026-06-23 and reverted). Since consensus-anchored
  candidates are demoted to volume before this branch, no consensus pick
  reaches Telegram/webhook.
- **Exports.** The dashboard CSV export (`app/api/dashboard_src/app.js:1955`)
  emits `anchor_type` as a data column and carries no actionability flag —
  it cannot present a consensus pick as bettable. There is no server-side
  pick export.

### Residual coupling (report-only, no change decided here)

Alert-surface enforcement is **indirect**: it holds because the
`require_sharp_anchor` demotion runs upstream of the premium-only dispatch,
not because the alert path checks `anchor_type` itself. If
`VALUE_REQUIRE_SHARP_ANCHOR` were ever flipped to false (or volume alerting
re-enabled — deliberately a one-line change,
`app/pipeline.py:2922-2923`), a consensus-anchored pick could alert with
only the "(Consensus)" label from `app/notifications/base.py:115` to mark
it. The dashboard's `isActionable` would still refuse it independently.
This ADR pins the rule so any such flip is recognized as contradicting an
accepted decision, not a free tuning knob.

## Decision

1. **Affirm and pin the rule at every surface:** consensus-anchored picks
   are informational/tracking only — never actionable on the dashboard,
   never alerted, never exposure-reserving, and never presented as bettable
   in any export or future surface. Any change that would let a
   consensus-anchored pick surface as actionable (including flipping
   `VALUE_REQUIRE_SHARP_ANCHOR` off or re-enabling volume alerting while
   consensus picks mint there) requires superseding this ADR with an
   operator-signed decision.
2. **Consensus picks continue to mint at the volume tier** for telemetry
   and CLV accrual. Stopping the mints would blind the funnel: the review
   condition below is only evaluable if consensus-anchored trusted closes
   keep accruing.
3. **Pre-registered review condition:** revisit (do not auto-flip) only if,
   on post-2026-08-03 data, consensus-anchored trusted CLV reaches
   **n ≥ 100 with the 95% CI of mean clv_log excluding 0 from above**. The
   decision itself stays operator-signed, consistent with the shadow-first
   promotion policy.

## Consequences

- No behavioral change ships with this ADR — it converts an emergent
  property of two independent gates into a pinned, citable rule, and makes
  the config flags that uphold it decision-protected rather than tunable.
- The negative-CLV consensus cell (−7.05%) keeps accruing evidence at zero
  exposure cost (volume tier reserves nothing), preserving the ability to
  detect if consensus anchoring ever becomes genuinely +CLV.
- Picks, edges, EV, and stakes remain informational throughout — this
  system never places bets, and nothing here presents betting as
  guaranteed profit; the operator reviews picks and places any bet
  manually.
- Rollback: this ADR decides posture, not code. Reverting it means writing
  a superseding ADR against the pre-registered review condition in
  Decision 3; no config or code change is needed to keep the current
  behavior.
