---
name: canonical-matcher-verifier
description: "Invariants and verification procedure for the cross-source event matcher (app/resolution/). Use when changing matching.py, aliases_seed.json, marker/veto logic, alias batches, or reviewing anything that could attach a wrong Pinnacle close to a pick."
allowed_tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Canonical Matcher Verifier

A wrong-game Pinnacle close is fake CLV — the project's cardinal sin. The matcher
errs toward NO match. Any change here is verified against these invariants, never
against match-rate alone. **Do not loosen a threshold to lift match rate.**

## Hard invariants (app/resolution/matching.py)

- **Marker preservation.** `normalize_name` KEEPS women/youth/reserve tokens;
  `distinguishing_markers` derives {women, youth, reserve} incl. the TRAILING bare
  `2`/`3`/`b` reserve ordinal (only as last token, ≥2 tokens). A one-sided marker is a
  categorical VETO — present-vs-absent IS a conflict. `_KNOWN_CLUB_WHITELIST`
  (Boca Juniors, Young Boys, ...) is consulted first. Slug fallback must refuse a slug
  that LOSES a marker the display name carries.
- **Two-tier fuzzy bands.** Accept = `_JW_ACCEPT 0.92` AND `_TOKEN_SORT_ACCEPT 90.0`
  on alias-canonicalized, marker-stripped bases; `_JW_REVIEW_FLOOR 0.84`..0.92 is the
  REVIEW band — never auto-accepted, only tapped into match_review_queue.
- **Ambiguity margin.** Two DISTINCT candidates within `_AMBIGUITY_MARGIN 0.04`
  summed-JW → REJECT. Duplicate captures of ONE fixture (same canonical teams,
  kickoffs ≤ `_DUPLICATE_CAPTURE_SECONDS` apart) collapse to nearest; same-teams legs
  ACROSS that bound reject (`same_teams_kickoff_split`).
- **Kickoff windows.** Candidate-FETCH window is deliberately wide; ACCEPT is gated
  separately at `_ACCEPT_MINUTE_DRIFT 360` (6h). Never pass the fetch window as the
  accept bound (the go-live ±2-day bug).
- **Tennis initial veto.** Two tennis-canonical names (`"surname f"`) with different
  trailing initials are DIFFERENT players regardless of JW (`cerundolo f/j` scores 0.964).
- **Disambiguating tokens.** Base names differing ONLY by `_DISAMBIGUATING_TOKENS`
  (united/city/sociedad/b/ii/...) are distinct clubs — veto, never fuzzy-merge.
- **Noise tokens are frozen.** `_NOISE_TOKENS` excludes cd/sd/gd/ad/... on purpose
  (see CD-Nacional below). Never broaden it.

## Alias-batch process (the ONLY sanctioned recall lever)

1. `uv run python -m tools.export_alias_candidates` → `docs/review/alias_candidates_<date>.csv`
   (probe-cascade replay + risk flags; `tools/review_queue_cli.py export` feeds the same CSV
   shape from match_review_queue).
2. A HUMAN sets `human_decision=approve` per row; any risk-flagged approval requires
   `reviewer_notes`. Never approve on similarity alone — confirm same club via the
   fixture (same opponent, same kickoff) or an authoritative source.
3. `uv run python -m tools.review_aliases <csv>` → patch + `test_alias_batch_<date>.py`
   skeleton + rejected-pairs block. Seed is written ONLY with `--apply`.
4. Bar to ship: `uv run pytest -q` green AND the wrong-game audit
   (`app.maintenance.wrong_game_audit` / self-audit job) reports **0 new merges**, AND
   the golden `_NOT_ADDED` pairs in tests/test_resolution_nameform_aliases.py stay distinct.

## Verify pass for ANY matcher change

- Run the resolution suites: `uv run pytest tests/test_resolution*.py tests/test_wrong_game_audit.py -q`.
- **Differential fuzz** (the pattern from the last verify pass): drive the SAME inputs
  through the old and new decision paths and assert decision-identity where claimed —
  e.g. `match_event_hardened` vs `match_event_hardened_scored` (wrapper must be
  byte-identical; `review_out` is a tap, NEVER a gate), and pre/post-change decisions
  over a corpus of real name pairs (golden `_AUTO_ADDED`/`_NOT_ADDED` + probe output).
  Any decision flip must be explainable and intended.
- Keep mirrors in sync: `wrong_game_audit._JW_ACCEPT/_TOKEN_SORT_ACCEPT/_DEFAULT_MAX_MINUTE_DRIFT`
  and the probe/tools constants (`_JW_NEARMISS`, `_ACCEPT_SECONDS`) mirror matching.py.
- Measure funnel movement read-only: GET /resolution/match-rate (links block) or
  `scripts/research/probe_unmatched_split.py` — matched / COVERAGE-GAP / NO-COUNTERPART /
  NAME-FORM. Only NAME-FORM (~15.5%, 123 fixtures at last run) is alias-addressable.

## Gotchas

- **The CD-Nacional pitfall.** Stripping club-form tokens (cd/gd/sd) reduces
  "CD Nacional"→"nacional", merging Nacional Madeira with Nacional (Uruguay); it failed
  `test_seed_alias_canonicals_do_not_collide` and was REVERTED (2026-06-27). Recover
  near-misses with reviewed per-club aliases, never blanket normalization.
- **High JW is not same-club.** "Western City Rangers" ↔ "Western Knights" and
  Bayswater/Bayswater City look mergeable and are NOT — that's why the exporter's
  `known_false_pattern`/`city_club_ambiguity` flags exist. Sibling tennis players and
  United/City pairs BEAT the accept thresholds; only the categorical vetoes stop them.
- **A trailing digit is a reserve marker.** "Anything 2"/"... B" is a reserve side
  (MLS Next Pro, Castilla); synthetic test names with trailing digits silently trip the
  marker veto ("Rival 2 FC" carries {reserve} — a real bug found writing tools tests).
- **Aggregate counts ≠ fixture counts.** The probe emits one pair PER qualifying
  candidate-orientation; dedupe by normalized pair before counting "N candidates".
- **match-rate is capture-bound, not name-bound.** The ceiling is fixtures absent from
  one side (26.5% at last audit); don't chase fuzzy recall when the counterparty was
  never captured (COVERAGE-GAP/NO-COUNTERPART are unfixable by aliases).
- **review_status is VARCHAR(16).** Store `approved`/`rejected`; `reviewed_approved`
  (17 chars) overflows. The queue-mark UPDATE may touch ONLY review_status/reviewed_at.
