"""One-shot OddsPapi signup verification for the staged ARCADIA cross-check.

Runnable the moment the operator creates an account (NO account exists at
build time — this script makes the FIRST live calls). It answers the A7
evaluation's conditions precedent
(docs/research/2026-07-05-oddspapi-crosscheck-evaluation.md):

  (i)   does the key authenticate?
  (ii)  is the free-tier quota visible, and is it >= ~100 req/month
        (the bar for an N=40+ monthly cross-check)?
  (iii) do recent FINISHED football fixtures actually carry Pinnacle
        pre-kickoff price points (a derivable close)?
  (iv)  numbers-only GO/NO-GO summary.

Budget: <= 10 requests TOTAL (hard-enforced via OddsPapiPolicy
max_requests_per_run=10 — the client raises before an 11th request).

Research script, NOT app runtime: reads the key from env directly. The key
is NEVER printed, and no full URL is ever printed (the key rides the query
string; httpx/httpcore loggers are pinned to WARNING by the client).

Usage (from the repo root, after the operator puts the key in .env):

    ODDSPAPI_KEY=... .venv/bin/python scripts/research/verify_oddspapi.py
    # optional: --sport-id <football sportId> --tournament-id <id>
    #           --fixture-id <id> (skip resolution, spend budget on odds)

Read-only GET; mints no picks, writes nothing to the DB.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import statistics
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.ingestion.oddspapi import (
    OddsPapiCrosscheckClient,
    OddsPapiFixture,
    OddsPapiPolicy,
    _flatten_outcome_entries,  # module-internal reuse is fine for a research script
    parse_fixtures,
    parse_tournaments,
    price_history_open_close_before,
)

SCRIPT_REQUEST_BUDGET = 10
QUOTA_BAR_PER_MONTH = 100  # A7 condition 1: below this the free tier is NO-GO
MAX_ODDS_SAMPLES = 3  # historical-odds fixtures sampled (1 req each)
FINISHED_LOOKBACK_DAYS = 30
FINISHED_MARGIN_HOURS = 3  # kickoff at least this long ago -> treat as finished


def _status_of(exc: httpx.HTTPStatusError) -> int:
    return exc.response.status_code


def _pre_ko_stats(payload: dict, kickoff: datetime) -> dict[str, Any]:
    """Per-fixture Pinnacle history stats: total points, pre-KO points, minutes
    between the last pre-KO point and kickoff (min across outcomes = the point
    closest to KO), and whether a pre-KO (open, close) pair is derivable."""
    books = payload.get("bookmakers")
    total = 0
    pre_ko = 0
    last_ages_min: list[float] = []
    has_close = False
    if isinstance(books, dict):
        for book_node in books.values():
            markets = book_node.get("markets") if isinstance(book_node, dict) else None
            if not isinstance(markets, dict):
                continue
            for market in markets.values():
                outcomes = market.get("outcomes") if isinstance(market, dict) else None
                if not isinstance(outcomes, dict):
                    continue
                for outcome_node in outcomes.values():
                    entries = _flatten_outcome_entries(outcome_node)
                    total += len(entries)
                    opening, closing = price_history_open_close_before(entries, kickoff)
                    if closing is not None:
                        has_close = True
                    ts: list[datetime] = []
                    for entry in entries:
                        raw = entry.get("createdAt")
                        if isinstance(raw, str):
                            try:
                                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                            except ValueError:
                                continue
                            if parsed.tzinfo is not None:
                                ts.append(parsed.astimezone(UTC))
                    pre = [t for t in ts if t < kickoff]
                    pre_ko += len(pre)
                    if pre:
                        last_ages_min.append((kickoff - max(pre)).total_seconds() / 60.0)
    return {
        "total_points": total,
        "pre_ko_points": pre_ko,
        "last_pre_ko_age_min": min(last_ages_min) if last_ages_min else None,
        "has_pre_ko_close": has_close,
    }


async def _run(args: argparse.Namespace) -> int:
    key = os.environ.get("ODDSPAPI_KEY", "").strip()
    if not key:
        print("ODDSPAPI_KEY is not set. Operator steps:")
        print("  1. Create the free account at oddspapi.io (confirm quota + ToS at signup).")
        print("  2. Put the key in .env as ODDSPAPI_KEY=... (gitignored, 0600; never commit).")
        print("  3. Re-run: ODDSPAPI_KEY=... .venv/bin/python scripts/research/verify_oddspapi.py")
        return 2

    policy = OddsPapiPolicy(
        enabled=True,  # explicit, script-local: this IS the operator's first live probe
        api_key=key,
        bookmakers=("pinnacle",),
        max_requests_per_run=SCRIPT_REQUEST_BUDGET,
    )

    auth_ok = False
    auth_status: int | None = None
    quota_headers: dict[str, str] = {}
    tournaments_n = 0
    fixtures_listed = 0
    sample_fixture_keys: list[str] = []
    finished: list[OddsPapiFixture] = []
    odds_stats: list[dict[str, Any]] = []

    async with OddsPapiCrosscheckClient(policy) as client:
        # (i)+(ii) auth + quota visibility on the cheapest listing endpoint.
        try:
            payload = await client.tournaments(sport_id=args.sport_id)
            auth_ok = True
            auth_status = 200
            quota_headers = dict(client.last_rate_headers)
            tournaments = parse_tournaments(payload)
            tournaments_n = len(tournaments)
        except httpx.HTTPStatusError as exc:
            auth_status = _status_of(exc)
            auth_ok = auth_status not in (401, 403)
            tournaments = []
        except httpx.HTTPError as exc:
            print(f"transport error on /tournaments: {type(exc).__name__}")
            tournaments = []

        # (iii)a fixture resolution -> recent finished fixtures.
        now = datetime.now(tz=UTC)
        if args.fixture_id:
            finished = [OddsPapiFixture.model_validate({"fixtureId": args.fixture_id})]
        elif auth_ok:
            tournament_ids = (
                [args.tournament_id]
                if args.tournament_id
                else [t.tournament_id for t in tournaments[:2]]
            )
            for tid in tournament_ids:
                if len(finished) >= MAX_ODDS_SAMPLES:
                    break
                try:
                    fx_payload = await client.fixtures(tid)
                except httpx.HTTPStatusError as exc:
                    print(f"/fixtures tournamentId={tid}: HTTP {_status_of(exc)}")
                    continue
                except httpx.HTTPError as exc:
                    print(f"/fixtures tournamentId={tid}: {type(exc).__name__}")
                    continue
                fixtures = parse_fixtures(fx_payload)
                fixtures_listed += len(fixtures)
                if fixtures and not sample_fixture_keys:
                    rows = fx_payload if isinstance(fx_payload, list) else []
                    if rows and isinstance(rows[0], dict):
                        sample_fixture_keys = sorted(rows[0].keys())
                cutoff_new = now - timedelta(hours=FINISHED_MARGIN_HOURS)
                cutoff_old = now - timedelta(days=FINISHED_LOOKBACK_DAYS)
                for fixture in fixtures:
                    if fixture.start_time is None:
                        continue
                    if cutoff_old <= fixture.start_time <= cutoff_new:
                        finished.append(fixture)
                        if len(finished) >= MAX_ODDS_SAMPLES:
                            break

        # (iii)b historical odds for the finished samples (1 req each).
        for fixture in finished[:MAX_ODDS_SAMPLES]:
            try:
                hist = await client.historical_odds(fixture.fixture_id)
            except httpx.HTTPStatusError as exc:
                print(f"/historical-odds fixture={fixture.fixture_id}: HTTP {_status_of(exc)}")
                continue
            except httpx.HTTPError as exc:
                print(f"/historical-odds fixture={fixture.fixture_id}: {type(exc).__name__}")
                continue
            kickoff = fixture.start_time or now  # --fixture-id path may lack KO
            odds_stats.append(_pre_ko_stats(hist, kickoff))
            if not quota_headers:
                quota_headers = dict(client.last_rate_headers)

        used = client.requests_used

    # ---- numbers-only summary ------------------------------------------------
    with_history = sum(1 for s in odds_stats if s["total_points"] > 0)
    with_pre_ko_close = sum(1 for s in odds_stats if s["has_pre_ko_close"])
    ages = [s["last_pre_ko_age_min"] for s in odds_stats if s["last_pre_ko_age_min"] is not None]
    quota_remaining: int | None = None
    for name, value in quota_headers.items():
        if "remaining" in name.lower():
            with contextlib.suppress(ValueError):
                quota_remaining = int(float(value))
            break

    quota_text = str(quota_remaining) if quota_remaining is not None else "unknown"
    print("\n================ ODDSPAPI VERIFY ================")
    print(f"requests_used            : {used}/{SCRIPT_REQUEST_BUDGET}")
    print(f"auth_ok                  : {int(auth_ok)} (HTTP {auth_status})")
    print(f"quota_headers            : {quota_headers if quota_headers else 'none-visible'}")
    print(f"quota_remaining          : {quota_text}")
    print(f"tournaments_parsed       : {tournaments_n}")
    print(f"fixtures_listed          : {fixtures_listed}")
    if sample_fixture_keys:
        print(f"fixture_row_keys         : {sample_fixture_keys}")
    print(f"finished_sampled         : {len(odds_stats)}")
    print(f"with_pinnacle_history    : {with_history}")
    print(f"with_pre_ko_close        : {with_pre_ko_close}")
    print(
        f"median_last_pre_ko_age_m : {statistics.median(ages):.1f}"
        if ages
        else "median_last_pre_ko_age_m : n/a"
    )
    print("=================================================")

    quota_ok = quota_remaining is None or quota_remaining >= QUOTA_BAR_PER_MONTH
    go = auth_ok and with_pre_ko_close >= 1 and quota_ok
    reasons: list[str] = []
    if not auth_ok:
        reasons.append("auth_failed")
    if with_pre_ko_close < 1:
        reasons.append("no_pre_ko_pinnacle_points")
    if not quota_ok:
        reasons.append(f"quota_below_{QUOTA_BAR_PER_MONTH}")
    if quota_remaining is None:
        reasons.append("quota_not_header_visible_confirm_in_dashboard")
    print(f"VERDICT: {'GO' if go else 'NO-GO'}" + (f" [{','.join(reasons)}]" if reasons else ""))
    return 0 if go else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify OddsPapi free tier for the cross-check")
    parser.add_argument("--sport-id", type=int, default=None, help="football sportId if known")
    parser.add_argument("--tournament-id", default="", help="skip tournament discovery")
    parser.add_argument("--fixture-id", default="", help="skip resolution; probe this fixture")
    sys.exit(asyncio.run(_run(parser.parse_args())))
