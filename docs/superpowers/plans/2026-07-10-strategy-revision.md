# Strategy Revision Implementation Plan — Evidence-Scoped Value Pipeline v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconfigure the live strategy to the evidence — keep the (externally triple-validated) sharp-anchored value architecture, cut the one market segment with measured negative trusted CLV, fix two found defects, and stand up the evidence engine that decides every future promotion.

**Architecture:** No new strategy is invented (research verdict: sharp-vs-soft line shopping measured by devigged sharp-close CLV is the only publicly validated shape; our volume-tier trusted CLV is +2.2%, our soccer-spreads cell +5.2%, basketball-spreads shadow +10.5%). Changes are: (1) tier-scope by market evidence via the existing per-market premium floor mechanism; (2) alert-path guard for stake-0 picks; (3) shadow-only sizing/QC upgrades from the research corpus; (4) reporting that shows the operator the right instrument (trusted CLV with CIs, not small-n ROI).

**Tech Stack:** Existing: Python 3.12 + pydantic v2, SQLAlchemy async, pytest, numpy. No new dependencies.

## Why this plan (diagnosis summary — read before executing)

Live DB evidence (2026-07-10, read-only queries, trust rules per `clv-evidence-reviewer` skill):

| Fact | Number | Implication |
|---|---|---|
| July premium settled P&L | n=15, −88.46, all minted 2026-06-30→07-04 | The losing cohort predates the 2026-07-07 selection fixes; n=15 is statistically noise |
| Premium minted post-fix | 19 picks, ALL still unsettled | The *current* strategy has produced no settled evidence yet — "not winning" is unmeasured, not refuted |
| Volume-tier trusted sharp CLV | +0.0220 (SE 0.0126, n=182) | The measurement layer is honest and the candidate machine finds real value — borderline-significant positive |
| Soccer spreads (AH) trusted CLV | +0.0518 (SE 0.0212, n=34) | Best live cell; matches Hegarty & Whelan (AH unbiased) |
| Soccer totals trusted CLV | **−0.0602 (SE 0.0241, n=24)** | The one measured drag — cut from premium until diagnosed |
| Basketball spreads trusted CLV | +0.1054 (SE 0.0136, n=23) | Shadow accruing toward its pre-registered promotion gate |
| Pick 2332 | premium, alerted, `recommended_stake_amount=0.00`, won, pnl 0 | Real defect: exposure grant of 0 still alerts (app/pipeline.py:1100-1112) |
| Bets needed to *prove* a 3% edge on ROI | ~8,700 (even odds, 95%/80% power) | ROI dashboards cannot answer "is it winning" at our n; trusted CLV can |

Verdict on "wrong coding": the CLV/settlement measurement layer checks out (voids excluded, tautology/fabrication guards active, tier split correct). Two concrete defects/anomalies exist and are Tasks 1–2. The rest of the underperformance picture is (a) a pre-fix cohort already diagnosed and fixed, (b) one mis-scoped market (totals), (c) sample sizes the operator is reading as signal.

Research corroboration (see `docs/research/2026-07-10-ultimate-strategy-synthesis.md`, `...-whole-internet-research.md`): the architecture was independently converged on by Kaggle 2026 winners (market prices at 90% weight), practitioner taxonomy (top-down price-taking = the realistic solo path), and commercial products (Unabated Line). The plan therefore *rescopes and hardens* rather than replaces.

## Global Constraints

- **HARD: picks-only — no bet placement code anywhere; `bash scripts/safety_audit.sh` must exit 0 after every task.**
- Gates after every task: `uv run pytest -q` green, `uvx ruff check .` clean, `uvx ruff format --check app tests` clean, `uv run mypy app tests` clean.
- No network in tests (httpx.MockTransport / fakeredis). TDD: failing test first.
- UTC-aware datetimes everywhere; pure-math boundary (`app/risk/`, `app/edge/` = numpy/stdlib only; env read only in `app/config.py`).
- Never `&&` in shell; never bare `rm`; quote paths.
- **Spent-holdout ledger is binding:** no re-tuning on 2425+2526; new thresholds ship shadow-first or config-default-off; promotions require pre-registered single-shot or live trusted-CLV CI > 0 at n ≥ 50.
- Shadow-first mandate for any new sport/strategy behavior (operator mandate 2026-07-04).
- Deploy = `docker compose --profile prod build app` then `up -d --force-recreate app` from `/workspace`; **check CI on the pushed SHA to success** (operator mandate 2026-07-02).
- Commit per task; `git commit -m "checkpoint"` before anything large.

