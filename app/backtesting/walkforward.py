"""Walk-forward backtest engine for the Dixon-Coles value strategy.

Leakage-safe (walkforward-backtest skill): for each match we fit only on
results STRICTLY before its date, bet at the pre-match Bet365 price, settle
on the actual result, and measure CLV against the Pinnacle CLOSING line.
Pure-ish: takes already-loaded MatchRows + a fit callback; no IO here.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.backtesting.clv import clv_log
from app.ingestion.football_data import MatchRow
from app.probabilities.devig import DevigMethod, devig

# A fit callback: given history rows + as_of date, return a priced-matchup
# function (home, away) -> (p_home, p_draw, p_away) or None if unpriceable.
PricedFn = Callable[[str, str], tuple[float, float, float] | None]
FitFn = Callable[[Sequence[MatchRow], date], PricedFn]


@dataclass
class Bet:
    match_date: date
    home: str
    away: str
    selection: str  # H | D | A
    model_prob: float
    fair_prob: float
    edge: float
    ev: float
    odds_taken: float  # Bet365 pre-match
    won: bool
    clv: float | None  # log CLV vs Pinnacle close (None if no closing odds)


@dataclass
class BacktestReport:
    n_eval_matches: int
    n_priced: int
    bets: list[Bet] = field(default_factory=list)

    def at_threshold(self, min_edge: float, min_ev: float = 0.0) -> "ThresholdStats":
        chosen = [b for b in self.bets if b.edge >= min_edge and b.ev > min_ev]
        return ThresholdStats.from_bets(min_edge, chosen)


@dataclass
class ThresholdStats:
    min_edge: float
    n: int
    hit_rate: float
    roi: float  # profit per unit staked (flat 1u stakes)
    avg_edge: float
    avg_clv: float | None
    pct_beat_close: float | None
    profit_units: float

    @classmethod
    def from_bets(cls, min_edge: float, bets: list[Bet]) -> "ThresholdStats":
        n = len(bets)
        if n == 0:
            return cls(min_edge, 0, 0.0, 0.0, 0.0, None, None, 0.0)
        profit = sum((b.odds_taken - 1.0) if b.won else -1.0 for b in bets)
        hits = sum(1 for b in bets if b.won)
        clvs = [b.clv for b in bets if b.clv is not None]
        avg_clv = sum(clvs) / len(clvs) if clvs else None
        beat = sum(1 for c in clvs if c > 0) / len(clvs) if clvs else None
        return cls(
            min_edge=min_edge,
            n=n,
            hit_rate=hits / n,
            roi=profit / n,
            avg_edge=sum(b.edge for b in bets) / n,
            avg_clv=avg_clv,
            pct_beat_close=beat,
            profit_units=profit,
        )


def run_walkforward(
    matches: Sequence[MatchRow],
    fit_fn: FitFn,
    *,
    warmup_matches: int = 300,
    training_window_days: int = 540,
    refit_every_days: int = 7,
    devig_method: DevigMethod = DevigMethod.POWER,
) -> BacktestReport:
    """Chronological walk-forward over `matches`. Bets the 1X2 market at
    Bet365 pre-match odds whenever the model has positive edge; CLV vs the
    Pinnacle close."""
    ordered = sorted(matches, key=lambda m: m.match_date)
    eligible = [m for m in ordered if _has_b365(m)]
    report = BacktestReport(n_eval_matches=0, n_priced=0)

    priced_fn: PricedFn | None = None
    last_fit: date | None = None

    for i, m in enumerate(eligible):
        if i < warmup_matches:
            continue
        report.n_eval_matches += 1

        # Refit on a rolling window of results strictly before this match.
        if last_fit is None or (m.match_date - last_fit).days >= refit_every_days:
            window_start = m.match_date - timedelta(days=training_window_days)
            history = [h for h in ordered if window_start <= h.match_date < m.match_date]
            if len(history) >= 100:
                try:
                    priced_fn = fit_fn(history, m.match_date)
                    last_fit = m.match_date
                except Exception:
                    priced_fn = None

        if priced_fn is None:
            continue
        priced = priced_fn(m.home_team, m.away_team)
        if priced is None:
            continue
        report.n_priced += 1

        b365 = (m.b365_home, m.b365_draw, m.b365_away)
        fair = devig([float(o) for o in b365], method=devig_method)  # type: ignore[arg-type]
        close_fair = _closing_fair(m, devig_method)

        for idx, sel in enumerate(("H", "D", "A")):
            odds = float(b365[idx])  # type: ignore[arg-type]
            model_p = priced[idx]
            fair_p = fair[idx]
            edge = model_p - fair_p
            ev = model_p * (odds - 1.0) - (1.0 - model_p)
            if edge <= 0.0 or ev <= 0.0:
                continue
            clv = None
            if close_fair is not None:
                clv = clv_log(odds, close_fair[idx])
            report.bets.append(
                Bet(
                    match_date=m.match_date,
                    home=m.home_team,
                    away=m.away_team,
                    selection=sel,
                    model_prob=model_p,
                    fair_prob=fair_p,
                    edge=edge,
                    ev=ev,
                    odds_taken=odds,
                    won=(m.result == sel),
                    clv=clv,
                )
            )
    return report


def _has_b365(m: MatchRow) -> bool:
    return all(o is not None and o > 1.0 for o in (m.b365_home, m.b365_draw, m.b365_away))


def _closing_fair(m: MatchRow, method: DevigMethod) -> tuple[float, float, float] | None:
    odds = (m.pinnacle_closing_home, m.pinnacle_closing_draw, m.pinnacle_closing_away)
    if not all(o is not None and o > 1.0 for o in odds):
        return None
    p = devig([float(o) for o in odds], method=method)  # type: ignore[arg-type]
    return (p[0], p[1], p[2])


def bankroll_path_from_bets(
    bets: Sequence[Bet], fractional_kelly: float = 0.25, cap: float = 0.02
) -> tuple[float, float]:
    """Compound fractional-Kelly bankroll over the bets (chronological).
    Returns (final_multiple, max_drawdown)."""
    ordered = sorted(bets, key=lambda b: b.match_date)
    bankroll = 1.0
    peak = 1.0
    max_dd = 0.0
    for b in ordered:
        kelly = ((b.odds_taken - 1.0) * b.model_prob - (1.0 - b.model_prob)) / (b.odds_taken - 1.0)
        frac = min(max(kelly, 0.0) * fractional_kelly, cap)
        stake = bankroll * frac
        bankroll += stake * (b.odds_taken - 1.0) if b.won else -stake
        peak = max(peak, bankroll)
        if peak > 0:
            max_dd = max(max_dd, (peak - bankroll) / peak)
    return bankroll, max_dd
