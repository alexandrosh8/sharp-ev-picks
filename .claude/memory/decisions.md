# Decisions Log

- 2026-06-10 — Project is a **manual-betting +EV picks decision-support
  platform** (never an auto-betting bot, never "paper trading" by default).
  Enforcement layers: ADR-0002.
- 2026-06-10 — Clean-room core: `app/` code written fresh from researched
  repos/literature; sibling projects (kestrel, Betting Picks) are NOT ported.
- 2026-06-10 (later, user direction) — **Proven libraries used DIRECTLY**:
  penaltyblog, lightgbm/xgboost, nba_api, OddsHarvester (backfills) as
  dependencies — ADR-0011. Exceptions (evidence-based): WagerBrain (Kelly
  p/q-swap bug) and betfairlightweight (ships bet execution) stay out.
  Existing pure-math core stays; parity-tested against penaltyblog (1e-8).
- 2026-06-10 — Free-first odds ingestion; paid Odds API keys optional
  (ADR-0010 when research completes).
- 2026-06-10 — Hooks design accepted: ADR-0003.
- 2026-06-10 — Memory system: project-local markdown (this directory) +
  docs/adr/; external memory tools rejected — ADR-0001.
