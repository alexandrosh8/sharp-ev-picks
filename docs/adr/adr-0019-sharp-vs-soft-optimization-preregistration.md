# ADR-0019 — Pre-registration of the sharp-vs-soft optimization hypotheses

- **Status:** Accepted (2026-06-30)
- **Relates to:** ADR-0006 (devig method policy), ADR-0016 (major-league
  premium gate), ADR-0017 (CLV close-anchor provenance). Operationalizes
  `docs/research/2026-06-30-sharp-vs-soft-calibrate-optimize.md`.

## Context

The 13-agent sharp-vs-soft calibrate/optimize research firmed up the held-out
CLV vs the **true Betfair-BSP sharp close** on the merged 2024-07..2025-12 sample
(123k MATCH_ODDS / 114k OU2.5). The edge is **real-but-marginal**: 1X2
+0.0091±0.0131, OU2.5 +0.0076±0.0103 — both 2-SE CIs straddle 0.

The research found exactly one structurally-justified improvement (the H2H odds
ceiling, **shipped** in STEP 1 — not pre-registered, it rests on the
favourite-longshot band replicating across the split, not on a tuned threshold).
Everything else is **validate-first**: it cannot be honestly confirmed because
the **2025 holdout is SPENT** — it has been read across PRs #156-160 and every
BSP sweep this session. Re-tuning any parameter on it, or reading a "winner" off
its sweep, converts an honest estimate into an overfit one.

This ADR **freezes** the remaining hypotheses and their acceptance criteria
BEFORE a fresh, never-examined slate exists, so the eventual evaluation is a
valid single-shot test rather than a continuation of the spent-holdout search.

## Decision

The following hypotheses are **pre-registered**. Each will be evaluated **once**,
on a fresh slate (the operator's next uncorrupted 2026 Betfair-BSP tar, or a
nested/walk-forward CSCV over 2024-07..2025-12 in which no fold's holdout is
reused). No parameter below may be re-tuned on the 2025 holdout.

### Frozen hypotheses

| # | Hypothesis (frozen value) | Rationale |
|---|---------------------------|-----------|
| H1 | **max_odds = 5.0** on H2H (shipped). On fresh data: confirm (i) the `[5.0, inf)` band stays CLV-negative and (ii) pooled 1X2 held-out CLV WITH the ceiling clears point - 2*SE > 0. | Favourite-longshot band, -0.087 (>4 SE), replicates across split. |
| H2 | **Edge thresholds: 1X2 ~ 0.010, OU2.5 ~ 0.005** (probability-space). Do NOT adopt the 0.02-0.03 rows. | High-threshold rows are n-collapse mirages (n~20). |
| H3 | **Selection objective = TRAIN CLV-significance** (max mean `clv_log` s.t. ddof=1 t-CI lower > 0, n >= 150), replacing `max(TRAIN ROI)`. | Mirrors `optimize_thresholds.py` doctrine; the ROI key is noise-dominated. |
| H4 | **Devig pool = shift-family only (multiplicative PRUNED); global default POWER** (shipped); `value_devig_per_market` EMPTY. | All shift methods tie within 1 SE; multiplicative 6-8 SE worse on 3-way. |
| H5 | **fractional_kelly = 0.125** (from 0.25), KEEPING the 2% per-bet cap, ONLY if the fresh-data realized/nominal edge ratio `k` warrants it. | Sizing is per-bet-ROI-invariant; this is risk-shaping. The CAP (not the fraction) is the binding ruin control (no-cap full-Kelly ruin 86%). |
| H6 | **Pinnacle-AND-consensus agreement gate** (tolerance frozen at the value recorded in the research log) as a selection variant; anchor SOURCE unchanged. | Lowest-priority; may prune Pinnacle longshot picks toward the favourite-leaning consensus profile. |

### Acceptance criteria (per hypothesis, on the fresh slate)

Accept a change ONLY if, on the fresh holdout:
1. held-out **mean `clv_log` CI lower bound > 0** (ddof=1 SE), AND
2. **bootstrap ROI CI not worse than baseline** (`_roi_bootstrap_ci`), AND
3. **n >= 150 per market**, AND
4. the run's **PBO (probability of backtest overfitting)** is reported and not
   elevated.

Anything failing these stays OFF. H1 is already live; if fresh data REFUTES it
(band not negative / pooled CLV not > 0), it is rolled back.

## Consequences

- A clean 2026 BSP tar is now a **named blocking dependency** for STEP 4
  (the validate-first rollout). Until it lands, the live config is STEP 1 only.
- STEP 2 instrumentation (widen the sweep to MEASURE the in-force devig + probit;
  add PBO/CSCV + bootstrap-ROI-CI reporting) is **visibility-only** — it must
  never re-select on the 2025 data.
- This ADR is the contract that prevents spent-holdout overfitting: any future
  session that "tunes up" these numbers on 2025 is violating it.
