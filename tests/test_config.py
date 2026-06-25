"""Settings safety validator: tampering with picks-only flags is fatal."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings, gate_policy, stake_policy, value_policy
from app.edge.value_policy import ValuePolicy


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
        {"max_recommended_stake_percent": 5.0},  # 500% — fat-fingered .env (audit #1)
        {"max_recommended_stake_percent": 0.0},
        {"max_recommended_stake_percent": -0.1},
        {"max_daily_exposure_percent": 1.5},
        {"max_daily_exposure_percent": 0.0},
        {"fractional_kelly": 1.5},
        {"fractional_kelly": 0.0},
        {"bankroll_base": 0.0},
        {"bankroll_base": -100.0},
        # recommended per-bet cap must never exceed the daily cap
        {"max_recommended_stake_percent": 0.10, "max_daily_exposure_percent": 0.05},
    ],
)
def test_stake_caps_out_of_bounds_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_stake_caps_within_bounds_accepted() -> None:
    s = make_settings(
        bankroll_base=1000.0,
        fractional_kelly=0.25,
        max_recommended_stake_percent=0.02,
        max_daily_exposure_percent=0.05,
    )
    assert s.max_recommended_stake_percent == 0.02
    assert s.max_daily_exposure_percent == 0.05


@pytest.mark.parametrize(
    "overrides",
    [
        {"auto_betting": True},
        {"bet_execution_enabled": True},
        {"picks_only": False},
        {"manual_betting_only": False},
        {"read_only_market_data": False},
        {"paper_trading": True},  # locked false — not a paper-trading system
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


def test_arcadia_proxy_urls_parse_without_secret_leak() -> None:
    s = make_settings(
        arcadia_proxy_urls=(
            "http://user-one:pass-one@proxy-one.example:8000,"
            "https://user-two:pass-two@proxy-two.example:8443"
        )
    )
    assert s.arcadia_proxies() == (
        "http://user-one:pass-one@proxy-one.example:8000",
        "https://user-two:pass-two@proxy-two.example:8443",
    )


def test_bad_arcadia_proxy_url_error_does_not_echo_secret() -> None:
    secret = "leaky-password"
    with pytest.raises(ValidationError) as excinfo:
        make_settings(arcadia_proxy_urls=f"http://user:{secret}@proxy.example")
    msg = str(excinfo.value)
    assert secret not in msg
    assert "proxy.example" not in msg


def test_public_app_bind_requires_dashboard_auth() -> None:
    with pytest.raises(ValidationError, match="APP_HOST_BIND exposes the dashboard"):
        make_settings(app_host_bind="0.0.0.0")


def test_public_app_bind_passes_with_dashboard_auth() -> None:
    s = make_settings(
        app_host_bind="0.0.0.0",
        dashboard_auth_enabled=True,
        dashboard_auth_password_hash="pbkdf2_sha256$1$abcd$1234",
        dashboard_session_secret="test-session-secret",
    )
    assert s.app_host_bind == "0.0.0.0"


def test_dashboard_auth_password_hash_format_is_validated() -> None:
    with pytest.raises(ValidationError, match="DASHBOARD_AUTH_PASSWORD_HASH must look like"):
        make_settings(
            dashboard_auth_enabled=True,
            dashboard_auth_password_hash="pbkdf2_sha256",
            dashboard_session_secret="test-session-secret",
        )


def test_loopback_app_bind_does_not_require_dashboard_auth() -> None:
    assert make_settings(app_host_bind="127.0.0.1").dashboard_auth_enabled is False


def test_dashboard_auth_enabled_with_blank_creds_is_first_run_mode() -> None:
    # Auth ON but no .env hash/secret is allowed: the password is set via the
    # first-run /setup screen (persisted to the DB), so nothing lives in .env.
    s = make_settings(dashboard_auth_enabled=True)
    assert s.dashboard_auth_enabled is True
    assert s.dashboard_auth_password_hash.get_secret_value() == ""


def test_dashboard_auth_requires_both_hash_and_secret_or_neither() -> None:
    with pytest.raises(ValidationError, match="BOTH DASHBOARD_AUTH_PASSWORD_HASH"):
        make_settings(
            dashboard_auth_enabled=True,
            dashboard_auth_password_hash="pbkdf2_sha256$1$abcd$1234",
            dashboard_session_secret="",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oddsportal_concurrency", 0),  # Semaphore(0) upstream = silent hang
        ("oddsportal_concurrency", 6),  # responsible-pacing ceiling
        ("oddsportal_concurrency", -1),
        ("oddsportal_request_delay", 0.4),  # below responsible floor
        ("oddsportal_request_delay", -1.0),
        ("poll_interval_seconds", 29),
        ("poll_interval_seconds", 0),
        ("scrape_nav_timeout_ms", 14999),  # below the upstream-default floor
        ("scrape_nav_timeout_ms", 120001),  # above the stall-guard cap
        ("scrape_nav_timeout_ms", 0),
        ("scrape_cycle_timeout_seconds", 59),  # below the 60s floor
        ("scrape_cycle_timeout_seconds", 7201),  # above the 2h cap
        ("scrape_cycle_timeout_seconds", 0),
    ],
)
def test_out_of_range_pacing_knobs_fail_at_startup(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oddsportal_concurrency", 1),
        ("oddsportal_concurrency", 5),
        ("oddsportal_request_delay", 0.5),
        ("oddsportal_request_delay", 3.0),
        ("poll_interval_seconds", 30),
        ("poll_interval_seconds", 300),
        ("scrape_nav_timeout_ms", 15000),  # floor = upstream default (allowed)
        ("scrape_nav_timeout_ms", 30000),  # the recommended default
        ("scrape_nav_timeout_ms", 120000),  # cap
        ("scrape_cycle_timeout_seconds", 60),  # floor
        ("scrape_cycle_timeout_seconds", 900),  # the recommended default
        ("scrape_cycle_timeout_seconds", 7200),  # cap
    ],
)
def test_in_range_pacing_knobs_pass(field: str, value: float) -> None:
    assert getattr(make_settings(**{field: value}), field) == value


def test_scrape_cycle_timeout_default_is_prod_safe() -> None:
    # The per-cycle scrape watchdog must be ON by default (no .env needed): a
    # hung Over/Under extraction can never wedge poll_odds in prod. Generous
    # enough that a healthy worldwide slate finishes, finite so a wedge can't.
    s = make_settings()
    assert s.scrape_cycle_timeout_seconds == 900.0
    assert 60.0 <= s.scrape_cycle_timeout_seconds <= 7200.0


def test_scrape_nav_timeout_default_raises_the_upstream_15s_floor() -> None:
    # The default must be ABOVE OddsHarvester's hardcoded 15000ms (the whole
    # point of the knob): fewer heavy match pages time out.
    assert make_settings().scrape_nav_timeout_ms == 30000
    assert make_settings().scrape_nav_timeout_ms > 15000


def test_oddsportal_use_json_feed_defaults_off() -> None:
    # The curl_cffi JSON feed is SELECTABLE and OFF by default — the proven
    # Playwright/OddsHarvester path stays the default until prod-verified. When
    # the flag is later flipped, there is NO Playwright odds fallback: a per-match
    # JSON failure is a scrape gap (operator 2026-06-23).
    assert make_settings().oddsportal_use_json_feed is False


def test_oddsportal_use_json_feed_enables_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODDSPORTAL_USE_JSON_FEED", "true")
    assert Settings(_env_file=None).oddsportal_use_json_feed is True


def test_out_of_range_pacing_knob_via_env_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODDSPORTAL_CONCURRENCY", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_value_strategy_defaults_are_the_train_chosen_optimum() -> None:
    # v4: chosen on TRAIN seasons only over 7 devig methods with the 1.60
    # odds floor, confirmed one-shot on holdout — docs/backtesting/
    # value-findings.md. Must parse as a valid DevigMethod or the scheduler
    # would crash at startup.
    from app.probabilities.devig import DevigMethod

    s = make_settings()
    assert s.pick_strategy == "value"
    assert s.value_min_edge == 0.03
    assert s.value_min_odds == 1.30  # validated floor (2026-06-18 held-out sweep)
    assert DevigMethod(s.value_devig) is DevigMethod.DIFFERENTIAL_MARGIN


def test_volume_tier_floor_default_is_validated_v2_threshold() -> None:
    # v2 holdout n=379, CLV +0.019 — the volume (shadow) tier's evidence base.
    assert make_settings().value_volume_min_edge == 0.015


def test_volume_floor_above_premium_is_fatal() -> None:
    # The volume tier extends BELOW the premium threshold; inverting the
    # ordering would alert on unvalidated edges — refuse to start.
    with pytest.raises(ValidationError, match="VALUE_VOLUME_MIN_EDGE"):
        make_settings(value_volume_min_edge=0.05, value_min_edge=0.03)


def test_equal_tier_floors_disable_volume_cleanly() -> None:
    s = make_settings(value_volume_min_edge=0.03, value_min_edge=0.03)
    assert s.value_volume_min_edge == s.value_min_edge  # valid: tier off


def test_betfair_exchange_min_liquidity_default_admits_obscure_markets() -> None:
    # REGRESSION (2026-06-23): the old £500 floor (a single major-match probe)
    # silently dropped every obscure Betfair market (live liquidity £12-£23) ->
    # only 22 betfair_soccer events ever captured. The default must stay low
    # enough to admit real small-exchange markets while still gating £0 dust.
    s = make_settings()
    assert s.betfair_exchange_min_liquidity == 10.0
    assert s.betfair_exchange_min_liquidity < 14.0  # the thinnest live obscure £


def test_all_leagues_with_wide_market_list_is_fatal() -> None:
    """leagues=all + a market list OVER the budget = multi-hour cycles
    (live-measured ~73s/match) whose slate the odds-age gate then almost
    entirely discards — the trim is mandatory, so refuse to start. The
    committed DEFAULT markets are within budget (see
    test_default_leagues_are_all_within_market_budget), so this drives the
    guard with an explicit over-budget override."""
    wide_football = "1x2,btts,double_chance,dnb,over_under_2_5"  # 5 > budget 4
    wide_basketball = (
        "home_away,over_under_games_220_5,over_under_games_225_5,"
        "over_under_games_230_5,over_under_games_235_5"  # 5 > budget 4
    )
    with pytest.raises(ValidationError, match="ODDSPORTAL_FOOTBALL_MARKETS"):
        make_settings(oddsportal_football_leagues="all", oddsportal_football_markets=wide_football)
    with pytest.raises(ValidationError, match="ODDSPORTAL_BASKETBALL_MARKETS"):
        make_settings(
            oddsportal_basketball_leagues="all", oddsportal_basketball_markets=wide_basketball
        )


def test_all_leagues_within_market_budget_passes() -> None:
    from app.config import ODDSPORTAL_ALL_LEAGUES_MARKET_BUDGET

    s = make_settings(
        oddsportal_football_leagues="all",
        oddsportal_football_markets="1x2,over_under_2_5,btts,double_chance",
        oddsportal_basketball_leagues="all",
        oddsportal_basketball_markets="home_away",
    )
    assert len(s.oddsportal_football_markets.split(",")) <= ODDSPORTAL_ALL_LEAGUES_MARKET_BUDGET


def test_scoped_leagues_allow_a_wide_market_list() -> None:
    # The budget binds ONLY on the exact ["all"] sentinel — a SCOPED-league
    # config (specific slugs, not "all") keeps the full devig-sound market
    # families. (Lists mixing 'all' with slugs are rejected by the loader as
    # an unknown league, so they never reach a wide-market scrape.)
    s = make_settings(
        oddsportal_football_leagues="england-premier-league",
        oddsportal_football_markets=(
            "1x2,btts,double_chance,dnb,over_under_1_5,over_under_2_5,over_under_3_5"
        ),
    )
    assert len(s.oddsportal_football_markets.split(",")) > 4


def test_default_leagues_are_all_within_market_budget() -> None:
    # The committed default matches the reference deployment: worldwide
    # ("all") leagues with the market list trimmed to the budget, so a bare
    # Settings() constructs and a fresh deploy stays under an hour per cycle.
    from app.config import ODDSPORTAL_ALL_LEAGUES_MARKET_BUDGET

    s = make_settings()
    assert s.oddsportal_football_leagues == "all"
    assert s.oddsportal_basketball_leagues == "all"
    assert len(s.oddsportal_football_markets.split(",")) <= ODDSPORTAL_ALL_LEAGUES_MARKET_BUDGET
    assert len(s.oddsportal_basketball_markets.split(",")) <= ODDSPORTAL_ALL_LEAGUES_MARKET_BUDGET


# --- premium-tier adjustment knobs (2026-06 research): default OFF -----------


def test_premium_adjustment_knobs_default_to_current_behavior() -> None:
    # Every knob empty/None => the built policies are exact no-ops. This is
    # the contract that protects the live defaults (spent-holdout discipline:
    # no knob may change behavior without fresh-domain/nested-CV evidence).
    s = make_settings()
    assert s.value_min_edge_per_market == ""
    assert s.value_odds_bands == ""
    assert s.value_min_books_per_market == ""
    assert s.stake_max_drawdown is None
    assert s.stake_max_drawdown_probability is None
    # The premium-adjustment knobs stay no-op; the only non-empty default is the
    # max_edge data-error ceiling — a default-ON safety guard, not a tunable knob.
    assert value_policy(s) == ValuePolicy(max_edge=0.20)
    stakes = stake_policy(s)
    assert stakes.max_drawdown is None
    assert stakes.max_drawdown_probability is None


def test_value_policy_parses_market_maps_and_bands() -> None:
    s = make_settings(
        value_min_edge_per_market="1x2:0.04, Over_Under_2_5:0.035",
        value_odds_bands="1.8-2.6, 3.0-4.2",
        value_min_books_per_market="over_under_1_5:5",
    )
    policy = value_policy(s)
    assert policy.min_edge_by_market == (("1x2", 0.04), ("over_under_2_5", 0.035))
    assert policy.odds_bands == ((1.8, 2.6), (3.0, 4.2))
    assert policy.min_books_by_market == (("over_under_1_5", 5),)


def test_value_policy_parses_major_leagues() -> None:
    s = make_settings(value_major_leagues="Premier League, LaLiga , Serie A,")
    # names kept as given (normalized only at compare time), blanks dropped
    assert value_policy(s).major_leagues == ("Premier League", "LaLiga", "Serie A")
    # empty (default) = gate disabled — the policy stays the no-op default
    assert value_policy(make_settings()).major_leagues == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("value_min_edge_per_market", "1x2"),  # no value
        ("value_min_edge_per_market", "1x2:abc"),  # not a number
        ("value_min_edge_per_market", ":0.04"),  # empty key
        ("value_min_edge_per_market", "1x2:0.04,1x2:0.05"),  # duplicate key
        ("value_min_edge_per_market", "1x2:1.5"),  # edge outside (0, 1)
        ("value_odds_bands", "2.6-1.8"),  # lo > hi
        ("value_odds_bands", "0.9-2.0"),  # lo <= 1.0 (not decimal odds)
        ("value_odds_bands", "abc"),
        ("value_min_books_per_market", "h2h:0"),  # < 1 is a pointless entry
        ("value_min_books_per_market", "h2h:1.5"),  # not an integer
    ],
)
def test_malformed_adjustment_knobs_fail_at_startup(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_per_market_floor_below_volume_floor_is_fatal() -> None:
    # Same tier-ordering rule as the global validator, applied per market.
    with pytest.raises(ValidationError, match="inverts the tiers"):
        make_settings(value_min_edge_per_market="1x2:0.01")  # volume floor 0.015


def test_odds_band_entirely_below_min_odds_is_fatal() -> None:
    # value_min_odds=1.30 already rejects everything such a band could match;
    # a dead band silently rejecting ALL picks must refuse to start instead.
    with pytest.raises(ValidationError, match="can never match"):
        make_settings(value_odds_bands="1.1-1.25")


def test_stake_drawdown_knobs_must_be_set_together() -> None:
    with pytest.raises(ValidationError, match="both or neither"):
        make_settings(stake_max_drawdown=0.5)
    with pytest.raises(ValidationError, match="both or neither"):
        make_settings(stake_max_drawdown_probability=0.1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stake_max_drawdown", 0.0),
        ("stake_max_drawdown", 1.0),
        ("stake_max_drawdown_probability", 0.0),
        ("stake_max_drawdown_probability", 1.5),
    ],
)
def test_out_of_range_stake_drawdown_knobs_are_fatal(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_valid_stake_drawdown_pair_flows_into_stake_policy() -> None:
    s = make_settings(stake_max_drawdown=0.5, stake_max_drawdown_probability=0.1)
    stakes = stake_policy(s)
    assert stakes.max_drawdown == 0.5
    assert stakes.max_drawdown_probability == 0.1
    # the validated defaults stay untouched alongside the optional knob
    assert stakes.fractional_kelly == 0.25
    assert stakes.max_stake_fraction == 0.02


def test_parse_scraper_proxy_pool_parses_and_hides_creds() -> None:
    from app.config import parse_scraper_proxy_pool

    pool = parse_scraper_proxy_pool("1.2.3.4|8080|user|pass,5.6.7.8|9090|u2|p2")
    assert len(pool) == 2
    assert pool[0].url == "http://1.2.3.4:8080"
    assert pool[0].username == "user"
    assert pool[0].password == "pass"
    assert "user" not in pool[0].url  # creds are NOT in the url field

    # malformed (3 fields) rejected; the secret value never appears in the error
    with pytest.raises(ValueError) as ei:
        parse_scraper_proxy_pool("1.2.3.4|8080|onlyuser")
    assert "onlyuser" not in str(ei.value)
    # non-numeric port rejected
    with pytest.raises(ValueError):
        parse_scraper_proxy_pool("1.2.3.4|notaport|u|p")
    # empty -> empty tuple (default OFF)
    assert parse_scraper_proxy_pool("") == ()
