"""Application settings — the ONLY module that reads the environment.

SAFETY (ADR-0002): this platform is decision-support only. The validator
below turns any attempt to enable betting execution into a fatal startup
error. There is no code anywhere that reads these flags to enable anything.
"""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.edge.gates import GatePolicy
from app.edge.steam import SteamPolicy
from app.edge.value_policy import ValuePolicy
from app.ingestion.base import ScraperProxy
from app.probabilities.devig import DevigMethod
from app.risk.exposure import DailyExposureLedger
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


def parse_market_devig(raw: str) -> tuple[tuple[str, DevigMethod], ...]:
    """VALUE_DEVIG_PER_MARKET entries as (market_key, DevigMethod) pairs.

    Fails FAST (like the other premium knobs) on a method name that is not one
    of the 8 known DevigMethod values, so a typo in .env can never silently
    fall through to the global method on the affected markets."""
    out: list[tuple[str, DevigMethod]] = []
    for key, value in _parse_market_map(raw, "VALUE_DEVIG_PER_MARKET"):
        try:
            method = DevigMethod(value)
        except ValueError:
            valid = ", ".join(m.value for m in DevigMethod)
            raise ValueError(
                f"VALUE_DEVIG_PER_MARKET[{key}]: {value!r} is not a known devig method "
                f"(valid: {valid})"
            ) from None
        out.append((key, method))
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


def parse_major_leagues(raw: str) -> tuple[str, ...]:
    """VALUE_MAJOR_LEAGUES csv of scraped league names for the PREMIUM tier.

    Empty = gate disabled (the no-op default). Names are kept as given (the
    scrape's ``league_name``) and normalized only at compare time by
    app/edge/value_policy.is_major_league; blank entries are dropped.
    """
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def parse_proxy_urls(raw: str, env_name: str = "ARCADIA_PROXY_URLS") -> tuple[str, ...]:
    """Parse comma/newline separated proxy URLs without ever echoing secrets."""
    urls: list[str] = []
    seen: set[str] = set()
    for idx, entry in enumerate(raw.replace("\n", ",").split(","), start=1):
        url = entry.strip()
        if not url:
            continue
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
            raise ValueError(
                f"{env_name}: proxy entry #{idx} must be http(s)://user:pass@host:port"
            )
        if url in seen:
            raise ValueError(f"{env_name}: duplicate proxy entry #{idx}")
        seen.add(url)
        urls.append(url)
    return tuple(urls)


