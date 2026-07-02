# Fresh-2026 BSP single-shot — pre-run capture (runbook: docs/runbooks/fresh-2026-bsp-validation.md)
utc: 2026-07-02T21:56:36Z
commit: f2db457e579bd68c7b0b3d365f105037156dc50b
porcelain: 1 tracked changes (untracked .IMPLEMENTAUDIT/ reports dir only)
input tar: data/betfair/bsp/combined_train2025_holdout2026.tar (9,275,719,680 bytes)
  built from: data (1).tar sha256=7315dcbf1ccdabe02899f490976ebd3e38dea9669c0396eb83cc658b5ed2bc24 (2025-Apr..Dec train side; 453 BASIC/2026/* members EXCLUDED as possibly-peeked; final member BASIC/2025/Dec/29/35084877/1.252010287.bz2 EXCLUDED, truncated tail)
  and:        data (2).tar sha256=9123d3203f79e33ac09e2a6a2f8d91ccff8d684ac74e0a02cbde9002440ed330 (2026-Jan..Jun holdout side, FRESH; final member BASIC/2026/Jun/20/35732830/1.259268835.bz2 EXCLUDED, truncated tail)
  member accounting: train 1,336,289 kept + 453 dropped-2026 + 1 truncated = 1,336,743 (GNU); holdout 902,857 kept + 1 truncated = 902,858 (GNU)
config hash: 6abe1a319fc4abfc3df0dbff8dfaf7aecce6b3c94eaae993b14efaa1fbceb20c
frozen values: devig= power  ml_ceiling= 5.0  min_edge= 0.03  vol_min_edge= 0.015  kelly= 0.25
env: uv 0.11.23 (x86_64-unknown-linux-gnu) / Python 3.14.6
DO-NOT-RUN checks: 1 fresh-2026 PASS (holdout tar 2026-Jan..Jun) / 2 frozen-params PASS (H4 restored, hash above) / 3 no-prior-peek PASS (data(2) uploaded Jul-1 07:20, bsp probes dated Jun-30; zero references) / 4 tree-clean+CI-green PASS (#429) / 5 single-shot intent PASS (one invocation, one reading)

## VERDICT (single reading, 2026-07-02 ~22:20 UTC)

Per ADR-0019 acceptance (held-out cluster-robust CLV CI lower bound > 0 AND
n >= 150 per market AND bootstrap ROI CI not worse than baseline):

- [1x2] chosen-on-train shin/0.005 -> HELD-OUT n=13, ROI +45.2% (meaningless
  at n=13), CLV **-0.0243 +/- 0.0585 (cluster)**, incCLV -0.0174.
  ACCEPTANCE: **NOT MET** (n 13 << 150; CLV point-negative, CI straddles 0).
- [ou25] chosen-on-train power/0.010 -> HELD-OUT n=3, ROI -100%, CLV
  -0.0567 +/- 0.0466. ACCEPTANCE: **NOT MET** (n 3 << 150).
- Train-side (2025, spent) at frozen thresholds remained positive
  (1x2 power/0.005: +0.0105 +/- 0.0046, n=1590) — selection-side only,
  no evidential weight.

DATA CAVEAT (recorded, not investigated further per single-shot discipline):
the bet rate collapsed from ~20% of train matches to ~0.45% of test matches
at the same thresholds — a 2026-window soft-fill/join coverage anomaly is
likely (football-data 2526 Max/book columns), which suppressed holdout n.
This run therefore neither validates nor refutes; the pre-registered
acceptance simply was not met. NO re-runs. The 2026-Jan..Jun slate is SPENT.

CONSEQUENCES: all H1-H6 stay at their shipped (conservative) settings; no
gate/threshold/staking change; live CLV accrual remains the primary evidence
path; README headline caveats stay, now citing this run.
