# Pinnacle (arcadia) coverage — offer vs fetch vs link (2026-08-03)

**Question.** Post-alias-batch, ~37% of pick events carry an active
`pinnacle_arcadia` link and the audit classified the unlinked majority as
"capture-bound: coverage-gap / no counterpart". Is that right, and which
scope/config levers would raise coverage?

**Method (read-only).** (1) Client/code review of
`app/ingestion/pinnacle_arcadia.py` + scheduler wiring. (2) Bounded live
probes through the existing production proxy pool inside the app container
(1x `/sports`, per-sport `/matchups`, 1x soccer `/markets/straight`, plus a
16-request 401-diagnosis loop; no proxies/keys/URLs printed). (3) Read-only
SQL against picks / events / event_source_links / match_review_queue.
Probe artifacts: session scratchpad `pinnacle_coverage/` (not committed).

## Findings

1. **Fetch scope is already sport-wide.** The client requests whole sports
   (`/sports/{id}/matchups` + `/markets/straight`, no league filter), for
   `soccer,tennis,basketball,american_football` — exactly the sports picks
   span (last 14d: soccer, tennis, american_football, basketball). There is
   **no league we "don't fetch"**; a per-league fetch lever does not exist in
   this API shape. Adding baseball/hockey/rugby/handball ids would buy
   nothing (no picks in those sports) and cost request budget.

2. **The "capture-bound" majority was a capture *outage*, not an offering
   gap.** `pinnacle_soccer` event creation shows a total blackout ~Jul 18 →
   Jul 26 (zero events created), squarely covering the Jul 21–25 UEFA
   qualifier legs that dominate the unlinked list. Live probe confirms
   arcadia **offers** the top "missing" leagues today (30 Conference-League-
   Qualifier events inside 72h; MLS, Leagues Cup, CFL, ATP/WTA/Challenger all
   present).

3. **Post-recovery link coverage is ~70%, not ~37%.** Pick events with
   kickoff ≥ 2026-07-27: 118/169 linked (70%). The 37% projection is
   dominated by the outage window. Residual per-league (kickoff ≥ 07-27):

   | league (pick side) | events | linked | class |
   |---|---|---|---|
   | UEFA Conf. Lg Qualifying | 48 | 22 | name-form linking (counterparts captured: 79 in `pinnacle_soccer`) |
   | tennis Match Coupon | 41 | 37 | mostly linked already |
   | Champions Lg Qualifying | 22 | 17 | name-form linking |
   | US Major League | 12 | 9 | linked majority |
   | Europa Lg Qualifying | 11 | 8 | name-form linking |
   | CFL | 4 | 1 | name-form linking (`Canadian Football` ns captured) |
   | rest (7 leagues) | 31 | 24 | small residuals |

   Name-form evidence at identical kickoffs: `FK Riga↔Riga FC`,
   `Vardar Skopje↔Vardar`, `CSKA 1948 Sofia↔CSKA 1948`,
   `Sport Club do Recife↔Sport Recife`, `Cuiaba EC↔Cuiaba`,
   `Operario↔Operario Ferroviario` (full list in scratchpad
   `alias_pair_candidates.txt`; review queue adds 27 `jw_below_accept`,
   36 `kickoff_drift`, 10 `token_sort_below_accept` in 14d).

4. **Bursty upstream 401s were an unhandled cycle-killer.** Probes measured
   back-to-back 401s on the production pool minutes apart from 7×200 runs of
   the *identical* request — an edge/egress rejection wearing a 401 (the
   endpoints require no key; no key is configured, so it is not a credential
   failure). The client rotated proxies only on 403; a 401 failed the
   sport's entire capture cycle instantly with zero retry. This failure mode
   is the plausible mechanism of the Jul 18–26 blackout.
   `arcadia_discover_config` is **not** a fix: discovery of the public guest
   key via `www.pinnacle.com/config/app.json` fails through the proxy pool
   (falls back harmlessly), and the 401s are not key-deterministic.

5. **Markets: no widening warranted.** Soccer straight-market census
   (period-0, open): moneyline 685, main total 711, main spread 712 — all
   captured. Not captured: `team_total` (1 407) and period≥1 (halves), but
   picks carry only `h2h/totals/spreads/dnb/btts`; period markets are
   filtered out of the candidate pipeline, DNB is anchorable via the already-
   captured AH 0.0, and BTTS does not exist in arcadia straight markets.
   Alternates stay excluded by design (wider margin pollutes devig). The
   a26e42b vocabulary folding is unaffected.

## Implemented

- `app/ingestion/pinnacle_arcadia.py`: `_PROXY_ROTATE_STATUSES` now
  `{401, 403}` — a 401 rotates to the next proxy and retries (3-attempt
  ceiling unchanged; exhaustion still surfaces `PinnacleArcadiaError(401)`,
  status-only, never URL/key). TDD:
  `tests/test_pinnacle_arcadia.py::test_client_retries_401_blocked_proxy_then_succeeds`,
  `::test_client_exhausts_401_then_raises_arcadia_error`; 401 removed from
  the permanent-4xx no-retry parametrization.

## Recommended decisions

1. **Ship the 401-rotation fix** (done in-tree; uncommitted per task).
2. **Follow-up (data-level, reviewed):** an alias batch for UEFA-qualifier
   clubs + the Brazilian short forms from `alias_pair_candidates.txt`, via
   the mandatory dry-run-diff review (normalization-collision trap;
   `canonical-matcher-verifier`). Projected gain ≈ +15–25 pick events/14d
   linked (~+10–15 pp on the 70% baseline). Do NOT drop matcher thresholds.
3. **Follow-up (observability):** the Jul 18–26 blackout went unalerted —
   consider a dead-man alert on `pinnacle_*` event-creation rate.
4. No sport/league/market scope changes.

## External free Pinnacle surfaces (research note only — no integration)

- **PS3838 / Pinnacle888 mirror API** ([betsapi reference](https://pinnacleapi.github.io/betsapi),
  [ps3838api on PyPI](https://pypi.org/project/ps3838api/),
  [Arbusers thread](https://arbusers.com/pinnacle-api-t6554/)): same odds,
  but requires a funded bookmaker account + credentials → rejected
  (no-credentials rule; ADR-0002 posture).
- **PulseScore PS3838 feed** ([pulsescore.net/ps3838-api](https://pulsescore.net/ps3838-api)):
  third-party aggregator with a small free tier; volume we need is paid →
  rejected under the free-data-only mandate (same class as BettingIsCool).
- **SportsGameOdds Pinnacle feed** ([sportsgameodds.com](https://sportsgameodds.com/bookmakers/pinnacle-odds-api)):
  commercial aggregator → rejected on cost.
- Conclusion: the arcadia guest feed remains the only compliant free
  surface; reliability (401 rotation, blackout alerting) is the lever, not a
  new source.

## Open questions

- Root cause of the Jul 18–26 blackout (401 storm vs proxy-pool-wide block)
  is inferred, not proven — logs at DEBUG were suppressed by the warn-once
  gate.
- Scottish League Cup (men) never appeared in the captured namespace during
  its group stage; verify arcadia's league name for it when round 2 enters
  the 72h horizon before classifying it "uncovered".
