# Escalated alias pairs — operator review (2026-07-03)

**Purpose.** The 2026-07-02 alias batch (`88eeb52`) approved 28 / rejected 58 of 86
candidates and escalated **16 "tempted" pairs** in the row notes of
`docs/review/alias_candidates_2026-07-02.csv` — 9 thin-evidence (< 2 co-occurring
fixtures at vetting time) + 7 known-false-flagged (mandate-reject but with real
co-occurrence evidence recorded). This file lays them out one-by-one for the
operator's judgment. **Nothing here is applied.** Any approval goes through the
sanctioned batch process (`tools/review_aliases.py`, wrong-game audit 0-new-merges,
golden `_NOT_ADDED` review where flagged).

**Marker reconstruction note (honesty).** 14 rows carry explicit escalation markers
in `reviewer_notes` (`escalate` on the known-false rows, `re-export next batch` on
the thin rows). The commit message counts 16 (9+7). The two reconstructed remainder
rows are AC-0073 (known-false, note records "mandate reject **despite 2 co-occurring
fixtures**") and AC-0045 (thin, note records "fewer than 2 **despite exact
opponent+kickoff**") — the only other rows whose notes record temptation-grade
evidence. AC-0030 (Sabah) is included as a borderline appendix row for completeness.

**Evidence refresh (2026-07-03, last-7-days fixture co-occurrence, live DB).** For
every pair below, the co-occurrence SQL (same fixture visible in both the
OddsPortal and `pinnacle_*` namespaces: aligned opponent + kickoff within 60 min)
was re-run against events with `starts_at >= now()-7d`. Rows marked **UPGRADED**
have accrued a NEW co-occurring fixture since the 2026-07-02 vetting.

Approval bar reminder (from the batch process): >= 2 distinct co-occurring fixtures,
no unresolved risk flag, or an explicit operator exception with reviewer_notes.

---

## A. Known-false-flagged "tempted" pairs (7)

### 1. AC-0002 — `Gigantes San Francisco` (oddsportal) <> `Indios de San Francisco de Macoris` (pinnacle_basketball)
- **Sport/league/country:** basketball / LNB / Dominican Republic
- **Vetting evidence:** 4 co-occurring LNB fixtures with identical opponents+kickoffs
  (Metros de Santiago 06-19; Heroes de Moca 06-25/27/28) — but the 06-28 fixture has
  home/away **inverted** across sources.
- **Example events:** Heroes de Moca vs Gigantes San Francisco @ 2026-06-28 22:00 UTC.
- **7-day refresh:** 2 co-occurrences still visible (Heroes de Moca 06-27 exact;
  06-28 **H/A inverted**). No new fixtures.
- **Risk flags:** `weak_similarity|known_false_pattern` (seed known-false list).
- **Why escalated:** evidence pattern suggests one franchise under two brandings,
  but the mandate-reject flag + the H/A inversion blocked approval.
- **Recommended decision: needs-more-evidence.** Two distinct San Francisco de
  Macorís franchises exist historically (Indios; Gigantes del Cibao are ALSO based
  in SFM) — the name overlap is exactly the trap the known-false list encodes. The
  H/A inversion is a classic wrong-fixture symptom. Require an authoritative roster/
  schedule source naming the OddsPortal side before any golden-list change.
- human_decision:
- reviewer_notes:

### 2. AC-0014 — `Gremio Juventus` (oddsportal) <> `Juventus SC` (pinnacle_soccer)
- **Sport/league/country:** soccer / Catarinense 2 / Brazil
- **Vetting evidence:** 2 exact co-occurrences (Nacao 06-17 22:30; Blumenau 07-01
  18:00), Catarinense 2 both sides — strongly suggests GE Juventus (Jaraguá do Sul).
- **Example events:** Blumenau vs Gremio Juventus @ 2026-07-01 18:00 UTC.
- **7-day refresh:** 1 co-occurrence in window (Blumenau 07-01, exact). No new.
- **Risk flags:** `weak_similarity|city_club_ambiguity|known_false_pattern`
  (**literal golden `_NOT_ADDED` pair**).
- **Why escalated:** meets the >=2-fixtures bar on evidence; blocked solely by the
  golden list, which cannot be overridden without operator sign-off.
- **Recommended decision: approve, conditional on a golden-list amendment.** Both
  sides sit in the same minor Brazilian league with twice-aligned exact fixtures;
  the golden entry appears to encode generic Juventus-collision fear (Juventus
  Turin/Mooca) that a league-scoped check refutes here. If approved, the
  `_NOT_ADDED` pair must be consciously removed in the same commit with a test.
- human_decision:
- reviewer_notes:

### 3. AC-0017 — `Annan` (oddsportal) <> `Annan Athletic` (pinnacle_soccer)
- **Sport/league/country:** soccer / Club Friendly / World (club is Scottish L2)
- **Vetting evidence:** 2 exact co-occurrences (Johnstone Burgh 06-23; Gretna 2008 06-30).
- **Example events:** Gretna 2008 vs Annan @ 2026-06-30 18:45 UTC.
- **7-day refresh:** 1 in window (Gretna 2008 06-30, exact). No new.
- **Risk flags:** `weak_similarity|city_club_ambiguity|known_false_pattern`
  ('athletic' disambiguating-token class).
- **Why escalated:** meets the >=2 bar; blocked by the token-class mandate
  (bare-name vs +Athletic can be distinct clubs, e.g. Dunfermline-class traps).
- **Recommended decision: approve (operator token-class exception).** "Annan" has
  exactly one senior club (Annan Athletic FC); no competing "Annan <other>" exists
  in either namespace. Low residual risk; document the exception in reviewer_notes.
- human_decision:
- reviewer_notes:

### 4. AC-0053 — `Minnesota 2` (oddsportal) <> `Minnesota United II` (pinnacle_soccer)
- **Sport/league/country:** soccer / MLS Next Pro / USA
- **Vetting evidence:** 2 exact co-occurrences (Los Angeles FC 2/II 06-25 02:00;
  Tacoma Defiance 06-29 00:00), MLS Next Pro both sides.
- **Example events:** Los Angeles FC 2 vs Minnesota 2 @ 2026-06-25 02:00 UTC.
- **7-day refresh:** 1 in window (Tacoma Defiance 06-29, exact). No new.
- **Risk flags:** `weak_similarity|same_country_common_name|known_false_pattern`
  ('united' disambiguating-token class).
- **Why escalated:** evidence meets the bar; blocked by the 'united' token mandate.
- **Recommended decision: approve (whitelisted exception).** Reserve markers agree
  on BOTH sides (trailing `2` = `II` — the matcher's marker logic already treats
  both as {reserve}), and MLS Next Pro has exactly one Minnesota entry (MNUFC2).
  The 'united' trap (e.g. "Minnesota" vs "Minnesota United" as different clubs)
  does not apply when both names carry the reserve marker and the league is closed.
- human_decision:
- reviewer_notes:

### 5. AC-0058 — `Playford Patriots` (oddsportal) <> `Playford City` (pinnacle_soccer) — **UPGRADED (new fixture)**
- **Sport/league/country:** soccer / NPL South Australia / Australia
- **Vetting evidence:** 2 exact co-occurrences (Campbelltown City 06-20; FK Beograd 06-27).
- **Example events:** Playford Patriots vs FK Beograd @ 2026-06-27 05:30 UTC.
- **7-day refresh:** 2 in window — FK Beograd 06-27 (exact) **and a NEW third
  fixture: Sturt Lions vs Playford Patriots / Sturt Lions vs Playford City @
  2026-07-04 05:30 UTC** (both namespaces list it). Total distinct co-occurrences
  now **3**.
- **Risk flags:** `weak_similarity|same_country_common_name|known_false_pattern`
  (**literal seed known-false: Playford Patriots/Playford City**).
- **Why escalated:** evidence says one renamed club (the SA club is "Playford City
  Patriots SC"); the literal known-false entry mandates reject.
- **Recommended decision: approve, conditional on golden/known-false amendment.**
  Three aligned fixtures across two weeks in a one-club-per-town league is strong;
  the known-false entry looks like a stale rename guard. Amend it deliberately.
- human_decision:
- reviewer_notes:

### 6. AC-0076 — `Redlands` (oddsportal) <> `Redlands United` (pinnacle_soccer) — **UPGRADED (new fixture)**
- **Sport/league/country:** soccer / Queensland Premier League / Australia
- **Vetting evidence:** 2 exact co-occurrences (Holland Park Hawks 06-26 10:30;
  Logan Lightning 06-30 10:30), QPL both sides.
- **Example events:** Logan Lightning vs Redlands @ 2026-06-30 10:30 UTC.
- **7-day refresh:** 2 in window — Logan Lightning 06-30 (exact) **and a NEW third
  fixture: North Star vs Redlands / North Star vs Redlands United @ 2026-07-04
  07:45 UTC.** Total distinct co-occurrences now **3**.
- **Risk flags:** `same_country_common_name|city_club_ambiguity|known_false_pattern`
  (literal seed known-false; 'united' token class).
- **Why escalated:** evidence meets the bar; literal known-false mandate blocked it.
- **Recommended decision: approve, conditional on golden/known-false amendment.**
  Same reasoning as AC-0058: three aligned QPL fixtures, single plausible club
  (Redlands United FC). The 'united' generic fear is refuted by league scoping.
- human_decision:
- reviewer_notes:

### 7. AC-0073 — `Racing` (oddsportal) <> `Racing Beirut` (pinnacle_soccer) *(reconstructed 7th known-false)*
- **Sport/league/country:** soccer / Lebanon PL context / Lebanon
- **Vetting evidence:** 2 co-occurrences (Nejmeh 06-29 13:00 exact; Al Riyadi 07-02 13:00).
- **Example events:** Nejmeh SC vs Racing @ 2026-06-29 13:00 UTC.
- **7-day refresh:** both still visible (Nejmeh SC 06-29; Al Riyadi Abbasiyah 07-02),
  opponents aligned via already-approved Lebanese aliases. No new fixtures.
- **Risk flags:** `known_false_pattern` (literal seed known-false: bare 'Racing' is
  a **generic base** — Racing Club Avellaneda / Racing Santander / Racing Beirut class).
- **Why escalated:** the note explicitly records "mandate reject despite 2
  co-occurring fixtures; recall needs a scoped fix, not a generic-base alias."
- **Recommended decision: reject (as a global alias); build scoped recall instead.**
  A global `racing -> Racing Beirut` alias is the CD-Nacional pitfall verbatim: it
  would merge every bare "Racing" worldwide into the Lebanese club. The correct fix
  is league/country-scoped aliasing (not currently supported by aliases_seed.json)
  — track as matcher tech-debt, do not approve under the current mechanism.
- human_decision:
- reviewer_notes:

---

## B. Thin-evidence pairs (9)

### 8. AC-0015 — `A. Salzburg` (oddsportal) <> `Austria Salzburg` (pinnacle_soccer)
- **Sport/league/country:** soccer / Club Friendly / World (Austria)
- **Vetting evidence:** 1 co-occurring fixture (07-01 vs Tirol/WSG Tirol — opponent
  itself an unvetted near-pair, AC-0032).
- **Example events:** A. Salzburg vs Tirol @ 2026-07-01 16:30 UTC.
- **7-day refresh:** same single fixture (= Austria Salzburg vs WSG Tirol).
  **No new fixtures — still 1.**
- **Risk flags:** `weak_similarity`.
- **Why escalated:** likely Austria Salzburg, but 1 fixture < the >=2 bar and the
  only opponent is itself unproven (evidence circularity).
- **Recommended decision: needs-more-evidence.** Austrian season restart will
  produce clean co-occurrences quickly; nothing forces an exception now.
- human_decision:
- reviewer_notes:

### 9. AC-0021 — `Din. Zagreb` (oddsportal) <> `Dinamo Zagreb` (pinnacle_soccer)
- **Sport/league/country:** soccer / Club Friendly / World (Croatia)
- **Vetting evidence:** 1 co-occurring fixture (Grosuplje 07-02; opponent an
  unvetted near-pair), despite 6 sample events on the pick side.
- **Example events:** Grosuplje vs Din. Zagreb @ 2026-07-02 16:30 UTC.
- **7-day refresh:** same single fixture (Grosuplje/Brinje Grosuplje 07-02 16:30).
  **No new — still 1.**
- **Risk flags:** none.
- **Why escalated:** obvious abbreviation, blocked purely by the 1-fixture count.
- **Recommended decision: approve (operator "1 fixture is enough" exception).**
  "Din. Zagreb" has exactly one plausible expansion in world football; the
  abbreviation class (`Din.` -> `Dinamo`) is benign, and Dinamo's European/league
  fixtures are high-value for Pinnacle anchoring. If the operator prefers strict
  rules: needs-more-evidence (a 2nd fixture is days away).
- human_decision:
- reviewer_notes:

### 10. AC-0023 — `Grasshoppers` (oddsportal) <> `Grasshopper Club Zurich` (pinnacle_soccer)
- **Sport/league/country:** soccer / Club Friendly / World (Switzerland)
- **Vetting evidence:** 1 exact co-occurring fixture (Cham 07-01 16:30).
- **Example events:** Cham vs Grasshoppers @ 2026-07-01 16:30 UTC.
- **7-day refresh:** same single fixture. **No new — still 1.**
- **Risk flags:** none.
- **Why escalated:** unambiguous name, blocked purely by count.
- **Recommended decision: approve (operator 1-fixture exception)** — same reasoning
  as AC-0021 (unique club, benign name form); strict alternative: needs-more-evidence.
- human_decision:
- reviewer_notes:

### 11. AC-0027 — `Plzen` (oddsportal) <> `Viktoria Plzen` (pinnacle_soccer)
- **Sport/league/country:** soccer / Club Friendly / World (Czechia)
- **Vetting evidence:** 1 co-occurring fixture (Trnava pair 07-01 — opponent itself
  the known-false-flagged AC-0034 pair).
- **Example events:** Plzen vs Trnava @ 2026-07-01 15:00 UTC.
- **7-day refresh:** same single fixture (= Viktoria Plzen vs Spartak Trnava).
  **No new — still 1.**
- **Risk flags:** `weak_similarity|city_club_ambiguity` (bare 'Plzen' is a city name).
- **Why escalated:** obviously Viktoria Plzen to a human, but generic-base name +
  unvetted opponent + 1 fixture.
- **Recommended decision: needs-more-evidence.** Unlike AC-0021/0023 the pick-side
  name is a bare CITY (generic-base class) and the single opponent is itself a
  flagged pair. Wait for a league fixture with a vetted opponent.
- human_decision:
- reviewer_notes:

### 12. AC-0028 — `Queen's Park` (oddsportal) <> `Queens Park` (pinnacle_soccer)
- **Sport/league/country:** soccer / Club Friendly / World (Scotland)
- **Vetting evidence:** 1 distinct fixture (Clyde 06-30 18:45; the archive
  double-captured both orientations, so raw pair count overstates).
- **Example events:** Clyde vs Queen's Park @ 2026-06-30 18:45 UTC.
- **7-day refresh:** same single distinct fixture (2 rows = both orientations of
  the same Clyde fixture — the aggregate-vs-fixture-count gotcha). **No new — still 1.**
- **Risk flags:** none (punctuation-only difference).
- **Why escalated:** punctuation-only difference, blocked purely by count.
- **Recommended decision: approve (operator 1-fixture exception).** The difference
  is a single apostrophe; collision candidates (Queens Park Rangers) never appear
  in this bare form. Worth also checking whether `normalize_name` SHOULD already
  fold apostrophes — if it does and this pair still queued, that is a normalization
  bug to fix instead of an alias.
- human_decision:
- reviewer_notes:

### 13. AC-0045 — `Benjamin Aceval` (oddsportal) <> `Club Doctor Benjamín Aceval` (pinnacle_soccer) *(reconstructed 9th thin row)*
- **Sport/league/country:** soccer / Division Intermedia / Paraguay
- **Vetting evidence:** 1 exact co-occurring fixture (Independiente FBC 06-30 21:00)
  — "fewer than 2 despite exact opponent+kickoff".
- **Example events:** Independiente FBC vs Benjamin Aceval @ 2026-06-30 21:00 UTC.
- **7-day refresh:** same single fixture. **No new — still 1.**
- **Risk flags:** `weak_similarity` (accent/word-order distance only).
- **Why escalated:** exact opponent+kickoff alignment on the one fixture that exists.
- **Recommended decision: needs-more-evidence.** Name is effectively unique
  (a town-named club), but Division Intermedia plays weekly — the 2nd fixture will
  accrue within days; no reason to spend an exception.
- human_decision:
- reviewer_notes:

### 14. AC-0056 — `NE Metrostars` (oddsportal) <> `MetroStars` (pinnacle_soccer) — **UPGRADED: now approvable on existing rules**
- **Sport/league/country:** soccer / NPL South Australia / Australia
- **Vetting evidence:** 1 co-occurring fixture (Para 06-27).
- **Example events:** Para vs NE Metrostars @ 2026-06-27 05:30 UTC.
- **7-day refresh:** **2 distinct co-occurrences** — Para/Para Hills Knights
  06-27 05:30 AND a **NEW fixture: NE Metrostars vs West Torrens / MetroStars vs
  West Torrens Birkalla @ 2026-07-04 09:45 UTC.** This pair now MEETS the >=2
  co-occurring-fixtures bar with no known-false flag — approvable under existing
  rules, no exception needed.
- **Risk flags:** `weak_similarity|city_club_ambiguity` (soft flags; club is
  "North Eastern MetroStars", so `NE` is a benign truncation).
- **Why escalated:** was thin (1 fixture) at vetting time.
- **Recommended decision: approve** (on existing rules, next batch).
- human_decision:
- reviewer_notes:

### 15. AC-0074 — `Zhenis` (oddsportal) <> `Zhenys` (pinnacle_soccer)
- **Sport/league/country:** soccer / Premier League / Kazakhstan
- **Vetting evidence:** 1 exact co-occurring fixture (Astana 07-03 14:00); the
  archive name 'Zhenys' is ALSO reused in the Kazakhstan Women league.
- **Example events:** FC Astana vs Zhenis @ 2026-07-03 14:00 UTC.
- **7-day refresh:** same single fixture (= Astana vs Zhenys). **No new — still 1.**
- **Risk flags:** none listed, but the women-league name reuse is a real marker
  hazard (a women-side event without the marker token would merge wrongly).
- **Why escalated:** transliteration variant with exact fixture alignment, 1 fixture.
- **Recommended decision: needs-more-evidence.** Almost certainly the same club,
  but the women-league reuse means approval should wait for a 2nd men's-league
  co-occurrence AND a check that the women's events carry the women marker in both
  namespaces.
- human_decision:
- reviewer_notes:

### 16. AC-0081 — `ML Vitebsk` (oddsportal) <> `Maxline Vitebsk` (pinnacle_soccer)
- **Sport/league/country:** soccer / Vysshaya Liga / Belarus
- **Vetting evidence:** 1 exact co-occurring fixture (Slavia Mozyr 07-01 17:30);
  the B side missed the 06-27 Naftan game (capture gap, not a mismatch).
- **Example events:** Slavia Mozyr vs ML Vitebsk @ 2026-07-01 17:30 UTC.
- **7-day refresh:** same single fixture. **No new — still 1.**
- **Risk flags:** `weak_similarity` (`ML` -> `Maxline` benign-expansion class).
- **Why escalated:** benign-expansion class, blocked purely by count.
- **Recommended decision: needs-more-evidence** (weekly league; 2nd fixture is
  imminent). If the operator wants it now, the expansion class is low-risk.
- human_decision:
- reviewer_notes:

---

## C. Cove Rangers / Turriff Utd status (requested explicitly)

- **Pair:** `Turriff Utd` (oddsportal) <> `Turriff United` (pinnacle_soccer);
  the co-occurring fixture is **Cove Rangers vs Turriff Utd / Cove Rangers vs
  Turriff United @ 2026-07-02 18:00 UTC** (Scottish friendly). "Cove Rangers"
  itself matches exactly on both sides — the alias question is only Utd/United.
- **Not in the 2026-07-02 CSV** (it surfaced via the `self_audit wrong_game_anchor`
  false-flag during overnight monitoring, after the export was cut).
- **7-day refresh: still exactly 1 co-occurring fixture** — below the >=2 bar, so
  under existing rules it stays queued for the next export.
- **Operational cost of waiting:** every audit cycle re-flags this fixture as a
  wrong-game anomaly (known-benign noise the operator must keep ignoring — the 5
  baseline anomalies in the batch-commit audit were this same Utd/United class).
- **Recommended decision: approve (operator "1 fixture is enough" exception).**
  `Utd -> United` is a pure benign-expansion class with the identical town prefix;
  wrong-club risk is effectively nil, and approving silences a recurring audit
  false-flag. Strict alternative: wait for the next Turriff co-occurrence.
- human_decision:
- reviewer_notes:

---

## Appendix — borderline row, listed for completeness (not counted in the 16)

### AC-0030 — `Sabah Baku` (oddsportal) <> `Sabah FK` (pinnacle_soccer)
- **Sport/league/country:** soccer / Club Friendly / World (Azerbaijan)
- **Vetting evidence:** co-occurrences exist (Universitatea Craiova 06-24 / Polissya
  Zhytomyr 07-02; Ferencvaros 06-28 nearby) — BUT the archive canonical `Sabah FK`
  **spans two real teams** (the Azerbaijani club AND a Malaysia President Cup U20
  side, Johor Darul Ta'zim III fixture 06-25).
- **Example events:** Polissya Zhytomyr vs Sabah Baku @ 2026-07-02 15:00 UTC.
- **7-day refresh:** 1 in window (Polissya Zhytomyr 07-02, exact). No new.
- **Risk flags:** `city_club_ambiguity`.
- **Recommended decision: reject as an alias.** More fixtures cannot fix a canonical
  key that points at two teams; the fix is archive-side (split/dedupe the `Sabah FK`
  canonical), not an alias. Kept here so the temptation is documented.
- human_decision:
- reviewer_notes:

---

*Prepared 2026-07-03 (read-only DB evidence refresh; window = events starting
now()-7d, opponent+kickoff aligned within 60 min across namespaces). Nothing in
this file was applied to `aliases_seed.json`, the golden lists, or the queue.*
