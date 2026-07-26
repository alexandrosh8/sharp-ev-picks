"""TASK EL: the poll path's dominant CPU sections must not run on the event loop.

Measured locally (2026-07-26 timing harness): one OddsChecker match page costs
0.4-1.2s of BeautifulSoup+json CPU per html-scanning pass (2-3 passes per page:
supported_market_ids_from_match_page and the parse_match_page fallback), and
the value pipeline's group+devig block costs ~0.5s per 18k snapshots. Both ran
inline on the event loop, freezing every concurrent HTTP response once per poll
cycle (GET /live TTFB outliers of 1.1-2.6s on the poll cadence). These tests
pin the fix: those sections must execute in a worker thread via
asyncio.to_thread, never on the loop thread.
"""

import threading
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.ingestion.base import EventDirectory
from app.pipeline import run_value_pipeline
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn
from tests.test_value_pipeline import (
    FakeLoader,
    RecordingSink,
    make_deps,
    market_snapshots,
)


async def test_oddschecker_match_page_parse_runs_off_the_event_loop(
    monkeypatch: Any,
) -> None:
    import app.ingestion.oddschecker as oc

    loop_thread = threading.current_thread().name
    seen: dict[str, str] = {}

    def fake_supported_ids(
        html: str,
        *,
        markets: Sequence[Market] | None = None,
        include_other: bool = False,
    ) -> list[str]:
        seen["supported_market_ids"] = threading.current_thread().name
        return []

    def fake_parse_match_page(
        html: str,
        *,
        url: str,
        directory: EventDirectory,
        now: datetime | None = None,
        markets: Sequence[Market] | None = None,
        max_snapshots: int = oc.MAX_SNAPSHOTS_PER_MATCH,
    ) -> list[OddsSnapshotIn]:
        seen["parse_match_page"] = threading.current_thread().name
        return []

    monkeypatch.setattr(oc, "supported_market_ids_from_match_page", fake_supported_ids)
    monkeypatch.setattr(oc, "parse_match_page", fake_parse_match_page)

    loader = oc.OddsCheckerLoader(EventDirectory())
    page = oc.OddsCheckerFetchResult(
        url="https://www.oddschecker.com/football/test-match/winner",
        html="<html></html>",
        status_code=200,
    )
    out = await loader._parse_modern_or_legacy_match_page(page, now=None, session=None)
    assert out == []
    # Both html-scanning passes must have run OFF the loop thread.
    assert seen["supported_market_ids"] != loop_thread
    assert seen["parse_match_page"] != loop_thread


async def test_value_pipeline_pricing_block_runs_off_the_event_loop(
    monkeypatch: Any,
) -> None:
    import app.pipeline as pipeline_mod

    loop_thread = threading.current_thread().name
    seen: dict[str, str] = {}
    real_fair = pipeline_mod.event_fair_probs

    def spy_fair(*args: Any, **kwargs: Any) -> Any:
        seen["event_fair_probs"] = threading.current_thread().name
        return real_fair(*args, **kwargs)

    monkeypatch.setattr(pipeline_mod, "event_fair_probs", spy_fair)

    sink = RecordingSink()
    deps = make_deps(sink, FakeLoader(market_snapshots()))
    picks = await run_value_pipeline(deps, "soccer")
    # Behavior unchanged: this slate still mints its premium pick.
    assert len(picks) == 1
    # The group+devig pricing block must have run OFF the loop thread.
    assert seen["event_fair_probs"] != loop_thread
