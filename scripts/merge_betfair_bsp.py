#!/usr/bin/env python
"""Merge newly-downloaded Betfair historical soccer closes INTO the existing BSP
caches without losing what is already cached.

WHY a dedicated merge: value_backtest's ``run-betfair-bsp`` SHORT-CIRCUITS on an
existing cache (``soccer_match_odds.jsonl.gz`` etc.) — if the cache exists it is
read as-is and new files are ignored. We hold only the parsed CACHE for the
2024-25 season (not the original raw archive), so a naive re-scan would lose it.
This script parses the new download, UNIONs it with each existing cache (dedup by
market_id), and rewrites the cache — additive, idempotent, order-stable.

Read-only on Betfair data; writes ONLY the three jsonl.gz caches. Places no bets,
logs no credentials (ADR-0002 / safety rules).

  # 1. Drop the new download (a .tar Basic archive, or loose .bz2/.json market
  #    stream files) into the incoming dir, then:
  uv run python scripts/merge_betfair_bsp.py --incoming data/betfair/bsp/incoming
  # 2. Re-verify the held-out CLV under the new ddof=1 SE + bootstrap ROI CI:
  uv run python scripts/value_backtest.py run-betfair-bsp
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from app.ingestion.betfair_bsp import (
    BetfairMarketClose,
    load_betfair_dir,
    load_betfair_tar_by_type,
    read_market_cache,
    write_market_cache,
)

# market_type -> cache filename (the three soccer close types value_backtest reads)
CACHES: dict[str, str] = {
    "MATCH_ODDS": "soccer_match_odds.jsonl.gz",
    "OVER_UNDER_25": "soccer_over_under.jsonl.gz",
    "ASIAN_HANDICAP": "soccer_handicap.jsonl.gz",
}


def _dedup(markets: list[BetfairMarketClose]) -> list[BetfairMarketClose]:
    """Union by market_id, keeping the FIRST occurrence (existing cache wins on a
    clash so a re-download never overwrites a settled close)."""
    seen: set[str] = set()
    out: list[BetfairMarketClose] = []
    for m in markets:
        key = m.market_id or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(m)
    return out


def _coverage(markets: list[BetfairMarketClose]) -> list[str]:
    by_month: Counter[str] = Counter()
    for m in markets:
        if m.kickoff_utc is not None:
            by_month[m.kickoff_utc.strftime("%Y-%m")] += 1
    return sorted(by_month)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--incoming",
        type=Path,
        required=True,
        help="dir holding the new download: a .tar Basic archive (all 3 types) or "
        "loose per-market .bz2/.json stream files (MATCH_ODDS only)",
    )
    ap.add_argument("--bsp-dir", type=Path, default=Path("data/betfair/bsp"))
    args = ap.parse_args(argv)

    if not args.incoming.is_dir():
        print(f"incoming dir not found: {args.incoming}")
        return 1

    tars = sorted(args.incoming.glob("*.tar"))
    new_by_type: dict[str, list[BetfairMarketClose]] = {t: [] for t in CACHES}
    if tars:
        for tar in tars:
            print(f"scanning {tar} for {tuple(CACHES)} (one ~5GB pass)...")
            buckets = load_betfair_tar_by_type(tar, market_types=tuple(CACHES))
            for t in CACHES:
                new_by_type[t].extend(buckets.get(t, []))
    else:
        # Loose stream files: the dir loader extracts MATCH_ODDS only. (For loose
        # OU/AH, repackage them into a .tar, or ask and I will add a by-type dir
        # loader.)
        new_by_type["MATCH_ODDS"] = load_betfair_dir(args.incoming)

    total_added = 0
    for market_type, fname in CACHES.items():
        cache = args.bsp_dir / fname
        existing = read_market_cache(cache) if cache.is_file() else []
        new = new_by_type[market_type]
        if not new and not existing:
            continue
        merged = _dedup(existing + new)
        added = len(merged) - len(existing)
        total_added += added
        write_market_cache(cache, merged)
        months = _coverage(merged)
        span = f"{months[0]} -> {months[-1]} ({len(months)} months)" if months else "no dated rows"
        print(
            f"{market_type:14s}: cached {len(existing):6d} + new-parsed {len(new):6d} "
            f"= {len(merged):6d} (+{added}) | coverage {span}"
        )
    print(
        f"\nDONE. {total_added} new markets merged. Re-verify: "
        "uv run python scripts/value_backtest.py run-betfair-bsp"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
