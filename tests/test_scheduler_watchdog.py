"""poll_odds wedge WATCHDOG (incident 2026-07-02 23:38Z): a hung per-sport
cycle must be cancelled at the budget and never starve later cycles."""

import asyncio
import logging

import pytest

from app.scheduler import run_sport_cycle_guarded


@pytest.fixture(autouse=True)
def _clear_poll_heartbeats():  # type: ignore[no-untyped-def]
    from app.pipeline import LAST_POLL

    LAST_POLL.clear()
    yield
    LAST_POLL.clear()


@pytest.mark.asyncio
async def test_hung_cycle_is_cancelled_at_budget_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wedged(deps: object, sport_key: str) -> None:
        started.set()
        try:
            await asyncio.sleep(3600)  # the wedge
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with caplog.at_level(logging.WARNING, logger="app.scheduler"):
        await asyncio.wait_for(
            run_sport_cycle_guarded(wedged, object(), "soccer", cycle_budget=1), timeout=10
        )
    assert started.is_set()
    assert cancelled.is_set()  # the inner task really was cancelled, not leaked
    assert any("WATCHDOG" in r.message for r in caplog.records)
    from app.pipeline import LAST_POLL

    assert LAST_POLL["soccer"]["state"] == "failed"
    assert LAST_POLL["soccer"]["failure_reason"] == "timeout"
    assert LAST_POLL["soccer"]["degraded"] is True


@pytest.mark.asyncio
async def test_fast_cycle_untouched_and_next_sport_reachable() -> None:
    ran: list[str] = []

    async def fast(deps: object, sport_key: str) -> None:
        ran.append(sport_key)

    await run_sport_cycle_guarded(fast, object(), "soccer", cycle_budget=900)
    await run_sport_cycle_guarded(fast, object(), "basketball", cycle_budget=0)  # watchdog off
    assert ran == ["soccer", "basketball"]
    from app.pipeline import LAST_POLL

    assert LAST_POLL["soccer"]["state"] == "completed"
    assert LAST_POLL["basketball"]["state"] == "completed"


@pytest.mark.asyncio
async def test_running_sport_publishes_in_progress_heartbeat() -> None:
    from app.pipeline import LAST_POLL

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(deps: object, sport_key: str) -> None:
        del deps, sport_key
        started.set()
        await release.wait()

    task = asyncio.create_task(run_sport_cycle_guarded(slow, object(), "soccer", 900))
    await started.wait()
    assert LAST_POLL["soccer"]["state"] == "in_progress"
    assert LAST_POLL["soccer"]["in_progress"] is True

    release.set()
    await task
    assert LAST_POLL["soccer"]["state"] == "completed"
    assert LAST_POLL["soccer"]["in_progress"] is False


@pytest.mark.asyncio
async def test_pipeline_exception_still_swallowed_type_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom(deps: object, sport_key: str) -> None:
        raise ValueError("secret-bearing message must not be logged")

    with caplog.at_level(logging.ERROR, logger="app.scheduler"):
        await run_sport_cycle_guarded(boom, object(), "soccer", cycle_budget=900)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "ValueError" in joined
    assert "secret-bearing" not in joined
    from app.pipeline import LAST_POLL

    assert LAST_POLL["soccer"]["state"] == "failed"
    assert LAST_POLL["soccer"]["failure_reason"] == "ValueError"
