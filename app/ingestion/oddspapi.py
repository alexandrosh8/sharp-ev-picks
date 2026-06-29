"""Read-only OddsPapi historical-odds client + NBA loader.

WHAT THIS IS. OddsPapi (https://oddspapi.io) exposes a free-tier REST API that
includes Pinnacle and 350+ books. Its historical-odds endpoint (documented at
https://oddspapi.io/us/docs/get-historical-odds, fetched 2026-06-29) returns the
full per-market price history for one fixture:

    GET /v4/historical-odds?fixtureId={id}&bookmakers={slugs}&apiKey={key}

    {
      "fixtureId": "...",
      "bookmakers": {
        "{slug}": { "markets": { "{marketId}": { "outcomes": {
          "{outcomeId}": { "players": { "{playerId}": [
            { "createdAt": "<ISO-8601>", "price": <number>, "limit": <number>,
              "active": <bool>, "exchangeMeta": <object|null> }, ...
          ] } } } } } }
      }
    }

The price-history array is chronological, so the FIRST entry is the OPENING price
and the LAST is the CLOSING (last pre-tip) price. Authentication is the ``apiKey``
QUERY parameter (every endpoint requires it). Bookmakers are a comma-separated
slug list, max 3 (e.g. ``pinnacle,bet365``).

HONEST SCOPE — PINNACLE ANCHOR, SHALLOW FREE TIER. With ``pinnacle`` in the slug
list this source gives a genuine SHARP pre-match anchor (Pinnacle opening) AND a
sharp CLOSE (Pinnacle closing) for the same fixture — the pieces CLV needs. Add a
soft slug (e.g. ``bet365``) and the soft opening becomes the takeable bet price to
test against the Pinnacle fair. BUT the free tier is shallow: history depth, book
breadth and fixture coverage are all limited, so soft prices and some points will
often be missing (mapped to None, never fabricated). Treat thin/absent rows as
expected.

This is a GET/READ-only client. It NEVER places a bet, NEVER authenticates beyond
the read ``apiKey``, and NEVER logs the key or full request URLs (which carry the
key in their query string). The free key is OPERATOR-PROVIDED via ``ODDSPAPI_KEY``
and is OPTIONAL — when absent the caller skips cleanly.

Because the opaque ``marketId``/``outcomeId`` differ per fixture, the on-disk
backtest format is an operator-placed "bundle" JSON per fixture: the resolved
``home_team``/``away_team``/``startTime``/score plus a ``moneyline`` mapping to the
right ids, wrapping the raw ``historical_odds`` payload. ``load_oddspapi_dir``
reads a directory of these bundles. Decimal at the odds boundary; UTC-aware times.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.oddspapi.io"
HISTORICAL_ODDS_PATH = "/v4/historical-odds"


def _to_decimal(value: object) -> Decimal | None:
    """Odds -> Decimal, or None for missing/<=1.0/garbage. Goes through ``str``
    so a JSON float never leaks its binary artefact into the boundary value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return dec if dec > 1 else None


