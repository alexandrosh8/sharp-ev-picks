# Fresh-2026-H2 BSP Slate — Readiness Investigation (2026-07-03)

Read-only investigation at commit `547a281` (HEAD, main), operator-requested.
No files modified by the investigation itself, no backtest re-run, spent tars
untouched (probed only via `tar -tf` membership listing). Runbook amendments
derived from this report landed separately in
`docs/runbooks/fresh-2026-bsp-validation.md`.

## (a) Runbook readiness verdict

**Mechanically executable, but NOT fit for a 2026-H2 slate as-is.**

Verified against HEAD:

- All cited flags exist in `scripts/value_backtest.py`: `--betfair-bsp-tar`
  (line 1152), `--betfair-bsp-split-date` (1157), `--max-odds` (1205),
  `--fill-universe` (1208), `--markets`. Cited line refs accurate:
  fill-universe doc at 18–24, cluster-robust SEs `clv_pinn_se_cl`/`roi_se_cl`
  at 124–173, `_roi_bootstrap_ci` at 79; the gate verdict uses `clv_pinn_se_cl`
  (1335).
- Config-probe fields in the runbook's hash command all exist in
  `app/config.py`: `fractional_kelly` (292), `value_min_edge` (341),
  `value_volume_min_edge` (354), `value_devig` (367),
  `value_moneyline_max_odds` (511).
- `git log f2db457..HEAD -- scripts/value_backtest.py
  app/ingestion/betfair_bsp.py app/config.py docs/runbooks/…` → **no
  changes**. The runbook matches the code.

**Required before the H2 run** (now flagged in the runbook's 2026-07-03
UPDATE section):

1. **Anchor source (blocking).** The command's fair-value anchor is
   football-data.co.uk Pinnacle columns (`MARKETS` in
   `scripts/value_backtest.py:206-221`: `PSH/PSD/PSA` anchor, `PSCH/PSCD/PSCA`
   close, `P>2.5`/`PC>2.5` for OU2.5). Those columns are **0% populated after
   2026-01-15** (proof below). A new anchor loader (warehouse ARCADIA export —
   recommendation below) is a prerequisite code change whose design must be
   frozen/pre-registered before the H2 slate exists.
2. **Split date.** `--betfair-bsp-split-date 2026-01-01` must change; per the
   ADR-0019 appendix the 2026-Jan..Jun slate is "SPENT for selection AND
   evaluation". Note the ARCADIA anchor only exists from 2026-06-17, so no
   train window with the *same* anchor source exists yet — the H2 train/test
   design is an open decision to pre-register.
3. **Coverage-verification pre-check** — per-month anchor/fill/join counts in
   the header *before* the run. This exact omission burned the Jan–Jun slate.
4. `data/betfair/bsp/incoming/data2026.tar` is a **symlink to the spent
   `data (2).tar`** (sha256 `9123d320…`) — a booby trap for a future session.

## (b) Anomaly confirmation — mechanically verified

**Hypothesis confirmed: the 20% → 0.45% bet-rate collapse is a data-coverage
artifact — football-data.co.uk stopped publishing Pinnacle columns after
2026-01-15 — not a strategy or code bug.**

Per-month usable coverage (all outcomes present and > 1.0) across the 44
cached `2425_*`/`2526_*` season CSVs in `data/ml/cache/` — the exact join
source `value_backtest.py` consumes via `fetch_season_csv`:

| month | rows | PSH (1X2 anchor) | PSCH (close) | P>2.5 (OU anchor) | B365H | MaxH (fill) | BFEH |
|---|---|---|---|---|---|---|---|
| 2025-04 | 885 | 100% | 100% | 100% | 100% | 100% | 99% |
| 2025-09 | 713 | 100% | 100% | 100% | 100% | 100% | 98% |
| 2025-10 | 709 | 94% | 94% | 94% | 100% | 100% | 99% |
| 2025-11 | 793 | **37%** | 39% | 37% | 100% | 100% | 98% |
| 2025-12 | 763 | **48%** | 48% | 48% | 100% | 100% | 100% |
| 2026-01 | 822 | **20%** | 19% | 19% | 100% | 100% | 100% |
| 2026-02 | 880 | **0%** | 0% | 0% | 100% | 100% | 99% |
| 2026-03 | 820 | **0%** | 0% | 0% | 100% | 100% | 99% |
| 2026-04 | 868 | **0%** | 0% | 0% | 100% | 100% | 71% |
| 2026-05 | 526 | **0%** | 0% | 0% | 98% | 100% | 65% |

- **Per-day pinpoint:** last date with any usable `PSH` row = **2026-01-15**
  (3/3 rows). 2026-01-16: 0/17. 2026-01-17: 0/105. Zero thereafter, every
  league.
- Soft columns (B365, Max, Avg, BFE) remain ~100% — precisely the anchor
  vanished, not fills. (`WHH` died earlier, Apr 2025, irrelevant.)