---

### Task 1: Never alert a stake-0 premium pick (defect fix)

Pick 2332 was alerted as premium with `recommended_stake_fraction=0.0` (exposure ledger granted 0 — cap exhausted). An alert with stake 0 is operator noise and mis-states the strategy. Rule: a premium candidate whose granted stake fraction is 0 is **demoted to the volume tier** (persisted + CLV-tracked, not alerted, reserving nothing) with demotion reason `"stake_zero"`.

**Files:**
- Modify: `app/pipeline.py` (the exposure-grant branch around lines 1100–1112 where `"recommended_stake_fraction": granted` is set; the demotion mechanism mirrors the existing ML-filter/major-league demotions in `run_value_pipeline`)
- Test: `tests/test_pipeline_stake_zero.py` (new)

**Interfaces:**
- Consumes: existing demotion plumbing in `run_value_pipeline` (demotion reason slugs, tier reassignment) — find it by grepping `demotion` in `app/pipeline.py`; the major-league demotion (`is_major_league`) is the template.
- Produces: premium candidates with `granted == 0.0` become tier `"volume"`, demotion reason includes `"stake_zero"`. No schema change.

- [ ] **Step 1: Write the failing test.** Follow the existing pipeline-test fixtures (see `tests/test_value.py` / existing `run_value_pipeline` tests for the builder pattern used to fake deps). The test builds a premium-eligible candidate, forces the exposure ledger to grant 0 (exhaust the cap in the fixture or stub the reserve call), runs the pipeline, and asserts:

```python
async def test_stake_zero_premium_is_demoted_to_volume(pipeline_deps_factory):
    # exposure ledger fixture: daily cap already fully reserved -> grant == 0.0
    deps = pipeline_deps_factory(exposure_cap_exhausted=True)
    result = await run_value_pipeline(deps)
    pick = result.picks[0]
    assert pick.tier == "volume"
    assert "stake_zero" in pick.demotion_reasons
    assert pick.recommended_stake_fraction == 0.0
    assert result.n_alerted == 0
```

(Adapt fixture names to the real ones in the existing tests — the assertion set is the contract.)

- [ ] **Step 2: Run it, verify it fails** — `uv run pytest tests/test_pipeline_stake_zero.py -v` → FAIL (pick stays premium/alerted today).
- [ ] **Step 3: Implement.** In the grant branch (`app/pipeline.py` ~1100–1112): when the candidate's tier is `premium` and `granted == 0.0`, reassign tier to `volume`, append `"stake_zero"` to the demotion-reason list, skip the alert emit. Do not change the volume-tier path (volume already carries 0 stake by design).
- [ ] **Step 4: Full gates** — `uv run pytest -q`; `uvx ruff check .`; `uvx ruff format --check app tests`; `uv run mypy app tests`; `bash scripts/safety_audit.sh`.
- [ ] **Step 5: Commit** — `git commit -m "fix(pipeline): demote stake-0 premium candidates to volume (never alert stake 0)"`

### Task 2: Cut soccer totals from the premium tier (config, demote-not-drop)

Totals is the one cell with measured negative trusted CLV (−0.0602, SE 0.0241, n=24). Use the EXISTING per-market premium floor: `VALUE_MIN_EDGE_PER_MARKET` (parsed by `parse_market_min_edges`, wired at `app/config.py:1562` into `ValuePolicy.min_edge_by_market`). Setting the totals floor prohibitively high demotes every totals candidate to volume (still persisted + CLV-tracked — the evidence keeps accruing; nothing is dropped).

**Files:**
- Modify: `.env` (production, on host): add `totals:0.99` to `VALUE_MIN_EDGE_PER_MARKET` (preserving any existing entries, comma-separated)
- Modify: `.env.example`: document the entry with the evidence one-liner and the re-promotion criterion
- Test: extend the existing `parse_market_min_edges` test in `tests/test_config.py` only if no case covers a >0.5 floor (verify first — do not duplicate)

