"""Backtest the sharp-vs-soft VALUE strategy — review-corrected methodology.

Fair value = devig(Pinnacle pre-match). Bet the best available price (Max
across books) when it beats Pinnacle fair by >= threshold. No goals model.

Corrections from the 2026-06-10 deep review:
- ONE bet per match (highest-edge selection) — multiple mutually-exclusive
  bets per match are correlated and inflate the i.i.d. confidence interval.
- CLV is reported against TWO references: devig(Pinnacle close) AND
  devig(Max-of-books close). The second strips the mechanical
  "best-of-N-books premium" — beating it is the stricter test of selection
  skill. The thr=0 row is the explicit BASELINE null (bet everything).
- Thresholds are swept on TRAIN seasons only; the best train threshold is
  then evaluated once on held-out TEST seasons. No in-sample headline.
- The verdict is COMPUTED from the held-out numbers, never hardcoded.

    uv run python scripts/value_backtest.py
    uv run python scripts/value_backtest.py --train-seasons 2122,2223,2324 --test-seasons 2425,2526

Decision-support only — nothing here places bets.
"""

import argparse
import asyncio
import csv
import io
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

from app.backtesting.clv import clv_log
from app.ingestion.beatthebookie_series import load_series_dir, to_fd_row
from app.ingestion.betfair_bsp import attach_betfair_close, load_betfair_dir
from app.ingestion.football_data import fetch_season_csv
from app.ingestion.tennis_data import TennisMatchRow, load_tennis_dir
from app.probabilities.devig import DevigMethod, devig
from app.resolution.matching import default_aliases


def _f(x: object) -> float | None:
    try:
        v = float(str(x))
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


@dataclass(frozen=True)
class VBet:
    won: bool
    odds: float
    edge: float
    clv_pinn: float | None  # vs devig(Pinnacle close)
    clv_max: float | None  # vs devig(Max-of-books close) — stricter


@dataclass
class Stats:
    n: int
    hit: float
    roi: float
    clv_pinn: float | None
    clv_pinn_se: float | None
    clv_max: float | None
    clv_max_se: float | None
    beat_pinn: float | None

    @classmethod
    def from_bets(cls, bets: list[VBet]) -> "Stats":
        n = len(bets)
        if n == 0:
            return cls(0, 0.0, 0.0, None, None, None, None, None)
        profit = sum((b.odds - 1.0) if b.won else -1.0 for b in bets)
        cp = [b.clv_pinn for b in bets if b.clv_pinn is not None]
        cm = [b.clv_max for b in bets if b.clv_max is not None]

        def mean_se(xs: list[float]) -> tuple[float | None, float | None]:
            if not xs:
                return None, None
            m = sum(xs) / len(xs)
            se = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs)) / math.sqrt(len(xs))
            return m, se

        mp, sp = mean_se(cp)
        mm, sm = mean_se(cm)
        return cls(
            n=n,
            hit=sum(1 for b in bets if b.won) / n,
            roi=profit / n,
            clv_pinn=mp,
            clv_pinn_se=sp,
            clv_max=mm,
            clv_max_se=sm,
            beat_pinn=(sum(1 for c in cp if c > 0) / len(cp)) if cp else None,
        )


# Market definitions: (pre-match Pinnacle cols, pre-match Max cols, closing
# Pinnacle cols, closing Max cols, settle fn over the CSV row per outcome idx)
def _won_1x2(r: dict, i: int) -> bool | None:
    ftr = r.get("FTR")
    if ftr not in ("H", "D", "A"):
        return None
    return ftr == ("H", "D", "A")[i]


def _won_ou25(r: dict, i: int) -> bool | None:
    try:
        goals = int(r["FTHG"]) + int(r["FTAG"])
    except (KeyError, TypeError, ValueError):
        return None
    return (goals >= 3) if i == 0 else (goals <= 2)


def _won_h2h(r: dict, i: int) -> bool | None:
    """2-way tennis match-winner settlement. ``H2HWIN`` is the side index (0/1)
    that actually won AFTER the leak-safe side randomization in
    ``_tennis_to_rows`` — it is NOT fixed at 0 (the source lists odds
    winner-first; leaving the winner permanently on side 0 would leak the
    outcome into selection)."""
    w = r.get("H2HWIN")
    if w not in ("0", "1"):
        return None
    return int(w) == i


