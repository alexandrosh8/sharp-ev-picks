"""BSP-stream pre-off drift study — the definitive retest of the steam family.

Idea #2 of docs/research/2026-07-10-github-strategy-sweep.md, executed at
archive scale (~163k soccer MATCH_ODDS + ~151k OVER_UNDER_25 markets,
2024-08..2026-06). The live steam gate (`VALUE_STEAM_GATE_ENABLED`) was
walk-forward tested 2026-06-28 and KEPT OFF on n=1 live evidence; this study
exists to SETTLE that verdict at scale, not to re-tune the gate.

FROZEN QUESTION (from the protocol): does pre-off drift/velocity from T-24h to
T-60m predict the remaining move from T-60m to BSP? Per market type (1X2, OU).

FROZEN SUCCESS BAR (pre-set, not movable): sign-prediction AUC >= 0.55 across
>= 6 CONSECUTIVE monthly folds AND a positive filtered-CLV delta (95% CI
excluding 0) when the drift filter is applied to the frozen selection-rule
replay (edge >= 3%, odds [1.6, 4.0], power devig, one pick per match — reused
verbatim from scripts/research/ah_anchor_backtest.py). Anything below the bar
=> documented verdict: STEAM STAYS OFF permanently.

ADR-0019 FRAMING (stated explicitly, per the study brief): the 2025 BSP tars
are registered SPENT for the ADR-0019 live-gate hypotheses, and the 2026-H1
slice was consumed by the 2026-07-02 single-shot for the AH one-shot question.
This study CONSULTS that data anyway because steam's fate is a
KEEP-OFF-or-retest decision on an ALREADY-OFF gate — a closure study, not new
live-gate tuning. No threshold, coefficient, or filter parameter measured here
may be lifted into the live pipeline; a RETEST-JUSTIFIED verdict would require
its own pre-registered forward test on unspent data.

LEAKAGE RULES: features (drift/velocity) are computed ONLY from stream
messages with publish-time pt <= the per-market cutoff (T-60m before
`marketTime`); the snapshot is taken BEFORE applying the first message that
crosses each cutoff, so no post-cutoff message ever touches a feature. Markets
whose in-play flip precedes the T-60m cutoff, or whose kickoff moved > 30 min
after the first definition, are EXCLUDED and counted (never silently dropped).

HONESTY: n per fold; ddof=1 SEs; bootstrap clustered by market-day; every
exclusion reason counted and reported (silent truncation forbidden).

Run (offline; reads local tars/caches, writes checkpoints + report JSON):
    .venv/bin/python scripts/research/bsp_drift_study.py extract \
        --tar "data/betfair/bsp/data.tar" --out CKPT.jsonl.gz [--limit N]
    .venv/bin/python scripts/research/bsp_drift_study.py analyze \
        --ckpt CKPT1.jsonl.gz CKPT2.jsonl.gz --out-json results.json
    .venv/bin/python scripts/research/bsp_drift_study.py replay \
        --ckpt CKPT1.jsonl.gz ... --out-json replay.json

Decision-support only — nothing here places bets. Does NOT touch app/ code.
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import json
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Frozen replay rule — imported, never redefined (ah_anchor_backtest.py is the
# canonical frozen source: EDGE_MIN=0.03, ODDS_MIN=1.6, ODDS_MAX=4.0, POWER).
import scripts.research.ah_anchor_backtest as ah  # noqa: E402
from app.ingestion.betfair_bsp import (  # noqa: E402
    DRAW_SELECTION_ID,
    SOCCER_EVENT_TYPE_ID,
    _parse_market_time,
    _peek_market_def,
    _RunnerLadder,
    _to_decimal,
)

MARKET_TYPES = ("MATCH_ODDS", "OVER_UNDER_25")
CUTOFF_HOURS = (24.0, 3.0, 1.0)  # T-24h, T-3h (short window), T-60m
SEED = 20260710
B_BOOT = 2000
AUC_BAR = 0.55
MIN_CONSECUTIVE_FOLDS = 6
MIN_FOLD_MARKETS = 200  # a fold below this is reported but ineligible for the streak
PLAUSIBLE_MONTHS = ("2024-01", "2026-12")  # kickoff sanity band (archive era)
KO_MOVED_TOL_S = 30 * 60
FRESH24_MAX_AGE_H = 12.0  # T-24h snapshot must be based on a message <= 12h older
FRESH60_MAX_AGE_H = 3.0  # T-60m snapshot must be based on a message <= 3h older


# --------------------------------------------------------------------------
# extract: streaming tar pass -> per-market pre-off price-path checkpoint
# --------------------------------------------------------------------------


def parse_market_path(lines: list[str]) -> dict | None:
    """One market's mcm sequence -> JSON row with prices at each pre-off cutoff.

    Cutoffs are computed from the FIRST marketDefinition's marketTime. Each
    cutoff snapshot is taken before applying the first message whose pt
    exceeds it (stream pt is monotonic), so a snapshot only ever contains
    state from messages with pt <= cutoff — the leakage guarantee.
    """
    kickoff: datetime | None = None
    first_kickoff: datetime | None = None
    cutoffs: list[float] | None = None  # epoch-ms thresholds, ascending
    snaps: list[dict[int, str] | None] = [None] * len(CUTOFF_HOURS)
    snap_asof: list[float | None] = [None] * len(CUTOFF_HOURS)
    ladders: dict[int, _RunnerLadder] = {}
    latest_def: dict | None = None
    pre_inplay: dict[int, str] | None = None
    in_play_ms: float | None = None
    last_applied_pt: float | None = None
    market_id: str | None = None
    n_msgs_pre = [0] * len(CUTOFF_HOURS)

    def _prices_now() -> dict[int, str]:
        return {
            sid: str(price)
            for sid, ladder in ladders.items()
            if (price := ladder.best_back()) is not None
        }

    for line in lines:
        if not line or not line.strip():
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(msg, dict) or msg.get("op") != "mcm":
            continue
        pt = msg.get("pt")
        if isinstance(pt, (int, float)) and cutoffs is not None:
            for k, cut_ms in enumerate(cutoffs):
                if snaps[k] is None and pt > cut_ms:
                    snaps[k] = _prices_now()
                    snap_asof[k] = last_applied_pt
        for mc in msg.get("mc", []) or []:
            if not isinstance(mc, dict):
                continue
            if mc.get("id"):
                market_id = str(mc["id"])
            mdef = mc.get("marketDefinition")
            if isinstance(mdef, dict):
                latest_def = mdef
                ko = _parse_market_time(mdef.get("marketTime"))
                if ko is not None:
                    kickoff = ko
                    if first_kickoff is None:
                        first_kickoff = ko
                        cutoffs = [
                            (ko - timedelta(hours=h)).timestamp() * 1000.0 for h in CUTOFF_HOURS
                        ]
                if mdef.get("inPlay") is True and in_play_ms is None:
                    pre_inplay = _prices_now()
                    if isinstance(pt, (int, float)):
                        in_play_ms = float(pt)
            for rc in mc.get("rc", []) or []:
                if not isinstance(rc, dict):
                    continue
                sid = rc.get("id")
                if not isinstance(sid, int):
                    continue
                ladders.setdefault(sid, _RunnerLadder()).apply(rc)
        if isinstance(pt, (int, float)):
            last_applied_pt = float(pt)
            if cutoffs is not None:
                for k, cut_ms in enumerate(cutoffs):
                    if pt <= cut_ms:
                        n_msgs_pre[k] += 1

    if latest_def is None or first_kickoff is None:
        return None
    raw_runners = latest_def.get("runners")
    if not isinstance(raw_runners, list) or not raw_runners:
        return None

    close_snap = pre_inplay if pre_inplay is not None else _prices_now()
    runners = []
    for rd in raw_runners:
        if not isinstance(rd, dict) or not isinstance(rd.get("id"), int):
            continue
        sid = rd["id"]
        bsp = _to_decimal(rd.get("bsp"))
        status = str(rd.get("status") or "")
        sp = rd.get("sortPriority")
        runners.append(
            {
                "sid": sid,
                "name": rd.get("name") if isinstance(rd.get("name"), str) else None,
                "sp": sp if isinstance(sp, int) else 99,
                "status": status,
                "bsp": str(bsp) if bsp is not None else None,
                "close": (str(bsp) if bsp is not None else close_snap.get(sid)),
                "p": [snaps[k].get(sid) if snaps[k] is not None else None for k in range(3)],
            }
        )
    if not runners:
        return None
    runners.sort(key=lambda r: (r["sp"], r["sid"]))

    ko_moved = abs((kickoff - first_kickoff).total_seconds()) > KO_MOVED_TOL_S if kickoff else False
    comp = latest_def.get("competition")
    return {
        "market_id": market_id or "",
        "market_type": latest_def.get("marketType"),
        "event_name": latest_def.get("eventName"),
        "competition": comp.get("name") if isinstance(comp, dict) else None,
        "kickoff_utc": first_kickoff.isoformat(),
        "ko_moved": ko_moved,
        "in_play_utc": (
            datetime.fromtimestamp(in_play_ms / 1000.0, tz=UTC).isoformat()
            if in_play_ms is not None
            else None
        ),
        "settled": str(latest_def.get("status") or "") == "CLOSED",
        "bsp_reconciled": bool(latest_def.get("bspReconciled")),
        "snap_asof": [
            datetime.fromtimestamp(t / 1000.0, tz=UTC).isoformat() if t is not None else None
            for t in snap_asof
        ],
        "n_msgs_pre": n_msgs_pre,
        "runners": runners,
    }


def cmd_extract(args: argparse.Namespace) -> int:
    tar_path: Path = args.tar
    out: Path = args.out
    done = out.with_suffix(out.suffix + ".done")
    if done.is_file() and not args.force:
        print(f"already extracted ({done}) — skipping; --force to redo")
        return 0
    if not tar_path.is_file():
        print(f"tar not found: {tar_path}", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".partial")
    wanted = set(MARKET_TYPES)
    scanned = kept = 0
    t0 = time.time()
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        try:
            with tarfile.open(tar_path, mode="r|*") as tar:
                for member in tar:
                    scanned += 1
                    if scanned % 100_000 == 0:
                        rate = scanned / max(time.time() - t0, 1e-9)
                        print(
                            f"[{tar_path.name}] {scanned} members, {kept} kept, "
                            f"{rate:.0f}/s, {time.time() - t0:.0f}s",
                            flush=True,
                        )
                    if args.limit and scanned > args.limit:
                        break
                    if not member.isfile() or not member.name.endswith(".bz2"):
                        continue
                    fobj = tar.extractfile(member)
                    if fobj is None:
                        continue
                    try:
                        raw = bz2.decompress(fobj.read())
                    except (OSError, ValueError, EOFError):
                        continue
                    lines = raw.decode("utf-8", errors="replace").splitlines()
                    et, mt = _peek_market_def(lines)
                    if et != SOCCER_EVENT_TYPE_ID or mt not in wanted:
                        continue
                    row = parse_market_path(lines)
                    if row is None or row["market_type"] not in wanted:
                        continue
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                    kept += 1
        except (tarfile.TarError, OSError) as exc:
            # documented truncated-tail members — record and finish cleanly
            print(f"[{tar_path.name}] tar read ended early: {type(exc).__name__}", flush=True)
    tmp.rename(out)
    meta = {"tar": str(tar_path), "scanned": scanned, "kept": kept, "elapsed_s": time.time() - t0}
    done.write_text(json.dumps(meta))
    print(f"[{tar_path.name}] DONE {json.dumps(meta)}")
    return 0


# --------------------------------------------------------------------------
# analyze: fit-free monotonic association readout, monthly folds
# --------------------------------------------------------------------------


@dataclass
class RunnerRow:
    market_id: str
    market_type: str
    month: str
    day: str  # market-day cluster key
    sid: int
    d24: float  # ln(p60 / p24)
    d3: float | None  # ln(p60 / p3h)
    vel: float | None  # d24 per hour of actual observed span
    y: float  # ln(close / p60); close = BSP else last pre-in-play best-back
    is_bsp: bool = False  # True when the close is a reconciled BSP


def _load_markets(ckpts: list[Path]) -> tuple[list[dict], dict[str, int]]:
    """Load checkpoint rows, dedupe by market_id (first wins), count dupes."""
    seen: set[str] = set()
    rows: list[dict] = []
    counts = {"raw": 0, "dup_market_id": 0}
    for p in ckpts:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                d = json.loads(line)
                counts["raw"] += 1
                mid = d.get("market_id") or ""
                if mid in seen:
                    counts["dup_market_id"] += 1
                    continue
                seen.add(mid)
                rows.append(d)
    return rows, counts


def _fresh(asof_iso: str | None, cutoff: datetime, max_age_h: float) -> bool:
    if not asof_iso:
        return False
    asof = datetime.fromisoformat(asof_iso)
    return (cutoff - asof).total_seconds() <= max_age_h * 3600.0


def build_rows(markets: list[dict]) -> tuple[list[RunnerRow], dict[str, int]]:
    """Markets -> runner rows with every exclusion reason counted."""
    c: dict[str, int] = {
        "markets_in": 0,
        "excl_not_settled": 0,
        "excl_ko_implausible": 0,
        "excl_ko_moved": 0,
        "excl_inplay_before_t60": 0,
        "markets_bsp_reconciled": 0,
        "runner_no_close": 0,
        "runner_no_p60": 0,
        "runner_no_p24": 0,
        "runner_stale_t24": 0,
        "runner_stale_t60": 0,
        "runner_bad_y": 0,
        "runners_kept": 0,
        "markets_kept": 0,
    }
    out: list[RunnerRow] = []
    for m in markets:
        c["markets_in"] += 1
        if not m.get("settled"):
            c["excl_not_settled"] += 1
            continue
        ko = datetime.fromisoformat(m["kickoff_utc"])
        month = f"{ko.year:04d}-{ko.month:02d}"
        if not (PLAUSIBLE_MONTHS[0] <= month <= PLAUSIBLE_MONTHS[1]):
            c["excl_ko_implausible"] += 1
            continue
        if m.get("ko_moved"):
            c["excl_ko_moved"] += 1
            continue
        t60 = ko - timedelta(hours=CUTOFF_HOURS[2])
        t24 = ko - timedelta(hours=CUTOFF_HOURS[0])
        t3 = ko - timedelta(hours=CUTOFF_HOURS[1])
        ip = m.get("in_play_utc")
        if ip is not None and datetime.fromisoformat(ip) <= t60:
            c["excl_inplay_before_t60"] += 1
            continue
        # target close = reconciled BSP else last pre-in-play best-back — the
        # repo's canonical BetfairMarketClose.close_price convention (the price
        # CLV is scored against). BSP-reconciled fraction reported; the
        # BSP-only subset is a sensitivity split (`is_bsp` on each row).
        if m.get("bsp_reconciled"):
            c["markets_bsp_reconciled"] += 1
        asof = m.get("snap_asof") or [None, None, None]
        kept_any = False
        for r in m.get("runners", []):
            close = r.get("close")
            p24, p3h, p60 = (r.get("p") or [None, None, None])[:3]
            if close is None:
                c["runner_no_close"] += 1
                continue
            if p60 is None:
                c["runner_no_p60"] += 1
                continue
            if p24 is None:
                c["runner_no_p24"] += 1
                continue
            if not _fresh(asof[0], t24, FRESH24_MAX_AGE_H):
                c["runner_stale_t24"] += 1
                continue
            if not _fresh(asof[2], t60, FRESH60_MAX_AGE_H):
                c["runner_stale_t60"] += 1
                continue
            f24, f60, fclose = float(p24), float(p60), float(close)
            if min(f24, f60, fclose) <= 1.0:
                c["runner_bad_y"] += 1
                continue
            d24 = float(np.log(f60 / f24))
            y = float(np.log(fclose / f60))
            if not (np.isfinite(d24) and np.isfinite(y)):
                c["runner_bad_y"] += 1
                continue
            d3 = None
            if p3h is not None and float(p3h) > 1.0 and _fresh(asof[1], t3, FRESH60_MAX_AGE_H):
                d3 = float(np.log(f60 / float(p3h)))
            vel = None
            if asof[0] and asof[2]:
                span_h = (
                    datetime.fromisoformat(asof[2]) - datetime.fromisoformat(asof[0])
                ).total_seconds() / 3600.0
                if span_h > 0.5:
                    vel = d24 / span_h
            out.append(
                RunnerRow(
                    market_id=m["market_id"],
                    market_type=m["market_type"],
                    month=month,
                    day=ko.date().isoformat(),
                    sid=r["sid"],
                    d24=d24,
                    d3=d3,
                    vel=vel,
                    y=y,
                    is_bsp=r.get("bsp") is not None,
                )
            )
            c["runners_kept"] += 1
            kept_any = True
        if kept_any:
            c["markets_kept"] += 1
    return out, c


def _auc(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """Mann-Whitney AUC of `scores` for predicting labels==True."""
    pos = scores[labels]
    neg = scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return None
    # tie-aware midranks (Mann-Whitney)
    allv = np.concatenate([pos, neg])
    _, inv, cnt = np.unique(allv, return_inverse=True, return_counts=True)
    csum = np.cumsum(cnt)
    avg_rank = csum - (cnt - 1) / 2.0
    r = avg_rank[inv]
    return float((r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 10:
        return None

    def rank(v: np.ndarray) -> np.ndarray:
        uniq, inv, cnt = np.unique(v, return_inverse=True, return_counts=True)
        csum = np.cumsum(cnt)
        return (csum - (cnt - 1) / 2.0)[inv]

    rx, ry = rank(x), rank(y)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def _boot_auc_ci(
    rows: list[RunnerRow], feat: str, rng: np.random.Generator
) -> tuple[float, float] | None:
    """Date-clustered bootstrap 95% CI on pooled AUC."""
    days = sorted({r.day for r in rows})
    if len(days) < 20:
        return None
    by_day: dict[str, list[RunnerRow]] = {}
    for r in rows:
        by_day.setdefault(r.day, []).append(r)
    vals = np.full(B_BOOT, np.nan)
    day_arr = list(by_day.values())
    g = len(day_arr)
    for b in range(B_BOOT):
        take = rng.integers(0, g, size=g)
        sc, lb = [], []
        for i in take:
            for r in day_arr[i]:
                f = getattr(r, feat)
                if f is not None:
                    sc.append(f)
                    lb.append(r.y > 0)
        if sc:
            a = _auc(np.asarray(sc), np.asarray(lb, dtype=bool))
            if a is not None:
                vals[b] = a
    return float(np.nanpercentile(vals, 2.5)), float(np.nanpercentile(vals, 97.5))


def analyze(rows: list[RunnerRow]) -> dict:
    result: dict = {}
    rng = np.random.default_rng(SEED)
    for mt in MARKET_TYPES:
        sub = [r for r in rows if r.market_type == mt]
        months = sorted({r.month for r in sub})
        folds = []
        streak = best_streak = 0
        for mo in months:
            fr = [r for r in sub if r.month == mo]
            n_mkts = len({r.market_id for r in fr})
            d24 = np.array([r.d24 for r in fr])
            y = np.array([r.y for r in fr])
            auc24 = _auc(d24, y > 0)
            sp24 = _spearman(d24, y)
            d3_rows = [(r.d3, r.y) for r in fr if r.d3 is not None]
            auc3 = (
                _auc(np.array([a for a, _ in d3_rows]), np.array([b for _, b in d3_rows]) > 0)
                if d3_rows
                else None
            )
            eligible = n_mkts >= MIN_FOLD_MARKETS
            passed = eligible and auc24 is not None and auc24 >= AUC_BAR
            streak = streak + 1 if passed else 0
            best_streak = max(best_streak, streak)
            folds.append(
                {
                    "month": mo,
                    "n_markets": n_mkts,
                    "n_runners": len(fr),
                    "auc_d24": auc24,
                    "spearman_d24": sp24,
                    "auc_d3": auc3,
                    "eligible": eligible,
                    "pass": passed,
                }
            )
        # pooled
        d24 = np.array([r.d24 for r in sub])
        y = np.array([r.y for r in sub])
        pooled_auc = _auc(d24, y > 0) if len(sub) else None
        auc_ci = _boot_auc_ci(sub, "d24", rng) if len(sub) else None
        # decile table of d24 -> mean y (ddof=1 SE)
        deciles = []
        if len(sub) >= 100:
            qs = np.quantile(d24, np.linspace(0, 1, 11))
            for i in range(10):
                lo, hi = qs[i], qs[i + 1]
                mask = (d24 >= lo) & (d24 <= hi if i == 9 else d24 < hi)
                if mask.sum() > 1:
                    deciles.append(
                        {
                            "decile": i + 1,
                            "d24_range": [float(lo), float(hi)],
                            "n": int(mask.sum()),
                            "mean_y": float(y[mask].mean()),
                            "se_y": float(y[mask].std(ddof=1) / np.sqrt(mask.sum())),
                            "frac_y_pos": float((y[mask] > 0).mean()),
                        }
                    )
        bsp_sub = [r for r in sub if r.is_bsp]
        result[mt] = {
            "n_runners": len(sub),
            "n_markets": len({r.market_id for r in sub}),
            "n_runners_bsp_only": len(bsp_sub),
            "pooled_auc_d24_bsp_only": (
                _auc(np.array([r.d24 for r in bsp_sub]), np.array([r.y for r in bsp_sub]) > 0)
                if bsp_sub
                else None
            ),
            "pooled_auc_d24": pooled_auc,
            "pooled_auc_d24_ci95_dayclustered": auc_ci,
            "pooled_spearman_d24": _spearman(d24, y) if len(sub) else None,
            "best_consecutive_pass_streak": best_streak,
            "bar_met_auc": best_streak >= MIN_CONSECUTIVE_FOLDS,
            "folds": folds,
            "deciles_d24": deciles,
        }
    return result


def cmd_analyze(args: argparse.Namespace) -> int:
    markets, load_counts = _load_markets(args.ckpt)
    rows, counts = build_rows(markets)
    res = {
        "load_counts": load_counts,
        "coverage_counts": counts,
        "analysis": analyze(rows),
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(res, indent=1))
    print(json.dumps({"load": load_counts, "coverage": counts}, indent=1))
    for mt, a in res["analysis"].items():
        print(
            f"{mt}: n_mkts={a['n_markets']} pooled AUC(d24)={a['pooled_auc_d24']} "
            f"CI={a['pooled_auc_d24_ci95_dayclustered']} "
            f"best_streak={a['best_consecutive_pass_streak']} bar_met={a['bar_met_auc']}"
        )
    return 0


# --------------------------------------------------------------------------
# replay: frozen selection rule + drift filter -> CLV delta
# --------------------------------------------------------------------------


def cmd_replay(args: argparse.Namespace) -> int:
    from app.ingestion.betfair_bsp import attach_betfair_close, read_market_cache
    from app.probabilities.devig import devig
    from app.resolution.matching import default_aliases

    markets_ckpt, _ = _load_markets(args.ckpt)
    drift_by_market: dict[str, dict] = {m["market_id"]: m for m in markets_ckpt}

    # fixtures overlapping the BSP archive window (2024-08..2026-06)
    fixtures = ah.load_fixtures(("2425", "2526"), ah.LEAGUES)
    v1 = [f for f in fixtures if f.ps is not None and f.mx is not None]
    for fx in v1:
        fx.fair_v1 = devig(fx.ps, ah.DEVIG)  # type: ignore[arg-type]

    cache = [m for m in read_market_cache(ah.BSP_CACHE) if m.market_type == "MATCH_ODDS"]
    fd_rows = [
        {
            "_ridx": str(i),
            "HomeTeam": f.home,
            "AwayTeam": f.away,
            "Date": f.kickoff_date.strftime("%d/%m/%Y"),
            "FTR": f.ftr,
        }
        for i, f in enumerate(v1)
    ]
    joined, stats = attach_betfair_close(fd_rows, cache, aliases=default_aliases())
    mkt_by_ridx = {int(r["_ridx"]): r["BetfairMarketId"] for r in joined}
    for r in joined:
        closes = ah._odds3(r, ("PSCH", "PSCD", "PSCA"))
        if closes is not None:
            v1[int(r["_ridx"])].bf_close = closes

    picks = []  # (date, clv, drift_or_None, won, price)
    c = {
        "fixtures": len(v1),
        "joined_bsp": stats.n_joined,
        "picks_rule": 0,
        "picks_clv": 0,
        "picks_with_drift": 0,
    }
    for i, fx in enumerate(v1):
        if fx.fair_v1 is None or fx.mx is None:
            continue
        sel = ah._select(fx.fair_v1, fx.mx)
        if sel is None:
            continue
        c["picks_rule"] += 1
        p_close = ah._close_probs(fx.bf_close, "bsp") if fx.bf_close else None
        if p_close is None:
            continue
        price = fx.mx[sel]
        clv = float(np.log(price * p_close[sel]))
        c["picks_clv"] += 1
        drift = None
        mid = mkt_by_ridx.get(i)
        m = drift_by_market.get(mid) if mid else None
        if m is not None:
            # map sel (0=H 1=D 2=A) to runner: draw = fixed id; home/away by
            # sortPriority order of non-draw runners (Betfair convention,
            # matches attach_betfair_close's candidate construction).
            runners = m.get("runners", [])
            non_draw = [r for r in runners if r["sid"] != DRAW_SELECTION_ID]
            target = None
            if sel == 1:
                target = next((r for r in runners if r["sid"] == DRAW_SELECTION_ID), None)
            elif len(non_draw) >= 2:
                target = non_draw[0] if sel == 0 else non_draw[1]
            if target is not None:
                p24, _, p60 = (target.get("p") or [None, None, None])[:3]
                ko = datetime.fromisoformat(m["kickoff_utc"])
                asof = m.get("snap_asof") or [None, None, None]
                t24 = ko - timedelta(hours=CUTOFF_HOURS[0])
                t60 = ko - timedelta(hours=CUTOFF_HOURS[2])
                if (
                    p24 is not None
                    and p60 is not None
                    and float(p24) > 1.0
                    and float(p60) > 1.0
                    and _fresh(asof[0], t24, FRESH24_MAX_AGE_H)
                    and _fresh(asof[2], t60, FRESH60_MAX_AGE_H)
                ):
                    drift = float(np.log(float(p60) / float(p24)))
        if drift is not None:
            c["picks_with_drift"] += 1
        won = "HDA".index(fx.ftr) == sel
        picks.append((fx.kickoff_date, clv, drift, won, price))

    # universe for the delta: picks with BOTH clv and drift (same base set)
    base = [(d, clv, drift, won, price) for d, clv, drift, won, price in picks if drift is not None]
    kept = [p for p in base if p[2] < 0.0]  # drift filter: market moved TOWARD the pick

    def _mean_clv(rows: list) -> float | None:
        return float(np.mean([r[1] for r in rows])) if rows else None

    def _roi(rows: list) -> float | None:
        if not rows:
            return None
        return float(np.mean([(r[4] - 1.0) if r[3] else -1.0 for r in rows]))

    # date-clustered paired bootstrap on delta = mean(kept) - mean(base)
    rng = np.random.default_rng(SEED)
    days = sorted({r[0] for r in base})
    by_day: dict = {}
    for r in base:
        by_day.setdefault(r[0], []).append(r)
    day_rows = [by_day[d] for d in days]
    g = len(day_rows)
    deltas = np.full(B_BOOT, np.nan)
    for b in range(B_BOOT):
        take = rng.integers(0, g, size=g)
        allv, keptv = [], []
        for i in take:
            for r in day_rows[i]:
                allv.append(r[1])
                if r[2] < 0.0:
                    keptv.append(r[1])
        if allv and keptv:
            deltas[b] = float(np.mean(keptv) - np.mean(allv))
    point = (_mean_clv(kept) - _mean_clv(base)) if kept and base else None
    ci = (
        (float(np.nanpercentile(deltas, 2.5)), float(np.nanpercentile(deltas, 97.5))) if g else None
    )
    res = {
        "counts": c,
        "n_base": len(base),
        "n_kept_drift_neg": len(kept),
        "mean_clv_base": _mean_clv(base),
        "mean_clv_kept": _mean_clv(kept),
        "clv_delta_kept_minus_base": point,
        "clv_delta_ci95_dayclustered": ci,
        "roi_base": _roi(base),
        "roi_kept": _roi(kept),
        "bar_met_clv": (point is not None and ci is not None and point > 0 and ci[0] > 0),
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract", help="tar -> pre-off price-path checkpoint")
    ex.add_argument("--tar", type=Path, required=True)
    ex.add_argument("--out", type=Path, required=True)
    ex.add_argument("--limit", type=int, default=0, help="stop after N members (pipeline check)")
    ex.add_argument("--force", action="store_true")
    an = sub.add_parser("analyze", help="fit-free association readout, monthly folds")
    an.add_argument("--ckpt", type=Path, nargs="+", required=True)
    an.add_argument("--out-json", type=Path, required=True)
    rp = sub.add_parser("replay", help="frozen-rule CLV replay with drift filter")
    rp.add_argument("--ckpt", type=Path, nargs="+", required=True)
    rp.add_argument("--out-json", type=Path, required=True)
    args = ap.parse_args(argv)
    return {"extract": cmd_extract, "analyze": cmd_analyze, "replay": cmd_replay}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
