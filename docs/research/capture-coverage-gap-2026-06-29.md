# Capture-coverage gap analysis — what to ingest vs leave (2026-06-29)

**Scope:** the cross-source match rate between our OddsPortal pick scrape and the
sharp Pinnacle archive. The binding constraint is **NO-COUNTERPART (26.5%)**, not
alias near-misses. This report ranks the coverage-gap leagues by value and gives a
concrete **ingest / leave / fix** recommendation. Read-only; cites live counts.

**Instruments (both new, read-only, under `scripts/research/`):**
- `coverage_gap.py` — per-league label aggregation + sharp-inventory cross-ref.
- `alias_review_queue.py` — the companion ambiguous-alias review surface.

**Data:** live `betting-ai-postgres-1`, 332 pick-fixtures with a known kickoff,
replayed through the live-equivalent cascade (`match_event` → OddsPortal slug
fallback → `match_event_hardened`). Run 2026-06-29.

---

## 1. Headline — the gap is CAPTURE, not naming

| label | n | share |
|---|---|---|
| matched | 209 | 63.0% |
| coverage-gap (no same-day archive at all) | 0 | 0.0% |
| **no-counterpart** (archive that day, no same fixture) | **88** | **26.5%** |
| name-form (alias-addressable near-miss) | 35 | 10.5% |

Of the **123 unmatched** fixtures (88 + 35), splitting each pick-league against the
Pinnacle archive's *own* league inventory:

| gap kind | est. addressable picks | meaning |
|---|---|---|
| **CAPTURE** | **96** | Pinnacle prices **no** league with that key → nothing to match against |
| mixed | 22 | some no-cp + some alias near-miss in the same league |
| NAME-MATCH | 5 | Pinnacle **does** price the league → miss is naming/marker, not capture |

**78% of the addressable gap is CAPTURE** — the sharp book does not book those
leagues. Aliases cannot fix a counterpart that was never captured, and for the
league types involved (below) there is no liquid sharp line to capture even if we
built new ingestion.

### Addressable picks by value tier

| value tier | est. addressable picks | verdict |
|---|---|---|
| major | 9* | *false-positive tag — see §2 |
| mid | 50 | mostly minnow/friendly/regional → low value |
| thin-obscure | 44 | state/lower-division/regional → low value |
| youth-women-reserve | 20 | outside the backtest universe → leave |

---

## 2. The "major" tier is a heuristic false positive

The value-tier regex promoted three keys to "major" that are **not** the leagues
their names suggest (league keys carry no country in this table):

- **soccer "Premier League" (7 est. gain, CAPTURE)** — live teams are FC
  Ulaanbaatar (Mongolia), Paro/Drukpa (Bhutan), Al Salmiya (Kuwait), Welwalo
  Adigrat (Ethiopia), Al Shorta (Lebanon). A grab-bag of **minnow national top
  flights**, not the EPL. Pinnacle prices none of them → CAPTURE. **Re-tier: thin.**
- **basketball "World Cup" (1 est. gain)** — already 12/13 matched; not a gap.
- **soccer "MLS Next Pro" (1, mixed)** — a single alias near-miss
  (`Minnesota 2` ↔ `Minnesota United II`), already on the review queue.

After correction, the **true major-league capture gap is effectively zero** — every
real top-flight is either out of season (June) or already matched.

Likewise in "mid": **GFA League** = Gambia FA League (Real Banjul, Steve Biko);
**Copa de la Liga** = Peruvian Liga 1 (Sport Huancayo, U. de Deportes). Regional,
not major.

---

## 3. The single high-value, genuinely actionable finding: WNBA

**WNBA — 16 picks, 11 matched, 5 no-counterpart, gap kind = NAME-MATCH.**

This is the *only* gap that is both high-value (a real, liquid league) **and**
already has the sharp counterpart captured (Pinnacle prices WNBA; archive confirmed
`True`). The 5 misses are **not** a capture gap and **not** an alias gap — they trip
the wrong-game **women-marker veto**: OddsPortal labels WNBA teams with a trailing
`W` (`Washington Mystics W`) while Pinnacle uses the bare name (`Washington
Mystics`). Because the **entire league is women**, that `W` is not actually a
fixture-distinguishing marker, but the matcher correctly refuses it under the
general veto (a women-vs-men conflation would be a wrong-game CLV defect).

