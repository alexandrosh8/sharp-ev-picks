# Strategy Types Beyond Value Betting — Viability for a Picks-Only System (2026-06-23)

Decision-support only. This system never places bets. Sourced research brief (deep-research-agent).

## TL;DR
For a **picks-only, manual-placement** system (minutes of lag), the binding constraint is **execution speed + account survival**, not edge. Only **player props** and **middling** are genuine extensions of our edge that fit. Promo/matched betting fits mechanically but is a separate, income-capped side activity. Everything else needs speed we don't have, is folklore, or violates our no-autobet rule.

## Ranked (edge realism × fit-with-manual-picks)
| Rank | Strategy | Edge / ROI | Speed? | Fit | Note |
|---|---|---|---|---|---|
| 1 | **Player props** | real; ~5–10%; news-latency edge persists **hours** | No | **Strong** | tiny limits ($250–500), fast-limited; **sharp-book prop devig is weak** (Pinnacle outsources props to Swish) → use multi-book consensus / DFS pick'em as anchor |
| 2 | **Value (devig-CLV)** *(our current)* | ~2.5–5.5% yield; CLV-significant in ~50–65 bets | No | Strong (baseline) | degrades in low-liquidity leagues (our bleed) |
| 3 | Promo / matched betting | guaranteed +EV; ~£200–500/mo decaying | No | mechanical fit, **outside the model** | gubbed fast; income ceiling; keep OUT of pipeline ROI/CLV accounting |
| 4 | **Middling / scalping** | real but rare; break-even ≈ book margin (~5.3%) | No | fits; gated by access | build = line-divergence detector on our **own odds warehouse** |
| 5 | Fade-the-public | **mostly folklore** (Levitt: no systematic bettor edge) | No | marginal — one model input at most | needs paid handle data we lack |
| 6 | Arbitrage / sure-bets | risk-free math but 0.5–3%, **seconds windows** | **Yes** | poor (gubbed in weeks) | one-legged-arb risk |
| 7 | In-play / live | tiny & fleeting (post-goal edge corrects 5–6 min) | **Yes** | **fails** (manual lag too slow) | courtsiding variant = illegal |
| 8 | Market-making | retail = adversely selected | **Yes** | **No** — *is order placement* → violates our no-autobet rule | Betfair Premium Charge taxes winners 20–60% |

## Top picks to pilot
1. **Player props** — same devig machinery, structurally softer market, news-latency edge in our reachable (minutes) window. Caveats: don't devig vs a single sharp book (Pinnacle props are late/low-limit/Swish-priced) — use multi-book consensus or static DFS pick'em lines; tiny limits; judge by **CLV vs consensus close**, not short-run P&L (needs 500–1000 bets to confirm). *Blocked today: we have no live prop-odds feed — needs a prop data source first.*
2. **Middling** — the only other guaranteed-edge fit; build a **line-divergence detector** on our append-only odds warehouse + a two-ticket emitter, break-even hard-coded to combined book margin. Rare on efficient NFL/NBA totals; more common on soccer totals / Asian handicaps. Measure divergence frequency in our warehouse before allocating.

## Avoid / flagged
- **Folklore:** fade-the-public as a standalone edge. **Speed-gated (can't do):** arb back-legs, exchange/ladder trading, in-play, market-making. **Illegal:** courtsiding. **Safety-violating:** market-making (= order placement).
- **Cross-cutting throttle:** soft-book **limiting/gubbing** caps realized yield on ALL viable strategies (incl. ours) → account diversification + stake discipline.

## On our obscure-league bleed (confirms the fix we built)
Devig is only as good as its source line; Pinnacle/Betfair run **low limits / thin liquidity on minor leagues**, so the "sharp close" there is one model's number, not a market — our fair-value anchor was unreliable exactly where we bled. **Fix = the require-sharp-anchor / liquidity gate (built, committed, OFF until August).**

## Key sources
football-data.co.uk Pinnacle-efficiency (87,960 pairs, slope≈1.0); Buchdahl on CLV (Pinnacle Odds Dropper); StatPick (prop softness/limits); OddsJam (props, boosts); Levitt NBER w9422 (no systematic edge); Croxson & Reade EJ 2014 (in-play efficient); BettingUSA / generalbet (middling break-even); Betfair Premium Charge docs. (Vendor yield figures are seller-published; realized < theoretical is the trustworthy pattern.)
