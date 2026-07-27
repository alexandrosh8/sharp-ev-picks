"""DB-backed test for gate_reason_breakdown (the /lab/gate-reasons aggregate).

Seeds real candidate_evaluations rows via the sanctioned writer and asserts the
jsonb reason-slug aggregation, the clean-premium count, and the window filter.
Uses the compose Postgres (:5433, ``betting_ai_test``); skipped when absent,
inside ONE rolled-back transaction so nothing commits.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.storage.candidate_audit import CandidateEvaluationInput, record_candidate_evaluation
from app.storage.models import Event, League, Sport, Team
from app.storage.repositories import gate_reason_breakdown
from tests.database import TEST_DATABASE_URL

NOW = datetime.now(UTC)


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as probe:
            await probe.exec_driver_sql("SELECT 1")
    except Exception:  # noqa: BLE001
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


async def _seed_event(factory: async_sessionmaker) -> int:
    tag = uuid4().hex[:10]
    async with factory() as session:
        sport = Sport(key=f"soccer_{tag}", name="Soccer")
        session.add(sport)
        await session.flush()
        league = League(sport_id=sport.id, key=f"lg_{tag}", name="League", country="")
        session.add(league)
        await session.flush()
        home = Team(sport_id=sport.id, name=f"Home {tag}", normalized_name=f"home {tag}")
        away = Team(sport_id=sport.id, name=f"Away {tag}", normalized_name=f"away {tag}")
        session.add_all([home, away])
        await session.flush()
        event = Event(
            sport_id=sport.id,
            league_id=league.id,
            home_team_id=home.id,
            away_team_id=away.id,
            external_ref=f"https://example.test/{tag}",
            starts_at=NOW + timedelta(hours=3),
        )
        session.add(event)
        await session.flush()
        event_id = event.id
        await session.commit()
    return event_id


def _mk(
    event_id: int, selection: str, tier: str, reasons: tuple[str, ...], when: datetime
) -> CandidateEvaluationInput:
    return CandidateEvaluationInput(
        event_id=event_id,
        sport_key="soccer",
        market="h2h",
        selection=selection,
        tier=tier,
        evaluated_at=when,
        reasons=reasons,
    )


async def test_gate_reason_breakdown_counts_slugs_and_clean_premium(
    factory: async_sessionmaker,
) -> None:
    event_id = await _seed_event(factory)
    async with factory() as session:
        # in-window: two no_sharp_anchor (one with a sub-reason), one clean premium
        await record_candidate_evaluation(
            session, _mk(event_id, "a", "volume", ("no_sharp_anchor",), NOW)
        )
        await record_candidate_evaluation(
            session,
            _mk(
                event_id,
                "b",
                "volume",
                ("no_sharp_anchor", "no_sharp_anchor:exchange_liquidity_floor"),
                NOW,
            ),
        )
        await record_candidate_evaluation(session, _mk(event_id, "c", "premium", (), NOW))
        # out-of-window (48h ago): must NOT be counted at the default 24h window
        await record_candidate_evaluation(
            session,
            _mk(event_id, "d", "volume", ("draw_selection_demotion",), NOW - timedelta(hours=48)),
        )
        await session.commit()

        out = await gate_reason_breakdown(session, hours=24)

    assert out["window_hours"] == 24
    assert out["n_evaluations"] == 3  # the 48h-old row is excluded
    assert out["n_clean_premium"] == 1  # selection "c"
    assert out["reasons"]["no_sharp_anchor"] == 2
    assert out["reasons"]["no_sharp_anchor:exchange_liquidity_floor"] == 1
    assert "draw_selection_demotion" not in out["reasons"]  # out of window
    # sorted by descending count
    assert list(out["reasons"]) == sorted(out["reasons"], key=lambda k: (-out["reasons"][k], k))
