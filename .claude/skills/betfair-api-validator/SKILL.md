---
name: betfair-api-validator
description: "Doctrine for the read-only Betfair Exchange API integration (app/ingestion/betfair_api.py). Use when reviewing/extending the Betfair API client, shadow capture, promotion, compare semantics, or interpreting SHADOW/COMPARE logs and match rates."
allowed_tools:
  - Read
  - Grep
  - Glob
  - Bash
---

# Betfair API Validator

## Read-only allowlist doctrine (non-negotiable)

- `_ALLOWED_OPS = frozenset({listMarketCatalogue, listMarketBook})` (betfair_api.py:79);
  `_rpc` refuses any other op BEFORE login/HTTP (line 615). `scripts/safety_audit.sh` check 9
  greps for both the frozenset and the `not in _ALLOWED_OPS` refusal — keep those literal strings.
- The JSON-RPC data calls are POST **by protocol** but read-only by op. Do not "fix" them to GET;
  the guarantee is the allowlist + the absent order/account identifiers
  (`tests/test_betfair_api.py::test_no_order_or_account_methods_in_module`).
- Credentials: `.env` → `SecretStr` → memory only. Session token (ssoid) never touches disk.
  Errors carry op name + errorCode/HTTP status only — never URL, body, or token.

## Catalogue vs book

- `listMarketCatalogue` = identity: event/competition/`marketStartTime`/runners
  (sortPriority 1=home, 2=away; draw = selectionId 58805). No prices.
- `listMarketBook` (EX_BEST_OFFERS) = prices: best `availableToBack` (price, size£).
  `size` is AVAILABLE depth at best back — a liquidity proxy, NOT matched volume.
- Join via `join_match_odds`; a market missing home/away runners is skipped, never guessed.

## Rate budget

- listMarketBook weight cap: 200 points/request; EX_BEST_OFFERS ≈ 5/market →
  `_MARKET_BOOK_BATCH = 25` (~125 points). Exceeding returns TOO_MUCH_DATA.
- Cycle: `BETFAIR_API_POLL_INTERVAL_SECONDS` (300s default) × (1 catalogue + ⌈N/25⌉ book calls).
- Single dedicated outbound proxy (operator requirement); `keepAlive` refreshes the session,
  non-SUCCESS clears the token → re-login exactly once on expiry codes.

## Compare semantics (SHADOW/COMPARE logs)

- `delta = api_price − reference_price` per role (home/draw/away); reference = latest
  OddsPortal-sourced "betfair exchange" H2H rows role-mapped by team name (scheduler.py:1031-1068).
- `within_one_tick` uses the Betfair tick ladder at the COARSER (higher) price and returns
  None when either price is absent — an absent price never counts as agreement.
- `freshness_gap_seconds = api_captured_at − reference.captured_at`; live readings show
  gaps of 2-9 h and `api_fresher=100%` — the scrape anchor is structurally stale.
- Promotion (`VALUE_BETFAIR_API_PROMOTE`, default OFF) is evidence-gated on these logs;
  shadow rows carry `"betfair exchange (api-shadow)"` — deliberately NOT in `SHARP_BOOKS`.

## Gotchas

- **200-cap / 72h-window truncation.** `list_market_catalogue(max_results=200)` with
  `sort=FIRST_TO_START` over `BETFAIR_API_WINDOW_HOURS=72` silently drops everything past the
  200th market. When SHADOW logs show `fetched=200`, the window is SATURATED: match-rate,
  coverage, and comparison counts are all biased toward the nearest kickoffs. Check for
  saturation before reading any trend; shrink the window or page before widening scope.
- **Match rate ≈ 18.5% is a denominator artifact, not (only) a matcher problem.** The 200
  fetched markets include leagues the scrape never carries. Only trend it against a fixed
  window and alongside `event_source_links` confidence rows.
- **`league=None` in the hardened matcher is deliberate** (betfair_api.py:962-977): Betfair
  competition names never normalize-equal OddsPortal league names — passing them false-blocks
  every market. Name + tight kickoff window + ambiguity guard carry precision.
- **Promoted rows must speak the canonical selection vocabulary** — matched candidate
  home/away + "Draw", never Betfair runner names (`_snapshots_for`, bug fixed 2026-07-01),
  or the anchor's per-selection lookup silently misses (`complete=False`).
- **Never rename SHADOW_BOOKMAKER or add it to SHARP_BOOKS.** Its exclusion is the structural
  guarantee that shadow rows can never anchor a pick. Promotion is the only sanctioned path.
- **RPC errors must propagate.** `capture_once` never swallows auth/RPC failures into an
  empty report — an empty window is a benign zero; an exception is the scheduler's to log.
