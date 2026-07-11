"""Task 5 composition-root n_eff wiring (audit-lows 2026-07-11, item 8).

The uncertainty-shrink SHADOW annotation shipped with PipelineDeps.stake_shrink
left at its default and stake_neff_lookup=None, so every pick annotated
phi/n_eff/shrunk_fraction as None. The composition root now binds
uncertainty_shrink_policy(settings) and a CACHED per-(sport, market) settled
trusted-CLV-count lookup (repositories.settled_trusted_counts behind a TTL —
never a per-pick query). The enable flag stays default-OFF: stakes are
bit-identical; only the annotations become real. No network, no DB — the
count source is stubbed at its import site.
"""

from datetime import UTC, datetime

import fakeredis.aioredis as fakeredis
import httpx
import pytest

import app.scheduler as scheduler_mod
from app.config import Settings
from app.scheduler import _build_stake_neff_source, build_scheduler
from app.storage import repositories


def make_settings(**overrides: object) -> Settings:
    overrides.setdefault("odds_source", "oddsportal")
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


class _FakeSessionFactory:
    """Bare async-context session factory; the session itself is never used
    because settled_trusted_counts is stubbed."""

    def __call__(self) -> "_FakeSessionFactory":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_neff_lookup_reads_cached_counts_and_honors_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def fake_counts(session: object) -> dict[tuple[str, str], int]:
        calls["n"] += 1
        return {("soccer", "totals"): 41}

    monkeypatch.setattr(repositories, "settled_trusted_counts", fake_counts)
    lookup, refresh = _build_stake_neff_source(_FakeSessionFactory(), ttl_seconds=3600.0)  # type: ignore[arg-type]

    # Before any refresh: honest None (annotation stays null, never fabricated).
    assert lookup("value", "soccer", "totals") is None
    await refresh()
    assert calls["n"] == 1
    assert lookup("value", "soccer", "totals") == 41
    # strategy is not a warehouse dimension — ignored by design.
    assert lookup("model", "soccer", "totals") == 41
    # Unknown cell: None (honest), never 0-fabricated.
    assert lookup("value", "basketball", "h2h") is None
    # Within the TTL a second refresh is a no-op — never a per-cycle re-query.
    await refresh()
    assert calls["n"] == 1


async def test_neff_refresh_failure_is_isolated_and_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def flaky_counts(session: object) -> dict[tuple[str, str], int]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return {("soccer", "h2h"): 7}

    monkeypatch.setattr(repositories, "settled_trusted_counts", flaky_counts)
    lookup, refresh = _build_stake_neff_source(_FakeSessionFactory(), ttl_seconds=3600.0)  # type: ignore[arg-type]

    await refresh()  # must not raise — annotation source never blocks minting
    assert lookup("value", "soccer", "h2h") is None
    # The failed attempt did NOT stamp the TTL clock: the next cycle retries.
    await refresh()
    assert calls["n"] == 2
    assert lookup("value", "soccer", "h2h") == 7


async def test_build_scheduler_binds_stake_shrink_policy_and_neff_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    real_deps = scheduler_mod.PipelineDeps

    def capturing_deps(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_deps(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scheduler_mod, "PipelineDeps", capturing_deps)
    settings = make_settings(stake_uncertainty_kappa=77.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200))
    ) as client:
        build_scheduler(
            settings,
            client,
            fakeredis.FakeRedis(),
            session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
        )

    shrink = captured["stake_shrink"]
    assert shrink.enabled is False  # type: ignore[attr-defined]  # SHADOW: default-off, ADR-0022 gated
    assert shrink.kappa == 77.0  # type: ignore[attr-defined]  # Settings-built at the composition root
    lookup = captured["stake_neff_lookup"]
    assert callable(lookup)
    # Unrefreshed at build time: the lookup reads the (empty) cache — None.
    assert lookup("value", "soccer", "h2h") is None


async def test_neff_refresh_runs_via_settled_trusted_counts_shape() -> None:
    # The DB aggregation reuses promotion_distance_cells: pin the (sport,
    # market) -> n_trusted projection on a pure rows fixture.
    now = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    trusted_row = (
        "soccer",
        "totals",
        now,
        0.03,  # clv_log
        "pinnacle",
        True,  # close_independent
        True,  # has_snapshot_close
        2.0,  # decimal_odds
        0.52,  # closing_fair_probability
        0.55,  # model_probability (moved: non-tautological)
        None,
        None,
        "SoftBook",
    )
    untrusted_row = ("soccer", "totals", now, None, None, None, None, None, None, None, None, None)
    cells = repositories.promotion_distance_cells([trusted_row, untrusted_row], now=now)
    counts = {(str(c["sport"]), str(c["market"])): int(c["n_trusted"]) for c in cells}
    assert counts == {("soccer", "totals"): 1}
