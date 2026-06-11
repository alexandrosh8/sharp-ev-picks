"""Free OddsPortal odds via OddsHarvester, used directly (ADR-0012).

OddsHarvester (MIT, jordantete/OddsHarvester) scrapes oddsportal.com — an
odds AGGREGATOR, not a bookmaker: read-only data collection, no betting
surface. Its `run_scraper` coroutine is consumed as-is; this module only
adapts its match dicts into our `OddsSnapshotIn` stream and registers team
context in the EventDirectory for the model layer.

The oddsharvester import is lazy so the default (extras-free) install and CI
profile keep working; install with `uv sync --extra backfill`.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from app.ingestion.base import EventDirectory, EventTeams
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

logger = logging.getLogger(__name__)

ScrapeFn = Callable[..., Awaitable[Any]]

# OddsHarvester market keys we can devig SOUNDLY (mutually-exclusive,
# full-coverage outcomes). Each line of a totals/handicap family groups
# separately via OddsSnapshotIn.market_detail. HALF-LINE Asian handicaps
# (±0.5, ±1.5, …) and European handicaps (3-way incl. handicap-draw) are
# full markets — direct devig is valid. INTEGER/QUARTER AH lines carry push
# outcomes (probabilities do not sum to 1) and are rejected at construction.
# The pricing bridge for them now EXISTS (app/models/ah_bridge.py, 2026-06-10:
# devigged 1X2+OU2.5 -> goal grid -> split-stake EV); they stay rejected here
# until the value pipeline EV path is wired and backtest-validated.
_EXACT_MARKETS: dict[str, Market] = {
    "1x2": Market.H2H,  # football/basketball 3-way
    "home_away": Market.H2H,  # basketball moneyline (OddsHarvester has no "moneyline" key)
    "btts": Market.BTTS,
    "dnb": Market.DNB,
    "double_chance": Market.DOUBLE_CHANCE,
}


def _market_for_key(key: str) -> Market | None:
    if key in _EXACT_MARKETS:
        return _EXACT_MARKETS[key]
    if key.startswith("over_under_"):
        return Market.TOTALS
    if key.startswith(("asian_handicap_", "european_handicap_")):
        return Market.SPREADS
    return None


def _line_from_key(market_key: str) -> float | None:
    """'over_under_2_5' -> 2.5; 'asian_handicap_-1_5' -> -1.5;
    'asian_handicap_games_-7_5_games' -> -7.5; 'european_handicap_+1' -> 1.0."""
    raw = market_key
    for prefix in (
        "over_under_games_",
        "over_under_",
        "asian_handicap_games_",
        "asian_handicap_",
        "european_handicap_",
    ):
        if raw.startswith(prefix):
            raw = raw.removeprefix(prefix).removesuffix("_games")
            try:
                return float(raw.replace("_", "."))
            except ValueError:
                return None
    return None


def _fmt_line(line: float) -> str:
    return f"{line:+g}"


def _validate_markets(markets: Sequence[str]) -> None:
    unknown = [m for m in markets if _market_for_key(m) is None]
    if unknown:
        raise ValueError(f"unsupported oddsportal markets: {unknown}")
    for m in markets:
        if m.startswith("asian_handicap"):
            line = _line_from_key(m)
            if line is None or abs(line % 1.0) != 0.5:
                # integer/quarter lines have PUSH outcomes -> direct devig
                # invalid; only half-lines are sound without a score grid.
                raise ValueError(
                    f"asian handicap line must be a half line (±0.5, ±1.5, …), got: {m}"
                )
        if m.startswith("over_under_") and _line_from_key(m) is None:
            raise ValueError(f"cannot parse totals line from: {m}")
        if m.startswith("european_handicap_") and _line_from_key(m) is None:
            raise ValueError(f"cannot parse handicap line from: {m}")


# Leagues OddsPortal carries but OddsHarvester 0.3.0's registry omits —
# standard URL pattern, each verified live (HTTP 200) on 2026-06-11.
# (turkey-super-lig and greece-super-league ARE upstream keys already.)
_EXTRA_LEAGUES: dict[str, dict[str, str]] = {
    "football": {
        "netherlands-eredivisie": "https://www.oddsportal.com/football/netherlands/eredivisie",
        "belgium-jupiler-pro-league": (
            "https://www.oddsportal.com/football/belgium/jupiler-pro-league"
        ),
    }
}


def register_extra_leagues() -> None:
    """Extend OddsHarvester's league registry in-place (idempotent).

    setdefault means upstream wins the moment a release adds these keys.
    """
    from oddsharvester.utils.sport_league_constants import SPORTS_LEAGUES_URLS_MAPPING

    for sport_name, extras in _EXTRA_LEAGUES.items():
        for sport, leagues in SPORTS_LEAGUES_URLS_MAPPING.items():
            if str(getattr(sport, "value", sport)) == sport_name:
                for slug, url in extras.items():
                    leagues.setdefault(slug, url)


async def _default_scrape(**kwargs: Any) -> Any:
    """Call OddsHarvester's run_scraper as-is (lazy import)."""
    register_extra_leagues()
    from oddsharvester.core.scraper_app import run_scraper
    from oddsharvester.utils.command_enum import CommandEnum

    return await run_scraper(command=CommandEnum.UPCOMING_MATCHES, **kwargs)