MARKETS = {
    "1x2": (
        ("PSH", "PSD", "PSA"),
        ("MaxH", "MaxD", "MaxA"),
        ("PSCH", "PSCD", "PSCA"),
        ("MaxCH", "MaxCD", "MaxCA"),
        _won_1x2,
    ),
    "ou25": (
        ("P>2.5", "P<2.5"),
        ("Max>2.5", "Max<2.5"),
        ("PC>2.5", "PC<2.5"),
        ("MaxC>2.5", "MaxC<2.5"),
        _won_ou25,
    ),
}

# Tennis 2-way match winner (H2H). Kept SEPARATE from MARKETS on purpose: MARKETS
# is parity-locked against the production evaluate_staking.MARKETS (football
# 1x2/ou25 only), so the tennis market lives in its own map and is threaded into
# bets_for/_sweep_and_eval via `markets_map`. Pinnacle pre-match = sharp; Max
# pre-match = soft; the closing columns (PSCH*/MaxCH*) are DELIBERATELY absent
# from a tennis row -> devig(close) is None -> CLV is n/a (tennis-data has NO
# close).
TENNIS_MARKETS = {
    "h2h": (
        ("PSH0", "PSH1"),
        ("MaxH0", "MaxH1"),
        ("PSCH0", "PSCH1"),
        ("MaxCH0", "MaxCH1"),
        _won_h2h,
    ),
}


def bets_for(
    rows: list[dict],
    thr: float,
    devig_method: DevigMethod = DevigMethod.POWER,
    markets: tuple[str, ...] = ("1x2",),
    min_odds: float = 1.0,
    max_odds: float = 1000.0,
    markets_map: dict | None = None,
) -> list[VBet]:
    """One bet per (match, market): the highest-edge selection >= threshold.

    ``markets_map`` defaults to the football MARKETS dict; the tennis path passes
    TENNIS_MARKETS so the parity-locked MARKETS stays football-only."""
    mkts = MARKETS if markets_map is None else markets_map
    out: list[VBet] = []
    for r in rows:
        for market in markets:
            ps_c, mx_c, psc_c, mxc_c, won_fn = mkts[market]
            ps = [_f(r.get(c)) for c in ps_c]
            mx = [_f(r.get(c)) for c in mx_c]
            psc = [_f(r.get(c)) for c in psc_c]
            mxc = [_f(r.get(c)) for c in mxc_c]
            if None in ps or None in mx or won_fn(r, 0) is None:
                continue
            sharp = devig(ps, method=devig_method)  # type: ignore[arg-type]
            close_p = devig(psc, method=devig_method) if None not in psc else None  # type: ignore[arg-type]
            close_m = devig(mxc, method=devig_method) if None not in mxc else None  # type: ignore[arg-type]
            best: tuple[float, int] | None = None  # (edge, idx)
            for i in range(len(ps)):
                if mx[i] < min_odds or mx[i] > max_odds:  # type: ignore[operator]
                    continue  # odds band: floor (no short prices) + ceiling (no longshots)
                edge = sharp[i] - 1.0 / mx[i]  # type: ignore[operator]
                if edge >= thr and (best is None or edge > best[0]):
                    best = (edge, i)
            if best is None:
                continue
            edge, i = best
            won = won_fn(r, i)
            if won is None:
                continue
            out.append(
                VBet(
                    won=won,
                    odds=mx[i],  # type: ignore[arg-type]
                    edge=edge,
                    clv_pinn=clv_log(mx[i], close_p[i]) if close_p else None,  # type: ignore[arg-type]
                    clv_max=clv_log(mx[i], close_m[i]) if close_m else None,  # type: ignore[arg-type]
                )
            )
    return out


async def load(leagues: list[str], seasons: list[str]) -> list[dict]:
    rows: list[dict] = []
    async with httpx.AsyncClient() as client:
        for lg in leagues:
            for s in seasons:
                for _ in range(4):
                    try:
                        txt = await fetch_season_csv(client, lg, s)
                        rows.extend(csv.DictReader(io.StringIO(txt)))
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(1.5)
                await asyncio.sleep(0.3)
    return rows


