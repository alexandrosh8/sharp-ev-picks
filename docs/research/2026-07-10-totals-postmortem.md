# Soccer totals CLV post-mortem — verdict: market-signal

**Date:** 2026-07-10 (UTC) · **Task:** strategy-revision plan Task 3
(`docs/superpowers/plans/2026-07-10-strategy-revision.md`)
**Script:** `scripts/research/totals_clv_postmortem.py` (READ-ONLY, SELECT-only)
**Run:** `docker exec -w /srv/betting-ai betting-ai-app-1 /srv/betting-ai/.venv/bin/python
scripts/research/totals_clv_postmortem.py` against the live prod DB
(host-run `uv run python` cannot reach 127.0.0.1:5433 from the sandbox; the
container run hits the same postgres service the app uses).
**Trust rules:** exact mirror of the repository trusted sharp subset
(`app/storage/repositories.py`: non-tautological, non-fabricated,
`has_snapshot_close`, sharp `closing_anchor_type`,
`close_independent_of_fill IS TRUE`, symmetric devig-fallback flags) — the
totals headline reproduced the plan's number exactly (n=24, −0.0602, SE 0.0241).

## Question

Soccer totals trusted sharp-CLV is **−0.0602 (SE 0.0241, n=24)** while the
soccer spreads control cell is positive. Is the drag (a) a close-matching
defect (line mismatch), (b) devig structure, or (c) genuine market signal?

## Verdict

**market-signal** (most likely by a clear margin; hypotheses ranked below).

- Totals trusted CLV: mean −0.0602, SE 0.0241, n=24 → t(23) = −2.50,
  95% t-CI **[−0.110, −0.010] — excludes 0 on the negative side.**
- Spreads control (same gate, same window): mean +0.0336, SE 0.0275, n=35,
  95% t-CI [−0.022, +0.090]. Totals−spreads difference ≈ −0.094
  (Welch SE ≈ 0.037, t ≈ −2.6) — the two cells statistically separate.
- Both bug hypotheses were tested directly and found unsupported (below), so
  this is **not** `insufficient-n`: the headline is significant and the
  alternative explanations were affirmatively probed, not just under-powered.

**Decision (per the plan's rule):** totals stays **volume-only** (Task 2
premium block stands; demote-not-drop, evidence keeps accruing). The standing
re-promotion criterion remains the Task 2 one — trusted totals CLV CI > 0 at
n ≥ 50 — which also functions as the falsifier if the caveats below turn out
to matter.

## Hypothesis (a) — close-matching defect: REFUTED

1. **Global warehouse line-integrity check:** across **30,828** distinct
   `(market, selection)` snapshot pairs on full-match totals/handicap keys
   (`totals_*`, `over_under_*`, `asian_handicap_*`, `spreads_minus/plus_*`),
   **0 pairs** have a selection-embedded line different from the market-key
   line. A line-bearing selection literal ("Under 2.5") always pins its own
   line, so the legacy line-blind `(market, selection)` close match cannot
   grade a 2.5 pick against a 3.0-line close. (5,789 pairs carry lineless
   selections — bare "Over"/"Under" — but none of the 24 trusted totals picks
   are lineless: all 24 selections embed line 2.5.)
2. **Direct per-pick verification** via the D3 close provenance
   (`close_anchor_book` + `close_snapshot_captured_at` → matched
   `odds_snapshots` rows):

   | close-line check (totals, trusted) | n | mean CLV | SE |
   |---|---|---|---|
   | line_consistent (verified same line) | 11 | −0.0666 | 0.0164 |
   | unverifiable (pre-provenance rows)   | 13 | −0.0547 | 0.0431 |

   The **verified-consistent** subset is the *most* negative — the opposite of
   what a line-mismatch defect predicts. Every settled row predates the
   2026-07-10 `market_detail` mint stamp (all 24 legacy line-blind), but the
   line-blind path was provably safe for this cohort.

## Hypothesis (b) — devig structure: UNSUPPORTED

The trusted subset already excludes asymmetric devig fallbacks; within it:

| mint_fb × close_fb (totals) | n | mean CLV | SE |
|---|---|---|---|
| False × False (symmetric, recorded) | 20 | −0.0773 | 0.0262 |
| None × False (pre-column mint flag) | 4 | +0.0252 | 0.0438 |

Mint and close fairs are devigged from the **same 2-way group with the same
configured method** on every row (no 3-way-fitted method applied to a 2-way
market anywhere in the chain), and the dominant symmetric-method cell carries
the whole drag. The positive n=4 cell is a time cohort (pre-column rows), far
too small to indicate a devig artifact. Spreads shows the same pattern with
flipped sign (False×False n=28 +0.0103; None×False n=5 +0.1380).

## Hypothesis (c) — market signal: SUPPORTED

The drag is uniform across every split; the control cell is not.

| split | totals | spreads (control) |
|---|---|---|
| headline (trusted) | n=24, **−0.0602**, SE 0.0241 | n=35, +0.0336, SE 0.0275 |
| close book: Betfair Exchange | n=11, −0.0666 | n=6, −0.0493 |
| close book: (null, pre-provenance) | n=13, −0.0547 | n=23, +0.0597 |
| close book: Pinnacle | — | n=6, +0.0163 |
| mint anchor: Betfair Exchange | n=8, −0.0342 | n=19, +0.0528 |
| mint anchor: consensus(median) | n=14, −0.0877 | n=14, +0.0091 |
| mint anchor: Pinnacle | n=2, +0.0283 | n=2, +0.0226 |
| line | 2.5: n=24 (−0.0602) — **100% of totals picks are line 2.5** | \|0.5\|: n=15 +0.0510 · \|1.5\|: n=13 +0.0232 · \|2.5\|: n=7 +0.0155 |

Reading: totals is negative under *both* close-anchor provenances and *both*
major mint anchors (consensus-anchored worst at −0.0877); spreads is positive
in the same cells. This matches the literature prior already cited in the
plan (Hegarty & Whelan: AH/spreads unbiased; derived/totals markets weaker) —
sharp books price the heavily-traded 2.5 goals line efficiently, and our soft
line-shopped totals prices do not beat that close.

## Honest caveats (why this stays falsifiable at n ≥ 50)

1. **n=24 is small.** The CI excludes 0 but its edge is −0.010; per-cell split
   n's (2–14) cannot rule out cell-level noise. The Task 2 re-promotion gate
   (CI > 0 at n ≥ 50) is the standing re-check.
2. **Stale sharp closes.** On rows with capture provenance, the trusted totals
   "close" is a median **~239 min (~4 h)** pre-kickoff Betfair row (spreads:
   ~41 min) — the known stale-sharp-close pattern (`clv-evidence-reviewer`
   gotchas; next-session queue item #1, close-freshness shadow study). A T−4h
   sharp price is a mid-market reference, not a true close; direction of the
   induced bias is unknown, so this widens uncertainty without pointing at a
   bug in matching or devig.
3. **Pre-fix cohort.** All trusted rows (both markets) were minted
   2026-06-29 → 2026-07-03, i.e. before the 2026-07-07 selection fixes. The
   verdict describes that cohort; post-fix totals evidence accrues in the
   volume tier.
4. **Spreads headline drift vs the plan.** The plan quoted +0.0518 (n=34);
   this run measures +0.0336 (n=35) under the CURRENT repository gate. The
   delta is one deep-negative row (pick 2045, clv −0.586, odds 7.0) that the
   since-fixed unconditional `|clv_log|>0.5` cutoff would have dropped; the
   current inputs-present rule correctly keeps it. Totals reproduced exactly.

## Decision rule applied

- ~~close-matching-bug~~ → would have meant TDD fix + re-promote after n≥50
  CI>0. Refuted.
- ~~devig-structure~~ → unsupported.
- **market-signal → totals stays volume-only** (per plan: permanently, i.e.
  no re-promotion initiative; the Task 2 criterion — trusted CLV CI > 0 at
  n ≥ 50 — remains the only path back and requires no action to keep
  measuring).
- ~~insufficient-n~~ → not applicable: headline CI excludes 0 and both bug
  hypotheses were affirmatively probed. Had the CI straddled 0, the ranking
  would have been market-signal > devig-structure > close-matching-bug.