def _parse_time(raw: object) -> datetime | None:
    """ISO-8601 (``...Z`` or with offset) -> tz-aware UTC, or None. Never naive."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def price_history_open_close(
    entries: Iterable[Mapping[str, object]],
) -> tuple[Decimal | None, Decimal | None]:
    """(opening, closing) decimal price from a price-history array.

    Keeps only ``active`` entries with a valid (>1.0) price, sorts them by
    ``createdAt`` (the array SHOULD be chronological, but we sort defensively),
    and returns the earliest as the open and the latest as the close. ``(None,
    None)`` when no usable point exists."""
    usable: list[tuple[str, Decimal]] = []
    for e in entries:
        if not isinstance(e, Mapping):
            continue
        if e.get("active") is False:
            continue
        price = _to_decimal(e.get("price"))
        if price is None:
            continue
        created = e.get("createdAt")
        usable.append((str(created) if created is not None else "", price))
    if not usable:
        return None, None
    usable.sort(key=lambda x: x[0])
    return usable[0][1], usable[-1][1]


def _flatten_outcome_entries(outcome_node: object) -> list[Mapping[str, object]]:
    """All price-history entries under an outcome's ``players`` buckets, flattened.

    A moneyline outcome has one (team-level) player bucket; flattening tolerates
    however many buckets the payload carries without assuming a key name."""
    if not isinstance(outcome_node, Mapping):
        return []
    players = outcome_node.get("players")
    if not isinstance(players, Mapping):
        return []
    out: list[Mapping[str, object]] = []
    for arr in players.values():
        if isinstance(arr, list):
            out.extend(e for e in arr if isinstance(e, Mapping))
    return out


def outcome_open_close(
    bookmaker_node: object, market_id: str, outcome_id: str
) -> tuple[Decimal | None, Decimal | None]:
    """(open, close) decimal price for one (market, outcome) of one bookmaker.

    Navigates ``markets[market_id].outcomes[outcome_id].players[*]`` and reduces
    the flattened history to first/last. ``(None, None)`` for any missing level."""
    if not isinstance(bookmaker_node, Mapping):
        return None, None
    markets = bookmaker_node.get("markets")
    if not isinstance(markets, Mapping):
        return None, None
    market = markets.get(market_id)
    if not isinstance(market, Mapping):
        return None, None
    outcomes = market.get("outcomes")
    if not isinstance(outcomes, Mapping):
        return None, None
    return price_history_open_close(_flatten_outcome_entries(outcomes.get(outcome_id)))


@dataclass(frozen=True, slots=True)
class OddsPapiGame:
    """One NBA fixture: Pinnacle sharp pre-match (open) + sharp close + best-soft
    pre-match (open), with the settled result. Odds are ``Decimal`` (NUMERIC
    discipline) or None when the free tier did not carry the point.

    ``*_pinnacle_open`` is the sharp ANCHOR (devig this for fair value);
    ``*_pinnacle_close`` is the sharp CLOSE (the CLV reference); ``*_best_soft_open``
    is the best takeable pre-match price across the configured soft books.
    """

    fixture_id: str
    commence_utc: datetime | None  # tz-aware UTC; never naive
    home_team: str
    away_team: str
    result: str | None  # "H" | "A" | None (NBA: no draw)
    home_pinnacle_open: Decimal | None
    away_pinnacle_open: Decimal | None
    home_pinnacle_close: Decimal | None
    away_pinnacle_close: Decimal | None
    home_best_soft_open: Decimal | None
    away_best_soft_open: Decimal | None
    home_best_soft_close: Decimal | None
    away_best_soft_close: Decimal | None


def _result_from_bundle(bundle: Mapping[str, object]) -> str | None:
    explicit = bundle.get("result")
    if explicit in ("H", "A"):
        return str(explicit)
    home = bundle.get("home_score")
    away = bundle.get("away_score")
    if isinstance(home, (int, float)) and isinstance(away, (int, float)):
        if home > away:
            return "H"
        if away > home:
            return "A"
    return None


def _best(prices: Iterable[Decimal | None]) -> Decimal | None:
    """Max (best takeable) of the non-None decimal prices, or None."""
    valid = [p for p in prices if p is not None]
    return max(valid) if valid else None


def parse_fixture_bundle(
    bundle: Mapping[str, object],
    *,
    sharp: str = "pinnacle",
    soft: Sequence[str] = (),
) -> OddsPapiGame | None:
    """Map one operator-placed fixture bundle to an :class:`OddsPapiGame`.

    Requires the ``sharp`` bookmaker (Pinnacle) to carry the moneyline anchor —
    without a sharp price there is no valid CLV reference, so the fixture is
    skipped (None). The best soft price is the MAX across the ``soft`` slugs at
    the open (the line-shopping bet price) and at the close (a stricter CLV ref).
    """
    moneyline = bundle.get("moneyline")
    hist = bundle.get("historical_odds")
    if not isinstance(moneyline, Mapping) or not isinstance(hist, Mapping):
        return None
    books = hist.get("bookmakers")
    if not isinstance(books, Mapping):
        return None
    market_id = str(moneyline.get("marketId"))
    home_oid = str(moneyline.get("home_outcomeId"))
    away_oid = str(moneyline.get("away_outcomeId"))

    sharp_node = books.get(sharp)
    home_p_open, home_p_close = outcome_open_close(sharp_node, market_id, home_oid)
    away_p_open, away_p_close = outcome_open_close(sharp_node, market_id, away_oid)
    if (
        home_p_open is None
        and home_p_close is None
        and away_p_open is None
        and away_p_close is None
    ):
        return None  # no Pinnacle anchor at all -> unusable for CLV

    soft_home_open: list[Decimal | None] = []
    soft_away_open: list[Decimal | None] = []
    soft_home_close: list[Decimal | None] = []
    soft_away_close: list[Decimal | None] = []
    for slug in soft:
        node = books.get(slug)
        ho, hc = outcome_open_close(node, market_id, home_oid)
        ao, ac = outcome_open_close(node, market_id, away_oid)
        soft_home_open.append(ho)
        soft_home_close.append(hc)
        soft_away_open.append(ao)
        soft_away_close.append(ac)

    return OddsPapiGame(
        fixture_id=str(bundle.get("fixtureId") or ""),
        commence_utc=_parse_time(bundle.get("startTime")),
        home_team=str(bundle.get("home_team") or "").strip(),
        away_team=str(bundle.get("away_team") or "").strip(),
        result=_result_from_bundle(bundle),
        home_pinnacle_open=home_p_open,
        away_pinnacle_open=away_p_open,
        home_pinnacle_close=home_p_close,
        away_pinnacle_close=away_p_close,
        home_best_soft_open=_best(soft_home_open),
        away_best_soft_open=_best(soft_away_open),
        home_best_soft_close=_best(soft_home_close),
        away_best_soft_close=_best(soft_away_close),
    )


def load_oddspapi_dir(
    path: Path, *, sharp: str = "pinnacle", soft: Sequence[str] = ()
) -> list[OddsPapiGame]:
    """Read-only: parse every operator-placed ``*.json`` fixture bundle in ``path``.

    Unparseable / anchorless bundles are skipped with a log line. An absent
    directory returns ``[]`` (the caller prints the operator instruction). Sorted
    by (commence, fixture_id) for determinism."""
    if not path.is_dir():
        return []
    games: list[OddsPapiGame] = []
    for f in sorted(path.glob("*.json")):
        try:
            bundle = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            logger.warning("skip oddspapi bundle %s: %s", f.name, type(exc).__name__)
            continue
        if not isinstance(bundle, Mapping):
            continue
        game = parse_fixture_bundle(bundle, sharp=sharp, soft=soft)
        if game is not None:
            games.append(game)
    games.sort(key=lambda g: (g.commence_utc or datetime.min.replace(tzinfo=UTC), g.fixture_id))
    return games


class OddsPapiClient:
    """Minimal READ-ONLY GET client for the OddsPapi historical-odds endpoint.

    Authenticates with the ``apiKey`` query parameter (the documented scheme). It
    performs GET requests only; it never places a bet, never authenticates beyond
    the read key, and never logs the key or full URLs (the key rides the query
    string). Pass an ``httpx.AsyncClient`` (with a ``base_url``) for testing via
    ``MockTransport``; otherwise one is created against ``BASE_URL``.
    """

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client
        self._owns_client = client is None
        # Secret-hygiene rule: httpx/httpcore log the FULL request URL at INFO,
        # and the apiKey rides the query string. Pin them to WARNING so the key
        # never reaches the logs (mirrors app.main._silence_url_logging for the
        # script/backtest entrypoints that never start the FastAPI lifespan).
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    async def __aenter__(self) -> OddsPapiClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=BASE_URL)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def historical_odds(
        self, fixture_id: str, *, bookmakers: Sequence[str] = ("pinnacle",)
    ) -> dict:
        """GET the full price history for one fixture (read-only).

        ``bookmakers`` is a slug list (max 3 per the docs). On error, logs only the
        exception TYPE and status — never the URL/key."""
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=BASE_URL)
            self._owns_client = True
        params = {
            "fixtureId": fixture_id,
            "bookmakers": ",".join(bookmakers[:3]),
            "apiKey": self._api_key,
        }
        try:
            response = await self._client.get(HISTORICAL_ODDS_PATH, params=params, timeout=30.0)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("oddspapi historical-odds HTTP %s", exc.response.status_code)
            raise
        except httpx.HTTPError as exc:
            logger.warning("oddspapi historical-odds error: %s", type(exc).__name__)
            raise
        data = response.json()
        return data if isinstance(data, dict) else {}


__all__ = [
    "BASE_URL",
    "HISTORICAL_ODDS_PATH",
    "OddsPapiClient",
    "OddsPapiGame",
    "load_oddspapi_dir",
    "outcome_open_close",
    "parse_fixture_bundle",
    "price_history_open_close",
]
