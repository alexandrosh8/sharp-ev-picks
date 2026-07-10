# CLV close-freshness shadow study — stored close vs the Pinnacle ARCADIA archive close

**Date:** 2026-07-10 (UTC) · **Status:** SHADOW ONLY — `CLV_USE_PINNACLE_ARCHIVE`
remains **OFF**; the operator signs any flip. Nothing here changed config or code
paths.

**Instrument:** `scripts/research/clv_close_freshness_study.py` (read-only:
SELECT-only session, rolled back; the resolve path's best-effort observability
writes are monkeypatched to no-ops for the run — `event_source_links` /
`match_review_queue` counts verified unchanged, 746/153 before and after).
It consumes the REAL flag-gated machinery — `app.clv_trueup._pinnacle_archive_close`
→ `app.storage.repositories.resolve_pinnacle_close_snaps` (strict hardened
matcher, marker-safe slug fallback, per-market selection re-key, in-play cutoff
at the arcadia event's own kickoff) — and re-derives the close fair through the
same chokepoints `finalize_closing_from_snapshots` uses
(`group_market_prices` → `_settleable_groups` → `_merge_vocabulary_groups` →
`event_fair_probs`, devig=`power` from live Settings, same `value_policy`,
effective-odds fill netting, exact-`market_detail` matching with fail-closed
line-blind ambiguity refusal). Trust gates are the imported production guards
(`_clv_row_is_tautological`, `_clv_row_is_fabricated`,
`_devig_fallback_asymmetric`, `_SHARP_CLOSE_ANCHORS`, independence). The
stored-side trusted-subset replication was cross-checked against direct SQL:
**n=215, mean clv_log −0.03482 — exact match**.

Population: **1,092 settled picks** (all of `picks JOIN result_tracking`;
0 superseded, 0 kickoff-unknown). Run: 2026-07-10T22:29Z.

## (a) Match rate — arcadia archive vs settled picks

| | matched archive event | USABLE close (anchorable fair + CLV) |
|---|---|---|
| **overall** | 849/1092 (77.7%) | **292/1092 (26.7%)** |
| soccer | 351/538 (65.2%) | 113/538 (21.0%) |
| basketball | 366/416 (88.0%) | 131/416 (31.5%) |
| tennis | 132/138 (95.7%) | 48/138 (34.8%) |
| h2h | 143/210 (68.1%) | **127/210 (60.5%)** |
| spreads | 414/466 (88.8%) | 123/466 (26.4%) |
| totals | 155/194 (79.9%) | 42/194 (21.6%) |
| double_chance | 86/152 (56.6%) | 0/152 (0.0%) |
| btts | 51/70 (72.9%) | 0/70 (0.0%) |
| tier=volume | 819/1049 (78.1%) | 282/1049 (26.9%) |
| tier=premium | 30/43 (69.8%) | 10/43 (23.3%) |

Refusal funnel: 243 `no_archive_match` (fixture not matched/covered),
557 `no_anchorable_fair` (matched, but the archive does not price the pick's
market/line, or the selection re-key dropped it). The gap between "matched" and
"usable" is **structural, not a matcher defect**: `resolve_pinnacle_close_snaps`
maps only h2h/totals/spreads vocabularies (double_chance and btts are dropped by
design — Pinnacle's arcadia namespace does not carry them in a mappable form),
spreads lines frequently differ from the pick's line, and the known
integer-totals vocabulary mismatch (L-arcadia-300, `totals_3_0` vs `totals_3`)
suppresses part of the totals bucket. h2h at 60.5% usable is the honest ceiling
signal for the markets a sharp close actually exists for.

## (b) Close-age delta (minutes before kickoff)

Paired subset (both a stored `close_snapshot_captured_at` and an arcadia close;
n=127):

| close source | p25 | median | p75 |
|---|---|---|---|
| current stored close | 18m | **191m** | 711m |
| arcadia close | 10m | **27m** | 478m |

All-rows view: stored close age n=460 median 184m [12, 570]; arcadia close age
n=292 median 27m [10, 553]. This confirms the audit's framing: the platform's
"close" is mostly a T-3h price where the archive holds a T-27m (median) row —
a **~7× freshness improvement at the median** on the directly comparable subset.
(The p75 tail stays high on both sides: change-only persistence means an
unmoved line's last capture can legitimately be old.)

## (c) CLV delta under the trust gates

- **Trusted subset today (stored close): n=215, mean clv_log −0.03482 ± 0.02067 SE**
  (unweighted per-pick mean; prod's headline is stake-weighted — same sign and
  magnitude class).
- **Paired subset** (trusted today AND arcadia-close trusted): n=135
  - mean CLV stored close: −0.01491 · mean CLV arcadia close: −0.01247
  - **per-pick delta (arc − stored): +0.00244 ± 0.00952 SE** — CI spans 0.
  - beat-close sign flips: 6 (+→−), 4 (−→+) of 135.
- **Hypothetical trusted set with the flag ON** (arcadia close where usable and
  clean, stored trusted close otherwise): **n=296 (216 arcadia-closed, 80 kept
  stored), mean −0.02711 ± 0.01616 SE**.
- **81 rows newly enter the trusted subset** (soccer 41, basketball 27,
  tennis 13) — mostly picks whose stored close was consensus-anchored or a
  poll-time fallback and therefore untrusted today.

**Honest reading:** the stale close is NOT materially biasing trusted CLV where
both closes exist — the paired delta is +0.002 with an SE four times larger.
What the flag actually buys is (i) a ~38% larger trusted evidence base
(215→296) with (ii) a dramatically fresher close and (iii) a cleaner guard
profile (below). It does not rescue the negative trusted CLV: −0.035 → −0.027,
both CIs still overlap materially and both point estimates remain negative.
Nobody should flip this flag expecting CLV to turn positive; the case for it is
measurement quality and evidence mass, not a better-looking headline.

## (d) Guard checks (M349 design inputs)

On the 292 rows with an arcadia fair:

| guard | arcadia close | stored close (all 1,092 rows, reference) |
|---|---|---|
| fabricated POSITIVE (close-implied edge > +0.20) | **1** | 13 |
| implausible NEGATIVE (edge < −0.20) — M349 symmetric counter | **1** | 3 |
| \|clv_log\| > 0.5 magnitude bound | 11 | — |
| tautological vs pick-time fair (ε=1e-3) | 75 (25.7% of 292) | 393 (36.0% of 1,092) |
| devig-fallback asymmetric (mint vs arc) | 0 | — |

The arcadia close is structurally cleaner: 1 fabricated vs 13, and the
symmetric implausible-negative counter registers exactly 1 row (consistent with
the audit's "M349 impact today: 1 trusted-gate row"). The 11 rows past the
|clv_log|>0.5 magnitude bound all have BOTH real inputs present, so per the
production guard's design the edge test governs and they are NOT excluded —
they are genuine large line moves, which a fresh sharp close surfaces more
often than a stale one. The tautology rate falls 36%→26% because a T-27m
Pinnacle close has moved from the mint line more often than a T-3h echo.

## DRAFT operator flip criterion for `CLV_USE_PINNACLE_ARCHIVE` (pre-registered)

The flag stays OFF until the operator signs. Proposed criterion, with today's
measured values in brackets — **thresholds fixed now, before any further data
is collected**, so the flip decision cannot be tuned on the outcome:

1. **Match-rate floor (scoped):** arcadia usable-close rate ≥ 50% on the
   markets the archive can price for a settled pick — h2h [60.5% ✅]; report
   spreads/totals but do NOT gate on them (line-mismatch is structural).
   Overall usable rate ≥ 25% [26.7% ✅].
2. **No false merges:** 0 wrong-game attachments in a manual audit of ≥ 30
   randomly sampled matched picks (mirror of the 61.3%/0-false-merge go-live
   audit for the pick-time anchor) [NOT YET RUN — required before signing].
3. **Delta bound (no silent re-grade):** |paired trusted-CLV delta| ≤ 0.02
   absolute, or its 95% CI includes 0 [+0.00244 ± 0.00952, CI includes 0 ✅] —
   i.e. the archive close changes measurement provenance, not the verdict.
4. **Guard conditions:** under the arcadia close, fabricated-positive rate
   ≤ 1% [1/292 = 0.3% ✅] AND implausible-negative (M349 symmetric) rate ≤ 1%
   [1/292 = 0.3% ✅], with the implausible-negative counter REPORTED separately
   (never silently excluded) per the M349 design.
5. **Freshness gain is real:** median arcadia close age < half the median
   stored close age on the paired subset [27m vs 191m ✅].
6. **Rollout shape:** flip is settlement-path-only (no mint change), plus the
   D2 sharp-close-echo gate stays ON; re-run this script one week after the
   flip and confirm (3) and (4) still hold on the new rows; rollback = flip
   the flag back (no data migration — closes are recomputed at settlement).

Open item blocking signature: criterion (2) manual wrong-game audit, and the
operator's acceptance that the trusted aggregate will be restated from
n=215/−0.0348 to ~n=296/−0.0271 (a definition change that must be annotated on
the dashboard the day of the flip, not slipped in).

## Riders (read-only verification, no code changes)

### Rider 1 — read-side raw-vs-effective odds alignment (`app/storage/repositories.py:1256`)

Verified: `_clv_row_is_fabricated` (repositories.py:1236, the read-side guard)
computes the close-implied edge as `closing_fair_probability − 1/decimal_odds`
using the **raw** fill odds (line 1257), while the write-side gate
(`finalize_closing_from_snapshots`, app/clv_trueup.py:1439) tests
`fair − 1/fill_eff` with **commission-netted effective** odds — the same
convention `clv_log` itself uses. For exchange fills (effective < raw ⇒
1/eff > 1/raw) the read-side edge reads LARGER than the write-side edge, so the
read guard is strictly more trigger-happy on exchange picks: a close the writer
accepted can in principle be flagged fabricated at read time. Measured impact
today: 35 settled exchange-fill picks (29 with a close), maximum raw-vs-effective
implied-probability window 0.0128 (Betfair 5% commission), and **0 rows whose
guard verdict actually differs** under the two conventions. Conclusion: real but
currently inert inconsistency, conservative in direction (over-exclusion, never
fake-CLV admission). Recommended disposition: align the read-side guard to
`effective_odds(bookmaker, decimal_odds)` the next time repositories.py's guard
block is touched — it needs the bookmaker (already in the row) threaded into
`_clv_row_is_fabricated`; no urgency and no shadow risk.

### Rider 2 — per-source sharp-close freshness gate (M-clv-1338)

Verified against this study's data: `finalize_closing_from_snapshots` injects
sharp-archive rows whenever EITHER the soft scrape OR the sharp source is fresh
within `SNAPSHOT_CLOSE_MAX_GAP` (4h), and `event_fair_probs` then prefers the
sharp anchor regardless of the per-source age within that window — so a 3.9h-old
sharp row outranks a T-10m soft consensus (the skill's documented gotcha;
Betfair-anchored stored closes run mean ~5.5h, median ~2.3h in today's data:
`closing_anchor_type='sharp'` mean age 329m, `pinnacle` 491m, `consensus` 362m).
The pick-time anchor loader already solved this exact problem with a per-source
freshness clock (`_fresh_source` in `build_sharp_anchor_loader`,
app/clv_trueup.py:1693 — each source keeps its rows only if ITS OWN newest row
is fresh). The natural design is to apply the same per-source test to the
settlement injection: gate each injected sharp SOURCE (pinnacle archive,
Betfair) on its own last-capture age vs a per-source bound, falling back to the
fresh soft consensus with honest `closing_anchor_type='consensus'` — a
tightening-only change, structurally identical to the existing D2 echo gate.
Caveat this study adds: with arcadia's capture healthy (median T-13m event
clock, T-27m anchor rows here) the pinnacle path would rarely be gated, but the
Betfair injection path (dedicated capture, historically 4-5h stale) is exactly
what the gate exists for. Changes CLV semantics ⇒ per the ledger it stays in
the shadow-first work package with pre-registered review; this script can serve
as the shadow harness (add a `--max-sharp-close-age` parameter and re-report
(b)/(c) under the gate) before any code change.

## Reproduction

```
uv run python scripts/research/clv_close_freshness_study.py            # full
uv run python scripts/research/clv_close_freshness_study.py --limit 50 # smoke
```

(Read-only; needs the compose postgres reachable via `DATABASE_URL`.)
