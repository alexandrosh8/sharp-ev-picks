# Roadmap — monitor-and-accrue

> Updated 2026-07-04 (UTC). Current status and limitations live in
> `README.md` ("Status — monitor-and-accrue"); this file tracks posture and
> next milestones only. The original 8-phase build plan (2026-06-10) is
> retired — its history is in git (`git log -- docs/roadmap.md`).

## Where the build plan landed

Phases 1-2 (scaffold, live ingestion), 4 (settlement + CLV loop),
6 (edge-engine hardening, devig sweep), 7 (dashboard — now the
five-workspace SignalDesk) and 8 (Ubuntu production deployment) are
delivered. The standalone-model phases were superseded by evidence: the
football Dixon-Coles model was built but demoted to screens-only
(`PICK_STRATEGY=model`, negative backtested CLV), and no standalone NBA
model was built — basketball instead accrues shadow evidence through the
sharp-vs-soft value pipeline. Sharp-vs-soft line shopping is the live
strategy (`docs/backtesting/value-findings.md`, ADR-0019).

## Standing posture

- **Monitor-and-accrue:** production is monitored, evidence accrues, and a
  clean monitoring round with no changes is a valid, successful outcome.
  Trusted-CLV samples are not yet conclusive for any sport, including the
  live football pipeline. No validated model and no profitability is
  claimed or promised.
- **Validation:** the H2 prospective single-shot and the H6 agreement gate
  are pre-registered and signed (ADR-0019). H2 stays deliberately un-run
  until future BSP data exists and the coverage preflight prints PASS (it
  currently prints DO-NOT-RUN, correctly); H6 is shadow/monitor-only.
- **Sports policy — shadow-first:** football is the only pick-minting
  pipeline. Basketball (closest shadow candidate), tennis and NFL accrue
  shadow or display-only evidence and promote only on trusted CLV, sample
  size, freshness, source agreement and settlement reliability — never a
  bare env flip.
- **Manual betting only:** stakes/edges/EV are informational; the operator
  reviews every pick and places any bet personally. Nothing here is a
  guarantee of profit.

## Next evidence milestones (mirrors README)

1. Monthly per-sport quality reports (coverage, agreement, freshness,
   settlement, sample sufficiency).
2. Trusted-CLV accrual per sport/market/freshness bucket.
3. Source-agreement coverage growth.
4. Settlement-reliability confirmation (first live tennis retirement case
   under the `pinnacle_one_set` convention).
5. The H2 prospective validation, once its data exists and the preflight
   passes.
