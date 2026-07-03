"""ARCADIA Pinnacle anchor exporter + preflight — H2 validation workflow ONLY.

Reads the append-only ``odds_snapshots`` warehouse (read-only SELECTs), builds
the auditable anchor dataset defined by ``app/backtesting/arcadia_anchor.py``
(the ADR-0019 2026-07-03 amendment), and writes it under
``data/validation/arcadia/`` with a manifest carrying full provenance (git
SHA, config hash, environment, dataset sha256). Rejected rows are exported,
never dropped. Raw data is never modified (SELECT-only; writers refuse to
overwrite existing outputs).

This script is NOT part of live pick minting, alerting, or gating — it exists
so the future H2 validation run is mechanical and contamination-guarded.
This system places no bets.

Usage:
  uv run python scripts/arcadia_anchor_export.py export \
      --from 2026-07-01 --to 2026-12-31
  uv run python scripts/arcadia_anchor_export.py preflight \
      --dataset data/validation/arcadia/anchors_2026-07-01_2026-12-31.csv
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

from app.backtesting.arcadia_anchor import (
    ANCHOR_SOURCE,
    ELIGIBLE_SOURCE_SPORT_KEYS,
    EXPORT_SCHEMA_VERSION,
    FROZEN_CONFIG_SHA256,
    SnapshotObs,
    build_anchor_rows,
    evaluate_contamination_guards,
    preflight_report,
    read_dataset,
    write_dataset,
    write_manifest,
)
from app.resolution import default_aliases
from app.resolution.matching import EventCandidate, match_event_hardened_scored

_OUT_DIR = Path("data/validation/arcadia")


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # pragma: no cover — provenance best-effort outside a repo
        return "unknown"


def _config_sha256() -> str:
    """The runbook's exact config-freeze recipe over live Settings."""
    from app.config import Settings

    s = Settings()
    keys = (
        "value_devig",
        "value_moneyline_max_odds",
        "value_min_edge",
        "value_volume_min_edge",
        "fractional_kelly",
    )
    payload = json.dumps({k: str(getattr(s, k)) for k in keys}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _env_fingerprint() -> dict[str, str]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }


async def _load_observations(
    window_start: date, window_end: date
) -> tuple[list[SnapshotObs], dict[int, tuple[int, float, str]]]:
    """Read-only pull of pinnacle-namespace snapshots + a canonical-event match
    (observability only) against the OddsPortal-side namespace."""
    from sqlalchemy.orm import aliased

    from app.config import get_settings
    from app.database import create_engine, create_session_factory
    from app.storage.models import Event, League, OddsSnapshot, Sport, Team

    engine = create_engine(get_settings())
    factory = create_session_factory(engine)
    lo = datetime.combine(window_start, time.min, tzinfo=UTC)
    hi = datetime.combine(window_end, time.max, tzinfo=UTC)
    home_t, away_t = aliased(Team), aliased(Team)

    observations: list[SnapshotObs] = []
    pinnacle_events: dict[int, tuple[str, str, datetime]] = {}
    counterparts: dict[str, list[EventCandidate]] = {}
    async with factory() as session:
        stmt = (
            select(
                OddsSnapshot.id,
                OddsSnapshot.event_id,
                Sport.key,
                League.name,
                home_t.name,
                away_t.name,
                Event.starts_at,
                OddsSnapshot.market,
                OddsSnapshot.selection,
                OddsSnapshot.decimal_odds,
                OddsSnapshot.captured_at,
            )
            .join(Event, Event.id == OddsSnapshot.event_id)
            .join(Sport, Sport.id == Event.sport_id)
            .join(League, League.id == Event.league_id)
            .join(home_t, home_t.id == Event.home_team_id)
            .join(away_t, away_t.id == Event.away_team_id)
            .where(
                Sport.key.in_(ELIGIBLE_SOURCE_SPORT_KEYS),
                Event.starts_at >= lo,
                Event.starts_at <= hi,
            )
        )
        for row in (await session.execute(stmt)).all():
            (sid, eid, sport_key, league, home, away, starts, market, sel, odds, cap) = row
            starts = starts if starts.tzinfo else starts.replace(tzinfo=UTC)
            cap = cap if cap.tzinfo else cap.replace(tzinfo=UTC)
            observations.append(
                SnapshotObs(
                    snapshot_id=sid,
                    event_id=eid,
                    sport_key=sport_key,
                    league=league,
                    home=home,
                    away=away,
                    starts_at=starts,
                    market=market,
                    selection=sel,
                    decimal_odds=odds,
                    captured_at=cap,
                )
            )
            pinnacle_events[eid] = (home, away, starts)

        # Counterpart (OddsPortal-side) events for the canonical match tap.
        base_sports = tuple(k.removeprefix("pinnacle_") for k in ELIGIBLE_SOURCE_SPORT_KEYS)
        stmt2 = (
            select(Event.id, Sport.key, home_t.name, away_t.name, Event.starts_at)
            .join(Sport, Sport.id == Event.sport_id)
            .join(home_t, home_t.id == Event.home_team_id)
            .join(away_t, away_t.id == Event.away_team_id)
            .where(
                Sport.key.in_(base_sports),
                Event.starts_at >= lo - timedelta(days=1),
                Event.starts_at <= hi + timedelta(days=1),
            )
        )
        for eid, sport_key, home, away, starts in (await session.execute(stmt2)).all():
            starts = starts if starts.tzinfo else starts.replace(tzinfo=UTC)
            counterparts.setdefault(sport_key, []).append(
                EventCandidate(ref=str(eid), home=home, away=away, kickoff=starts)
            )
    await engine.dispose()

    aliases = default_aliases()
    canonical: dict[int, tuple[int, float, str]] = {}
    for eid, (home, away, starts) in pinnacle_events.items():
        for cands in counterparts.values():
            outcome = match_event_hardened_scored(
                home, away, starts, cands, aliases=aliases, ordered=True
            )
            if outcome is not None:
                canonical[eid] = (
                    int(outcome.candidate.ref),
                    outcome.confidence,
                    outcome.method,
                )
                break
    return observations, canonical