def _fmt(stats: Stats, label: str, baseline: Stats | None = None) -> str:
    if stats.n == 0:
        return f"{label:>9} | (no bets)"
    cp = (
        f"{stats.clv_pinn:+.4f}+/-{2 * (stats.clv_pinn_se or 0):.4f}"
        if stats.clv_pinn is not None
        else "n/a"
    )
    cm = (
        f"{stats.clv_max:+.4f}+/-{2 * (stats.clv_max_se or 0):.4f}"
        if stats.clv_max is not None
        else "n/a"
    )
    inc = ""
    if baseline and baseline.clv_pinn is not None and stats.clv_pinn is not None:
        inc = f" | incCLV {stats.clv_pinn - baseline.clv_pinn:+.4f}"
    return (
        f"{label:>9} | n={stats.n:5d} | hit {stats.hit * 100:4.1f}% | "
        f"ROI {stats.roi * 100:+6.2f}% | CLVpinn {cp} | CLVmax {cm}{inc}"
    )


def _sweep_and_eval(
    train_rows: list[dict],
    test_rows: list[dict],
    markets: tuple[str, ...],
    min_odds: float,
    max_odds: float,
    devig_methods: tuple[DevigMethod, ...],
    thresholds: tuple[float, ...],
    *,
    train_label: str,
    test_label: str,
    markets_map: dict | None = None,
) -> None:
    """Shared TRAIN-sweep -> single-shot HELD-OUT evaluation (one bet per
    match/market). Used by the football-data, BeatTheBookie, Betfair-BSP and
    tennis-data paths so the methodology (baseline null, train-only selection,
    computed verdict) is identical. ``markets_map`` lets the tennis path supply
    TENNIS_MARKETS without polluting the parity-locked MARKETS."""
    print(f"{train_label}: {len(train_rows)} matches | {test_label}: {len(test_rows)} matches\n")
    print("TRAIN sweep (thr=0.000 rows are the BASELINE null — bet everything):")
    sweep: list[tuple[DevigMethod, float, Stats]] = []
    baselines: dict[DevigMethod, Stats] = {}
    for dm in devig_methods:
        baselines[dm] = Stats.from_bets(
            bets_for(train_rows, 0.0, dm, markets, min_odds, max_odds, markets_map)
        )
        print(_fmt(baselines[dm], f"{dm.value[:5]}/0.000"))
        for thr in thresholds:
            s = Stats.from_bets(
                bets_for(train_rows, thr, dm, markets, min_odds, max_odds, markets_map)
            )
            sweep.append((dm, thr, s))
            print(_fmt(s, f"{dm.value[:5]}/{thr:.3f}", baselines[dm]))

    viable = [(d, t, s) for d, t, s in sweep if s.n >= 150]
    if not viable:
        print("\nNo viable combo (n>=150) on train — verdict: NO PROVEN EDGE.")
        return
    best_dm, best_thr, best_train = max(viable, key=lambda x: x[2].roi)
    print(
        f"\nchosen on TRAIN: devig={best_dm.value} thr={best_thr} "
        f"(ROI {best_train.roi * 100:+.2f}%, n={best_train.n})"
    )

    print("\nHELD-OUT TEST evaluation (single shot, never tuned on):")
    baseline_test = Stats.from_bets(
        bets_for(test_rows, 0.0, best_dm, markets, min_odds, max_odds, markets_map)
    )
    test = Stats.from_bets(
        bets_for(test_rows, best_thr, best_dm, markets, min_odds, max_odds, markets_map)
    )
    print(_fmt(baseline_test, "0.000"))
    print(_fmt(test, f"{best_thr:.3f}", baseline_test))
    for market in markets:
        m_stats = Stats.from_bets(
            bets_for(test_rows, best_thr, best_dm, (market,), min_odds, max_odds, markets_map)
        )
        print(_fmt(m_stats, f"  {market}", baseline_test))


