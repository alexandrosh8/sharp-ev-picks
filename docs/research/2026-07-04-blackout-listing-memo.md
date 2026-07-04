# OddsPortal listing blackout — 1a verdict + 1c volume memo (2026-07-04)

Numbers-first, to inform (not make) the operator's residential-proxy decision.
See also the memory entry `oddsportal-listing-blackout-2026-07-04` and the
recovery-watch shipped this session (`evaluate_listing_recovery`, self_audit).

## 1a — Is it a code issue? No. Upstream/ASN-keyed, code path sound.

Evidence (continuous production telemetry — the live listing IS the probe, run
every cycle; not a one-off isolated scrape):

- The listing scrape **executes cleanly every cycle** and logs
  `oddsportal scrape via proxy #N listed 0 matches — empty slate`. A code/fetch
  failure would raise and log an exception; instead the fetch returns HTTP-OK
  with an **empty** fixture payload. Empty data, not a broken fetcher.
- **The same proxy pool works for every other OddsPortal endpoint**: per-match
  feeds and ARCADIA both wrote rows through it in the same window (soccer 3,417 /
  basketball 856 / tennis 336 / NFL 48 ARCADIA rows in the last 2 h; occasional
  per-match soccer rows on already-known events). Egress, TLS impersonation, and
  proxy auth are all healthy — only fixture **discovery** returns empty.
- Consistent with the prior rendered+feed diagnosis: `ajax-nextgames*` decrypts
  to `nullResultText` "no matches" and popular-leagues returns `rows: []` to our
  datacenter ASN, while match pages + bootstrap tokens serve normally.

Verdict: **not fixable in-repo**; camoufox/stealth is contraindicated (a real
stealth Chromium already receives the empty payload — the discriminator is
network origin, not fingerprint). Levers unchanged: residential/mobile egress
for the listing call, or wait for a possible upstream revert (now auto-detected).

## 1c — Listing-call volume (for proxy sizing)

| Quantity | Value | Source |
|---|---|---|
| Poll cycle interval | 300 s | `poll_interval_seconds` default, `app/config.py:747` |
| Cycles per day | 288 | 86400 / 300 |
| Sports listed per cycle | 4 | soccer, basketball, tennis, american_football |
| **Healthy listing calls/cycle** | **~4** | 1 per sport (no proxy-rotation retries) |
| **Healthy listing calls/day** | **~1,150** | 4 × 288 |
| Blackout-inflated listing lines/hour | ~385 | log count; proxy-rotation retries on empty inflate this — a blackout artifact, not steady-state |
| Blackout duration so far | ~7 h (from ~12:40 UTC) | monitoring log |

**Residential lever sizing (the key number):** the memory-documented lever routes
**only the listing call** through residential egress; the heavy per-match fan-out
stays on the datacenter pool (match pages still serve). That is **~1 listing
request per sport per cycle → ~1,150 small JSON requests/day → ~34,500/month**
at the current 4-sport, 300 s cadence. If a single combined listing endpoint
replaces per-sport calls, it drops to ~288/day (~8,600/month).

## Pricing (NOT verified here — needs a live quote at this volume)

Per the evidence-grounding rule, no vendor price is asserted. What the operator
needs to price: **~8,600–34,500 residential requests/month of small (~sub-100 KB)
JSON**, listing-only. This is a **low-tier** residential/mobile plan (most
providers bill per-GB or per-request; this volume is a few GB/month at most).
Get a quote at that tier before deciding; do not size for the full scrape volume
(the per-match fan-out does not need residential egress).

## Status

Fail-closed has held for the full ~7 h — zero stale picks minted. Recovery is now
**auto-detected**: `evaluate_listing_recovery` fires a one-shot WARN the moment a
cycle lists > 0 matches after ≥ 3 dark cycles (self_audit, no extra requests).
No manual monitoring loop required going forward.
