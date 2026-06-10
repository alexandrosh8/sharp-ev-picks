"""Application settings — the ONLY module that reads the environment.

SAFETY (ADR-0002): this platform is decision-support only. The validator
below turns any attempt to enable betting execution into a fatal startup
error. There is no code anywhere that reads these flags to enable anything.
"""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.edge.gates import GatePolicy
from app.risk.staking import StakePolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai"
    redis_url: str = "redis://localhost:6380/0"

    # --- Safety flags (locked defaults; flipping any is a fatal error) ------
    picks_only: bool = True
    manual_betting_only: bool = True
    auto_betting: bool = False
    bet_execution_enabled: bool = False
    read_only_market_data: bool = True
    paper_trading: bool = False

    # --- Pick gates ----------------------------------------------------------
    min_edge: float = 0.03
    min_ev: float = 0.01
    min_confidence: float = 0.60
    max_odds_age_seconds: float = 300.0
    min_liquidity: float = 0.0

    # --- Recommended stake sizing (informational only) ------------------------
    bankroll_base: float = 1000.0
    fractional_kelly: float = 0.25
    max_recommended_stake_percent: float = 0.02
    max_daily_exposure_percent: float = 0.05

    # --- Alerts ----------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""

    # --- Odds sources (read-only access; all optional) ---------------------------
    odds_api_key: str = ""
    odds_api_key_1: str = ""
    odds_api_key_2: str = ""
    odds_api_key_3: str = ""

    @model_validator(mode="after")
    def _enforce_picks_only(self) -> "Settings":
        if self.auto_betting or self.bet_execution_enabled:
            raise ValueError(
                "SAFETY VIOLATION: AUTO_BETTING/BET_EXECUTION_ENABLED must stay false. "
                "This platform never places bets (ADR-0002)."
            )
        if not (self.picks_only and self.manual_betting_only and self.read_only_market_data):
            raise ValueError(
                "SAFETY VIOLATION: PICKS_ONLY, MANUAL_BETTING_ONLY and "
                "READ_ONLY_MARKET_DATA must stay true (ADR-0002)."
            )
        return self

    def odds_api_keys(self) -> tuple[str, ...]:
        """Configured Odds API keys for rotation, in order, empties dropped."""
        keys = (self.odds_api_key, self.odds_api_key_1, self.odds_api_key_2, self.odds_api_key_3)
        return tuple(k for k in keys if k)


def gate_policy(settings: Settings) -> GatePolicy:
    return GatePolicy(
        min_edge=settings.min_edge,
        min_ev=settings.min_ev,
        min_confidence=settings.min_confidence,
        max_odds_age_seconds=settings.max_odds_age_seconds,
        min_liquidity=settings.min_liquidity,
    )


def stake_policy(settings: Settings) -> StakePolicy:
    return StakePolicy(
        fractional_kelly=settings.fractional_kelly,
        max_stake_fraction=settings.max_recommended_stake_percent,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
