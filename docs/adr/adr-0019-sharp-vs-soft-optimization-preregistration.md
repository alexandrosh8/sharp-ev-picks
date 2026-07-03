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

- **H1 self-validates forward.** The odds ceiling is applied as a SHADOW-tier CAP
  (premium→volume), NOT a hard drop: the >5.0 1X2 longshot band is capped at the
  shadow tier — never alerted or staked, but persisted + CLV-tracked — so it keeps
  accruing forward CLV on OWN-captured Pinnacle+BSP data. This is the honest
  validation path after the discovery that football-data.co.uk dropped Pinnacle
  pre-match odds for 2026 (only ~7% coverage), which starves the retrospective
  football-data BSP backtest of a sharp anchor. Confirm H1 on the accruing shadow
  longshot CLV instead.
- A clean 2026 BSP tar is now a **named blocking dependency** for STEP 4
  (the validate-first rollout). Until it lands, the live config is STEP 1 only.
- STEP 2 instrumentation (widen the sweep to MEASURE the in-force devig + probit;
  add PBO/CSCV + bootstrap-ROI-CI reporting) is **visibility-only** — it must
  never re-select on the 2025 data.
- This ADR is the contract that prevents spent-holdout overfitting: any future
  session that "tunes up" these numbers on 2025 is violating it.


## 2026-07-02 — fresh-2026 single-shot executed; slate SPENT

The pre-registered single-shot ran once on the operator-supplied 2026 slate
(train = 2025 rows of `data (1).tar` minus its 453 possibly-peeked 2026
members; holdout = `data (2).tar`, 2026-Jan..Jun; hashes + member accounting
in `docs/research/2026-07-02-fresh-2026-single-shot-header.md`).

Outcome: **acceptance NOT met** — held-out n=13 (1X2) / n=3 (OU2.5) at the
frozen thresholds (bar: n>=150), CLV point estimates negative with CIs
straddling zero; a train->test bet-rate collapse (~20% -> ~0.45%) indicates a
2026-window soft-fill/join coverage anomaly that suppressed holdout n. Per
discipline: no re-runs, no tuning on this data. H1-H6 remain at shipped
conservative settings. The 2026-Jan..Jun slate is SPENT for selection AND
evaluation; the next single-shot requires a later, coverage-verified slate
(2026-H2 or beyond) with the fill-coverage anomaly understood first.


## 2026-07-03 AMENDMENT — H2 validation protocol (ARCADIA anchor), FROZEN

Prepared ahead of data per operator instruction; the future H2 run is
mechanical against these rules. Code mirror (constants + fail-closed logic):
`app/backtesting/arcadia_anchor.py`; exporter/preflight:
`scripts/arcadia_anchor_export.py`; wiring: `scripts/value_backtest.py
--anchor-dataset` (betfair-bsp source only). **Operator sign-off on this
amendment is required BEFORE the H2 slate exists.** Changing any frozen value
below after sign-off voids the pre-registration.

### Independent sharp-anchor source
- **Source:** the project's own Pinnacle ARCADIA capture in `odds_snapshots`
  (`pinnacle_soccer` namespace; `anchor_source = "pinnacle_arcadia"`).
  Independent of the Betfair BSP under evaluation (different venue).
  football-data PS* columns are DEAD after 2026-01-15 and are replaced, not
  supplemented. Betfair non-BSP snapshots are NOT a permitted anchor
  (same-market circularity).

### Eligibility (frozen)
- **Date range:** kickoffs 2026-07-01 .. 2026-12-31 (UTC).
- **Sports:** soccer only.
- **Leagues:** any league present in BOTH the ARCADIA capture and the BSP
  archive that survives the fail-closed event match — no league whitelist;
  league evidence is enforced via the country-token contradiction veto plus
  full participant+kickoff identity (stricter than free-text league equality).
- **Markets:** `1x2` (from `h2h`, 3 outcomes) and `ou25` (from
  `over_under_2_5`, 2 outcomes), period = full match. All other market keys
  are exported as rejected rows (`unsupported_market`).

### Anchor definition + freshness (frozen)
- **Anchor** = LAST complete outcome set (all outcomes at one `captured_at`)
  with `3600s <= kickoff - captured_at <= 86400s`. Older ⇒ `anchor_stale`;
  none in window ⇒ `anchor_missing`; superseded sets are exported rejected.
- **Same-source close (secondary only):** last complete set in the final hour
  (`0 <= kickoff - captured_at < 3600s`). It NEVER substitutes the BSP close
  in the headline CLV (same-source exclusion), and is excluded entirely when
  separated from the anchor by < 1800s (`close_tautological`).
- `freshness_seconds = kickoff - captured_at` rides every row.

### Event/market matching (fail-closed; frozen)
- Event: `match_event_hardened_scored`, orientation LOCKED
  (`allow_orientation_flip=False`), kickoff window **±60 min** (vs the live
  360-min accept drift), one-sided women/youth/reserve marker = veto,
  country-token league contradiction = veto, ≥2 acceptable source events =
  `event_ambiguous` reject.
- Market: exact `market_type` + `period` + `line` + `selection` equality.
  1x2 selections resolve by alias-canonical equality only (never fuzzy);
  unresolved = rejected.
