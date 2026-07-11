# Arcadia NBL1 double-header — league-marker veto + warehouse split (fix note)

**Date:** 2026-07-11 (UTC) · **Status:** implemented, tests green, NOT committed
(orchestrator reviews/commits). Companion to
`docs/research/2026-07-11-arcadia-match-audit.md` (the failing evidence: 9/292
wrong-game attachments, all NBL1 men-plus-women double-headers).

## What shipped

### Part 1 — consume-side league-derived marker veto (tightening-only)

`app/storage/repositories.py`:

- `_league_marker_set(league_name)` — derives `{women, youth, reserve}` from a
  LEAGUE label, reusing `app/resolution/matching.py`'s existing marker
  vocabulary (`_WOMEN_MARKERS`, `_YOUTH_WORD_MARKERS`, `_YOUTH_AGE`, and the
  word members of `_RESERVE_MARKERS`). It deliberately EXCLUDES the positional
  reserve rules that are correct for TEAM names and wrong for league labels:
  trailing `2`/`3`/`b` and roman `ii`/`iii` name SENIOR second divisions in
  league labels ("Bundesliga 2", "Serie B", "Liga II", "League 2") — treating
  them as reserve markers would false-veto whole leagues.
- `resolve_pinnacle_close_snaps` now joins the arcadia candidate's league
  label and, AFTER the matcher accepts, refuses the close when
  `_league_marker_set(arc_league) − (distinguishing_markers(pick_home) ∪
  distinguishing_markers(pick_away))` is non-empty. One-directional and
  post-match, so it can only ever REJECT matches that previously succeeded —
  never accept anything new. Refusals log a quarantine line
  (`pinnacle close: league-marker veto refused …`).
- **Tennis is exempt**: fixtures are person-named (no marker-less same-name
  twin exists), and women's-tour labels ("ITF Women …") would otherwise veto
  every correct women's tennis close — pure recall loss with zero wrong-game
  risk avoided.

### Part 2 — warehouse mint-time double-header split

Two distinct mechanisms produced the "one arcadia event ref carrying BOTH
games' markets" contamination:

1. **Since 2026-07-08 (our warehouse — FIXED).** The PR1a/PR1b mint-time dedup
   resolvers (`_resolve_canonical_event` Tier-1 exact-team-id ±2h,
   `_resolve_canonical_event_by_pair` Tier-2 pair-key ±2h team sports) merged
   the men's arcadia matchup id onto the already-minted women's event row:
   identical club names → same team ids / same pair key, and the 105-120 min
   double-header gap is inside both tolerances. Live proof: 35
   `exact_team_id` links in `pinnacle_basketball`, all dated ≥ 2026-07-08
   09:09 (Tier-1's ship date), e.g. men's source id `1632344664` merged onto
   canonical 13681 "Ringwood Hawks v Bendigo Braves" whose league is
   `Australia - NBL1 Women`. Fix: both tiers now exclude a candidate whose
   league label DISAGREES with the incoming league on
   `_league_marker_set` markers ("league-marker split"), so the double-header
   mints two event rows; genuine duplicate captures (marker-equal leagues)
   still merge. With both rows present, the hardened matcher's
   duplicate-capture collapse picks the NEAREST kickoff, so a men's pick
   anchors its own men's close (tested end-to-end).

2. **Before 2026-07-08 (upstream-inherent — DOCUMENTED, not fixable
   capture-side).** The audit's July 2-4 rows predate Tier-1: no second
   (men's) event row, no source link, no redirect exists for any of the six
   contaminated fixtures (e.g. event 7076 / ref `1632193196`, Ringwood v
   Kilsyth), yet the row carries both totals clusters (150-160.5 women's,
   184.5-185.5 men's — concurrent from 2026-06-30) and its h2h stream shows a
   sharp discontinuity at the women's tip and keeps updating ~2h past it.
   With the pre-Tier-1 code, a distinct men's matchup id would have MINTED
   its own row — none exists — so the men's markets must have arrived under
   the women's matchup id from the feed itself (arcadia id reuse / rolling
   the double-header on one matchup entry). `app/ingestion/pinnacle_arcadia.py`
   joins markets to matchups strictly by `matchupId` and cannot attribute a
   market row to a game the feed does not distinguish; no capture-side change
   was made. Part 1's consume-side veto makes these legacy rows unable to
   anchor any pick regardless (men's picks: league-marker veto; women's
   picks: the pre-existing team-name marker veto).

### Part 3 — shadow freshness re-report (report-only)

`scripts/research/clv_close_freshness_study.py` gained
`--max-sharp-close-age-minutes MIN [MIN …]`: a section (e) that RE-REPORTS the
stored-close trusted subset excluding sharp closes older than each cap, with a
per-`closing_anchor_type` breakdown. No production gate, no config flag
(shadow-first mandate).

## Tests

`tests/test_resolution_league_marker_veto.py` — 4 pure + 8 DB tests:
women's-league refusal (the audit class), one-sidedness (women-marked pick
still matches), men's-league unaffected, division-numbered leagues not
reserve-vetoed, tennis exemption, double-header mints separate events,
same-league duplicates still merge, and the end-to-end nearest-event men's
close after the split.

## Re-audit requirement (unchanged)

Criterion 2 of the `CLV_USE_PINNACLE_ARCHIVE` flip still requires a FRESH
≥30-pick manual audit with 0 wrong-game attachments after this remediation;
the deterministic collection harness from the 2026-07-11 audit can be reused.