async def run_beatthebookie(args: argparse.Namespace) -> None:
    """BeatTheBookie odds_series backtest (worldwide-league breadth sanity check).

    HONEST SCOPE: this source has NO sharp book — the "anchor" is the market
    CONSENSUS (mean across ~32 soft books) and the bet is the best (Max) line.
    CLV here is measured vs the consensus/Max CLOSE, NOT a sharp close, so a
    positive edge mostly reflects the best-of-N best-price premium. Read every
    number against the thr=0 bet-everything baseline; the binding sharp-CLV
    proof lives in scripts/value_backtest.py's football-data (Pinnacle) path.
    Train/test split is by kick-off DATE (odds_series ends, odds_series_b
    begins, ~2016-03-01) — there are no football-data season codes here.
    """
    dirs = [Path(d.strip()) for d in args.btb_dir.split(",") if d.strip()]
    missing = [str(d) for d in dirs if not d.is_dir()]
    if missing:
        print("BeatTheBookie data not found. Operator must place the unzipped")
        print("odds_series / odds_series_b per-game .txt files at, e.g.:")
        for d in dirs:
            print(f"    {d}")
        print("Download (Dropbox links in the upstream README): odds_series.zip,")
        print("odds_series_b.zip from github.com/Lisandro79/BeatTheBookie")
        print(f"(missing: {', '.join(missing)}). Read-only; this script places no bets.")
        return
    matches = [m for d in dirs for m in load_series_dir(d)]
    split = date.fromisoformat(args.btb_split_date)
    markets = ("1x2",)  # this source carries 1x2 only (no OU/AH series)
    min_odds, max_odds = args.min_odds, args.max_odds

    print(f"\nBEAT-THE-BOOKIE BACKTEST — {len(matches)} matches from {len(dirs)} dir(s)")
    print("SOFT/CONSENSUS data: anchor = consensus(mean of ~32 books), bet = best(Max).")
    print("CLV is vs the CONSENSUS close, NOT a sharp close (breadth, not proof).")
    print(f"market 1x2 only | min_odds {min_odds} | split by kickoff date {split.isoformat()}\n")
    if not matches:
        print("No usable matches parsed. Operator must place data files; places no bets.")
        return

    train_rows = [to_fd_row(m) for m in matches if m.kickoff_utc.date() < split]
    test_rows = [to_fd_row(m) for m in matches if m.kickoff_utc.date() >= split]
    devig_methods = (
        DevigMethod.POWER,
        DevigMethod.SHIN,
        DevigMethod.MULTIPLICATIVE,
        DevigMethod.ODDS_RATIO,
    )
    thresholds = (0.005, 0.010, 0.020, 0.030, 0.050)
    _sweep_and_eval(
        train_rows,
        test_rows,
        markets,
        min_odds,
        max_odds,
        devig_methods,
        thresholds,
        train_label="train (odds_series)",
        test_label="test (odds_series_b)",
    )
    print(
        "\nCaveat: consensus close is NOT a sharp reference; the best-price premium "
        "inflates apparent edge. Worldwide-league breadth only. Manual review "
        "required. This system does not place bets."
    )


