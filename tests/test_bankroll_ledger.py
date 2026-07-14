"""A8 bankroll ledger — migration chain, default-off inertness, idempotent
settled-P&L append, balance/drawdown aggregate math, endpoint auth + shape.

The ledger is INFORMATIONAL ONLY (picks-only platform): a hypothetical
starting balance + running settled P&L, never money movement, never a live
staking input. Ships OFF: BANKROLL_STARTING_BALANCE unset writes NOTHING.

DB tests use the compose Postgres (:5433); skipped when absent, inside ONE
rolled-back transaction (the tests/test_betfair_targets.py ``factory``
pattern) so nothing commits to the shared test DB. No network, ever.
"""

import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.maintenance.bankroll_ledger import sync_bankroll_ledger
from app.storage.models import (
    BankrollLedgerEntry,
    Event,
    League,
    ModelVersion,
    Pick,
    ResultTracking,
    Sport,
    Team,
)
from app.storage.repositories import bankroll_ledger_report
from tests.database import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "b8e5d2f7a4c1_bankroll_ledger.py"
DB_URL = TEST_DATABASE_URL
NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


# --- migration chain (pure, no DB) -----------------------------------------


def test_migration_chains_off_prior_head_single_head() -> None:
    spec = importlib.util.spec_from_file_location("_mig_b8e5d2f7a4c1", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "b8e5d2f7a4c1"
    assert mod.down_revision == "f8a3c5d7e9b1"  # picks_steam_shadow_verdict (A5)
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["e7f1a9c3b5d2"]


def test_setting_ships_off_by_default() -> None:
    from app.config import Settings

    assert Settings.model_fields["bankroll_starting_balance"].default is None


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


async def _seed_settled_picks(
    factory: async_sessionmaker,
    pnls: list[Decimal | None],
    first_settled_at: datetime = NOW,
) -> list[int]:
    """One event + model version; one Pick + ResultTracking per pnl (None pnl =
    settled row without a P&L, which the ledger must skip). Returns pick ids;
    settled_at advances one minute per pick so ledger order is deterministic."""
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
            starts_at=first_settled_at - timedelta(hours=3),
        )
        mv = ModelVersion(name="value-sharp-vs-soft", version="v3", sport_id=sport.id)
        session.add_all([event, mv])
        await session.flush()
        pick_ids: list[int] = []
        for i, pnl in enumerate(pnls):
            pick = Pick(
                event_id=event.id,
                model_version_id=mv.id,
                market="h2h",
                selection=f"sel_{i}",
                bookmaker="bet365",
                decimal_odds=Decimal("2.00"),
                model_probability=Decimal("0.55"),
                fair_probability=Decimal("0.55"),
                edge=Decimal("0.05"),
                ev=Decimal("0.10"),
                confidence=Decimal("0.7"),
                recommended_stake_fraction=Decimal("0.01"),
                recommended_stake_amount=Decimal("20.00"),
                status="settled",
            )
            session.add(pick)
            await session.flush()
            session.add(
                ResultTracking(
                    pick_id=pick.id,
                    outcome="won" if (pnl or 0) >= 0 else "lost",
                    pnl=pnl,
                    roi=None,
                    settled_at=first_settled_at + timedelta(minutes=i),
                )
            )
            pick_ids.append(pick.id)
        await session.commit()
    return pick_ids


async def _ledger_rows(factory: async_sessionmaker) -> list[BankrollLedgerEntry]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(BankrollLedgerEntry).order_by(
                        BankrollLedgerEntry.occurred_at, BankrollLedgerEntry.id
                    )
                )
            )
            .scalars()
            .all()
        )


# --- default OFF = zero behavior change --------------------------------------


async def test_inactive_default_writes_nothing(factory: async_sessionmaker) -> None:
    await _seed_settled_picks(factory, [Decimal("50.00")])
    assert await sync_bankroll_ledger(factory, starting_balance=None) == 0
    async with factory() as session:
        count = await session.scalar(select(func.count(BankrollLedgerEntry.id)))
    assert count == 0
    async with factory() as session:
        report = await bankroll_ledger_report(session)
    assert report == {
        "active": False,
        "starting_balance": None,
        "current_balance": None,
        "max_drawdown": None,
        "n_entries": 0,
        "series": [],
    }


# --- active sync: seed + append + idempotency ---------------------------------


