# ADR-0011: Proven Libraries Are Used Directly (supersedes the clean-room-only policy)

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) — explicit direction: "All that repos its
  safe to use and download direct as its proven works so no need to build it
  again from scratch."

## Context

The original 2026-06-10 decision was clean-room application code with
researched repos as references/oracles. The user revised this: proven,
inspected libraries should be consumed directly. The Phase B research
(docs/research/betting-repo-research.md) had already inspected every
candidate file-by-file, so each gets an evidence-based role.

## Decision

| Package                      | Role                                                                                                                           | Install                                                     |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------- |
| **penaltyblog** (MIT, v1.11) | Dixon-Coles fitting + implied-probability methods used directly in phase 3; football-data/Understat/ClubElo scrapers available | `uv sync --extra football` ✅ installed, imports verified   |
| **lightgbm / xgboost**       | NBA + ensemble engines per ADR-0009                                                                                            | `--extra models` ✅ installed (4.6.0 / 3.2.0)               |
| **nba_api** (MIT, 3.7k★)     | NBA stats ingestion (phase 5)                                                                                                  | `--extra nba` ✅ installed (1.11.4)                         |
| **OddsHarvester** (MIT)      | One-off OddsPortal historical backfills, used directly as a tool                                                               | `--extra backfill` (requires-python bumped to ≥3.12 for it) |

The already-built pure-math core (`app/probabilities`, `app/risk`,
`app/edge`) **stays**: it is tested, oracle-validated, and the pipeline's
dependency surface for math remains zero. Direct-use and our core now
cross-validate each other: `tests/test_parity_penaltyblog.py` proves all four
devig methods match penaltyblog to 1e-8 on every test book, and
`tests/test_devig.py` carries mberk/shin's exact reference values.

## Evidence-based exception — WagerBrain

WagerBrain is NOT taken as a dependency despite the general direction: its
flagship `basic_kelly_criterion` is mathematically wrong (p/q swap — returns
−0.10 where correct Kelly is +0.10 at p=0.55, evens; verified by file
inspection 2026-06-10), it is unpackaged, untested, and 6 years stale. "Proven
works" does not hold for it. Its correct margin/vig formulas remain
cross-reference material only, and its bug lives on as our regression guard
(`tests/test_kelly.py::test_kelly_sign_guards_against_p_q_swap`).
**betfairlightweight** likewise stays banned as an import — it ships
one-call bet execution (`place_orders`), violating ADR-0002.

## Consequences

- requires-python ≥3.12 (Dockerfile → python:3.12-slim).
- Extras keep the default runtime lean; CI installs the default profile and
  the parity test auto-skips where extras are absent.
- Phase 3 consumes `penaltyblog.models` Dixon-Coles directly instead of
  reimplementing; our calibration/walk-forward gates still apply unchanged.
