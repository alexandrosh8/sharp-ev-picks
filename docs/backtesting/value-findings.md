# Value-Strategy Findings — Review-Corrected (v2)

- **Date:** 2026-06-10 (v2 — after the 23-finding deep review; methodology
  corrected, claims re-validated on held-out data)
- **Strategy:** sharp-vs-soft line shopping (`app/edge/value.py`,
  `scripts/value_backtest.py`). Fair value = devig(Pinnacle pre-match); bet
  the best available price (Max across books) when it beats fair by ≥
  threshold. **No goals model.**

## Corrected methodology (what changed in v2 and why)

The first version of this document overstated the result. The deep review
found and we fixed:

1. **One bet per match** (highest-edge selection only) — H/D/A bets on the
   same match are correlated; counting them separately inflated n and
   narrowed the CI illegitimately.
2. **The right null is "bet everything", not zero.** Betting the Max line on
   every match already shows +0.009–0.016 CLV mechanically (best-of-N-books
   premium). Selection skill = CLV **incremental** to that baseline.
3. **Dual CLV references**: vs devig(Pinnacle close) _and_ vs
   devig(Max-of-books close). The second strips the best-price premium
   entirely — the strictest test.
4. **No in-sample headline.** Thresholds are swept on TRAIN seasons
   (2021-24); the chosen threshold is evaluated ONCE on held-out TEST
   seasons (2024-26).
5. The printed verdict is **computed from the held-out numbers** — the
   script can and will print "NO PROVEN EDGE" if that's what the data says.

## Held-out result (6 leagues; train 2122-2324, test 2425-2526)

Train sweep chose **edge ≥ 0.015** (best train ROI with n ≥ 100). Held-out:

|                           | n       | hit%     | ROI         | CLV vs Pinnacle close | CLV vs Max close     | incremental CLV     |
| ------------------------- | ------- | -------- | ----------- | --------------------- | -------------------- | ------------------- |
| baseline (bet everything) | 1994    | 43.6     | −1.17%      | +0.0090 ± 0.0035      | +0.0055              | —                   |
| **picks (edge ≥ 0.015)**  | **126** | **59.5** | **+12.67%** | **+0.0351 ± 0.0194**  | **+0.0278 ± 0.0193** | **+0.0261 (> 2SE)** |

**Computed verdict: POSITIVE selection skill on held-out data** — the picks'
CLV clears the bet-everything baseline by more than 2 standard errors, ROI is
positive, and the edge survives even the Max-of-books closing reference
(i.e., it is not just the best-price premium).

## Engine hardening (same review, `app/edge/value.py`)

- **Exchange commission netted out** before any comparison/edge/EV
  (Betfair 5%, Smarkets/Matchbook 2%, configurable) — gross exchange prices
  otherwise fake edges the size of min_edge.
- **No-Pinnacle fallback is a ≥3-book median consensus**, never a single
  lowest-overround book — one stale quote can no longer contaminate fair
  value for every selection (the review's worked example is now a test).
- **Anchors with implausible overround** (underround or > 0.12) are rejected;
  the market is skipped.
- **min_odds gate (default 1.30)** — ultra-short "edges" are devig noise.
- 14 unit tests cover these paths, including the review's adversarial cases.

## Honest caveats (unchanged in spirit, sharpened in detail)

1. **n = 126 on holdout is modest**; the CLV CI excludes zero but not by
   much against the Max-close reference. Keep tracking live CLV per pick.
2. **Best-price assumption**: capturing the Max line live needs many book
   accounts and prompt action; realized prices are often a notch lower.
3. **Soft books limit/close winning accounts** — the structural constraint on
   every value bettor; volume is harder than the table implies (~120
   bets/year across 6 leagues at this threshold).
4. The OddsPortal free scrape only supports this where a match lists enough
   books (and rarely Pinnacle); The Odds API `regions=eu` is the better live
   feed for the validated Pinnacle-anchored path.
5. Decision-support only: the system finds the value and names the
   book/price; the user reviews and places any bet. Nothing is a guarantee
   of profit.

## How it's wired

- `app/edge/value.py::find_value_bets` — pure, review-hardened, 14 tests.
- `scripts/value_backtest.py` — the v2 validation above (re-runnable).
- `scripts/value_picks.py` — LIVE picks with min-odds gate; names the exact
  book and price.
