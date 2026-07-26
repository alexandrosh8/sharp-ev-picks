"""Unsettleable-pick warning dedup (audit S, 2026-07-26).

The 30s settlement cycle used to re-warn EVERY unsettleable pick EVERY cycle
(167k warnings/6h). The engine now warns once per (pick, reason) — again only
on reason change — and emits one per-cycle summary line instead.

No DB needed: the unparseable branch of ``_settle_one`` raises before any
session use, so a transient ``Pick`` and a dummy session exercise it fully.
"""

import logging
from collections import Counter
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.settlement.engine import (
    _settle_one,
    _unsettleable_summary,
    reset_unsettleable_warning_state,
)
from app.storage.models import Pick

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
HOME = "Alpha FC"
AWAY = "Beta United"

# The unsettleable branch never touches the session; a dummy proves it.
SESSION = cast(AsyncSession, object())


def _pick(pick_id: int, market: str = "spreads", selection: str = "Gamma Town -0.75") -> Pick:
    return Pick(id=pick_id, market=market, selection=selection)


async def test_unsettleable_warning_emitted_once_per_pick(caplog) -> None:  # type: ignore[no-untyped-def]
    reset_unsettleable_warning_state()
    pick = _pick(4242)
    with caplog.at_level(logging.WARNING, logger="app.settlement.engine"):
        assert await _settle_one(SESSION, pick, HOME, AWAY, 2, 1, NOW) is False
        assert await _settle_one(SESSION, pick, HOME, AWAY, 2, 1, NOW) is False
        assert await _settle_one(SESSION, pick, HOME, AWAY, 2, 1, NOW) is False
    warned = [r for r in caplog.records if "not settleable" in r.getMessage()]
    assert len(warned) == 1


async def test_unsettleable_warning_re_emitted_on_reason_change(caplog) -> None:  # type: ignore[no-untyped-def]
    reset_unsettleable_warning_state()
    pick = _pick(4243)
    with caplog.at_level(logging.WARNING, logger="app.settlement.engine"):
        assert await _settle_one(SESSION, pick, HOME, AWAY, 2, 1, NOW) is False
        # Same pick, different failure reason (selection text changed on the
        # ORM instance) -> state change -> warn again, once.
        pick.selection = "Delta City -0.75"
        assert await _settle_one(SESSION, pick, HOME, AWAY, 2, 1, NOW) is False
        assert await _settle_one(SESSION, pick, HOME, AWAY, 2, 1, NOW) is False
    warned = [r for r in caplog.records if "not settleable" in r.getMessage()]
    assert len(warned) == 2


async def test_unsettleable_warnings_independent_across_picks(caplog) -> None:  # type: ignore[no-untyped-def]
    reset_unsettleable_warning_state()
    with caplog.at_level(logging.WARNING, logger="app.settlement.engine"):
        assert await _settle_one(SESSION, _pick(4244), HOME, AWAY, 2, 1, NOW) is False
        assert await _settle_one(SESSION, _pick(4245), HOME, AWAY, 2, 1, NOW) is False
    warned = [r for r in caplog.records if "not settleable" in r.getMessage()]
    assert len(warned) == 2


async def test_unsettleable_counts_collected_by_market(caplog) -> None:  # type: ignore[no-untyped-def]
    reset_unsettleable_warning_state()
    counts: Counter[str] = Counter()
    with caplog.at_level(logging.WARNING, logger="app.settlement.engine"):
        assert (
            await _settle_one(
                SESSION,
                _pick(4246),
                HOME,
                AWAY,
                2,
                1,
                NOW,
                unsettleable_counts=counts,
            )
            is False
        )
        assert (
            await _settle_one(
                SESSION,
                _pick(4247, market="btts", selection="Maybe"),
                HOME,
                AWAY,
                2,
                1,
                NOW,
                unsettleable_counts=counts,
            )
            is False
        )
    assert counts == Counter({"spreads": 1, "btts": 1})


def test_unsettleable_summary_format() -> None:
    summary = _unsettleable_summary(Counter({"btts": 2, "spreads": 1}))
    assert summary == "3 picks unsettleable (2 btts, 1 spreads)"
