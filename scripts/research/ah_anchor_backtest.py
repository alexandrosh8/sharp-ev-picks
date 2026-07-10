"""AH-anchored soccer fair vs power-devig 1X2 fair — walk-forward CLV study.

Idea #1 of docs/research/2026-07-10-litx-strategy-sweep.md (Hegarty & Whelan:
1X2 odds carry favourite-longshot bias, Asian Handicap odds on the same
fixtures do not). Question: does STRICTLY REPLACING the devigged 1X2 Pinnacle
pre-match anchor with an AH+O/U-derived fair improve the sharp-close CLV of
value picks selected against the soft best (Max) price?

DESIGN (pre-specified, no tuning; where the sweep doc is silent the task
brief's rules apply):

- Data: local football-data.co.uk cache (data/ml/cache/{season}_{league}.csv,
  22 European leagues). Eval window = seasons 1920..2526 per the sweep design.
  All anchor inputs are PRE-MATCH columns only (PSH/PSD/PSA, PAHH/PAHA + AHh,
  P>2.5/P<2.5, MaxH/MaxD/MaxA); closing columns (PSC*/MaxC*/PC*/AHCh) are
  never read by the anchor builders — they score CLV only.
- Baseline arm (v1): power-devig(PSH, PSD, PSA) — the deployed anchor.
- Candidate arm (v2): power-devig 2-way (PAHH, PAHA) gives the fair AH
  effective cover probability at line AHh; power-devig(P>2.5, P<2.5) gives the
  fair P(total >= 3). Solve the bivariate-Poisson-with-Dixon-Coles-tau score
  model for (lambda_home, lambda_away) matching both, then read 1X2 off the
  score matrix. Quarter lines split into two half-stakes; the effective cover
  probability is W/(W+L) over the halves (push returns stake), which is
  exactly what a 2-way devig of the quoted AH odds recovers.
- The single fitted parameter (Dixon-Coles rho) is fitted by MLE per eval
  season on seasons STRICTLY BEFORE it (expanding window back to 1213; the
  betbrain-era BbAv AH/OU columns supply the market inputs pre-1920). Fit
  matches' lambdas are solved at rho=0 (one-pass approximation, documented).
- Selection (identical for both arms, mirrors the deployed premium doctrine —
  frozen, not swept): candidates H/D/A at the pre-match Max price; keep
  edge = fair_p * price - 1 >= 0.03, price in [1.6, 4.0]; ONE pick per match
  (argmax edge). Primary comparison universe = fixtures where BOTH anchors
  exist (coverage is reported separately).
- Scoring close (log-ratio CLV, app/backtesting/clv.py convention:
  clv = ln(price * p_close_fair)): PRIMARY = Betfair exchange close where the
  strict canonical-event join matches (app/ingestion/betfair_bsp cache), else
  power-devig(PSCH, PSCD, PSCA). Both legs also reported separately — the
  Pinnacle-close leg is NOT independent of the hypothesis (if the 1X2 close
  retains the same favourite-longshot bias, scoring against it penalises the
  arm that corrects the bias).
- Inference: per-season and pooled mean-CLV delta (v2 - v1) with a match-day
  (date) clustered bootstrap (B=2000, seed 20260710); ddof=1 i.i.d. and
  date-clustered SEs on each arm's mean. Flat-stake ROI at the Max price is
  reported as the secondary truth check.
- Success bar (sweep design): pooled delta 95% CI excluding 0 on the primary
  close AND >= 80% anchor coverage retained.

HONESTY / STATUS: exploratory, NOT a pre-registered confirmatory test. The
1920-2526 football-data seasons have been consulted by earlier studies of
OTHER hypotheses (value_backtest sweeps; the AH-market betting one-shot
consumed 2425+2526 for that separate question), and the BSP 2025 holdout is
spent for the ADR-0019 hypotheses. The strongest available verdict here is
PROMISING-PREREGISTER; any promotion requires a pre-registered forward test.

Run (offline; reads only local files, writes only the report):
    .venv/bin/python scripts/research/ah_anchor_backtest.py
    .venv/bin/python scripts/research/ah_anchor_backtest.py --quick  # smoke

Decision-support only — nothing here places bets.
"""

from __future__ import annotations

import argparse
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.backtesting.clv import cluster_robust_se  # noqa: E402
from app.probabilities.devig import DevigMethod, devig  # noqa: E402

CACHE_DIR = REPO_ROOT / "data" / "ml" / "cache"
BSP_CACHE = REPO_ROOT / "data" / "betfair" / "bsp" / "soccer_match_odds.jsonl.gz"
REPORT_PATH = REPO_ROOT / "docs" / "research" / "2026-07-10-ah-anchor-backtest.md"

LEAGUES = (
    "E0", "E1", "E2", "E3", "EC", "SC0", "SC1", "SC2", "SC3",
    "D1", "D2", "I1", "I2", "SP1", "SP2", "F1", "F2",
    "N1", "B1", "P1", "T1", "G1",
)  # fmt: skip
ALL_SEASONS = (
    "1213", "1314", "1415", "1516", "1617", "1718", "1819",
    "1920", "2021", "2122", "2223", "2324", "2425", "2526",
)  # fmt: skip
EVAL_SEASONS = ("1920", "2021", "2122", "2223", "2324", "2425", "2526")
PINNACLE_BLACKOUT = date(2026, 1, 15)  # PS*/PSC* columns dead on/after this

