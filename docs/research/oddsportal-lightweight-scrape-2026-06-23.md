# Lightweight (non-browser) OddsPortal scraping — repo evaluation (2026-06-23)

**Question:** Find proven repos that scrape OddsPortal via the `fb.oddsportal.com`
JSON `.dat` feed using `requests`/`httpx`/`curl_cffi` (NOT Playwright/Selenium
browser-render), to guide migration off the vendored OddsHarvester full-Chromium
path (~570 renders/pass, load 8, 45 min/pass).

Companion to `scraping-tooling-2026-06-23.md` (which ranks LIBRARIES). This file
ranks the candidate REPOS and supplies the NEW encrypted-feed decrypt recipe.

## Headline finding (HONEST)

**No maintained repo reliably solves the CURRENT (2024+) rotating-token JSON-feed.**
- The exact feed+token technique is real and documented, but the public repos
  that implement it (jckkrr 2023, borewicz 2016) target the **OLD plaintext feed**
  (`fb.oddsportal.com/feed/match/...dat` returning readable JSON).
- Around **late 2024 OddsPortal changed the feed**: endpoint moved to
  `www.oddsportal.com/feed/match-event/{ver}-{sport}-{id}-1-2-{xhash}.dat` and the
  body is now **base64 + AES-256-CBC encrypted**, key derived via PBKDF2-HMAC-SHA256
  (1000 iters) from a `password,salt` pair embedded in `res/public/js/build/app.js`.
  The only public working recipe is a **StackOverflow answer (Dec 2024)** — NOT a repo.
- Every actively-maintained (2025-2026) OddsPortal repo is **browser-based**
  (Playwright or Selenium): OddsHarvester, Mg30 (Node), djibril-marega, golden-mane-labs.

**Recommendation:** (b) Learn the feed+token+decrypt technique — do NOT adopt any
repo as a dependency. Build our own httpx/curl_cffi client from:
1. jckkrr — token-from-HTML extraction + archive/xeid enumeration (pattern).
2. codereview.stackexchange "requests only" — clean archive-listing pattern.
3. StackOverflow #79241543 — the AES-CBC/PBKDF2 decrypt for the NEW feed (the
   missing piece none of the repos have).

## Scoring table

