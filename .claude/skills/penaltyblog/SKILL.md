---
name: penaltyblog
description: "penaltyblog public API (v1.11.0) — football models (Dixon-Coles etc.), implied/devig, betting (Kelly/value/arb), scrapers, ratings, metrics, backtest. Use when calling penaltyblog in this project (app/models/football_dc.py)."
allowed_tools:
  - Read
  - Write
  - Edit
  - Bash
---

# penaltyblog API (v1.11.0)

Authoritative for the installed version (introspected 2026-06-10). Upstream's
own `.claude/skills/penaltyblog/SKILL.md` is referenced in their docs but is
gitignored in their repo and not published — this file is built from the
installed package, so it matches exactly what we run.

Top modules: `models bayes betting implied matchflow metrics ratings scrapers fpl viz xt backtest`.

## models — goal models

Classes: `PoissonGoalsModel`, `DixonColesGoalModel`, `BivariatePoissonGoalModel`,
`NegativeBinomialGoalModel`, `WeibullCopulaGoalsModel`,
`ZeroInflatedPoissonGoalsModel`, `BayesianGoalModel`,
`HierarchicalBayesianGoalModel`.

```python
from penaltyblog.models import DixonColesGoalModel
m = DixonColesGoalModel(goals_home, goals_away, teams_home, teams_away,
                        weights=None, neutral_venue=None)   # arrays/Series, equal length
m.fit(minimizer_options={"maxiter": 1000}, use_gradient=True)
grid = m.predict(home_team, away_team, max_goals=15, normalize=True, neutral_venue=False)
```

`fit()` raises `ValueError("Optimization failed ... Iteration limit reached")`
on hard data — pass `minimizer_options={"maxiter": 1000}`. `predict()` returns a
`FootballProbabilityGrid` and raises `ValueError("goal_matrix contains negative
probabilities")` for extreme matchups (DC rho correction) — catch and skip.

## FootballProbabilityGrid — derived markets

Properties: `home_win`, `draw`, `away_win`, `home_draw_away` (tuple),
`btts_yes`, `btts_no`, `double_chance_1x/12/x2`, `draw_no_bet_home/away`,
`win_to_nil_home/away`, `expected_points_home/away`,
`home_goal_distribution`, `away_goal_distribution`, `total_goals_distribution`,
`exact_score`.
Methods: `total_goals("over"|"under", line)`, `totals(line)->(under,push,over)`,
`asian_handicap(...)`, `asian_handicap_probs(...)`.

## implied — devig

```python
from penaltyblog.implied import calculate_implied
r = calculate_implied([2.5, 3.2, 2.9], "power")   # multiplicative|additive|power|shin|
r.probabilities                                    # differential_margin_weighting|odds_ratio|logarithmic
```

Returns `ImpliedProbabilities` with `.probabilities`. **Our `app/probabilities/devig.py`
matches this to 1e-8 (tests/test_parity_penaltyblog.py).**

## betting

`kelly_criterion`, `multiple_kelly_criterion`, `identify_value_bet`,
`calculate_bet_value`, `find_arbitrage_opportunities`, `arbitrage_hedge`,
`convert_odds`. **NOTE:** in THIS project use `app/risk/staking.py` for Kelly
(our caps + transparent decomposition); penaltyblog betting is reference only.

## scrapers (free data)

`FootballData`, `Understat`, `ClubElo`, `FBRef`. We use our own
`app/ingestion/football_data.py` loaders; penaltyblog scrapers are an option
for Understat xG / ClubElo enrichment (roadmap phase 6).

## ratings · metrics · backtest

ratings: `Elo`, `Colley`, `Massey`, `PiRatingSystem`.
metrics: `rps_average`, `rps_array`, `multiclass_brier_score`,
`ignorance_score` — proper scoring for calibration (calibration-eval skill).
`backtest` module exists but is a naive date-loop — use our
`walkforward-backtest` skill / `app/backtesting/` instead.

## Gotchas

- **fit() can hit the iteration limit** on small/degenerate samples — always
  pass `minimizer_options={"maxiter": 1000}` (we do in `football_dc.py`).
- **predict() raises on extreme matchups** (negative grid cells from the rho
  correction) — `app/models/football_dc.py` catches `ValueError` and returns
  no prediction rather than crashing the poll.
- **Team names must match exactly** between fit and predict — penaltyblog has
  no fuzzy matching; resolution is our job (`DixonColesFootballModel.resolve_team`).
- **No py.typed marker** — mypy can't analyze it; it's in the ignore-missing
  list in `pyproject.toml`.
- **`calculate_implied` rejects a str** — pass a `list[float]`, not a string.
- **`total_goals("over"|"under", line)` silently EXCLUDES push mass** on
  integer lines (over + under sum to < 1). For integer/quarter totals lines
  use `totals(line)`, which returns the full `(under, push, over)` split
  (verified against penaltyblog 1.11.0: `totals(3.0)` on
  `create_dixon_coles_grid(1.5, 1.1, -0.05)` → (0.518, 0.218, 0.264)).
- **TWO OPPOSITE Dixon-Coles rho conventions ship in 1.11.0** — never mix
  rho across families. PAPER (tau(0,1)=1+rho·λ_home): the fitted
  `DixonColes` model kernel + basic `goal_expectancy`. TRANSPOSED
  (tau(0,1)=1+rho·λ_away): `goal_expectancy_extended` +
  `create_dixon_coles_grid`. Feeding a fitted-model rho into
  `create_dixon_coles_grid` silently mis-prices 1-0/0-1 (moves AH ±0.5/±1);
  they coincide only when λ_home == λ_away. `app/models/ah_bridge.py`
  (extended→grid) is consistent as-is. Pinned by
  `tests/test_penaltyblog_rho_convention.py`.

## Forbidden mistakes

- Using penaltyblog's Kelly/value-bet output to auto-place bets — this project
  is decision-support only.
- Re-deriving devig here — our pure-math core already matches penaltyblog (1e-8).
- Treating its backtester as leakage-safe (it is not).