async def test_sync_seeds_starting_balance_then_appends_settled_pnl(
    factory: async_sessionmaker,
) -> None:
    pick_ids = await _seed_settled_picks(factory, [Decimal("50.00"), Decimal("-25.00"), None])
    appended = await sync_bankroll_ledger(factory, starting_balance=1000.0, now=NOW)
    assert appended == 3  # starting balance + 2 settled picks; None-pnl row skipped
    rows = await _ledger_rows(factory)
    assert [r.entry_type for r in rows] == ["starting_balance", "settled_pnl", "settled_pnl"]
    assert [r.balance_after for r in rows] == [
        Decimal("1000.00"),
        Decimal("1050.00"),
        Decimal("1025.00"),
    ]
    assert [r.pick_id for r in rows] == [None, pick_ids[0], pick_ids[1]]
    # settled entries carry the pick's settlement time, not the sync time
    assert rows[1].occurred_at == NOW
    assert rows[2].occurred_at == NOW + timedelta(minutes=1)


async def test_sync_is_idempotent_and_absorbs_later_settles(
    factory: async_sessionmaker,
) -> None:
    await _seed_settled_picks(factory, [Decimal("50.00"), Decimal("-25.00")])
    assert await sync_bankroll_ledger(factory, starting_balance=1000.0, now=NOW) == 3
    # Re-running appends nothing — never double-counts a settled pick.
    assert await sync_bankroll_ledger(factory, starting_balance=1000.0, now=NOW) == 0
    assert len(await _ledger_rows(factory)) == 3
    # A pick settled later (e.g. the manual dashboard path) is absorbed next sync.
    await _seed_settled_picks(
        factory, [Decimal("-10.00")], first_settled_at=NOW + timedelta(hours=1)
    )
    assert await sync_bankroll_ledger(factory, starting_balance=1000.0, now=NOW) == 1
    rows = await _ledger_rows(factory)
    assert rows[-1].balance_after == Decimal("1015.00")


# --- read aggregate: balance series + max drawdown ----------------------------


async def test_report_balance_series_and_max_drawdown(factory: async_sessionmaker) -> None:
    await _seed_settled_picks(factory, [Decimal("50.00"), Decimal("-25.00")])
    await sync_bankroll_ledger(factory, starting_balance=1000.0, now=NOW)
    async with factory() as session:
        report = await bankroll_ledger_report(session)
    assert report["active"] is True
    assert report["starting_balance"] == 1000.0
    assert report["current_balance"] == 1025.0
    # peak 1050 -> trough 1025: drawdown 25/1050
    assert report["max_drawdown"] == pytest.approx(25 / 1050)
    assert report["n_entries"] == 3
    assert [p["balance_after"] for p in report["series"]] == [1000.0, 1050.0, 1025.0]
    assert report["series"][1]["entry_type"] == "settled_pnl"
    assert report["series"][1]["amount"] == 50.0


async def test_report_feature_detects_missing_table(factory: async_sessionmaker) -> None:
    # Pre-migration DB: to_regclass is NULL -> empty shape, never UndefinedTable.
    # DDL is transactional in Postgres; the fixture rolls the DROP back.
    async with factory() as session:
        await session.execute(text("DROP TABLE bankroll_ledger"))
        report = await bankroll_ledger_report(session)
    assert report["active"] is False
    assert report["series"] == []


# --- endpoint: shape + auth (pure, no DB) --------------------------------------


class _NoTableSession:
    """Session stub whose to_regclass probe reports the table absent."""

    async def scalar(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None


def test_bankroll_endpoint_serves_empty_shape_pre_migration() -> None:
    from app.api.auth import require_dashboard_auth
    from app.api.deps import get_session
    from app.api.routes import router

    async def _stub_session() -> AsyncIterator[AsyncSession]:
        yield _NoTableSession()  # type: ignore[misc]

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = _stub_session
    app.dependency_overrides[require_dashboard_auth] = lambda: None
    body = TestClient(app).get("/bankroll").json()
    assert body["active"] is False
    assert body["current_balance"] is None
    assert body["series"] == []


def test_bankroll_endpoint_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    from app.api.auth import hash_password, install_auth
    from app.api.deps import get_session
    from app.api.routes import router
    from app.config import Settings

    settings = Settings.model_construct(
        dashboard_auth_enabled=True,
        dashboard_auth_username="admin",
        dashboard_auth_password_hash=SecretStr(hash_password("pw-test-only")),
        dashboard_session_secret=SecretStr("0123456789abcdef0123456789abcdef"),
        dashboard_session_ttl_seconds=12 * 60 * 60,
        app_env="local",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    async def _no_session() -> AsyncIterator[None]:
        yield None

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = _no_session
    install_auth(app)
    res = TestClient(app, follow_redirects=False).get("/bankroll")
    assert res.status_code == 401