- **Arithmetic:** train-side (2025-Apr..Dec) anchored fraction ≈ 4,220/5,181 =
  81%; test-side (2026-Jan..May) = 162/3,916 = 4.1%. The decisive line is the
  *baseline* (zero-threshold) row in
  `docs/research/2026-07-02-fresh-2026-single-shot.log`: train thr=0 bets
  3,230/7,756 rows (41.6%) vs held-out thr=0 **n=48/2,861 (1.7%)** — a 24×
  collapse *before any strategy threshold applies*. Only a missing join
  column produces that. All held-out bets necessarily came from
  2026-01-01..15, the only anchored window.
- Coverage decay actually *began* 2025-10/11 (94%→37%) — the train side was
  already degraded; 2026-01-15 is where it hit zero.

## (c) Independent sharp-anchor options for 2026-H2 — ranked

**1. RECOMMENDED: own Pinnacle ARCADIA capture in the warehouse**
(`app/ingestion/pinnacle_arcadia.py`, ADR-0013), accrued forward through H2.
Measured (read-only SELECTs on the live DB):

- Coverage: `bookmaker='Pinnacle'` live since **2026-06-17**; 2026-06:
  497,487 snaps / 5,381 events; 2026-07 (3 days): 164,271 / 1,856. Soccer
  namespace: 2,988 distinct events in the first 16 days (~190/day ≈
  5.5k/month); June markets: h2h 5,317 events, totals 4,487, AH 4,489.
- Close quality: `is_closing` never set, but last-capture proximity to
  kickoff over 6,483 completed events: **median 17.5 min pre-KO, p90 75 min,
  64% within 30 min, 87% within 60 min** — a credible quasi-close, plus a
  true "anchor at signal time" from the pre-match series.
- Independence: Pinnacle's own book — fully independent of Betfair BSP.
  In policy (GET-only, free, shipped).
- Gaps to close: (i) cross-source matching to BSP fixtures via
  `app/resolution/matching.py` (capture-bound ceiling ~63%; canonical matcher
  only, no fuzzy); (ii) an `odds_snapshots` → backtest-row exporter does not
  exist yet; (iii) capture uptime through H2 (proxy dependence — Arcadia 403s
  direct egress).
- n-feasibility: ~5.5k soccer events/month × 6 months × even 40% BSP-match ×
  ~10% bet rate ≫ 150 per market — comfortably clears the ADR bar if capture
  stays up.

**2. The Odds API free tier** (`app/ingestion/odds_api.py`, `regions=eu`
includes Pinnacle) — same independence, already implemented; budget-limited
(500 credits/mo) → redundant forward capture, not primary.

**3. OddsPortal archive closes** — the only retroactive Feb-2026+ option, but
high ToS/ban risk, close-only, scrape-fragile; Jan–Jun is spent anyway →
reserve for H2 capture-outage gap-filling only.

**4. football-data.co.uk non-Pinnacle columns** — still ~100% through 2026-05
(B365/Max/Avg/BFE); natural *fill* source and soft-close CLV reference, but
not a sharp anchor; a consensus-anchor variant changes the strategy definition
and would need its own pre-registration. Monitor whether `PS*` returns in the
2026-27 season files.

**5. REJECTED as anchor: Betfair Exchange non-BSP close snapshots.** Not
independent — BSP is computed from the same market's pre-off bets/SP pool;
"CLV vs BSP" against a same-market anchor is circular. (Also thin: 24,077
snaps / 1,013 events, June onward.) Fine as a fill/commission-netted price,
never the independent anchor.

Note: the warehouse cannot backfill — **`odds_snapshots` starts 2026-06**
(1.18M snaps / 7,354 events / 20 books in June; nothing earlier). Feb–May 2026
is unrecoverable from own data.

## (d) Operator-supplied vs self-assembled

**Operator must supply:**

1. The **2026-H2 Betfair BSP tar** (Jul–Dec 2026), uncorrupted/untruncated,
   once the window closes — earliest honest run: after Dec 2026.
2. **Sign-off on an amended pre-registration** (anchor source = own ARCADIA
   capture; new split design) *before* the slate exists — an anchor swap is a
   protocol change, not tuning, but must be frozen now for the single-shot to
   stay valid.
3. Proxy budget/uptime for the Arcadia capture (capture starvation is the new
   coverage risk).

**We can build ahead of the data (none of it done yet):**

1. The sharp-anchor archive — already accruing automatically since 2026-06-17.
2. A warehouse→backtest exporter + BSP-fixture matcher wiring (canonical
   matcher only), tested, walk-forward-safe (last-pre-KO close, signal-time
   anchor).
3. Monthly read-only coverage-monitoring queries (the ones used here) so
   anchor starvation is caught *before* the next slate is burned.
