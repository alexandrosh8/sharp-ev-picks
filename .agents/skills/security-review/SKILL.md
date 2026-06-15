---
name: security-review
description: "Project security review procedure. Use when committing changes that touch HTTP clients, logging, persistence, or secrets flow, and when running the periodic safety audit."
allowed_tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Security Review

## Purpose

Two gates in one procedure: no secret ever leaks, and no bet-placement code
path ever exists.

## Procedure

1. Secret scan: `gitleaks dir . --no-banner` (workspace) and the staged
   hook `gitleaks git --pre-commit --staged` (fail closed at commit time).
2. Log/exception audit: grep for `exc.url`, full-URL logging, and raw
   payload dumps; HTTP errors must log `type(exc).__name__` + status only.
3. Sanitizer check: any persisted/logged upstream payload passes redaction
   of keys AND values matching
   `(?i)(token|password|bearer|authorization|apiKey|appKey|secret)`.
4. **No-autobet audit** (`scripts/safety_audit.sh`): in `app/`, these greps
   MUST return nothing: `placeOrder|place_order|place_bet|cancelOrder|
listMarketBook|betfair.*(order|login)`, `selenium`, `playwright.*(submit|
click)`, credential-storage patterns. And these MUST hit: the PICKS_ONLY
   validator in `app/config.py`; `AUTO_BETTING=false` in `.env.example`.
5. `.env` hygiene: file mode 0600, gitignored, never read outside
   `app/config.py`; `.env.example` carries names only.
6. Dependency review on every new package: maintenance, install scripts,
   known CVEs; record verdicts in docs/research/ when non-trivial.

## Checklist

- [ ] gitleaks clean (workspace + staged)
- [ ] safety_audit.sh exits 0
- [ ] No new env reads outside app/config.py
- [ ] New dependencies reviewed and justified

## Gotchas

- **Query strings are the #1 key leak** — The Odds API puts `apiKey=` in
  the URL; any logged URL or stringified exception leaks it.
- **Test fixtures count** — gitleaks scans tests too; synthetic keys must
  not match real key formats (use `test-key-000…` shapes).
- **`git commit --no-verify` bypasses the hook** — the safety net is
  gitleaks in CI as well, never only the local hook.
- **Memory files are a leak surface** — `.Codex/memory/` is committed;
  it must never contain key fragments, account names, or chat IDs.

## Forbidden mistakes

- Approving any code that could place, modify, or cancel a bet.
- Storing betting credentials/cookies/session tokens anywhere.
- Skipping the safety audit because "nothing betting-related changed".
