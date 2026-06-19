# ADR-0015: Read-only Betfair Exchange BACK-odds capture from OddsPortal (isolated, off-by-default)

- **Status:** accepted
- **Date:** 2026-06-19
- **Deciders:** GodFather (Alexis) — "Build a read-only Betfair Exchange odds
  capture from OddsPortal … MIRRORING the existing ARCADIA pattern exactly … a
  separate independent capture that mints NO picks, stores under an isolated
  sharp namespace, off-by-default, change-gated."

## Context

`app/edge/value.py` already lists `"betfair exchange"` in `SHARP_BOOKS` with a
5% `EXCHANGE_COMMISSION`: the exchange BACK price is a commission-netted sharp
anchor the line-shopping strategy can use, the same role Pinnacle plays
(ADR-0013). But our default OddsHarvester scrape never captures it:
OddsHarvester's `parse_market_odds` scopes to the MAIN bookmaker table and
skips the Betfair row with "Incomplete odds data for bookmaker" because the
exchange row's BACK/LAY/liquidity cells do not match `ODDS_BLOCK_CLASS_PATTERN`
(we currently SUPPRESS that warning via `_ExchangeIncompleteOddsFilter` in
`app/ingestion/oddsportal.py`).

A live UK-proxy probe (2026-06-19) confirmed OddsPortal DOES serve a Betfair
Exchange row on a match page, in a dedicated section, on liquidity-rich (major)
matches and ABSENT on obscure ones (liquidity-gated coverage).

## Decision

Build `app/ingestion/betfair_exchange.py` — a dedicated, ISOLATED, read-only
reader of that row — replicating the arcadia archive's SHAPE exactly:

- **Independent capture job, never an `ODDS_SOURCE`.** `build_scheduler`
  registers a separate `IntervalTrigger` job gated by `BETFAIR_EXCHANGE_ENABLED`
  (default **OFF** — unlike arcadia, because each read spends a full browser
  page-load per match, so it stays opt-in until its target slate is scoped). It
  runs ALONGSIDE the active source and mints **no picks/alerts**.
