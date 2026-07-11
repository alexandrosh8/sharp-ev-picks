# Arcadia wrong-game match RE-audit — after the league-marker-veto remediation

**Date:** 2026-07-11 (UTC) · **Status:** READ-ONLY independent verification, no
flag flipped, no rows written, no commit. Companion/successor to
`docs/research/2026-07-11-arcadia-match-audit.md` (which found 9 wrong
attachments, all NBL1 women double-headers) and to the flip criterion 2 of
`docs/research/2026-07-10-clv-close-freshness-study.md` ("0 wrong-game
attachments in a manual audit of ≥ 30 randomly sampled matched picks").
`CLV_USE_PINNACLE_ARCHIVE` remains **OFF** regardless of this verdict — the
flip is the operator's decision.

## Headline

**VERDICT: criterion 2 PASSES on this re-audit — 0 WRONG / 0 ambiguous / 34
audited** (fresh stratified sample from the post-remediation usable subset),
**and all 9 previously-wrong picks now REFUSE** (league-marker veto, no
arcadia close returned).

The population-level class screens that exactly enumerated the prior failure
are now **empty** over the full 284-row usable subset:

- arcadia league label carrying a women/youth/reserve marker the pick side
  lacks, yet attached: **0 rows** (was 9);
- non-tennis kickoff delta ≥ 60 min: **0 rows** (was 9 — the same 9).

The usable-population diff vs the prior audit is surgical: **exactly the 9
wrong picks removed** (1525, 1532, 1585, 2035, 2307, 2421, 2573, 2574, 2594),
**one new settled pick added** (83082, Wimbledon women's final, verified
CORRECT in the sample). 292 − 9 + 1 = 284.

## What was re-audited (deployed remediation)

Container `betting-ai-app-1` (rebuilt, up at audit time) carries:

1. `resolve_pinnacle_close_snaps` — **league-derived marker veto**
   (tightening-only): if the matched arcadia event's league label carries a
   {women, youth, reserve} marker (via `_league_marker_set`, reusing
   matching.py's marker vocabulary) that the pick side's team names lack, the
   match is refused (`return []`) BEFORE any close snaps or observability
   link are produced. Tennis exempt (person-named fixtures; "WTA/ITF Women"
   labels would veto correct women's closes).
2. Mint-time Tier-1/Tier-2 event dedup — `_league_marker_set` splits
   marker-disagreeing league labels so a double-header's other game is never
   merged onto the pick's event.

Verified in the deployed file (`/srv/betting-ai/app/storage/repositories.py`:
`_league_marker_set` at 324, resolve-path veto at ~3414-3440, dedup splits at
~408/525). The veto sits before the accepted-link observability write, so
refused matches record nothing.

## Method (read-only, real consume path — same harness as the prior audit)

- Re-ran resolution over all settled picks via the DEPLOYED container code:
  `docker exec -i -w /srv/betting-ai betting-ai-app-1 .venv/bin/python -`
  running a collector that calls the REAL flag-gated path
  `app.clv_trueup._pinnacle_archive_close` →
  `repositories.resolve_pinnacle_close_snaps`, then the study's own
  `_arc_fair_for_pick` (imported from
  `scripts/research/clv_close_freshness_study.py`) for usability — never a
  reimplementation.
- `repositories._record_pinnacle_link_observability` monkeypatched to a
  **capturing no-op** (accepted-link payloads held in memory for provenance:
  raw arcadia names, kickoff, method, confidence; nothing written); session
  rolled back. A logging tap on `app.storage.repositories` captured every
  "league-marker veto refused" INFO line.
- **Write-safety check:** `event_source_links` / `match_review_queue` counts
  **782 / 159 before and after** the run (unchanged).
- Enumeration: **1,098 settled picks** evaluated (0 superseded in the joined
  set, 0 missing kickoff). Funnel: 839 archive-matched → **284 usable**
  (arcadia close anchorable for the pick's own market/line/selection).
  Refusals: 243 `no_archive_match`, 555 `no_anchorable_fair`,
  **16 `veto_league_marker`** — matching the veto author's own 16-refusal
  count, independently reproduced.
- Team-identity verification used the alias table itself
  (`aliases.canonical(normalize_name(strip_markers(name)))` on both sides —
  the exact bases the matcher compares), `distinguishing_markers` on both
  sides, kickoff deltas (UTC), league labels on both sides, same-base-pair
  sibling arcadia events in the ±2-day fetch window, and market/line
  correspondence of the returned close snaps. Tennis pairing uniqueness was
  verified with a dedicated tennis-canonical check (below).

## (1) The 9 previously-wrong picks — refusal confirmed

Every one of the 9 resolves to **snaps = 0** with the league-marker veto log
line fired during its own resolve call:

| pick | fixture (pick side) | market/selection | vetoed | veto target (arcadia ref) |
|---|---|---|---|---|
| 1525 | Ringwood v Kilsyth 07-02 10:00 | spreads / Kilsyth +2.5 | YES | 1632193196 (`['women']`) |
| 1532 | Ringwood v Kilsyth 07-02 10:00 | h2h / Kilsyth | YES | 1632193196 (`['women']`) |
| 1585 | Willetton Tigers v Joondalup Wolves 07-03 12:30 | h2h / Willetton Tigers | YES | 1632207704 (`['women']`) |
| 2035 | Ringwood v Kilsyth 07-02 10:00 | spreads / Kilsyth +4.5 | YES | 1632193196 (`['women']`) |
| 2307 | Hobart Chargers v Melbourne Tigers 07-03 10:00 | spreads / Hobart Chargers -3.5 | YES | 1632207775 (`['women']`) |
| 2421 | West Adelaide Bearcats v Central Districts Lions 07-04 10:45 | h2h / Central Districts Lions | YES | 1632228808 (`['women']`) |
| 2573 | Central Coast Crusaders v Hornsby S. 07-04 09:00 | h2h / Hornsby S. | YES | 1632218328 (`['women']`) |
| 2574 | Central Coast Crusaders v Hornsby S. 07-04 09:00 | h2h / Central Coast Crusaders | YES | 1632218328 (`['women']`) |
| 2594 | Hills Hornets v Maitland M. 07-04 08:15 | spreads / Hills Hornets +1.5 | YES | 1632218316 (`['women']`) |

**9/9 refused.** The remaining 7 veto firings are other NBL1 picks that
previously died later in the funnel (`no_anchorable_fair`) and now refuse
earlier (Hobart/Sandringham, Lakeside/Willetton, Norwood/West Adelaide,
Frankston/Knox, Bendigo/Sandringham, Frankston/Ringwood, Eltham/Sandringham)
— all `['women']`-league arcadia targets, i.e. the same class, none a
false-positive veto.

## (2) Fresh stratified sample (n = 34) from the 284-row usable subset

Population risk composition first: 283/284 `exact_canonical` at confidence
1.0; 1/284 fuzzy (`jw_two_tier`, 0.9647 — pick 2488, the only conf < 1.0 and
only non-exact method). Slug-fallback matches: **0**. 23 rows with kickoff
delta > 30 min — **all tennis** (Wimbledon scheduled-slot vs court-time
drift). 14 rows with ≥ 2 same-base-pair arcadia events in the ±2-day window.
1 marker-carrying pick (257, reserve on BOTH sides). Every usable row's pick
market is present in its returned snap markets (global check, 0 exceptions).

| stratum | rule | n |
|---|---|---|
| same-base-pair-in-window ≥ 2 | ALL | 14 |
| fuzzy / conf < 1.0 / non-exact method | ALL (pick 2488) | 1 |
| marker-carrying pick | ALL (pick 257) | 1 |
| largest kickoff deltas | top 8 | 8 |
| women/youth-labeled arcadia league that attached | ALL | **0 (class empty)** |
| random stratified fill (sport×market, seeded, preferring picks NOT in the prior audit) | | 10 |
| **total (union)** | | **34** |

Sample coverage: basketball 12 · soccer 12 · tennis 10; h2h 19 · spreads 11 ·
totals 4; 12 of the 34 were not in the prior audit's table (392, 480, 900,
997, 1018, 1643, 2003, 2203, 2278, 2465, 35092, 71414, 83082 minus overlaps).

### Per-pick verification table

CORRECT = same fixture confirmed: alias-base equality (or tennis-canonical
identity), kickoff aligned or benignly drifted on a uniquely-paired fixture,
league consistent (incl. no one-sided marker), pick's market present in the
returned close snaps, and — where same-base-pair siblings exist — the matched
row is the Δ0 marker-consistent one.

| pick | sport | market | pick event | arcadia event (raw) | ko Δmin | method/conf | flags | verdict |
|---|---|---|---|---|---|---|---|---|
| 128 | basketball | h2h | Zielona Gora v Legia 06-19 18:15 | Zielona Gora v Legia Warszawa 18:15 | 0.0 | exact/1.0 | same_pair=3 (series legs 06-17/06-21, both outside accept drift) | CORRECT |
| 133 | basketball | h2h | Titanes Del Licey v Caneros 06-20 00:30 | Titanes del Distrito Nacional v Caneros del Este 00:30 | 0.0 | exact/1.0 | same_pair=2 (reverse leg 06-21) | CORRECT |
| 150 | basketball | h2h | Gigantes de Carolina v Cangrejeros 06-23 00:00 | Gigantes de Carolina v Cangrejeros de Santurce 00:00 | 0.0 | exact/1.0 | same_pair=2 (reverse leg 06-21) | CORRECT |
| 153 | basketball | h2h | (same fixture as 150) | (same) | 0.0 | exact/1.0 | same_pair=2 | CORRECT |
| 257 | soccer | h2h | Shanghai Second v Dalian Yingbo B 06-23 08:00 | Shanghai Segenda v Dalian Yingbo II 08:00 | 0.0 | exact/1.0 | reserve marker BOTH sides; league China - League Two (marker-free label) | CORRECT |
| 392 | soccer | totals | Blumenau v Caravaggio 06-24 18:00 | Blumenau v Caravaggio 18:00 | 0.0 | exact/1.0 | Catarinense 2 both sides | CORRECT |
| 480 | soccer | h2h | Afturelding v Njardvik 06-26 19:15 | Afturelding v UMF Njardvik 19:15 | 0.0 | exact/1.0 | Division 1 ≡ Iceland 1. Deild | CORRECT |
| 601 | basketball | spreads | Borneo Hornbills v Pelita Jaya 06-26 12:00 | Borneo Hornbills v Pelita Jaya Jakarta 12:00 | 0.0 | exact/1.0 | same_pair=3 (IBL playoff legs 06-24/06-28) | CORRECT |
| 900 | tennis | h2h | Sabalenka A. v Kessler M. 07-01 12:10 | Aryna Sabalenka v McCartney Kessler 13:00 | 50.0 | exact/1.0 | unique both-names pairing (check below) | CORRECT |
| 974 | soccer | h2h | Claypole v Canuelas 06-30 18:00 | Claypole v Canuelas 18:00 | 0.0 | exact/1.0 | same_pair=2; sibling is **Reserve League** reversed 07-01 — NOT matched | CORRECT |
| 997 | tennis | h2h | Swan K. v Keys M. 07-02 12:05 | Katie Swan v Madison Keys 13:00 | 55.0 | exact/1.0 | unique pairing; mixed-doubles Swan row correctly not matched | CORRECT |
| 1018 | tennis | h2h | Jacquet K. v Bublik A. 07-02 17:05 | Kyrian Jacquet v Alexander Bublik 18:00 | 55.0 | exact/1.0 | unique pairing | CORRECT |
| 1349 | basketball | h2h | Portugal v Montenegro 07-02 18:00 | Portugal v Montenegro 18:00 | 0.0 | exact/1.0 | same_pair=2; sibling is **U20 Women** 07-04 — NOT matched | CORRECT |
| 1643 | basketball | h2h | Egypt v Mali 07-02 15:00 | Egypt v Mali 15:00 | 0.0 | exact/1.0 | FIBA WC Africa qual | CORRECT |
| 1697 | soccer | totals | Spain v Austria 07-02 19:00 | Spain v Austria 19:00 | 0.0 | exact/1.0 | same_pair=2; sibling is **U19 Women** 07-04 — NOT matched | CORRECT |
| 1741 | basketball | h2h | Portugal v Montenegro 07-02 18:00 | (same as 1349) | 0.0 | exact/1.0 | same_pair=2 | CORRECT |
| 1742 | basketball | spreads | (same fixture) | (same) | 0.0 | exact/1.0 | same_pair=2 | CORRECT |
| 1960 | basketball | spreads | (same fixture) | (same) | 0.0 | exact/1.0 | same_pair=2 | CORRECT |
| 2003 | basketball | h2h | Ukraine v Georgia 07-02 15:30 | Ukraine v Georgia 15:30 | 0.0 | exact/1.0 | - | CORRECT |
| 2203 | soccer | spreads | Wieczysta Krakow v Artis Brno 07-02 15:00 | same 15:00 | 0.0 | exact/1.0 | club friendly, both sides | CORRECT |
| 2222 | tennis | h2h | Dimitrov G. v Berrettini M. 07-04 15:30 | Grigor Dimitrov v Matteo Berrettini 17:30 | 120.0 | exact/1.0 | unique pairing; Dimitrov's R16 vs Fery in window NOT matched (one-side only) | CORRECT |
| 2278 | basketball | spreads | Sweden v Czech Republic 07-03 17:00 | same 17:00 | 0.0 | exact/1.0 | - | CORRECT |
| 2454 | soccer | spreads | Queensland Lions v Olympic FC 07-04 09:30 | same 09:30 | 0.0 | exact/1.0 | same_pair=3: **U23 05:00 and Women 07:15 siblings NOT matched**, senior Δ0 matched | CORRECT |
| 2465 | basketball | totals | Canada v Puerto Rico 07-03 23:10 | same 23:10 | 0.0 | exact/1.0 | - | CORRECT |
| 2488 | soccer | h2h | Gold Coast Utd v Magic United 07-05 05:00 | Gold Coast United v Magic United 05:00 | 0.0 | jw_two_tier/0.9647 | ONLY fuzzy match in population (Utd↔United, same club) | CORRECT |
| 2578 | soccer | h2h | Hume City v Dandenong City 07-04 04:00 | same 04:00 | 0.0 | exact/1.0 | same_pair=2: **U23 06:15 sibling NOT matched** | CORRECT |
| 2592 | soccer | spreads | Sutherland Sharks v APIA Leichhardt 07-04 07:30 | same 07:30 | 0.0 | exact/1.0 | same_pair=2: **U20 09:45 sibling NOT matched** | CORRECT |
| 16129 | tennis | h2h | Belinda Bencic v Coco Gauff 07-05 16:26 | same 19:45 | 199.0 | exact/1.0 | court-time drift, unique pairing (WTA R16) | CORRECT |
| 16130 | tennis | h2h | (same fixture) | (same) | 199.0 | exact/1.0 | | CORRECT |
| 35092 | tennis | spreads | Grigor Dimitrov v Arthur Fery 07-06 15:28 | same 15:29 | 0.9 | exact/1.0 | unique pairing (R16) | CORRECT |
| 36922 | tennis | spreads | Jiri Lehecka v Alexander Zverev 07-06 17:06 | same 20:30 | 204.0 | exact/1.0 | court-time drift, unique pairing (R16) | CORRECT |
| 71414 | tennis | h2h | Marta Kostyuk v Linda Noskova 07-09 15:53 | same 16:30 | 36.6 | exact/1.0 | unique pairing (SF); each player's other rounds one-side only | CORRECT |
| 76232 | tennis | totals (totals_3_5) | Arthur Fery v Alexander Zverev 07-10 12:39 | same 12:35 | 4.3 | exact/1.0 | line matched (vocabulary merge) | CORRECT |
| 83082 | tennis | h2h | Karolina Muchova v Linda Noskova 07-11 15:12 | same 16:05 | 52.8 | exact/1.0 | NEW row since prior audit (Wimbledon Final); unique pairing | CORRECT |

### Tennis pairing-uniqueness check

For all 10 sampled tennis rows, a tennis-canonical sweep of the arcadia
window (±2 days) listed every event sharing a surname token with the pick
pair: in **every case exactly one** event matches BOTH players — the matched
one; all other surname hits are single-player (other rounds / doubles) and
were not matched. (e.g. pick 2222: Dimitrov's R2 and R16 fixtures sit in the
window but only Dimitrov–Berrettini matches both names.)

## Verdict (criterion 2)

- **PASS on this re-audit: 0 wrong / 0 ambiguous / 34 audited**, with the
  previously-failing class exactly enumerable and now **empty over the full
  284-row usable population** (0 marker-league attachments, 0 non-tennis
  kickoff deltas ≥ 60 min).
- **9/9 previously-wrong picks refuse** via the deployed league-marker veto;
  16 total veto refusals, all `['women']`-league NBL1 targets — no
  false-positive veto observed (no correct match lost: the removed usable
  rows are exactly the 9 wrong ones).
- Residual risk noted for the operator: the veto keys off the arcadia
  LEAGUE label. A future double-header whose arcadia league label carries no
  marker (or a marker word outside the {women, youth, reserve} vocabulary)
  would not be caught by this veto; the prior audit's ingest-side observation
  (one arcadia event row carrying BOTH games' markets) remains open as an
  independent capture defect to watch.
- **`CLV_USE_PINNACLE_ARCHIVE` stays OFF.** This document only certifies
  criterion 2 on this sample; the flip decision (and any signature) is the
  operator's.

## Reproduction

- Collector (read-only, capturing monkeypatch + logging tap + rollback):
  scratchpad `arcadia_reaudit_collect.py`, piped to
  `docker exec -i -w /srv/betting-ai betting-ai-app-1
  /srv/betting-ai/.venv/bin/python -`; outputs JSON
  (`reaudit_out.json`: funnel counts, 16 veto lines, target-9 refusal report,
  284 usable rows with link provenance, alias bases, markers, kickoff deltas,
  sibling events, snap market/line keys).
- Write-safety: `SELECT count(*) FROM event_source_links` /
  `match_review_queue` before and after — **782 / 159 unchanged**.
- Sample draw: seeded (20260711) stratified fill on top of the exhaustive
  risky classes; tennis uniqueness check is a separate read-only script
  (both in the session scratchpad).