class OddsPortalLoader:
    """OddsLoader over OddsHarvester's upcoming-matches scraper."""

    def __init__(
        self,
        directory: EventDirectory,
        leagues_by_sport_key: dict[str, tuple[str, list[str]]],
        markets: Sequence[str] = ("1x2", "over_under_2_5"),
        scrape_fn: ScrapeFn | None = None,
        headless: bool = True,
        max_pages: int = 1,
        date: str | None = None,
        markets_by_sport_key: dict[str, Sequence[str]] | None = None,
        days_ahead: int | None = None,
    ) -> None:
        """`leagues_by_sport_key` maps our sport key (e.g. "soccer") to
        (oddsharvester sport, [oddsportal league slugs]). `markets_by_sport_key`
        overrides `markets` per sport key (football and basketball use
        different OddsHarvester market keys). `date` is an optional YYYYMMDD
        filter; None (default) scrapes the general upcoming page.
        `days_ahead` switches to dated scrapes computed at FETCH time:
        one pass per UTC date from today through today+days_ahead — cycles
        then cover exactly the actionable games instead of a league's whole
        future fixture list (far-future matches are skipped by design)."""
        self._markets_by_sport = {k: tuple(v) for k, v in (markets_by_sport_key or {}).items()}
        for market_list in (tuple(markets), *self._markets_by_sport.values()):
            _validate_markets(market_list)
        # leagues=["all"] -> league-less scrape of oddsportal's dated daily
        # page (every league that day). That URL needs a date, so "all"
        # requires dated scraping. Cycle time scales with the day's full
        # fixture list x markets — busy weekends take a while.
        if days_ahead is None and date is None:
            for _sport, league_list in leagues_by_sport_key.values():
                if league_list == ["all"]:
                    raise ValueError(
                        "leagues=['all'] needs dated scraping — set days_ahead (or date)"
                    )
        self._directory = directory
        self._config = dict(leagues_by_sport_key)
        self._markets = tuple(markets)
        self._scrape = scrape_fn or _default_scrape
        self._headless = headless
        self._max_pages = max_pages
        self._date = date
        self._days_ahead = days_ahead

    def _markets_for(self, sport_key: str) -> tuple[str, ...]:
        return self._markets_by_sport.get(sport_key, self._markets)

    async def fetch_odds(self, sport_key: str) -> list[OddsSnapshotIn]:
        if sport_key not in self._config:
            logger.warning("no oddsportal league config for sport key %s", sport_key)
            return []
        sport, leagues = self._config[sport_key]
        # ["all"] -> league-less scrape: the dated daily page already lists
        # every league's games for that day.
        scrape_leagues: list[str] | None = None if leagues == ["all"] else leagues
        now = datetime.now(tz=UTC)
        if self._days_ahead is not None:
            # Dated pages (UTC, matching browser_timezone_id below): today
            # through today+N — only the actionable slate, computed per fetch.
            dates: list[str | None] = [
                (now + timedelta(days=offset)).strftime("%Y%m%d")
                for offset in range(self._days_ahead + 1)
            ]
        else:
            dates = [self._date]

        matches: list[dict[str, Any]] = []
        seen_links: set[str] = set()
        for scrape_date in dates:
            result = await self._scrape(
                sport=sport,
                date=scrape_date,
                leagues=scrape_leagues,
                markets=list(self._markets_for(sport_key)),
                headless=self._headless,
                max_pages=self._max_pages,
                # CRITICAL: oddsportal embeds timestamps shifted to the
                # BROWSER's timezone; without this, kickoffs/capture times
                # inherit the host offset (observed +3h on a Cyprus-time Mac)
                # while labeled UTC. Also keeps dated pages aligned to the
                # UTC dates computed above (upstream gotcha doc §10).
                browser_timezone_id="UTC",
            )
            for match in getattr(result, "success", None) or []:
                home = str(match.get("home_team") or "").strip()
                away = str(match.get("away_team") or "").strip()
                link = str(
                    match.get("match_link") or f"{home}|{away}|{match.get('match_date', '')}"
                )
                if link in seen_links:
                    continue  # same fixture listed on adjacent date pages
                seen_links.add(link)
                matches.append(match)

        snapshots: list[OddsSnapshotIn] = []
        for match in matches:
            snapshots.extend(self._convert_match(match, now, self._markets_for(sport_key)))
        # Per-market counts make scrape gaps visible: OddsPortal market-tab
        # navigation is DOM-fragile upstream, so secondary markets (btts/dnb/
        # AH/EH) intermittently come back empty while 1x2 succeeds — that is
        # why picks can skew to h2h. Gaps are expected; never bypass anti-bot.
        per_market: dict[str, int] = {}
        for snap in snapshots:
            key = snap.market_detail or str(snap.market)
            per_market[key] = per_market.get(key, 0) + 1
        missing = [m for m in self._markets_for(sport_key) if m not in per_market]
        logger.info(
            "oddsportal %s: %d matches -> %d snapshots %s%s",
            sport_key,
            len(matches),
            len(snapshots),
            per_market,
            f" | markets with NO odds: {missing}" if missing and matches else "",
        )
        return snapshots

    def sport_segment(self, sport_key: str) -> str | None:
        """URL sport segment for a sport key ("soccer" -> "football") — lets
        callers pre-filter match links before spending a scrape budget."""
        cfg = self._config.get(sport_key)
        return str(cfg[0]) if cfg else None

    async def fetch_match_odds(
        self, sport_key: str, match_links: Sequence[str]
    ) -> list[OddsSnapshotIn]:
        """Scrape SPECIFIC match pages (open picks outside the dated window
        still need fresh prices). Links from other sports are filtered out
        — oddsportal URLs embed the sport segment."""
        if sport_key not in self._config:
            return []
        sport, _leagues = self._config[sport_key]
        links = [link for link in match_links if f"/{sport}/" in link]
        if not links:
            return []
        now = datetime.now(tz=UTC)
        result = await self._scrape(
            sport=sport,
            match_links=links,
            markets=list(self._markets_for(sport_key)),
            headless=self._headless,
            browser_timezone_id="UTC",  # see fetch_odds — host tz leaks otherwise
        )
        snapshots: list[OddsSnapshotIn] = []
        for match in getattr(result, "success", None) or []:
            snapshots.extend(self._convert_match(match, now, self._markets_for(sport_key)))
        logger.info(
            "oddsportal %s match-link revalidation: %d links -> %d snapshots",
            sport_key,
            len(links),
            len(snapshots),
        )
        return snapshots

    def _convert_match(
        self, match: dict[str, Any], now: datetime, markets: Sequence[str]
    ) -> list[OddsSnapshotIn]:
        home = str(match.get("home_team") or "").strip()
        away = str(match.get("away_team") or "").strip()
        if not home or not away:
            return []
        event_id = str(match.get("match_link") or f"{home}|{away}|{match.get('match_date', '')}")
        league = str(match.get("league_name") or "")
        self._directory.register(
            event_id,
            EventTeams(
                home=home,
                away=away,
                league=league,
                starts_at=_parse_ts(match.get("match_date")),
            ),
        )
        captured_at = _parse_ts(match.get("scraped_date")) or now

        snapshots: list[OddsSnapshotIn] = []
        for market_key in markets:
            entries = match.get(f"{market_key}_market") or []
            market = _market_for_key(market_key)
            if market is None:  # pragma: no cover — blocked by _validate_markets
                continue
            seen_books: set[str] = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                bookmaker = str(entry.get("bookmaker_name") or "unknown")
                # OddsHarvester can emit duplicate bookmaker rows (e.g. odds
                # history); duplicates would corrupt devig (6-leg "markets").
                if bookmaker in seen_books:
                    continue
                seen_books.add(bookmaker)
                for label, selection in _selections(market_key, home, away):
                    odds = _parse_odds(entry.get(label))
                    if odds is None:
                        continue
                    snapshots.append(
                        OddsSnapshotIn(
                            event_id=event_id,
                            bookmaker=bookmaker,
                            market=market,
                            selection=selection,
                            decimal_odds=odds,
                            captured_at=captured_at,
                            ingested_at=now,
                            market_detail=market_key,
                        )
                    )
        return snapshots


