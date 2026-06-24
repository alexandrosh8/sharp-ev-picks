"""Unit test for the SelectionManager._get_current_value patch in
app/ingestion/oddsportal.py.

After a market-tab switch OddsPortal re-renders the kickoff-events-nav, so the
unpatched immediate query_selector reads the active-period element as None/stale.
ensure_selected then misses its "already on the default Full Time period"
short-circuit, clicks needlessly, and — when the post-click verify races the
re-render — logs a benign ``ERROR Failed to set period to: Full Time`` while the
correct Full-Time odds are in fact extracted (sweep stays 0 failed / 100%).

The patch waits for the active element to (re)attach before reading, so the
short-circuit fires: no click, no false ERROR. The function is duck-typed over
page/strategy, so this needs no oddsharvester install (unlike the integration
suite in test_oddsportal_patches.py, which is import-skipped without it).
"""

import logging
from types import SimpleNamespace

import pytest

from app.ingestion.oddsportal import _patched_get_current_value


class _FakeActive:
    def __init__(self, value: str) -> None:
        self._value = value


class _FakeStrategy:
    name = "period"
    container_selector = "div[data-testid='kickoff-events-nav']"
    active_class = "active-item-calendar"

    async def extract_active_value(self, elem: "_FakeActive") -> str:
        return elem._value


def _self() -> SimpleNamespace:
    return SimpleNamespace(logger=logging.getLogger("test.selection"))


class _RaceyPage:
    """The active element is queryable only AFTER wait_for_selector is awaited —
    models the kickoff-nav re-render the unpatched immediate read loses."""

    def __init__(self, value: str) -> None:
        self._value = value
        self._attached = False
        self.waited = False

    async def wait_for_selector(
        self, selector: str, *, state: str | None = None, timeout: float | None = None
    ) -> "_FakeActive":  # noqa: ARG002
        self.waited = True
        self._attached = True
        return _FakeActive(self._value)

    async def query_selector(self, selector: str) -> "_FakeActive | None":  # noqa: ARG002
        return _FakeActive(self._value) if self._attached else None


@pytest.mark.asyncio
async def test_patched_get_current_value_waits_then_reads_settled_period() -> None:
    page = _RaceyPage("Full Time")
    value = await _patched_get_current_value(_self(), page, _FakeStrategy())
    assert page.waited  # it waited for the active element to (re)attach
    assert value == "Full Time"  # ...and read the settled default period (no click needed)


class _NeverPage:
    """Active element never attaches and the wait times out — the patch must
    fall through gracefully to None (same end-state as upstream), never raise."""

    async def wait_for_selector(
        self, selector: str, *, state: str | None = None, timeout: float | None = None
    ) -> None:  # noqa: ARG002
        raise TimeoutError("never attached")

    async def query_selector(self, selector: str) -> None:  # noqa: ARG002
        return None


@pytest.mark.asyncio
async def test_patched_get_current_value_returns_none_when_never_attached() -> None:
    value = await _patched_get_current_value(_self(), _NeverPage(), _FakeStrategy())
    assert value is None