# Frozen selection rule (deployed premium doctrine — NOT swept here).
EDGE_MIN = 0.03
ODDS_MIN = 1.6
ODDS_MAX = 4.0
ODDS_BANDS = ((1.6, 2.0), (2.0, 2.5), (2.5, 3.0), (3.0, 4.0))

SEED = 20260710
B_BOOT = 2000
K_GOALS = 16  # score matrix 0..15 goals each side (renormalised)
RHO_BOUNDS = (-0.15, 0.15)
DEVIG = DevigMethod.POWER


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------


@dataclass
class Fixture:
    """One parsed football-data row (eval seasons)."""

    league: str
    season: str
    kickoff_date: date
    home: str
    away: str
    ftr: str  # H | D | A
    # pre-match (signal-time)
    ps: tuple[float, float, float] | None  # Pinnacle 1X2
    mx: tuple[float, float, float] | None  # Max (best of books) 1X2
    ah: tuple[float, float, float] | None  # (line AHh, PAHH, PAHA)
    ou: tuple[float, float] | None  # (P>2.5, P<2.5)
    # closes (scoring only — never feed anchors)
    psc: tuple[float, float, float] | None  # Pinnacle closing 1X2
    bf_close: tuple[float, float, float] | None = None  # Betfair exchange close
    # anchors (filled later)
    fair_v1: tuple[float, float, float] | None = None
    fair_v2: tuple[float, float, float] | None = None


