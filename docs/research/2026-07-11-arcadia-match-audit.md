# Arcadia wrong-game match audit — criterion 2 of the `CLV_USE_PINNACLE_ARCHIVE` flip

**Date:** 2026-07-11 (UTC) · **Status:** READ-ONLY audit, no flag flipped, no rows
written, no commit. Companion to
`docs/research/2026-07-10-clv-close-freshness-study.md` (criterion 2:
"0 wrong-game attachments in a manual audit of ≥ 30 randomly sampled matched
picks").

## Headline

**VERDICT: criterion 2 FAILS — 9 WRONG attachments / 0 ambiguous / 43 audited
(38 stratified-sampled + 5 class-extension rows).**

All 9 wrong rows are ONE root cause: **Australian NBL1 men-plus-women
double-headers**. Pinnacle's arcadia namespace lists the **women's** fixture
under **marker-less club names identical to the men's team names**
("Ringwood Hawks v Kilsyth Cobras", league `Australia - NBL1 Women`), tipping
105–120 minutes before the men's game. The matcher compares
marker-stripped alias bases (identical on both sides), the marker veto cannot
fire (NEITHER compared name carries a women/youth/reserve token — the marker
lives only in the arcadia **league label**, which `resolve_pinnacle_close_snaps`
deliberately passes as `None`/incomparable), and a 120-minute kickoff drift is
far inside the `_ACCEPT_MINUTE_DRIFT` 360-minute accept bound → the men's pick
is attached to the women's Pinnacle close with `exact_canonical`
confidence 1.0. That is fake CLV — the cardinal sin.

In the full 292-row usable subset the class is exactly enumerable:
**9 rows / 7 distinct fixtures** (screen: `arc_league ~ 'NBL1 Women'` while the
pick side carries no women marker; the same 9 rows are the ONLY non-tennis rows
with kickoff delta ≥ 60 min). 9/292 = **3.1% wrong-game rate** in the subset
that would feed trusted CLV under the flag.

## Method (read-only, real consume path)

- Re-ran the study's resolution over all **1,092 settled picks** using the same
  harness as `scripts/research/clv_close_freshness_study.py`: the REAL
  flag-gated path `app.clv_trueup._pinnacle_archive_close` →
  `repositories.resolve_pinnacle_close_snaps` (hardened matcher, marker-safe
  slug fallback, per-market re-key, own-kickoff in-play cutoff), then the same
  fair-derivation chokepoints (`_settleable_groups` → `_merge_vocabulary_groups`
  → `event_fair_probs`, devig=power, live value policy).
- `repositories._record_pinnacle_link_observability` was monkeypatched to a
  **capturing no-op** (accepted-link payload kept in memory, nothing written);
  session rolled back. Verified zero writes: `event_source_links` /
  `match_review_queue` counts **757 / 159 before and after** the run.
- Usable-close subset reproduced the study exactly: **292/1092** (soccer 113,
  basketball 131, tennis 48; h2h 127, spreads 123, totals 42).
- Team-identity verification used the **alias table itself**, not eyeballing:
  for every row, both sides' names were canonicalized with
  `aliases.canonical(strip_markers(name))` (the exact bases the matcher
  compares) plus `distinguishing_markers`, Jaro–Winkler in both orientations,
  kickoff deltas (UTC), league labels on both sides, same-base-pair sibling
  events in the ±2-day fetch window, and market/line correspondence of the
  returned close snaps.

## Stratification of the audited sample (n=43)

| dimension | counts |
|---|---|
| sport | basketball 21 · soccer 13 · tennis 9 |
| market | h2h 23 · spreads 13 · totals 7 |
| risky classes oversampled | ALL fuzzy matches in the 292 (1: `jw_two_tier`, conf 0.965) · ALL confidence<1.0 (1) · ALL marker-carrying rows (1: reserve-team pick 257) · ALL same-base-pair-in-window rows sampled from (15 in population, 14 audited) · top kickoff-delta rows (8) · ALL 9 arc-`NBL1 Women` rows (4 landed in the stratified draw; the other 5 added as class extension) |
| slug-fallback matches | **0 exist in the 292 usable rows** (every accepted match was `exact_canonical` except one `jw_two_tier`) — the slug-fallback risk class is empty in this population |

Notes on population risk composition: 291/292 matches are `exact_canonical`
(alias-canonical base equality) at confidence 1.0; 1/292 is fuzzy
(`jw_two_tier`, 0.965). 31/292 have kickoff delta > 30 min — 22 are tennis
(scheduled-slot vs actual court time on uniquely-paired Wimbledon/ATP draws,
verified correct in every sampled case), 9 are the NBL1 wrong-game class.

## Per-pick verification table

Verdicts: CORRECT = same fixture confirmed (alias-base equality via the alias
table, kickoff aligned or benignly drifted on a uniquely-paired fixture, league
consistent, pick's market/line present in the returned close snaps).
ko Δmin > 30 flagged per the audit spec; every such row is explained in-line
(tennis court-time drift) or ruled WRONG (NBL1).

| pick | sport | market (line) | selection | pick event (home v away) | arcadia event (raw) | ko Δmin | method | conf | alias-base equal | flags | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 128 | basketball | h2h (-) | Zielona Gora | Zielona Gora v Legia (2026-06-19T18:15) | Zielona Gora v Legia Warszawa (2026-06-19T18:15) | 0.0 | exact_canonical | 1.000 | yes | same_pair=3 | CORRECT |
| 133 | basketball | h2h (-) | Caneros | Titanes Del Licey v Caneros (2026-06-20T00:30) | Titanes del Distrito Nacional v Caneros del Este (2026-06-20T00:30) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 | CORRECT |
| 150 | basketball | h2h (-) | Cangrejeros | Gigantes de Carolina v Cangrejeros (2026-06-23T00:00) | Gigantes de Carolina v Cangrejeros de Santurce (2026-06-23T00:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 | CORRECT |
| 153 | basketball | h2h (-) | Gigantes de Carolina | Gigantes de Carolina v Cangrejeros (2026-06-23T00:00) | Gigantes de Carolina v Cangrejeros de Santurce (2026-06-23T00:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 | CORRECT |
| 257 | soccer | h2h (-) | Dalian Yingbo B | Shanghai Second v Dalian Yingbo B (2026-06-23T08:00) | Shanghai Segenda v Dalian Yingbo II (2026-06-23T08:00) | 0.0 | exact_canonical | 1.000 | yes | markers (reserve, BOTH sides) | CORRECT |
| 360 | soccer | h2h (-) | Vikingur Reykjavik | Breidablik v Vikingur Reykjavik (2026-06-25T19:15) | Breidablik v Vikingur Reykjavik (2026-06-25T19:15) | 0.0 | exact_canonical | 1.000 | yes | - | CORRECT |
| 601 | basketball | spreads (-) | Borneo Hornbills +1.5 | Borneo Hornbills v Pelita Jaya (2026-06-26T12:00) | Borneo Hornbills v Pelita Jaya Jakarta (2026-06-26T12:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=3 (IBL playoff legs, days apart) | CORRECT |
| 624 | soccer | totals (-) | Over 2.5 | Uruguay v Spain (2026-06-27T00:00) | Uruguay v Spain (2026-06-27T00:00) | 0.0 | exact_canonical | 1.000 | yes | - | CORRECT |
| 891 | soccer | totals (-) | Under 2.5 | U. De Concepcion v Nublense (2026-07-01T19:00) | Universidad de Concepcion v Nublense (2026-07-01T19:00) | 0.0 | exact_canonical | 1.000 | yes | - | CORRECT |
| 931 | soccer | h2h (-) | JKT Tanzania | JKT Tanzania v Young Africans (2026-06-30T13:00) | JKT Tanzania v Young Africans (2026-06-30T13:00) | 0.0 | exact_canonical | 1.000 | yes | - | CORRECT |
| 974 | soccer | h2h (-) | Claypole | Claypole v Canuelas (2026-06-30T18:00) | Claypole v Canuelas (2026-06-30T18:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 (reverse leg next day, outside accept drift) | CORRECT |
| 1005 | tennis | h2h (-) | Svajda Z. | Svajda Z. v Majchrzak K. (2026-07-02T12:20) | Zachary Svajda v Kamil Majchrzak (2026-07-02T13:00) | 40.0 | exact_canonical | 1.000 | via tennis-canonical | ko_delta=40 (court-time drift, unique pairing Wimbledon R2) | CORRECT |
| 1349 | basketball | h2h (-) | Montenegro | Portugal v Montenegro (2026-07-02T18:00) | Portugal v Montenegro (2026-07-02T18:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 (2nd leg 2 days later, outside drift) | CORRECT |
| 1396 | soccer | h2h (-) | Kahibah | Edgeworth E. v Kahibah (2026-07-03T10:00) | Edgeworth v Kahibah (2026-07-03T10:00) | 0.0 | exact_canonical | 1.000 | yes (alias: edgeworth eagles) | - | CORRECT |
| 1525 | basketball | spreads (-) | Kilsyth +2.5 | Ringwood v Kilsyth (2026-07-02T10:00) | Ringwood Hawks v Kilsyth Cobras (2026-07-02T08:00) | 120.0 | exact_canonical | 1.000 | yes | ko_delta=120; arc league NBL1 **Women** | **WRONG** |
| 1532 | basketball | h2h (-) | Kilsyth | Ringwood v Kilsyth (2026-07-02T10:00) | Ringwood Hawks v Kilsyth Cobras (2026-07-02T08:00) | 120.0 | exact_canonical | 1.000 | yes | ko_delta=120; arc league NBL1 **Women** | **WRONG** |
| 1585 | basketball | h2h (-) | Willetton Tigers | Willetton Tigers v Joondalup Wolves (2026-07-03T12:30) | Willetton Tigers v Joondalup Wolves (2026-07-03T10:30) | 120.0 | exact_canonical | 1.000 | yes | ko_delta=120; arc league NBL1 **Women** | **WRONG** |
| 1697 | soccer | totals (-) | Under 2.5 | Spain v Austria (2026-07-02T19:00) | Spain v Austria (2026-07-02T19:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 (2nd event 2 days later, outside drift) | CORRECT |
| 1741 | basketball | h2h (-) | Portugal | Portugal v Montenegro (2026-07-02T18:00) | Portugal v Montenegro (2026-07-02T18:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 | CORRECT |
| 1742 | basketball | spreads (-) | Montenegro +1.5 | Portugal v Montenegro (2026-07-02T18:00) | Portugal v Montenegro (2026-07-02T18:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 | CORRECT |
| 1960 | basketball | spreads (-) | Portugal -3.5 | Portugal v Montenegro (2026-07-02T18:00) | Portugal v Montenegro (2026-07-02T18:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 | CORRECT |
| 1964 | basketball | totals (-) | Over 170.5 | Switzerland v Serbia (2026-07-02T18:00) | Switzerland v Serbia (2026-07-02T18:00) | 0.0 | exact_canonical | 1.000 | yes | - | CORRECT |
| 2004 | basketball | totals (-) | Under 161.5 | Ukraine v Georgia (2026-07-02T15:30) | Ukraine v Georgia (2026-07-02T15:30) | 0.0 | exact_canonical | 1.000 | yes | - | CORRECT |
| 2035 | basketball | spreads (-) | Kilsyth +4.5 | Ringwood v Kilsyth (2026-07-02T10:00) | Ringwood Hawks v Kilsyth Cobras (2026-07-02T08:00) | 120.0 | exact_canonical | 1.000 | yes | ko_delta=120; arc league NBL1 **Women** | **WRONG** |
| 2222 | tennis | h2h (-) | Dimitrov G. | Dimitrov G. v Berrettini M. (2026-07-04T15:30) | Grigor Dimitrov v Matteo Berrettini (2026-07-04T17:30) | 120.0 | exact_canonical | 1.000 | via tennis-canonical | ko_delta=120 (court-time drift, unique pairing Wimbledon R3) | CORRECT |
| 2307 | basketball | spreads (-) | Hobart Chargers -3.5 | Hobart Chargers v Melbourne Tigers (2026-07-03T10:00) | Hobart Chargers v Melbourne Tigers (2026-07-03T08:00) | 120.0 | exact_canonical | 1.000 | yes | class-extension: arc NBL1 Women; canonical `Hobart Chargers W v Melbourne Tigers W` exists at 08:00 | **WRONG** |
| 2342 | soccer | spreads (-) | Uniao Central +1.5 | Uniao Central v Campos AA (2026-07-03T17:45) | Uniao Central v Campos AA (2026-07-03T17:45) | 0.0 | exact_canonical | 1.000 | yes | - | CORRECT |
| 2400 | basketball | totals (-) | Over 161.5 | Netherlands v Latvia (2026-07-03T17:30) | Netherlands v Latvia (2026-07-03T17:30) | 0.0 | exact_canonical | 1.000 | yes | - | CORRECT |
| 2421 | basketball | h2h (-) | Central Districts Lions | West Adelaide Bearcats v Central Districts Lions (2026-07-04T10:45) | West Adelaide Bearcats v Central Districts Lions (2026-07-04T09:00) | 105.0 | exact_canonical | 1.000 | yes | class-extension: arc NBL1 Women; canonical `... W v ... W` exists at 09:00 | **WRONG** |
| 2454 | soccer | spreads (-) | Queensland Lions -1.5 | Queensland Lions v Olympic FC (2026-07-04T09:30) | Queensland Lions v Olympic FC (2026-07-04T09:30) | 0.0 | exact_canonical | 1.000 | yes | same_pair=3 (reschedule duplicates; matched Δ0) | CORRECT |
| 2488 | soccer | h2h (-) | Magic United | Gold Coast Utd v Magic United (2026-07-05T05:00) | Gold Coast United v Magic United (2026-07-05T05:00) | 0.0 | jw_two_tier | 0.965 | jw=0.9647 (Utd↔United, same club) | fuzzy; conf<1; same_pair=2 (stale-kickoff duplicate at 02:45; matched Δ0) | CORRECT |
| 2573 | basketball | h2h (-) | Hornsby S. | Central Coast Crusaders v Hornsby S. (2026-07-04T09:00) | Central Coast Crusaders v Hornsby Spiders (2026-07-04T07:00) | 120.0 | exact_canonical | 1.000 | yes | class-extension: arc NBL1 Women | **WRONG** |
| 2574 | basketball | h2h (-) | Central Coast Crusaders | Central Coast Crusaders v Hornsby S. (2026-07-04T09:00) | Central Coast Crusaders v Hornsby Spiders (2026-07-04T07:00) | 120.0 | exact_canonical | 1.000 | yes | class-extension: arc NBL1 Women | **WRONG** |
| 2578 | soccer | h2h (-) | Hume City | Hume City v Dandenong City (2026-07-04T04:00) | Hume City v Dandenong City (2026-07-04T04:00) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 (duplicate at 06:15; matched Δ0) | CORRECT |
| 2592 | soccer | spreads (-) | Sutherland Sharks +0.5 | Sutherland Sharks v APIA Leichhardt (2026-07-04T07:30) | Sutherland Sharks v APIA Leichhardt (2026-07-04T07:30) | 0.0 | exact_canonical | 1.000 | yes | same_pair=2 | CORRECT |
| 2594 | basketball | spreads (-) | Hills Hornets +1.5 | Hills Hornets v Maitland M. (2026-07-04T08:15) | Hills Hornets v Maitland Mustangs (2026-07-04T06:15) | 120.0 | exact_canonical | 1.000 | yes | class-extension: arc NBL1 Women; canonical `Hills Hornets W v Maitland Mustangs W` exists at 06:15 | **WRONG** |
| 16129 | tennis | h2h (-) | Belinda Bencic | Belinda Bencic v Coco Gauff (2026-07-05T16:26) | Belinda Bencic v Coco Gauff (2026-07-05T19:45) | 199.0 | exact_canonical | 1.000 | yes | ko_delta=199 (court-time drift, unique pairing WTA Wimbledon R16) | CORRECT |
| 16130 | tennis | h2h (-) | Coco Gauff | Belinda Bencic v Coco Gauff (2026-07-05T16:26) | Belinda Bencic v Coco Gauff (2026-07-05T19:45) | 199.0 | exact_canonical | 1.000 | yes | ko_delta=199 | CORRECT |
| 36269 | tennis | spreads (-) | Alex De Minaur -1.5 | Alex De Minaur v Flavio Cobolli (2026-07-06T12:12) | Alex De Minaur v Flavio Cobolli (2026-07-06T12:14) | 1.9 | exact_canonical | 1.000 | yes | - | CORRECT |
| 36922 | tennis | spreads (-) | Alexander Zverev -1.5 | Jiri Lehecka v Alexander Zverev (2026-07-06T17:06) | Jiri Lehecka v Alexander Zverev (2026-07-06T20:30) | 204.0 | exact_canonical | 1.000 | yes | ko_delta=204 (court-time drift, unique pairing ATP Wimbledon R16) | CORRECT |
| 38666 | tennis | spreads (-) | Madison Keys -1.5 | Madison Keys v Linda Noskova (2026-07-06T15:18) | Madison Keys v Linda Noskova (2026-07-06T15:15) | 3.3 | exact_canonical | 1.000 | yes | - | CORRECT |
| 45774 | tennis | h2h (-) | Arthur Fery | Flavio Cobolli v Arthur Fery (2026-07-08T14:17) | Flavio Cobolli v Arthur Fery (2026-07-08T14:30) | 12.8 | exact_canonical | 1.000 | yes | pick league "Match Coupon" is a bet365 grouping; players unique (Wimbledon QF) | CORRECT |
| 76232 | tennis | totals (totals_3_5) | Over 3.5 | Arthur Fery v Alexander Zverev (2026-07-10T12:39) | Arthur Fery v Alexander Zverev (2026-07-10T12:35) | 4.3 | exact_canonical | 1.000 | yes | line matched via vocabulary merge (`over_under_3_5` ≡ `totals_3_5`) | CORRECT |

## The 9 WRONG rows — full evidence

**Rows:** 1525, 1532, 2035 (Ringwood v Kilsyth, NBL1 South, 2026-07-02) ·
1585 (Willetton Tigers v Joondalup Wolves, NBL1 West, 2026-07-03) ·
2307 (Hobart Chargers v Melbourne Tigers, NBL1 South, 2026-07-03) ·
2421 (West Adelaide Bearcats v Central Districts Lions, NBL1 Central,
2026-07-04) · 2573, 2574 (Central Coast Crusaders v Hornsby Spiders, NBL1
East, 2026-07-04) · 2594 (Hills Hornets v Maitland Mustangs, NBL1 East,
2026-07-04). 7 distinct fixtures, 2 deep-dived, all 7 pattern-verified.

Evidence chain (all SELECT-only):

1. **Canonical side has BOTH fixtures.** e.g. for Ringwood/Kilsyth the events
   table holds `Ringwood W v Kilsyth W` at 08:00 UTC AND the pick's men's
   `Ringwood v Kilsyth` at 10:00 UTC (OddsPortal slugs marker-consistent).
   Same for Hobart (`W` at 08:00, men 10:00), Hills Hornets (`W` at 06:15,
   men 08:15), West Adelaide (`W` at 09:00, men 10:45).
2. **The arcadia namespace holds ONLY ONE event per double-header**, whose
   kickoff equals the WOMEN'S tip and whose league is
   `Australia - NBL1 Women` — but whose team names carry **no W marker**
   (`Ringwood Hawks v Kilsyth Cobras`). The marker veto is structurally blind
   here: markers exist only in the league label, which the resolver passes as
   incomparable.
3. **The men's picks' settled results sit on the men's market**, not the
   matched event's: Ringwood/Kilsyth settled 87–95 (total 182); the matched
   arcadia event's totals lines cluster at **152–160.5** (the women's market)
   with a second cluster at **184.5–185.5**. Willetton/Joondalup settled
   94–89 (183) vs clusters **161–162** and **193.5–194.5**.
4. **The matched arcadia event row is itself contaminated**: the two disjoint
   totals clusters (30+ points apart, captured in overlapping windows) show
   the row carries markets from BOTH games of the double-header — Pinnacle's
   alternates never span 30 points. The h2h stream keeps updating ~2h past
   the event's own kickoff and shows a sharp discontinuity at the women's
   tip (1.6369→1.4717 across 07:46→08:00). The resolver's in-play cutoff
   (the arcadia event's OWN kickoff, 08:00) therefore selects a "close"
   whose game attribution is impossible — at best the women's close, at
   worst an interleaved mixture. Either way it is not the men's close.

Classification note: I scored these WRONG rather than AMBIGUOUS because the
matched event's identity (kickoff + league label) is the women's fixture and
the close cutoff was taken at the women's tip-off; "the row also carries some
men's odds we cannot isolate" does not rescue attribution.

## Criterion-2 verdict and disposition

- **FAIL. 9 wrong-game attachments (3.1% of the 292 usable rows) — the
  criterion requires 0.** `CLV_USE_PINNACLE_ARCHIVE` must NOT be signed on
  this evidence.
- The failure is **narrow and fully enumerable**: every wrong row satisfies
  `arc_league` containing a women marker the pick side lacks; equivalently,
  every non-tennis kickoff delta ≥ 60 min in the usable subset is exactly
  this class. No other wrong attachment surfaced across 34 clean rows,
  including every same-league same-day sibling case, the only fuzzy match,
  the only marker-carrying pick, and all reschedule-duplicate cases.
- Candidate remediation (for the orchestrator; NOT implemented here, changes
  matcher semantics → shadow-first with its own re-audit):
  1. In `resolve_pinnacle_close_snaps`, derive `distinguishing_markers` from
     the arcadia event's **league name** as well as its team names, and veto
     when the pick side lacks a marker the arcadia league carries (a
     tightening-only categorical veto, same family as the existing marker
     veto).
  2. Independently worth filing: the arcadia CAPTURE merges both games of a
     same-club double-header onto one event row (two totals clusters, h2h
     stream past kickoff) — an ingest-side defect that would corrupt even a
     correctly-matched close.
  3. After remediation, re-run this audit (the collection harness is
     deterministic) and require 0 wrong on a fresh ≥30 sample before
     signature.

## Reproduction

- Collector (read-only, capturing monkeypatch + rollback, run inside the app
  container): scratchpad script `arcadia_match_audit_collect.py` piped to
  `docker exec -i -w /srv/betting-ai betting-ai-app-1
  /srv/betting-ai/.venv/bin/python -`; it reuses
  `scripts/research/clv_close_freshness_study.py`'s `_arc_fair_for_pick` and
  enumerates the 292 usable rows with link provenance, alias bases, markers,
  kickoff deltas, sibling events, and snap-level line correspondence.
- Write-safety check: `SELECT count(*) FROM event_source_links` /
  `match_review_queue` before and after — 757/159 unchanged.
- Class screen over the 292: `arc_league ILIKE '%women%'` AND no women marker
  on the pick side → the 9 rows above; non-tennis `kickoff_delta ≥ 60 min`
  → the same 9 rows.