async def run_betfair_bsp(args: argparse.Namespace) -> None:
    """Backtest CLV vs a TRUE SHARP CLOSE — the Betfair Starting Price / settled
    pre-in-play exchange close — joined onto football-data PRE-MATCH prices.

    HONEST SCOPE: the Betfair source supplies the CLOSE ONLY (BSP / last
    pre-in-play best-back) plus the settled result. It is NOT a pre-match price
    you could have bet in this dataset. The bet price is the football-data Max
    (soft, line-shopping) pre-match line the backtest already loads; the Betfair
    sharp close is JOINED to it by kickoff DATE + canonical team-name match
    (``app/resolution/matching``, strict — never guesses). CLV is then the bet's
    value vs that real sharp close — the sharp-anchor complement to the
    consensus-anchored BeatTheBookie breadth path. Data is OPERATOR-PLACED
    (account-gated, sandbox-unreachable); when the dir is absent we print the
    operator instruction and place no bets.
    """
    bsp_dir = Path(args.betfair_bsp_dir)
    markets = load_betfair_dir(bsp_dir)
    if not markets:
        print("Betfair historical data not found. Operator must place the unzipped")
        print("per-market STREAM files (one market per .bz2 / .json, Exchange Stream")
        print(f"market-change format) at, e.g.:\n    {bsp_dir}")
        print("Source (account-gated Basic tier): historicdata.betfair.com — soccer")
        print("(eventTypeId=1) + basketball (eventTypeId=7522) MATCH_ODDS markets.")
        print("A live fetch from this sandbox returns HTTP 401. Read-only; places no bets.")
        return

    leagues = [x.strip() for x in args.leagues.split(",") if x.strip()]
    train_s = [x.strip() for x in args.train_seasons.split(",") if x.strip()]
    test_s = [x.strip() for x in args.test_seasons.split(",") if x.strip()]
    min_odds, max_odds = args.min_odds, args.max_odds
    aliases = default_aliases()

    print("\nBETFAIR-BSP BACKTEST — sharp CLOSE = Betfair BSP/last-pre-in-play")
    print(f"{len(markets)} operator-placed markets | pre-match = football-data Max (soft)")
    print("CLV is vs a REAL sharp close (not consensus). BSP is CLOSE-only; the bet")
    print("price is the pre-match Max line, joined by date + canonical team name.\n")

    fd_train = await load(leagues, train_s)
    fd_test = await load(leagues, test_s)
    train_rows, train_stats = attach_betfair_close(fd_train, markets, aliases=aliases)
    test_rows, test_stats = attach_betfair_close(fd_test, markets, aliases=aliases)
    for label, st in (("train", train_stats), ("test", test_stats)):
        print(
            f"join[{label}]: fd_rows={st.n_fd_rows} markets={st.n_markets} "
            f"joined={st.n_joined} unmatched={st.n_unmatched} "
            f"result_conflict={st.n_result_conflict}"
        )
    if not train_rows or not test_rows:
        print("\nToo few joined rows to evaluate (place more overlapping Betfair markets).")
        print("Read-only; this script places no bets.")
        return

    devig_methods = (
        DevigMethod.POWER,
        DevigMethod.SHIN,
        DevigMethod.MULTIPLICATIVE,
        DevigMethod.ODDS_RATIO,
    )
    thresholds = (0.005, 0.010, 0.020, 0.030, 0.050)
    _sweep_and_eval(
        train_rows,
        test_rows,
        ("1x2",),
        min_odds,
        max_odds,
        devig_methods,
        thresholds,
        train_label="train (fd Max + Betfair close)",
        test_label="test (fd Max + Betfair close)",
    )
    print(
        "\nNote: 'CLVpinn' here is CLV vs the BETFAIR sharp close (the close slots were "
        "overwritten with BSP/last-pre-in-play). Manual review required; places no bets."
    )


def _tennis_to_rows(matches: list[TennisMatchRow], *, seed: str) -> list[dict]:
    """Convert completed, fully-priced tennis matches to h2h backtest rows.

    LEAK GUARD: tennis-data lists every odds column winner-first (PSW/PSL,
    MaxW/MaxL), so a fixed [winner, loser] layout would make the eventual winner
    ALWAYS side 0 — the selector would then settle any side-0 pick as a
    guaranteed win, leaking the outcome into ROI. We flip a SEEDED coin per match
    (deterministic, reproducible, independent of the result) so the side order is
    uncorrelated with the outcome; ``H2HWIN`` records which side actually won.
    No closing columns are written — tennis-data has none."""
    import random

    rng = random.Random(seed)
    out: list[dict] = []
    for m in matches:
        if not m.completed:
            continue  # quarantine Retired / Walkover / Awarded
        if m.psw is None or m.psl is None or m.maxw is None or m.maxl is None:
            continue  # need both sharp + soft prices on both sides
        if rng.random() < 0.5:  # swap: loser's odds sit on side 0
            ps0, ps1, mx0, mx1, win = m.psl, m.psw, m.maxl, m.maxw, 1
        else:
            ps0, ps1, mx0, mx1, win = m.psw, m.psl, m.maxw, m.maxl, 0
        out.append(
            {
                "PSH0": str(ps0),
                "PSH1": str(ps1),
                "MaxH0": str(mx0),
                "MaxH1": str(mx1),
                "H2HWIN": str(win),
                "Date": m.match_date.strftime("%d/%m/%Y"),
            }
        )
    return out


