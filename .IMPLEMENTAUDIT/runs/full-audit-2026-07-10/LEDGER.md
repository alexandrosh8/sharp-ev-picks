# Full audit 2026-07-10 — LEDGER

40 raw findings (10-lane workflow wf_fd3ef5df-f7e; verify agents hit session limit →
high/medium verification done INLINE by the orchestrator with quoted-code evidence;
workflow verify re-run w6rhdnzib in flight for independent confirmation of the rest).

## FIXED this session (test-first; every fix has a pinned regression test)

| # | Finding | Fix |
|---|---|---|
| H1 | pinnacle_arcadia change-gate advanced before persist, no rollback; persist failure aborted remaining sports (silent archive loss) | gate_prior snapshot + rollback + per-sport isolation in capture_once (mirrors Betfair 2026-07-09 fix); test: test_capture_once_persist_failure_rolls_back_change_gate |
| H3 | _settle_totals had no Asian quarter-line handling (Over 2.25 graded full LOST where HALF_LOST correct) — latent under default source, real via odds_api | quarter split via _totals_component + shared _combine_quarter (extracted from spreads); 6 tests |
| H4 | tennis scraped "finals" lacked set-pattern plausibility gate — 3 LIVE events carried game scores (4-6/7-6/6-3) and settled 3 h2h picks (pick 1107 internally contradictory: home "won" at 4-6) | _is_implausible_final tennis branch (component > 3 rejected; ties/1-0 partials allowed for retirements); DATA REPAIR: rt rows for picks 1017/1107/1241 deleted, statuses reset to alerted, garbage scores NULLed (backup: repair_backup.txt); test added |
| H5 | _row_structural_sane compared ENTRY price vs LIVE-fair min_acceptable_odds → falsely flagged positive-CLV picks (market moved toward pick) as "structurally impossible", suppressing their rating | entry-vs-entry basis: floor recomputed from model_probability × edge_floor × book; regression case added (moved-toward pick sane) |
| M171 | settled P&L credited GROSS winnings on exchange fills (3 live Matchbook picks) while EV/Kelly net commission at mint | pick_pnl(bookmaker=) nets WON/HALF_WON through effective_odds; threaded at engine + provisional display call sites; 5 tests |
| M360 | integer-line spreads on 2-way-handicap sports graded adjusted tie LOST (European semantics) — wrong for basketball/tennis/NFL (push) | integer_tie_pushes via _TWO_WAY_HANDICAP_SPORTS threaded from settle_selection; soccer unchanged; live check: the 2 settled tennis integer spreads had clear margins — no repair needed |
| M863 | oddschecker persisted the SAME Betfair Sportsbook under two names ('Betfair' 2,241 rows vs 'Betfair Sportsbook' 753k) — double-counts a book in consensus/min-books AND bare 'betfair' maps to 5% EXCHANGE commission in effective_odds (mispriced edge on those rows) | _bookmaker_name resolves the ambiguous bare 'Betfair' through the code's canonical fallback (BF→Sportsbook, OE→Exchange); historical rows left (code attribution lost; mispricing direction was conservative — edges understated); test added |
| M136 | odds_api spreads selections unsigned on positive lines ("Patriots 3.5") → settlement's _SIGNED_LINE_RE rejects → pick permanently unsettleable | spreads points formatted {:+g}; totals unchanged; regression test (note: test added post-implementation — pins behavior) |
| L-dash-2430 | Today "Qualified now" KPI counted AFTER .slice(0,5) — could never exceed 5 | count full qualified set before slicing |
| L-dash-2476 | Top-tracked-edges + Next-kickoffs counts included status='superseded' dedup twins (double-count) | isRankable excludes superseded |

## REFUTED / no-change (inline verification)

| # | Finding | Why |
|---|---|---|
| H2 | oddsportal_json swallows all transport failures | Designed scrape-gap policy: every catch logs type-name, gaps expected per CLAUDE.md, dead-man's-switch covers feed death, downstream freshness fails closed |
| M349 | value.py fabricated-close guard one-sided | Deliberate, documented in code: the magnitude fallback firing with both inputs present dropped legitimate longshots; negative-implied-edge closes are natural (team news) |
| M-odds_api-152 | "no half-line guard" | Resolved by H3: integer + quarter lines are now correctly settleable; a guard would reject valid markets |