def _f(row: dict[str, str], col: str) -> float | None:
    raw = (row.get(col) or "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val


def _odds3(row: dict[str, str], cols: tuple[str, str, str]) -> tuple[float, float, float] | None:
    vals = tuple(_f(row, c) for c in cols)
    # `not (v > 1.0)` (rather than `v <= 1.0`) also rejects NaN, whose
    # comparisons are all False and would otherwise slip through.
    if any(v is None or not np.isfinite(v) or not v > 1.0 for v in vals):
        return None
    return vals  # type: ignore[return-value]


def _parse_date(raw: str | None) -> date | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _read_csv_rows(season: str, league: str) -> list[dict[str, str]]:
    import csv

    path = CACHE_DIR / f"{season}_{league}.csv"
    if not path.is_file():
        raise FileNotFoundError(
            f"football-data cache file missing: {path} — expected the offline "
            "cache written by scripts/ml/build_value_dataset.py"
        )
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        return [r for r in csv.DictReader(fh) if (r.get("HomeTeam") or "").strip()]


def load_fixtures(seasons: tuple[str, ...], leagues: tuple[str, ...]) -> list[Fixture]:
    out: list[Fixture] = []
    for season in seasons:
        for league in leagues:
            for r in _read_csv_rows(season, league):
                d = _parse_date(r.get("Date"))
                ftr = (r.get("FTR") or "").strip()
                if d is None or ftr not in ("H", "D", "A"):
                    continue
                ah_line = _f(r, "AHh")
                ah_odds = _odds3(r, ("PAHH", "PAHH", "PAHA"))  # reuse validator
                ah = None
                if ah_line is not None and ah_odds is not None:
                    ah = (ah_line, ah_odds[1], ah_odds[2])
                ou_over, ou_under = _f(r, "P>2.5"), _f(r, "P<2.5")
                ou = None
                if ou_over is not None and ou_under is not None and min(ou_over, ou_under) > 1.0:
                    ou = (ou_over, ou_under)
                out.append(
                    Fixture(
                        league=league,
                        season=season,
                        kickoff_date=d,
                        home=r["HomeTeam"].strip(),
                        away=(r.get("AwayTeam") or "").strip(),
                        ftr=ftr,
                        ps=_odds3(r, ("PSH", "PSD", "PSA")),
                        mx=_odds3(r, ("MaxH", "MaxD", "MaxA")),
                        ah=ah,
                        ou=ou,
                        psc=_odds3(r, ("PSCH", "PSCD", "PSCA")),
                    )
                )
    return out


def load_rho_fit_rows(
    max_season: str, leagues: tuple[str, ...]
) -> list[tuple[str, float, float, float, float, float, int, int]]:
    """(season, line, ah_h, ah_a, over, under, hg, ag) for seasons < max_season.

    Pinnacle pre-match AH/OU where present (1920+), else betbrain-era BbAv
    columns. Only used to fit the Dixon-Coles rho — never enters selection.
    """
    out: list[tuple[str, float, float, float, float, float, int, int]] = []
    for season in ALL_SEASONS:
        if season >= max_season:
            break
        for league in leagues:
            for r in _read_csv_rows(season, league):
                try:
                    hg, ag = int(r["FTHG"]), int(r["FTAG"])
                except (KeyError, ValueError):
                    continue
                for line_c, h_c, a_c, ov_c, un_c in (
                    ("AHh", "PAHH", "PAHA", "P>2.5", "P<2.5"),
                    ("BbAHh", "BbAvAHH", "BbAvAHA", "BbAv>2.5", "BbAv<2.5"),
                ):
                    line = _f(r, line_c)
                    vals = tuple(_f(r, c) for c in (h_c, a_c, ov_c, un_c))
                    if line is None or any(v is None or v <= 1.0 for v in vals):
                        continue
                    ah_h, ah_a, over, under = vals  # type: ignore[misc]
                    out.append((season, line, ah_h, ah_a, over, under, hg, ag))
                    break
    return out


# --------------------------------------------------------------------------
# score model: independent Poisson + Dixon-Coles tau, vectorised
# --------------------------------------------------------------------------


def _pois_table(lam: np.ndarray, k: int) -> np.ndarray:
    """Poisson pmf table, shape (n, k): p[:, j] = e^-lam lam^j / j!."""
    n = lam.shape[0]
    out = np.empty((n, k))
    out[:, 0] = np.exp(-lam)
    for j in range(1, k):
        out[:, j] = out[:, j - 1] * lam / j
    return out


def _score_matrix(lh: np.ndarray, la: np.ndarray, rho: float) -> np.ndarray:
    """Truncated DC score matrix, shape (n, K, K), renormalised to sum 1."""
    ph = _pois_table(lh, K_GOALS)
    pa = _pois_table(la, K_GOALS)
    m = ph[:, :, None] * pa[:, None, :]
    if rho != 0.0:
        m[:, 0, 0] *= np.clip(1.0 - lh * la * rho, 1e-10, None)
        m[:, 0, 1] *= np.clip(1.0 + lh * rho, 1e-10, None)
        m[:, 1, 0] *= np.clip(1.0 + la * rho, 1e-10, None)
        m[:, 1, 1] *= np.clip(1.0 - rho, 1e-10, None)
    m /= m.sum(axis=(1, 2), keepdims=True)
    return m


_SUP_VALUES = np.arange(-(K_GOALS - 1), K_GOALS)  # supremacy grid


def _supremacy_pmf(m: np.ndarray) -> np.ndarray:
    """P(home_goals - away_goals = s), shape (n, 2K-1); col s = index s+K-1."""
    n = m.shape[0]
    out = np.zeros((n, 2 * K_GOALS - 1))
    for s in range(-(K_GOALS - 1), K_GOALS):
        out[:, s + K_GOALS - 1] = np.trace(m, offset=-s, axis1=1, axis2=2)
    return out


def _ah_effective_prob(sup_pmf: np.ndarray, lines: np.ndarray) -> np.ndarray:
    """Fair effective cover probability W/(W+L) for the HOME AH stake.

    Home covers half-line l when supremacy + l > 0; an integer component
    pushes at supremacy == -l. Matches the settle convention
    (FTHG - FTAG) + AHh > 0 used elsewhere in this repo.
    """
    win = np.zeros(lines.shape[0])
    lose = np.zeros(lines.shape[0])
    grid = _SUP_VALUES[None, :]
    for offset in (-0.25, 0.25):
        # evaluate both quarter components for every row; for half/integer
        # lines the two components coincide (same masks), which just doubles
        # W and L and leaves W/(W+L) unchanged.
        component = lines[:, None] + offset
        is_quarter = np.abs(np.round(lines * 4.0).astype(int) % 2) == 1
        eff_line = np.where(is_quarter[:, None], component, lines[:, None])
        win += (sup_pmf * (grid + eff_line > 1e-9)).sum(axis=1)
        lose += (sup_pmf * (grid + eff_line < -1e-9)).sum(axis=1)
    total = win + lose
    return np.where(total > 0, win / np.maximum(total, 1e-300), np.nan)


def _p_over25(m: np.ndarray) -> np.ndarray:
    """P(total goals >= 3) from the score matrix."""
    t0 = m[:, 0, 0]
    t1 = m[:, 1, 0] + m[:, 0, 1]
    t2 = m[:, 2, 0] + m[:, 1, 1] + m[:, 0, 2]
    return 1.0 - (t0 + t1 + t2)


def _bisect(
    f: Callable[[np.ndarray], np.ndarray],
    lo: np.ndarray,
    hi: np.ndarray,
    iters: int = 60,
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised bisection for f increasing in x. Returns (root, ok_mask)."""
    flo, fhi = f(lo), f(hi)
    ok = (flo <= 0) & (fhi >= 0)
    lo, hi = lo.copy(), hi.copy()
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        take_hi = fm > 0
        hi = np.where(take_hi, mid, hi)
        lo = np.where(take_hi, lo, mid)
    return 0.5 * (lo + hi), ok


def solve_lambdas(
    p_ah: np.ndarray, lines: np.ndarray, p_over: np.ndarray, rho: float, chunk: int = 20000
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Solve (lambda_h, lambda_a) matching the AH fair cover prob at `lines`
    and fair P(over 2.5), under DC tau with `rho`. Returns (lh, la, ok).

    Chunked to bound peak memory (each bisection step builds (n, K, K))."""
    if p_ah.shape[0] > chunk:
        parts = [
            solve_lambdas(p_ah[i : i + chunk], lines[i : i + chunk], p_over[i : i + chunk], rho)
            for i in range(0, p_ah.shape[0], chunk)
        ]
        return (
            np.concatenate([p[0] for p in parts]),
            np.concatenate([p[1] for p in parts]),
            np.concatenate([p[2] for p in parts]),
        )
    n = p_ah.shape[0]
    # initial total-goals mean from the (rho=0) closed form: total ~ Poisson(m)
    m_lo = np.full(n, 0.05)
    m_hi = np.full(n, 12.0)

    def f_m_plain(mm: np.ndarray) -> np.ndarray:
        return (1.0 - np.exp(-mm) * (1.0 + mm + 0.5 * mm * mm)) - p_over

    m_tot, ok_m = _bisect(f_m_plain, m_lo, m_hi)
    d_sup = np.zeros(n)
    ok = ok_m
    for _ in range(2):  # (d | m) then (m | d), twice — tau coupling is tiny

        def f_d(dd: np.ndarray, m_tot: np.ndarray = m_tot) -> np.ndarray:
            lh = 0.5 * (m_tot + dd)
            la = 0.5 * (m_tot - dd)
            sup = _supremacy_pmf(_score_matrix(lh, la, rho))
            return _ah_effective_prob(sup, lines) - p_ah

        span = m_tot - 1e-4
        d_sup, ok_d = _bisect(f_d, -span, span)
        ok &= ok_d

        def f_m(mm: np.ndarray, d_sup: np.ndarray = d_sup) -> np.ndarray:
            lh = np.clip(0.5 * (mm + d_sup), 1e-6, None)
            la = np.clip(0.5 * (mm - d_sup), 1e-6, None)
            return _p_over25(_score_matrix(lh, la, rho)) - p_over

        m_tot, ok_m2 = _bisect(f_m, np.maximum(np.abs(d_sup) + 1e-4, 0.05), m_hi)
        ok &= ok_m2
    lh = np.clip(0.5 * (m_tot + d_sup), 1e-6, None)
    la = np.clip(0.5 * (m_tot - d_sup), 1e-6, None)
    # residual check — both market equations must be met tightly
    matrix = _score_matrix(lh, la, rho)
    res_ah = np.abs(_ah_effective_prob(_supremacy_pmf(matrix), lines) - p_ah)
    res_ou = np.abs(_p_over25(matrix) - p_over)
    ok &= (res_ah < 1e-4) & (res_ou < 1e-4)
    return lh, la, ok


def one_x_two_from_lambdas(
    lh: np.ndarray, la: np.ndarray, rho: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    m = _score_matrix(lh, la, rho)
    sup = _supremacy_pmf(m)
    k = K_GOALS - 1
    p_home = sup[:, k + 1 :].sum(axis=1)
    p_draw = sup[:, k]
    p_away = sup[:, :k].sum(axis=1)
    return p_home, p_draw, p_away


def fit_rho(lh: np.ndarray, la: np.ndarray, hg: np.ndarray, ag: np.ndarray) -> float:
    """MLE of the DC rho given per-match market lambdas and observed scores.

    Only the (0,0)/(0,1)/(1,0)/(1,1) cells depend on rho (tau preserves
    normalisation exactly), so the log-likelihood reduces to the tau factors
    at the observed low-score cells.
    """
    from scipy.optimize import minimize_scalar

    c00 = (hg == 0) & (ag == 0)
    c01 = (hg == 0) & (ag == 1)
    c10 = (hg == 1) & (ag == 0)
    c11 = (hg == 1) & (ag == 1)

    def nll(rho: float) -> float:
        ll = 0.0
        ll += np.log(np.clip(1.0 - lh[c00] * la[c00] * rho, 1e-10, None)).sum()
        ll += np.log(np.clip(1.0 + lh[c01] * rho, 1e-10, None)).sum()
        ll += np.log(np.clip(1.0 + la[c10] * rho, 1e-10, None)).sum()
        ll += np.log(np.clip(1.0 - rho, 1e-10, None)).sum() * int(c11.sum())
        return -ll

    res = minimize_scalar(nll, bounds=RHO_BOUNDS, method="bounded")
    return float(res.x)


# --------------------------------------------------------------------------
# Betfair close join (independent scoring leg)
# --------------------------------------------------------------------------


def attach_bsp_closes(fixtures: list[Fixture]) -> tuple[int, int]:
    """Attach the Betfair exchange close to each fixture where the strict
    canonical join matches. Returns (n_joined, n_markets)."""
    if not BSP_CACHE.is_file():
        print(f"(BSP cache absent at {BSP_CACHE} — Betfair leg skipped)")
        return 0, 0
    from app.ingestion.betfair_bsp import attach_betfair_close, read_market_cache
    from app.resolution.matching import default_aliases

    markets = [m for m in read_market_cache(BSP_CACHE) if m.market_type == "MATCH_ODDS"]
    rows = [
        {
            "_ridx": str(i),
            "HomeTeam": fx.home,
            "AwayTeam": fx.away,
            "Date": fx.kickoff_date.strftime("%d/%m/%Y"),
            "FTR": fx.ftr,
        }
        for i, fx in enumerate(fixtures)
    ]
    joined, stats = attach_betfair_close(rows, markets, aliases=default_aliases())
    n = 0
    for row in joined:
        closes = _odds3(row, ("PSCH", "PSCD", "PSCA"))
        if closes is None:
            continue
        fixtures[int(row["_ridx"])].bf_close = closes
        n += 1
    return n, stats.n_markets


# --------------------------------------------------------------------------
# selection + scoring
# --------------------------------------------------------------------------


@dataclass
class Pick:
    season: str
    kickoff_date: date
    arm: str  # v1 | v2
    sel: int  # 0=H 1=D 2=A
    price: float
    edge: float
    clv_primary: float | None  # vs BSP close where joined else Pinnacle close
    clv_pinnacle: float | None
    clv_bsp: float | None
    close_source: str  # bsp | pinnacle | none
    profit: float  # flat 1u at `price`


def _select(fair: tuple[float, float, float], mx: tuple[float, float, float]) -> int | None:
    best_i, best_edge = None, EDGE_MIN
    for i in range(3):
        price = mx[i]
        if not (ODDS_MIN <= price <= ODDS_MAX):
            continue
        edge = fair[i] * price - 1.0
        if edge >= best_edge:
            best_i, best_edge = i, edge
    return best_i


DEGENERATE_CLOSES = {"pinnacle": 0, "bsp": 0}  # counted per run, reported


def _close_probs(odds: tuple[float, float, float] | None, source: str) -> tuple[float, ...] | None:
    """Devigged close, or None when devig degenerates (underflow to 0, NaN).

    A degenerate close (e.g. power-devig underflow on an extreme exchange
    longshot) would inject -inf/NaN into a CLV mean — the kestrel-clv rule is
    drop-and-count, never silently average.
    """
    if odds is None:
        return None
    p = devig(odds, DEVIG)
    if all(np.isfinite(x) and 0.0 < x < 1.0 for x in p):
        return p
    DEGENERATE_CLOSES[source] += 1
    return None


def make_picks(fixtures: list[Fixture]) -> list[Pick]:
    picks: list[Pick] = []
    for fx in fixtures:
        assert fx.mx is not None
        p_close_pinn = _close_probs(fx.psc, "pinnacle")
        p_close_bsp = _close_probs(fx.bf_close, "bsp")
        for arm, fair in (("v1", fx.fair_v1), ("v2", fx.fair_v2)):
            assert fair is not None
            i = _select(fair, fx.mx)
            if i is None:
                continue
            price = fx.mx[i]
            clv_p = float(np.log(price * p_close_pinn[i])) if p_close_pinn else None
            clv_b = float(np.log(price * p_close_bsp[i])) if p_close_bsp else None
            if clv_b is not None:
                clv_primary, src = clv_b, "bsp"
            elif clv_p is not None:
                clv_primary, src = clv_p, "pinnacle"
            else:
                clv_primary, src = None, "none"
            won = "HDA".index(fx.ftr) == i
            picks.append(
                Pick(
                    season=fx.season,
                    kickoff_date=fx.kickoff_date,
                    arm=arm,
                    sel=i,
                    price=price,
                    edge=fair[i] * price - 1.0,
                    clv_primary=clv_primary,
                    clv_pinnacle=clv_p,
                    clv_bsp=clv_b,
                    close_source=src,
                    profit=(price - 1.0) if won else -1.0,
                )
            )
    return picks


# --------------------------------------------------------------------------
# inference: date-clustered bootstrap on the arm delta
# --------------------------------------------------------------------------


@dataclass
class ArmStats:
    n: int
    mean: float | None
    se_iid: float | None
    se_cl: float | None  # date-clustered


def arm_stats(values: list[float], dates: list[date]) -> ArmStats:
    n = len(values)
    if n == 0:
        return ArmStats(0, None, None, None)
    arr = np.asarray(values)
    se_iid = float(arr.std(ddof=1) / np.sqrt(n)) if n > 1 else None
    return ArmStats(n, float(arr.mean()), se_iid, cluster_robust_se(values, dates))


def boot_delta(
    v1: list[tuple[date, float]],
    v2: list[tuple[date, float]],
    rng: np.random.Generator,
) -> tuple[float, float, float] | None:
    """Mean(v2) - mean(v1) with a date-clustered bootstrap 95% CI."""
    if not v1 or not v2:
        return None
    days = sorted({d for d, _ in v1} | {d for d, _ in v2})
    idx = {d: i for i, d in enumerate(days)}
    g = len(days)

    def agg(rows: list[tuple[date, float]]) -> tuple[np.ndarray, np.ndarray]:
        s, c = np.zeros(g), np.zeros(g)
        for d, x in rows:
            s[idx[d]] += x
            c[idx[d]] += 1.0
        return s, c

    s1, c1 = agg(v1)
    s2, c2 = agg(v2)
    point = s2.sum() / c2.sum() - s1.sum() / c1.sum()
    deltas = np.full(B_BOOT, np.nan)
    for b in range(B_BOOT):
        take = rng.integers(0, g, size=g)
        cc1, cc2 = c1[take].sum(), c2[take].sum()
        if cc1 and cc2:
            deltas[b] = s2[take].sum() / cc2 - s1[take].sum() / cc1
    lo = float(np.nanpercentile(deltas, 2.5))
    hi = float(np.nanpercentile(deltas, 97.5))
    return point, lo, hi


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _pairs(picks: list[Pick], arm: str, attr: str) -> list[tuple[date, float]]:
    out = []
    for p in picks:
        if p.arm != arm:
            continue
        v = getattr(p, attr)
        if v is not None:
            out.append((p.kickoff_date, float(v)))
    return out


def _fmt_arm(s: ArmStats) -> str:
    if s.n == 0 or s.mean is None:
        return "n=0"
    cl = f" (cl2SE {2 * s.se_cl:.4f})" if s.se_cl is not None else ""
    return f"{s.mean:+.4f}{cl}"


def _delta_row(
    picks: list[Pick], attr: str, label: str
) -> tuple[ArmStats, ArmStats, tuple[float, float, float] | None]:
    """Deterministic per-(attr, subset) bootstrap: identical inputs always
    yield identical CIs (the verdict must match the pooled table row)."""
    rng = np.random.default_rng([SEED, zlib.crc32(f"{attr}|{label}".encode())])
    p1 = _pairs(picks, "v1", attr)
    p2 = _pairs(picks, "v2", attr)
    s1 = arm_stats([x for _, x in p1], [d for d, _ in p1])
    s2 = arm_stats([x for _, x in p2], [d for d, _ in p2])
    return s1, s2, boot_delta(p1, p2, rng)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="2 leagues, no BSP join (smoke)")
    ap.add_argument("--out", type=Path, default=REPORT_PATH)
    args = ap.parse_args(argv)
    t0 = time.time()
    leagues = ("E0", "D1") if args.quick else LEAGUES

    fixtures = load_fixtures(EVAL_SEASONS, leagues)
    n_loaded = len(fixtures)
    n_blackout = sum(1 for f in fixtures if f.kickoff_date >= PINNACLE_BLACKOUT and f.ps is None)
    # v1-anchorable universe: pre-match Pinnacle 1X2 + Max prices + any close path
    v1_universe = [f for f in fixtures if f.ps is not None and f.mx is not None]
    print(
        f"loaded {n_loaded} fixtures ({len(leagues)} leagues, seasons {EVAL_SEASONS[0]}..)"
        f" | v1-anchorable {len(v1_universe)}"
        f" | post-2026-01-15 rows without Pinnacle pre-match: {n_blackout}"
    )

    # ---- rho per eval season (expanding window, strictly past seasons) -----
    rho_by_season: dict[str, float] = {}
    fit_cache: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    all_fit = load_rho_fit_rows(max_season="9999", leagues=leagues)
    fit_seasons = sorted({r[0] for r in all_fit})
    for s in fit_seasons:
        rows = [r for r in all_fit if r[0] == s]
        p_ah = np.array([devig((r[2], r[3]), DEVIG)[0] for r in rows])
        p_ov = np.array([devig((r[4], r[5]), DEVIG)[0] for r in rows])
        lines = np.array([r[1] for r in rows])
        lh, la, ok = solve_lambdas(p_ah, lines, p_ov, rho=0.0)
        hg = np.array([r[6] for r in rows])
        ag = np.array([r[7] for r in rows])
        fit_cache[s] = (lh[ok], la[ok], hg[ok], ag[ok])
    for s in EVAL_SEASONS:
        past = [k for k in fit_cache if k < s]
        if not past:
            rho_by_season[s] = 0.0
            continue
        lh = np.concatenate([fit_cache[k][0] for k in past])
        la = np.concatenate([fit_cache[k][1] for k in past])
        hg = np.concatenate([fit_cache[k][2] for k in past])
        ag = np.concatenate([fit_cache[k][3] for k in past])
        rho_by_season[s] = fit_rho(lh, la, hg, ag)
    print("rho by season:", {k: round(v, 4) for k, v in rho_by_season.items()})

    # ---- anchors ------------------------------------------------------------
    for fx in v1_universe:
        fx.fair_v1 = devig(fx.ps, DEVIG)  # type: ignore[arg-type]
    v2_candidates = [f for f in v1_universe if f.ah is not None and f.ou is not None]
    n_solved = 0
    for s in EVAL_SEASONS:
        batch = [f for f in v2_candidates if f.season == s]
        if not batch:
            continue
        rho = rho_by_season[s]
        p_ah = np.array([devig((f.ah[1], f.ah[2]), DEVIG)[0] for f in batch])
        p_ov = np.array([devig(f.ou, DEVIG)[0] for f in batch])
        lines = np.array([f.ah[0] for f in batch])
        lh, la, ok = solve_lambdas(p_ah, lines, p_ov, rho)
        ph, pd_, pa = one_x_two_from_lambdas(lh, la, rho)
        for j, f in enumerate(batch):
            if ok[j]:
                f.fair_v2 = (float(ph[j]), float(pd_[j]), float(pa[j]))
                n_solved += 1
    coverage = n_solved / len(v1_universe) if v1_universe else 0.0
    print(
        f"AH+OU present on {len(v2_candidates)}/{len(v1_universe)} v1-anchorable fixtures; "
        f"solver converged {n_solved} -> anchor coverage {coverage:.1%}"
    )

    # ---- common universe, BSP close, picks ----------------------------------
    common = [f for f in v1_universe if f.fair_v2 is not None]
    n_bsp = 0
    if not args.quick:
        n_bsp, n_markets = attach_bsp_closes(common)
        print(
            f"Betfair close joined on {n_bsp}/{len(common)} common fixtures "
            f"(from {n_markets} cached MATCH_ODDS markets)"
        )
    picks = make_picks(common)
    n1 = sum(1 for p in picks if p.arm == "v1")
    n2 = sum(1 for p in picks if p.arm == "v2")
    print(f"picks: v1={n1} v2={n2} (edge>={EDGE_MIN}, odds [{ODDS_MIN},{ODDS_MAX}], 1/match)")
    print(f"degenerate closes dropped (devig underflow/NaN): {DEGENERATE_CLOSES}")

    # ---- tables --------------------------------------------------------------
    lines_out: list[str] = []
    w = lines_out.append
    w("# AH-Anchored Soccer Fair vs Power-Devig 1X2 Fair — Walk-Forward CLV Study")
    w("")
    w("**Date:** 2026-07-10  ·  **Author:** quant-backtest-engineer (agent)")
    w(f"**Script:** `scripts/research/ah_anchor_backtest.py` (seed {SEED}, B={B_BOOT})")
    w("**Design source:** docs/research/2026-07-10-litx-strategy-sweep.md, idea #1")
    w("(Hegarty & Whelan — 1X2 odds carry favourite-longshot bias, AH odds do not).")
    w("")
    w("## Status — exploratory, NOT confirmatory")
    w("")
    w("The 1920-2526 football-data seasons have been consulted by earlier studies of")
    w("other hypotheses (value_backtest sweeps; the AH-market *betting* one-shot")
    w("consumed 2425+2526 for that separate question), and the BSP 2025 holdout is")
    w("spent for the ADR-0019 hypotheses. This study asks a NEW question (anchor")
    w("replacement for 1X2 pick selection) but runs on previously-seen data, so the")
    w("strongest verdict available here is PROMISING-PREREGISTER. No thresholds were")
    w("tuned: the selection rule is the deployed premium doctrine, frozen up front")
    w(f"(edge >= {EDGE_MIN}, odds in [{ODDS_MIN}, {ODDS_MAX}], one pick per match,")
    w("power devig everywhere). The only fitted parameter (Dixon-Coles rho) is fitted")
    w("per season on strictly earlier seasons (expanding window back to 1213).")
    w("")
    w("## Method")
    w("")
    w("- **v1 (baseline) anchor:** power-devig(PSH, PSD, PSA) — deployed behaviour.")
    w("- **v2 (candidate) anchor:** power-devig 2-way (PAHH, PAHA) = fair AH cover")
    w("  probability at line AHh; power-devig(P>2.5, P<2.5) = fair P(total >= 3).")
    w("  Solve independent-Poisson + Dixon-Coles-tau for (lambda_h, lambda_a)")
    w("  matching both fair probabilities (quarter lines split into two half-stakes;")
    w("  the 2-way devig recovers exactly the push-adjusted W/(W+L) the model")
    w("  matches). 1X2 fair read off the score matrix (goals 0..15, renormalised).")
    w("- **Universe:** fixtures where BOTH anchors exist (coverage reported below);")
    w("  22 football-data leagues, seasons 1920-2526. All anchor inputs pre-match;")
    w("  closing columns score CLV only.")
    w("- **Selection (both arms, identical):** H/D/A at the pre-match Max price,")
    w("  edge = fair x price - 1 >= 0.03, price in [1.6, 4.0], argmax-edge one pick")
    w("  per match.")
    w("- **CLV:** clv = ln(price x p_close_fair) (app/backtesting/clv.py). Primary")
    w("  close = Betfair exchange close where the strict canonical join matches,")
    w("  else power-devig Pinnacle close; each leg also reported separately. The")
    w("  Pinnacle-close leg is not independent of the hypothesis: if the 1X2 close")
    w("  retains the favourite-longshot bias, it penalises the arm correcting it.")
    w("- **Inference:** per-season and pooled delta (v2 - v1) with a match-day")
    w("  (date-)clustered bootstrap 95% CI; date-clustered SEs (cl2SE) per arm;")
    w("  flat-stake ROI at the Max price as the secondary truth check.")
    w("")
    w("## Data honesty — n up front")
    w("")
    w(
        f"- Fixtures loaded (seasons {EVAL_SEASONS[0]}-{EVAL_SEASONS[-1]}, "
        f"{len(leagues)} leagues): {n_loaded}"
    )
    w(f"- v1-anchorable (pre-match Pinnacle 1X2 + Max prices): {len(v1_universe)}")
    w(f"- AH+OU pre-match present: {len(v2_candidates)}; solver converged: {n_solved}")
    w(f"- **Anchor coverage: {coverage:.1%}** (success bar >= 80%)")
    w(f"- Betfair close joined: {n_bsp}/{len(common)} common fixtures")
    w(
        f"- Degenerate closes dropped (devig underflow/NaN, per source): "
        f"pinnacle={DEGENERATE_CLOSES['pinnacle']}, bsp={DEGENERATE_CLOSES['bsp']} "
        "(a degenerate close falls back to the other close source for the primary "
        "metric; it is never averaged as -inf/NaN)"
    )
    w(f"- 2026-01-15 Pinnacle blackout: {n_blackout} rows on/after the blackout have")
    w("  no pre-match Pinnacle columns and drop out of BOTH arms (the 2526 season")
    w("  is effectively its first half only — reported, not imputed).")
    w("- Dixon-Coles rho (walk-forward, fitted on strictly earlier seasons):")
    w("  " + ", ".join(f"{k}={v:+.4f}" for k, v in rho_by_season.items()))
    w("")

    def season_table(attr: str, title: str) -> None:
        w(f"## {title}")
        w("")
        w("| season | n v1 | n v2 | mean CLV v1 | mean CLV v2 | delta v2-v1 | 95% CI (date-boot) |")
        w("|---|---|---|---|---|---|---|")
        for s in (*EVAL_SEASONS, "POOLED"):
            sub = picks if s == "POOLED" else [p for p in picks if p.season == s]
            s1, s2, d = _delta_row(sub, attr, s)
            if d is None:
                w(f"| {s} | {s1.n} | {s2.n} | {_fmt_arm(s1)} | {_fmt_arm(s2)} | n/a | n/a |")
                continue
            point, lo, hi = d
            star = " **excludes 0**" if (lo > 0 or hi < 0) else ""
            w(
                f"| {s} | {s1.n} | {s2.n} | {_fmt_arm(s1)} | {_fmt_arm(s2)} "
                f"| {point:+.4f} | [{lo:+.4f}, {hi:+.4f}]{star} |"
            )
        w("")

    season_table("clv_primary", "Primary CLV (BSP close where joined, else Pinnacle close)")
    season_table("clv_bsp", "Betfair-close leg only (independent close)")
    season_table("clv_pinnacle", "Pinnacle-close leg only (NOT hypothesis-independent)")
    season_table("profit", "Flat-stake ROI (profit units per pick, secondary)")

    # odds bands (pooled, primary close)
    w("## Pooled by odds band (primary close)")
    w("")
    w("| band | n v1 | n v2 | mean CLV v1 | mean CLV v2 | delta | 95% CI |")
    w("|---|---|---|---|---|---|---|")
    for lo_b, hi_b in ODDS_BANDS:
        sub = [p for p in picks if lo_b <= p.price < hi_b]
        s1, s2, d = _delta_row(sub, "clv_primary", f"band{lo_b}")
        if d is None:
            w(
                f"| [{lo_b},{hi_b}) | {s1.n} | {s2.n} "
                f"| {_fmt_arm(s1)} | {_fmt_arm(s2)} | n/a | n/a |"
            )
            continue
        point, lo, hi = d
        w(
            f"| [{lo_b},{hi_b}) | {s1.n} | {s2.n} | {_fmt_arm(s1)} | {_fmt_arm(s2)} "
            f"| {point:+.4f} | [{lo:+.4f}, {hi:+.4f}] |"
        )
    w("")

    # selection agreement + close-source split
    w("## Selection agreement")
    w("")
    both = same = 0
    for fx in common:
        assert fx.fair_v1 is not None and fx.fair_v2 is not None and fx.mx is not None
        i1 = _select(fx.fair_v1, fx.mx)
        i2 = _select(fx.fair_v2, fx.mx)
        if i1 is not None and i2 is not None:
            both += 1
            same += int(i1 == i2)
    w(
        f"- fixtures where both arms pick: {both}; same selection: {same} ({same / both:.1%})"
        if both
        else "- no overlapping picks"
    )
    n_src_bsp = sum(1 for p in picks if p.close_source == "bsp")
    n_src_pin = sum(1 for p in picks if p.close_source == "pinnacle")
    n_src_none = sum(1 for p in picks if p.close_source == "none")
    w(f"- primary-close source split: bsp={n_src_bsp}, pinnacle={n_src_pin}, unscored={n_src_none}")
    w("")

    # verdict — same deterministic bootstrap as the POOLED primary-close row
    s1, s2, d = _delta_row(picks, "clv_primary", "POOLED")
    verdict = "INSUFFICIENT-DATA"
    detail = ""
    if d is not None and s1.n >= 200 and s2.n >= 200:
        point, lo, hi = d
        cov_ok = coverage >= 0.80
        if lo > 0 and cov_ok:
            verdict = "PROMISING-PREREGISTER"
            detail = "delta CI excludes 0 on the primary close and coverage >= 80%."
        elif not cov_ok:
            verdict = "NO-EFFECT (coverage fail)" if lo <= 0 else "INSUFFICIENT-DATA"
            detail = f"anchor coverage {coverage:.1%} < 80% success bar."
            if lo > 0:
                detail += " Delta CI excludes 0, but the coverage bar fails."
        elif hi < 0:
            verdict = "NO-EFFECT"
            detail = (
                "pooled delta 95% CI excludes 0 on the NEGATIVE side — the AH anchor "
                "is significantly WORSE than the deployed 1X2 anchor on this close."
            )
        else:
            verdict = "NO-EFFECT"
            detail = "pooled delta 95% CI straddles 0 on the primary close."
    w("## Verdict")
    w("")
    if d is not None:
        point, lo, hi = d
        w(f"**{verdict}** — pooled primary-close CLV delta (v2 - v1) = {point:+.4f}")
        w(f"[{lo:+.4f}, {hi:+.4f}] (date-clustered bootstrap, B={B_BOOT}); anchor")
        w(f"coverage {coverage:.1%}. {detail}")
    else:
        w(f"**{verdict}** — no scoreable picks.")
    w("")
    w("If PROMISING-PREREGISTER: next step is an operator-signed pre-registration")
    w("(fresh 2026-27 season + live shadow, per the shadow-first mandate) BEFORE any")
    w("gate change; nothing in this study alters production behaviour.")
    w("")
    w(f"_Runtime {time.time() - t0:.0f}s. Decision-support only — no bets placed._")

    args.out.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    print(f"\nreport -> {args.out}")
    print(f"VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
