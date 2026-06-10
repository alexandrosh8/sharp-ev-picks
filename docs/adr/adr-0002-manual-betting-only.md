# ADR-0002: Manual-Betting-Only Safety Architecture

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

This platform detects +EV picks for Football/Soccer and NBA. The owner reviews
picks and places any bet personally. The master requirements forbid: automatic
bet placement, Betfair order submission, bookmaker login automation, browser
betting automation, storing betting credentials/cookies, anti-bot bypass, and
real-money execution code. A single accidental code path that can place a bet
is unacceptable.

## Decision

Enforce "picks-only" through **five independent layers**, so no single mistake
can produce an executing system:

1. **CLAUDE.md hard rules** — the first content section forbids execution code;
   any instruction that would allow it must be rewritten before acting.
2. **Agent forbidden-actions** — all 15 project agents carry the verbatim line:
   "Never write, scaffold, or suggest code that places bets automatically,
   authenticates to a bookmaker for placement, or drives a browser to a
   betting slip. Decision-support only."
3. **Runtime config validator** — `app/config.py` raises at startup if
   `AUTO_BETTING` or `BET_EXECUTION_ENABLED` is true, or `PICKS_ONLY` /
   `MANUAL_BETTING_ONLY` / `READ_ONLY_MARKET_DATA` is false. There is no code
   that reads these flags to _enable_ anything — they exist only to fail fast
   if tampered with.
4. **CI safety audit** — `scripts/safety_audit.sh` greps `app/` for
   order-placement identifiers (placeOrder, place_bet, cancelOrder,
   listMarketBook…), login/browser automation (selenium, playwright submit),
   and credential storage; the build fails on any hit.
5. **Read-only integrations** — all market-data clients are GET-only; Betfair
   credentials slots in `.env.example` are explicitly read-only market data.

## Justification

Defense in depth: instructions guide the model, the validator guards runtime,
the audit guards the codebase, and the integration policy guards the network
boundary. Each layer fails independently and loudly.

## Alternatives considered

- Single `AUTO_BETTING` flag gating an execution module — rejected: the
  existence of an execution module is itself the hazard.
- Trusting instructions alone — rejected: instructions don't survive
  refactors; CI greps do.

## Consequences

- Recommended stakes are informational only; result tracking is manual entry.
- Any future exchange integration must pass the safety audit unchanged —
  i.e., it can only ever read market data.
