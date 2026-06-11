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

    # --- Pick strategy --------------------------------------------------------
    # "value" = sharp-vs-soft line shopping (BACKTESTED, positive holdout CLV —
    #           docs/backtesting/value-findings.md). The validated default.
    # "model" = Dixon-Coles goals model (negative CLV in backtest; screens only).
    #
    # Defaults below are the v4 train-chosen optimum over SEVEN devig methods
    # with the 1.60 odds floor: differential-margin devig, edge >= 0.03 —
    # holdout n=61, ROI +21.1%, incremental CLV +0.106 (> 2SE). shin/0.03 is
    # statistically indistinguishable (n=58, CLV +0.108). Volume tier:
    # VALUE_MIN_EDGE=0.015 (v2 holdout n=379, CLV +0.019).
    pick_strategy: str = "value"
    value_min_edge: float = 0.03
    # User policy: never pick odds below 1.60. The backtest validated at
    # >= 1.30; a higher floor only narrows to a subset of validated picks.
    value_min_odds: float = 1.60
    value_devig: str = "differential_margin_weighting"  # any DevigMethod value

    # --- Odds sources (read-only access) -----------------------------------------
    # "oddsportal" = free OddsPortal odds via OddsHarvester (default, no key);
    # "odds_api"   = The Odds API (needs keys below).
    odds_source: str = "oddsportal"
    oddsportal_football_leagues: str = "england-premier-league"  # csv of slugs
    # Devig-sound markets only: full mutually-exclusive outcome sets. Asian
    # handicaps are HALF-LINES only (integer/quarter lines carry pushes and
    # are rejected by the loader); European handicap is 3-way. 1x2+ou25 are
    # backtest-validated; the rest use the identical mechanism on thinner
    # evidence. Every extra market adds scrape time per match.
    oddsportal_football_markets: str = (
        "1x2,over_under_2_5,btts,dnb,double_chance,asian_handicap_-1_5,european_handicap_-1"
    )
    # Basketball (club competitions only — OddsHarvester maps no national-team
    # events like EuroBasket). Empty leagues = basketball polling off.
    # Totals lines are per-game; configure a band around current league totals.
    oddsportal_basketball_markets: str = (
        "home_away,over_under_games_215_5,over_under_games_220_5,over_under_games_225_5"
    )
    oddsportal_basketball_leagues: str = "nba,euroleague"
    # Dated scraping: each cycle covers today..today+N (UTC) instead of a
    # league's whole upcoming list — far-future fixtures are skipped and
    # cycle time tracks the actionable slate. Unset = legacy upcoming page.
    oddsportal_days_ahead: int | None = 1
    # OddsHarvester's own pacing knobs (README: "adjust responsibly").
    # Concurrency = parallel match pages; request_delay = seconds between
    # requests (+ jitter upstream). Tuning these is sanctioned configuration
    # — anti-bot bypassing remains forbidden everywhere.
    oddsportal_concurrency: int = 3
    oddsportal_request_delay: float = 1.0
    # Browser locale, paired with the loader's forced UTC timezone for a
    # coherent human fingerprint (UTC = London -> en-GB).
    oddsportal_locale: str = "en-GB"
    # Seconds between poll cycles. With max_instances=1 + coalesce, a value
    # below the cycle duration just runs cycles back-to-back — effective
    # freshness is one cycle length; the scrape itself is the floor.
    poll_interval_seconds: int = 300
    footballdata_league_codes: str = "E0"  # csv, European mmz4281 divisions
    footballdata_seasons: str = "2425,2526"  # csv, football-data 4-digit seasons
    # Optional: train on a "new leagues" country code (e.g. BRA) instead of the
    # European codes — use for in-season non-European leagues. Empty = European.
    footballdata_new_league_code: str = ""
    football_totals_line: float = 2.5
    model_confidence: float = 0.65

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
