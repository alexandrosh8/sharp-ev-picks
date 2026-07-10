# Full audit 2026-07-10 — run root

**Audit object:** operator-requested full-project bug audit + fix: sharp ingestion
(Pinnacle Arcadia, Betfair), soft ingestion (OddsPortal/oddschecker/odds_api), math
core (devig/edge/EV/Kelly/exposure), CLV chain, settlement, pipeline tiering,
dashboard/API display correctness; plus online upstream-change research and
backtest-data acquisition research. Implementation authorized by operator
("review code build verify... implement it"), direct-to-main per standing mandate.

**Baseline:** main @ c79144e, synced origin. Untracked: this session's research docs.

**Smoke A:** mypy clean (233 files); ruff format clean; safety audit PASSED;
ruff check `.` → 1 pre-existing I001 in alembic/versions/c3d5e7f9a1b4 (out of CI
scope — CI gates app/tests; classify pre-existing, cosmetic). pytest count being
re-captured (first capture lost the summary line to the warnings block) —
task brad04xpl → pytest_smoke_a.txt.

**Phases:**
1. FIND — workflow wz1ggjrtz (wf_fd3ef5df-f7e): 8 code lanes + 2 online lanes,
   structured findings.
2. VERIFY — adversarial verification per finding (same workflow, phase 2).
3. IMPLEMENT — confirmed findings, severity-ordered, TDD (failing test → fix →
   green), full gates per change (pytest/ruff/format/mypy/safety).
4. SMOKE B + deploy — full suite; compose build+recreate; CI poll to green on
   pushed SHA.
5. CLOSE — LEDGER.md all items terminal; memory + AGENTS notes if durable.

**Known-open exclusions (not re-reported by finders):** totals negative CLV
(strategy-revision plan Task 3), stake-0 premium alert (plan Task 1).

**Do-not-touch:** ADR-0021 frozen devig params; spent holdout 2425+2526; alerting
scope beyond evidence; anything resembling bet placement.

**Smoke A final:** pytest exit 0 (full suite green; summary line lost to buffering — exit code authoritative, morning run 3303 passed). mypy clean, format clean, safety PASSED, ruff 1 pre-existing alembic I001.