## DEFERRED (real, needs its own scoped work — recorded, not dropped)

| # | Finding | Disposition |
|---|---|---|
| M-clv-1338 | finalize_closing_from_snapshots has no per-source freshness gate on injected sharp-archive close rows (stale sharp outranks fresh consensus; Betfair-anchored closes median ~4-5h old) | KNOWN (documented in clv-evidence-reviewer skill). Changes CLV semantics → needs shadow-first design + pre-registered review; folded into the strategy-revision plan follow-ups |
| M-clv-1297 | soft_fresh coverage verdict computed from event-wide last capture (includes dedicated Betfair rows) → can overstate soft freshness | Same work package as M-clv-1338 (close-provenance freshness pass) |
| M-repo-4063 + M-pipe-1107 | stake-0 cap-denial marker permanent ('duplicate_denied' forever) + zero-grant persist-fail hole | Same area as strategy-revision plan Task 1 (stake-0 demotion) — execute together |
| M-arcadia-870 | shared `now` across per-sport fetches skews captured_at by retry time | Low real impact (seconds-scale); fix opportunistically with next arcadia change |
| L-routes-1447 | manual POST /result lacks settled-sibling dedup guard | Manual endpoint, operator-only; add guard with next routes change |
| L-oddschecker-969 | correct_score capture collapses distinct scorelines onto 'Draw' selection | Archive-only market (not minted); fix with next oddschecker change; flagged to keep out of any future devig group |
| L-scheduler-1067 | dedicated liquidity-gated Betfair capture inert since 2026-07-05 (gate requires odds_source!=oddschecker) | CONFIG decision for operator: dedicated capture carried only ~5% of Betfair coverage (main scrape has 95%); surface in report |
| L-scheduler-1013 | ARCADIA_DISCOVER_CONFIG uses direct client not proxy | Opt-in feature currently unused; fix when feature is enabled |
| L-arcadia-300 | integer-totals vocab mismatch (totals_3_0 vs totals_3) | Archive-only namespace; align tokens with next vocabulary audit (see 2026-07-10 AH/spreads vocab audit doc) |
| L-oddsportal-1728, L-oddschecker-1733 | diagnostics/override threading nits | Batch with next touch of those modules |

## ONLINE-LANE INTEL (acquisitions/risks — no code defect)

- **football-data.co.uk Pinnacle columns permanently dead (~2026-01-15) but the SAME free files now carry populated Betfair Exchange closing columns** → candidate replacement close source for the football backtest spine (verify column semantics before use).
- **BettingIsCool Pinnacle Data API (api.bettingiscool.com): full Pinnacle tick-level odds history Jan 2021–now (soccer/basketball/tennis+)** → potentially closes THE data gate for NBA/tennis backtests; needs terms/pricing/provenance verification before any adoption.
- Betfair historic data pricing confirmed (ADVANCED soccer £69/month-of-data); promo BSP daily CSVs STILL published (verified 2026-07-08 file exists).
- aussportsbetting.com still maintained; OddsPapi free tier includes /historical-odds (250 req/mo, no multiplier) with Pinnacle.
- Guest Arcadia feed: multiple 2025-26 reports say it serves DELAYED odds (real-time is funded-login only) + continuity risk post-2025-07-23 API closure → freshness assumptions on the Arcadia anchor should be re-measured (own capture timestamps vs Betfair moves); The Odds API historical = 10× credits.
- Pinnacle actively restricting third-party access → keep OddsPortal/oddschecker fallbacks warm.

## Smoke

- Smoke A: pytest exit 0 (green), mypy clean, format clean, safety PASSED, 1 pre-existing alembic I001 (out of CI scope).
- Smoke B: see SMOKE_B.txt (in flight at ledger write).
