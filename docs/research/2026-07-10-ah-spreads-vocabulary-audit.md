# AH / spreads market-key vocabulary audit — sign conventions & merge safety

**Date:** 2026-07-10 · **Author:** clv-trueup line-ambiguity workstream
**Question:** can `_canonical_group_detail` (app/clv_trueup.py — now
`canonical_market_detail` in app/pipeline.py) safely fold the Asian-handicap /
spreads vocabularies (`asian_handicap_-1_0` vs `spreads_minus_1`,
`asian_handicap_0_5` vs `spreads_minus_0_5`) the way it folds
`over_under_X_Y` → `totals_X_Y`?

**Verdict: NO — UNSAFE. The spreads/handicap class stays fail-closed.**
The per-pick fix for the live collision victims (picks 62270 "Spain -1",
74637 "England -0.5") is the mint-time `picks.market_detail` stamp
(same-day change), which matches the close on the exact canonical group and
never needs the cross-vocabulary merge.

All evidence below is from the live compose Postgres (`odds_snapshots`,
read-only, queried 2026-07-10 ~05:30 UTC) and from the two parsers
(`app/ingestion/oddsportal.py`, `app/ingestion/oddschecker.py`).

## (a) Do European-handicap keys exist?

- **In code:** yes. OddsPortal's `_market_for_key` maps
  `european_handicap_*` → `Market.SPREADS` and `_selections` emits a 3-way
  group (`team1_handicap` / `draw_handicap` / `team2_handicap`,
  oddsportal.py:2010-2015).
- **In data:** zero rows. The only SPREADS-family key populations are
  `asian_handicap_<line>` (325,276 rows / 209 keys),
  `asian_handicap_games_*` (67,554 / 183) and `spreads_*`
  (3,565,691 / 576). No `european_handicap%` key has ever been stored.
- **BUT the EH *product* is live inside the `spreads_*` key space.**
  OddsChecker's `_is_spread_market_type` accepts any market type containing
  "handicap"/"spread", so the 3-way handicap (with draw leg) lands in the
  same `spreads_<sign>_<line>` keys as the 2-way AH: 44,516 rows across 71
  events / 17 keys carry `Draw ±N` selections (e.g. event 12412
  Spain–Belgium: `spreads_minus_1` holds `Spain -1`, `Belgium -1` **and**
  `Draw -1` from up to 24 books; `spreads_minus_2` holds `Spain -2`,
  `Belgium -2`, `Draw -2`).

## (b) What the key sign means — three incompatible conventions

| Producer | Key form | Sign semantics | Group composition |
|---|---|---|---|
| OddsPortal (`_selections`, oddsportal.py:2005-2009) | `asian_handicap_<±L>` | **HOME team's** signed line | Both sides of ONE book: `{home +L, away −L}`. Verified live: `asian_handicap_-1_0` → `France -1` / `Morocco +1`. |
| OddsChecker (`_market_detail` on the per-bet `raw_bet.get("line")`, oddschecker.py:1150-1156) | `spreads_<minus\|plus>_<L>` | **The named selection's own** signed line (line is per-bet, not per-market) | One key spans BOTH teams at the same signed line, i.e. legs of TWO different books: live event 12909 `spreads_minus_0_5` = `{Muchova -0.5, Noskova -0.5}`, `spreads_plus_0_5` = `{Muchova +0.5, Noskova +0.5}`. |
| Sharp capture (Pinnacle/Betfair fingerprint, 63,902 + 6,506 rows) | `asian_handicap_<L>` / `asian_handicap_games_<L>` **unsigned** | Neither of the above — unsigned keys collide with signed spellings **for the same selections**: `asian_handicap_games_-1_5` and `asian_handicap_games_1_5` both hold `Gigantes San Francisco -1.5` / `Heroes de Moca +1.5` (event 5441); `asian_handicap_-7_0` and `asian_handicap_games_-7` both hold `Aguada Santeros -7` (event 6108). | Same market, two spellings — the key's sign/axis is not trustworthy across producers. |

The selection string itself ("England -0.5") does reliably carry
team + signed line in every vocabulary (`_fmt_line` / `_line_bearing_selection`
both bake `{team} {line:+g}`). The live pick pair confirms the cross-provider
sign flip is a *reference-team* difference, not a data error: pick 74637
"England -0.5" sits under OddsPortal `asian_handicap_0_5` (home Norway? no —
key = home line +0.5, England is the away side) and under OddsChecker
`spreads_minus_0_5` (England's own line −0.5).

## (c) Can two different lines/products share (Market.SPREADS, selection)?

Yes — three distinct ways, each fatal to a merge:

1. **AH vs EH under the same key + same selection string.** OddsChecker's
   `spreads_minus_2` group for event 12412 mixes `Spain -2` prices quoted for
   the 2-way Asian handicap (push on a 2-goal win) and for the 3-way European
   handicap (loses on a 2-goal win, draw leg exists) — different products,
   different fair probabilities, byte-identical `(SPREADS, "Spain -2")` key.
   Nothing in the row distinguishes them.
2. **Absolute-line canonicalization merges two different books.** Both
   `asian_handicap_+L` and `asian_handicap_-L` coexist per event (e.g. events
   2453, 2715: `+1_0` alongside `-1_0` … `-6_5`). `abs(line)` canonicalization
   would fold the home−L book `{home −L, away +L}` and the home+L book
   `{home +L, away −L}` into one 4-selection pseudo-book; `event_fair_probs`
   devigs each group as mutually-exclusive-and-exhaustive, so the merged
   group's close fairs would be normalized across two books (≈halved) —
   corrupted CLV, strictly worse than the fail-closed skip.
3. **OddsChecker groups are already not clean books.** Because the key sign
   follows the *selection's* line, `spreads_plus_0_5` = `{A +0.5, B +0.5}`
   sums to 1 + P(draw) — devig over it is invalid on arrival. Folding
   OddsPortal's clean two-sided AH group into that key space would inherit
   the defect. 294 (event, selection) pairs already appear under BOTH an
   `asian_handicap_*` and a `spreads_*` key.

## Conclusion & disposition

- The merge precondition — "the selection string uniquely pins the line for
  all spreads vocabularies AND no EH/3-way product shares the key space" —
  **fails on both clauses**: the EH product shares both the key space and the
  exact selection strings (finding 1), and no selection-independent
  canonical form exists that keeps different books apart (findings 2, 3).
- `_merge_vocabulary_groups` therefore keeps AH/spreads **unmerged**;
  the line-ambiguity guard stays fail-closed for this class
  (regression-pinned by
  `tests/test_clv_trueup.py::test_merge_vocabulary_groups_never_merges_handicap_vocabularies`
  and the live-pair assertions in `tests/test_pick_market_detail.py`).
- Forward fix shipped instead: picks now persist the mint-time canonical
  `market_detail`, and `revalidate_open_picks` / `finalize_closing_from_snapshots`
  match stamped picks on the exact canonical group — collision-proof without
  ever guessing across vocabularies. Legacy (NULL) picks keep today's
  fail-closed behavior.
- Optional future work (not required for correctness): normalize the
  OddsChecker spreads capture to a home-line, two-sided vocabulary at
  ingestion (and split EH into its own key space) — only that would make a
  cross-provider merge meaningful.
