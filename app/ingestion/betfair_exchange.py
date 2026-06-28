"""Read-only Betfair Exchange BACK-odds capture from OddsPortal's JSON feed.

GET-only PUBLIC market data — NO account, NO login, NO stored credentials, NO
order placement, NO betslip, NO anti-bot bypass. This is the SAME encrypted
curl_cffi JSON/HTTP feed the main football scrape already reads
(``app/ingestion/oddsportal_json.py``); this module only reads back the Betfair
Exchange row (provider id ``"44"``) the generic loader skips, so it can keep its
own ISOLATED capture (an INDEPENDENT archive that mints NO picks/alerts) and feed
the matched-``volume`` liquidity gate the old DOM reader was uniquely built for.

WHY JSON, NOT PLAYWRIGHT (rebuild 2026-06-28): the prior reader drove a headless
Chromium render of each match page to scrape the ``betting-exchanges`` DOM row —
slow + heavy + DOM-fragile. The JSON feed already carries
``oddsdata.back[<E-key>].odds["44"]`` (the Betfair BACK price) and the matching
``volume["44"]`` (matched £ liquidity), so the capture now rides the proven fast
path: build the per-(event, market) ``.dat`` URL via ``build_feed_url``, GET it on
the shared curl_cffi feed session, ``decrypt_feed_body`` it, and read the Betfair
block. No browser, no Chromium, no hydration polling.

MARKETS (the FEASIBLE Betfair-present feeds, recon-verified):
  * soccer 1x2            — feed ``E-1-2-0-0-0``;   odds["44"] = 3-way DICT.
  * soccer over_under_2_5 — feed ``E-2-2-0-2.5-0``; odds["44"] = 2-way LIST.
  * basketball home_away  — feed ``E-{betType}-{scope}-0-0-0`` (the event's own
    bootstrap default, recon ``E-3-1-0-0-0``); odds["44"] = 2-way LIST.
Basketball totals (over_under) + handicap (asian_handicap) are DELIBERATELY NOT
captured: the Betfair id 44 is absent from those feeds (only ~8 books quote them)
— a confirmed negative finding. ``parse_betfair_feed`` reads odds["44"]; when it
is absent it simply yields no row (never guesses, never crashes).

Liquidity gate (the DOM reader's one unique guarantee, preserved): an outcome
whose matched ``volume["44"]`` is below ``min_liquidity`` — or absent — is SKIPPED,
so a thin/unbacked Betfair price never becomes a sharp anchor.

Isolation + binding (unchanged, ADR-0015 v2): rows persist INLINE onto the
canonical event (``external_ref`` == the OddsPortal match URL,
``bookmaker="Betfair Exchange"``) with ``attach_only_to_existing=True`` — never
minting an event from Betfair-only data — so the resolver/anchor wiring downstream
(``clv_trueup.resolve_betfair_back_snaps``, the sharp anchor, the coverage
instruments) keeps working unchanged.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.ingestion.base import EventTeams, ScraperProxy
from app.ingestion.oddsportal import (
    _market_for_key,
    _parse_odds,
    _selections,
)
from app.ingestion.oddsportal_json import (
    _feed_captured_at,
    _outcome_at,
    _resolve_feed_market,
    build_feed_url,
    decrypt_feed_body,
    extract_bootstrap_tokens,
)
from app.schemas.odds import OddsSnapshotIn

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

BOOKMAKER = "Betfair Exchange"  # normalized -> SHARP_BOOKS / EXCHANGE_COMMISSION
# The decrypted feed keys every book's odds by numeric provider id; Betfair
# Exchange is "44" (static map app/ingestion/oddsportal_bookmakers.py).
BETFAIR_PROVIDER_ID = "44"

# OddsPortal sport URL segment per our sport key — also the gate of which sports
# the capture serves (soccer + basketball only). Kept for call-site + scheduler
# compatibility (BETFAIR_EXCHANGE_SPORTS csv) and as the supported-sport guard.
SPORT_SEGMENTS: dict[str, str] = {"soccer": "football", "basketball": "basketball"}

# The FEASIBLE Betfair-present (sport -> OddsHarvester market key) feeds. Soccer
# carries the 1x2 BACK row + the OU 2.5 ladder; basketball carries the moneyline.
# The order is the request order. Basketball over_under / asian_handicap are
# excluded on purpose (Betfair id 44 absent — see module docstring).
_FEED_MARKETS_BY_SPORT: dict[str, tuple[str, ...]] = {
    "soccer": ("1x2", "over_under_2_5"),
    "basketball": ("home_away",),
}


def feed_markets_for_sport(sport: str) -> tuple[str, ...]:
    """The feasible Betfair feed market keys for a sport ("soccer" -> 1x2 + OU 2.5,
    "basketball" -> home_away). Empty tuple for any unsupported sport."""
    return _FEED_MARKETS_BY_SPORT.get(sport, ())


class BetfairExchangeError(Exception):
    """Non-retryable read failure. Message never contains the URL or creds."""


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _coerce_liquidity(raw: Any) -> float | None:
    """The feed's matched ``volume["44"]`` outcome value as a non-negative float,
    or None when absent/garbled. A None feeds the gate as "no matched liquidity"
    (the outcome is dropped), never an ungated price."""
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0.0 else None


def parse_betfair_feed(
    payload: Mapping[str, Any],
    *,
    market_key: str,
    default_bet_id: int,
    default_scope_id: int,
    home: str,
    away: str,
    event_id: str,
    min_liquidity: float,
    now: datetime,
) -> list[OddsSnapshotIn]:
    """Extract the Betfair Exchange (id 44) BACK rows for ONE market from a
    decrypted feed payload — the load-bearing pure parser.

    Reuses the canonical feed vocabulary verbatim: ``_resolve_feed_market`` for the
    feed key + outcome-index -> odds-label map (so the basketball moneyline binds
    to the event's bootstrap default like the main scrape), ``_outcome_at`` for the
    DICT-vs-LIST shape, ``_selections`` for the readable selection names, and
    ``_market_for_key`` for the ``Market`` enum. The matched ``volume["44"]`` per
    outcome drives the liquidity gate.

    Benign-gap discipline (req #3): an empty/absent ``oddsdata.back`` block, a
    missing feed key, or an absent Betfair id 44 all yield ``[]`` — a thin/closed
    market recovered next cycle, NEVER inferred as "Betfair absent" and never a
    crash. ``captured_at`` is the feed's provider observation time (``d.time-base``,
    UTC-aware), matching the Playwright path's ``scraped_date`` semantics.
    """
    resolved = _resolve_feed_market(
        market_key, default_bet_id=default_bet_id, default_scope_id=default_scope_id
    )
    if resolved is None:
        return []
    market = _market_for_key(market_key)
    if market is None:
        return []

    back = (((payload.get("d") or {}).get("oddsdata") or {}).get("back")) or {}
    if not isinstance(back, Mapping):
        return []
    block = back.get(resolved.feed_key)
    if not isinstance(block, Mapping):
        return []  # market/line not in this feed body — benign gap

    odds_by_bookie = block.get("odds")
    if not isinstance(odds_by_bookie, Mapping):
        return []
    betfair_odds = odds_by_bookie.get(BETFAIR_PROVIDER_ID)
    if betfair_odds is None:
        # Betfair id 44 absent from this feed (e.g. basketball totals/handicap):
        # only ~8 books quote those — a confirmed negative, NOT an error.
        return []
    volume_by_bookie = block.get("volume")
    betfair_volume = (
        volume_by_bookie.get(BETFAIR_PROVIDER_ID) if isinstance(volume_by_bookie, Mapping) else None
    )

    captured_at = _feed_captured_at(payload, now)
    label_to_selection = dict(_selections(market_key, home.strip(), away.strip()))
    snapshots: list[OddsSnapshotIn] = []
    for index, label in resolved.index_to_label.items():
        selection = label_to_selection.get(label)
        if selection is None:
            continue
        decimal_odds = _parse_odds(_outcome_at(betfair_odds, index))
        if decimal_odds is None:
            continue
        liquidity = (
            _coerce_liquidity(_outcome_at(betfair_volume, index))
            if betfair_volume is not None
            else None
        )
        # Liquidity gate: an outcome with no matched volume (or below the floor) is
        # dropped — the exchange's thin/unbacked prices never anchor a pick.
        if liquidity is None or liquidity < min_liquidity:
            continue
        snapshots.append(
            OddsSnapshotIn(
                event_id=event_id,
                bookmaker=BOOKMAKER,
                market=market,
                selection=selection,
                decimal_odds=decimal_odds,
                liquidity=liquidity,
                captured_at=captured_at,
                ingested_at=now,
                market_detail=market_key,
            )
        )
    return snapshots


@dataclass(frozen=True)
class FeedFeasible:
    """One decrypted feed ready for ``parse_betfair_feed``: the OddsHarvester market
    key, the event's bootstrap betType/scope defaults (used for the dynamic
    basketball moneyline binding; 0/0 for the static soccer markets), and the
    decrypted feed payload. The reader's injectable ``feed_loader`` yields these so
    the suite never touches curl_cffi / the AES decrypt."""

    market_key: str
    default_bet_id: int
    default_scope_id: int
    payload: Mapping[str, Any]


FeedLoader = Callable[[str, str], Awaitable[Sequence[FeedFeasible]]]


@dataclass(frozen=True)
class MatchTarget:
    """One match page to read: its OddsPortal URL + team/league/kickoff context
    (event identity throughout the platform IS the match link)."""

    event_id: str  # the normalized match link (canonical external_ref)
    url: str
    teams: EventTeams


# Browser-TLS fingerprint for the curl_cffi feed session — same INTENT as the main
# scrape's pin (a coherent human fingerprint, never anti-bot defeat beyond TLS
# impersonation). Kept in lockstep with oddsportal_json_session.PINNED_IMPERSONATE.
_IMPERSONATE = "chrome146"
# The feed's GET headers (the same X-Requested-With/Accept the main scrape sends).
_FEED_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json,text/plain,*/*",
}


class BetfairExchangeReader:
    """Reads the Betfair Exchange BACK rows for one match off the OddsPortal JSON
    feed, across the feasible markets for the sport.

    The per-match feed FETCH (HTML bootstrap -> per-market ``.dat`` GET -> decrypt)
    is injected as ``feed_loader`` so the suite runs with NO network / NO curl_cffi.
    The default loader uses the proven curl_cffi feed machinery through an optional
    read-only proxy pool, exactly like the main football scrape.
    """

    def __init__(
        self,
        *,
        min_liquidity: float,
        proxy_pool: Sequence[ScraperProxy] = (),
        feed_loader: FeedLoader | None = None,
        geo: str = "GB",
        lang: str = "en",
    ) -> None:
        self._min_liquidity = min_liquidity
        self._proxy_pool = tuple(proxy_pool)
        self._proxy_cursor = 0
        self._feed_loader = feed_loader or self._network_load
        self._geo = geo
        self._lang = lang

    async def read_snapshots(
        self, target: MatchTarget, *, sport: str, now: datetime
    ) -> list[OddsSnapshotIn]:
        """The gated Betfair BACK snapshots for one match across every feasible
        market for ``sport``. An empty result is a benign gap (no Betfair-liquid
        row this cycle), never an error. Transport failures in the default loader
        surface as ``BetfairExchangeError`` for the caller to log + skip."""
        feeds = await self._feed_loader(target.url, sport)
        snapshots: list[OddsSnapshotIn] = []
        for feed in feeds:
            snapshots.extend(
                parse_betfair_feed(
                    feed.payload,
                    market_key=feed.market_key,
                    default_bet_id=feed.default_bet_id,
                    default_scope_id=feed.default_scope_id,
                    home=target.teams.home,
                    away=target.teams.away,
                    event_id=target.event_id,
                    min_liquidity=self._min_liquidity,
                    now=now,
                )
            )
        return snapshots

    # --- default (production) loader: the real curl_cffi feed fetch ---------- #
    async def _network_load(self, match_url: str, sport: str) -> list[FeedFeasible]:
        """GET-only: bootstrap the match page, then GET + decrypt each feasible
        market feed for ``sport``. Rotates through the proxy pool on transport
        failure (one session binds one proxy). A benign empty feed is omitted; a
        total transport failure raises ``BetfairExchangeError`` (type only, never
        the URL/creds)."""
        markets = feed_markets_for_sport(sport)
        if not markets:
            return []
        from curl_cffi.requests import AsyncSession

        from app.ingestion.oddsportal import _proxy_with_creds

        pool: Sequence[ScraperProxy | None] = self._proxy_pool or (None,)
        n = len(pool)
        rotation = [(self._proxy_cursor + offset) % n for offset in range(n)]
        last_exc: Exception | None = None
        for slot in rotation:
            proxy = pool[slot]
            session_kwargs: dict[str, Any] = {"impersonate": _IMPERSONATE}
            if proxy is not None and proxy.url:
                # Credentials are inlined ONLY at the request boundary, never
                # logged (the loader's logs are type/index-only).
                inline = _proxy_with_creds(proxy)
                session_kwargs["proxies"] = {"https": inline, "http": inline}
            try:
                async with AsyncSession(**session_kwargs) as session:
                    feeds = await self._load_with_session(session, match_url, markets)
                self._proxy_cursor = (slot + 1) % n
                return feeds
            except Exception as exc:  # network / TLS / timeout -> failover
                last_exc = exc
                logger.warning(
                    "betfair exchange feed load via proxy slot %d failed (%s); trying next",
                    slot if self._proxy_pool else -1,
                    type(exc).__name__,
                )
                continue
        if last_exc is not None:
            raise BetfairExchangeError(
                f"betfair exchange feed load failed after {n} proxy attempts "
                f"({type(last_exc).__name__})"
            ) from last_exc
        return []

    async def _load_with_session(
        self, session: Any, match_url: str, markets: Sequence[str]
    ) -> list[FeedFeasible]:
        """One match's bootstrap + per-market feed GET/decrypt on a bound session.

        A non-200 HTML page or an unparseable bootstrap is a benign gap (``[]``).
        A per-feed transport error / off-window / decrypt failure skips THAT market
        (recovered next cycle), never the whole match. Only a transport failure on
        the bootstrap GET propagates (so the caller can fail over proxies)."""
        resp = await session.get(match_url, impersonate=_IMPERSONATE)
        if getattr(resp, "status_code", 0) != 200:
            return []
        try:
            token = extract_bootstrap_tokens(resp.text)
        except ValueError:
            return []  # bootstrap header absent/unparseable -> benign gap

        feeds: list[FeedFeasible] = []
        for market_key in markets:
            url = build_feed_url(
                token.sport_id,
                token.event_id,
                market_key,
                token.default_bet_id,
                token.default_scope_id,
            )
            if url is None:
                continue
            try:
                feed_resp = await session.get(
                    url,
                    headers=_FEED_HEADERS,
                    impersonate=_IMPERSONATE,
                    params={"geo": self._geo, "lang": self._lang},
                )
            except Exception as exc:  # one feed's transport blip -> benign skip
                logger.warning(
                    "betfair exchange feed GET failed for market %s (%s) — gap",
                    market_key,
                    type(exc).__name__,
                )
                continue
            if getattr(feed_resp, "status_code", 0) != 200:
                continue
            try:
                payload = decrypt_feed_body(feed_resp.text)
            except (ValueError, RuntimeError) as exc:
                # off-window / empty / decrypt-rotation / version-guard: all benign
                # for one market (the rest of the slate is unaffected this cycle).
                logger.info(
                    "betfair exchange feed decrypt skipped for market %s (%s)",
                    market_key,
                    type(exc).__name__,
                )
                continue
            feeds.append(
                FeedFeasible(
                    market_key=market_key,
                    default_bet_id=token.default_bet_id,
                    default_scope_id=token.default_scope_id,
                    payload=payload,
                )
            )
        return feeds


_BETFAIR_EVENT_PREFIX = "betfair:"


def _namespace_event_ref(event_id: str) -> str:
    """LEGACY namespacing helper retained for ``app/storage/repositories.py``'s
    coverage diagnostic (``betfair_archive_capture_by_sport``), which still probes
    the historical ``"betfair:" + ref`` archive namespace. Inline binding (ADR-0015
    v2) no longer writes under this prefix — the capture attaches Betfair rows onto
    the CANONICAL event — but the prefix is kept so the diagnostic stays callable."""
    if event_id.startswith(_BETFAIR_EVENT_PREFIX):
        return event_id
    return f"{_BETFAIR_EVENT_PREFIX}{event_id}"


class BetfairExchangeCapture:
    """Forward Betfair Exchange BACK-odds capture INLINE onto the canonical event
    (``bookmaker="Betfair Exchange"``) across the feasible markets per sport.

    Mirrors PinnacleArcadiaCapture: an INDEPENDENT capture that mints NO
    picks/alerts and never touches the live pick/dashboard path. Change-gated in
    memory on the per-(sport, event, selection) decimal price so a snapshot is
    written only when a BACK price moves — the latest pre-kickoff row IS that
    selection's exchange close. The gate resets on restart (re-emits each still-
    open price once with a fresh captured_at; the unique key includes captured_at).
    """

    def __init__(
        self,
        reader: BetfairExchangeReader | None,
        session_factory: async_sessionmaker | None,
        *,
        targets_fn: Callable[[str], Sequence[MatchTarget] | Awaitable[Sequence[MatchTarget]]],
        sports: Sequence[str],
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """``targets_fn(sport)`` yields the match pages to read this cycle for a
        sport key. It is injected so this module never owns the listing/scheduling
        policy — the composition root supplies the DB-sourced bounded/rotating
        targets and tests supply a static list. It may be SYNC (returns the
        sequence) or ASYNC (returns an awaitable of it)."""
        self._reader = reader
        self._session_factory = session_factory
        self._targets_fn = targets_fn
        self._sports = tuple(sports)
        self._now_fn = now_fn or _utc_now
        self._seen_price: dict[tuple[str, str, str], float] = {}

    def _select_fresh(
        self, sport: str, event_id: str, snapshots: Sequence[OddsSnapshotIn]
    ) -> list[OddsSnapshotIn]:
        """Keep only snapshots whose BACK price changed since last seen (keyed on
        (sport, event, selection) — selections are unique across the captured
        markets, so the 1x2 / OU / moneyline legs never collide)."""
        fresh: list[OddsSnapshotIn] = []
        for snapshot in snapshots:
            key = (sport, event_id, snapshot.selection)
            if self._seen_price.get(key) == snapshot.decimal_odds:
                continue
            self._seen_price[key] = snapshot.decimal_odds
            fresh.append(snapshot)
        return fresh

    async def capture_once(self) -> dict[str, int]:
        """One capture cycle across all configured sports. Returns new rows
        written per sport. A failed match is logged (type only, never the URL)
        and skipped — never aborts the rest of the cycle."""
        if self._reader is None:
            return {sport: 0 for sport in self._sports}

        from app.storage.repositories import persist_odds_snapshots

        now = self._now_fn()
        written: dict[str, int] = {}
        for sport in self._sports:
            if sport not in SPORT_SEGMENTS:
                logger.warning("betfair exchange: unsupported sport %r; skipping", sport)
                continue
            fresh: list[OddsSnapshotIn] = []
            teams_by_event: dict[str, EventTeams] = {}
            # Honest-zero accounting: `targets` counts fixtures we had a page to
            # read this cycle (0 = nothing scraped for this sport), and
            # `events_with_quotes` counts those that yielded a Betfair-liquid row.
            targets = 0
            events_with_quotes = 0
            target_list = self._targets_fn(sport)
            if inspect.isawaitable(target_list):
                target_list = await target_list
            for target in target_list:
                targets += 1
                try:
                    snapshots = await self._reader.read_snapshots(target, sport=sport, now=now)
                except Exception as exc:
                    logger.warning(
                        "betfair exchange read failed for one %s match: %s",
                        sport,
                        type(exc).__name__,
                    )
                    continue
                if not snapshots:
                    continue  # no row / all outcomes below the liquidity floor
                events_with_quotes += 1
                # INLINE BINDING (ADR-0015 v2): persist under the CANONICAL
                # external_ref (target.event_id == the OddsPortal match URL, the
                # same ref the main scrape's soft-book rows use), so the Betfair
                # rows become just another bookmaker on the canonical event — no
                # cross-source matching needed for any Betfair-priced game.
                event_ref = target.event_id
                event_fresh = self._select_fresh(sport, event_ref, snapshots)
                if event_fresh:
                    fresh.extend(event_fresh)
                    teams_by_event[event_ref] = target.teams
            if not fresh or self._session_factory is None:
                written[sport] = 0
                if self._session_factory is not None:
                    if targets == 0:
                        logger.info(
                            "betfair exchange: %s captured 0 — no fixtures to read this "
                            "cycle (sport scrape off or empty slate)",
                            sport,
                        )
                    else:
                        logger.info(
                            "betfair exchange: %s captured 0 — read %d fixtures, %d had a "
                            "Betfair-liquid BACK row (thin slate / no new price)",
                            sport,
                            targets,
                            events_with_quotes,
                        )
                continue
            # INLINE BINDING (ADR-0015 v2): persist under the CANONICAL sport with
            # attach_only_to_existing=True, so the Betfair rows ATTACH to the
            # canonical event the main scrape already created and NEVER mint one
            # from Betfair-only data. A fixture whose canonical event has not landed
            # yet is skipped THIS cycle and attaches on a later one.
            rows = await persist_odds_snapshots(
                self._session_factory,
                fresh,
                teams_by_event,
                sport=sport,
                default_league=sport,
                attach_only_to_existing=True,
            )
            written[sport] = rows
            if rows:
                logger.info(
                    "betfair exchange: %s captured %d new BACK rows (%d events of %d read)",
                    sport,
                    rows,
                    len(teams_by_event),
                    targets,
                )
        return written
