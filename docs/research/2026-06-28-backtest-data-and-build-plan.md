# Backtest Data + Build/Investigation Plan — 2026-06-28

Status snapshot: app deployed & healthy. Live now: fast curl_cffi JSON Betfair capture
(Playwright retired; soccer 1x2/ou2.5 + basketball ML), the BTB + Betfair-BSP backtest
loaders (default-off, awaiting operator data), CLV/edge follow-ups, and the **3 feature
gates ON *for testing*** (steam enforce, per-market devig 1x2→multiplicative / ou2.5→power,
logit-pool anchor). Committed defaults for those gates stay OFF (walk-forward was
inconclusive — see `gates-validated-keep-off-2026-06-28`). Premium scoped by sharp anchor;
major-league filter off.

---

## Part A — Backtest data: what's worth getting

The value method needs, per match: a **pre-match price to bet** + a **closing price to
measure CLV** (ideally a **sharp** close), + the **result**. Ranked by value × how
reachable it is.

| # | Source | Gives | Value to us | How to get | Effort | Status |
|---|--------|-------|-------------|-----------|--------|--------|
| 1 | **football-data.co.uk** | EU football: pre-match Max soft + **Pinnacle close** + result, 18 leagues × many seasons | The validated sharp-CLV backtest (+22% holdout). Calibration base. | cached in repo | — | ✅ **HAVE IT** (Phase-5 ran on it) |
| 2 | **Betfair BSP** (historicdata.betfair.com) | **True sharp CLOSE** + result for soccer/basketball/**tennis** | Extends the sharp-CLV proof to NBA/tennis; validates the steam gate; gold-standard close | free **Betfair account** (Basic tier) → download stream files → drop in `data/betfair/bsp/` | operator download | 🔧 **loader BUILT** (Phase-2), needs data |
| 3 | **Beat-the-Bookie** | ~114k **worldwide** football matches, opening+closing+result (soft/consensus) | Calibration DEPTH (crosses isotonic n≥1000) + worldwide-league sanity backtest | Dropbox links → drop in `data/beatthebookie/` | operator download | 🔧 **loader BUILT**, needs data |
| 4 | **tennis-data.co.uk** | Tennis **pre-match Pinnacle** (PSW/PSL) + Max + result. **No close.** | Tennis pre-match value scan; pair with #2 for the tennis close | free CSV (sister site of #1) | small loader (~1 file, like football_data.py) | ⬜ not built |
| 5 | **OddsPapi / ParlayAPI(hoopR)** | Free-tier multi-sport / multi-year **NBA** lines (Pinnacle on OddsPapi; consensus on hoopR) | NBA backtest breadth | free API key | API client build | ⬜ not built |
| 6 | **sportsbookreviewsonline** | Free **NBA** opening+closing (consensus, no Pinnacle) | NBA backtest (consensus close) | free Excel | small Excel loader | ⬜ not built |

**Does NOT help the backtest:** the *free delayed Betfair API* — it's live (delayed) odds
going *forward*, not historical; it can't backfill the past. It only upgrades **live
anchoring** (a separate, deferred item).

**Worth-knowing distinctions:**
- *Closing-only* sources (football-data non-EU feed, BTB's 880k `closing_odds.csv`, most
  Kaggle NBA sets) are **useless for the value method** — no pre-match price to bet.
- A *sharp* close (#1 Pinnacle, #2 Betfair BSP) is what proves the strategy. Consensus
  closes (#3, #6) only give breadth + calibration, not the sharp-CLV proof.

---

## Part B — What else to do (prioritized)

### Now (the running loop)
- **B0. Monitor the 3 gates' forward CLV.** They're ON for testing; the validation was
  inconclusive, not positive. Each monitor cycle: watch that steam isn't demoting +CLV
  picks, and that per-market devig / logit don't drift CLV negative. Re-run the walk-forward
  once in-season settled volume accrues. *Revert any gate via its `.env` line if it hurts.*

### Data acquisition → measurement (biggest single unlock)
- **B1. Place BTB + BSP data** (operator drops the files) → I run the extended backtests +
  feed the ML calibration set → report. This is what turns the two built loaders into real
  numbers (sharp-CLV on NBA/tennis via BSP; calibration depth via BTB).
- **B2. (optional) tennis-data.co.uk loader** (~1 file) → tennis pre-match backtest; pair
  with BSP for the tennis close.

### Market / sport expansion (build + investigate)
- **B3. Investigate football Asian Handicap as a traded market.** Betfair *anchors* it
  (recon-confirmed), unlike BTTS/DC/DNB which it doesn't price. Backtest AH on football-data
  first; if it clears the bar, wire it into the value markets + the dedicated Betfair capture.
- **B4. Tennis picks.** Tennis match-winner has a ready Betfair anchor. Enable picks once
  tennis data + calibration exist (see `calibrate-nfl-tennis-future`). Currently visibility-only.

### Live anchoring (optional upgrade)
- **B5. Free live Betfair read-only API.** Fresher than the OddsPortal scrape (1–180s delay,
  fine for pre-match; rate limits fine). Cost: a new event-matching burden (like Pinnacle's).
  Deferred — do only if live Betfair coverage proves a bottleneck.

### Standing infra (the real premium-volume constraint)
- **B6. Scrape cycle throughput.** The worldwide all-leagues slate doesn't fully fit the
  600s freshness window → thin premium off-season. Lever: rotating-proxy/IP scaling +
  concurrency, not hammering one IP. The biggest driver of *more picks*.

### Calibration (when data allows)
- **B7. NFL + tennis calibration** (per memory) — needs their data + market coverage first.

---

## Open questions the data/tests will answer (decision gates)
1. **Do the 3 gates prove out in-season?** Flip-to-default only on walk-forward v2>v1 on
   CLV with CI excluding 0 (the bar Phase-5 used). Until then they stay test-only / off-default.
2. **Does Betfair BSP confirm sharp-CLV on NBA/tennis** the way Pinnacle does on EU football?
   (Needs B1.)
3. **Is football AH +EV** enough to justify a new traded market? (Needs B3 backtest.)
4. **Does worldwide-league breadth (BTB) improve the ML value-filter calibration** (isotonic
   n≥1000) without leaking? (Needs B1.)

---

*Any item above can be turned into a detailed TDD implementation plan when we pick it up.
The two built loaders (B1) are the highest-leverage next step and only need the data dropped in.*