**Interfaces:**
- Consumes: `parse_market_min_edges` / `ValuePolicy.min_edge_by_market` (`app/edge/value_policy.py:43`) — already validated ≥ volume floor and applied as the premium gate.
- Produces: operational config; no code change expected.

- [ ] **Step 1: Verify mechanism in a test** (only if absent): a candidate in market `totals` with edge 0.06 and `min_edge_by_market=(("totals", 0.99),)` lands in tier `volume`, not `premium`. Run targeted existing tests: `uv run pytest tests/test_value.py -k "min_edge" -v`.
- [ ] **Step 2: Apply to `.env`** on the host (mode 0600; never commit), update `.env.example` with:

```bash
# 2026-07-10 evidence gate: soccer totals trusted sharp-CLV is -6.0% (n=24) -> premium-blocked.
# Re-promote ONLY when trusted totals CLV CI > 0 at n >= 50 (see docs/superpowers/plans/2026-07-10-strategy-revision.md Task 3).
VALUE_MIN_EDGE_PER_MARKET=totals:0.99
```

- [ ] **Step 3: Deploy + verify** — `docker compose --profile prod build app`; `docker compose --profile prod up -d --force-recreate app`; then confirm in logs/dashboard that new totals candidates mint as `volume`.
- [ ] **Step 4: Commit** (`.env.example` + any test) — `git commit -m "config(value): premium-block soccer totals on negative trusted CLV evidence (demote-not-drop)"`

### Task 3: Totals post-mortem diagnostic (decides re-promotion or bug-fix)

