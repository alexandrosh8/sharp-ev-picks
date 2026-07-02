"""Evidence-gated sport promotion at the composition root (audit WP6).

Doctrine: a sport leaves the experimental shadow tier (earns premium alerts)
only after its per-(sport, market) CLV-readiness evidence clears the
SportMarketClvGate bars (app/backtesting/live_evidence.py) AND the operator
logs an ADR. Findings fixed here:

1. NBA_EXPERIMENTAL=false was a bare env flip — zero evidence check. Now the
   promotion additionally requires the explicit acknowledgement flag
   NBA_PROMOTION_ACKNOWLEDGE_EVIDENCE=true; without it the composition root
   REFUSES (logs + keeps basketball experimental). Fail-closed.
2. The odds_source="odds_api" branch never populated experimental_sports at
   all, so basketball would have alerted premium there even with
   NBA_EXPERIMENTAL=true. Both branches now share one helper.
"""

import httpx
import pytest

from app.config import Settings
from app.scheduler import _unvalidated_sport_scopes

_BB = frozenset({"basketball"})
_TENNIS = frozenset({"tennis"})
_NFL = frozenset({"american_football"})


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. The promotion gate itself (pure helper)
# ---------------------------------------------------------------------------


def test_basketball_experimental_by_default() -> None:
    experimental, visibility = _unvalidated_sport_scopes(make_settings(), basketball_keys=_BB)
    assert experimental == _BB
    assert visibility == frozenset()


def test_nba_experimental_false_alone_refuses_promotion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The bare env flip must NOT promote: without the explicit evidence
    # acknowledgement, basketball STAYS experimental and the refusal is logged.
    settings = make_settings(nba_experimental=False)
    with caplog.at_level("WARNING", logger="app.scheduler"):
        experimental, _ = _unvalidated_sport_scopes(settings, basketball_keys=_BB)
    assert experimental == _BB  # fail-closed: still demoted to shadow
    assert any("NBA_PROMOTION_ACKNOWLEDGE_EVIDENCE" in r.message for r in caplog.records)


def test_nba_promotion_with_acknowledged_evidence() -> None:
    # Deliberate, ADR-logged promotion: BOTH flags → basketball leaves the
    # experimental set (premium-eligible).
    settings = make_settings(nba_experimental=False, nba_promotion_acknowledge_evidence=True)
    experimental, visibility = _unvalidated_sport_scopes(settings, basketball_keys=_BB)
    assert experimental == frozenset()
    assert visibility == frozenset()


def test_acknowledge_alone_does_not_promote() -> None:
    # The acknowledgement flag on its own changes nothing while
    # NBA_EXPERIMENTAL=true — demotion stands.
    settings = make_settings(nba_promotion_acknowledge_evidence=True)
    experimental, _ = _unvalidated_sport_scopes(settings, basketball_keys=_BB)
    assert experimental == _BB


def test_acknowledge_flag_defaults_false() -> None:
    assert make_settings().nba_promotion_acknowledge_evidence is False


def test_tennis_and_nfl_visibility_only_by_default() -> None:
    experimental, visibility = _unvalidated_sport_scopes(
        make_settings(), basketball_keys=_BB, tennis_keys=_TENNIS, nfl_keys=_NFL
    )
    assert experimental == _BB
    assert visibility == _TENNIS | _NFL


def test_tennis_and_nfl_experimental_when_unvalidated_picks_enabled() -> None:
    experimental, visibility = _unvalidated_sport_scopes(
        make_settings(enable_unvalidated_picks=True),
        basketball_keys=_BB,
        tennis_keys=_TENNIS,
        nfl_keys=_NFL,
    )
    assert experimental == _BB | _TENNIS | _NFL
    assert visibility == frozenset()


def test_empty_keys_yield_empty_scopes() -> None:
    experimental, visibility = _unvalidated_sport_scopes(
        make_settings(), basketball_keys=frozenset()
    )
    assert experimental == frozenset()
    assert visibility == frozenset()


# ---------------------------------------------------------------------------
# 2. Both scheduler branches consult the SAME gate helper
# ---------------------------------------------------------------------------


async def _recorded_scope_calls(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, frozenset[str]]]:
    import fakeredis.aioredis as fakeredis

    import app.scheduler as scheduler_mod

    calls: list[dict[str, frozenset[str]]] = []
    real = scheduler_mod._unvalidated_sport_scopes

    def recording(
        settings_arg: Settings,
        *,
        basketball_keys: frozenset[str] = frozenset(),
        tennis_keys: frozenset[str] = frozenset(),
        nfl_keys: frozenset[str] = frozenset(),
    ) -> tuple[frozenset[str], frozenset[str]]:
        calls.append(
            {
                "basketball": basketball_keys,
                "tennis": tennis_keys,
                "nfl": nfl_keys,
            }
        )
        return real(
            settings_arg,
            basketball_keys=basketball_keys,
            tennis_keys=tennis_keys,
            nfl_keys=nfl_keys,
        )

    monkeypatch.setattr(scheduler_mod, "_unvalidated_sport_scopes", recording)
    redis = fakeredis.FakeRedis()
    async with httpx.AsyncClient() as client:
        scheduler_mod.build_scheduler(settings, client, redis)
    return calls


async def test_oddsportal_branch_routes_sports_through_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = await _recorded_scope_calls(make_settings(), monkeypatch)
    assert calls == [{"basketball": _BB, "tennis": _TENNIS, "nfl": _NFL}]


async def test_odds_api_branch_routes_basketball_through_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression (audit WP6): this branch previously never populated
    # experimental_sports — basketball would alert premium unconditionally.
    settings = make_settings(odds_source="odds_api", odds_api_key_1="test-key")
    calls = await _recorded_scope_calls(settings, monkeypatch)
    assert calls == [
        {"basketball": frozenset({"basketball_nba"}), "tennis": frozenset(), "nfl": frozenset()}
    ]


async def test_odds_api_branch_without_keys_skips_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No keys → polling disabled → no pipeline → nothing to scope.
    settings = make_settings(odds_source="odds_api")
    calls = await _recorded_scope_calls(settings, monkeypatch)
    assert calls == []
