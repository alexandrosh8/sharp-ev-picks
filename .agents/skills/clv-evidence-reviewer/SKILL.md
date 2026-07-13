---
name: clv-evidence-reviewer
description: "Trust rules for this repo's live CLV evidence — tautology/fabrication/circularity guards, the trusted sharp subset, and the SQL split patterns to audit them. Use when reviewing CLV numbers, /performance output, close-capture changes, or any claim that live CLV is positive/negative."
allowed_tools:
  - Read
  - Grep
  - Glob
  - Bash
---

# CLV Evidence Reviewer

Judge whether a CLV figure in this repo is EVIDENCE or an artifact. Never let a
blended/indicative number pass as the trusted one.

## Trust rules (definitions live in code — cite these, don't restate from memory)

- **Tautological close** — `_clv_row_is_tautological` (app/storage/repositories.py:792):
  `abs(closing_fair_probability - model_probability) <= CLV_TAUTOLOGY_EPS (1e-3)`.
  The clv_log re-encodes the pick-time edge. Excluded from BOTH the blended headline
  and the trusted sharp subset. Live rate ≈44% of settled picks (2026-07 audit).
- **Fabricated close** — `_clv_row_is_fabricated` (repositories.py:829): close-implied
  edge `closing_fair - 1/decimal_odds > 0.20` (CLV_IMPLAUSIBLE_CLOSE_EDGE) or
  `|clv_log| > 0.5`. Residue of the fixed double-chance orientation bug.
- **Circular close** — `persisted_close_independent` (app/edge/value.py:181): fill book
  priced its own close OR close fair didn't move (> CLV_TAUTOLOGY_EPS, value.py:163).
  Trusted subset requires `close_independent_of_fill IS TRUE` — NULL is NOT trusted.
- **Trusted sharp subset gate** (repositories.py:1014-1037): not excluded AND
  `has_snapshot_close` truthy AND `closing_anchor_type IN ('pinnacle','sharp')`
  (_SHARP_CLOSE_ANCHORS, :756) AND `close_independent_of_fill IS TRUE` AND symmetric
  devig fallback (`_devig_fallback_asymmetric`, :899).
- **Honesty floors**: MIN_HEADLINE_N = 50 (repositories.py:764) nulls headline
  roi/CLV/beat-rate below n; MIN_STRATUM_N = 50 (app/backtesting/live_evidence.py:43)
  per stratum. Significance: t-CI must exclude 0, Wilson low > 0.5 (repositories.py:865).
- **CLV before ROI at small n**: at n<~200 ROI is noise; the decision instrument is
  trusted-sharp CLV with its CI. Never quote ROI without its n and roi_status.

## SQL split patterns (read-only prod: docker exec betting-ai-postgres-1 psql -U betting_ai -d betting_ai)

- Population: `picks p JOIN result_tracking rt ON rt.pick_id=p.id` + settled_at window.
- Tautology flag in SQL: `abs(p.closing_fair_probability - p.model_probability) <= 0.001`.
- Always split by `p.anchor_type (mint) x p.closing_anchor_type x sport x p.market`:
  same-source cells (consensus→consensus, pinnacle→pinnacle) carry ~all tautologies;
  cross-source cells are ~0% tautological — a different source proves independence.
- Close freshness: join `odds_snapshots os ON os.event_id=p.event_id AND
  os.captured_at <= e.starts_at`; `bool_or(os.captured_at > p.created_at)` separates
  echo-of-mint-line from a fresh-but-unmoved observation.
- Sharp sources: Betfair rows = `os.bookmaker ILIKE 'betfair%'` on the pick's OWN event
  (liquidity NULL = main scrape, set = dedicated capture — do NOT drop the main-scrape
  rows, they carry ~95% of Betfair coverage). Pinnacle archive rows live on SEPARATE
  events under `sports.key LIKE 'pinnacle%'`, matched by name — join via
  `event_source_links (source='pinnacle_arcadia')` where populated, never by fuzzy SQL.

## Gotchas

- **Tautological ≠ fake.** A fresh close observation of a genuinely unmoved line also
  trips the 1e-3 equality — the guard keys on VALUE, not provenance. ~half the live
  tautologies are fresh-static soft markets (revalidated_at median T-9m). Without
  persisted close provenance you cannot tell echo from static; don't "fix" the rate by
  widening the epsilon or dropping the guard.
- **A stale sharp row outranks a fresh consensus close.** finalize_closing_from_snapshots
  (app/clv_trueup.py:877-890) injects Pinnacle/Betfair archive rows whenever the SOFT
  scrape is fresh, with no per-source freshness check — Betfair-anchored closes run
  median ~4-5h old. Treat `closing_anchor_type='sharp'` + old sharp rows as suspect.
- **`has_snapshot_close IS NULL` means the poll-time fallback close stood** (finalize
  returned False: soft gap > SNAPSHOT_CLOSE_MAX_GAP=4h and no fresh sharp, or no
  anchorable fair at close). ~half of settled picks. These can never enter the trusted
  subset; don't count them as "closes with provenance".
- **model_probability is the pick-time MARKET fair only under the VALUE strategy**
  (strategy coupling, repositories.py:804-816). On model-strategy rows the tautology
  guard silently no-ops — never generalize the SQL equality test to those rows.
- **Change-only persistence: per-row age never gates validity by itself.** A days-old
  row can be a true close if the event kept being scraped/captured (Arcadia is
  version-gated at 120s). Coverage is judged on the EVENT/SOURCE last-capture clock
  (closing_odds_from_snapshots, repositories.py:1584), not per-row age.
- **btts (and most obscure-league spreads/totals) have NO sharp close source.** Their
  tautology/consensus share is structural — more capture cannot fix it. Scope any
  "improve the close" work to markets a sharp book actually prices.
