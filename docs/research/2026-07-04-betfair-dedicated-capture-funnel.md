# Betfair dedicated-capture funnel — structural-limit verdict (2026-07-04)

Question (memory `do-not-remove-main-scrape-betfair`): why is the dedicated
volume-gated Betfair capture thin vs the main scrape's ungated provider-44 rows —
structural feed limit, or code defect? Re-verified after the JSON-feed migration,
the 70-target budget, the D1 close-boost band, and the proxy-robustness work.

**Verdict: STRUCTURAL (feed-volume-bound), re-confirmed with post-migration data.
No code defect found. The old "~22 liquid markets/day" figure is outdated — the
capture now lands 26–61 events/day — but the ceiling remains what OddsPortal
publishes as `volume["44"]`.**

Measurement window: 2026-06-27T12:00Z → 2026-07-04T12:00Z (7d, **pre-blackout**).
Confound stated: the OddsPortal LISTING blackout started ~2026-07-04 12:40 UTC;
all `captured_at >= 2026-07-04 12:00Z` data and all retained container logs
(restart 18:19 UTC) are blackout-tainted and excluded from the funnel numbers.

## Funnel (7d pre-blackout, prod DB)

Provenance split per skill gotcha: dedicated = `liquidity IS NOT NULL`
(OddsPortal displayed per-outcome £), main scrape = `liquidity IS NULL`.

| Stage | soccer 1x2 | soccer OU2.5 | basketball H2H |
|---|---|---|---|
| 1. Feed carries a Betfair price (main-scrape events) | 373 | 376 | 89 |
| 2. Eligible dedicated target (http ref, known kickoff) | 373 (0 lost) | 376 (0 lost) | 89 (0 lost) |
| 3. Read budget per cycle (env `BETFAIR_EXCHANGE_MAX_TARGETS_PER_CYCLE=70`, 300 s interval ⇒ ~288 cycles/day, never-captured-FIRST rotation) | every eligible event read many times/day | same | 39 targets/cycle observed |
| 4. `volume["44"]` present and ≥ £10 at some read ⇒ dedicated rows | **109 (29%)** | **112 (30%)** | **60 (67%)** |

Dedicated volume per day (rows / distinct events), 06-27 → 07-03:
51/17, 239/46, 196/45, 67/26, 140/47, 271/58, 209/61. American Football
home_away: 6 main events, 0 dedicated — expected (`BETFAIR_EXCHANGE_SPORTS=soccer,basketball`).

## Where the volume dies — and where it does NOT

- **Not target selection.** All 214 missed soccer 1x2 events had `http%` refs and
  known future kickoffs (0 eligibility loss). `select_betfair_targets` sorts
  never-captured events FIRST, so the missed events received the MOST reads.
- **Not fetch failures.** Post-restart cycles read full target lists (70/70,
  39/39); 1 proxy-slot failure line, 0 per-match read-failure lines in retained
  logs. The fetch machinery (curl_cffi feed session, proxy pool, 8 s/25 s
  timeouts, quarantine-filtered sweep capped at `PROXY_MAX_FAILOVER_BETFAIR=6`)
  is shared with the main scrape, which succeeded for all 373 events.
- **Not the £10 floor.** Captured-liquidity histogram (14d, n=1876) is smooth
  above the floor — £10–15: 315 (17%), £15–25: 421, £25–50: 324, ≥£50: 816
  (43.5%) — no truncation wall. The 2026-07-01 finding stands: drops are
  `volume=None` (absent), not below-floor.
- **Not a JSON-migration shape break.** `parse_betfair_feed` reads
  `block["volume"]["44"]` per outcome; rows with sane £ values were written every
  day of the window including 2026-07-04 — the current feed's volume shape parses.
- **Volume presence is the killer, and it is market-structure-shaped:** misses
  skew to illiquid leagues (Torneo Federal 19 missed/0 captured, Primera
  Nacional 17/0) while liquid ones capture well (Premier League 14 captured/9
  missed); and dedicated rows sit nearer kickoff (median 10.5 h to KO, p90
  30.9 h) than main Betfair rows (17.6 h / 41.3 h) — OddsPortal displays the
  Betfair depth figure mostly where/when real depth exists.

## Live probe (blackout-confounded, 3 requests total)

One missed Serie B event (main Betfair price captured 11:40Z) probed at ~19:00Z
through the production loader: bootstrap + 1x2 + OU2.5 feeds all fetched and
decrypted OK, but `oddsdata.back` was EMPTY (0 keys) on both markets — the
blackout evidently thins per-match feed CONTENT for our ASN on some requests
too. Inconclusive on volume presence for missed events; not load-bearing for
the verdict (stages above decide it). Re-probe 2–3 missed-event feeds after the
blackout clears if a direct absent-volume exhibit is wanted.

## Deltas vs the 2026-07-01 memory entry

- Dedicated coverage is ~15–20× the old figure (~3 events / "22 markets/day" →
  109–112 soccer events/week, 26–61 events/day) — the 70-target budget, D1
  close-boost, DB-sourced rotation and proxy-timeout fixes were NOT no-ops at
  capture level. The old "raising MAX_TARGETS changed nothing" claim predates
  those fixes.
- The main scrape still dominates ANCHORING (~59/62) because it is denser,
  covers every market (AH ladder, match_winner), and NULL liquidity stays
  anchor-eligible — that invariant is untouched. Do not remove provider 44
  from the main scrape (unchanged).

## Upstream lever (noted, not built)

The only path to more GATED liquidity is Betfair's own read-only API
(`app/ingestion/betfair_api.py`, SHADOW-only): `availableToBack.size` on
listMarketBook covers every Betfair-traded market regardless of what OddsPortal
displays. Promotion (`VALUE_BETFAIR_API_PROMOTE`) is operator-gated; per the
sharp-anchor-auditor skill, any wiring of captured liquidity into
`VALUE_EXCHANGE_MIN_LIQUIDITY` (£50) needs the pct-below-50 probe first
(currently 43.5% of dedicated rows are ≥£50) and a shadow rollout.

No code changed for this task (analysis only); `scripts/safety_audit.sh`
untouched surface.
