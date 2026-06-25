# OddsPortal JSON Migration — Overnight Handoff (2026-06-24 ~01:35 UTC)

## TL;DR
Deployed the curl_cffi JSON-feed migration, it **did not deliver**, **rolled it back**. **Prod is SAFE** on the proven Playwright path (`ODDSPORTAL_USE_JSON_FEED=false`), healthy, soft-book odds flowing. The JSON ingester code is sound; the **wiring** has one architectural gap to fix (below). Do NOT re-enable the flag until that's fixed.

## What works (keep)
- Branch `feat/oddsportal-json-ingester` (`f4f4366`): the curl_cffi ingester `app/ingestion/oddsportal_json.py` — fetch feed, **AES-256-CBC decrypt** (static `app.js` key), parse, **bookmaker ID->NAME** mapping, version-guard, no-fallback. **20 tests pass; full suite green (974).** The MD5 feed-URL hash turned out **moot** (OddsPortal doesn't validate the 32-hex segment) — pure-Python `hashlib.md5`, no browser.

## Why it was rolled back (the gap to fix)
Enabling `ODDSPORTAL_USE_JSON_FEED=true` produced **no soft-book odds** (only Pinnacle arcadia flowed) and **no speed win** (`poll_odds` still didn't complete). Logs showed OddsHarvester **still Playwright-loading every match page** (`Scraping match ...`, `match_details extracted dom=[...]`, `SelectionManager`).

**Root cause:** the wire used `markets=[]` to make OddsHarvester skip per-market *tab navigation* — but OddsHarvester **still opens each match page (Playwright render) to read the header (team context)**. So:
1. The expensive **page-load remains** -> no CPU/speed savings.
2. The per-match JSON odds did not reach persistence as soft books (only arcadia Pinnacle was written; `poll_odds` never completed a cycle to flush, and the path is still entangled with the Playwright per-match flow).

## The fix needed (morning, supervised — NOT a safe overnight change)
**Bypass OddsHarvester's per-match flow entirely when JSON is on.** Get match URLs + team context (home/away/league/kickoff) from the **dated LISTING page** (it already carries them — no per-match page needed), then fetch odds ONLY via `oddsportal_json.scrape_match_odds()`. Concretely:
1. Make the listing yield `(url, EventTeams)` **without** opening each match page (use the listing DOM's team names, or OddsHarvester's link-extraction step alone).
2. Confirm the JSON per-match odds actually reach `persist_odds_snapshots` with soft-book NAMES (the verifier saw only `Pinnacle` — prove a soft book like `bet365` lands).
3. Re-verify: `poll_odds` completes FAST, soft books flow, `bookmaker ~ '^[0-9]+$'` count = 0, CPU drops from ~load-8.

## Current safe state
- Prod: Playwright path, `health=healthy`, soft books flowing (the pre-migration baseline — functional but the chronic slow `poll_odds`, which is the *original* CPU problem the migration was meant to fix).
- Rollback image: `betting-ai-app:rollback-pre-migration`. Re-enable JSON only via the flag AFTER the fix above.
- Overnight monitor cron `acc61e0e` (every ~20 min) watches health + soft-book flow + auto-fixes any NEW (non-migration) issue, else keeps prod on the safe path.

## Also pending
- penaltyblog model bake-off (workflow `wf_b1c8836f`) — empirical "is Dixon-Coles best on CLV" verdict, still running; result will be reported.
- NBA features: parked (no free pre-match odds to backtest against; operator declined paid data).
