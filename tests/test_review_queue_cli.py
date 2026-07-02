"""Tests for tools/review_queue_cli.py — the match_review_queue triage CLI.

Pure half: the review-CSV row shaping + the VARCHAR(16) status mapping.
DB half (compose Postgres on :5433; skip absent, the house pattern): mark's
UPDATE touches ONLY review_status/reviewed_at and the stored values fit the
schema. No live network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.storage.models import MatchReviewQueue
from app.storage.repositories import MatchReviewIn, enqueue_match_reviews
from tools.alias_vetting import CSV_COLUMNS
from tools.review_queue_cli import _STATUS_MAP, _mark, queue_row_to_review_row

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"

_EVIDENCE = {
    "query_base_home": "alpha united",
    "query_base_away": "west torrens",
    "candidate_base_home": "alpha united",
    "candidate_base_away": "west torrens birkalla",
    "jw_home": 1.0,
    "jw_away": 0.9143,
    "token_sort_home": 100.0,
    "token_sort_away": 72.73,
    "kickoff_delta_seconds": 0.0,
}


def test_status_map_values_fit_varchar16() -> None:
    # match_review_queue.review_status is String(16): the CLI statuses MUST map
    # to stored values that fit, or mark would raise at the driver.
    for cli_status, stored in _STATUS_MAP.items():
        assert cli_status.startswith("reviewed_")
        assert len(stored) <= 16


def test_queue_row_to_review_row_shape_and_weaker_side() -> None:
    q = MatchReviewQueue(
        source="pinnacle",
        source_event_id="ev-123",
        candidate_canonical_event_id=None,
        confidence_score=Decimal("0.9143"),
        reason="jw_below_accept",
        evidence_json=dict(_EVIDENCE),
    )
    q.id = 42
    row = queue_row_to_review_row(q)
    assert set(row) == set(CSV_COLUMNS)
    assert row["candidate_id"] == "MRQ-42"
    # the WEAKER (min-JW) side is the surfaced alias pair
    assert row["raw_name_a"] == "west torrens"
    assert row["raw_name_b"] == "west torrens birkalla"
    assert row["suggested_alias_key"] == "west torrens birkalla"
    assert row["human_decision"] == ""
    assert "city_club_ambiguity" in row["risk_flags"]


@pytest.fixture
async def engine():  # type: ignore[no-untyped-def]
    eng = create_async_engine(DB_URL)
    try:
        async with eng.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:
        await eng.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    yield eng
    await eng.dispose()


async def test_mark_updates_only_review_status_and_reviewed_at(engine: AsyncEngine) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    source_event = "cli-test-ev-1"
    async with maker() as session:
        await enqueue_match_reviews(
            session,
            [
                MatchReviewIn(
                    source="pinnacle",
                    source_event_id=source_event,
                    candidate_canonical_event_id=None,
                    confidence=0.9143,
                    reason="jw_below_accept",
                    evidence=dict(_EVIDENCE),
                )
            ],
        )
        await session.commit()
        row = (
            await session.execute(
                select(MatchReviewQueue).where(MatchReviewQueue.source_event_id == source_event)
            )
        ).scalar_one()
        row_id, before_conf = row.id, row.confidence_score
    try:
        await _mark(engine, row_id, "reviewed_approved", notes=None)
        async with maker() as session:
            after = await session.get(MatchReviewQueue, row_id)
            assert after is not None
            assert after.review_status == "approved"  # fits String(16)
            assert after.reviewed_at is not None
            # nothing else touched
            assert after.confidence_score == before_conf
            assert after.reason == "jw_below_accept"
            assert after.evidence_json == _EVIDENCE
    finally:
        # cleanup: the test DB is session-scoped; drop the synthetic row so
        # re-runs / other tests never see it (delete via ORM object, not DDL).
        async with maker() as session:
            obj = await session.get(MatchReviewQueue, row_id)
            if obj is not None:
                await session.delete(obj)
                await session.commit()
