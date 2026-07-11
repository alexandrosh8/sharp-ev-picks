# ADR-0022 — Evidence-scoped tiering: pre-registered promotion / kill criteria (ACCEPTED)

- **Status:** ACCEPTED — signed by the operator 2026-07-11 (in-session
  "proceed" on the decision list). Criteria below are binding; written BEFORE
  the evidence arrives so no future decision re-tunes on observed data.
- **Date:** 2026-07-10 (drafted) / 2026-07-11 (signed)
- **Context:** strategy-revision plan Task 7
  (`docs/superpowers/plans/2026-07-10-strategy-revision.md`). Companion
  evidence: totals post-mortem (`docs/research/2026-07-10-totals-postmortem.md`,
  verdict market-signal), CLV close-freshness study
  (`docs/research/2026-07-10-clv-close-freshness-study.md`).
- **Instrument:** trusted sharp-close CLV (the `_settled_close_is_trusted`
  subset: snapshot close, named sharp anchor, close-independent-of-fill,
  non-tautological, non-fabricated, symmetric devig) with 95% t-CI — never
  small-n ROI. Sample floors per the shadow-first policy memo (2026-07-04).

## Binding criteria (verbatim; operator signs below)

1. **Soccer totals re-promotion.** Totals is premium-blocked
   (`VALUE_MIN_EDGE_PER_MARKET=totals:0.99`, Task 2) on measured trusted CLV
   −0.0602 (SE 0.0241, n=24, CI excludes 0) with post-mortem verdict
   *market-signal*. Re-promote ONLY when trusted totals CLV 95% CI > 0 at
   n ≥ 50 on evidence accrued AFTER 2026-07-10. Otherwise volume-only
   permanently.
2. **Basketball spreads promotion to premium.** Requires trusted CLV 95%
   CI > 0 at n ≥ 50 AND source agreement + freshness + coverage per the
   shadow-first policy (2026-07-04). At n=23 (accruing) — do not promote
   early regardless of the point estimate.
3. **Premium tier kill criterion.** If the post-fix premium cohort (minted
   after 2026-07-07) reaches trusted CLV 95% CI < 0 at n ≥ 50, premium
   alerts pause automatically pending operator review. (Cohort stands at
   8/50 trusted closes settled as of 2026-07-10.)
4. **Soccer h2h review trigger.** All-history soccer h2h trusted CLV is
   −11.4% at n=62 (threshold met, mixed pre/post-fix cohorts and tiers).
   This does NOT auto-change scope (the pre-fix cohort dominates); it DOES
   require the criterion-3 cohort split to be reported on the scorecard, and
   if the POST-FIX h2h premium cell alone reaches CI < 0 at n ≥ 30, h2h
   joins totals in the per-market premium block pending review.
5. **Uncertainty-shrink enforcement (Task 5 flag).** Only after ≥ 30 days of
   shadow annotations show the shrunk stakes would not have cut aggregate
   trusted-CLV-weighted EV by more than the drawdown reduction justifies
   (comparison reported; operator signs).
6. **CLV_USE_PINNACLE_ARCHIVE flip.** Per the close-freshness study's
   six-condition draft criterion (match-rate floors, paired-delta bound,
   guard-trip ceilings, ≥30-pick manual wrong-game audit with 0 wrong
   attachments, settlement-path-only, rollback = flag off). The flip also
   restates the trusted aggregate (~n=215→296) and MUST be annotated as a
   definition change on the scorecard.
7. **No re-tuning on the spent holdout.** Sizing/threshold changes are NEVER
   tuned on seasons 2425+2526; fresh evidence = live shadow or the season
   2627 single-shot (ADR-0023) only.
8. **BTTS premium-block** (signature-time amendment, 2026-07-11, per the
   one-time-amendment rule below). BTTS has NO trusted-close instrument
   (0/70 settled picks; the arcadia namespace structurally lacks BTTS) —
   premium BTTS picks can never be CLV-validated. BTTS is premium-blocked
   (`VALUE_MIN_EDGE_PER_MARKET` gains `btts:0.99`, demote-not-drop).
   Re-promote ONLY when a verifiable independent sharp-close source for BTTS
   exists AND trusted BTTS CLV 95% CI > 0 at n ≥ 50.

## Signature

- Operator: **SIGNED — Alexis (operator), 2026-07-11**, via in-session
  directive: "proceed Decisions waiting on you — 1. Sign ADR-0022 (tier
  criteria) … 2. btts:0.99 premium-block … say the word." The btts block is
  the operator's one-time signature amendment (criterion 8).

After this signature, amendments require a new ADR.
