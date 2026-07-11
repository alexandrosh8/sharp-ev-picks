"""Mint-time picks.market_detail — canonical detail stamp + exact CLV matching.

Retires the line-blind (event, market, selection) key for NEW picks: the
canonical devig-group detail is stamped at mint and the CLV true-up matches
the close on it exactly. Legacy NULL rows keep the fail-closed guard.

The AH/spreads vocabulary MERGE was audited and rejected
(docs/research/2026-07-10-ah-spreads-vocabulary-audit.md): those details must
canonicalize to THEMSELVES (fail-closed), pinned here with the live pairs.
"""

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.pipeline import canonical_market_detail
from app.schemas.base import Market
from app.schemas.picks import PickOut, StakeBreakdownOut
from app.storage.models import Pick

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "c3d5e7f9a1b4_picks_market_detail.py"
PRIOR_HEAD = "a2f7d4c9e1b8"


# --- canonical detail ---------------------------------------------------------


def test_canonical_market_detail_folds_proven_lineless_and_totals() -> None:
    # The classes proven line-identical (live evidence 2026-07-10).
    assert canonical_market_detail(None) is None
    assert canonical_market_detail("h2h") is None
    assert canonical_market_detail("1x2") is None
    assert canonical_market_detail("btts") is None
    assert canonical_market_detail("over_under_2_5") == "totals_2_5"
    assert canonical_market_detail("totals_2_5") == "totals_2_5"  # idempotent


def test_canonical_market_detail_keeps_spreads_vocabularies_fail_closed() -> None:
    """Audit verdict 2026-07-10: AH/spreads must NOT merge — the OddsChecker
    spreads_* key space mixes 2-way AH and 3-way EH products on identical
    selection strings, and +L/-L books coexist per event (see
    docs/research/2026-07-10-ah-spreads-vocabulary-audit.md). The LIVE
    collision pairs stay distinct (picks 62270 'Spain -1', 74637
    'England -0.5')."""
    # live pair 1 — pick 62270
    assert canonical_market_detail("asian_handicap_-1_0") == "asian_handicap_-1_0"
    assert canonical_market_detail("spreads_minus_1") == "spreads_minus_1"
    # live pair 2 — pick 74637 (note the cross-provider SIGN flip)
    assert canonical_market_detail("asian_handicap_0_5") == "asian_handicap_0_5"
    assert canonical_market_detail("spreads_minus_0_5") == "spreads_minus_0_5"


def test_canonical_market_detail_never_collapses_distinct_lines() -> None:
    # Two genuinely different lines must NEVER share a canonical label.
    pairs = [
        ("over_under_2_5", "over_under_3_5"),
        ("totals_2_5", "totals_3_5"),
        ("asian_handicap_-1_0", "asian_handicap_+1_0"),
        ("spreads_minus_1", "spreads_plus_1"),
        ("spreads_minus_0_5", "spreads_minus_1_5"),
    ]
    for a, b in pairs:
        ca, cb = canonical_market_detail(a), canonical_market_detail(b)
        assert ca is not None and cb is not None
        assert ca != cb, f"{a!r} and {b!r} collapsed to {ca!r}"


# --- PickOut contract ---------------------------------------------------------


def _pick_out(market_detail: str | None) -> PickOut:
    return PickOut(
        pick_id="p-md",
        sport="soccer",
        league="test-league",
        event="Home FC vs Away FC",
        event_id="evt-md",
        market=Market.TOTALS,
        selection="Over 2.5",
        bookmaker="SoftBook",
        decimal_odds=2.0,
        model_probability=0.5,
        fair_probability=0.5,
        edge=0.05,
        ev=0.1,
        confidence=0.9,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(raw_kelly=0.1, fractional=0.025, capped=True, final=0.02),
        odds_age_seconds=30.0,
        reason_summary="market-detail test",
        market_detail=market_detail,
        created_at=datetime.now(tz=UTC),
    )


def test_pickout_market_detail_roundtrip_and_default() -> None:
    stamped = _pick_out("totals_2_5")
    assert stamped.market_detail == "totals_2_5"
    # frozen pydantic v2 model round-trips the field
    assert PickOut.model_validate(stamped.model_dump()).market_detail == "totals_2_5"
    # optional: absent -> None (legacy alert paths unaffected)
    payload = _pick_out(None).model_dump()
    payload.pop("market_detail")
    assert PickOut.model_validate(payload).market_detail is None


# --- ORM model ----------------------------------------------------------------


def test_pick_model_has_nullable_market_detail_column() -> None:
    col = Pick.__table__.columns["market_detail"]
    assert col.nullable is True  # legacy rows stay NULL -> legacy CLV behavior


# --- migration ----------------------------------------------------------------


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("picks_market_detail", MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_imports_cleanly_and_chains_off_prior_head() -> None:
    mod = _load_migration()
    assert mod.revision == "c3d5e7f9a1b4"
    assert mod.down_revision == PRIOR_HEAD
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_alembic_graph_has_single_head() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["a9d2c4e6f8b1"]
