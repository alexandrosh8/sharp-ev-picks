"""Minimal operator CLI over match_review_queue (read + mark ONLY).

The queue holds borderline matcher REJECTS (observability tap — never a gate);
this CLI lets a human triage them. Commands:

  list   --status pending [--limit N]   id, source, names, confidence, reason, created_at
  show   <id>                           the full row + pretty evidence_json
  export --csv <path>                   review-CSV shape (same as
                                        tools/export_alias_candidates.py, so ONE
                                        review workflow serves both sources)
  mark   <id> --status reviewed_approved|reviewed_rejected [--notes "..."]

The ONLY database write this tool can perform is mark's UPDATE of
``review_status`` + ``reviewed_at`` (parameterized via the app's SQLAlchemy
async session). ``review_status`` is VARCHAR(16), so the CLI statuses map to the
stored values ``approved`` / ``rejected``. ``--notes`` is NEVER written to the
DB — it is echoed and appended to a local JSONL audit file next to the CSVs
(docs/review/queue_review_notes.jsonl).

Every command takes ``--dsn`` so the operator can run it inside the prod
container (postgres reachable at postgres:5432 there, not localhost:5433).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.storage.models import MatchReviewQueue
from tools.alias_vetting import compute_risk_flags, write_review_csv

_STATUS_MAP = {"reviewed_approved": "approved", "reviewed_rejected": "rejected"}
_NOTES_LOG = Path("docs/review/queue_review_notes.jsonl")


def _make_engine(dsn: str | None) -> AsyncEngine:
    if dsn is not None:
        return create_async_engine(dsn, pool_pre_ping=True)
    from app.config import get_settings
    from app.database import create_engine

    return create_engine(get_settings())


def queue_row_to_review_row(q: MatchReviewQueue) -> dict[str, str]:
    """One queue row -> one review-CSV row (the alias-vetting CSV shape).

    One pair per row: surface the WEAKER side (min JW) — that side is the
    alias-actionable near-miss; the other side already scores higher."""
    ev: dict[str, Any] = q.evidence_json or {}
    jw_home = float(ev.get("jw_home", 0.0) or 0.0)
    jw_away = float(ev.get("jw_away", 0.0) or 0.0)
    side = "home" if jw_home <= jw_away else "away"
    name_a = str(ev.get(f"query_base_{side}", ""))
    name_b = str(ev.get(f"candidate_base_{side}", ""))
    confidence = float(q.confidence_score)
    return {
        "candidate_id": f"MRQ-{q.id}",
        "source_a": "oddsportal",
        "raw_name_a": name_a,
        "source_b": q.source,
        "raw_name_b": name_b,
        "sport": "",
        "league": "",
        "country": "",
        "confidence": f"{confidence:.4f}",
        "reason": q.reason,
        "sample_event_count": "1",
        "example_events": (
            f"{ev.get('query_base_home', '?')} vs {ev.get('query_base_away', '?')}"
            f" @ source_event={q.source_event_id}"
        ),
        "suggested_alias_key": name_b,
        "risk_flags": "|".join(compute_risk_flags(name_a, name_b, confidence)),
        "human_decision": "",
        "reviewer_notes": "",
    }


def _names(evidence: dict[str, Any] | None) -> str:
    ev = evidence or {}
    return (
        f"{ev.get('query_base_home', '?')} v {ev.get('query_base_away', '?')}"
        f"  <->  {ev.get('candidate_base_home', '?')} v {ev.get('candidate_base_away', '?')}"
    )


async def _list(engine: AsyncEngine, status: str, limit: int) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        rows = (
            (
                await session.execute(
                    select(MatchReviewQueue)
                    .where(MatchReviewQueue.review_status == status)
                    .order_by(MatchReviewQueue.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        print(f"(no {status!r} rows)")
        return
    for q in rows:
        print(
            f"#{q.id:<6} {q.source:<10} conf={float(q.confidence_score):.4f} "
            f"{q.reason:<26} {q.created_at.isoformat()}  {_names(q.evidence_json)}"
        )


async def _show(engine: AsyncEngine, row_id: int) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        q = await session.get(MatchReviewQueue, row_id)
    if q is None:
        print(f"no match_review_queue row #{row_id}")
        return
    print(f"id                : {q.id}")
    print(f"source            : {q.source}")
    print(f"source_event_id   : {q.source_event_id}")
    print(f"source_market_id  : {q.source_market_id}")
    print(f"candidate_event_id: {q.candidate_canonical_event_id}")
    print(f"confidence        : {float(q.confidence_score):.6f}")
    print(f"reason            : {q.reason}")
    print(f"review_status     : {q.review_status}")
    print(f"created_at        : {q.created_at.isoformat()}")
    print(f"reviewed_at       : {q.reviewed_at.isoformat() if q.reviewed_at else None}")
    print("evidence_json     :")
    print(json.dumps(q.evidence_json or {}, indent=2, ensure_ascii=False, default=str))


async def _export(engine: AsyncEngine, csv_path: Path, status: str) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        rows = (
            (
                await session.execute(
                    select(MatchReviewQueue)
                    .where(MatchReviewQueue.review_status == status)
                    .order_by(MatchReviewQueue.id)
                )
            )
            .scalars()
            .all()
        )
    write_review_csv([queue_row_to_review_row(q) for q in rows], csv_path)
    print(f"exported {len(rows)} {status!r} queue rows -> {csv_path}")


async def _mark(engine: AsyncEngine, row_id: int, cli_status: str, notes: str | None) -> None:
    stored = _STATUS_MAP[cli_status]
    reviewed_at = datetime.now(tz=UTC)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(MatchReviewQueue)
                .where(MatchReviewQueue.id == row_id)
                .values(review_status=stored, reviewed_at=reviewed_at)
            ),
        )
        await session.commit()
    if result.rowcount == 0:
        print(f"no match_review_queue row #{row_id} — nothing marked")
        return
    print(f"marked #{row_id} review_status={stored!r} reviewed_at={reviewed_at.isoformat()}")
    if notes:
        _NOTES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _NOTES_LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "id": row_id,
                        "status": stored,
                        "notes": notes,
                        "reviewed_at": reviewed_at.isoformat(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        print(f"notes appended locally -> {_NOTES_LOG} (notes are never written to the DB)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="match_review_queue triage (read + mark only)")
    parser.add_argument("--dsn", default=None, help="async SQLAlchemy DSN override")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list queue rows")
    p_list.add_argument("--status", default="pending")
    p_list.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show", help="show one row with full evidence")
    p_show.add_argument("id", type=int)

    p_export = sub.add_parser("export", help="export queue rows in the review-CSV shape")
    p_export.add_argument("--csv", type=Path, required=True)
    p_export.add_argument("--status", default="pending")

    p_mark = sub.add_parser("mark", help="mark one row reviewed (the only DB write)")
    p_mark.add_argument("id", type=int)
    p_mark.add_argument("--status", required=True, choices=sorted(_STATUS_MAP))
    p_mark.add_argument("--notes", default=None)

    args = parser.parse_args(argv)
    engine = _make_engine(args.dsn)

    async def _run() -> None:
        try:
            if args.command == "list":
                await _list(engine, args.status, args.limit)
            elif args.command == "show":
                await _show(engine, args.id)
            elif args.command == "export":
                await _export(engine, args.csv, args.status)
            elif args.command == "mark":
                await _mark(engine, args.id, args.status, args.notes)
        finally:
            await engine.dispose()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
