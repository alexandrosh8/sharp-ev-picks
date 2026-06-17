"""Application settings — the ONLY module that reads the environment.

SAFETY (ADR-0002): this platform is decision-support only. The validator
below turns any attempt to enable betting execution into a fatal startup
error. There is no code anywhere that reads these flags to enable anything.
"""

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.edge.gates import GatePolicy
from app.edge.value_policy import ValuePolicy
from app.risk.staking import StakePolicy

# leagues=all scrapes oddsportal's WORLDWIDE daily pages — typically 100-300+
# matches/day vs a league's ~10 — and every market key costs one browser tab
# per match page. Measured live (2026-06-12): ~73 s/match at 18 tabs and
# concurrency 3, i.e. multi-hour cycles; everything captured more than
# MAX_ODDS_AGE_SECONDS before a cycle ends is then discarded by the odds-age
# gate, so only the scrape's tail survives. Cap the per-sport market list
# whenever 'all' is configured: 4 tabs at concurrency 5 keeps a 300-400 match
# slate inside a ~30-minute freshness window.
ODDSPORTAL_ALL_LEAGUES_MARKET_BUDGET = 4


def _parse_market_map(raw: str, env_name: str) -> tuple[tuple[str, str], ...]:
    """Parse a csv of "market_key:value" into ordered (key, raw_value) pairs.

    Keys are lowercased; blanks are skipped; a malformed or duplicate entry
    is a fatal config error (fail fast at startup, like the pacing knobs).
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in (e.strip() for e in raw.split(",") if e.strip()):
        key, sep, value = entry.rpartition(":")
        key = key.strip().lower()
        value = value.strip()
        if not sep or not key or not value:
            raise ValueError(f"{env_name}: bad entry {entry!r} (expected 'market_key:value')")
        if key in seen:
            raise ValueError(f"{env_name}: duplicate market key {key!r}")
        seen.add(key)
        pairs.append((key, value))
    return tuple(pairs)


def parse_market_min_edges(raw: str) -> tuple[tuple[str, float], ...]:
    """VALUE_MIN_EDGE_PER_MARKET entries as (market_key, edge) pairs."""
    out: list[tuple[str, float]] = []
    for key, value in _parse_market_map(raw, "VALUE_MIN_EDGE_PER_MARKET"):
        try:
            edge = float(value)
        except ValueError:
            raise ValueError(
                f"VALUE_MIN_EDGE_PER_MARKET[{key}]: {value!r} is not a number"
            ) from None
        if not 0.0 < edge < 1.0:
            raise ValueError(f"VALUE_MIN_EDGE_PER_MARKET[{key}]={edge} must be in (0, 1)")
        out.append((key, edge))
    return tuple(out)


def parse_market_min_books(raw: str) -> tuple[tuple[str, int], ...]:
    """VALUE_MIN_BOOKS_PER_MARKET entries as (market_key, count) pairs."""
    out: list[tuple[str, int]] = []
    for key, value in _parse_market_map(raw, "VALUE_MIN_BOOKS_PER_MARKET"):
        try:
            count = int(value)
        except ValueError:
            raise ValueError(
                f"VALUE_MIN_BOOKS_PER_MARKET[{key}]: {value!r} is not an integer"
            ) from None
        if count < 1:
            raise ValueError(f"VALUE_MIN_BOOKS_PER_MARKET[{key}]={count} must be >= 1")
        out.append((key, count))
    return tuple(out)


def parse_odds_bands(raw: str) -> tuple[tuple[float, float], ...]:
    """VALUE_ODDS_BANDS csv ("lo-hi,...") as inclusive (lo, hi) band pairs."""
    bands: list[tuple[float, float]] = []
    for entry in (e.strip() for e in raw.split(",") if e.strip()):
        lo_s, sep, hi_s = entry.partition("-")
        try:
            lo, hi = float(lo_s), float(hi_s)
        except ValueError:
            raise ValueError(f"VALUE_ODDS_BANDS: bad band {entry!r} (expected 'lo-hi')") from None
        if not sep:
            raise ValueError(f"VALUE_ODDS_BANDS: bad band {entry!r} (expected 'lo-hi')")
        if not 1.0 < lo <= hi:
            raise ValueError(
                f"VALUE_ODDS_BANDS: band {entry!r} needs 1.0 < lo <= hi (decimal odds)"
            )
        bands.append((lo, hi))
    return tuple(bands)


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
    # Mirrors docker-compose.yml's host-side app bind. It does not configure
    # uvicorn inside the container; it exists so public Docker binds fail fast
    # unless dashboard auth is enabled.
    app_host_bind: str = "127.0.0.1"

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
    # How long an UNCHANGED market state stays suppressed by the alert
    # idempotency store. Open picks routinely live for days (taken up to
    # weeks before kickoff); a 24h TTL re-alerted every still-open same-odds
    # pick daily. A price move mints a new dedupe key (it includes
    # decimal_odds) and alerts immediately regardless of this TTL.
    alert_dedupe_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, ge=60)

    # --- Dashboard auth (optional) --------------------------------------------
    # OFF by default so dev/CI and fresh installs are never locked out. Enable
    # in .env (gitignored, 0600) by setting enabled + a PBKDF2 hash + a random
    # session secret. The plaintext password NEVER lives in tracked code or
    # .env — only the salted hash (app.api.auth.hash_password). /health stays
    # public for the compose healthcheck and external watchdog.
    dashboard_auth_enabled: bool = False
    dashboard_auth_username: str = "admin"
    dashboard_auth_password_hash: str = ""  # "pbkdf2_sha256$iters$salt$hash"
    dashboard_session_secret: str = ""  # HMAC key for the session cookie
    dashboard_session_ttl_seconds: int = Field(default=12 * 60 * 60, ge=60)

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
    # Volume tier (informational shadow tier): candidates whose edge clears
    # this floor but NOT value_min_edge are persisted with tier='volume' —
    # no alert, no daily-exposure reservation — purely to accumulate live
    # CLV evidence at scale. Validation: v2 holdout n=379, CLV +0.019
    # (premium tier above: v4 holdout n=61, ROI +21.1%, CLV +0.106).
    # Must stay <= value_min_edge; setting it EQUAL disables the volume
    # tier cleanly (no edge can be >= volume and < premium at once).
    value_volume_min_edge: float = Field(default=0.015, ge=0.0)
    # User policy: never pick odds below 1.60. The backtest validated at
    # >= 1.30; a higher floor only narrows to a subset of validated picks.
    value_min_odds: float = 1.60
    value_devig: str = "differential_margin_weighting"  # any DevigMethod value
    # --- ML value filter (meta-labeling SECONDARY model — OFF by default) ----
    # Scores value CANDIDATES (never match outcomes — ML winner-prediction
    # backtested negative and is forbidden as a strategy). One-shot held-out
    # evidence (seasons 2425+2526, scripts/ml/train_value_filter.py --final,
    # docs/research/ml-value-filter.md): META q>=0.725 selected n=396 bets,
    # ROI +12.0% (boot CI [-1.6%, +26.7%]), incremental CLV vs the vig-free
    # Max-of-books close +0.0357 ± 0.0075 (2SE) — beating the thr=0 null,
    # the per-(league,market) threshold control (+0.0082), and the volume
    # baseline; all four pre-registered adoption criteria passed (ADOPT).
    # OFF: scores are still annotated on picks whenever the artifact loads
    # (shadow evidence + dashboard display) but never change behavior.
    # ON: premium candidates scoring BELOW the manifest's frozen operating
    # point are demoted to the volume (shadow) tier — no alert, no exposure
    # reservation; out-of-scope candidates always pass through unfiltered.
    value_ml_filter: bool = False
    # Directory holding value_filter_manifest.json + value_filter_model.txt
    # (gitignored; produced by the trainer, copied to the host at deploy —
    # docs/deployment/openclaw-ubuntu.md). Missing artifacts = no scoring.
    value_ml_model_dir: str = "data/ml"
    # Which manifest/model files inside VALUE_ML_MODEL_DIR the loader reads.
    # Defaults = the v1 ADOPT artifacts. The v2 retrain (scripts/ml/
    # train_value_filter_v2.py, docs/research/premium-tier-v2.md) writes
    # value_filter_manifest_v2.json + value_filter_model_v2.txt with verdict
    # "CANDIDATE": seasons 2425+2526 are SPENT as a holdout, so v2 cannot
    # honestly claim ADOPT until live shadow CLV + the one-shot fresh 2627
    # season decide. The default therefore stays v1 — pointing these at the
    # v2 files requires VALUE_ML_MANIFEST_ALLOW_SHADOW=true below.
    value_ml_manifest_filename: str = "value_filter_manifest.json"
    value_ml_model_filename: str = "value_filter_model.txt"
    # Allow loading a manifest whose verdict is NOT "ADOPT" (e.g. the v2
    # SHADOW-CANDIDATE) for ANNOTATION-ONLY scoring: picks carry the score
    # for dashboards + live-shadow CLV stratification, but demotion never
    # happens — both app/pipeline.py and app/scheduler.py refuse to enforce
    # on a shadow model even if VALUE_ML_FILTER=true. Enforcement always
    # requires a true ADOPT manifest. Default OFF.
    value_ml_manifest_allow_shadow: bool = False

    # --- Premium-tier adjustment knobs (2026-06 strategy research) -----------
    # ALL default OFF (empty/None = exactly the validated global-threshold
    # behavior). Each knob exists so the existing harness can evaluate it
    # later; NONE may be enabled without nested season-blocked walk-forward
    # evidence WITHIN train seasons <= 2324 or a never-consulted fresh domain
    # (AH market, never-loaded divisions) — seasons 2425+2526 are SPENT as a
    # holdout (.claude/memory/decisions.md; docs/backtesting/value-findings.md;
    # docs/research/ml-value-filter.md). The binding verdict for any setting
    # chosen there is live shadow CLV + the one-shot fresh 2627 season.
    #
    # Per-market PREMIUM min-edge overrides, csv of "market_key:edge" — e.g.
    # "1x2:0.04,over_under_2_5:0.035,asian_handicap_-1_5:0.05". Keys are the
    # line-qualified source market (OddsSnapshotIn.market_detail, the same
    # keys the dashboard per-market counters show) or the market family
    # ("h2h", "totals", ...); most specific wins; unlisted markets keep
    # VALUE_MIN_EDGE. One mapping env var instead of VALUE_MIN_EDGE_1X2-style
    # suffixes because line-qualified keys ("asian_handicap_-1_5") are not
    # legal env-var fragments and new markets must not need code changes.
    value_min_edge_per_market: str = ""
    # Odds-band gate refinement, csv of "lo-hi" inclusive RAW-odds bands —
    # e.g. "1.8-2.6" or "1.6-2.4,3.0-4.2". Empty = only the VALUE_MIN_ODDS
    # floor (current behavior). FLB literature establishes margin is loaded
    # onto longshots; WHICH bands are structurally soft at soft books is
    # practitioner folklore — bands must be learned on nested CV (<= 2324),
    # never asserted (research item #1/FLB, rank #5).
    value_odds_bands: str = ""
    # Market-expansion scaffolding: per-market minimum distinct-bookmaker
    # count, csv of "market_key:count" — e.g. "over_under_1_5:5". A market
    # quoted by fewer books is skipped entirely (thin-liquidity proxy for
    # new lines/divisions). Empty = no floor anywhere (current behavior).
    value_min_books_per_market: str = ""

    # --- Optional drawdown-constrained staking (default OFF) -----------------
    # Both set => Kelly multiplier = min(FRACTIONAL_KELLY, lambda*) where
    # lambda* solves Pr(drawdown > STAKE_MAX_DRAWDOWN) <=
    # STAKE_MAX_DRAWDOWN_PROBABILITY (Busseti-Boyd 2016 single-bet closed
    # form — app/risk/staking.py). Staking never changes per-bet yield; this
    # shapes bankroll growth/drawdown only, and stakes stay informational
    # (picks-only platform, ADR-0002). Phase-6 decision; do not enable
    # without the evidence protocol above.
    stake_max_drawdown: float | None = Field(default=None, gt=0.0, lt=1.0)
    stake_max_drawdown_probability: float | None = Field(default=None, gt=0.0, lt=1.0)

    # --- Odds sources (read-only access) -----------------------------------------
    # "oddsportal" = free OddsPortal odds via OddsHarvester (default, no key);
    # "odds_api"   = The Odds API (needs keys below).
    odds_source: str = "oddsportal"
    oddsportal_football_leagues: str = "england-premier-league"  # csv of slugs
    # Devig-sound markets only: full mutually-exclusive outcome sets. Asian
    # handicaps are HALF-LINES only (integer/quarter lines carry pushes and
    # are rejected by the loader); European handicap is 3-way and devig-sound
    # at any integer line. 1x2+ou25 are backtest-validated; the rest use the
    # identical mechanism on thinner evidence. Every extra market adds one
    # scrape tab per match — each line below is a deliberate liquidity call.
    #
    # This default is every devig-sound market FAMILY oddsharvester 0.3.0
    # supports, with lines trimmed to the liquid band: football OU 0.5 and
    # 5.5+ plus EH ±3/±4 are dead-liquidity tabs that only add scrape time
    # and gap-noise. FULL upstream-supported sets for future tuning
    # (.venv/.../oddsharvester/utils/sport_market_constants.py is the
    # documentation of record):
    #   OU (FootballOverUnderMarket): 0_5..8_5 in quarter steps — only the
    #     half-lines (_5) are devig-sound; integer/quarter lines push.
    #   AH (FootballAsianHandicapMarket): -4..+2 in quarter steps — only
    #     half-lines (-3_5..+1_5) are devig-sound here.
    #   EH (FootballEuropeanHandicapMarket): -4..-1, +1..+4 (all integer,
    #     all 3-way devig-sound).
    oddsportal_football_markets: str = (
        "1x2,btts,double_chance,dnb,"
        "over_under_1_5,over_under_2_5,over_under_3_5,over_under_4_5,"
        "asian_handicap_-3_5,asian_handicap_-2_5,asian_handicap_-1_5,"
        "asian_handicap_-0_5,asian_handicap_+0_5,asian_handicap_+1_5,"
        "european_handicap_-2,european_handicap_-1,european_handicap_+1,european_handicap_+2"
    )
    # Basketball (club competitions only — OddsHarvester maps no national-team
    # events like EuroBasket). Empty leagues = basketball polling off.
    # Totals/AH lines are per-game; the band covers modern NBA/Euroleague
    # totals (200.5-245.5) and spreads (±10.5). FULL upstream sets are
    # 161 OU tabs (BasketballOverUnderMarket: 100_5..260_5, every half
    # point) and 52 AH tabs (BasketballAsianHandicapMarket: -25_5..+25_5,
    # all half-lines) — scraping them all only adds cycle time on tabs
    # OddsPortal rarely prices.
    oddsportal_basketball_markets: str = (
        "home_away,"
        "over_under_games_200_5,over_under_games_205_5,over_under_games_210_5,"
        "over_under_games_215_5,over_under_games_220_5,over_under_games_225_5,"
        "over_under_games_230_5,over_under_games_235_5,over_under_games_240_5,"
        "over_under_games_245_5,"
        "asian_handicap_games_-10_5_games,asian_handicap_games_-7_5_games,"
        "asian_handicap_games_-5_5_games,asian_handicap_games_-3_5_games,"
        "asian_handicap_games_-1_5_games,asian_handicap_games_+1_5_games,"
        "asian_handicap_games_+3_5_games,asian_handicap_games_+5_5_games,"
        "asian_handicap_games_+7_5_games,asian_handicap_games_+10_5_games"
    )
    oddsportal_basketball_leagues: str = "nba,euroleague"
    # --- Tennis (VISIBILITY-ONLY / UNVALIDATED) ------------------------------
    # OddsHarvester 0.3.0 scrapes tennis (151 ATP/WTA league URLs) and the
    # loader now ingests the devig-sound tennis markets below. BUT tennis is
    # NOT an alerting sport: the held-out value backtest
    # (scripts/sports/tennis_backtest.py) could not clear the doctrine gate.
    # (Leak-corrected 2026-06-16: tennis-data.co.uk lists odds winner-first, so
    # the loader now randomizes side order — see assign_sides. Leak-free held-out
    # ROI is positive but NOT conclusive — ATP +4.9% [-1.4%,+10.5%], WTA +3.1%
    # [-2.7%,+8.9%], both CIs crossing 0.) tennis-data.co.uk carries no
    # Pinnacle/Max closing columns, so
    # incremental CLV vs the close is UNDEFINED for tennis and the >2 SE bar
    # cannot even be evaluated. Tennis therefore enters as VISIBILITY-ONLY:
    # scraped rows appear in the AVAILABLE GAMES view tagged unvalidated=true,
    # and the pipeline mints NO picks and sends NO alerts for it (enforced in
    # app/scheduler.py + app/pipeline.py, not just by config).
    #
    # Empty leagues = tennis polling OFF (the default). It is OFF because a
    # third sport across 151 leagues materially grows cycle time; enable in
    # .env only when the operator wants the unvalidated visibility feed.
    # Markets are the devig-sound tennis set: match_winner (2-way ML), totals
    # half-lines on BOTH axes (sets _5 and games _5), and AH HALF-lines on
    # both axes (integer/zero AH lines push and are rejected by the loader;
    # correct_score is many-outcome and has no pairwise devig path).
    oddsportal_tennis_leagues: str = ""  # csv of atp-/wta- slugs; empty = OFF
    oddsportal_tennis_markets: str = (
        "match_winner,"
        "over_under_sets_2_5,over_under_sets_3_5,"
        "over_under_games_21_5,over_under_games_22_5,over_under_games_23_5,"
        "asian_handicap_-1_5_sets,asian_handicap_+1_5_sets,"
        "asian_handicap_-3_5_games,asian_handicap_-2_5_games,"
        "asian_handicap_+2_5_games,asian_handicap_+3_5_games"
    )
    # --- American football / NFL (REJECTED — no live code) -------------------
    # The loader supports NFL markets config-only (same home_away/over_under_/
    # asian_handicap_ keys as football/basketball), BUT the NFL value backtest
    # verdict is REJECT: there is no free source carrying both a sharp price
    # and a true closing line, so the sharp-vs-close CLV protocol is
    # impossible to run — NFL cannot be validated even as visibility-only with
    # honest provenance. Per doctrine a rejected sport gets NO live alerts;
    # we deliberately add NO NFL config flags and NO scheduler wiring so it
    # cannot be toggled on by accident. Revisit only if a free sharp+close
    # NFL odds source appears (docs/research/free-odds-sources.md).
    # Dated scraping: each cycle covers today..today+N (UTC) instead of a
    # league's whole upcoming list — far-future fixtures are skipped and
    # cycle time tracks the actionable slate. Unset = legacy upcoming page.
    oddsportal_days_ahead: int | None = 1
    # OddsHarvester's own pacing knobs (upstream README Disclaimer: "Use
    # responsibly and ensure compliance with their terms of service").
    # Concurrency = parallel match pages; request_delay = seconds between
    # requests (+ jitter upstream). Tuning these is sanctioned configuration
    # — anti-bot bypassing remains forbidden everywhere. Bounds fail fast at
    # startup: concurrency 0 becomes Semaphore(0) upstream (silent hang);
    # >5 or sub-0.5s delays exceed responsible pacing for a free source.
    oddsportal_concurrency: int = Field(default=3, ge=1, le=5)
    oddsportal_request_delay: float = Field(default=1.0, ge=0.5)
    # Browser locale, paired with the loader's forced UTC timezone for a
    # coherent human fingerprint (UTC = London -> en-GB).
    oddsportal_locale: str = "en-GB"
    # Seconds between poll cycles. With max_instances=1 + coalesce, a value
    # below the cycle duration just runs cycles back-to-back — effective
    # freshness is one cycle length; the scrape itself is the floor. The
    # >=30s floor blocks hammering-by-typo on a free scraped source.
    poll_interval_seconds: int = Field(default=300, ge=30)
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

    # --- Pinnacle arcadia sharp-line archive (read-only; opt-in, OFF) ---------
    # Clean-room GET-only capture of Pinnacle's PUBLIC guest JSON API
    # (guest.api.arcadia.pinnacle.com) — the free live-Pinnacle sharp anchor
    # this project's biggest documented data gap needs (ADR-0013,
    # docs/research/free-odds-sources.md). It runs as an INDEPENDENT capture job
    # ALONGSIDE the active ODDS_SOURCE; it never replaces it and mints NO picks/
    # alerts. Captured period-0 moneyline closes land under the isolated
    # `pinnacle_<sport>` warehouse namespace (bookmaker="Pinnacle"), so they
    # never pollute the live dashboard/pick path. ToS-grey + endpoint-fragile
    # (treat scrape gaps as expected). The public guest key is NOT an account
    # credential and is never a bookmaker login or order-placement path
    # (ADR-0002). OFF by default — a fourth read-only feed across many leagues.
    arcadia_enabled: bool = False
    arcadia_base_url: str = "https://guest.api.arcadia.pinnacle.com/0.1"
    # Public guest x-api-key (Pinnacle's own web-client constant). The endpoints
    # used here require NONE, so the default is empty and nothing is committed;
    # set in .env only if Pinnacle ever starts requiring it. Kept out of logs/
    # exceptions like every other key.
    arcadia_guest_key: str = ""
    # csv of sport keys to archive (soccer,tennis,basketball,american_football).
    arcadia_sports: str = "soccer,tennis,basketball"
    # Only archive events kicking off within this horizon (bounds volume; the
    # close is the last pre-kickoff observation regardless of horizon).
    arcadia_horizon_hours: int = Field(default=72, ge=1)
    # Capture cadence. Change-gated by Pinnacle's per-market version int, so a
    # short interval just tracks repricings; near kickoff is what matters. The
    # >=30s floor blocks hammering-by-typo on a free source.
    arcadia_poll_interval_seconds: int = Field(default=120, ge=30)
    # When true, the settlement-time snapshot close ALSO injects the STRICT
    # cross-source match's Pinnacle ARCHIVE close (app/resolution, ADR-0013), so
    # incremental CLV anchors on a real sharp close. OFF by default: it changes
    # anchor_type/CLV for matched picks, so enable only after validating the
    # match rate — the matcher is strict (no fuzzy), but a wrong close would
    # corrupt CLV. Requires ARCADIA_ENABLED so the archive exists to match.
    clv_use_pinnacle_archive: bool = False

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

    @model_validator(mode="after")
    def _enforce_dashboard_auth_config(self) -> "Settings":
        if self.dashboard_auth_enabled:
            missing = [
                name
                for name, value in (
                    ("DASHBOARD_AUTH_PASSWORD_HASH", self.dashboard_auth_password_hash),
                    ("DASHBOARD_SESSION_SECRET", self.dashboard_session_secret),
                )
                if not value
            ]
            if missing:
                raise ValueError("DASHBOARD_AUTH_ENABLED=true requires: " + ", ".join(missing))
            hash_parts = self.dashboard_auth_password_hash.split("$")
            if len(hash_parts) != 4 or hash_parts[0] != "pbkdf2_sha256":
                raise ValueError(
                    "DASHBOARD_AUTH_PASSWORD_HASH must look like "
                    "pbkdf2_sha256$iterations$salt$hash. In compose .env files, "
                    "wrap it in single quotes so Docker Compose does not "
                    "interpolate the $ separators."
                )
        bind = self.app_host_bind.strip().lower().strip("[]")
        loopback = bind == "localhost" or bind == "::1" or bind.startswith("127.")
        if not loopback and not self.dashboard_auth_enabled:
            raise ValueError(
                "APP_HOST_BIND exposes the dashboard outside loopback; set "
                "DASHBOARD_AUTH_ENABLED=true with DASHBOARD_AUTH_PASSWORD_HASH and "
                "DASHBOARD_SESSION_SECRET before binding the app publicly."
            )
        return self

    @model_validator(mode="after")
    def _enforce_all_leagues_market_budget(self) -> "Settings":
        # 'all' leagues + a wide market list = multi-hour cycles whose slate
        # the odds-age gate then almost entirely discards (see the budget
        # constant above). The trim is mandatory, not advisory — fail fast
        # like the pacing knobs do instead of scraping for hours and
        # picking nothing.
        for sport, leagues, markets in (
            ("FOOTBALL", self.oddsportal_football_leagues, self.oddsportal_football_markets),
            ("BASKETBALL", self.oddsportal_basketball_leagues, self.oddsportal_basketball_markets),
            ("TENNIS", self.oddsportal_tennis_leagues, self.oddsportal_tennis_markets),
        ):
            slugs = [s.strip() for s in leagues.split(",") if s.strip()]
            keys = [m.strip() for m in markets.split(",") if m.strip()]
            # ["all"] is the loader's exact league-less sentinel (a list
            # MIXING 'all' with slugs is not, and fails in the loader).
            if slugs == ["all"] and len(keys) > ODDSPORTAL_ALL_LEAGUES_MARKET_BUDGET:
                raise ValueError(
                    f"ODDSPORTAL_{sport}_LEAGUES=all scrapes the worldwide daily page "
                    f"(hundreds of matches; every market key adds one browser tab per "
                    f"match): {len(keys)} markets configured, budget is "
                    f"{ODDSPORTAL_ALL_LEAGUES_MARKET_BUDGET}. Trim "
                    f"ODDSPORTAL_{sport}_MARKETS or scope the leagues — at ~73s/match "
                    "with 18 tabs a cycle runs HOURS and the odds-age gate then "
                    "silently discards almost the whole slate."
                )
        return self

    @model_validator(mode="after")
    def _enforce_tier_ordering(self) -> "Settings":
        # The volume tier is a SUBSET extension below the premium threshold;
        # an inverted ordering would silently alert on unvalidated edges.
        if self.value_volume_min_edge > self.value_min_edge:
            raise ValueError(
                "VALUE_VOLUME_MIN_EDGE must be <= VALUE_MIN_EDGE "
                "(set them equal to disable the volume tier)."
            )
        return self

    @model_validator(mode="after")
    def _enforce_premium_adjustment_knobs(self) -> "Settings":
        # Default-off knobs must still fail FAST when set badly: a malformed
        # mapping or a contradictory band silently changing the pick flow is
        # exactly the failure mode the pacing validators exist to prevent.
        for key, edge in parse_market_min_edges(self.value_min_edge_per_market):
            if edge < self.value_volume_min_edge:
                raise ValueError(
                    f"VALUE_MIN_EDGE_PER_MARKET[{key}]={edge} is below "
                    f"VALUE_VOLUME_MIN_EDGE={self.value_volume_min_edge} — a per-market "
                    "premium floor under the volume floor inverts the tiers."
                )
        parse_market_min_books(self.value_min_books_per_market)
        for lo, hi in parse_odds_bands(self.value_odds_bands):
            if hi < self.value_min_odds:
                raise ValueError(
                    f"VALUE_ODDS_BANDS band {lo}-{hi} sits entirely below "
                    f"VALUE_MIN_ODDS={self.value_min_odds} — it can never match."
                )
        if (self.stake_max_drawdown is None) != (self.stake_max_drawdown_probability is None):
            raise ValueError(
                "STAKE_MAX_DRAWDOWN and STAKE_MAX_DRAWDOWN_PROBABILITY define one "
                "constraint — set both or neither."
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
        # Optional drawdown constraint (default None/None = the plain
        # 0.25x/2% path, numerically unchanged) — see Settings comments.
        max_drawdown=settings.stake_max_drawdown,
        max_drawdown_probability=settings.stake_max_drawdown_probability,
    )


def value_policy(settings: Settings) -> ValuePolicy:
    """Optional value-gate refinements; the default (empty) Settings knobs
    build the all-empty no-op policy — current live behavior, untouched."""
    return ValuePolicy(
        min_edge_by_market=parse_market_min_edges(settings.value_min_edge_per_market),
        odds_bands=parse_odds_bands(settings.value_odds_bands),
        min_books_by_market=parse_market_min_books(settings.value_min_books_per_market),
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
