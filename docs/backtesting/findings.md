# Backtest Findings — Does the Pick Finder Actually Have Edge?

- **Date:** 2026-06-10
- **Method:** walk-forward (`scripts/backtest.py`, `app/backtesting/walkforward.py`),
  leakage-safe: for each match, fit Dixon-Coles only on results STRICTLY
  before its date (rolling 540-day window, weekly refit), bet the 1X2 market
  at Bet365 pre-match odds when the model shows edge, settle on the real
  result, measure ROI and **CLV vs the Pinnacle closing line**. Flat 1u stakes.

## Headline result: the naive Dixon-Coles strategy does NOT beat the market

| League                       | edge≥0.05 bets | hit% | ROI%     | mean CLV   | beats close % | Kelly bankroll |
| ---------------------------- | -------------- | ---- | -------- | ---------- | ------------- | -------------- |
| EPL (E0, 5 seasons)          | 923            | 34.1 | **−3.4** | **−0.075** | 20.4          | ×0.30 (−70%)   |
| Championship (E1, 5 seasons) | 1401           | 32.7 | **−9.1** | **−0.072** | 18.8          | ×0.06 (−94%)   |

Every edge threshold (0.02 → 0.15) loses money in both leagues. **CLV is
strongly negative and statistically conclusive** (EPL: −0.075 ± 0.007 at 95%):
the picks beat the Pinnacle close only ~20% of the time. Negative CLV is the
definitive signal — it means the model's "edges" are illusory; the market
price is already sharper than the model, and the line moves further against
the pick by kickoff.

## Why threshold/league/devig tuning can't fix it

The negative CLV is a property of the _information_ in the model, not the
filter. Raising the edge threshold just selects a smaller subset of the same
mispriced bets (and the highest "edges" are the biggest model errors — see the
World Cup screen, where Dixon-Coles rates Australia at 35.7% vs a 17.8% market
because Australia pads goals against minnows). Blending the model toward the
market scales every edge by the same factor `w`, so it selects the same bets
and leaves per-bet CLV unchanged. None of these creates information the market
lacks.

## What this means for a "solid pick finder bot"

A pick finder is only "solid" when it has **backtested positive CLV**. This
one does not — and now we can prove it, on real data, honestly. The pipeline
(ingestion → Dixon-Coles → devig → edge → Kelly → alerts → persistence) is
correct and works end-to-end; the _model_ simply has no edge over the market.

To get real edge you need information the market underweights:

1. **xG-based models** (markets price xG with a lag) — but StatsBomb's
   freeze-frame xG license forbids commercial use of derived analysis;
   Understat/FBref (via penaltyblog, already bound) is the license-clean path
   and the right next experiment — **and it must be backtested to prove CLV
   before trusting it.**
2. **Injuries / confirmed lineups** (a key player out is a first-order edge
   Dixon-Coles is blind to) — sources exist (transfermarkt, Sofascore) but are
   license-unclear / fragile scrapers (see `pickbot-repo-discovery.md`), and
   crucially there is **no historical injury feed aligned with odds history to
   backtest the gain** — so injury-adjusted picks cannot yet be _proven_ to
   beat the market.
3. **Betting genuinely soft books** the backtest can't measure (we bet
   Bet365, a moderately sharp book).

## The honest discipline going forward

Track CLV on every real pick (the schema already has `clv_log`/`beat_close`).
If a future model version shows **persistently positive CLV** in this same
walk-forward harness, it is a real edge — until then, picks are a model-vs-
market screen for manual review, not a profit engine. This system never places
bets; nothing here is a guarantee of profit.
