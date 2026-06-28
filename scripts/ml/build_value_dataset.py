"""Build the leakage-safe VALUE-CANDIDATE training dataset (one parquet).

Doctrine (docs/backtesting/value-findings.md, .claude/memory): the edge is
sharp-vs-soft line shopping. ML here means META-MODELING the value signal —
learning which candidates (best pre-match price beats devigged-Pinnacle
pre-match fair) deliver positive held-out CLV/ROI. This script only ASSEMBLES
the candidate pool; it never selects, never tunes, never touches a holdout.

One output row per (match, market in {1x2, ou25}, selection) where
    edge = p_fair_sharp[i] - 1/best_price[i] >= --min-edge (default 0.005).

Column eras (verified inventory, football-data.co.uk mmz4281 files):
  - PSH/PSD/PSA (Pinnacle pre-match) exist from 2012/13 -> seasons 1213+.
  - Best pre-match price: Max*  (2019/20+, "maxavg" era) or BbMx*
    (BetBrain consensus, 2005/06-2018/19, "betbrain" era). The two pools
    differ -> the `era` regime flag is a SIGNAL feature.
  - ou25 needs Pinnacle pre-match OU (P>2.5/P<2.5), which exists 1920+ only;
    no ou25 candidates exist in the betbrain era.
  - CLV labels: devig(PSC*) from 1213; devig(MaxC*) from 1920 only ->
    clv_max is null in the betbrain era.
  - PSH/B365H/BbMx* are football-data's collection-time snapshot (~T-1 day),
    NOT a true opener. They are signal-time-safe; `open_to_signal_drift`
    is therefore always null for this source (no opening column exists) and
    is kept only so future sources (NBA open_spread, odds_snapshots) share
    the schema.

Leakage contract: every column is classified ID / SIGNAL / LABEL in SCHEMA
below. Close-derived and outcome-derived columns are LABELs; a LABEL column
appearing among features is a build-breaking defect (assert_no_label_leak,
locked by tests/test_ml_dataset.py). ADR-0006: fill-side and close-side fair
probabilities use the SAME devig method (DIFFERENTIAL_MARGIN, the production
v4 default).

v2 EXTENSIONS (2026-06-12) — additive SIGNAL features + fresh-domain tagging:
  - Rolling pre-match form: shift(1)-equivalent per-team rolling means of
    PAST matches' goals/shots/SOT/corners (raw per-match stats are outcomes
    = LABEL class; only strictly-prior aggregates are SIGNAL). Computed from
    every match in the league-season file, not just candidate matches.
  - Favourite-longshot-bias features: odds_band buckets + fair_prob_rank
    (Buchdahl: the FLB lives in how margin distributes across odds levels).
  - Anchor-disagreement v2: best-vs-Pinnacle price ratio / prob gap, plus
    per-method devig deltas vs the canonical method (extends devig_spread).
  - Understat xG rolling features (big-5 leagues, 2014/15+): r5/r10 xG
    for/against + xG overperformance (goals - xG, mean-reversion signal).
    Nullable everywhere else — LightGBM handles NaN natively. Team-name
    resolution is the explicit UNDERSTAT_TO_FOOTBALL_DATA table below;
    unmatched names quarantine with a log line, never fuzzy-merged.
  - consulted_before tag (ID class): True iff the league is in the
    18-league universe consumed by every prior backtest/training run
    (docs/research/ml-value-filter.md section 2; scripts/value_backtest.py
    default; trainer LEAGUES_18). Fresh never-consulted divisions: EC + SC1
    (assembled into v1's parquet but excluded from ALL evaluation by the
    trainer's LEAGUES_18 filter) and SC2 + SC3 (never downloaded before).
    The football-data "new leagues" feed (BRA/ARG/...) is excluded: it
    carries closing odds only — no signal-time price, protocol impossible.

v3 EXTENSIONS (2026-06-12, additive — v1/v2 outputs untouched without flags):
  - --anchor-consensus: also emit CONSENSUS-ANCHORED 1x2 candidates. Fair =
    devig(median of per-book pre-match odds across soft books, EXCLUDING
    Pinnacle so the anchor is independent of the Pinnacle labels, and
    EXCLUDING BFE = Betfair Exchange, whose quotes embed commission).
    Mirrors the live fallback in app/edge/value.py::_consensus_anchor
    (median across >= 3 full-market books, overround sanity gate).
    Candidate when best price beats consensus fair by >= --min-edge.
    ou25 consensus is IMPOSSIBLE in this source: the only non-Pinnacle
    per-book OU columns are B365>2.5/<2.5 (one book < 3) — documented scope
    limit, not a bug. Each row is tagged `anchor_type` (pinnacle/consensus).
  - --ah: also emit ASIAN HANDICAP candidates (market="ah", maxavg era only
    — PAHH/PAHA exist 1920+). Main line AHh, anchor devig(PAHH, PAHA)
    (2-way), best price MaxAHH/MaxAHA, closes PCAHH/PCAHA + MaxCAHH/MaxCAHA.
    HALF-LINE ROWS ONLY (line*2 odd): integer lines push and quarter lines
    split stakes (app/settlement/outcomes.py, app/models/ah_bridge.py);
    the simplest honest scope is the push-free half-line subset (~23% of
    AH-priced matches — the AH_COVERAGE counters print the exact fraction
    per build). Close labels (pinn_close_fair/max_close_fair/clv_*) are
    emitted ONLY when the closing line AHCh equals the pre-match line AHh —
    a close on a different line is a different bet and labeling against it
    would be wrong. `ah_line` carries the (home) line, SIGNAL class.
  - Either flag -> schema v3 (adds anchor_type + ah_line columns) and the
    default output moves to value_candidates_v3.parquet. Without the flags
    the emitted rows, columns, and ordering of --v1 / v2 are byte-identical
    to before (SCHEMA_V1 / SCHEMA_V2 frozen tuples, locked by tests).

SPENT-HOLDOUT DISCIPLINE (binding): seasons 2425+2526 are SPENT (consulted
4x) for the 1x2/ou25 markets. Any number computed on them is
CONTAMINATED-REFERENCE, never a headline. Development happens on nested
season-blocked walk-forward within <=2324; fresh evaluation only on
never-consulted domains; binding verdict = live shadow CLV + the one-shot
fresh 2627 season. The AH market has never been consulted in any season —
its 2425+2526 slice is the legitimate fresh domain for the ONE pre-registered
AH one-shot (scripts/ml/anchor_ah_backtest.py), after which it too is spent.

Run (downloads are cached under data/ml/cache/ -- read-only GETs):

    uv run python scripts/ml/build_value_dataset.py                  # v2
    uv run python scripts/ml/build_value_dataset.py --v1             # v1 repro
    uv run python scripts/ml/build_value_dataset.py --no-understat   # offline v2
    uv run python scripts/ml/build_value_dataset.py --leagues E0,D1 --seasons 2425

Output: data/ml/value_candidates_v2.parquet (v2 default) or
data/ml/value_candidates.parquet (--v1, identical column set + row content to
the original builder). data/ is gitignored; raw artifacts are never committed.

Decision-support only — nothing here places bets.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import io
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from app.backtesting.clv import clv_log
from app.ingestion.beatthebookie_series import SeriesMatch, load_series_dir
from app.ingestion.football_data import LEAGUES, fetch_season_csv
from app.probabilities.devig import DevigMethod, devig

UK_TZ = ZoneInfo("Europe/London")  # football-data Date/Time are UK local

# Production v4 premium-tier default (app/config.py); ADR-0006 demands the
# same method on the fill side (fair_prob/edge) and the close side (labels).
CANONICAL_DEVIG = DevigMethod.DIFFERENTIAL_MARGIN
# Disagreement-spread feature: structurally different methods only.
SPREAD_METHODS = (DevigMethod.MULTIPLICATIVE, DevigMethod.SHIN, DevigMethod.POWER)

MIN_EDGE_DEFAULT = 0.005  # wide pool — the harness evaluates filters on top

# 1213..2526: Pinnacle pre-match (PSH) exists from 2012/13 per the verified
# column-era inventory. Earlier seasons have no sharp anchor -> not viable.
SEASONS_ALL = [f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(2012, 2026)]

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DEFAULT_V1 = REPO_ROOT / "data" / "ml" / "value_candidates.parquet"
OUT_DEFAULT_V2 = REPO_ROOT / "data" / "ml" / "value_candidates_v2.parquet"
OUT_DEFAULT_V3 = REPO_ROOT / "data" / "ml" / "value_candidates_v3.parquet"
OUT_DEFAULT_BTB = REPO_ROOT / "data" / "ml" / "value_candidates_btb.parquet"
OUT_DEFAULT = OUT_DEFAULT_V1  # back-compat alias (v1 artifact path)
# Operator-placed BeatTheBookie odds_series dirs (see app/ingestion/beatthebookie_series)
BTB_DIR_DEFAULT = REPO_ROOT / "data" / "beatthebookie"
CACHE_DEFAULT = REPO_ROOT / "data" / "ml" / "cache"

# ---------------------------------------------------------------------------
# v3 — anchor types + consensus-anchor configuration (Track A, 2026-06-12)
# ---------------------------------------------------------------------------
ANCHOR_PINNACLE = "pinnacle"
ANCHOR_CONSENSUS = "consensus"

# Per-book 1x2 column prefixes eligible for the historical consensus anchor.
# PS (Pinnacle) is EXCLUDED so the anchor is independent of the Pinnacle
# close labels; BFE (Betfair Exchange) is EXCLUDED because its quotes embed
# commission (the live path nets it out; the CSVs cannot). Composites
# (Max/Avg/BbMx/BbAv) and closing (*C*) columns are never consensus inputs.
CONSENSUS_BOOKS: tuple[str, ...] = (
    "B365",
    "BW",
    "IW",
    "WH",
    "VC",
    "LB",
    "SJ",
    "GB",
    "BS",
    "SO",
    "SB",
    "1XB",
    "BF",
    "CL",
)
MIN_CONSENSUS_BOOKS = 3  # parity with app/edge/value.py::MIN_CONSENSUS_BOOKS
CONSENSUS_MAX_OVERROUND = 0.12  # parity with the live anchor sanity gate

# ---------------------------------------------------------------------------
# v3 — Asian handicap market configuration (Track B, 2026-06-12)
# ---------------------------------------------------------------------------
AH_LINE_COL = "AHh"  # pre-match main line (home handicap)
AH_PINN_COLS = ("PAHH", "PAHA")  # SIGNAL: Pinnacle pre-match AH (1920+)
AH_BEST_COLS = ("MaxAHH", "MaxAHA")  # SIGNAL: Max-of-books pre-match AH
AH_CLOSE_LINE_COL = "AHCh"  # closing main line — labels only when == AHh
AH_PINN_CLOSE_COLS = ("PCAHH", "PCAHA")  # LABEL: Pinnacle close
AH_MAX_CLOSE_COLS = ("MaxCAHH", "MaxCAHA")  # LABEL: Max close
AH_SELECTIONS = ("home", "away")

# Build-scoped AH coverage counters (reset per build; printed in the quality
# report so the half-line-only scope fraction is documented per run).
AH_COVERAGE: dict[str, int] = {}


def _reset_ah_coverage() -> None:
    AH_COVERAGE.clear()
    AH_COVERAGE.update(
        priced=0,  # matches with a parseable AHh + full PAH/MaxAH pre-match
        half_line=0,  # ... of which the line is a push-free half line
        skipped_integer_or_quarter=0,  # excluded by the half-line doctrine
        close_line_matched=0,  # half-line matches whose AHCh == AHh
    )


_reset_ah_coverage()

# Frozen v1 league universe (the LEAGUES dict at v1 build time). --v1 must
# reproduce the original artifact, so this list never grows.
V1_LEAGUES: tuple[str, ...] = (
    "E0",
    "E1",
    "E2",
    "E3",
    "EC",
    "SC0",
    "SC1",
    "D1",
    "D2",
    "I1",
    "I2",
    "SP1",
    "SP2",
    "F1",
    "F2",
    "N1",
    "B1",
    "P1",
    "T1",
    "G1",
)

# The 18-league universe consumed by EVERY prior evaluation (value_backtest
# default + trainer LEAGUES_18, docs/research/ml-value-filter.md section 2).
# Rows outside this set are never-consulted fresh domains (EC/SC1 were
# assembled into the v1 parquet but filtered out of all training/eval;
# SC2/SC3 were never downloaded at all).
CONSULTED_LEAGUES: frozenset[str] = frozenset(
    {
        "E0",
        "E1",
        "E2",
        "E3",
        "SC0",
        "D1",
        "D2",
        "I1",
        "I2",
        "SP1",
        "SP2",
        "F1",
        "F2",
        "N1",
        "B1",
        "P1",
        "T1",
        "G1",
    }
)

# ---------------------------------------------------------------------------
# Understat xG enrichment (big-5 top flights; Understat coverage 2014/15+).
# penaltyblog.scrapers.Understat verified locally 2026-06-11 (memory note).
# ---------------------------------------------------------------------------
UNDERSTAT_COMP: dict[str, str] = {
    "E0": "ENG Premier League",
    "SP1": "ESP La Liga",
    "D1": "DEU Bundesliga 1",
    "I1": "ITA Serie A",
    "F1": "FRA Ligue 1",
}
UNDERSTAT_FIRST_END_YEAR = 15  # season "1415" is the first with Understat xG

# Versioned entity-resolution table: Understat team name -> football-data
# team name. Derived 2026-06-12 by diffing the complete distinct-name sets of
# both sources over seasons 1415-2526 (cached files); identity matches are
# omitted (the join falls back to the raw name). Unmatched names QUARANTINE
# with a log line — no fuzzy auto-merge in the hot path (ingestion rule).
UNDERSTAT_TO_FOOTBALL_DATA: dict[str, str] = {
    # E0
    "Manchester City": "Man City",
    "Manchester United": "Man United",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nott'm Forest",
    "Queens Park Rangers": "QPR",
    "West Bromwich Albion": "West Brom",
    "Wolverhampton Wanderers": "Wolves",
    # SP1
    "Athletic Club": "Ath Bilbao",
    "Atletico Madrid": "Ath Madrid",
    "Celta Vigo": "Celta",
    "Deportivo La Coruna": "La Coruna",
    "Espanyol": "Espanol",
    "Rayo Vallecano": "Vallecano",
    "Real Betis": "Betis",
    "Real Oviedo": "Oviedo",
    "Real Sociedad": "Sociedad",
    "Real Valladolid": "Valladolid",
    "SD Huesca": "Huesca",
    "Sporting Gijon": "Sp Gijon",
    # D1
    "Arminia Bielefeld": "Bielefeld",
    "Bayer Leverkusen": "Leverkusen",
    "Borussia Dortmund": "Dortmund",
    "Borussia M.Gladbach": "M'gladbach",
    "Eintracht Frankfurt": "Ein Frankfurt",
    "FC Cologne": "FC Koln",
    "FC Heidenheim": "Heidenheim",
    "Fortuna Duesseldorf": "Fortuna Dusseldorf",
    "Greuther Fuerth": "Greuther Furth",
    "Hamburger SV": "Hamburg",
    "Hannover 96": "Hannover",
    "Hertha Berlin": "Hertha",
    "Mainz 05": "Mainz",
    "Nuernberg": "Nurnberg",
    "RasenBallsport Leipzig": "RB Leipzig",
    "St. Pauli": "St Pauli",
    "VfB Stuttgart": "Stuttgart",
    # I1
    "AC Milan": "Milan",
    "Parma Calcio 1913": "Parma",
    "SPAL 2013": "Spal",
    # F1
    "Clermont Foot": "Clermont",
    "GFC Ajaccio": "Ajaccio GFCO",
    "Paris Saint Germain": "Paris SG",
    "SC Bastia": "Bastia",
    "Saint-Etienne": "St Etienne",
}

# Per-book home-price columns used as a book-count proxy in the maxavg era
# (the betbrain era carries an explicit Bb1X2 book count).
BOOK_HOME_COLS = (
    "B365H",
    "BWH",
    "IWH",
    "PSH",
    "WHH",
    "VCH",
    "LBH",
    "SBH",
    "GBH",
    "SJH",
    "BSH",
    "SOH",
    "1XBH",
    "BFH",
    "BFEH",
    "CLH",
)


def _f(x: object) -> float | None:
    """Odds parser — parity with scripts/value_backtest.py::_f (rejects <=1.0)."""
    try:
        v = float(str(x))
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


def _line(x: object) -> float | None:
    """Handicap-line parser: any finite float (lines are signed, often <= 0)."""
    raw = str(x).strip() if x is not None else ""
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _is_half_line(line: float) -> bool:
    """True for push-free half lines (x.5): line*2 is an ODD integer.

    Integer lines push and quarter lines split stakes — both excluded by the
    half-line-only doctrine (module docstring)."""
    doubled = line * 2.0
    return doubled.is_integer() and not line.is_integer()


def _consensus_odds(r: dict[str, str], suffixes: Sequence[str]) -> list[float] | None:
    """Median pre-match odds per selection across non-Pinnacle soft books.

    Mirrors app/edge/value.py::_consensus_anchor: only books pricing the FULL
    market participate; >= MIN_CONSENSUS_BOOKS required; the median market
    must pass the live overround sanity gate (0 <= overround <= 0.12) or the
    match has no trustworthy consensus anchor. Returns None when no anchor.
    """
    complete: list[list[float]] = []
    for book in CONSENSUS_BOOKS:
        odds_opt = [_f(r.get(f"{book}{s}")) for s in suffixes]
        if None not in odds_opt:
            complete.append([v for v in odds_opt if v is not None])
    if len(complete) < MIN_CONSENSUS_BOOKS:
        return None
    med = [statistics.median(book_odds[i] for book_odds in complete) for i in range(len(suffixes))]
    overround = sum(1.0 / o for o in med) - 1.0
    if not 0.0 <= overround <= CONSENSUS_MAX_OVERROUND:
        return None
    return med


# --------------------------------------------------------------------------
# Market specs — column maps verified against the data inventory.
# --------------------------------------------------------------------------
def _won_1x2(r: dict[str, str], i: int) -> bool | None:
    ftr = r.get("FTR")
    if ftr not in ("H", "D", "A"):
        return None
    return ftr == ("H", "D", "A")[i]


def _won_ou25(r: dict[str, str], i: int) -> bool | None:
    try:
        goals = int(r["FTHG"]) + int(r["FTAG"])
    except (KeyError, TypeError, ValueError):
        return None
    return (goals >= 3) if i == 0 else (goals <= 2)


@dataclass(frozen=True)
class MarketSpec:
    pinn_cols: tuple[str, ...]  # SIGNAL: Pinnacle pre-match
    best_by_era: tuple[tuple[str, tuple[str, ...]], ...]  # SIGNAL, first match wins
    pinn_close_cols: tuple[str, ...]  # LABEL: Pinnacle close
    max_close_cols: tuple[str, ...]  # LABEL: Max-of-books close (maxavg era only)
    selections: tuple[str, ...]
    won_fn: Callable[[dict[str, str], int], bool | None]


MARKETS: dict[str, MarketSpec] = {
    "1x2": MarketSpec(
        pinn_cols=("PSH", "PSD", "PSA"),
        best_by_era=(
            ("maxavg", ("MaxH", "MaxD", "MaxA")),
            ("betbrain", ("BbMxH", "BbMxD", "BbMxA")),
        ),
        pinn_close_cols=("PSCH", "PSCD", "PSCA"),
        max_close_cols=("MaxCH", "MaxCD", "MaxCA"),
        selections=("H", "D", "A"),
        won_fn=_won_1x2,
    ),
    "ou25": MarketSpec(
        pinn_cols=("P>2.5", "P<2.5"),
        best_by_era=(("maxavg", ("Max>2.5", "Max<2.5")),),  # no betbrain Pinnacle OU
        pinn_close_cols=("PC>2.5", "PC<2.5"),
        max_close_cols=("MaxC>2.5", "MaxC<2.5"),
        selections=("over", "under"),
        won_fn=_won_ou25,
    ),
}

# --------------------------------------------------------------------------
# Leakage contract: ID / SIGNAL / LABEL classification for every column.
# SIGNAL = available at pick time (pre-match snapshot). LABEL = close- or
# outcome-derived; references only, NEVER features.
# --------------------------------------------------------------------------
# Rolling-form features (v2): shift(1)-equivalent per-team rolling means of
# strictly-prior matches in the same league-season. Goals get r5+r10; the
# stats columns (eras vary by division: 1213+ in E0..SC0, 1718+ elsewhere)
# get r5. Nullable: first match of a season and stat-less files yield None.
_FORM_FEATS: tuple[str, ...] = (
    "gf_r5",
    "ga_r5",
    "gf_r10",
    "ga_r10",
    "shots_for_r5",
    "shots_against_r5",
    "sot_for_r5",
    "sot_against_r5",
    "corners_for_r5",
    "corners_against_r5",
)
# Understat xG rolling features (v2): big-5 leagues 1415+ only, else null.
_XG_FEATS: tuple[str, ...] = (
    "xg_for_r5",
    "xg_against_r5",
    "xg_for_r10",
    "xg_against_r10",
    "xg_overperf_r10",  # mean(goals_for - xg_for) last 10 — mean-reversion signal
)

SCHEMA: dict[str, str] = {
    # identifiers (join keys, split keys — not model features, not labels)
    "league": "ID",
    "season": "ID",
    "match_date": "ID",  # UK-local match date (football-data join key)
    "kickoff_utc": "ID",  # Date+Time (UK local) -> UTC; null pre-2019/20 (no Time col)
    "home_team": "ID",
    "away_team": "ID",
    "market": "ID",
    "selection": "ID",
    "consulted_before": "ID",  # v2 split key: league in CONSULTED_LEAGUES (18)
    "anchor_type": "ID",  # v3 split key: pinnacle | consensus (anchor that priced fair)
    # SIGNAL features — pre-match only
    "era": "SIGNAL",  # best-price pool regime: maxavg (1920+) vs betbrain (1213-1819)
    "fair_prob": "SIGNAL",  # devig(ANCHOR pre-match)[i], CANONICAL_DEVIG (anchor_type says which)
    "edge": "SIGNAL",  # fair_prob - 1/best_price
    "best_price": "SIGNAL",  # odds level: best pre-match price for the selection
    "pinn_price": "SIGNAL",  # Pinnacle pre-match price; null on consensus rows w/o PS*
    "overround_pinn": "SIGNAL",  # sum(1/PS*) - 1; null on consensus rows w/o PS*
    "overround_best": "SIGNAL",  # sum(1/best*) - 1 (can be < 0: composite line)
    "devig_spread": "SIGNAL",  # max pairwise |fair_i| diff across SPREAD_METHODS
    "open_to_signal_drift": "SIGNAL",  # always null here: football-data has no opener
    "book_count": "SIGNAL",  # Bb1X2 (betbrain) | count of per-book 1x2 cols (maxavg)
    "selection_type": "SIGNAL",  # fav/draw/dog by fair-prob rank within the market
    "day_of_week": "SIGNAL",  # 0=Mon .. 6=Sun
    "days_to_season_end": "SIGNAL",  # vs nominal June 30 (fixed, no schedule leak)
    "is_argmax_edge": "SIGNAL",  # highest-edge selection within (match, market, anchor)
    "ah_line": "SIGNAL",  # v3: AH (home) main line, half-lines only; null off-market
    # SIGNAL v2 — favourite-longshot-bias structure (odds level, pre-match)
    "odds_band": "SIGNAL",  # best_price bucket: (1,1.5] (1.5,2] (2,3] (3,5] (5,10] (10,inf)
    "fair_prob_rank": "SIGNAL",  # 1 = highest fair_prob within (match, market)
    # SIGNAL v2 — anchor disagreement (Pinnacle vs best, devig-method spread)
    "price_ratio_best_pinn": "SIGNAL",  # best_price / pinn_price; null w/o PS*
    "prob_gap_pinn_best": "SIGNAL",  # 1/pinn_price - 1/best_price; null w/o PS*
    "fair_mult_delta": "SIGNAL",  # devig(multiplicative)[i] - fair_prob
    "fair_shin_delta": "SIGNAL",  # devig(shin)[i] - fair_prob
    "fair_power_delta": "SIGNAL",  # devig(power)[i] - fair_prob
    # SIGNAL v2 — rolling pre-match form (nullable; strictly-prior matches only)
    **{f"home_{f}": "SIGNAL" for f in _FORM_FEATS},
    **{f"away_{f}": "SIGNAL" for f in _FORM_FEATS},
    # SIGNAL v2 — Understat xG rolling (nullable; big-5 1415+ only)
    **{f"home_{f}": "SIGNAL" for f in _XG_FEATS},
    **{f"away_{f}": "SIGNAL" for f in _XG_FEATS},
    # LABELS — close-derived / outcome-derived; never features
    "won": "LABEL",
    "bet_odds": "LABEL",  # settlement-side copy of best_price (profit accounting)
    "pinn_close_fair": "LABEL",  # devig(PSC*)[i], same method (ADR-0006)
    "max_close_fair": "LABEL",  # devig(MaxC*)[i]; null in betbrain era
    "clv_pinn": "LABEL",  # ln(bet_odds * pinn_close_fair)
    "clv_max": "LABEL",  # ln(bet_odds * max_close_fair) — stricter reference
    "profit_units": "LABEL",  # flat 1u: odds-1 if won else -1
}
FEATURE_COLUMNS: tuple[str, ...] = tuple(c for c, k in SCHEMA.items() if k == "SIGNAL")
LABEL_COLUMNS: tuple[str, ...] = tuple(c for c, k in SCHEMA.items() if k == "LABEL")
ID_COLUMNS: tuple[str, ...] = tuple(c for c, k in SCHEMA.items() if k == "ID")

# Exact v1 column set + order — the --v1 output and the original artifact.
SCHEMA_V1: tuple[str, ...] = (
    "league",
    "season",
    "match_date",
    "kickoff_utc",
    "home_team",
    "away_team",
    "market",
    "selection",
    "era",
    "fair_prob",
    "edge",
    "best_price",
    "pinn_price",
    "overround_pinn",
    "overround_best",
    "devig_spread",
    "open_to_signal_drift",
    "book_count",
    "selection_type",
    "day_of_week",
    "days_to_season_end",
    "is_argmax_edge",
    "won",
    "bet_odds",
    "pinn_close_fair",
    "max_close_fair",
    "clv_pinn",
    "clv_max",
    "profit_units",
)

# v3-only columns. SCHEMA_V2 (the exact v2 column set + order) is derived by
# removing them from SCHEMA, so the v2 artifact stays byte-identical no
# matter where the v3 keys sit in the dict.
V3_ONLY_COLUMNS: tuple[str, ...] = ("anchor_type", "ah_line")
SCHEMA_V2: tuple[str, ...] = tuple(c for c in SCHEMA if c not in V3_ONLY_COLUMNS)


@dataclass(frozen=True)
class Candidate:
    league: str
    season: str
    match_date: date
    kickoff_utc: datetime | None
    home_team: str
    away_team: str
    market: str
    selection: str
    era: str
    fair_prob: float
    edge: float
    best_price: float
    pinn_price: float | None  # null only on v3 consensus rows without PS*
    overround_pinn: float | None
    overround_best: float
    devig_spread: float
    open_to_signal_drift: float | None
    book_count: int
    selection_type: str
    day_of_week: int
    days_to_season_end: int
    is_argmax_edge: bool
    # v2 — fresh-domain split key
    consulted_before: bool
    # v3 — anchor split key + AH line (None off-market)
    anchor_type: str
    ah_line: float | None
    # v2 — FLB structure
    odds_band: str
    fair_prob_rank: int
    # v2 — anchor disagreement
    price_ratio_best_pinn: float | None
    prob_gap_pinn_best: float | None
    fair_mult_delta: float
    fair_shin_delta: float
    fair_power_delta: float
    # v2 — rolling pre-match form (strictly-prior matches; nullable)
    home_gf_r5: float | None
    home_ga_r5: float | None
    home_gf_r10: float | None
    home_ga_r10: float | None
    home_shots_for_r5: float | None
    home_shots_against_r5: float | None
    home_sot_for_r5: float | None
    home_sot_against_r5: float | None
    home_corners_for_r5: float | None
    home_corners_against_r5: float | None
    away_gf_r5: float | None
    away_ga_r5: float | None
    away_gf_r10: float | None
    away_ga_r10: float | None
    away_shots_for_r5: float | None
    away_shots_against_r5: float | None
    away_sot_for_r5: float | None
    away_sot_against_r5: float | None
    away_corners_for_r5: float | None
    away_corners_against_r5: float | None
    # v2 — Understat xG rolling (big-5 1415+; nullable elsewhere)
    home_xg_for_r5: float | None
    home_xg_against_r5: float | None
    home_xg_for_r10: float | None
    home_xg_against_r10: float | None
    home_xg_overperf_r10: float | None
    away_xg_for_r5: float | None
    away_xg_against_r5: float | None
    away_xg_for_r10: float | None
    away_xg_against_r10: float | None
    away_xg_overperf_r10: float | None
    won: bool
    bet_odds: float
    pinn_close_fair: float | None
    max_close_fair: float | None
    clv_pinn: float | None
    clv_max: float | None
    profit_units: float


def assert_no_label_leak() -> None:
    """Build-breaking leakage gate: LABELs must never enter the feature set."""
    feats, labels, ids = set(FEATURE_COLUMNS), set(LABEL_COLUMNS), set(ID_COLUMNS)
    assert not feats & labels, f"label columns leaked into features: {feats & labels}"
    assert not feats & ids and not labels & ids, "ID columns must stay out of both sets"
    close_or_outcome = {
        "won",
        "bet_odds",
        "pinn_close_fair",
        "max_close_fair",
        "clv_pinn",
        "clv_max",
        "profit_units",
    }
    assert close_or_outcome <= labels, "every close/outcome-derived column must be a LABEL"
    # no raw close column (PSC*/MaxC*/PC*/AH closes) may appear in the schema at all
    raw_close = {c for s in MARKETS.values() for c in (*s.pinn_close_cols, *s.max_close_cols)}
    raw_close |= {AH_CLOSE_LINE_COL, *AH_PINN_CLOSE_COLS, *AH_MAX_CLOSE_COLS}
    assert not raw_close & set(SCHEMA), "raw close columns must not be dataset columns"
    fields = {f.name for f in dataclasses.fields(Candidate)}
    assert fields == set(SCHEMA), f"SCHEMA out of sync with Candidate: {fields ^ set(SCHEMA)}"
    # v2 invariants ----------------------------------------------------------
    # v1 columns survive verbatim (column-set reproducibility under --v1)
    assert set(SCHEMA_V1) <= set(SCHEMA), "SCHEMA_V1 must be a subset of SCHEMA"
    assert {c: SCHEMA[c] for c in SCHEMA_V1} == {
        c: k for c, k in SCHEMA.items() if c in set(SCHEMA_V1)
    }, "v1 column classification must not change"
    # v3 invariants ----------------------------------------------------------
    # v2 column set + order survive verbatim (default builds stay byte-identical)
    assert set(SCHEMA_V2) == set(SCHEMA) - set(V3_ONLY_COLUMNS), "SCHEMA_V2 drift"
    assert set(SCHEMA_V1) <= set(SCHEMA_V2), "SCHEMA_V1 must survive inside SCHEMA_V2"
    assert SCHEMA["anchor_type"] == "ID", "anchor_type is a split key (ID), not a feature"
    assert SCHEMA["ah_line"] == "SIGNAL", "ah_line is a pre-match SIGNAL"
    # the closing AH line gates LABELS only; it must never be a column itself
    assert AH_CLOSE_LINE_COL not in SCHEMA
    # the consensus anchor must be independent of Pinnacle (and exchange-free)
    assert "PS" not in CONSENSUS_BOOKS and "BFE" not in CONSENSUS_BOOKS
    # raw per-match stats are OUTCOMES: only rolling PRE-MATCH aggregates may
    # appear, and every rolling/xG column must be a strictly-prior aggregate
    raw_stats = {"FTHG", "FTAG", "HS", "AS", "HST", "AST", "HC", "AC"}
    assert not raw_stats & set(SCHEMA), "raw match-stat columns are LABEL-class, never columns"
    rolling = {f"{side}_{f}" for side in ("home", "away") for f in (*_FORM_FEATS, *_XG_FEATS)}
    assert rolling <= feats, "every rolling form/xG column must be classified SIGNAL"
    assert "consulted_before" in ids, "consulted_before is a split key (ID), not a feature"


# --------------------------------------------------------------------------
# Row -> candidates
# --------------------------------------------------------------------------
def _parse_match_date(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_kickoff_utc(d: date, time_raw: str | None) -> datetime | None:
    """Date + Time are UK local (inventory note); convert to UTC. Null when
    the Time column is absent (pre-2019/20)."""
    if not time_raw or not time_raw.strip():
        return None
    try:
        t = datetime.strptime(time_raw.strip(), "%H:%M").time()
    except ValueError:
        return None
    return datetime.combine(d, t, tzinfo=UK_TZ).astimezone(UTC)


def _season_end(season: str) -> date:
    """Nominal season end: June 30 of the end year. Fixed by convention —
    using the actual last fixture date would leak future schedule info."""
    return date(2000 + int(season[2:4]), 6, 30)


def _book_count(r: dict[str, str], era: str) -> int:
    if era == "betbrain":
        raw = (r.get("Bb1X2") or "").strip()
        try:
            return int(float(raw))
        except ValueError:
            pass  # fall through to the column-count proxy
    return sum(1 for c in BOOK_HOME_COLS if _f(r.get(c)) is not None)


def _selection_type(market: str, fair: Sequence[float], i: int) -> str:
    if market == "1x2" and i == 1:
        return "draw"
    if market == "1x2":
        other = 2 if i == 0 else 0
        return "fav" if fair[i] >= fair[other] else "dog"
    return "fav" if fair[i] >= max(fair) else "dog"


def _fairs_by_method(odds: Sequence[float]) -> dict[DevigMethod, Sequence[float]]:
    """Fair-prob vectors for each structurally different devig method."""
    return {m: devig(odds, method=m) for m in SPREAD_METHODS}


def _devig_spreads(fairs: Mapping[DevigMethod, Sequence[float]], n: int) -> list[float]:
    """Per-selection max pairwise fair-prob disagreement across SPREAD_METHODS."""
    vecs = list(fairs.values())
    return [max(abs(a[i] - b[i]) for a in vecs for b in vecs) for i in range(n)]


# --------------------------------------------------------------------------
# v2 features: FLB structure, rolling pre-match form, Understat xG
# --------------------------------------------------------------------------
_ODDS_BANDS: tuple[tuple[float, str], ...] = (
    (1.5, "(1.0,1.5]"),
    (2.0, "(1.5,2.0]"),
    (3.0, "(2.0,3.0]"),
    (5.0, "(3.0,5.0]"),
    (10.0, "(5.0,10.0]"),
)


def _odds_band(price: float) -> str:
    """FLB odds-level bucket of the takeable (best) price."""
    for upper, label in _ODDS_BANDS:
        if price <= upper:
            return label
    return "(10.0,inf)"


def _fair_prob_rank(fair: Sequence[float], i: int) -> int:
    """1 = highest fair prob within the market; ties break by index (stable)."""
    return 1 + sum(1 for j, p in enumerate(fair) if p > fair[i] or (p == fair[i] and j < i))


def _mean_last(values: Sequence[float], k: int) -> float | None:
    """Mean of the last k values — shift(1).rolling(k, min_periods=1).mean()
    equivalence holds because callers append the current match only AFTER
    reading features (strictly-prior window)."""
    if not values:
        return None
    window = values[-k:]
    return sum(window) / len(window)


def _stat(r: dict[str, str], col: str) -> float | None:
    """Non-negative count parser for stats columns (HS/HC/FTHG/...)."""
    raw = (r.get(col) or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v >= 0 else None


_EMPTY_FORM: dict[str, float | None] = dict.fromkeys(_FORM_FEATS)
_EMPTY_XG: dict[str, float | None] = dict.fromkeys(_XG_FEATS)

# (stat key, home column, away column) — home's "for" is the home column.
_FORM_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("goals", "FTHG", "FTAG"),
    ("shots", "HS", "AS"),
    ("sot", "HST", "AST"),
    ("corners", "HC", "AC"),
)


def _form_feats(state: Mapping[str, list[float]]) -> dict[str, float | None]:
    return {
        "gf_r5": _mean_last(state["goals_for"], 5),
        "ga_r5": _mean_last(state["goals_against"], 5),
        "gf_r10": _mean_last(state["goals_for"], 10),
        "ga_r10": _mean_last(state["goals_against"], 10),
        "shots_for_r5": _mean_last(state["shots_for"], 5),
        "shots_against_r5": _mean_last(state["shots_against"], 5),
        "sot_for_r5": _mean_last(state["sot_for"], 5),
        "sot_against_r5": _mean_last(state["sot_against"], 5),
        "corners_for_r5": _mean_last(state["corners_for"], 5),
        "corners_against_r5": _mean_last(state["corners_against"], 5),
    }


FormLookup = dict[tuple[str, str, date], tuple[dict[str, float | None], dict[str, float | None]]]


def _build_form_lookup(rows: list[dict[str, str]]) -> FormLookup:
    """(home, away, date) -> (home-side, away-side) rolling pre-match form.

    Built from EVERY parseable match in the league-season file in
    chronological order (stable sort keeps file order within a date).
    Features are read BEFORE the current match is appended — the explicit
    shift(1) guarantee (betting-feature-engineering skill, rule 1).
    """
    parsed: list[tuple[date, dict[str, str]]] = []
    for r in rows:
        if not r.get("HomeTeam") or not r.get("Date"):
            continue
        d = _parse_match_date(r.get("Date"))
        if d is not None:
            parsed.append((d, r))
    parsed.sort(key=lambda t: t[0])  # stable mergesort-like ordering in CPython
    state: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    lookup: FormLookup = {}
    for d, r in parsed:
        home = r["HomeTeam"].strip()
        away = (r.get("AwayTeam") or "").strip()
        lookup[(home, away, d)] = (_form_feats(state[home]), _form_feats(state[away]))
        for stat, home_col, away_col in _FORM_SOURCES:
            hv, av = _stat(r, home_col), _stat(r, away_col)
            if hv is not None:
                state[home][f"{stat}_for"].append(hv)
                state[away][f"{stat}_against"].append(hv)
            if av is not None:
                state[home][f"{stat}_against"].append(av)
                state[away][f"{stat}_for"].append(av)
    return lookup


# xG lookup: (mapped home, mapped away) -> per-side feature dicts. Pairings
# are unique within a league-season, so no date join is needed (avoids
# cross-source timezone drift). Rolling order = Understat's own chronology.
XgLookup = dict[tuple[str, str], tuple[dict[str, float | None], dict[str, float | None]]]


def _understat_cache_path(cache_dir: Path, league: str, season: str) -> Path:
    return cache_dir / "understat" / f"{league}_{season}.csv"


def _fetch_understat_fixtures(
    league: str, season: str, cache_dir: Path
) -> list[dict[str, str]] | None:
    """Cached read-only GET of Understat results+xG; failures quarantine.

    Lazy penaltyblog import: penaltyblog.scrapers' package __init__ pulls in
    its FBRef module (tls_requests dependency) — we never call FBRef, and the
    import stays out of --v1 / --no-understat runs entirely.
    """
    path = _understat_cache_path(cache_dir, league, season)
    if not path.exists():
        try:
            from penaltyblog.scrapers.understat import Understat

            season_str = f"20{season[:2]}-20{season[2:]}"
            df = Understat(UNDERSTAT_COMP[league], season_str).get_fixtures().reset_index()
            cols = [
                "datetime",
                "team_home",
                "team_away",
                "goals_home",
                "goals_away",
                "xg_home",
                "xg_away",
            ]
            path.parent.mkdir(parents=True, exist_ok=True)
            df[cols].to_csv(path, index=False)
        except Exception as exc:  # noqa: BLE001 — never log URLs/exception text
            print(f"  quarantine understat {league} {season}: {type(exc).__name__}")
            return None
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _xg_feats(
    xg_for: Sequence[float], xg_against: Sequence[float], overperf: Sequence[float]
) -> dict[str, float | None]:
    return {
        "xg_for_r5": _mean_last(xg_for, 5),
        "xg_against_r5": _mean_last(xg_against, 5),
        "xg_for_r10": _mean_last(xg_for, 10),
        "xg_against_r10": _mean_last(xg_against, 10),
        "xg_overperf_r10": _mean_last(overperf, 10),
    }


def build_xg_lookup(fixtures: list[dict[str, str]]) -> XgLookup:
    """Strictly-prior rolling xG per team, keyed by mapped (home, away).

    State keys are raw Understat names (stable per team); only the JOIN key
    is passed through UNDERSTAT_TO_FOOTBALL_DATA (identity fallback). The
    current fixture is appended AFTER features are read — shift(1) again.
    """
    xg_for: dict[str, list[float]] = defaultdict(list)
    xg_against: dict[str, list[float]] = defaultdict(list)
    overperf: dict[str, list[float]] = defaultdict(list)
    lookup: XgLookup = {}
    ordered = sorted(fixtures, key=lambda f: (f.get("datetime") or "", f.get("team_home") or ""))
    for fx in ordered:
        h_us, a_us = (fx.get("team_home") or "").strip(), (fx.get("team_away") or "").strip()
        try:
            xgh, xga = float(fx["xg_home"]), float(fx["xg_away"])
            gh, ga = float(fx["goals_home"]), float(fx["goals_away"])
        except (KeyError, TypeError, ValueError):
            continue  # unresolved fixture -> no state update, no key
        if not h_us or not a_us:
            continue
        h_fd = UNDERSTAT_TO_FOOTBALL_DATA.get(h_us, h_us)
        a_fd = UNDERSTAT_TO_FOOTBALL_DATA.get(a_us, a_us)
        lookup[(h_fd, a_fd)] = (
            _xg_feats(xg_for[h_us], xg_against[h_us], overperf[h_us]),
            _xg_feats(xg_for[a_us], xg_against[a_us], overperf[a_us]),
        )
        xg_for[h_us].append(xgh)
        xg_against[h_us].append(xga)
        overperf[h_us].append(gh - xgh)
        xg_for[a_us].append(xga)
        xg_against[a_us].append(xgh)
        overperf[a_us].append(ga - xga)
    return lookup


def _quarantine_unmatched_names(
    league: str, season: str, rows: list[dict[str, str]], xg_lookup: XgLookup
) -> None:
    """Log mapped Understat names absent from the football-data file.

    Those fixtures simply never join (xG features stay null) — explicit
    quarantine, no fuzzy auto-merge in the hot path."""
    fd_names = {r["HomeTeam"].strip() for r in rows if r.get("HomeTeam")}
    fd_names |= {(r.get("AwayTeam") or "").strip() for r in rows if r.get("AwayTeam")}
    mapped = {name for key in xg_lookup for name in key}
    unmatched = sorted(mapped - fd_names)
    if unmatched:
        print(
            f"  quarantine understat names {league} {season}: {unmatched} "
            "(no fuzzy merge; xG features stay null for these joins)"
        )


def _form_kwargs(
    form_h: Mapping[str, float | None], form_a: Mapping[str, float | None]
) -> dict[str, float | None]:
    out = {f"home_{k}": form_h[k] for k in _FORM_FEATS}
    out.update({f"away_{k}": form_a[k] for k in _FORM_FEATS})
    return out


def _xg_kwargs(
    xg_h: Mapping[str, float | None], xg_a: Mapping[str, float | None]
) -> dict[str, float | None]:
    out = {f"home_{k}": xg_h[k] for k in _XG_FEATS}
    out.update({f"away_{k}": xg_a[k] for k in _XG_FEATS})
    return out


def _market_candidates(  # noqa: PLR0913 — one call site; explicit > a context object
    r: dict[str, str],
    league: str,
    season: str,
    d: date,
    kickoff: datetime | None,
    home: str,
    away: str,
    market: str,
    spec: MarketSpec,
    era: str,
    best: list[float],
    anchor_type: str,
    anchor: list[float],
    ps: list[float] | None,
    close_p: Sequence[float] | None,
    close_m: Sequence[float] | None,
    min_edge: float,
    consulted: bool,
    feats: Mapping[str, float | None],
) -> list[Candidate]:
    """Candidates of one (row, market) priced from ONE anchor odds vector.

    For anchor_type='pinnacle' the anchor IS ps (v1/v2-identical emission).
    For 'consensus' the anchor is the non-Pinnacle soft-book median; the
    Pinnacle-referencing features stay Pinnacle-derived (null when PS* is
    absent) so the anchor stays independent while the features survive.
    """
    fair = devig(anchor, method=CANONICAL_DEVIG)
    edges = [fair[i] - 1.0 / best[i] for i in range(len(anchor))]
    argmax_i = max(range(len(edges)), key=lambda i: edges[i])
    overround_pinn = (sum(1.0 / o for o in ps) - 1.0) if ps is not None else None
    overround_best = sum(1.0 / o for o in best) - 1.0
    fairs = _fairs_by_method(anchor)
    spreads = _devig_spreads(fairs, len(anchor))
    fair_mult = fairs[DevigMethod.MULTIPLICATIVE]
    fair_shin = fairs[DevigMethod.SHIN]
    fair_power = fairs[DevigMethod.POWER]
    out: list[Candidate] = []
    for i, edge in enumerate(edges):
        if edge < min_edge:
            continue
        won = spec.won_fn(r, i)
        if won is None:
            continue  # unresolvable result -> skipped (match counts printed)
        out.append(
            Candidate(
                league=league,
                season=season,
                match_date=d,
                kickoff_utc=kickoff,
                home_team=home,
                away_team=away,
                market=market,
                selection=spec.selections[i],
                era=era,
                fair_prob=fair[i],
                edge=edge,
                best_price=best[i],
                pinn_price=ps[i] if ps is not None else None,
                overround_pinn=overround_pinn,
                overround_best=overround_best,
                devig_spread=spreads[i],
                open_to_signal_drift=None,
                book_count=_book_count(r, era),
                selection_type=_selection_type(market, fair, i),
                day_of_week=d.weekday(),
                days_to_season_end=(_season_end(season) - d).days,
                is_argmax_edge=(i == argmax_i),
                consulted_before=consulted,
                anchor_type=anchor_type,
                ah_line=None,
                odds_band=_odds_band(best[i]),
                fair_prob_rank=_fair_prob_rank(fair, i),
                price_ratio_best_pinn=best[i] / ps[i] if ps is not None else None,
                prob_gap_pinn_best=(1.0 / ps[i] - 1.0 / best[i]) if ps is not None else None,
                fair_mult_delta=fair_mult[i] - fair[i],
                fair_shin_delta=fair_shin[i] - fair[i],
                fair_power_delta=fair_power[i] - fair[i],
                won=won,
                bet_odds=best[i],
                pinn_close_fair=close_p[i] if close_p else None,
                max_close_fair=close_m[i] if close_m else None,
                clv_pinn=clv_log(best[i], close_p[i]) if close_p else None,
                clv_max=clv_log(best[i], close_m[i]) if close_m else None,
                profit_units=(best[i] - 1.0) if won else -1.0,
                **feats,  # type: ignore[arg-type]
            )
        )
    return out


def _ah_candidates(  # noqa: PLR0913 — one call site; explicit > a context object
    r: dict[str, str],
    league: str,
    season: str,
    d: date,
    kickoff: datetime | None,
    home: str,
    away: str,
    min_edge: float,
    consulted: bool,
    feats: Mapping[str, float | None],
) -> list[Candidate]:
    """Asian-handicap candidates for one match — HALF-LINE rows only.

    Anchor = devig(PAHH, PAHA) (2-way, Pinnacle pre-match — maxavg era only);
    best = MaxAHH/MaxAHA. Settlement: AHh is the handicap the HOME side
    receives (football-data convention), so home covers iff
    (FTHG - FTAG) + AHh > 0 — on a half line no push is possible.
    Close labels are emitted ONLY when the closing line AHCh equals AHh:
    a close on a moved line prices a DIFFERENT bet and is not a valid CLV
    reference. AH_COVERAGE tracks the scope fractions for the build report.
    """
    line = _line(r.get(AH_LINE_COL))
    ps_opt = [_f(r.get(c)) for c in AH_PINN_COLS]
    best_opt = [_f(r.get(c)) for c in AH_BEST_COLS]
    if line is None or None in ps_opt or None in best_opt:
        return []
    AH_COVERAGE["priced"] += 1
    if not _is_half_line(line):
        AH_COVERAGE["skipped_integer_or_quarter"] += 1
        return []
    AH_COVERAGE["half_line"] += 1
    try:
        margin = int(r["FTHG"]) - int(r["FTAG"])
    except (KeyError, TypeError, ValueError):
        return []  # unresolvable result -> no labelable rows
    ps = [v for v in ps_opt if v is not None]
    best = [v for v in best_opt if v is not None]
    close_line = _line(r.get(AH_CLOSE_LINE_COL))
    close_p: Sequence[float] | None = None
    close_m: Sequence[float] | None = None
    if close_line is not None and close_line == line:
        AH_COVERAGE["close_line_matched"] += 1
        pc = [_f(r.get(c)) for c in AH_PINN_CLOSE_COLS]
        if None not in pc:
            close_p = devig([v for v in pc if v is not None], method=CANONICAL_DEVIG)
        mc = [_f(r.get(c)) for c in AH_MAX_CLOSE_COLS]
        if None not in mc:
            close_m = devig([v for v in mc if v is not None], method=CANONICAL_DEVIG)
    fair = devig(ps, method=CANONICAL_DEVIG)
    edges = [fair[i] - 1.0 / best[i] for i in range(len(ps))]
    argmax_i = 0 if edges[0] >= edges[1] else 1
    # Half line: margin is an integer, line is x.5 -> margin + line != 0.
    won_by_sel = (margin + line > 0, margin + line < 0)
    overround_pinn = sum(1.0 / o for o in ps) - 1.0
    overround_best = sum(1.0 / o for o in best) - 1.0
    fairs = _fairs_by_method(ps)
    spreads = _devig_spreads(fairs, len(ps))
    fair_mult = fairs[DevigMethod.MULTIPLICATIVE]
    fair_shin = fairs[DevigMethod.SHIN]
    fair_power = fairs[DevigMethod.POWER]
    out: list[Candidate] = []
    for i, edge in enumerate(edges):
        if edge < min_edge:
            continue
        won = won_by_sel[i]
        out.append(
            Candidate(
                league=league,
                season=season,
                match_date=d,
                kickoff_utc=kickoff,
                home_team=home,
                away_team=away,
                market="ah",
                selection=AH_SELECTIONS[i],
                era="maxavg",  # PAH*/MaxAH* exist in the maxavg era only
                fair_prob=fair[i],
                edge=edge,
                best_price=best[i],
                pinn_price=ps[i],
                overround_pinn=overround_pinn,
                overround_best=overround_best,
                devig_spread=spreads[i],
                open_to_signal_drift=None,
                book_count=_book_count(r, "maxavg"),
                selection_type=_selection_type("ah", fair, i),
                day_of_week=d.weekday(),
                days_to_season_end=(_season_end(season) - d).days,
                is_argmax_edge=(i == argmax_i),
                consulted_before=consulted,
                anchor_type=ANCHOR_PINNACLE,
                ah_line=line,
                odds_band=_odds_band(best[i]),
                fair_prob_rank=_fair_prob_rank(fair, i),
                price_ratio_best_pinn=best[i] / ps[i],
                prob_gap_pinn_best=1.0 / ps[i] - 1.0 / best[i],
                fair_mult_delta=fair_mult[i] - fair[i],
                fair_shin_delta=fair_shin[i] - fair[i],
                fair_power_delta=fair_power[i] - fair[i],
                won=won,
                bet_odds=best[i],
                pinn_close_fair=close_p[i] if close_p else None,
                max_close_fair=close_m[i] if close_m else None,
                clv_pinn=clv_log(best[i], close_p[i]) if close_p else None,
                clv_max=clv_log(best[i], close_m[i]) if close_m else None,
                profit_units=(best[i] - 1.0) if won else -1.0,
                **feats,  # type: ignore[arg-type]
            )
        )
    return out


def candidates_from_rows(
    league: str,
    season: str,
    rows: list[dict[str, str]],
    min_edge: float = MIN_EDGE_DEFAULT,
    xg_lookup: XgLookup | None = None,
    *,
    consensus_anchor: bool = False,
    include_ah: bool = False,
) -> list[Candidate]:
    """All candidate selections (edge >= min_edge) for one league-season.

    Candidate rule parity with scripts/value_backtest.py::bets_for, except the
    pool keeps EVERY qualifying selection (the harness applies one-bet-per-
    match via is_argmax_edge and any threshold/odds filters downstream).
    v2 features are purely additive — they never gate row emission, so --v1
    row content is unchanged. The v3 flags (consensus_anchor / include_ah)
    only ADD rows: with both off, emission is exactly the v2 behavior.
    """
    form_lookup = _build_form_lookup(rows)
    consulted = league in CONSULTED_LEAGUES
    out: list[Candidate] = []
    for r in rows:
        if not r.get("HomeTeam") or not r.get("Date"):
            continue
        d = _parse_match_date(r.get("Date"))
        if d is None:
            continue
        kickoff = _parse_kickoff_utc(d, r.get("Time"))
        home = r["HomeTeam"].strip()
        away = (r.get("AwayTeam") or "").strip()
        form_h, form_a = form_lookup.get((home, away, d), (_EMPTY_FORM, _EMPTY_FORM))
        xg_h, xg_a = (xg_lookup or {}).get((home, away), (_EMPTY_XG, _EMPTY_XG))
        feats = {**_form_kwargs(form_h, form_a), **_xg_kwargs(xg_h, xg_a)}
        for market, spec in MARKETS.items():
            era = ""
            best: list[float] = []
            for era_name, cols in spec.best_by_era:
                bx = [_f(r.get(c)) for c in cols]
                if None not in bx:
                    era, best = era_name, [v for v in bx if v is not None]
                    break
            if not best:
                continue
            ps_opt = [_f(r.get(c)) for c in spec.pinn_cols]
            ps: list[float] | None = (
                [v for v in ps_opt if v is not None] if None not in ps_opt else None
            )
            psc = [_f(r.get(c)) for c in spec.pinn_close_cols]
            close_p = (
                devig([v for v in psc if v is not None], method=CANONICAL_DEVIG)
                if None not in psc
                else None
            )
            mxc = [_f(r.get(c)) for c in spec.max_close_cols] if era == "maxavg" else [None]
            close_m = (
                devig([v for v in mxc if v is not None], method=CANONICAL_DEVIG)
                if None not in mxc
                else None
            )
            anchors: list[tuple[str, list[float]]] = []
            if ps is not None:
                anchors.append((ANCHOR_PINNACLE, ps))
            if consensus_anchor and market == "1x2":
                # ou25 has no >= 3 non-Pinnacle per-book columns (docstring)
                med = _consensus_odds(r, spec.selections)
                if med is not None:
                    anchors.append((ANCHOR_CONSENSUS, med))
            for anchor_type, anchor in anchors:
                out.extend(
                    _market_candidates(
                        r,
                        league,
                        season,
                        d,
                        kickoff,
                        home,
                        away,
                        market,
                        spec,
                        era,
                        best,
                        anchor_type,
                        anchor,
                        ps,
                        close_p,
                        close_m,
                        min_edge,
                        consulted,
                        feats,
                    )
                )
        if include_ah:
            out.extend(
                _ah_candidates(
                    r, league, season, d, kickoff, home, away, min_edge, consulted, feats
                )
            )
    return out


# --------------------------------------------------------------------------
# BeatTheBookie odds_series source (worldwide-league CALIBRATION breadth).
# HONEST SCOPE: this source has NO sharp book — the anchor is the market
# CONSENSUS (mean across ~32 soft books), tagged anchor_type='consensus', and
# every Pinnacle-specific column (pinn_price/overround_pinn/price_ratio/
# prob_gap) is null because no sharp price exists. The close labels
# (pinn_close_fair, clv_pinn) are computed against the CONSENSUS close, NOT a
# sharp close — they add calibration sample size (crossing isotonic n>=1000),
# never sharp-CLV proof. league='BTB' keeps these rows OUT of the trainer's
# frozen LEAGUES_18 evaluation universe by construction.
# --------------------------------------------------------------------------
def _btb_season(dt: datetime) -> str:
    """Football-style season code from a UTC kickoff (Jul-Jun season)."""
    start = dt.year if dt.month >= 7 else dt.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def btb_candidates(
    matches: Sequence[SeriesMatch], min_edge: float = MIN_EDGE_DEFAULT
) -> list[Candidate]:
    """Consensus-anchored 1x2 candidates from BeatTheBookie odds_series matches.

    Reuses _market_candidates with anchor_type=consensus and ps=None so the
    schema is filled identically to the live consensus-anchor path; book_count
    is then set to the real per-snapshot book count. Pure: no IO."""
    spec = MARKETS["1x2"]
    feats: dict[str, float | None] = {
        **_form_kwargs(_EMPTY_FORM, _EMPTY_FORM),
        **_xg_kwargs(_EMPTY_XG, _EMPTY_XG),
    }
    out: list[Candidate] = []
    for m in matches:
        d = m.kickoff_utc.date()
        season = _btb_season(m.kickoff_utc)
        row = {"FTR": m.result, "FTHG": str(m.home_score), "FTAG": str(m.away_score)}
        close_p = devig(list(m.close_consensus), method=CANONICAL_DEVIG)
        close_m = devig(list(m.close_best), method=CANONICAL_DEVIG)
        cands = _market_candidates(
            row,
            "BTB",
            season,
            d,
            m.kickoff_utc,
            f"m{m.match_id}_home",  # per-game files carry no team names (only id)
            f"m{m.match_id}_away",
            "1x2",
            spec,
            "maxavg",  # best-price era analog (Max line present)
            list(m.open_best),
            ANCHOR_CONSENSUS,
            list(m.open_consensus),
            None,  # ps: no sharp book -> all Pinnacle-specific columns null
            close_p,
            close_m,
            min_edge,
            False,  # consulted_before: fresh worldwide domain
            feats,
        )
        out.extend(dataclasses.replace(c, book_count=m.n_books_open) for c in cands)
    return out


# --------------------------------------------------------------------------
# IO: cached, paced, read-only GETs (reuses the tenacity-wrapped fetcher)
# --------------------------------------------------------------------------
async def _load_csv(
    client: httpx.AsyncClient, cache_dir: Path, league: str, season: str
) -> list[dict[str, str]] | None:
    """Returns parsed rows, or None if the file is unavailable (quarantined)."""
    cache = cache_dir / f"{season}_{league}.csv"
    if cache.exists():
        text = cache.read_text(encoding="utf-8", errors="replace")
    else:
        text = ""
        for attempt in range(4):
            try:
                text = await fetch_season_csv(client, league, season)
                break
            except httpx.HTTPStatusError as exc:  # never log URLs/exception text
                status = exc.response.status_code
                if status == 404:
                    print(f"  quarantine {league} {season}: HTTP 404 (no such file)")
                    return None
                print(f"  retry {league} {season}: HTTP {status} (attempt {attempt + 1})")
                await asyncio.sleep(1.5)
            except httpx.HTTPError as exc:
                print(f"  retry {league} {season}: {type(exc).__name__} (attempt {attempt + 1})")
                await asyncio.sleep(1.5)
        if not text:
            print(f"  quarantine {league} {season}: fetch failed after 4 attempts")
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        await asyncio.sleep(0.3)  # pacing — respect football-data.co.uk
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def _to_dataframe(cands: list[Candidate], schema_version: int = 2) -> pd.DataFrame:
    df = pd.DataFrame([dataclasses.asdict(c) for c in cands], columns=list(SCHEMA))
    df = df.sort_values(
        ["league", "season", "match_date", "home_team", "away_team", "market", "selection"],
        kind="mergesort",  # stable -> deterministic output ordering
    ).reset_index(drop=True)
    df["kickoff_utc"] = pd.to_datetime(df["kickoff_utc"], utc=True)
    nullable_v2 = tuple(
        f"{side}_{f}" for side in ("home", "away") for f in (*_FORM_FEATS, *_XG_FEATS)
    )
    cols = (
        "pinn_close_fair",
        "max_close_fair",
        "clv_pinn",
        "clv_max",
        "open_to_signal_drift",
        # nullable on v3 consensus/AH rows; plain float64 everywhere else
        "pinn_price",
        "overround_pinn",
        "price_ratio_best_pinn",
        "prob_gap_pinn_best",
        "ah_line",
        *nullable_v2,
    )
    for col in cols:
        df[col] = df[col].astype("float64")
    df["won"] = df["won"].astype(bool)
    df["is_argmax_edge"] = df["is_argmax_edge"].astype(bool)
    df["consulted_before"] = df["consulted_before"].astype(bool)
    df["book_count"] = df["book_count"].astype("int32")
    df["fair_prob_rank"] = df["fair_prob_rank"].astype("int32")
    if schema_version == 1:
        df = df[list(SCHEMA_V1)]
    elif schema_version == 2:  # frozen v2 column set + order (byte-identical)
        df = df[list(SCHEMA_V2)]
    return df


def _understat_eligible(league: str, season: str) -> bool:
    """Understat covers the big-5 top flights from 2014/15 onward."""
    return league in UNDERSTAT_COMP and int(season[:2]) >= UNDERSTAT_FIRST_END_YEAR - 1


async def build(
    leagues: list[str],
    seasons: list[str],
    cache_dir: Path,
    min_edge: float,
    schema_version: int = 2,
    use_understat: bool = True,
    consensus_anchor: bool = False,
    include_ah: bool = False,
) -> tuple[pd.DataFrame, list[tuple[str, str, int, int]]]:
    counts: list[tuple[str, str, int, int]] = []  # (league, season, matches, candidates)
    all_cands: list[Candidate] = []
    _reset_ah_coverage()
    async with httpx.AsyncClient() as client:
        for league in leagues:
            for season in seasons:
                rows = await _load_csv(client, cache_dir, league, season)
                if rows is None:
                    counts.append((league, season, 0, 0))
                    continue
                xg_lookup: XgLookup | None = None
                if schema_version >= 2 and use_understat and _understat_eligible(league, season):
                    # blocking requests call -> worker thread (batch script,
                    # but keep the loop honest anyway)
                    fixtures = await asyncio.to_thread(
                        _fetch_understat_fixtures, league, season, cache_dir
                    )
                    if fixtures:
                        xg_lookup = build_xg_lookup(fixtures)
                        _quarantine_unmatched_names(league, season, rows, xg_lookup)
                n_matches = sum(1 for r in rows if r.get("HomeTeam") and r.get("Date"))
                cands = candidates_from_rows(
                    league,
                    season,
                    rows,
                    min_edge,
                    xg_lookup,
                    consensus_anchor=consensus_anchor,
                    include_ah=include_ah,
                )
                counts.append((league, season, n_matches, len(cands)))
                all_cands.extend(cands)
    return _to_dataframe(all_cands, schema_version), counts


def _quality_report(df: pd.DataFrame, counts: list[tuple[str, str, int, int]]) -> None:
    print("\nrow counts per league/season (matches -> candidate rows):")
    by_league: dict[str, tuple[int, int]] = {}
    for league, season, n_m, n_c in counts:
        m, c = by_league.get(league, (0, 0))
        by_league[league] = (m + n_m, c + n_c)
        print(f"  {league:>4} {season}: {n_m:5d} matches -> {n_c:5d} candidates")
    print("\nper-league totals:")
    for league, (m, c) in sorted(by_league.items()):
        print(f"  {league:>4}: {m:6d} matches -> {c:6d} candidates")
    print(f"\nTOTAL: {sum(m for m, _ in by_league.values())} matches -> {len(df)} candidate rows")
    if df.empty:
        print("DATA-QUALITY ALERT: zero candidates assembled")
        return
    print("\nper-market / per-era candidate counts:")
    print(df.groupby(["market", "era"], observed=True).size().to_string())
    print("\nlabel availability (data-quality gates):")
    n = len(df)
    for col, expect in (
        ("clv_pinn", "near-100% (PSC* fill is 95-100% from 1213)"),
        ("clv_max", "maxavg-era rows only (MaxC* starts 2019/20)"),
    ):
        null_rate = float(df[col].isna().mean())
        print(
            f"  {col}: {n - int(df[col].isna().sum())}/{n} present "
            f"(null rate {null_rate:.2%}) — expected {expect}"
        )
    betbrain_with_max = df[(df["era"] == "betbrain") & df["clv_max"].notna()]
    if not betbrain_with_max.empty:
        print(f"DATA-QUALITY ALERT: {len(betbrain_with_max)} betbrain rows carry clv_max")
    maxavg = df[df["era"] == "maxavg"]
    if not maxavg.empty and float(maxavg["clv_pinn"].isna().mean()) > 0.10:
        print("DATA-QUALITY ALERT: >10% missing Pinnacle close in the maxavg era")
    if "consulted_before" in df.columns:
        _v2_quality_report(df)
    if "anchor_type" in df.columns:
        _v3_quality_report(df)
    print("\nschema classification:")
    print(f"  ID:     {', '.join(ID_COLUMNS)}")
    print(f"  SIGNAL: {', '.join(FEATURE_COLUMNS)}")
    print(f"  LABEL:  {', '.join(LABEL_COLUMNS)}")
    print("\nNOTE: open_to_signal_drift is all-null for football-data (pre-match")
    print("columns are a single collection-time snapshot, ~T-1 day; no true opener).")
    # TODO(elo enrichment): point-in-time ClubElo via penaltyblog — join
    #   get_elo_by_team validity windows (from <= match_date <= to) so each
    #   rating uses only strictly-prior matches. Blocked on the versioned
    #   (sport, canonical_name) -> per-source alias mapping table (ClubElo
    #   short names differ from football-data names, e.g. 'Bayern' vs
    #   'Bayern Munich'); unmatched names quarantine with a log line — no
    #   fuzzy auto-merge. Never join "latest rating as of today" onto
    #   historical matches (future-rating leak).


def _v2_quality_report(df: pd.DataFrame) -> None:
    """v2 gates: fresh-slice sizing + null-rate checks for the new features."""
    fresh = df[~df["consulted_before"]]
    print("\nfresh never-consulted slice (consulted_before=False):")
    if fresh.empty:
        print("  (none in this build)")
    else:
        print(fresh.groupby(["league", "era"], observed=True).size().to_string())
        print(f"  TOTAL fresh candidate rows: {len(fresh)}")
        fresh_maxavg = int((fresh["era"] == "maxavg").sum())
        print(f"  of which maxavg era (clv_max-labelable): {fresh_maxavg}")
        if len(fresh) < 1500:
            print(
                "\n*** DATA-QUALITY ALERT — FRESH SLICE TOO SMALL: "
                f"{len(fresh)} < 1500 candidate rows. A one-shot fresh-domain "
                "test on this slice alone is underpowered; the validator must "
                "combine it with the other fresh domains (AH market) or rely "
                "on live shadow CLV + the 2627 season. ***"
            )
    print("\nv2 feature null rates (nullable by construction — LightGBM-native NaN):")
    form_cols = [f"{s}_{f}" for s in ("home", "away") for f in _FORM_FEATS]
    xg_cols = [f"{s}_{f}" for s in ("home", "away") for f in _XG_FEATS]
    n = len(df)
    if n:
        form_null = float(df[form_cols].isna().any(axis=1).mean())
        goals_null = float(df[["home_gf_r5", "away_gf_r5"]].isna().any(axis=1).mean())
        print(f"  any form col null: {form_null:.2%} (stats-column eras differ by division)")
        print(f"  goals form null:   {goals_null:.2%} (expected ~ first-matches share)")
        eligible = df[
            df["league"].isin(UNDERSTAT_COMP)
            & (df["season"].str[:2].astype(int) >= UNDERSTAT_FIRST_END_YEAR - 1)
        ]
        if not eligible.empty:
            xg_null = float(eligible[xg_cols].isna().any(axis=1).mean())
            print(
                f"  xG null within big-5 1415+ rows: {xg_null:.2%} "
                "(first matches + quarantined names)"
            )
        outside = df.drop(eligible.index) if not eligible.empty else df
        if not outside.empty:
            leaked_xg = int(outside[xg_cols].notna().any(axis=1).sum())
            if leaked_xg:
                print(f"DATA-QUALITY ALERT: {leaked_xg} non-big-5/pre-1415 rows carry xG")
            else:
                print("  xG outside big-5 1415+: all null (expected missingness pattern)")


def _v3_quality_report(df: pd.DataFrame) -> None:
    """v3 gates: anchor split sizing + AH scope/label coverage."""
    print("\nv3 anchor split (rows per anchor_type x market):")
    print(df.groupby(["anchor_type", "market"], observed=True).size().to_string())
    cons = df[df["anchor_type"] == ANCHOR_CONSENSUS]
    if not cons.empty:
        assert (cons["market"] == "1x2").all(), "consensus anchor is 1x2-only by construction"
        n_lbl = int(cons["clv_pinn"].notna().sum())
        print(f"  consensus rows with clv_pinn label: {n_lbl}/{len(cons)}")
    ah = df[df["market"] == "ah"]
    if not ah.empty:
        priced = AH_COVERAGE.get("priced", 0)
        half = AH_COVERAGE.get("half_line", 0)
        matched = AH_COVERAGE.get("close_line_matched", 0)
        print("\nAH scope (half-line-only doctrine — coverage documented per build):")
        if priced:
            print(
                f"  AH-priced matches: {priced}; half-line: {half} "
                f"({half / priced:.1%}); skipped integer/quarter: "
                f"{AH_COVERAGE.get('skipped_integer_or_quarter', 0)}"
            )
        if half:
            print(
                f"  close line == pre-match line: {matched}/{half} ({matched / half:.1%}) "
                "— CLV labels exist only on these (a moved close is a different bet)"
            )
        for col in ("clv_pinn", "clv_max"):
            print(f"  ah rows with {col}: {int(ah[col].notna().sum())}/{len(ah)}")
        assert ah["ah_line"].notna().all(), "every AH row must carry its line"
        assert ((ah["ah_line"] * 2) % 2 != 0).all(), "non-half-line AH row leaked"


def _build_btb(btb_dirs: list[Path], min_edge: float, out: Path) -> int:
    """Assemble the BeatTheBookie consensus-anchored calibration parquet.

    Separate artifact (value_candidates_btb.parquet); never touches the v1/v2/v3
    football-data outputs. Read-only over operator-placed files."""
    assert_no_label_leak()
    found = [d for d in btb_dirs if d.is_dir()]
    if not found:
        print("BeatTheBookie data not found. Operator must unzip odds_series.zip /")
        print("odds_series_b.zip (Dropbox links in github.com/Lisandro79/BeatTheBookie")
        print("README) into per-game match_*.txt dirs, e.g.:")
        for d in btb_dirs:
            print(f"    {d}")
        print("Read-only academic data (GPL-3.0; cite arXiv:1710.02824). Places no bets.")
        return 1
    matches = [m for d in found for m in load_series_dir(d)]
    print(f"BeatTheBookie: {len(matches)} matches from {len(found)} dir(s), min_edge {min_edge}")
    cands = btb_candidates(matches, min_edge)
    df = _to_dataframe(cands, schema_version=3)  # consensus rows need anchor_type
    if df.empty:
        print("DATA-QUALITY ALERT: zero BeatTheBookie candidates assembled (no usable matches)")
    else:
        n = len(df)
        lbl = int(df["clv_pinn"].notna().sum())
        print(f"  {n} consensus candidate rows; clv (vs consensus close) present: {lbl}/{n}")
        print(f"  isotonic-eligible (n>=1000): {n >= 1000}")
        assert (df["anchor_type"] == ANCHOR_CONSENSUS).all(), "BTB rows must be consensus-anchored"
        assert df["pinn_price"].isna().all(), "BTB has no sharp price — pinn_price must be null"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, engine="pyarrow", index=False)
    print(f"\nwrote {len(df)} rows -> {out}")
    print("HONEST SCOPE: consensus anchor + consensus close (NOT sharp). Calibration")
    print("breadth only; sharp-CLV proof stays with the football-data Pinnacle path.")
    print("Decision-support only — nothing here places bets.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--source",
        choices=("football-data", "beatthebookie"),
        default="football-data",
        help="football-data.co.uk (default) or BeatTheBookie odds_series (consensus breadth)",
    )
    p.add_argument(
        "--btb-dir",
        default=f"{BTB_DIR_DEFAULT / 'odds_series'},{BTB_DIR_DEFAULT / 'odds_series_b'}",
        help="comma-separated dirs of operator-placed BeatTheBookie match_*.txt files",
    )
    p.add_argument("--leagues", default=None, help="default: all LEAGUES (v2) / V1_LEAGUES (--v1)")
    p.add_argument("--seasons", default=",".join(SEASONS_ALL))
    p.add_argument("--min-edge", type=float, default=MIN_EDGE_DEFAULT)
    p.add_argument("--out", type=Path, default=None, help="default: versioned parquet path")
    p.add_argument("--cache-dir", type=Path, default=CACHE_DEFAULT)
    p.add_argument(
        "--v1",
        action="store_true",
        help="reproduce the v1 dataset exactly (v1 columns, frozen v1 league set, no Understat)",
    )
    p.add_argument(
        "--no-understat",
        action="store_true",
        help="skip Understat xG enrichment (offline mode); xG features stay null",
    )
    p.add_argument(
        "--anchor-consensus",
        action="store_true",
        help="v3: ALSO emit consensus-anchored 1x2 candidates (anchor_type tag)",
    )
    p.add_argument(
        "--ah",
        action="store_true",
        help="v3: ALSO emit Asian-handicap half-line candidates (market='ah')",
    )
    args = p.parse_args(argv)
    if args.source == "beatthebookie":
        btb_dirs = [Path(d.strip()) for d in args.btb_dir.split(",") if d.strip()]
        out_btb: Path = args.out or OUT_DEFAULT_BTB
        return _build_btb(btb_dirs, args.min_edge, out_btb)
    v3 = args.anchor_consensus or args.ah
    if args.v1 and v3:
        p.error("--v1 cannot combine with the v3 flags (--anchor-consensus/--ah)")
    schema_version = 1 if args.v1 else (3 if v3 else 2)
    leagues_default = ",".join(V1_LEAGUES) if args.v1 else ",".join(LEAGUES)
    leagues = [x.strip() for x in (args.leagues or leagues_default).split(",") if x.strip()]
    seasons = [x.strip() for x in args.seasons.split(",") if x.strip()]
    out_by_version = {1: OUT_DEFAULT_V1, 2: OUT_DEFAULT_V2, 3: OUT_DEFAULT_V3}
    out: Path = args.out or out_by_version[schema_version]

    assert_no_label_leak()  # build-breaking gate, runs before any work

    print(
        f"building value-candidate pool v{schema_version}: "
        f"{len(leagues)} leagues x {len(seasons)} seasons, "
        f"min_edge {args.min_edge}, devig {CANONICAL_DEVIG.value}"
        + (f", anchors+={args.anchor_consensus}, ah+={args.ah}" if v3 else "")
    )
    df, counts = asyncio.run(
        build(
            leagues,
            seasons,
            args.cache_dir,
            args.min_edge,
            schema_version=schema_version,
            use_understat=not (args.v1 or args.no_understat),
            consensus_anchor=args.anchor_consensus,
            include_ah=args.ah,
        )
    )
    _quality_report(df, counts)

    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, engine="pyarrow", index=False)
    print(f"\nwrote {len(df)} rows ({len(df.columns)} columns, schema v{schema_version}) -> {out}")
    print("Decision-support only — this dataset informs manual picks; nothing places bets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