def _tennis_rows_for_years(matches: list[TennisMatchRow], years: set[int]) -> list[dict]:
    """Build h2h rows for the matches whose calendar year is in ``years``.

    Seeds the leak-guard coin PER CALENDAR YEAR (stable across runs and
    independent of the train/test assignment), mirroring the per-tour-year
    seeding in scripts/sports/tennis_backtest.py."""
    rows: list[dict] = []
    for y in sorted(years):
        group = [m for m in matches if m.match_date.year == y]
        rows.extend(_tennis_to_rows(group, seed=f"tennis-h2h-{y}"))
    return rows


async def run_tennis_data(args: argparse.Namespace) -> None:
    """Pre-match VALUE backtest for TENNIS (tennis-data.co.uk; ATP + WTA).

    HONEST SCOPE — PRE-MATCH ONLY, NO CLOSING LINE. tennis-data publishes
    Pinnacle PRE-MATCH (PSW/PSL) + Max-of-books PRE-MATCH (MaxW/MaxL) + the
    result, but it has NO closing column (unlike football-data's PSC*). So this
    path reports the held-out pre-match EDGE distribution and ROI only; CLV vs a
    close is UNDEFINED and prints as ``n/a``. Under doctrine a sport with no
    measurable closing-line CLV stays VISIBILITY-ONLY no matter how good ROI
    looks. To measure a tennis CLOSE this source must be paired with a
    Betfair-BSP tennis loader — which does not exist yet (betfair_bsp.py parses
    soccer/basketball MATCH_ODDS only, eventTypeId 1/7522, no tennis parser), so
    that join is a documented FUTURE hook, not wired here. Data is
    OPERATOR-PLACED on disk; when the dir is absent we print the operator
    instruction and place no bets.

    For the full TRAIN-sweep / one-shot held-out methodology with bootstrap CIs
    clustered by match, see scripts/sports/tennis_backtest.py; this is the
    additive value_backtest.py entry over the same source.
    """
    tdir = Path(args.tennis_dir)
    matches = load_tennis_dir(tdir)
    if not matches:
        print("tennis-data.co.uk season files not found. Operator must place the")
        print("per-season workbooks (one .xlsx per tour-year, or operator-exported")
        print(f".csv) at, e.g.:\n    {tdir}/atp_2023.xlsx\n    {tdir}/wta_2023.xlsx")
        print("Source (free, public): http://www.tennis-data.co.uk/alldata.php —")
        print("ATP {y}/{y}.xlsx (2001+), WTA {y}w/{y}.xlsx (2007+). Read-only GET;")
        print("this script never authenticates and places no bets.")
        return

    train_years = {int(y) for y in args.tennis_train_years.split(",") if y.strip()}
    test_years = {int(y) for y in args.tennis_test_years.split(",") if y.strip()}
    min_odds, max_odds = args.min_odds, args.max_odds
    completed = sum(1 for m in matches if m.completed)

    print("\nTENNIS-DATA BACKTEST — sharp(Pinnacle) vs best-of-books, PRE-MATCH ONLY")
    print(f"{len(matches)} matches ({completed} completed) from {tdir}")
    print("2-way match winner (H2H); one bet per match; settle on real Winner/Loser.")
    print("NOTE: tennis-data has NO closing line -> CLV vs close is UNDEFINED (n/a);")
    print("      the >2 SE CLV doctrine gate CANNOT be cleared -> VISIBILITY-ONLY.")
    print("      (A Betfair-BSP tennis close loader would be needed; none exists yet.)\n")

    train_rows = _tennis_rows_for_years(matches, train_years)
    test_rows = _tennis_rows_for_years(matches, test_years)
    if not train_rows or not test_rows:
        print(f"Too few rows after the year split (train years {sorted(train_years)},")
        print(f"test years {sorted(test_years)}). Place more season files; places no bets.")
        return

    devig_methods = (
        DevigMethod.POWER,
        DevigMethod.SHIN,
        DevigMethod.MULTIPLICATIVE,
        DevigMethod.ODDS_RATIO,
    )
    thresholds = (0.005, 0.010, 0.020, 0.030, 0.050)
    _sweep_and_eval(
        train_rows,
        test_rows,
        ("h2h",),
        min_odds,
        max_odds,
        devig_methods,
        thresholds,
        train_label=f"train {sorted(train_years)}",
        test_label=f"test {sorted(test_years)}",
        markets_map=TENNIS_MARKETS,
    )
    print(
        "\nVERDICT (computed): VISIBILITY-ONLY — no closing line in this source, so the "
        "incremental-CLV-vs-close gate is unevaluable regardless of held-out ROI above. "
        "Max line assumes line-shopping every book at the pre-match snapshot; soft books "
        "limit winners. Manual review required. This system does not place bets."
    )


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--source",
        choices=("football-data", "beatthebookie", "betfair-bsp", "tennis-data"),
        default="football-data",
        help=(
            "football-data.co.uk (Pinnacle, default), BeatTheBookie odds_series "
            "(consensus breadth), betfair-bsp (sharp close joined to fd Max), or "
            "tennis-data (ATP/WTA pre-match only, no close -> visibility-only)"
        ),
    )
    p.add_argument(
        "--tennis-dir",
        default="data/tennis",
        help="dir of operator-placed tennis-data.co.uk season files (.xlsx/.csv)",
    )
    p.add_argument(
        "--tennis-train-years",
        default="2019,2020,2021,2022,2023",
        help="calendar years used for the tennis TRAIN sweep",
    )
    p.add_argument(
        "--tennis-test-years",
        default="2024,2025,2026",
        help="calendar years used for the tennis held-out TEST (one shot)",
    )
    p.add_argument(
        "--betfair-bsp-dir",
        default="data/betfair/bsp",
        help="dir of operator-placed Betfair historical STREAM market files (.bz2/.json)",
    )
    p.add_argument(
        "--btb-dir",
        default="data/beatthebookie/odds_series,data/beatthebookie/odds_series_b",
        help="comma-separated dirs of operator-placed BeatTheBookie match_*.txt files",
    )
    p.add_argument(
        "--btb-split-date",
        default="2016-03-01",
        help="train/test split by kickoff date (odds_series | odds_series_b boundary)",
    )
    p.add_argument("--leagues", default="E0,E1,E2,E3,SC0,D1,D2,I1,I2,SP1,SP2,F1,F2,N1,B1,P1,T1,G1")
    # 2019/20+ is the maximal window with full PSH+Max+PSC+MaxC coverage
    # (Max/Avg columns replaced BetBrain in 2019/20; Pinnacle closing too).
    p.add_argument("--train-seasons", default="1920,2021,2122,2223,2324")
    p.add_argument("--test-seasons", default="2425,2526")
    p.add_argument("--markets", default="1x2,ou25")
    p.add_argument("--min-odds", type=float, default=1.0, help="odds floor for candidate picks")
    p.add_argument(
        "--max-odds", type=float, default=1000.0, help="odds ceiling — kill longshots (P2)"
    )
    args = p.parse_args()
    if args.source == "beatthebookie":
        await run_beatthebookie(args)
        return
    if args.source == "betfair-bsp":
        await run_betfair_bsp(args)
        return
    if args.source == "tennis-data":
        await run_tennis_data(args)
        return
    leagues = [x.strip() for x in args.leagues.split(",") if x.strip()]
    train_s = [x.strip() for x in args.train_seasons.split(",") if x.strip()]
    test_s = [x.strip() for x in args.test_seasons.split(",") if x.strip()]
    markets = tuple(x.strip() for x in args.markets.split(",") if x.strip())
    min_odds = args.min_odds
    max_odds = args.max_odds

    print(f"\nVALUE BACKTEST — {len(leagues)} leagues, markets {markets}, min_odds {min_odds}")
    if min_odds < 1.6:
        print("NOTE: production v4 config was selected WITH --min-odds 1.6 (odds floor);")
        print("      at the default floor the sweep may pick a different (equivalent) devig.")
    print(f"TRAIN {train_s} (devig x threshold sweep) | TEST {test_s} (held out, one shot)")
    print("One bet per (match, market); CLV vs Pinnacle close AND Max-of-books close.\n")

    train_rows = await load(leagues, train_s)
    test_rows = await load(leagues, test_s)
    print(f"train: {len(train_rows)} matches | test: {len(test_rows)} matches\n")

    devig_methods = (
        DevigMethod.POWER,
        DevigMethod.SHIN,
        DevigMethod.MULTIPLICATIVE,
        DevigMethod.ODDS_RATIO,
        DevigMethod.LOGARITHMIC,
        DevigMethod.DIFFERENTIAL_MARGIN,
    )
    thresholds = (0.005, 0.010, 0.015, 0.020, 0.030)

    print("TRAIN sweep (thr=0.000 rows are the BASELINE null — bet everything):")
    sweep: list[tuple[DevigMethod, float, Stats]] = []
    baselines: dict[DevigMethod, Stats] = {}
    for dm in devig_methods:
        baselines[dm] = Stats.from_bets(bets_for(train_rows, 0.0, dm, markets, min_odds, max_odds))
        print(_fmt(baselines[dm], f"{dm.value[:5]}/0.000"))
        for thr in thresholds:
            s = Stats.from_bets(bets_for(train_rows, thr, dm, markets, min_odds, max_odds))
            sweep.append((dm, thr, s))
            print(_fmt(s, f"{dm.value[:5]}/{thr:.3f}", baselines[dm]))

    # choose the (devig, threshold) maximizing train ROI with a workable sample
    viable = [(d, t, s) for d, t, s in sweep if s.n >= 150]
    if not viable:
        print("\nNo viable combo (n>=150) on train — verdict: NO PROVEN EDGE.")
        return
    best_dm, best_thr, best_train = max(viable, key=lambda x: x[2].roi)
    print(
        f"\nchosen on TRAIN: devig={best_dm.value} thr={best_thr} "
        f"(ROI {best_train.roi * 100:+.2f}%, n={best_train.n})"
    )

    print("\nHELD-OUT TEST evaluation (single shot, never tuned on):")
    baseline_test = Stats.from_bets(bets_for(test_rows, 0.0, best_dm, markets, min_odds, max_odds))
    test = Stats.from_bets(bets_for(test_rows, best_thr, best_dm, markets, min_odds, max_odds))
    print(_fmt(baseline_test, "0.000"))
    print(_fmt(test, f"{best_thr:.3f}", baseline_test))
    for market in markets:
        m_stats = Stats.from_bets(
            bets_for(test_rows, best_thr, best_dm, (market,), min_odds, max_odds)
        )
        print(_fmt(m_stats, f"  {market}", baseline_test))

    # computed verdict (never hardcoded): selection skill on held-out data =
    # incremental CLV-vs-Pinnacle over the baseline, with 2*SE separation,
    # plus the stricter CLV-vs-Max sign check.
    verdict = "NO PROVEN EDGE on held-out data"
    if (
        test.n >= 50
        and test.clv_pinn is not None
        and baseline_test.clv_pinn is not None
        and test.clv_pinn_se is not None
    ):
        inc = test.clv_pinn - baseline_test.clv_pinn
        if inc - 2 * test.clv_pinn_se > 0 and test.roi > 0:
            if (test.clv_max or 0) > 0:
                strict = "and beats even the Max-of-books close"
            else:
                strict = (
                    "but does NOT beat the Max-of-books close "
                    "(edge is mostly the best-price premium)"
                )
            verdict = (
                f"POSITIVE selection skill on held-out data: incremental CLV {inc:+.4f} "
                f"(>2SE), ROI {test.roi * 100:+.2f}% — {strict}"
            )
        elif test.roi > 0:
            verdict = (
                f"ROI positive ({test.roi * 100:+.2f}%) but incremental CLV {inc:+.4f} "
                "not conclusively above the bet-everything baseline"
            )
    print(f"\nVERDICT (computed): {verdict}")
    print(
        "\nCaveats: Max line assumes line-shopping every book at snapshot time; soft "
        f"books limit winners; high thresholds are selective ({len(leagues)} leagues swept)."
    )
    print("Manual review required. This system does not place bets.")


if __name__ == "__main__":
    asyncio.run(main())
