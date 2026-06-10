"""Settings safety validator: tampering with picks-only flags is fatal."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings, gate_policy, stake_policy


def make_settings(**overrides: Any) -> Settings:
    # _env_file=None keeps tests hermetic from any local .env
    return Settings(_env_file=None, **overrides)


def test_defaults_are_safe_and_load() -> None:
    s = make_settings()
    assert s.picks_only is True
    assert s.manual_betting_only is True
    assert s.auto_betting is False
    assert s.bet_execution_enabled is False
    assert s.read_only_market_data is True
    assert s.paper_trading is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"auto_betting": True},
        {"bet_execution_enabled": True},
        {"picks_only": False},
        {"manual_betting_only": False},
        {"read_only_market_data": False},
    ],
)
def test_safety_flag_tampering_is_fatal(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="SAFETY VIOLATION"):
        make_settings(**overrides)


def test_safety_flag_tampering_via_env_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_BETTING", "true")
    with pytest.raises(ValidationError, match="SAFETY VIOLATION"):
        Settings(_env_file=None)


def test_policies_built_from_settings() -> None:
    s = make_settings()
    gates = gate_policy(s)
    assert gates.min_edge == 0.03
    assert gates.min_ev == 0.01
    assert gates.max_odds_age_seconds == 300.0
    stakes = stake_policy(s)
    assert stakes.fractional_kelly == 0.25
    assert stakes.max_stake_fraction == 0.02


def test_odds_api_key_rotation_drops_empties() -> None:
    s = make_settings(odds_api_key_1="test-key-one", odds_api_key_3="test-key-three")
    assert s.odds_api_keys() == ("test-key-one", "test-key-three")


def test_value_strategy_defaults_are_the_v3_train_chosen_optimum() -> None:
    # Chosen on TRAIN seasons only (shin devig, thr=0.03) and confirmed once
    # on holdout — docs/backtesting/value-findings.md. Must parse as a valid
    # DevigMethod or the scheduler would crash at startup.
    from app.probabilities.devig import DevigMethod

    s = make_settings()
    assert s.pick_strategy == "value"
    assert s.value_min_edge == 0.03
    assert s.value_min_odds == 1.30
    assert DevigMethod(s.value_devig) is DevigMethod.SHIN