**This is a matching fix, not an ingestion task,** and it touches `app/resolution/
matching.py` (frozen for this workstream). Recommended hand-off to the matching
workstream: a per-league marker-suppression for all-women leagues (WNBA, and by the
same logic the women's soccer leagues *if* a sharp counterpart is ever captured),
so the league-wide `W` is dropped before the marker veto. Estimated immediate gain:
**+5 matched picks**, high relevance. The review queue surfaces these as
`wrong-game-marker` BLOCKERs (e.g. `Washington Mystics W vs Portland Fire W` ↔
`Washington Mystics vs Portland Fire`) so they are visible, not silently dropped.

---

## 4. Ranked coverage-gap leagues (value-first)

Full table in `coverage_gap.py` output / `coverage_gap.csv`. Top of each tier:

| tier | sport | league | picks | matched | no-cp | name-form | gap | est. gain | note |
|---|---|---|---|---|---|---|---|---|---|
| (real) major | — | — | — | — | — | — | — | **0** | no in-season top-flight gap |
| mid | basketball | **WNBA** | 16 | 11 | 5 | 0 | NAME-MATCH | 5 | **fix marker (see §3)** |
| mid | soccer | Club Friendly | 18 | 5 | 9 | 4 | CAPTURE | 13 | friendlies — low value, no sharp line |
| mid | soccer | GFA League (Gambia) | 9 | 2 | 7 | 0 | CAPTURE | 7 | obscure |
| mid | basketball | Friendly International | 4 | 0 | 4 | 0 | CAPTURE | 4 | friendlies |
| mid | basketball | BIG3 (3x3) | 2 | 0 | 2 | 0 | CAPTURE | 2 | different format; no sharp line |
| thin | soccer | Copa de la Liga (Peru) | 10 | 2 | 8 | 0 | CAPTURE | 8 | regional; Pinnacle not pricing |
| thin | soccer | USL League Two | 11 | 8 | 3 | 0 | CAPTURE | 3 | US lower div; mostly matched |
| thin | soccer | NSW League One | 4 | 0 | 2 | 2 | CAPTURE | 4 | AU state semi-pro |
| youth-women | soccer | Primera A Women | 4 | 0 | 4 | 0 | CAPTURE | 4 | archive lacks league → leave |
| youth-women | soccer | Super League Women | 4 | 0 | 4 | 0 | CAPTURE | 4 | archive lacks league → leave |

(38 gap-leagues total; the long tail is 1–2 picks each, overwhelmingly CAPTURE.)

---

## 5. Recommendation — ingest these / leave those

**Ingest: none.** There is no jointly-coverable league that justifies a new
sharp-data ingestion build. Every CAPTURE-gap league is one of: out-of-season or
minnow national top-flights, club friendlies, 3x3/exhibition formats, women's /
youth / reserve competitions, or AU/US state semi-pro divisions — none of which a
sharp book (Pinnacle) prices, so there is no closing line to capture. These are
also outside the settled-backtest universe (consistent with the standing note that
obscure-league premiums sit outside the backtest universe).

**Fix (not ingest), high priority — WNBA `W`-marker (+5 picks):** hand to the
matching workstream. Per-league women-marker suppression for all-women leagues.
This is the highest-value, lowest-cost lever in the whole gap and needs no new data
source. The same pattern would unlock the women's-soccer leagues **only if** a sharp
counterpart is later captured — today their archive league is absent (CAPTURE), so
the marker fix alone would not help them yet.

**Monitor (revisit only with new evidence):** Copa de la Liga (Peru, 8 no-cp) and
the Swedish lower-division cluster (Division 1/2/3) — if a read-only Betfair
Exchange feed is found to price them (Betfair capture is moneyline-only today), the
gap could be re-evaluated. Until then: CAPTURE, no action.

**Leave (the bulk — ~110 of 123 addressable picks):** all youth/women/reserve (20),
Club Friendly (13), GFA/national-minnow "Premier League" (≈14), 3x3/BIG3,
state-league and Division-3 long tail. Low betting value, no sharp line, outside the
pick universe we backtest.

### Bottom line

The 26.5% NO-COUNTERPART headline overstates the *fixable* gap: 96 of 123
addressable picks are CAPTURE on leagues no sharp book books, so the lever is **not
more ingestion**. The one concrete, high-value win — **WNBA, +5 picks** — is a
marker-handling fix on already-captured data, not a coverage problem. Net new-source
recommendation: **build nothing; fix the WNBA marker; leave the obscure tail.**