async def cmd_export(args: argparse.Namespace) -> int:
    window_start = date.fromisoformat(getattr(args, "from"))
    window_end = date.fromisoformat(args.to)
    out_dir = Path(args.out_dir)
    dataset_path = out_dir / f"anchors_{window_start}_{window_end}.csv"
    manifest_path = dataset_path.with_suffix(".manifest.json")
    if dataset_path.exists() or manifest_path.exists():
        print(f"REFUSED: output already exists ({dataset_path}) — never overwritten.")
        return 1

    observations, canonical = await _load_observations(window_start, window_end)
    print(f"{len(observations)} snapshot observations | {len(canonical)} canonical matches")
    rows = build_anchor_rows(observations, aliases=default_aliases(), canonical_matches=canonical)
    usable = sum(1 for r in rows if r.usable)
    sha = write_dataset(dataset_path, rows)
    write_manifest(
        manifest_path,
        {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "anchor_source": ANCHOR_SOURCE,
            "window_start": str(window_start),
            "window_end": str(window_end),
            "rows_total": len(rows),
            "rows_usable": usable,
            "rows_rejected": len(rows) - usable,
            "dataset_sha256": sha,
            "git_sha": _git_sha(),
            "config_sha256": _config_sha256(),
            "frozen_config_sha256": FROZEN_CONFIG_SHA256,
            "environment": _env_fingerprint(),
            "exported_at_utc": datetime.now(tz=UTC).isoformat(),
        },
    )
    print(f"dataset  -> {dataset_path} (sha256 {sha[:16]}...)")
    print(f"manifest -> {manifest_path}")
    print(f"rows: {len(rows)} total, {usable} usable, {len(rows) - usable} rejected (exported)")
    return 0


async def cmd_preflight(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset)
    manifest_path = dataset_path.with_suffix(".manifest.json")
    if not dataset_path.is_file():
        print(f"DO-NOT-RUN\n  anchor dataset missing: {dataset_path}")
        return 1
    rows = read_dataset(dataset_path)
    report = preflight_report(rows)
    dataset_sha = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    guard_violations = evaluate_contamination_guards(
        dataset_path=dataset_path,
        dataset_sha256=dataset_sha,
        input_sha256s=[manifest.get("dataset_sha256", dataset_sha)],
        window_start=(
            date.fromisoformat(manifest["window_start"]) if "window_start" in manifest else None
        ),
        window_end=(
            date.fromisoformat(manifest["window_end"]) if "window_end" in manifest else None
        ),
        config_sha256=_config_sha256(),
        output_dir=dataset_path.parent,
        preflight=report,  # self-evaluated: verdict must be PASS
        preflight_dataset_sha256=dataset_sha,
        anchor_source=manifest.get("anchor_source", ANCHOR_SOURCE),
    )
    report["contamination_guard_violations"] = guard_violations
    if guard_violations:
        report["verdict"] = "DO-NOT-RUN"
    marker_path = dataset_path.with_suffix(".preflight.json")
    marker = {
        "dataset_sha256": dataset_sha,
        "checked_at_utc": datetime.now(tz=UTC).isoformat(),
        **report,
    }
    marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"\nmarker -> {marker_path}")
    if report["verdict"] != "PASS":
        print("\nDO-NOT-RUN")
        return 1
    print("\nPREFLIGHT PASS")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="export the anchor dataset + manifest")
    exp.add_argument("--from", required=True, help="window start (YYYY-MM-DD, UTC)")
    exp.add_argument("--to", required=True, help="window end (YYYY-MM-DD, UTC)")
    exp.add_argument("--out-dir", default=str(_OUT_DIR))
    pre = sub.add_parser("preflight", help="coverage report + DO-NOT-RUN verdict")
    pre.add_argument("--dataset", required=True, help="exported anchors CSV path")
    args = parser.parse_args()
    if args.command == "export":
        return await cmd_export(args)
    return await cmd_preflight(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
