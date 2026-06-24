# Lifting the Sharp-Anchor Match-Rate (37% → higher) — Safely (2026-06-23)

Decision-support only. Two sourced research briefs (deep-research-agent + quant-sports-researcher). Goal: link MORE of the captured Pinnacle/Betfair archive events to canonical pick-events **without** reintroducing the wrong-game (men's-line-on-women's-pick) defect just fixed.

## Problem
Strict normalized-name match (+ 269 aliases, block on kickoff ±1d + sport, both home AND away) links **~37% soccer / ~46% basketball** of pick-events. The ~60% misses are **name-spelling variation, NOT missing coverage** (abbreviations, sponsor names, transliteration) → recoverable. Hard constraint: precision must stay ~perfect.

## The solution is a LAYERED pipeline, not one algorithm
Each recall lever is paired with an **independent** precision guard (Fellegi-Sunter: ANDing conditionally-independent signals drives false-merge → product of error rates).

```
STAGE 0  BLOCK    (sport, league, kickoff ±W)                     <- already have
STAGE 1  MARKER   (gender, age, squad) exact-match or both-absent <- have distinguishing_markers; extend
                  REJECT one-sided marker  (fixes fake-CLV by construction)
                  known-club whitelist FIRST (Young Boys, AFC, Castilla...)
STAGE 2  ALIAS    canonicalize via EXPANDED table (seed from datasets below)
STAGE 3  NORMALIZE NFKD + umlaut expand + drop FC/AFC noise        <- already have
STAGE 4  NAME     Jaro-Winkler on strip_markers(name)  (rapidfuzz)
                  EXCLUDE token_set_ratio / WRatio / partial_ratio / raw Levenshtein
                  disambiguating-token blocklist (United/City/Sociedad/Atletico...)
STAGE 5  DECIDE   ACCEPT JW>=0.92 AND token_sort_ratio>=90
                  REVIEW 0.84<=JW<0.92 -> ODDS CONFIRM (devig both, TVD<=tau, argmax agree)
                  REJECT otherwise
```

## Highest-ROI, lowest-risk: expand the alias table from FREE datasets
Nickname/abbreviation resolution is a **lookup problem, not similarity** ("Spurs"->Tottenham shares no chars/phonetics). Deterministic, auditable, monotonic (new alias only adds coverage).

| Rank | Dataset | Coverage | License | Risk |
|---|---|---|---|---|
| 1 | **openfootball/clubs** | ~2,710 clubs, top-5 Europe + lower, **men's**, reserves pre-separated | **CC0** | LOW |
| 2 | **pretrehr/Sports-betting `translation.json`** | 1,267 **odds-feed names** (football 791, basketball 209, +) — closest to our problem | **MIT** | MED (vet markers) |
| 3 | **withqwerty/reep `names.csv`** (`reep_t` rows only) | 2,159 team aliases + provider-ID columns | **CC0** | MED (exclude W/II/U rows) |
| 4 | **Wikidata** (QID + P31 + country + gender) | top-5 + all NBA; multilingual altLabels | **CC0** | HIGH — women's & men's share altLabel; gate on QID+gender |
| — | soccerdata `teamname_replacements.json` (Apache-2.0, men's-only by design — copy the *pattern*) | | | LOW |

Filter EVERY imported alias through the marker guard before insert. Add a hand map for reserves whose name != parent: Real Madrid B=**Castilla**, Sociedad B=**Sanse**, Bilbao B=**Bilbao Athletic**, Barca B=**Barca Atletic**, **Jong** Ajax/PSV/AZ.

## What NOT to do
- **No embeddings** for the accept path — anisotropic (random names ~0.95 cosine -> Man Utd/Man City collide) yet miss disjoint nicknames. Worst failure here = silent fake CLV. Offline suggestion-for-review only.
- **No `token_set_ratio`/`WRatio`/`partial_ratio`** — subset->100 merges Man Utd/Man City, Inter/AC Milan, Real Madrid/Sociedad. **Single biggest false-merge risk.**
- **No Splink/dedupe** — built for 15M+ records; overkill + less precision-controllable at our volume. `recordlinkage` is right-sized if a lib is wanted, but it's just the structured hand-rolled pipeline.

## No shared cross-source ID exists
Pinnacle (closed API 2025-07), OddsPortal (name + URL hash), Betfair (`selectionId` internal) emit **no shared external ID** (QID/Transfermarkt/Opta). The cross-provider registers (Wikidata/reep) link data providers, **zero bookmaker columns**. So matching is irreducibly name-based: fuzzy-match each source into a QID-keyed dictionary ONCE per team, persist it (esp. **Betfair selectionId**), reuse.

## Recommended build order (precision-first, all reuse existing stack)
1. **Extend the marker gate** (Stage 1) into the matcher itself (we have `distinguishing_markers` for the slug guard — generalize it + the known-club whitelist). Non-negotiable, goes first.
2. **Seed the alias table** from openfootball/clubs (CC0) + pretrehr (MIT), marker-filtered. Biggest safe recall lift, zero new code risk.
3. **Add Jaro-Winkler** (rapidfuzz) two-tier on stripped base names, with the disambiguating-token blocklist.
4. **Odds-vector confirm** for the danger band (devig both, TVD<=tau) — recovers ambiguous matches at ~0 false-merge.
- **Measure first:** one-time fuzzy pass of the OddsPortal corpus vs the seed -> the residual unmatched rate tells us whether the alias table or the blocking is the binding constraint.
- Property tests: one-sided marker => REJECT; every whitelisted club survives `strip_markers`; men's-league team with a "W" name => data alarm.

## Key sources
Cohen/Ravikumar/Fienberg KDD-2003 (JW best); NCES FCSM 2018 (JW 0.85->985TP/1FP); rapidfuzz docs (token_set_ratio subset trap); DoorDash (two-tier); Splink/MoJ (Fellegi-Sunter); Sportradar (gender/age_group IDs); soccerdata (manual alias table); Sage/arXiv (cross-book odds corr >=0.95); openfootball/clubs, pretrehr/Sports-betting, withqwerty/reep (CC0/MIT seeds).
