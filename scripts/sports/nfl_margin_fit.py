"""Fit the frozen NFL margin table for app/probabilities/nfl_margin.py.

READ-ONLY, OFFLINE fitter — SHADOW/annotation support only. This script never
places bets, never registers a data source, and performs NO network IO: the
operator downloads the FREE nflverse schedule/lines history first (MIT-licensed
code, community data — Lee Sharpe / nfldata) and points the fitter at the file:

    curl -sL -o /tmp/games.csv \
      "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
    uv run python scripts/sports/nfl_margin_fit.py --games /tmp/games.csv

Output is the COMMITTED frozen table (app/probabilities/nfl_margin_table.json)
— the pure-math module loads that file and never fetches anything live.

Model (nfelo-style key-number-weighted normal mixture):
  P(margin = m | mu) ∝ w_m * [Phi((m+0.5-mu)/sigma) - Phi((m-0.5-mu)/sigma)]
where mu is the expected home margin. The fit estimates
  - sigma: stddev of (result - spread_line) over completed games with a line
    (nflverse `spread_line` IS the expected home margin, positive = home
    favored; `result` = home_score - away_score), and
  - w_m: shrunk empirical/expected count ratio per signed integer margin —
    the key-number reweighting (3, 7, 6, 10, 14 spikes; 0 near-extinct since
    the 2017 OT rules). Cells are shrunk toward 1 with a pseudo-count prior
    and clipped, so sparse extreme margins can never mint wild weights.

No closing odds enter any FEATURE here: the table conditions margins on the
consensus spread of the SAME completed, historical game — a settlement-side
empirical distribution (walk-forward doctrine concerns model evaluation;
this is a frozen historical PMF shape, refit manually between seasons).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUT = _REPO_ROOT / "app" / "probabilities" / "nfl_margin_table.json"

# Signed margin support: NFL margins beyond +/-60 do not occur.
SUPPORT_MIN = -60
SUPPORT_MAX = 60
# Shrinkage pseudo-count toward ratio 1.0 (a cell needs real evidence to move).
PRIOR_COUNT = 5.0
# Weight clip: even a heavily-hit key number stays in sane territory.
W_MIN, W_MAX = 0.10, 5.0
MIN_SEASON = 1999  # first season nflverse carries lines consistently


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _cell_prob(m: int, mu: float, sigma: float) -> float:
    return _phi((m + 0.5 - mu) / sigma) - _phi((m - 0.5 - mu) / sigma)


def load_completed_lines(csv_path: Path) -> list[tuple[float, int]]:
    """(spread_line, result) for completed games with a consensus line."""
    rows: list[tuple[float, int]] = []
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                season = int(row["season"])
                spread = float(row["spread_line"])
                result = int(float(row["result"]))
            except (KeyError, TypeError, ValueError):
                continue  # unplayed / lineless rows
            if season < MIN_SEASON:
                continue
            rows.append((spread, result))
    return rows


def fit_table(samples: list[tuple[float, int]]) -> dict[str, object]:
    if len(samples) < 1000:
        raise ValueError(f"refusing to fit on {len(samples)} games (<1000)")
    residuals = [result - spread for spread, result in samples]
    n = len(residuals)
    mean_bias = sum(residuals) / n
    sigma = math.sqrt(sum((r - mean_bias) ** 2 for r in residuals) / (n - 1))

    counts: dict[int, float] = {m: 0.0 for m in range(SUPPORT_MIN, SUPPORT_MAX + 1)}
    expected: dict[int, float] = {m: 0.0 for m in range(SUPPORT_MIN, SUPPORT_MAX + 1)}
    for spread, result in samples:
        if SUPPORT_MIN <= result <= SUPPORT_MAX:
            counts[result] += 1.0
        for m in range(SUPPORT_MIN, SUPPORT_MAX + 1):
            expected[m] += _cell_prob(m, spread, sigma)

    weights: dict[str, float] = {}
    for m in range(SUPPORT_MIN, SUPPORT_MAX + 1):
        w = (counts[m] + PRIOR_COUNT) / (expected[m] + PRIOR_COUNT)
        weights[str(m)] = round(min(W_MAX, max(W_MIN, w)), 6)

    return {
        "_comment": (
            "Frozen NFL margin table (key-number-weighted normal mixture). "
            "Fit by scripts/sports/nfl_margin_fit.py from FREE nflverse/nfldata "
            "games.csv (completed games with consensus lines, season >= "
            f"{MIN_SEASON}). SHADOW/annotation support only — never a live "
            "premium fair-price source."
        ),
        "fitted_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "n_games": n,
        "sigma": round(sigma, 6),
        "mean_bias": round(mean_bias, 6),
        "support_min": SUPPORT_MIN,
        "support_max": SUPPORT_MAX,
        "prior_count": PRIOR_COUNT,
        "weights": weights,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=Path, required=True, help="nflverse games.csv path")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args()

    samples = load_completed_lines(args.games)
    table = fit_table(samples)
    args.out.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    print(f"n_games={table['n_games']} sigma={table['sigma']} mean_bias={table['mean_bias']}")
    keys = [0, 1, 2, 3, 4, 6, 7, 10, 14]
    w = table["weights"]
    assert isinstance(w, dict)
    for m in keys:
        print(f"  w[{m:+d}]={w[str(m)]}  w[{-m:+d}]={w[str(-m)]}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
