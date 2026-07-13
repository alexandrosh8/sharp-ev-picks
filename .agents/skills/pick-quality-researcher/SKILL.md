---
name: pick-quality-researcher
description: "Research procedure for pick-quality/strategy questions (CLV, devig, freshness, sport microstructure, repo scans). Use when evaluating a strategy idea, scanning GitHub/X/papers for betting methodology, or ranking quality levers."
allowed_tools:
  - WebSearch
  - WebFetch
  - Read
  - Grep
  - Glob
---

# Pick-Quality Researcher

## When to use
Any strategy/quality research: new sport/market ideas, devig methods,
freshness/timing questions, external repo evaluation, practitioner claims.

## Required inputs
The frozen-constraint list FIRST: ADR-0019 hypotheses + spent slates
(docs/adr/adr-0019-*.md), the shadow-first sport policy (memory), and what
.claude/memory/decisions.md has already settled — never re-litigate.

## Procedure
1. Read constraints; list what CANNOT change (frozen params, spent data).
2. Primary sources first: papers (Buchdahl WotC, Kaunitz 1710.02824,
   Štrumbelj/Shin devig, Stern + discrete-margin critiques), official book
   rules (settlement conventions), Pinnacle Resources analyses.
3. GitHub: inspect file-by-file before recommending; score license, tests,
   lookahead discipline, closing-odds usage. Use/Reject table per repo.
4. X/Twitter: discovery ONLY (accounts, datasets, failure modes) — search
   APIs return SEO spam for CLV terms; never cite a tweet as evidence.
5. Classify every finding: OPERATIONAL (usable now, no frozen params) /
   PRE-REGISTERABLE (draft the frozen one-sentence form) / REJECTED (why).
6. End with the blunt section: what the literature does NOT support.

## Forbidden shortcuts
No ROI-claim citations; no uninspected repo recommendations; no
threshold suggestions derived from already-read local outcomes without
declaring the readout spent.

## Gotchas
- **"Closing line value" searches (GitHub AND X) are SEO-spam farms.**
  Verified twice (2026-06-24, 2026-07-04). Search by mechanism terms
  (devig, implied probability, Shin) or by author instead.
- **Extraordinary win-rate claims are a rubric trip, not a lead.** 63-65%
  vs Pinnacle = reject without reading further (trading_alpha_tennis case).
- **Our devig stack is at/ahead of public open source** (mberk/shin golden
  vectors are the only artifact worth adopting — already in test_devig.py).
  Don't re-scan devig libs without a new reason.
- **Settlement conventions are book-specific** (Pinnacle one-set vs bet365
  void) — any cross-book CLV comparison must state whose convention applies.

## Output format
Ranked levers table (impact channel, class, evidence), Use/Reject repo
table, sources with URLs, "not supported" section. No profitability claims.