def parse_scraper_proxy_pool(
    raw: str, env_name: str = "SCRAPER_PROXY_POOL"
) -> tuple[ScraperProxy, ...]:
    """Parse comma-separated ``host|port|user|pass`` quads into frozen proxy
    entries. Never echoes credentials in errors (index/shape only)."""
    out: list[ScraperProxy] = []
    seen: set[str] = set()
    for idx, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) != 4 or not all(p.strip() for p in parts):
            raise ValueError(f"{env_name}: entry #{idx} must be 'host|port|user|pass'")
        host, port, user, pwd = (p.strip() for p in parts)
        if not port.isdigit():
            raise ValueError(f"{env_name}: entry #{idx} port must be numeric")
        url = f"http://{host}:{port}"
        if url in seen:
            raise ValueError(f"{env_name}: duplicate proxy host:port at #{idx}")
        seen.add(url)
        out.append(ScraperProxy(url=url, username=user, password=pwd))
    return tuple(out)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
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
    # Bounds-validated (audit #1): a fat-fingered .env (e.g. 5 read as 500%)
    # must NOT silently inflate recommended stakes past the safety envelope.
    bankroll_base: float = Field(default=1000.0, gt=0.0)
    fractional_kelly: float = Field(default=0.25, gt=0.0, le=1.0)
    max_recommended_stake_percent: float = Field(default=0.02, gt=0.0, le=1.0)
    max_daily_exposure_percent: float = Field(default=0.05, gt=0.0, le=1.0)
    # Per-event correlation backstop (Kelly assumes INDEPENDENT bets; multiple
    # +EV selections on the same event_id are correlated, so their COMBINED
    # recommended exposure is bounded). Default-conservative: ON, capped at 2x
    # the per-bet cap so a single full-cap pick always fits but two correlated
    # selections on one match can never jointly exceed it. Disable by setting
    # EVENT_EXPOSURE_CAP_ENABLED=false (then only the daily cap binds).
    event_exposure_cap_enabled: bool = True
    max_event_exposure_percent: float = Field(default=0.04, gt=0.0, le=1.0)

    # --- Alerts ----------------------------------------------------------------
    # SecretStr (audit #3): repr-redacted so a whole-Settings log/serialize can
    # never emit them in cleartext. telegram_bot_token rides the URL path;
    # webhook_url may embed credentials.
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    webhook_url: SecretStr = SecretStr("")
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
    dashboard_auth_password_hash: SecretStr = SecretStr("")  # pbkdf2_sha256$iters$salt$hash
    dashboard_session_secret: SecretStr = SecretStr("")  # HMAC key for the session cookie
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
    # Upper sanity ceiling on edge (data-error guard). A value edge above this on
    # a liquid market is a corrupted/mislabeled anchor (e.g. a swapped 1X2 feed),
    # never real value — the value scan rejects it so a feed defect can't mint a
    # phantom +EV pick. Must stay > value_min_edge. Soccer-appropriate at 0.20.
    value_max_edge: float = Field(default=0.20, gt=0.0)
    # Volume tier (informational shadow tier): candidates whose edge clears
    # this floor but NOT value_min_edge are persisted with tier='volume' —
    # no alert, no daily-exposure reservation — purely to accumulate live
    # CLV evidence at scale. Validation: v2 holdout n=379, CLV +0.019
    # (premium tier above: v4 holdout n=61, ROI +21.1%, CLV +0.106).
    # Must stay <= value_min_edge; setting it EQUAL disables the volume
    # tier cleanly (no edge can be >= volume and < premium at once).
    value_volume_min_edge: float = Field(default=0.015, ge=0.0)
    # Floor on candidate odds, set to the VALIDATED floor (1.30 = the engine
    # default). A 2026-06-18 held-out floor sweep showed 1.60->1.30 adds ~1
    # pick over two seasons with ROI/CLV unchanged (high-edge sub-1.60 value
    # bets barely exist), while dropping below 1.30 only admits the noisy
    # short-odds region (net-negative at the no-threshold baseline). So 1.30
    # captures all premium upside and guards the noise; raise in .env to be
    # more conservative. (2026-06-18 floor sweep; see .claude/memory/decisions.md.)
    value_min_odds: float = 1.30
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
    # Major-league PREMIUM gate, csv of scraped league names as OddsPortal emits
    # them (the per-event league_name), e.g. "Premier League,LaLiga,Serie A,
    # Bundesliga,Ligue 1,UEFA Champions League,NBA,EuroLeague". Empty = gate
    # DISABLED (every league premium-eligible — current behavior). When set, a
    # premium candidate whose scraped league is not in the set is DEMOTED to the
    # volume (shadow) tier: persisted + CLV-tracked, never alerted, never
    # reserving exposure. The honest-high-ROI lever — concentrate alerts +
    # exposure on leagues with real sharp-anchor coverage + liquidity (majors);
    # obscure slates have no free sharp close (~37% coverage is structural, see
    # .claude/memory/pitfalls.md 2026-06-20). Names normalized at compare time
    # (accents/case/spacing); curate from the per-league match-rate report.
    value_major_leagues: str = ""
    # Require-sharp-anchor PREMIUM gate (default False = DISABLED, current
    # behavior). When True, a premium candidate whose fair value came from the
    # soft consensus(median) — i.e. NO genuine sharp book (Pinnacle or Betfair)
    # priced the full market — is DEMOTED to the volume (shadow) tier: persisted
    # + CLV-tracked, never alerted, never reserving exposure. The season-proof,
    # name-proof sibling of VALUE_MAJOR_LEAGUES — it scopes premium by DATA (a
    # sharp anchor actually backed the price) rather than by curated league name,
    # so it stops obscure-league bleed (~37% sharp coverage is structural, see
    # .claude/memory/pitfalls.md 2026-06-20) without any per-season list upkeep.
    value_require_sharp_anchor: bool = False
    # Per-market-type devig override (ADR-0006), csv of "market_key:method" —
    # e.g. "over_under_2_5:probit,asian_handicap_-1_5:probit,1x2:shin". Keys are
    # the line-qualified source market (market_detail) or the market family
    # ("h2h","totals",...); most specific wins; unlisted markets keep the global
    # VALUE_DEVIG. Method names are validated against the 8 DevigMethod values at
    # startup (a typo fails fast). Empty = DISABLED — every market devigs with
    # VALUE_DEVIG (current behavior, the non-breaking default). The same override
    # flows to the CLV true-up + settlement close pricing so fill and close are
    # ALWAYS devigged with the identical per-market method (no CLV method-mix).
    value_devig_per_market: str = ""
    # CONSENSUS-fallback anchor as a log-odds (logit) POOL across full-market
    # books instead of the median-of-prices consensus. False = median (current
    # behavior, the non-breaking default). SCOPE: only the consensus fallback
    # fair value changes (no genuine sharp book priced the market); with
    # VALUE_REQUIRE_SHARP_ANCHOR=true those picks are already volume-tier, so
    # this sharpens the SHADOW tier's fair value + consensus-vs-median
    # comparisons, NOT premium pricing. (build #1 — app/edge/value.py.)
    value_consensus_logit_pool: bool = False

    # --- Line-movement / steam-awareness gate (app/edge/steam.py) ------------
    # Guards the dominant soft-book FALSE POSITIVE: a phantom edge from a moving
    # market read on a single snapshot — the soft price has already CONVERGED on
    # the anchor (edge correcting/evaporating) or the sharp anchor is STALE (last
    # seen beyond the freshness window). Default False = SHADOW: the per-candidate
    # verdict is computed + logged but the tier is UNCHANGED, so its effect on
    # real picks is measured before it enforces. True = ENFORCE: a tripped premium
    # candidate is DEMOTED to volume (shadow) — persisted + CLV-tracked, never
    # alerted — exactly like the other built-but-off premium gates (never a silent
    # drop). Enable only after the shadow logs show it would not bleed live edge.
    value_steam_gate_enabled: bool = False
    # Trajectory window the gate consults (seconds). Bounds the per-book history.
    value_steam_lookback_seconds: float = Field(default=21600.0, gt=0.0)  # 6h
    # Min in-window observations of the FILL book before any movement judgement.
    value_steam_min_points: int = Field(default=2, ge=2)
    # Convergence trip: fraction of the ORIGINAL fill-vs-anchor gap already closed.
    value_steam_close_frac: float = Field(default=0.5, gt=0.0, le=1.0)
    # Opening gap (prob units) below which there was no edge to close (suppresses
    # the convergence signal — avoids dividing a negligible gap).
    value_steam_min_initial_gap: float = Field(default=0.01, ge=0.0)
    # Stale-anchor trip: anchor's most-recent observation no older than this (s).
    value_steam_anchor_staleness_seconds: float = Field(default=7200.0, gt=0.0)  # 2h
    # Soft-steamed-away FLAG threshold (prob units the fill implied dropped).
    value_steam_soft_steam_away_delta: float = Field(default=0.04, gt=0.0)
    # Whether a soft-steamed-away flag also TRIPS the gate (default: flag only).
    value_steam_demote_on_soft_steam: bool = False

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
    # Default "all" = OddsPortal's worldwide dated daily page (every league
    # that day, no slug filter); off-season leagues simply yield no events.
    # Matches the reference deployment so a fresh committed deploy is wide.
    oddsportal_football_leagues: str = "all"  # csv of slugs, or "all" sentinel
    # Devig-sound markets only. With leagues="all" (the default) the budget
    # validator caps this at ODDSPORTAL_ALL_LEAGUES_MARKET_BUDGET (4) keys —
    # every key is one browser tab per match on a 100-300+ match daily page,
    # so the worldwide scrape stays under an hour. This 4-key set is the
    # backtest-validated 1x2+ou25 plus btts/double_chance, matching the
    # reference deployment.
    #
    # To run the FULL devig-sound families instead, SCOPE the leagues (set
    # ODDSPORTAL_FOOTBALL_LEAGUES to specific slugs, not "all") and widen this
    # in .env. Asian handicaps are HALF-LINES only (integer/quarter lines push
    # and are loader-rejected); European handicap is 3-way devig-sound at any
    # integer line. FULL upstream-supported sets (oddsharvester 0.3.0
    # utils/sport_market_constants.py is the documentation of record):
    #   OU (FootballOverUnderMarket): 0_5..8_5 in quarter steps — only the
    #     half-lines (_5) are devig-sound; integer/quarter lines push.
    #   AH (FootballAsianHandicapMarket): -4..+2 in quarter steps — only
    #     half-lines (-3_5..+1_5) are devig-sound here.
    #   EH (FootballEuropeanHandicapMarket): -4..-1, +1..+4 (all integer,
    #     all 3-way devig-sound).
    oddsportal_football_markets: str = "1x2,over_under_2_5,btts,double_chance"
    # Basketball (club competitions only — OddsHarvester maps no national-team
    # events like EuroBasket). With leagues="all" the budget validator caps
    # markets at 4 keys. On the JSON feed (ODDSPORTAL_USE_JSON_FEED) the
    # over_under_games / asian_handicap_games WILDCARDS each fetch EVERY priced
    # half-line of their betType in ONE GET (the whole ladder rides one feed body
    # — no per-line render cost), so the value engine can shop any line and a
    # game's totals are captured whatever its scoring band (a fixed 220.5-230.5
    # set missed every WNBA/college ~165 game). moneyline + all totals + all
    # handicaps = 3 keys, under the cap. SCOPE the leagues in .env to widen.
    oddsportal_basketball_markets: str = "home_away,over_under_games,asian_handicap_games"
    # Default "all" = worldwide basketball daily page; off-season (NBA and
    # Euroleague both done) simply yields no events. Matches the reference
    # deployment. Empty leagues = basketball polling off.
    oddsportal_basketball_leagues: str = "all"
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
    # Default = the current in-season slugs (grass), matching the reference
    # deployment so a fresh deploy shows the same visibility feed. These slugs
    # are SEASONAL — rotate them in .env as the tour moves (grass->hard->clay);
    # set empty to turn tennis polling OFF entirely. Market is match_winner
    # only (the 2-way ML, devig-sound) to keep the multi-tournament scrape
    # light; the full devig-sound tennis set (totals/AH half-lines on both the
    # sets and games axes) can be set in .env for scoped tuning.
    oddsportal_tennis_leagues: str = (
        "atp-halle,atp-london,atp-eastbourne,atp-mallorca,atp-wimbledon,"
        "wta-berlin,wta-birmingham,wta-eastbourne,wta-bad-homburg,wta-wimbledon"
    )  # csv of atp-/wta- slugs (seasonal); empty = OFF
    oddsportal_tennis_markets: str = "match_winner,over_under_sets_2_5"
    # --- American football / NFL (VISIBILITY-ONLY / UNVALIDATED) -------------
    # Mirrors tennis: NFL is scraped and shown in AVAILABLE GAMES tagged
    # unvalidated, and the pipeline mints NO picks/alerts for it — enforced in
    # app/scheduler.py (visibility_only_sports) AND the warehouse path
    # (_VALIDATED_SPORT_PREFIXES omits it). It earns alerts ONLY after a
    # held-out incremental-CLV-vs-close backtest clears the >2 SE bar.
    # The earlier REJECT ("no free source carries a sharp price + true close")
    # is now only PARTLY true: the read-only Pinnacle Arcadia archive
    # (american_football = sport-id 15, added to arcadia_sports below)
    # FORWARD-captures a free Pinnacle moneyline/totals/spread close for NFL —
    # so a CLV grade becomes possible once enough fixtures accrue AND the strict
    # cross-source matcher attaches them. Until then NFL stays visibility-only.
    # Leagues default "nfl" (the whole league); off-season the dated scrape
    # simply yields no events (same as basketball=all in summer). Set empty to
    # turn NFL polling OFF. Markets kept to home_away (moneyline) to keep the
    # visibility scrape light; widen in .env (loader supports over_under_/
    # asian_handicap_ keys). The market-budget guard engages ONLY when
    # leagues=all; with the scoped default slug it is uncapped but bounded by
    # that league's match volume (~1 browser tab per match per market key).
    oddsportal_nfl_leagues: str = "nfl,ncaa,cfl,ufl"  # csv american-football slugs; empty = OFF
    oddsportal_nfl_markets: str = "home_away"
    # Dated scraping: each cycle covers today..today+N (UTC) instead of a
    # league's whole upcoming list — far-future fixtures are skipped and
    # cycle time tracks the actionable slate. Unset = legacy upcoming page.
    oddsportal_days_ahead: int | None = 1
    # OddsHarvester's own pacing knobs (upstream README Disclaimer: "Use
    # responsibly and ensure compliance with their terms of service").
    # Concurrency = parallel match pages; request_delay = seconds between
    # requests (+ jitter upstream). Tuning these is sanctioned configuration
    # — anti-bot bypassing remains forbidden everywhere. Bounds fail fast at
    # startup: concurrency 0 becomes Semaphore(0) upstream (silent hang).
    # 5 is the single-IP pacing ceiling; ABOVE 5 needs >=1 proxy per concurrent
    # request (gated by _enforce_scrape_concurrency_within_proxy_budget, so each
    # GET exits a distinct IP). le=64 is an absolute sanity stop. Sub-0.5s delays
    # exceed responsible pacing for a free source.
    oddsportal_concurrency: int = Field(default=3, ge=1, le=64)
    oddsportal_request_delay: float = Field(default=1.0, ge=0.5)
    # OddsHarvester hardcodes a 15s match-page navigation (Page.goto) timeout
    # that is NOT env-configurable upstream; on OddsPortal's heavy pages a slow
    # load trips "Timeout 15000ms exceeded" and that one match is skipped (it is
    # re-scraped next cycle). Raise it at the OddsPortalLoader boundary so fewer
    # pages time out. Floor at 15000 (never LOWER the upstream default), cap at
    # 120000 so a typo can't make a single page stall a cycle. Increasing a
    # timeout is configuration, never an anti-bot bypass.
    scrape_nav_timeout_ms: int = Field(default=30000, ge=15000, le=120000)
    # HARD per-scrape-pass watchdog (seconds). A single hung OddsPortal
    # Over/Under extraction (PageScroller burns ~20s per missing sub-line, x52
    # across a slate) otherwise made a poll cycle run FOREVER — every later
    # interval slot then skipped ("max running instances reached") and
    # settle_results never ran (the cactusbets.cloud incident). Each
    # _scrape_with_failover pass (per date in fetch_odds, and the match-page
    # pass in fetch_match_odds) is bounded by this many seconds; on timeout the
    # hung scrape is CANCELLED and that pass is treated as empty (recovered next
    # cycle). Generous so a healthy worldwide slate finishes inside it, finite so
    # a wedge can't run unbounded. Floor 60s blocks a typo cutting healthy
    # cycles; cap 2h is the absolute sanity ceiling. PROD-SAFE WITH NO CONFIG —
    # the watchdog is ON by default. Only ever BOUNDS a read-only scrape; never
    # an anti-bot bypass.
    scrape_cycle_timeout_seconds: float = Field(default=900.0, ge=60.0, le=7200.0)
    # Browser locale, paired with the loader's forced UTC timezone for a
    # coherent human fingerprint (UTC = London -> en-GB).
    oddsportal_locale: str = "en-GB"
    # SELECTABLE per-match odds transport for the OddsPortal source. OFF by
    # default = the proven Playwright/OddsHarvester DOM scrape (unchanged). When
    # true, OddsPortalLoader fetches each match's ODDS via the curl_cffi JSON
    # feed (app/ingestion/oddsportal_json.py) — much faster + lighter. To make
    # the savings REAL, the dated LISTING then runs with NO markets (match URLs +
    # header team context only), so the expensive per-match Playwright odds
    # extraction is NEVER paid; the per-match odds come ONLY from curl_cffi.
    # There is NO Playwright odds fallback (operator instruction 2026-06-23): a
    # per-match JSON failure (decrypt / HTTP / envelope / version-guard) is logged
    # (type only) and the match is SKIPPED — a scrape gap, exactly like a benign
    # DOM miss. A JSON-wide key/bundle rotation fails CLOSED with a LOUD WARNING
    # (the version guard), never a wrong price. Numeric provider ids are mapped to
    # canonical bookmaker NAMES (a GET-only, cached registry); an unknown id is
    # skipped, never persisted numeric. Stays the Playwright DEFAULT until
    # prod-verified; the finished-SCORE capture path (score_only) is unaffected
    # (header-only read, not an odds feed). READ-ONLY GET-only either way.
    oddsportal_use_json_feed: bool = False
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

    odds_api_key: SecretStr = SecretStr("")  # SecretStr (audit #3): keys ride query strings
    odds_api_key_1: SecretStr = SecretStr("")
    odds_api_key_2: SecretStr = SecretStr("")
    odds_api_key_3: SecretStr = SecretStr("")
    # The Odds API regions to request (csv). "eu" carries Pinnacle AND Betfair
    # Exchange EU; add "uk" for Betfair Exchange UK too (betfair_ex_uk). More
    # regions = richer sharp coverage but more credits/request — widen only on a
    # paid/large free budget. Both Betfair variants fold to "betfair exchange"
    # (app/ingestion/odds_api._BOOK_CANONICAL) so they anchor CLV like Pinnacle.
    odds_api_regions: str = "eu"

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
    # (ADR-0002). ON by default — a fourth READ-ONLY feed that builds the free
    # live-Pinnacle sharp-line ARCHIVE (the irreplaceable line-shopping anchor).
    # GET-only; still mints NO picks/alerts. CLV_USE_PINNACLE_ARCHIVE stays OFF
    # until the cross-source match rate is validated. Set false to disable.
    arcadia_enabled: bool = True
    arcadia_base_url: str = "https://guest.api.arcadia.pinnacle.com/0.1"
    # Public guest x-api-key (Pinnacle's own web-client constant). The endpoints
    # used here require NONE, so the default is empty and nothing is committed;
    # set in .env only if Pinnacle ever starts requiring it. Kept out of logs/
    # exceptions like every other key.
    arcadia_guest_key: SecretStr = SecretStr("")
    # Best-effort PUBLIC key/base discovery (read-only GET of pinnacle.com's
    # web-client config blob, /config/app.json) — refreshes the guest key + base
    # URL if Pinnacle ever rotates them, hardening reliability. DEFAULT False so
    # the current path is byte-identical unless opted in; on ANY failure the
    # composition root falls back to ARCADIA_GUEST_KEY + ARCADIA_BASE_URL. The
    # discovered key is a PUBLIC web-client constant (authenticates no user) and
    # is handled as a SecretStr — never logged, committed, or put in an error.
    arcadia_discover_config: bool = False
    # csv of sport keys to archive (soccer,tennis,basketball,american_football).
    # american_football (sport-id 15) included so NFL's free Pinnacle ML/totals/
    # spread CLOSE is forward-captured — the prerequisite for ever CLV-grading
    # NFL picks (off-season the upstream simply returns no events).
    arcadia_sports: str = "soccer,tennis,basketball,american_football"
    # Only archive events kicking off within this horizon (bounds volume; the
    # close is the last pre-kickoff observation regardless of horizon).
    arcadia_horizon_hours: int = Field(default=72, ge=1)
    # Capture cadence. Change-gated by Pinnacle's per-market version int, so a
    # short interval just tracks repricings; near kickoff is what matters. The
    # >=30s floor blocks hammering-by-typo on a free source.
    arcadia_poll_interval_seconds: int = Field(default=120, ge=30)
    # Optional outbound proxies for Arcadia/Pinnacle archive capture only.
    # Store real values in .env as comma-separated http(s) proxy URLs:
    # http://user:pass@host:port,http://user:pass@host:port
    # The parser deliberately never echoes the configured URLs in validation
    # errors because proxy usernames/passwords are secrets.
    arcadia_proxy_urls: SecretStr = SecretStr("")
    # Optional rotating proxy pool for the LIVE OddsPortal scrape (read-only GET).
    # Empty = scrape from the host IP. Format: comma-separated host|port|user|pass
    # quads. SecretStr so the credentials stay out of logs/repr.
    scraper_proxy_pool: SecretStr = SecretStr("")
    # When true, the settlement-time snapshot close ALSO injects the STRICT
    # cross-source match's Pinnacle ARCHIVE close (app/resolution, ADR-0013), so
    # incremental CLV anchors on a real sharp close. OFF by default: it changes
    # anchor_type/CLV for matched picks, so enable only after validating the
    # match rate — the matcher is strict (no fuzzy), but a wrong close would
    # corrupt CLV. Requires ARCADIA_ENABLED so the archive exists to match.
    clv_use_pinnacle_archive: bool = False

    # --- Betfair Exchange BACK-odds capture (read-only; opt-in, OFF) ----------
    # Dedicated ISOLATED reader of OddsPortal's Betfair Exchange BACK/LAY row
    # (ADR-0015). OddsPortal serves a live Betfair Exchange row on
    # liquidity-rich (major) matches that OddsHarvester's main-table parser
    # skips; this captures its BACK side into the isolated `betfair_<sport>`
    # warehouse namespace (bookmaker="Betfair Exchange"). It mirrors the arcadia
    # archive exactly: an INDEPENDENT capture that runs ALONGSIDE the active
    # ODDS_SOURCE, mints NO picks/alerts, and never touches the live dashboard/
    # pick path. v1 is the ENABLER only (like arcadia) — "betfair exchange" is
    # already a SHARP_BOOK with EXCHANGE_COMMISSION in app/edge/value.py, but
    # nothing in v1 consumes these rows for picks.
    #
    # OFF by default (unlike arcadia): the reader spends a full browser page-load
    # per match, so it stays opt-in until its target slate is scoped. GET-only,
    # read-only — the page is loaded the same way the OddsPortal scrape already
    # loads it; NO Betfair API/login/session/order path exists (ADR-0002), so
    # there are deliberately no BETFAIR_* credential slots.
    betfair_exchange_enabled: bool = False
    # Backable £ liquidity floor: a BACK outcome whose displayed liquidity is
    # below this is SKIPPED (only £0/dust markets give unusable exchange prices).
    # The OLD 500.0 default was calibrated on a SINGLE major-match probe
    # (2026-06-19) and silently dropped every obscure market — only 22
    # betfair_soccer events were EVER captured. Live re-probe (2026-06-23) of the
    # U20/lower-division/friendly pages the operator confirmed carry Betfair odds
    # showed genuine small-market liquidity of £12-£23 per BACK outcome; that is
    # normal for a small exchange market, not a closed one. The floor is lowered
    # to admit those real prices while still gating £0 dust + the '0' empty-cell
    # sentinel (which parses to <=1.0 and is dropped upstream regardless).
    # Floored at 0 (0 = no gate).
    betfair_exchange_min_liquidity: float = Field(default=10.0, ge=0.0)
    # OFF by default: when True the Betfair reader extracts row tokens from the
    # rendered section HTML via the unit-testable bs4 parser instead of the in-page
    # JS (identical output, validated against a real fixture). The proven JS path
    # stays the default; this is the testable alternative.
    betfair_html_parser: bool = False
    # csv of sport keys to capture. "soccer" (the 3-way 1X2 BACK row) and
    # "basketball" (the 2-way moneyline BACK row) are supported; the default
    # stays "soccer" (committed) but "soccer,basketball" works end-to-end. A
    # basketball capture only sees fixtures when the basketball scrape is also
    # enabled (ODDSPORTAL_BASKETBALL_LEAGUES). Unsupported sport keys are skipped.
    betfair_exchange_sports: str = "soccer"
    # Capture cadence. Change-gated on the per-selection BACK price, so a short
    # interval just tracks repricings; near kickoff is what matters. The >=30s
    # floor blocks hammering-by-typo on a free scraped source.
    betfair_exchange_poll_interval_seconds: int = Field(default=300, ge=30)
    # PER-CYCLE TARGET BOUND (CPU-aware, prod fix 2026-06-23). The capture now
    # sources its match pages from the DB (recent upcoming soccer events with
    # odds, not yet kicked off) instead of the last COMPLETED full scrape's
    # event ids — decoupling it from poll_odds completion (one slow CPU-bound
    # scrape held poll_odds's single slot, so last_fetch_event_ids stayed empty
    # and the reader saw NO targets, capturing nothing — even £270k-liquidity
    # majors). Each capture cycle opens at most this many match pages, so the
    # reader can NEVER try all ~91 pages at once and worsen the CPU overload.
    # Ordered never-captured-first then stalest-Betfair-capture, so a small bound
    # ROTATES through the whole slate over successive cycles. Per-cycle page-load
    # cost == min(this, eligible events). 20 pages / 300s ≈ one page every 15s —
    # gentle on a CPU-bound box; raise only if the box has spare headroom.
    betfair_exchange_max_targets_per_cycle: int = Field(default=20, ge=1, le=200)
    # Only events kicking off within this many hours ahead are eligible targets
    # (and only those NOT yet started). Bounds the candidate set to the
    # actionable near slate — far-future fixtures carry thin/!absent exchange
    # liquidity and would dilute the per-cycle budget. 72h spans a normal slate.
    betfair_exchange_target_window_hours: int = Field(default=72, ge=1, le=336)
    # When true, the settlement-time snapshot close ALSO injects the captured
    # Betfair Exchange BACK close (EXACT match: the betfair event's external_ref
    # is deterministically "betfair:"+pick_ref, ADR-0015) so incremental CLV can
    # anchor on a real exchange sharp close. OFF by default, exactly like
    # clv_use_pinnacle_archive: it changes anchor_type/CLV for picks that have a
    # captured betfair close, so enable only after the betfair coverage report
    # (scripts/reports/betfair_exchange_coverage.py) shows real coverage. Both
    # flags may be on: event_fair_probs prefers Pinnacle (SHARP_BOOKS[0]) over
    # Betfair (index 2), so Pinnacle wins where both price the market and Betfair
    # only fills the gap. Requires BETFAIR_EXCHANGE_ENABLED so a close exists.
    clv_use_betfair_exchange: bool = False
    # build #6 (plan C8): when true, each open-pick re-price also appends a row to
    # pick_line_drift (the vig-free fair + CLV-so-far at that moment), building the
    # full bet-time->close drift path. OFF by default -> the table stays empty and
    # the re-price loop is bit-for-bit unchanged; a pure measurement add, flip it on
    # in-season to start accumulating drift history.
    clv_record_drift: bool = False

    # When true, the LIVE pick pipeline MERGES the captured free Betfair Exchange
    # + Pinnacle ARCADIA prices (re-keyed to each scraped event) into the anchor
    # set, so a pick anchors on the SHARP book instead of the soft-book consensus
    # median — making live picks match the validated Pinnacle-anchored backtest
    # wherever a free sharp price exists (Betfair = exact-ref, Pinnacle = strict
    # name match). Default OFF (current behavior = consensus-anchored). Needs
    # ARCADIA_ENABLED / BETFAIR_EXCHANGE_ENABLED so the archives are populated;
    # uses the SAME re-key/strict-match path as the settlement-time CLV close, so
    # no new false-match surface. Betfair needs a UK/EU proxy + a liquid major.
    value_sharp_anchor_from_archives: bool = False

    # --- ESPN free results auto-settlement (read-only SCORES, never odds) ----
    # ESPN's public site API gives final scores for basketball / NFL / tennis
    # with no key (app/ingestion/espn_scores.py), so the CLOSED tab auto-shows
    # the result + auto-calcs ROI for those sports too (soccer already settles
    # from football-data CSVs). Scores/fixtures ONLY — ESPN odds are soft and
    # are NEVER used as a close. On by default; harmless when a sport has no
    # picks (the feed is queried but nothing matches).
    espn_settle_enabled: bool = True
    # csv of warehouse sport keys to fetch ESPN scores for (see
    # espn_scores.SPORT_ESPN_SOURCES). Soccer is intentionally absent.
    espn_settle_sports: str = "basketball,american_football,tennis"
    # Days back to query ESPN each cycle (today .. today-N+1). Picks settle
    # within hours of kickoff; this bounds the catch-up window + request count
    # (days x feeds per cycle).
    espn_settle_days: int = Field(default=4, ge=1)
    # Also auto-settle from the OddsPortal-SCRAPED final score (Event.scraped_*),
    # so leagues with no free results feed (minor soccer etc.) settle themselves
    # with NO manual entry — the score was already fetched at scrape time, and it
    # lives on the pick's OWN event so settlement matches it exactly (no
    # cross-source name risk). Feed/ESPN scores still take precedence. Default ON.
    settle_from_scraped_scores: bool = True
    # Dedicated finished-score scrape job cadence (seconds). The finished-score
    # pass (capture_finished_scores) runs on its OWN light interval job —
    # SEPARATE from the heavy odds-polling pass — so results settle promptly even
    # when a full odds cycle is slow (the cactusbets.cloud prod gap: a 30-min+
    # odds cycle starved the hourly settle job and scores never landed). Each
    # finished link is scraped + committed individually under the per-link
    # timeout below. Default 60s (was 15 min): paired with the explicit
    # finished-status capture gate, a game becomes a candidate and is captured
    # within ~1 cycle of FT. The >=60s floor blocks hammering-by-typo on a free
    # scraped source. PROD-SAFE WITH NO CONFIG.
    results_scrape_interval_seconds: int = Field(default=60, ge=60)
    # Settlement cycle cadence (seconds). settle_results consumes scraped scores
    # from the DB (cheap — no scrape) and settles open picks; it runs on this
    # short interval (was an hourly cron) so a freshly-captured score settles
    # within ~1 cycle instead of up to an hour. >=15s floor. PROD-SAFE.
    settle_interval_seconds: int = Field(default=30, ge=15, le=3600)
    # Runtime self-audit cadence (seconds). A cheap READ-ONLY DB job that WARNs/
    # ERRORs on operational anomalies (awaiting-result backlog, stale odds) so
    # the health monitor catches issues proactively. PROD-SAFE WITH NO CONFIG.
    self_audit_interval_seconds: int = Field(default=600, ge=60)
    # Per-LINK match-page scrape timeout (seconds) for the finished-score pass.
    # One hung VPS proxy request must not stall the whole pass — each link runs
    # under its own asyncio.wait_for and a timeout drops just that link (retried
    # next cycle). Generous (heavy OddsPortal pages) but finite; bounds fail fast.
    results_scrape_link_timeout_seconds: float = Field(default=90.0, ge=10.0, le=300.0)
    # Per-CYCLE wall-clock budget (seconds) for the finished-score pass. When
    # spent, the pass STOPS CLEANLY (everything committed so far is durable; the
    # remainder drains next cycle) so one big backlog of slow pages can't run
    # unbounded. Default 10 min; cap keeps a typo from letting a cycle run for
    # hours. Should comfortably exceed one link timeout.
    results_scrape_cycle_budget_seconds: float = Field(default=600.0, ge=30.0, le=3600.0)
    # How far back (days) the finished-score pass re-scrapes still-open, unscored
    # picks for their final score. The old hardcoded 3d window stranded older picks
    # on "awaiting result" forever when a slow VPS missed their score in time; 14d
    # recovers them, and the per-cycle limit/budget still bound the work. PROD-SAFE.
    results_scrape_window_days: int = Field(default=14, ge=1, le=90)

    # Opt-in EXPERIMENTAL picks for UNVALIDATED sports (tennis, american_football).
    # OFF by default (committed) — these sports have not cleared the > 2 SE
    # held-out CLV gate, and tennis has NO free closing line so it can never be
    # CLV-validated. When ON, they DO mint picks, but every pick is forced to the
    # volume (shadow) tier: surfaced + CLV-tracked + auto-settled (ESPN), yet
    # NEVER alerted and NEVER reserving exposure. Honest "give me picks for
    # tennis/NFL" without claiming a validated edge or guaranteed ROI.
    enable_unvalidated_picks: bool = False

    # Basketball DEMOTION knob (Batch 3, audit 2026-06-26). Default True =
    # EXPERIMENTAL: basketball is still scraped, minted, persisted, CLV-tracked,
    # auto-settled, AND shown in the dashboard, but every basketball pick is
    # FORCED to the volume/shadow tier — NEVER alerted and reserving ZERO daily
    # exposure — because the sport has not cleared the held-out > 2 SE CLV gate
    # (per-sport evidence is only now accruing). The safe direction: it stops
    # alerting an unproven sport without losing its forward evidence. Wired at the
    # composition root (app/scheduler.py) into PipelineDeps.experimental_sports;
    # the warehouse display path (_VALIDATED_SPORT_PREFIXES) badges it unvalidated.
    # Set False ONLY after a per-(sport, market) CLV-readiness gate clears — a
    # deliberate, ADR-logged promotion, never automatic.
    nba_experimental: bool = True

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
        if self.paper_trading:
            raise ValueError(
                "SAFETY VIOLATION: PAPER_TRADING must stay false — this is a "
                "manual-betting decision-support platform, not a paper-trading "
                "system (CLAUDE.md safety table / ADR-0002)."
            )
        return self

    @model_validator(mode="after")
    def _enforce_stake_caps_coherent(self) -> "Settings":
        # Field bounds above already reject <=0 / >1 cap values; this guards the
        # cross-field invariant so the per-bet cap can never exceed (and thus
        # void) the daily exposure ceiling (audit #1).
        if self.max_recommended_stake_percent > self.max_daily_exposure_percent:
            raise ValueError("MAX_RECOMMENDED_STAKE_PERCENT must be <= MAX_DAILY_EXPOSURE_PERCENT")
        # The per-event cap must sit between the per-bet cap (so a single
        # full-cap pick always fits) and the daily cap (the daily ceiling can
        # never be voided by a looser per-event sub-cap).
        if self.event_exposure_cap_enabled and not (
            self.max_recommended_stake_percent
            <= self.max_event_exposure_percent
            <= self.max_daily_exposure_percent
        ):
            raise ValueError(
                "MAX_EVENT_EXPOSURE_PERCENT must be between "
                "MAX_RECOMMENDED_STAKE_PERCENT and MAX_DAILY_EXPOSURE_PERCENT"
            )
        return self

    @model_validator(mode="after")
    def _enforce_dashboard_auth_config(self) -> "Settings":
        if self.dashboard_auth_enabled:
            has_hash = bool(self.dashboard_auth_password_hash.get_secret_value())
            has_secret = bool(self.dashboard_session_secret.get_secret_value())
            # Both blank = the FIRST-RUN SETUP path: the password is set via the
            # /setup screen on first launch and persisted to the database, so no
            # hash/secret needs to live in .env. Supplying BOTH is the
            # hand-provisioned path; supplying exactly ONE is a misconfiguration.
            if has_hash != has_secret:
                raise ValueError(
                    "set BOTH DASHBOARD_AUTH_PASSWORD_HASH and "
                    "DASHBOARD_SESSION_SECRET, or NEITHER (leave both blank to set "
                    "the password via the first-run /setup screen, which stores "
                    "it in the database)."
                )
            if has_hash:
                hash_parts = self.dashboard_auth_password_hash.get_secret_value().split("$")
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
            ("NFL", self.oddsportal_nfl_leagues, self.oddsportal_nfl_markets),
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
    def _enforce_scrape_concurrency_within_proxy_budget(self) -> "Settings":
        # Concurrency is safe up to ONE in-flight request per proxy IP: per-match
        # rotation (SCRAPER_PROXY_POOL) spreads each concurrent GET to a distinct
        # IP. The single-IP ceiling is 5; ABOVE that, require >=1 proxy per
        # concurrent request so a higher concurrency can never pile N requests
        # onto one IP and invite a block.
        cap = max(5, len(self.scraper_proxies()))
        if self.oddsportal_concurrency > cap:
            raise ValueError(
                f"ODDSPORTAL_CONCURRENCY={self.oddsportal_concurrency} exceeds the safe "
                f"ceiling {cap}: above 5 you need >=1 proxy per concurrent request, but "
                f"SCRAPER_PROXY_POOL has {len(self.scraper_proxies())}. Add proxies or "
                "lower the concurrency so each request exits a distinct IP."
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
        if self.value_max_edge <= self.value_min_edge:
            raise ValueError(
                "VALUE_MAX_EDGE must be > VALUE_MIN_EDGE (it is the data-error ceiling)."
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
        # Per-market devig override: a bad method name must fail fast at startup,
        # never silently fall through to the global method on those markets.
        parse_market_devig(self.value_devig_per_market)
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
        parse_proxy_urls(self.arcadia_proxy_urls.get_secret_value())
        parse_scraper_proxy_pool(self.scraper_proxy_pool.get_secret_value())
        return self

    def odds_api_keys(self) -> tuple[str, ...]:
        """Configured Odds API keys for rotation, in order, empties dropped."""
        keys = (
            self.odds_api_key.get_secret_value(),
            self.odds_api_key_1.get_secret_value(),
            self.odds_api_key_2.get_secret_value(),
            self.odds_api_key_3.get_secret_value(),
        )
        return tuple(k for k in keys if k)

    def arcadia_proxies(self) -> tuple[str, ...]:
        """Configured Arcadia/Pinnacle outbound proxies, in rotation order."""
        return parse_proxy_urls(self.arcadia_proxy_urls.get_secret_value())

    def scraper_proxies(self) -> tuple[ScraperProxy, ...]:
        """Live OddsPortal scrape outbound proxy pool, in rotation order."""
        return parse_scraper_proxy_pool(self.scraper_proxy_pool.get_secret_value())

    def arcadia_effective_proxy_urls(self) -> tuple[str, ...]:
        """Proxy URLs for the Pinnacle Arcadia client, in rotation order.

        Arcadia 403s datacenter egress, so a proxy is REQUIRED for capture
        (direct fetches fail /sports discovery + every matchup). Prefer the
        dedicated ARCADIA_PROXY_URLS; when unset, fall back to the shared scraper
        pool — credentials embedded in the URL, the same shape parse_proxy_urls
        already accepts — so a single SCRAPER_PROXY_POOL keeps the sharp Pinnacle
        archive flowing without duplicate config. Empty only when BOTH are unset
        (capture then runs direct and 403s — logged, never fatal)."""
        own = self.arcadia_proxies()
        if own:
            return own
        return tuple(
            f"http://{p.username}:{p.password}@{p.url.split('://', 1)[-1]}"
            for p in self.scraper_proxies()
        )


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


def exposure_ledger(settings: Settings) -> DailyExposureLedger:
    """Build the daily exposure ledger from Settings (composition root only).

    The per-event correlation sub-cap is applied when EVENT_EXPOSURE_CAP_ENABLED
    is set (default); otherwise only the daily cap binds.
    """
    return DailyExposureLedger(
        max_daily_fraction=settings.max_daily_exposure_percent,
        max_event_fraction=(
            settings.max_event_exposure_percent if settings.event_exposure_cap_enabled else None
        ),
    )


def value_policy(settings: Settings) -> ValuePolicy:
    """Optional value-gate refinements; the default (empty) Settings knobs
    build the all-empty no-op policy — current live behavior, untouched."""
    return ValuePolicy(
        min_edge_by_market=parse_market_min_edges(settings.value_min_edge_per_market),
        odds_bands=parse_odds_bands(settings.value_odds_bands),
        min_books_by_market=parse_market_min_books(settings.value_min_books_per_market),
        major_leagues=parse_major_leagues(settings.value_major_leagues),
        require_sharp_anchor=settings.value_require_sharp_anchor,
        max_edge=settings.value_max_edge,
        devig_by_market=parse_market_devig(settings.value_devig_per_market),
        consensus_logit_pool=settings.value_consensus_logit_pool,
    )


def steam_policy(settings: Settings) -> SteamPolicy:
    """Line-movement / steam-awareness gate policy from Settings (root only).

    Always built so the gate RUNS once wired into the pipeline; the default
    ``enabled=False`` keeps it in SHADOW (compute + log, no tier change). The
    pure gate (app/edge/steam.py) never reads the environment — policy crosses
    the boundary as this frozen dataclass."""
    return SteamPolicy(
        enabled=settings.value_steam_gate_enabled,
        lookback_seconds=settings.value_steam_lookback_seconds,
        min_points=settings.value_steam_min_points,
        soft_toward_anchor_close_frac=settings.value_steam_close_frac,
        min_initial_gap=settings.value_steam_min_initial_gap,
        anchor_staleness_seconds=settings.value_steam_anchor_staleness_seconds,
        soft_steam_away_delta=settings.value_steam_soft_steam_away_delta,
        demote_on_soft_steam=settings.value_steam_demote_on_soft_steam,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