Determine whether the totals drag is (a) a close-matching defect (line mismatch — e.g. pick at 2.5 goals graded against a 3.0-line close; `market_detail` exact-key matching exists at `app/clv_trueup.py:437-452` with a legacy line-blind fallback), (b) devig structure (2-way totals devigged with a method fitted on 3-way), or (c) genuine market signal (sharp books price totals efficiently; the anchor's totals lines are thin).

**Files:**
- Create: `scripts/research/totals_clv_postmortem.py` (read-only SQL via the existing async session helpers used by sibling scripts in `scripts/research/`)
- Create: `docs/research/2026-07-XX-totals-postmortem.md` (findings)

**Interfaces:**
- Consumes: `picks` × `result_tracking` × `odds_snapshots` (splits per `clv-evidence-reviewer` skill); `canonical_market_detail` for line comparison.
- Produces: a verdict written to the findings doc: `close-matching-bug | devig-structure | market-signal | insufficient-n`, with per-line and per-anchor-book splits.

- [ ] **Step 1: Write the script.** It must report, for all settled totals picks with a trusted close: (1) `market_detail` at mint vs the detail of the matched close row (count exact / legacy-line-blind / mismatched); (2) trusted CLV split by `mint_devig_fell_back` × `close_devig_fell_back`; (3) trusted CLV split by anchor book and by line (2.5 vs others); (4) the same splits on spreads as the control cell (spreads is +5.2% — a defect present in both would show there too).
- [ ] **Step 2: Run it** — `uv run python scripts/research/totals_clv_postmortem.py` against prod (read-only).
- [ ] **Step 3: Write the verdict doc** with the decision rule: if close-matching-bug → fix with TDD and re-promote after fix + n≥50 CI>0; if market-signal → totals stays volume-only permanently (matches literature: AH/spreads unbiased, derived markets weaker); if insufficient-n → revisit at n≥50.
- [ ] **Step 4: Commit** — `git commit -m "research(totals): CLV post-mortem script + verdict"`

### Task 4: Trusted-CLV-first operator report (kill the ROI misread)

The operator judged the strategy on small-n ROI. Make the dashboard/performance output lead with the decision instrument.

**Files:**
- Modify: `app/backtesting/live_evidence.py` (report assembly; honesty floors already exist — `MIN_STRATUM_N=50`)
- Modify: `app/api/dashboard.html` (scorecard area)
- Test: extend the existing dashboard contract tests (`tests/` — grep `dashboard` for the suite that pins scorecard vocabulary)

**Interfaces:**
- Consumes: existing `live_evidence_report` strata + `performance_report` fields (`n_sharp_close`, `sharp_stake_weighted_clv_log`).
- Produces: three additions, no removals: (1) per-tier trusted-CLV headline with 95% CI and n (nulled below floor, as now); (2) a **CLV→yield calibration row**: realized flat-stake yield ÷ trusted CLV on the same subset, displayed against the RebelBetting public benchmark 0.8× (`docs/research/2026-07-10-whole-internet-research.md`, commercial lane); (3) a plain-language verdict line: "evidence sufficient/insufficient to judge profitability at current n" driven by the existing significance gates (t-CI excludes 0, Wilson low > 0.5).

- [ ] **Step 1: Failing test** for the new report fields (assert keys `trusted_clv_ci`, `clv_yield_ratio`, `evidence_verdict` present and nulled below floors).
- [ ] **Step 2: Implement in `live_evidence.py`** (pure aggregation — no new queries needed if the trusted subset rows are already loaded; otherwise extend the existing repository call, not raw SQL in the report layer).
- [ ] **Step 3: Dashboard render** — add the three rows to the scorecard; keep the existing state vocabulary pinned by contract tests.
- [ ] **Step 4: Full gates; deploy; screenshot-verify** the scorecard renders (use the existing Playwright/devtools QA flow).
- [ ] **Step 5: Commit** — `git commit -m "feat(evidence): trusted-CLV-first scorecard with CI, CLV->yield calibration and verdict line"`

### Task 5: Uncertainty-shrunk staking (shadow annotation only)

Research-backed sizing upgrade (Baker & McHale 2013; Bayesian-Kelly pattern; Uhrín 2021 ruin evidence): multiply the Kelly fraction by φ = n_eff/(n_eff+κ), where n_eff = settled trusted-CLV sample of the pick's (strategy, sport, market) cell and κ=50 (half-weight at 50 settled picks). SHADOW: computed and persisted in `stake_breakdown`, NOT applied to `recommended_stake_fraction` until a pre-registered review.

**Files:**
- Modify: `app/risk/staking.py` (pure function; policy fields via frozen dataclass — env only in `app/config.py`)
- Modify: `app/config.py` (new settings `STAKE_UNCERTAINTY_SHRINK_ENABLED=false`, `STAKE_UNCERTAINTY_KAPPA=50`)
- Modify: `app/pipeline.py` (thread the cell's n_eff into the staking call; store `phi` and `shrunk_fraction` in `stake_breakdown`)
- Test: `tests/test_staking_shrink.py` (new)

**Interfaces:**
- Consumes: `staking.py`'s existing breakdown object (`breakdown.final` consumed at `app/pipeline.py:634` and `:1826`).
- Produces: `uncertainty_shrink(fraction: float, n_eff: int, kappa: float) -> float` in `app/risk/staking.py`; `stake_breakdown` JSON gains `{"phi": float, "n_eff": int, "shrunk_fraction": float}`.

- [ ] **Step 1: Failing tests** — property tests in the repo's style: φ∈(0,1]; φ→1 as n_eff→∞; φ=0.5 at n_eff=κ; shrunk ≤ original; never negative; kappa≤0 raises.

```python
def test_shrink_half_weight_at_kappa():
    assert uncertainty_shrink(0.02, n_eff=50, kappa=50) == pytest.approx(0.01)

def test_shrink_monotone_in_n_eff():
    fs = [uncertainty_shrink(0.02, n, 50) for n in (0, 10, 50, 500, 50_000)]
    assert fs == sorted(fs) and fs[0] == 0.0 and fs[-1] < 0.02
```

- [ ] **Step 2: RED** — `uv run pytest tests/test_staking_shrink.py -v` fails (function absent).
- [ ] **Step 3: Implement** (5 lines, pure): `return fraction * (n_eff / (n_eff + kappa))` with validation.
- [ ] **Step 4: Wire shadow annotation** in the pipeline (flag-gated; default off → breakdown annotated, final unchanged).
- [ ] **Step 5: Full gates; commit** — `git commit -m "feat(risk): uncertainty-shrunk Kelly (shadow annotation, default off)"`

### Task 6: Anchor-thinness telemetry (mint-time, log-only)

Community evidence (arbusers): where the sharp anchor's own market is thin, large "edge vs anchor" is usually fake. We already floor exchange liquidity; add the missing telemetry for Pinnacle/consensus anchors: persist at mint the count of distinct books quoting the market and the anchor snapshot age. NO gating in this task — telemetry accrues until a walk-forward review defines a threshold (never tune on the spent holdout).

**Files:**
- Modify: `app/pipeline.py` (both mint sites where picks are assembled — the fields are already computed nearby for the width/vig gates; persist them)
- Modify: `app/storage/models.py` + new Alembic migration (two nullable columns on `picks`: `anchor_book_count SMALLINT`, `anchor_age_seconds NUMERIC(12,2)` — check first whether `steam_anchor_age_seconds` already carries the age; if so reuse it and add only the count)
- Test: extend the persistence test suite (`tests/test_persistence.py` pattern)

- [ ] **Step 1: Check for existing fields** (`steam_anchor_age_seconds` at mint; `anchor_match_confidence`) — reuse, don't duplicate.
- [ ] **Step 2: Failing test** asserting the new column(s) round-trip on a minted pick.
- [ ] **Step 3: Migration + persist; `uv run alembic upgrade head` up/down verified.**
- [ ] **Step 4: Full gates; commit** — `git commit -m "feat(value): persist anchor thinness telemetry at mint (log-only)"`

### Task 7: Pre-registered promotion/kill criteria (the governance file)

Write the binding criteria BEFORE the evidence arrives, so no future decision re-tunes on observed data.

**Files:**
- Create: `docs/adr/adr-0022-evidence-scoped-tiering.md`

**Contents (verbatim criteria, signed by the operator):**
- Soccer totals re-promotion: trusted sharp-CLV 95% CI > 0 at n ≥ 50 post-Task-3 verdict; else volume-only permanently.
- Basketball spreads promotion to premium: trusted CLV CI > 0 at n ≥ 50 AND source-agreement + freshness + coverage per the shadow-first policy memo (2026-07-04); current n=23 at +0.1054 — do not promote early.
- Premium tier kill criterion: if post-fix premium trusted CLV CI < 0 at n ≥ 50, premium alerts pause automatically pending review.
- Uncertainty shrink enforcement (Task 5 flag on): only after 30 days of shadow annotations show shrunk stakes would not have cut aggregate trusted-CLV-weighted EV by more than the drawdown reduction justifies (report the comparison, operator signs).
- Sizing/threshold changes NEVER re-tuned on 2425+2526 (spent); fresh evidence = live shadow or season 2627 single-shot only.

- [ ] **Step 1: Write ADR; operator signs.**
- [ ] **Step 2: Commit** — `git commit -m "docs(adr): ADR-0022 pre-registered tier promotion/kill criteria"`

### Task 8 (queued, do NOT start until Tasks 1–7 land): research-backed probes

Each is a separate one-day, shadow-only follow-up with its own plan; listed here so scope is visible, deliberately NOT specified to code level (their designs depend on Task 3/7 outcomes):

- Draw-frequency devig QC probe (all FLB devigs underestimate 1X2 draws — arXiv 2604.17194); shadow report only; ADR-0021 params stay frozen.
- Monte Carlo null-record simulator (Buchdahl MCoB / paper-betting-tracker idea) as a report section beside the CLV CIs.
- Picks-per-eligible-events alarm (Buchalter bet-volume smoke detector) as pipeline telemetry.
- Two-stage regress-to-market edge shrink (Peabody) — supersedes the pending "edge-shrink" backlog item; needs its own pre-registration.

---

## Success definition for the whole plan

Within ~4–6 weeks (enough settlements for n≥50 in the premium post-fix cohort):
- Premium trusted sharp-CLV CI reported per ADR-0022; decision (continue/kill) taken on that number, not on P&L eyeballing.
- Zero stake-0 alerts; totals absent from premium; totals verdict documented.
- Operator dashboard answers "is it winning?" with the statistically honest instrument.

## Self-review notes

- Every task compiles against verified anchors (pipeline.py:1100-1112 grant branch; config.py:1562 market-floor wiring; value_policy.py:43; clv_trueup.py:437-452; staking.py:152).
- No task touches devig params (ADR-0021 frozen), the spent holdout, or alerting scope beyond the evidence-backed totals cut.
- Task 6 has a reuse-check step because `steam_anchor_age_seconds` may already cover half of it.
- Tasks 1–2 are the "wrong coding" answer; Tasks 3–4 are the "no winning profits" answer; Tasks 5–8 are the research-corpus upgrades, all shadow-first.
