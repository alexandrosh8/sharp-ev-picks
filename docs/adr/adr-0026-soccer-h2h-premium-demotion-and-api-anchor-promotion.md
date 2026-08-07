# ADR-0026: Demote soccer h2h premium to visibility-only; promote the fresh Betfair API price to the anchor

- **Status:** Accepted (operator "Proceed", 2026-08-02)
- **Date:** 2026-08-02
- **Deciders:** operator (GodFather) + Claude session

## Context

The 2026-08-02 bookmaker-label restatement (see
`scripts/restate_oddschecker_betfair_labels.py`, ADR-relevant background in
the session memory `oddschecker-bookmaker-code-recycle-2026-08-02`) restated
2.33M rows after discovering the OddsChecker code recycle (OE=10bet was
persisted as "Betfair Exchange" for the entire oddschecker era). The
post-restatement trusted-CLV scorecard — computed strictly on the trusted
sharp subset (snapshot close, close-independent, sharp anchor, tautology/
fabrication/circularity guards) — reads:

| Cell | n | mean clv_log | 95% CI |
| --- | --- | --- | --- |
| soccer h2h (LIVE premium) | 109 | **−0.081** | **[−0.148, −0.015]** |
| soccer totals (LIVE premium) | 55 | −0.034 | [−0.060, −0.008] |
| consensus-anchored (all) | 153 | −0.0705 | [−0.1230, −0.0180] |
| genuine Betfair-Exchange-anchored | 58 | +0.0535 | [+0.0121, +0.0948] |
| Pinnacle-anchored | 19 | +0.0635 | [+0.0333, +0.0938] |

The live premium cells are the ones with significantly negative trusted CLV,
while every shadow cell is (correctly) held back by the promotion policy —
the live/shadow boundary was misaligned with the evidence in the wrong
direction. Separately, `betfair_anchor_verdicts` shows the inline-scraped
exchange anchor disagrees with the fresh read-only Betfair API price by more
than the tick tolerance in 179 of 204 fresh comparisons (88%) — the inline
anchor is systematically stale relative to the API.

## Decision

1. **Soccer h2h is capped at the volume tier** via
   `VALUE_VISIBILITY_ONLY_MARKETS=soccer:asian_handicap,soccer:h2h`
   (existing mechanism; demote-not-drop — picks remain visible/tracked,
   never actionable premium). Soccer totals stays live for now: its CI is
   negative but the effect is small (−3.4%) and dominated by the same tail/
   consensus slices already gated; revisit with the h2h re-promotion check.
2. **The fresh Betfair API price is promoted to the anchor** via
   `VALUE_BETFAIR_API_PROMOTE=true` (shadow-designed switch; the read-only
   GET-only API integration per the operator authorization of 2026-06-29).
   The staleness guard (`VALUE_BETFAIR_STALENESS_GUARD=true`, ENFORCING
   since 2026-08-02) remains as the disagreement backstop.

## Re-promotion criteria (pre-registered)

Soccer h2h returns to premium eligibility only when, on POST-restatement,
post-2026-08-02 trusted closes: n ≥ 50 AND the 95% CI of mean clv_log no
longer excludes 0 from below. Evaluate via the Lab promotion-distance
readout; the decision itself stays operator-signed.

## Consequences

- Premium volume drops (soccer h2h was the flagship premium cell); the
  remaining premium surface is sharp-anchored non-h2h soccer plus whatever
  passes gates elsewhere — consistent with the evidence that genuinely
  sharp-anchored picks carry positive trusted CLV.
- Anchor quality improves on every Betfair-anchored pick (API price at
  anchor time instead of a stale inline scrape), which also improves the
  honesty of the CLV fill side going forward.
- Rollback: remove `soccer:h2h` from the env list / set
  `VALUE_BETFAIR_API_PROMOTE=false`; both are pure config.

## Amendment 2026-08-07 — soccer AH cap covers the scraped `spreads_*` vocabulary

The `soccer:asian_handicap` visibility entry only matched the OddsPortal
feed's `asian_handicap_<line>` details; the OddsChecker scrape keys the SAME
soccer handicap product under native `spreads_<line>` details
(`spreads_minus_1`, `spreads_minus_0_75`, …), which therefore bypassed the
cap — a vocabulary hole. Verified live 2026-08-07: the Celtic −1
`spreads_minus_1` group reached tier=premium at a fabricated 14.6% edge
(Betfair Asian-line legs devigged against soft European-handicap quotes plus
an ~11 h-stale Draw leg; the class is now refused fail-closed by
`spread_pair_incoherent`, f008173). `app/config.py
parse_visibility_only_markets` now expands `soccer:asian_handicap` to also
emit `soccer:spreads` (soccer-scoped; the plain all-sports `asian_handicap`
key and other sports' entries are NOT expanded), so scraped soccer spreads
candidates are capped at the volume tier until forward evidence clears the
market. Rollback: pure config (remove the entry); no env change was needed to
deploy the amendment.
