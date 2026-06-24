# Strategy Optimization — Decision Doc (2026-06-23)

Decision-support only. This system never places bets. Edges/ROI are informational.

Consolidates: the value backtest, the per-league CLV breakdown, three research
briefs (odds-data sources, strategy optimizations, OSS tools), the operator's
uploaded reference, and the xG 3rd-strategy design spec.

## TL;DR
1. **Premium works.** The value strategy is +EV out-of-sample in sharp-covered
   leagues: held-out (2024/25–25/26, 13,144 matches) at the 3% edge gate →
   **ROI +21.1%, CLV +0.112 (>2SE)**, beating even the best-of-books close.
2. **The live losses are league selection, not the strategy.** Every
   football-data league has **positive CLV**; the leagues that bled us (GFA,
   Latvian Cup) are **outside the sharp-covered universe entirely** — no Pinnacle
   anchor, so the edge there was always illusory.
3. **The #1 fix is the league allowlist** (`VALUE_MAJOR_LEAGUES`, already built,
   currently OFF). Tier it by CLV. On hold pending operator go.
4. **3rd strategy (xG): conditional go** as a silent shadow tier, scoped to
   top-5 + Russia, **OU2.5/BTTS thesis** (not 1X2). It will NOT fix minor-league
   losses (Understat doesn't cover them).

## 1. Per-league CLV — the data-driven allowlist
Value strategy, thr 0.02, 1x2+OU2.5, min_odds 1.6, all seasons. CLV (vs Pinnacle
close) is the stable signal; ROI at these n is noisy. Ranked by CLV:

| Tier | Leagues (CLV vs Pinnacle close) |
|---|---|
| **A — elite** (CLV ≥ 0.075, >3SE, beat 84–94%) | Bundesliga +0.092 · Premier League +0.083 · Serie A +0.082 · Eredivisie +0.075 |
| **B — strong** (CLV 0.05–0.07) | Scotland L2 +0.057 · Championship +0.056 · Greece +0.053 · Scotland Prem +0.052 · Belgium +0.052 |
| **C — positive** (CLV 0.04–0.05) | La Liga +0.049 · Portugal +0.049 · England L1 +0.043 · Ligue 2 +0.043 · Ligue 1 +0.043 · Segunda +0.042 · England L2 +0.042 · National League +0.042 · Serie B +0.042 |
| **D — marginal** (CLV < 0.04, wide CI) | Turkey +0.038 · Scotland L1 +0.036 · 2.Bundesliga +0.036 · Scotland Champ +0.020 |

**Key reading:** the whole football-data universe is CLV-positive — even lower
divisions. The bleed comes from leagues **below** this tier (no sharp data).
So the allowlist = "leagues with sharp/Pinnacle coverage," **excluding the truly
obscure**, NOT "top divisions only." Weight Tier A highest if we tier confidence.
ROI≠CLV mismatches (Scotland Prem CLV +0.052 / ROI −27%; Ligue 1 +0.043 / −15%)
confirm: ROI over 27–85 bets is variance; CLV is the truth.

## 2. New data sources to add (all free, all read-only / safety-compatible)
- **Betfair Historical (Basic = FREE) + Betfair Delayed App Key (FREE, read-only)**
  → sharp closing + live anchor for minor leagues, replacing the 403-prone
  Pinnacle arcadia. **Read-only only — never the £499 Live/order key.**
- **Understat (xG) + FBRef** → feeds the 3rd strategy (§4) and future model work.
- **sportsbookreviewsonline.com** → free NBA odds history (NBA backtest gap).
- Keep: football-data.co.uk (major-league Pinnacle close), OddsPortal/OddsHarvester
  (minor-league Pinnacle, the free spine).
- Avoid wiring in: consumer +EV finder sites (soft-book based) and unvetted
  Claude skills (their own source cites 36% prompt-injection rate).

## 3. Strategy optimizations (full brief: ev-strategy-optimizations-2026-06-23.md)
Ranked impact ÷ effort:
- **P1 Liquidity/league gate** (very high, low) — = the allowlist in §1.
- **P2 Min-edge floor (~2–3%) + odds-band ceiling** (kill >5.0 longshots).
- **P3 Devig per market** — additive ≡ Shin (drop duplicate); 1x2 → Shin/power,
  totals/AH → power; multiplicative weakest. *(Backtest agrees.)*
- **P4 Calibration** (log-loss, forward-fit) · **P5 anchor routing** · **P6 Kelly guardrails.**
- Myth corrections: half-Kelly = ¼ variance (not ½); FLB already priced into a
  sharp Pinnacle close; "+EV lives in obscure leagues" is folklore.

## 4. 3rd strategy — xG shadow (CONDITIONAL GO)
Full spec from the modeling agent. Summary:
- **Edge thesis:** NOT "beat the close on 1X2" (Pinnacle too sharp; premium
  already +0.11 there). Real thesis = price **OU2.5/BTTS** independently, where
  premium has no sharp anchor and xG's goal-expectation signal is most direct.
- **Model:** Dixon-Coles on xG (reuse penaltyblog) → fallback Skellam-on-xG
  (Wilkens 2026: +10% ROI *calibrated*, ~1% raw — calibration is mandatory).
- **Data:** `understat` (amosbastian, async, maintained); top-5 + Russia, since
  2014/15. Join to football-data odds on (date, normalized teams) — needs an
  alias table (silent-failure risk).
- **Integration:** `run_xg_pipeline` in `app/pipeline.py`, tier hard-forced
  `volume` (never alerts, never reserves), distinct `model_version`, parallel
  dispatch after `run_value_pipeline`. Premium untouched.
- **Promotion gates (both required):** out-of-sample CLV>0 with bootstrap CI
  excluding 0, AND beats premium on the same matches (or prices something
  premium can't). Expect PASS on OU/BTTS, FAIL on 1X2.
- **Honest limit:** Understat ≠ minor leagues, so xG cannot fix the live bleed —
  the allowlist (§1) does that.

## Decision & next steps (production frozen until operator go)
1. **Premium:** build `VALUE_MAJOR_LEAGUES` from §1 (Tier A–C) mapped to scraped
   OddsPortal names; present for approval; flip when ready. *(highest-confidence lever)*
2. **Optimize:** P2 + P3 (small, backtest-backed code changes).
3. **3rd strategy:** build the xG shadow per §4 (ingester → model → calibration →
   backtest → shadow pipeline). Promote only on the two gates.
4. **Data:** add Betfair read-only (sharp anchor) + Understat (xG) ingestion.