- **Isolated `betfair_<sport>` warehouse namespace.** BACK observations persist
  via the normal `persist_odds_snapshots` path with `bookmaker="Betfair Exchange"`
  under sport keys `betfair_soccer` (etc.). Crucially, each captured event's
  `external_ref` is **namespaced with a `betfair:` prefix**
  (`_namespace_event_ref`): events are keyed by `external_ref` ALONE (globally
  unique, NOT sport-scoped), so without the prefix `persist_odds_snapshots`
  would reuse the live soccer Event row and the Betfair BACK price (a
  SHARP_BOOK) would leak into that event's closing-CLV anchor — caught and
  fixed in adversarial review. The `betfair:` events are therefore disjoint
  from live events sharing the same OddsPortal match URL, exactly as
  `pinnacle_<sport>` (keyed by Pinnacle's numeric id) is. AVAILABLE GAMES
  filters to `soccer`/`basketball`/`tennis` only, so the archive **never
  pollutes** the dashboard or pick path. A later cross-source resolution (by
  team names) bridges the `betfair:` event to the live event when validated.
- **Change-gated in memory** on the per-`(sport, event, selection)` BACK
  decimal price (mirrors arcadia's version-gate intent): one row per genuine
  reprice; the latest pre-kickoff row IS that selection's exchange close, picked
  up by `closing_odds_from_snapshots`.
- **Liquidity gate.** A BACK outcome whose backable £ liquidity is below
  `BETFAIR_EXCHANGE_MIN_LIQUIDITY` (default £500) is SKIPPED — thin exchange
  markets give unreliable prices. A row with no outcome clearing the floor
  yields nothing for that event (a silent-empty is impossible: nothing is
  invented, the event is simply absent that cycle).
- **v1 scope = football 1X2 (home/draw/away) BACK only.** The fractional→decimal
  conversion and BACK-before-LAY ordering are written to generalise (a 2-way
  `outcomes=("home","away")` path is already tested for tennis), but tennis and
  others stay out until their row is probed.
- **Reader sources its targets from the existing scrape.** The scheduler's
  `targets_fn` re-reads the SAME fixtures the last OddsPortal scrape discovered
  (`loader.last_fetch_event_ids` + `directory`); the module owns no
  listing/scheduling policy of its own.

### DOM contract confirmed live (2026-06-19, UK proxy)

On football match pages (e.g. `.../mexico-O6iHcNkd/south-korea-K6Gs7P6G/`):

| Selector                                      | Role                                                       |
| --------------------------------------------- | ---------------------------------------------------------- |
| `[data-testid="betting-exchanges-section"]`   | the exchange section wrapper                               |
| `[data-testid="betting-exchanges-table-row"]` | one exchange book row                                      |
| `img[alt="Betfair Exchange"]`                 | identifies the Betfair row (logo `/serve/bookmaker/44/`)   |
| `[data-testid="back-lay-text"]`               | the row's "Back" / "Lay" column header (Back precedes Lay) |
| `[data-testid="odd-container"]`               | the per-outcome BACK/LAY price cells                       |

Per the user's verified row, the cells render fractional BACK prices then
fractional LAY prices, each followed by a parenthesised £ liquidity, e.g.
`28/25 (9052) 5/2 (3307) 3/1 (1307)` (BACK home/draw/away) then
`57/50 (11317) 51/20 (41) 31/10 (2683)` (LAY). Fractional→decimal is
`num/den + 1` (28/25→2.12, 5/2→3.5, 3/1→4.0). The BACK side is the FIRST
`len(outcomes)` odds+liquidity pairs in DOM order; the LAY tail is ignored.

### Honest caveat — render fragility (2026-06-19)

On the specific matches probed in HEADLESS Chromium through our proxies, the
Betfair row's `odd-container` cells were intermittently **empty** of price text:
the row carried the bookmaker logo, the "Back"/"Lay" header, and a "CLAIM BONUS"
promotional overlay ("Bet £20 …"), but the fractional odds did not always
populate. This is the same render-fragility class as the rest of the OddsPortal
scrape — the exchange prices lazy-render and a headless/proxied load may catch
the row before they hydrate. The parser therefore treats an empty/odds-less row
as an EXPECTED gap (returns no quotes), never an error, and the capture is
off-by-default until live cycles show the hit-rate is worth the page-load cost.
The selectors above are the durable contract; the price hydration is the
fragile part, and gaps are expected (never bypass anti-bot to force them).

## Safety

GET-only PUBLIC market data — **no account, no login, no stored credentials, no
betslip, no order placement, no anti-bot bypass** (ADR-0002). The page is loaded
exactly the way `app/ingestion/oddsportal.py` already loads OddsPortal (headless
browser through an optional read-only proxy whose credentials travel as separate
Playwright fields, never in the URL or logs). There is deliberately **no Betfair
API/login/session/order path and no `BETFAIR_*` credential slot**. Playwright is
obtained via `importlib` and used for read-only page loads only, so
`scripts/safety_audit.sh` (which greps `app/` for `import playwright` /
`from playwright` and for bet-placement identifiers) stays green. Errors carry
the exception TYPE only — never the URL or proxy creds.

## Consequences

- **The archive accumulates a free Betfair Exchange BACK close** for major
  matches — a second commission-netted sharp anchor alongside Pinnacle, the
  irreplaceable line-shopping asset.
- **This is NOT instant validation.** Like arcadia, it is the data ENABLER.
  v1 mints NOTHING; turning it into edges needs (1) the value pipeline to read
  `betfair_<sport>` snapshots as a sharp anchor and (2) live CLV evidence.
- ToS-grey + DOM/render-fragile (same class as the OddsPortal scrape); treat
  gaps as expected, never bypass anti-bot protections.
- v1 captures football 1X2 BACK only; tennis (2-way) / totals / spreads are a
  later add once their exchange rows are probed.
