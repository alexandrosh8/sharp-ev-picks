# ADR-0023 (DRAFT — needs operator signature): Betfair-Exchange backtest anchor to replace dead Pinnacle columns

Date: 2026-07-10 · Status: DRAFT (pre-registration; no evaluation may run before
the operator signs and the fresh data exists)

## Context (verified 2026-07-10)

- football-data.co.uk Pinnacle columns (PSH/PSCH…) are permanently dead in
  current files (populated only for pre-2026-01-15 rows of 2025-26).
- The SAME free files carry Betfair Exchange 1X2 OPEN (BFEH/BFED/BFEA) and
  CLOSE (BFECH/BFECD/BFECA), plus OU2.5 and AH open+close pairs. Verified
  population: 2025-26 E0 358-360/380 (open≠close 93.3%); **2024-25 E0 380/380
  with BOTH pairs (BFE + PS) fully populated** — an anchor-overlap season.
- Older seasons (2324 and earlier) have Pinnacle only, NO BFE columns.
- Operator mandate: FREE data only (BettingIsCool et al. rejected on cost).

## Decision (three steps, each gated)

**Step A — anchor-agreement study (allowed immediately; consumes nothing).**
On 2024-25 (both pairs populated), measure agreement between the
Pinnacle-devigged close and the commission-netted Betfair-Exchange-devigged
close using ODDS COLUMNS ONLY — no outcomes loaded, no strategy run, no
selection: per-outcome fair-probability deltas, bias by odds band, and the
devig-method sensitivity. Pass criterion (frozen now): mean |Δfair| ≤ 0.01
with no odds-band bias exceeding 0.02 → BFE is a faithful anchor substitute.
Rationale: this is a DATA-QUALITY property measurement (like the 2026-06-12
consensus-anchor PS-free verification), not a strategy look.

**Step B — no development.** The validated Pinnacle-anchored strategy
(edge ≥ 3% vs devigged sharp close; ceiling 4.0; power devig) transfers
UNCHANGED to the BFE anchor with exchange commission netted on the EV side.
No parameter may be re-tuned on 2425/2526 (spent) for this switch.

**Step C — single-shot evaluation on season 2026-27 (the only fresh domain).**
When the 2627 files exist with ≥ N=150 selectable matches: run the unchanged
strategy once against the BFE anchor. Frozen metric: incremental CLV vs the
commission-netted BFEC close, bootstrap CI at match level; PASS iff
incCLV − 2SE > 0. ROI reported descriptively only. One look; the marker file
pattern of AH_ONESHOT_CONSUMED.json applies.

## NBA / Euroleague (asked 2026-07-10)

football-data.co.uk is soccer-only; NO free historical sharp open+close
exists for basketball (re-verified again today: BettingIsCool is paid and
rejected; SBR dead; Kaggle dumps consensus/soft; Betfair historic useful
tiers are paid). Therefore NBA/Euroleague CANNOT get a historical backtest
under the free-only mandate. Their path stays the ALREADY-RUNNING forward
self-capture: Arcadia pinnacle_basketball (median close capture T-13min) +
Betfair exchange rows + shadow-tier picks accruing trusted CLV (basketball
spreads +0.1054, SE 0.0136, n=23 as of today). Promotion decision at the
pre-registered gate: trusted CLV 95% CI > 0 at n ≥ 50 plus the shadow-first
policy checks. Euroleague rides the same basketball capture when in season;
its settlement has no ESPN feed (no euroleague slug) and relies on the
scraped-score path — hardened today by the plausibility gates — with manual
fallback.

## Signature

Operator: ______________  Date: __________
(Until signed: Step A may run; Steps B/C may not.)