| Repo | Odds-source method | Token handling | Anti-bot | Last commit | License | Stars | Verdict |
|---|---|---|---|---|---|---|---|
| **jckkrr/Unlayering_Oddsportal** | `requests`+bs4 → OLD plaintext `fb.oddsportal.com/feed/match/...dat`; JSON parsed direct | `xhash` regex'd from `PageEvent(...)` script in match HTML, `urllib.parse.unquote`. Re-fetches HTML each match (token fresh) | None (bare UA header) | 2023-01-09 (abandoned) | **None** (unlicensed) | ~11 | **reference-only** — exact technique, ideas only (no license, plaintext-era) |
| **borewicz/oddsportal** | `requests` → OLD plaintext `fb.oddsportal.com/feed/match/` + `/feed/postmatchscore/` | `xhashf`/`xhash` regex from match HTML, manual `%XX`→chr unhash | None | 2016-03-19 (abandoned) | None | 18 | **reference-only** — confirms token derivation independently; ancient |
| **pretrehr/Sports-betting** | **Selenium + selenium-wire** (Pinnacle via seleniumwire, many FR books) | n/a (browser) | browser/fake-UA | (live-arb tool) | GPL-3.0 | high | **reject** — browser; live-arb autoplacement shape; GPL |
| **gingeleski/odds-portal-scraper** | **Selenium + Chromedriver** + bs4 (`driver.page_source`) | n/a (browser) | headless Chrome | 2026-05-11 | Unlicense | 128 | **reject** — browser-render (the thing we're leaving) |
| **BeatTheBookie/oddsportal_scraper** | **Selenium** (`webdriver.Chrome`, `page_source`) + requests_html | n/a (browser) | headless Chrome, proxy arg | 2025-07-16 | (GPL text) | low | **reject** — browser |
| **jordantete/OddsHarvester** (our vendored) | **Playwright Chromium** | n/a (browser) | Playwright + proxy/UA | 2026-06-22 (active) | MIT | 193 | **reject for "lighter"** — it IS the heavy path we're migrating off |
| **Mg30/odds-portal-scraper** | **Playwright** (Node) | n/a (browser) | Playwright + proxy | 2025-11-24 (active) | ISC | 19 | **reject** — browser, Node |
| **djibril-marega/oddsportal_scraper** | **Playwright** (has pytest suite) | n/a (browser) | Playwright | 2026-05-10 (active) | MIT | low | **reject** — browser (despite good tests) |
| **golden-mane-labs/Sports-Betting-Demo** | Playwright (demo) | n/a | Playwright | 2026-02-03 | MIT | 12 | **reject** — demo, browser |
| **AinaRazafinjato/value-bets-scraper** | Playwright + bs4 | n/a | Playwright | 2025-04 | MIT | 2 | **reject + SAFETY FLAG** — stores `ODDPORTAL_USERNAME/PASSWORD` (bookmaker-style login creds). Do NOT mirror this pattern (violates our no-credential rule) |

## Files inspected (with proof)

- `jckkrr/Unlayering_Oddsportal/ODDSPORTAL_DATAHARVESTER.py` — `getMatchData()`:
  regexes `PageEvent\((.*)\);var`, json-loads it, `unquote(g1jl['xhash'])`, builds
  `f'https://fb.oddsportal.com/feed/match/1-1-{match_id}-1-2-{unhashed}.dat'`,
  then `getIndividualMatchOdds()` json-parses `jl['d']['oddsdata']['back']['E-1-2-0-0-0']`.
  `getOverviewPages()` enumerates `fb.oddsportal.com/ajax-sport-country-tournament-archive/...`
  pulling `xeid` per match.
- `jckkrr/.../scrapingTools_v2.py` — `getSoup(url, extra_headers)`: bare `requests.get`
  with UA + caller-supplied `Referer`. No anti-bot, no TLS impersonation.
- `borewicz/oddsportal/match.py` — `unhash(xhash)` (`%XX`→chr) + `get_match()` builds
  same `fb.oddsportal.com/feed/match/` URL; scope_ids {2,3,4}=full/1st/2nd half.
- `pretrehr/Sports-betting/sportsbetting/bookmakers/pinnacle.py` — imports `seleniumwire`;
  `requirements.txt` has selenium, selenium-wire, chromedriver-autoinstaller, websockets.
- `gingeleski/.../full_scraper/oddsportal/scraper.py` — `from selenium import webdriver`,
  `webdriver.Chrome('./chromedriver/chromedriver', ...)`. `requirements.txt`: selenium==3.141.0.
- `BeatTheBookie/oddsportal_scraper.py` — `init_browser()` → `webdriver.Chrome`, `browser.page_source`.
- `jordantete/OddsHarvester/pyproject.toml` — `playwright>=1.57.0`.
- `Mg30/.../package.json` — `"playwright": "^1.52.0"`; README "Built with Playwright".
- `djibril-marega/oddsportal_scraper/requirements.txt` — `playwright==1.55.0`.

## The token + feed technique (for our migration)

1. **xeid enumeration (cheap GET):** `fb.oddsportal.com/ajax-sport-country-tournament-archive/{sid}/{season_page_id}/X0/1/{tz}/{page}/`
   returns JSON `{d:{html:...}}`; parse `xeid` per row. (`sid`/`season_page_id` from the
   results-page `PageTournament(...)` script.) — pure requests, no browser.
2. **token (cheap GET, per match, fresh each time):** GET the match HTML, regex the
   `PageEvent(...)`/`xhash` value, `urllib.parse.unquote` it. Token rotates every few
   minutes, so re-derive per match rather than cache. **No reverse-engineering needed —
   the token is handed to us in the page.**
3. **odds (cheap GET):** build `.../feed/match-event/{ver}-{sport}-{xeid}-1-2-{xhash}.dat`.
   - OLD: body was plaintext JSON (jckkrr/borewicz path).
   - **NEW (2024+): body is base64; split on `:` into `encrypted:key`, urlsafe-b64decode
     `encrypted`, hex-decode `key` (the IV), PBKDF2-HMAC-SHA256(password, salt, 1000, 32)
     → AES-256-CBC decrypt → trim to last `}` → json.** password+salt live in `app.js`
     (`h(r.data,` call site) and rotate when app.js is rebuilt — so we must scrape+cache
     them, with a fallback to re-extract on decrypt failure.

## Risks / honesty for the migration

- **Brittleness moved, not removed.** Browser DOM-fragility → app.js password rotation +
  feed-format fragility. The encrypted-feed recipe is community-reverse-engineered and
  can break on any OddsPortal app.js change. Keep a Playwright fallback path.
- **Cloudflare/TLS fingerprinting:** plain `requests` (jckkrr/borewicz) had none; in 2026
  OddsPortal sits behind anti-bot. Use **curl_cffi (TLS impersonation)** for both the HTML
  and feed GETs, plus the mandatory `Referer: https://www.oddsportal.com/` and
  `x-requested-with: XMLHttpRequest` headers (per the SO answer).
- **No autobet risk in any reference.** All read-only GETs; flag only AinaRazafinjato for
  the bookmaker-credential anti-pattern (do not copy).
- **Licenses:** jckkrr/borewicz unlicensed → ideas/clean-room only, no code lift. SO answers
  are CC BY-SA (attribute, fine for an internally-rewritten client).
