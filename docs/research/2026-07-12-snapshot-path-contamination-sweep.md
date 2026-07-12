# Snapshot-path contamination sweep — verdict: NO residual cross-game contamination

**Date:** 2026-07-12 (UTC) · **Status:** READ-ONLY sweep; no DB rows changed by
this sweep (the only arcadia re-nulls remain the 6 from the 2026-07-12
signature execution). Closes the last open question of the NBL1 double-header
defect class (`docs/research/2026-07-11-arcadia-match-audit.md`,
`...-reaudit.md`, `...-double-header-fix.md`).

## Question

The mint-time Tier-1/Tier-2 event dedup (2026-07-08 → the fix) could, in
principle, have merged a women's arcadia matchup onto a men's canonical event
so that the ORDINARY snapshot-close path attached wrong-game Pinnacle prices —
a contamination surface independent of the resolve path that produced the
already-re-nulled 6. Also: the pre-07-08 upstream-inherent class (event-7076,
both games' markets under one matchup id).

## Method + findings (read-only SQL, live prod DB via container)

1. **The arcadia-close surface is isolated to `resolve_pinnacle_close_snaps`.**
   The arcadia capture writes to the isolated `pinnacle_<sport>` warehouse
   namespace, NOT `odds_snapshots`; `finalize_closing_from_snapshots` reads
   `odds_snapshots`; only `resolve_pinnacle_close_snaps` reads the arcadia
   namespace. So a pinnacle/arcadia close can enter a pick ONLY through the
   resolver — the exact path the deployed league-marker veto covers.
2. **The persisted merge signature does not exist in `event_source_links`.**
   All 557 `pinnacle_arcadia` links have `raw_league` NULL; **0** carry a
   women/youth/reserve label; **0** canonical events have >1 distinct arcadia
   matchup linked. There is no persisted men+women event merge to sweep — the
   contamination the audit found was resolve-time matching, not a stored merge.
3. **Cross-sport sharp-close check on marker-labeled leagues.** Every settled
   pick with a stored sharp (`pinnacle`/`sharp`) snapshot close on a
   women/youth/reserve league is a pick genuinely MADE on that league — its
   close correctly matches its own league (soccer women's/U19/U20, tennis
   Wimbledon Ladies [tennis is veto-exempt by design — person-named, no
   marker-less twin]). NONE is a men's pick carrying a women's close.
4. **The 6 re-nulled picks stay clean** (1585, 2307, 2421, 2573, 2574, 2594 —
   `close_exclusion_reason='wrong_game'`, closing_anchor_type NULL).

## Verdict

**NO residual cross-game contamination.** The arcadia-close contamination
class is fully remediated: the surface is the resolver alone (confirmed
isolated), the deployed veto refuses exactly the 16 NBL1-women rows
(re-audit 0-wrong/34), and the only sharp-subset members among them (the 6)
were re-nulled. No snapshot-path or pre-07-08 residual exists for any
settled pick's sharp close.

## Flagged for the operator (separate issue, NOT re-nulled)

Two settled picks show anomalous extreme-negative CLV via the **Betfair
Exchange** path (not arcadia, not the double-header class):
- **1671** — soccer, Euro U19 Women, h2h, clv_log **−1.28**
- **1672** — soccer, Euro U19 Women, h2h, clv_log **−2.03**

These are not wrong-game (pick and close share the league) and there is no
evidence to re-null them — but CLV magnitudes past −1.0 warrant a look for a
fill/close-book mismatch or a bad exchange close on that fixture. Left as-is
pending investigation; they are consensus/exchange-anchored context, and the
|clv_log|>0.5 fabrication fallback already governs their trusted-subset
membership.
