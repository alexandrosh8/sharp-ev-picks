"""DB integration for the anchor-match observability batch (compose Postgres;
skip absent).

Covers: persist/serialize of picks.anchor_match_confidence/method; the
event_source_links upsert (idempotent re-confirm); match_review_queue
idempotency (re-runs never duplicate); source_link_metrics null-safety; and the
resolver end-to-end taps — an ACCEPTED pinnacle match writes a link + fills
provenance_out, a REVIEW-BAND near-miss stays rejected but lands in the queue.
No live network.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ingestion.base import EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Event, EventSourceLink, MatchReviewQueue
from app.storage.repositories import (
    MatchReviewIn,
    SourceLinkByRef,
    enqueue_match_reviews,
    latest_picks_with_events,
    persist_odds_snapshots,
    persist_pick,
    resolve_pinnacle_close_snaps,
    source_link_metrics,
    upsert_event_source_links,
)

DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"
KO = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
CAPTURED = KO - timedelta(hours=2)


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("compose Postgres not reachable on :5433")
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


def _pick(event_id: str, home: str, **overrides) -> PickOut:  # type: ignore[no-untyped-def]
    base = PickOut(
        pick_id="p-links",
        sport="soccer",
        league="links_league",
        event=f"{event_id} fixture",
        event_id=event_id,
        market=Market.H2H,
        selection=home,
        bookmaker="testbook",
        decimal_odds=2.10,
        model_probability=0.55,
        fair_probability=0.50,
        edge=0.05,
        ev=0.155,
        confidence=0.70,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
        odds_age_seconds=30.0,
        liquidity=None,
        reason_summary="source-links test",
        tier="premium",
        created_at=KO - timedelta(hours=6),
    )
    return base.model_copy(update=overrides)


def _pin_snap(selection: str, odds: float, event: str) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id=event,
        bookmaker="Pinnacle",
        market=Market.H2H,
        selection=selection,
        decimal_odds=odds,
        captured_at=CAPTURED,
        ingested_at=CAPTURED,
    )


async def _seed_pinnacle_event(factory, ref: str, home: str, away: str) -> None:  # type: ignore[no-untyped-def]
    snaps = [_pin_snap(home, 2.10, ref), _pin_snap("Draw", 3.40, ref), _pin_snap(away, 3.60, ref)]
    teams = {ref: EventTeams(home=home, away=away, league="pin", starts_at=KO)}
    await persist_odds_snapshots(factory, snaps, teams, "pinnacle_soccer", "pinnacle_soccer")


async def _persist(factory, pick: PickOut, home: str, away: str) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session:
        await persist_pick(
            session,
            pick,
            EventTeams(home=home, away=away, league="links_league", starts_at=KO),
            "value-sharp-vs-soft",
            "v3",
        )
        await session.commit()


async def test_pick_carries_anchor_match_provenance_to_serving(factory) -> None:  # type: ignore[no-untyped-def]
    pick = _pick(
        "evt-links-prov",
        "Home Prov",
        anchor_type="pinnacle",
        anchor_book="Pinnacle",
        anchor_match_confidence=0.9765,
        anchor_match_method="jw_two_tier",
    )
    await _persist(factory, pick, "Home Prov", "Away Prov")
    async with factory() as session:
        rows = await latest_picks_with_events(session, limit=10)
    row = next(r for r in rows if "Home Prov" in r["event"])
    # string-serialized NUMERIC, like the other Decimal fields
    assert Decimal(row["anchor_match_confidence"]) == Decimal("0.9765")
    assert row["anchor_match_method"] == "jw_two_tier"


async def test_pick_without_provenance_serializes_null_safe(factory) -> None:  # type: ignore[no-untyped-def]
    pick = _pick("evt-links-null", "Home Null", anchor_type="consensus")
    await _persist(factory, pick, "Home Null", "Away Null")
    async with factory() as session:
        rows = await latest_picks_with_events(session, limit=10)
    row = next(r for r in rows if "Home Null" in r["event"])
    assert row["anchor_match_confidence"] is None
    assert row["anchor_match_method"] is None


async def test_upsert_event_source_links_is_idempotent_and_refreshes(factory) -> None:  # type: ignore[no-untyped-def]
    pick = _pick("evt-links-upsert", "Home Upsert")
    await _persist(factory, pick, "Home Upsert", "Away Upsert")
    first = datetime(2026, 11, 30, 10, 0, tzinfo=UTC)
    second = first + timedelta(hours=4)

    def link(matched_at: datetime, confidence: float) -> SourceLinkByRef:
        return SourceLinkByRef(
            source="pinnacle_arcadia",
            source_event_id="arc-123",
            canonical_external_ref="evt-links-upsert",
            confidence=confidence,
            method="exact_canonical",
            matched_at=matched_at,
        )

    async with factory() as session:
        assert await upsert_event_source_links(session, [link(first, 0.97)]) == 1
        assert await upsert_event_source_links(session, [link(second, 0.99)]) == 1
        await session.commit()
    async with factory() as session:
        rows = (await session.execute(select(EventSourceLink))).scalars().all()
        assert len(rows) == 1  # ON CONFLICT upsert-in-place, never a second row
        assert rows[0].matched_at == second  # refreshed
        assert rows[0].confidence_score == Decimal("0.990000")
        assert rows[0].active is True


async def test_upsert_skips_unknown_canonical_ref(factory) -> None:  # type: ignore[no-untyped-def]
    # No events row for the ref -> nothing to link against; written count is 0.
    orphan = SourceLinkByRef(
        source="pinnacle_arcadia",
        source_event_id="arc-orphan",
        canonical_external_ref="evt-never-seen",
        confidence=1.0,
        method="exact_canonical",
        matched_at=datetime.now(tz=UTC),
    )
    async with factory() as session:
        assert await upsert_event_source_links(session, [orphan]) == 0


async def test_enqueue_match_reviews_twice_yields_one_row(factory) -> None:  # type: ignore[no-untyped-def]
    pick = _pick("evt-links-queue", "Home Queue")
    await _persist(factory, pick, "Home Queue", "Away Queue")
    async with factory() as session:
        canonical_id = await session.scalar(
            select(Event.id).where(Event.external_ref == "evt-links-queue")
        )
        assert canonical_id is not None
        row = MatchReviewIn(
            source="pinnacle_arcadia",
            source_event_id="arc-queue-1",
            candidate_canonical_event_id=canonical_id,
            confidence=0.9096,
            reason="jw_below_accept",
            evidence={"jw_home": 0.9096},
        )
        await enqueue_match_reviews(session, [row])
        await enqueue_match_reviews(session, [row])  # re-run: ON CONFLICT DO NOTHING
        await session.commit()
    async with factory() as session:
        n = await session.scalar(select(func.count()).select_from(MatchReviewQueue))
        assert n == 1
        queued = (await session.execute(select(MatchReviewQueue))).scalars().one()
        assert queued.review_status == "pending"
        assert queued.reviewed_at is None


async def test_source_link_metrics_null_safe_when_empty(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session:
        metrics = await source_link_metrics(session)
    assert metrics == {
        "auto_linked": 0,
        "review_queued": 0,
        "rejected_observed": 0,
        "weak_links": 0,
        "by_source": {},
    }


async def test_source_link_metrics_counts_and_averages(factory) -> None:  # type: ignore[no-untyped-def]
    pick = _pick("evt-links-metrics", "Home Metrics")
    await _persist(factory, pick, "Home Metrics", "Away Metrics")
    async with factory() as session:
        canonical_id = await session.scalar(
            select(Event.id).where(Event.external_ref == "evt-links-metrics")
        )
        assert canonical_id is not None
        links = [
            SourceLinkByRef(
                source="pinnacle_arcadia",
                source_event_id=f"arc-m-{i}",
                canonical_external_ref="evt-links-metrics",
                confidence=conf,
                method="jw_two_tier",
                matched_at=datetime.now(tz=UTC),
            )
            for i, conf in enumerate((1.0, 0.93))  # one strong, one weak (<0.95)
        ]
        await upsert_event_source_links(session, links)
        await enqueue_match_reviews(
            session,
            [
                MatchReviewIn(
                    source="pinnacle_arcadia",
                    source_event_id="arc-m-review",
                    candidate_canonical_event_id=canonical_id,
                    confidence=0.90,
                    reason="jw_below_accept",
                )
            ],
        )
        await session.commit()
    async with factory() as session:
        metrics = await source_link_metrics(session)
    assert metrics["auto_linked"] == 2
    assert metrics["weak_links"] == 1
    assert metrics["review_queued"] == 1
    assert metrics["rejected_observed"] == 1
    per_source = metrics["by_source"]["pinnacle_arcadia"]
    assert per_source["links"] == 2
    assert per_source["avg_confidence"] == pytest.approx(0.965)


async def test_resolver_accept_records_link_and_provenance(factory) -> None:  # type: ignore[no-untyped-def]
    # End-to-end over the LIVE resolver: an accepted (alias-exact) pinnacle
    # match returns the close AND (a) fills provenance_out for the pick columns,
    # (b) upserts the event_source_links row keyed by the ARCADIA external_ref.
    await _seed_pinnacle_event(factory, "pin-links-mu", "Manchester United", "Chelsea")
    pick = _pick("evt-links-resolve", "Man Utd")
    await _persist(factory, pick, "Man Utd", "Chelsea")
    provenance: dict[str, tuple[float, str]] = {}
    async with factory() as session:
        out = await resolve_pinnacle_close_snaps(
            session,
            pinnacle_sport_key="pinnacle_soccer",
            pick_external_ref="evt-links-resolve",
            home="Man Utd",
            away="Chelsea",
            kickoff=KO,
            provenance_out=provenance,
        )
        assert out  # the close resolves exactly as before (behavior unchanged)
        assert provenance["evt-links-resolve"] == (1.0, "exact_canonical")
        await session.commit()  # the live loader commits its cycle session too
    async with factory() as session:
        link = (await session.execute(select(EventSourceLink))).scalars().one()
        assert link.source == "pinnacle_arcadia"
        assert link.source_event_id == "pin-links-mu"
        assert link.match_method == "exact_canonical"
        assert link.confidence_score == Decimal("1.000000")
        canonical_id = await session.scalar(
            select(Event.id).where(Event.external_ref == "evt-links-resolve")
        )
        assert link.canonical_event_id == canonical_id


async def test_resolver_review_band_reject_enqueues_and_stays_rejected(factory) -> None:  # type: ignore[no-untyped-def]
    # 'atletico mineiro' vs 'atletico madeira' — the documented [0.84, 0.92) JW
    # review band. The resolver must return [] exactly as before (the queue is a
    # tap, not a gate) AND enqueue one idempotent review row.
    await _seed_pinnacle_event(factory, "pin-links-band", "Atletico Madeira", "Chelsea")
    pick = _pick("evt-links-band", "Atletico Mineiro")
    await _persist(factory, pick, "Atletico Mineiro", "Chelsea")

    async def resolve() -> Sequence[OddsSnapshotIn]:
        async with factory() as session:
            out = await resolve_pinnacle_close_snaps(
                session,
                pinnacle_sport_key="pinnacle_soccer",
                pick_external_ref="evt-links-band",
                home="Atletico Mineiro",
                away="Chelsea",
                kickoff=KO,
            )
            await session.commit()
            return out

    assert await resolve() == []  # STILL rejected — zero behavior change
    assert await resolve() == []  # re-run: queue must stay one row (idempotent)
    async with factory() as session:
        queued = (await session.execute(select(MatchReviewQueue))).scalars().all()
        assert len(queued) == 1
        row = queued[0]
        assert row.source == "pinnacle_arcadia"
        assert row.source_event_id == "pin-links-band"
        assert row.reason == "jw_below_accept"
        assert row.evidence_json is not None
        assert row.evidence_json["candidate_base_home"] == "atletico madeira"
        assert Decimal("0.84") <= row.confidence_score < Decimal("0.92")
        canonical_id = await session.scalar(
            select(Event.id).where(Event.external_ref == "evt-links-band")
        )
        assert row.candidate_canonical_event_id == canonical_id
