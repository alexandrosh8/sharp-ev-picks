# ADR-0013: Free Live-Pinnacle Sharp-Line Archive via the arcadia guest API (clean-room, read-only)

- **Status:** accepted
- **Date:** 2026-06-16
- **Deciders:** GodFather (Alexis) — "check and find free data needed so both
  nba and tennis to validated … its good to add a part of this repo?
  ACHBIDHAN/Pinnacle_Football_Odds_Scraper" → "proceed bu take the important
  data and code from that repo that needed".

## Context

The decisive doctrine gate (`.claude/skills/github-research`, ADR-0010) is:
a sport is CLV-backtestable only with a FREE source carrying BOTH a **sharp
pre-match anchor (Pinnacle)** AND the **closing line**. Football has this via
football-data.co.uk's `PSC*` columns; **NBA and tennis do not** — every free
historical source (SBR, tennis-data.co.uk, Kaggle) is consensus/soft-only and
fails the gate. A free **live** Pinnacle feed is the project's biggest
documented data gap (`docs/research/free-odds-sources.md`).

GitHub research surfaced the unofficial Pinnacle **guest JSON API**
`guest.api.arcadia.pinnacle.com/0.1` (accountless, GET-only) in several repos,
incl. `ACHBIDHAN/Pinnacle_Football_Odds_Scraper`. That repo is **UNLICENSED**
(`gh api .../license` → 404; no LICENSE file) → all-rights-reserved, so **no
code is copyable**. The API _surface_, however, is fact, not expression.

## Decision

Build `app/ingestion/pinnacle_arcadia.py` — a **clean-room, read-only** capture
that takes the repo's _important data_ (API facts), not its code:

| Taken (facts, license-clean)                                                                                                                                                                                                                                                                                                                                                   | NOT taken                                                                                                                                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Endpoint paths `/sports/{id}/matchups`, `/sports/{id}/markets/straight?primaryOnly=false`; sport ids (soccer 29, tennis 33, basketball 4, NFL 15); the market `key` scheme (`s;0;m` = period-0 moneyline); American→decimal formula; the public guest `x-api-key` scheme (a public constant) — its VALUE is NOT committed (default empty; the two endpoints used require none) | Any source code from the (unlicensed) repo — the client, parser, and version-gate are written from scratch and verified against the LIVE API |

Architecture (the load-bearing choices):

- **Independent capture job, never an `ODDS_SOURCE`.** `ODDS_SOURCE` is
  single-select; making arcadia a source would _replace_ OddsPortal and leave
  nothing to line-shop against. Instead `build_scheduler` registers a separate
  `IntervalTrigger` job (gated by `ARCADIA_ENABLED`, OFF by default) that runs
  ALONGSIDE the active source and mints **no picks/alerts**.
- **Isolated `pinnacle_<sport>` warehouse namespace.** Captured period-0
  moneyline closes persist via the normal `persist_odds_snapshots` path with
  `bookmaker="Pinnacle"`, under sport keys `pinnacle_soccer` / `pinnacle_tennis`
  / `pinnacle_basketball`. AVAILABLE GAMES filters to `soccer`/`basketball`/
  `tennis` only, so the archive **never pollutes** the dashboard or pick path.
- **Change-gated on Pinnacle's per-market `version` int** (mirrors the
  pipeline's change-only cache) — one row per genuine reprice; the latest
  pre-kickoff row IS that event's sharp close, picked up automatically by
  `closing_odds_from_snapshots` (no `is_closing` flag, which is dead code).

## Verification (live, 2026-06-16)

Read-only smoke test against the real API (no DB writes):

| sport      | upcoming ≤72h | moneyline quotes | snapshots               |
| ---------- | ------------- | ---------------- | ----------------------- |
| tennis     | 249           | 245              | 490 (2-way)             |
| soccer     | 101           | 101              | 303 (3-way, incl. draw) |
| basketball | 29            | 28               | 56 (2-way)              |

Soccer 303 = 101×3 confirms the home/draw/away handling; samples emit
`bookmaker="Pinnacle"`, `market=h2h`, correct decimals. 16 unit/integration
tests (`tests/test_pinnacle_arcadia.py`, `httpx.MockTransport` + compose
Postgres); ruff/mypy/safety-audit green.

## Safety

GET-only PUBLIC market data — **no account, no login, no stored credentials, no
order placement** (ADR-0002). The guest key is a public web-client constant
(authenticates no user) and is **optional/empty by default** (the endpoints
used require none), so no secret is committed and gitleaks has nothing to flag.
Secret hygiene mirrors `OddsApiClient`: status-only errors, the URL never
stringified. `scripts/safety_audit.sh` passes.

## Consequences

- **The archive accumulates immediately** — the irreplaceable asset (a missed
  close is lost forever). Tennis is in-season now; NBA returns Oct 2026.
- **This is NOT instant validation.** It is the data _enabler_. Two further,
  separately-tested steps turn it into NBA/tennis CLV validation:
  1. **Cross-source event resolution** (deferred): attach a Pinnacle close to
     the matching OddsPortal event/pick. Done with a STRICT matcher only —
     fuzzy joins (e.g. tennis "Daniil Medvedev" vs OddsPortal "Medvedev
     Daniil") are forbidden; a wrong join = corrupted CLV.
  2. **Pick generation** for tennis/NBA (today visibility-only / 0 picks) —
     there must be picks to measure CLV on.
- ToS-grey + endpoint-fragile (same class as the OddsPortal scrape); treat
  gaps as expected, never bypass anti-bot protections. If Pinnacle ever
  requires the guest key, set `ARCADIA_GUEST_KEY` in `.env` (sniff-on-401).
- v1 captures period-0 **moneyline** only; totals/spreads are a later add.