- **Hard rejections (closed vocabulary):** `unsupported_market`,
  `incomplete_outcome_set`, `selection_unresolved`, `window_ineligible`,
  `sport_ineligible`, `anchor_stale`, `anchor_superseded`, `anchor_missing`,
  `close_tautological`, `event_unmatched`, `event_ambiguous`,
  `kickoff_out_of_window`, `market_mismatch`, `line_mismatch`,
  `selection_mismatch`. Missing/stale/ambiguous anchors are REJECTIONS —
  never a fallback price. Rejected rows are exported with reasons, never
  silently dropped.

### Metrics + acceptance (unchanged from the base ADR)
- Per market, HELD-OUT only: n (rows/clusters), mean `clv_log` ± cluster-
  robust SE (ddof=1) and 2-SE CI, bootstrap ROI CI vs baseline, PBO/CSCV;
  H1 band check; per-month join/anchor coverage in the header.
- **Acceptance:** CI lower bound > 0 AND ROI CI not worse than baseline AND
  n ≥ 150 per market AND PBO not elevated. **Failure conditions:** any
  acceptance clause failing, preflight not PASS, any contamination-guard
  violation, or per-month anchor coverage < 80% in any test month.

### Required artifacts + hashes (frozen)
- Exporter dataset CSV (all rows incl. rejected) + `.manifest.json` (git SHA,
  dataset sha256, config sha256, environment fingerprint, window, counts) +
  `.preflight.json` marker — under `data/validation/arcadia/`, never
  overwritten.
- **Config hash (frozen):**
  `6abe1a319fc4abfc3df0dbff8dfaf7aecce6b3c94eaae993b14efaa1fbceb20c`
  (runbook recipe over value_devig=power, value_moneyline_max_odds=5.0,
  value_min_edge=0.03, value_volume_min_edge=0.015, fractional_kelly=0.25).
  A drifted live config is a guard STOP.
- **Environment hash:** python version + platform fingerprint in the
  manifest; the run header must record `uv --version` and `git rev-parse
  HEAD` on a clean tree.
- **Spent-slate guard (sha256 hard STOPs):** data.tar `02dfcbfd62c733da…`,
  data (1).tar `7315dcbf1ccdabe0…`, data (2).tar `9123d3203f79e33a…`,
  combined_train2025_holdout2026.tar `dbcc3000dbf6dabe…` (full values in
  `app/backtesting/arcadia_anchor.py::SPENT_DATA_SHA256S`).

### Preflight (mandatory; DO-NOT-RUN gate)
`scripts/arcadia_anchor_export.py preflight` must PASS before any validation
run: per-month event/market coverage (≥300 usable events/month), usable +
rejected counts with reasons, missing-anchor rate ≤50%, stale-anchor rate
≤20%, match-confidence distribution, expected sample size (frozen pessimistic
5% bet-rate multiplier) vs the n≥150 bar. Any failure or guard violation ⇒
`DO-NOT-RUN`.

### H2 train/test design — FROZEN 2026-07-03 (adversarially evaluated)

**Pure prospective single-shot — no train side.** No selection of any kind
occurs on any 2026 data. The evaluated configuration is exactly the
pre-registered frozen H1–H6 values (config hash `6abe1a31…`, live and
unchanged), whose entire selection history is 2024-07..2025-12 data. All
ARCADIA-anchored, preflight-PASS rows with kickoff 2026-07-01..2026-12-31
form ONE held-out slate, read once via `--frozen-eval` (frozen parameters
loaded from `app/backtesting/arcadia_anchor.py::FROZEN_EVAL_THRESHOLDS` /
`FROZEN_EVAL_DEVIG` — never free CLI numbers). The runbook's TRAIN-sweep step
is replaced by: parameters = the frozen values; any parameter grid printed is
descriptive visibility ONLY and may never be selected from, on this or any
later slate. The acceptance-clause-2 baseline is the zero-threshold
(bet-everything) null computed on the same held-out rows with the frozen
POWER devig. Acceptance clause 4 (PBO) is redefined for this design as
satisfied by construction (no selection performed); the 2024-25 CSCV number
is carried in the run header for reference. H3 is reported as "not exercised"
(no selection step exists to apply it to); H6's agreement-gate row is
DESCOPED from this run unless its variant is implemented and committed before
the slate exists. 2025 football-data PS*-anchored rows MUST NOT appear on any
side of this run (spent for selection; anchor source non-comparable to
ARCADIA). After the single reading the H2 slate is SPENT for selection AND
evaluation.

Why this design (over a within-H2 split or a 2025-train side): a 2025 train
sweep is re-selection on the spent holdout (forbidden above) against a
non-comparable anchor; a within-H2 sweep re-opens selection on 2026 data,
overrides the frozen H2/H4 values via the max-train-ROI chooser, introduces a
free split-date parameter, and halves n. The prospective design has zero
selection on the slate by construction, maximal n, and a pre-registered
rollback path (H1) if refuted. Honest weaknesses accepted: a REJECT is less
diagnosable (mitigated by the descriptive grid), and the run confirms the
incumbent config — which is precisely what a prospective validation is.

*Operator sign-off of this amendment (one signature covers anchor source +
this split design) remains the final gate before the run.*
