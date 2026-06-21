"""Consensus-vs-max value backtest on BeatTheBookie (~880k worldwide matches).

The dataset has NO sharp book — only avg (consensus) + max (best-of-N price). So
this is the WEAKER cousin of scripts/value_backtest.py: fair = devig(avg
consensus); bet the max best price on the highest prob-edge selection (one
bet/match) when that edge >= threshold; settle on the real result.

Because max >= avg by construction, the bet-everything baseline (thr=0) already
banks the mechanical best-of-N premium. The HONEST signal is therefore the
INCREMENTAL ROI of a high threshold OVER that baseline on held-out data — does
selecting big consensus-deviations beat just betting every best price?

    uv run python scripts/beatthebookie_backtest.py
Decision-support only — places no bets.
"""

import asyncio

from app.ingestion.beatthebookie import BttMatch, load_btb_matches
from app.probabilities.devig import DevigMethod, devig

_THRESHOLDS = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08)


def _best_leg(m: BttMatch, method: DevigMethod) -> tuple[float, float, bool] | None:
    """(prob_edge, max_price, won) for the highest-edge 1X2 selection, or None."""
    try:
        ph, pd_, pa = devig((m.avg_home, m.avg_draw, m.avg_away), method=method)
    except ValueError:
        return None
    legs = [
        (ph - 1.0 / m.max_home, m.max_home, m.home_score > m.away_score),
        (pd_ - 1.0 / m.max_draw, m.max_draw, m.home_score == m.away_score),
        (pa - 1.0 / m.max_away, m.max_away, m.away_score > m.home_score),
    ]
    return max(legs, key=lambda leg: leg[0])


def _evaluate(matches: list[BttMatch], method: DevigMethod, thr: float) -> tuple[int, float, float]:
    n = 0
    wins = 0
    pnl = 0.0
    for m in matches:
        leg = _best_leg(m, method)
        if leg is None or leg[0] < thr:
            continue
        _, price, won = leg
        n += 1
        wins += int(won)
        pnl += (price - 1.0) if won else -1.0
    roi = pnl / n if n else 0.0
    hit = wins / n if n else 0.0
    return n, hit, roi


async def main() -> None:
    matches = await load_btb_matches()
    train = [m for m in matches if m.match_date.year <= 2012]
    test = [m for m in matches if m.match_date.year >= 2013]
    method = DevigMethod.SHIN
    print(f"BeatTheBookie consensus-vs-max backtest — {len(matches)} matches")
    print(f"  train (<=2012): {len(train)}   test (>=2013): {len(test)}   devig={method.value}\n")

    print("TRAIN sweep (thr=0.00 = baseline null — bet every best price):")
    best_thr = 0.0
    best_roi = -1e9
    for thr in _THRESHOLDS:
        n, hit, roi = _evaluate(train, method, thr)
        print(f"  thr={thr:.2f} | n={n:>6} | hit {hit:.3f} | ROI {roi * 100:+.2f}%")
        if thr > 0 and n >= 200 and roi > best_roi:
            best_roi = roi
            best_thr = thr

    print(f"\nchosen on TRAIN: thr={best_thr:.2f}")
    print("\nHELD-OUT TEST (single shot):")
    base_n, base_hit, base_roi = _evaluate(test, method, 0.0)
    sel_n, sel_hit, sel_roi = _evaluate(test, method, best_thr)
    base_pct, sel_pct = base_roi * 100, sel_roi * 100
    print(f"  baseline thr=0.00 | n={base_n:>6} | hit {base_hit:.3f} | ROI {base_pct:+.2f}%")
    print(f"  chosen   thr={best_thr:.2f} | n={sel_n:>6} | hit {sel_hit:.3f} | ROI {sel_pct:+.2f}%")
    incremental = (sel_roi - base_roi) * 100
    print(f"\nINCREMENTAL ROI over baseline (the honest signal): {incremental:+.2f} pts")
    print(
        "Caveat: avg=consensus, max=best-of-N price (NOT a sharp book). The baseline "
        "already banks the best-price premium; only the incremental row reflects "
        "consensus-deviation selection. Frozen 2000-2015. Places no bets."
    )


if __name__ == "__main__":
    asyncio.run(main())