def _selections(market_key: str, home: str, away: str) -> list[tuple[str, str]]:
    """OddsHarvester odds-label -> our selection name (must match what the
    model layer emits so the pipeline join works). Label names verified
    against OddsHarvester's SportMarketRegistrar (2026-06-10 research)."""
    if market_key == "1x2":
        return [("1", home), ("X", "Draw"), ("2", away)]
    if market_key == "home_away":  # basketball moneyline
        return [("1", home), ("2", away)]
    if market_key == "btts":
        return [("btts_yes", "BTTS Yes"), ("btts_no", "BTTS No")]
    if market_key == "dnb":
        return [("dnb_team1", home), ("dnb_team2", away)]
    if market_key == "double_chance":
        return [
            ("1X", f"{home} or Draw"),
            ("12", f"{home} or {away}"),
            ("X2", f"Draw or {away}"),
        ]
    line = _line_from_key(market_key)
    if line is None:
        return []
    if market_key.startswith("over_under_"):
        return [("odds_over", f"Over {line:g}"), ("odds_under", f"Under {line:g}")]
    if market_key.startswith("asian_handicap_games_"):  # basketball AH labels differ
        return [
            ("handicap_team_1", f"{home} {_fmt_line(line)}"),
            ("handicap_team_2", f"{away} {_fmt_line(-line)}"),
        ]
    if market_key.startswith("asian_handicap_"):
        return [
            ("team1_handicap", f"{home} {_fmt_line(line)}"),
            ("team2_handicap", f"{away} {_fmt_line(-line)}"),
        ]
    if market_key.startswith("european_handicap_"):
        return [
            ("team1_handicap", f"{home} {_fmt_line(line)}"),
            ("draw_handicap", f"Draw ({_fmt_line(line)})"),
            ("team2_handicap", f"{away} {_fmt_line(-line)}"),
        ]
    return []


def _parse_odds(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return value if value > 1.0 else None


def _parse_ts(raw: Any) -> datetime | None:
    """Parse OddsHarvester timestamps. The installed version emits
    'YYYY-MM-DD HH:MM:SS UTC' (not ISO) — handle both, plus epoch floats."""
    if not raw:
        return None
    text = str(raw).strip()
    # epoch seconds (odds-history rows)
    try:
        return datetime.fromtimestamp(float(text), tz=UTC)
    except (ValueError, OverflowError, OSError):
        pass
    cleaned = text.replace("Z", "+00:00")
    if cleaned.endswith(" UTC"):
        cleaned = cleaned[:-4]
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
