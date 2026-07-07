"""Candidate/rejection audit trail (external-audit #3): migration chain, model
round-trip, and the idempotent writer.

Pure MEASUREMENT infrastructure — never gates minting, never writes a pick. DB
tests use the compose Postgres (:5433, ``betting_ai_test``); skipped when absent,
inside ONE rolled-back transaction (the tests/test_bankroll_ledger.py ``factory``
pattern) so nothing commits to the shared test DB. No network, ever.
"""

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.storage.candidate_audit import (
    CANDIDATE_EVALUATION_REASONS,
    CandidateEvaluationInput,
    record_candidate_evaluation,
)
from app.storage.models import CandidateEvaluation, Event, League, Sport, Team

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "d1e2f3a4b5c6_candidate_evaluations.py"
DB_URL = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"
NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


# --- migration chain (pure, no DB) -----------------------------------------


def test_migration_chains_off_prior_head_single_head() -> None:
    spec = importlib.util.spec_from_file_location("_mig_d1e2f3a4b5c6", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "d1e2f3a4b5c6"
    assert mod.down_revision == "b8e5d2f7a4c1"  # bankroll_ledger (A8)
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["d1e2f3a4b5c6"]


def test_documented_reason_vocabulary_covers_pipeline_gates() -> None:
    # The known demotion/rejection slugs the pipeline distinguishes. Guards
    # against a silent drift when the pipeline wiring lands later.
    assert {
        "visibility_only",
        "odds_ceiling",
        "non_major_league",
        "no_sharp_anchor",
        "experimental_sport",
        "ml_filter",
        "steam",
        "structural_sanity",
        "stale",
        "off_band",
        "thin_books",
        "ah_implausible",
        "dc_implausible",
    } == set(CANDIDATE_EVALUATION_REASONS)


# --- DB fixtures -------------------------------------------------------------


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine(DB_URL)
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
    """One sport/league/teams/event; returns the event id."""
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


async def _rows(factory: async_sessionmaker, event_id: int) -> list[CandidateEvaluation]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(CandidateEvaluation)
                    .where(CandidateEvaluation.event_id == event_id)
                    .order_by(CandidateEvaluation.id)
                )
            )
            .scalars()
            .all()
        )


# --- writer: premium (kept) + demoted (reasons) round-trip -------------------


async def test_writer_records_premium_and_demoted_rows(factory: async_sessionmaker) -> None:
    event_id = await _seed_event(factory)
    premium = CandidateEvaluationInput(
        event_id=event_id,
        sport_key="soccer",
        market="h2h",
        selection="home",
        tier="premium",
        evaluated_at=NOW,
        anchor_book="Pinnacle",
        anchor_type="pinnacle",
        anchor_age_seconds=Decimal("42.500000"),
        anchor_liquidity=Decimal("1500.00"),
        best_book="bet365",
        best_odds=Decimal("2.1000"),
        edge=Decimal("0.045000"),
        fair_probability=Decimal("0.500000"),
    )
    demoted = CandidateEvaluationInput(
        event_id=event_id,
        sport_key="soccer",
        market="totals",
        market_detail="over_2.5",
        selection="over",
        tier="volume",
        evaluated_at=NOW,
        reasons=("no_sharp_anchor", "odds_ceiling"),
        anchor_book="consensus",
        anchor_type="consensus",
        best_book="williamhill",
        best_odds=Decimal("6.0000"),
        edge=Decimal("0.030000"),
        fair_probability=Decimal("0.180000"),
    )
    async with factory() as session:
        await record_candidate_evaluation(session, premium)
        await record_candidate_evaluation(session, demoted)
        await session.commit()

    rows = await _rows(factory, event_id)
    assert [r.tier for r in rows] == ["premium", "volume"]
    kept, dem = rows
    # premium keep: no reasons, full provenance survives the round-trip
    assert kept.reasons is None
    assert kept.market_detail == ""  # NOT NULL default, not a NULL
    assert kept.anchor_book == "Pinnacle"
    assert kept.anchor_age_seconds == Decimal("42.500000")
    assert kept.anchor_liquidity == Decimal("1500.00")
    assert kept.best_odds == Decimal("2.1000")
    assert kept.edge == Decimal("0.045000")
    assert kept.fair_probability == Decimal("0.500000")
    assert kept.evaluated_at == NOW
    # demoted: reasons captured in the {"reasons": [...]} shape
    assert dem.reasons == {"reasons": ["no_sharp_anchor", "odds_ceiling"]}
    assert dem.market_detail == "over_2.5"
    assert dem.anchor_type == "consensus"


async def test_writer_is_idempotent_per_candidate_per_cycle(factory: async_sessionmaker) -> None:
    event_id = await _seed_event(factory)
    candidate = CandidateEvaluationInput(
        event_id=event_id,
        sport_key="soccer",
        market="h2h",
        selection="home",
        tier="premium",
        evaluated_at=NOW,
    )
    async with factory() as session:
        await record_candidate_evaluation(session, candidate)
        await record_candidate_evaluation(session, candidate)  # same cycle -> no-op
        await session.commit()
    async with factory() as session:
        count = await session.scalar(
            select(func.count(CandidateEvaluation.id)).where(
                CandidateEvaluation.event_id == event_id
            )
        )
    assert count == 1

    # A later cycle (new evaluated_at) for the same candidate DOES append.
    async with factory() as session:
        await record_candidate_evaluation(
            session,
            CandidateEvaluationInput(
                event_id=event_id,
                sport_key="soccer",
                market="h2h",
                selection="home",
                tier="volume",
                evaluated_at=NOW + timedelta(minutes=5),
            ),
        )
        await session.commit()
    assert len(await _rows(factory, event_id)) == 2
