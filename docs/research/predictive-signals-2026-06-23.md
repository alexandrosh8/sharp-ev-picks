# Predictive Signals for Residual Edge Over the Sharp Close (2026-06-23)

Decision-support only. This system never places bets. Sourced research brief (quant-sports-researcher).

## The single most important finding
**Almost every "signal" people market as edge is PUBLIC before the close, and the close is semi-strong efficient** (Croxson & Reade 2014). Confirmed lineups, referee assignments, schedules, weather forecasts, obvious motivation — **already priced.** The literature is brutal: the rest-vs-market study found the NFL market **over-corrected** a once-real edge (Lopez & Bliss 2024); NBA player-absence bias is **fully removed by the close** (Dare et al. 2015); reverse line movement is **not profitable** to a follower (Francisco & Moore 2019).

Residual edge comes from only THREE places:
1. **Forecasting** a not-yet-public fact better than the market (e.g. P(plays | Questionable)).
2. **Latency** — beating a *soft book before its own close* (execution edge; our CLV vs Pinnacle is already gone).
3. **Improving the model's probability** as an upstream input (legit but competes with, not beats, the line).

## Ranking (edge-evidence × data-availability × integration-ease)
| Rank | Signal | Residual edge vs CLOSE | Free 2026 data | Effort | Where edge lives |
|---|---|---|---|---|---|
| **1** | **Rest / travel / schedule density** (NBA **eastward jet-lag**) | marginal (jet-lag); else priced | nba_api, football-data.org + haversine/TZ | **LOW** | model input; **eastward-home jet-lag penalty** (Song 2017; Roy & Forest 2018) is the one **untested-vs-close** seam → worth a dedicated backtest |
| 2 | **Referee** (cards only) | marginal in **soft cards markets**; priced elsewhere | football-data.co.uk (ref+cards cols), FBref | LOW–MED | only soft **cards/bookings** lines; needs a benchmarkable sharp cards close or it's fake-CLV |
| 3 | **NBA game-time-decision forecasting** | marginal, unproven | NBA injury report (needs JRE) | MED | **P(plays\|Questionable)** + load-mgmt is the only true pre-close signal; star-out point values are folklore |
| 4 | Motivation / rotation | marginal → priced | football-data.org standings | MED | **NBA totals early-season** (incomplete efficiency, Baryla/Borghesi 2007); else a dead-rubber risk filter |
| 5 | Weather | already-priced (soccer) | **Open-Meteo** (free, **non-commercial** terms) | LOW–MED | model input only; "underpriced weather" papers are NFL/NCAA, data ends 2004 |
| 6 | Public % / line movement / "sharp money" | **already-priced / tautological** | (none free; Covers = consensus) | — | reframe: **CLV-capture, not edge** — "line movement predicts the close" ≈ "the close is efficient" |

## Implications for this project
- **Stop thinking "signal → beat the close."** Reframe each candidate as a **model input** (gated by our walk-forward + CLV bar) or a **forecasting/latency play** (separate, harder). Our v2-beats-v1-on-CLV gate is the right bar.
- **Best ROI on effort: rest/travel features (Rank 1).** Pure schedule/geo math, LOW integration, no leakage, no paid feed, no scraper: `rest_days, is_b2b, is_3in4, tz_delta_signed, travel_km, congestion_7d, prior_euro_tie_72h`. The B2B flag itself is priced — the **directional jet-lag** component is the single best-supported *untested-vs-close* hypothesis. Worth a dedicated backtest *(blocked today: needs NBA pre-match odds, which the free SBR data lacks — see strategy-optimization-decision doc)*.
- **Referee → cards only**, gated on a benchmarkable cards close existing (else fake-CLV). **NBA Questionable forecaster** = highest-ceiling true-residual idea but MED effort + unproven (research spike). **Weather/motivation** = inputs only, not triggers. **Do NOT build** a sharp-money/fade-public module (peer-reviewed dead).

## ⚠️ Regime change (high-value): Pinnacle closed its public API (2025-07-23)
The clean free sharp-reference API is gone. Our live sharp reference is now **OddsPortal cross-book consensus + (read-only) Betfair Exchange** — *(note: our scraped Pinnacle "arcadia" web endpoint still serves data — verified capturing 252 sharp rows/cycle — but the documented public API is closed; do not rely on it returning).* Handle-% is no longer freely available. **Worth an ADR note** standardizing devig/CLV on OddsPortal consensus + Betfair.

## Source quality
Peer-reviewed anchors: Croxson & Reade (2014), Levitt (2004), Shank (2019), Francisco & Moore (2019), Dare/Dennis/Paul (2015), Baryla/Borghesi (2007), Lopez & Bliss (2024), Buraimo (2010), Bryson (2021), Song (2017), Roy & Forest (2018), Wu (2024), Link (2016). Flagged as **folklore**: per-star spread magnitudes, "books ignore referees", "underpriced weather" (soccer), CLV→ROI multipliers.
