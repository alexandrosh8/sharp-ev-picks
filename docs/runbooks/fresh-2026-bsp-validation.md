# Runbook: Fresh-2026 Betfair-BSP Single-Shot Validation (ADR-0019 H1–H6)

**Status:** ARMED, NOT RUN. This runbook is the pre-registered procedure for
evaluating ADR-0019's frozen hypotheses H1–H6 on the operator's next
uncorrupted 2026 Betfair-BSP tar. **The 2025 holdout is SPENT** — this run is
the one chance; the design forbids iteration. One command, one reading.

Nothing in this runbook was executed when it was written (2026-07-02). It
exists so the eventual run is mechanical, not improvised.

---

## DO-NOT-RUN conditions (check ALL before executing anything)

1. **Not a fresh slate.** The tar contains no 2026 data, is truncated, or
   overlaps only the spent 2024-07..2025-12 window → **STOP**.
2. **Pre-registration void.** Any parameter in the H1–H6 table has been
   re-tuned since ADR-0019 acceptance — diff `app/config.py` + the ADR against
   commit `8245a9c` → **STOP**, escalate to the operator.
3. **Slate already spent.** Anyone has already run a sweep/peek on this tar —
   check `docs/research/` and `.claude/memory/` for its filename/sha256 →
   **STOP**.
4. **Untested code.** Working tree dirty (`git status --porcelain` non-empty)
   or CI red → **STOP** (never evaluate on untested code).
5. **Intent to iterate.** If the plan is to "try a few thresholds" → **STOP**.
   One command, one reading. No re-runs after seeing output except for
   crash-class bugs — and then note the crash in the log before rerunning.

## Pre-run capture (record in the output file header)

- `git rev-parse HEAD`; `git status --porcelain` (must be empty).
- `sha256sum <tar>` + file size + a date-range probe: `tar -tf <tar> | head`
  (confirm 2026 paths are present).
- Config freeze check — record at minimum the values of `value_devig`,
  `value_moneyline_max_odds`, the edge thresholds (`value_min_edge` /
  `value_volume_min_edge` and the frozen per-market H2 values), and
  `fractional_kelly`, against the ADR-0019 frozen table. A config hash is
  fine, e.g.:
  `uv run python -c "from app.config import Settings; import hashlib, json; s=Settings(); print(hashlib.sha256(json.dumps({k: str(getattr(s,k)) for k in ('value_devig','value_moneyline_max_odds','value_min_edge','value_volume_min_edge','fractional_kelly')}, sort_keys=True).encode()).hexdigest())"`
- Environment: `uv --version`, `python --version`, `date -u`.
- Operator sign-off that the DO-NOT-RUN checks above all passed.

## The single-shot command (one invocation, both markets)

```bash
uv run python scripts/value_backtest.py \
  --source betfair-bsp \
  --betfair-bsp-tar /path/to/2026_data.tar \
  --betfair-bsp-split-date 2026-01-01 \
  --fill-universe soft \
  --markets 1x2,ou25 \
  --max-odds 5.0 \
  | tee "docs/research/$(date -u +%F)-fresh-2026-single-shot.log"
```

Notes:

- `--fill-universe soft` = soft-book fill net of commission — the
  live-faithful universe (`scripts/value_backtest.py:18-24`).
- SE verdicts use the by-match **cluster-robust** companions
  (`clv_*_se_cl` / `roi_se_cl`, `scripts/value_backtest.py:120-173`). The
  i.i.d. SEs in the output are display-only — never the verdict.

## Metrics to report (per market, HELD-OUT rows only)

- `n` (rows and clusters); mean `clv_log` ± cluster-robust SE and its 2-SE CI.
- Bootstrap ROI CI (`_roi_bootstrap_ci`) vs baseline.
- **H1 band check:** CLV of the `[5.0, ∞)` 1X2 band (must stay negative) AND
  pooled 1X2 CLV with the ceiling clears `point − 2·SE > 0`.
- **H2:** CLV at the frozen thresholds 0.010 (1X2) / 0.005 (OU2.5) ONLY — no
  threshold sweep readout.
- **H3:** TRAIN-CLV-significance objective sanity (n ≥ 150 clusters).
- **H4:** POWER devig in force, `value_devig_per_market` empty.
- **H5:** realized/nominal edge ratio k — report only; any Kelly change is
  conditional and out of scope for this run.
- **H6:** the agreement-gate variant at the frozen tolerance, reported as one
  extra row.
- PBO/CSCV number.

## Acceptance (ADR-0019, per hypothesis)

ACCEPT only when ALL hold on the held-out slice:

1. mean `clv_log` CI lower bound > 0 (cluster-robust SE, ddof=1);
2. bootstrap ROI CI not worse than baseline;
3. n ≥ 150 per market;
4. PBO not elevated.

Anything failing stays OFF. **If H1's band is refuted (the `[5.0, ∞)` band is
not CLV-negative held-out), roll the live `VALUE_MONEYLINE_MAX_ODDS=5.0`
ceiling back.**

## Output template

```
# Fresh-2026 BSP single-shot — <UTC timestamp>
commit: <git rev-parse HEAD>   tree: clean
tar: <path>  sha256: <hash>  size: <bytes>  date-range probe: <first paths>
config hash: <hash>  (value_devig=..., moneyline_max_odds=..., thresholds=..., kelly=...)
operator sign-off: DO-NOT-RUN checks 1-5 passed — <name/date>

## 1X2
| hypothesis | frozen value | n (rows/clusters) | clv_log ± 2·SE_cl | ROI CI | verdict |
|---|---|---|---|---|---|
| H1 ceiling 5.0 | ... | ... | ... | ... | ACCEPT/REJECT |
| H2 threshold 0.010 | ... | ... | ... | ... | ACCEPT/REJECT |
| ... | | | | | |

## OU 2.5
(same table; H2 frozen threshold 0.005)

## PBO/CSCV: <number>

## Slate status
THIS TAR IS NOW SPENT. Recorded in .claude/memory/ and appended to ADR-0019.
```

After the run, append a "slate now SPENT" line (tar sha256 + date) to
`.claude/memory/` and to ADR-0019 — the next agent must be unable to re-run
this tar innocently.
