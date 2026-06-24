# Web-Scraping & Source-Analysis Tooling — Ranked (2026-06-23)

Decision-support only. Two sourced research briefs (deep-research-agent x2). For improving how we fetch + parse OddsPortal (JS-rendered) and the Pinnacle JSON API. Respects the project "never bypass anti-bot" rule — evasion tools are presented factually, gated behind a ToS/ADR decision, never a default.

## TL;DR — two clear wins, in-policy
1. **`curl_cffi` for JSON endpoints (do first).** Drop-in `requests`/`httpx`-style client that impersonates a real browser's **TLS/JA3 + HTTP/2 fingerprint** — which is exactly what 2026 Cloudflare flags plain `httpx` on. **Pinnacle's arcadia JSON API is the textbook fit** (GET-only, no render). Near-drop-in robustness upgrade, keeps async, MIT, actively maintained (v0.15.0). Arguably NOT a "bypass" — it's hardening a legitimate read we already do.
2. **Intercept the JSON the browser fetches (the architectural fix for OddsPortal).** Instead of parsing the fragile rendered DOM, use Playwright `page.on("response")` to capture the XHR/JSON OddsPortal's own JS loads, then parse it. **Kills the period-race / "can't find tab" / cache-busting-AJAX bugs at the source** — same robustness as our Pinnacle JSON path. (OddsPortal's raw `.dat` feed needs a `secondary_id`/`xhash` token that **rotates every few minutes**, so pure httpx-replay is brittle → *intercept-in-browser*, don't raw-replay.)

## Ranked tools

### HTML parsers
| Rank | Tool | Why | Note |
|---|---|---|---|
| 1 | **parsel** | CSS **+ XPath** on libxml2 — XPath axes ("cell after header 'Over'") survive class-name churn that breaks CSS | Scrapy's parser; replace BeautifulSoup |
| 2 | selectolax[lexbor] | ~4x faster (Lexbor) | CSS only; use only if parsing is hot (it isn't — render dominates) |
| 3 | lxml | XPath + XSLT | baseline |
| x | BeautifulSoup (current) | slowest (~24-60x Lexbor); no XPath | migrate off |

### JSON extraction (OddsPortal embeds `JSON.parse('...')`)
- **chompjs** — parses JS-object literals (single quotes, trailing commas, unquoted keys) that `json.loads` chokes on; `parse_js_object(..., unicode_escape=True)`. Endorsed by Scrapy + Zyte. **This is our OddsPortal pattern.**
- **jmespath** (clean queries) then **jsonpath-ng** (recursive `$..odds`) for querying once parsed.

### API / XHR endpoint discovery (the key capability)
| Rank | Tool | For |
|---|---|---|
| 1 | **Playwright interception** (`page.on("response")`, `record_har_path`, `storage_state()`) | discover + replay in one Python pipeline; harvest cookies for httpx |
| 2 | Chrome DevTools (Fetch/XHR, Copy-as-cURL, Save-HAR) | fast first look |
| 3 | mitmproxy (+ addons) | system-wide / non-browser traffic |
| 4 | mitmproxy2swagger | turn captured flows into an OpenAPI spec of the undocumented API |

### Scrape-breakage detection (we feed +EV picks — silent empties are dangerous)
- **changedetection.io** (self-host, ~32k stars) — watches a CSS/XPath/JSONPath selector, **alerts Telegram/webhook** when it stops matching (maps onto our channels).
- **@medv/finder** — generates stable selectors (filters volatile Tailwind/CSS-in-JS classes).
- **Fail-closed structural gate** (`rows>0 and cols>0`) + **validate payloads, not status codes** — 2026 Cloudflare serves fake AI honeypot pages on `200 OK`.

## Browser / anti-bot (GATED — needs a ToS-waiver/ADR; not a default)
Independent benchmark (Paterson 2026, 7 tools x 31 CF targets): **our vanilla Playwright Chromium is the *most-blocked* Chromium tool** (leaks `Runtime.enable`, `navigator.webdriver`, HeadlessChrome UA).
- **nodriver** — benchmark winner (28/31, 0 blocked) but **AGPL-3.0** (network-use clause = real legal gate) + a rewrite (not Playwright API).
- **Patchright** — drop-in Playwright stealth replacement (Apache-2.0, zero rewrite, modest CF gain) — the low-cost option *if* we ever waive the rule.
- **Camoufox** — anti-detect Firefox (MPL-2.0); mid-pack on CF; only if a Firefox shape helps.
- **TLS clients CANNOT solve an active JS challenge** — they only help fingerprint-gated endpoints (hence curl_cffi for JSON, Playwright for challenge pages).

## REJECT (abandoned / ineffective in 2026)
FlareSolverr (stale May 2024), cloudscraper (dead Apr 2023), undetected-chromedriver (frozen Feb 2024, superseded by nodriver), tls-client py wrapper (stale; use `noble-tls`), autoscraper (stale 2022), botasaurus (unproven).

## Recommended action plan for betting-ai
1. **Adopt `curl_cffi`** for GET-only JSON sources — **Pinnacle arcadia first** (robustness + speed, in-policy). Lowest-risk highest-leverage change.
2. **Pilot Playwright network interception** in `app/ingestion/oddsportal.py` — capture the odds/Betfair JSON the page fetches; parse with **chompjs** + **jmespath**. Retires the DOM-fragility bug class.
3. **Migrate BeautifulSoup -> parsel** (XPath resilience) for the remaining DOM parse.
4. **Add changedetection.io + a fail-closed structural gate** so scrape breakage alerts instead of silently emptying (consistent with "treat scrape gaps as expected").
5. Anti-bot browsers (nodriver/Patchright/Camoufox) = a **gated waiver+AGPL-review decision**, not a default.

## Honesty flags (from the research)
"OddsPortal runs no Cloudflare" is a single-vendor claim; OddsPortal's exact inline JSON var name + `.dat` URL scheme are from single community repos — verify before relying. nodriver-beats-Camoufox rests on one benchmark (one IP, one night).
