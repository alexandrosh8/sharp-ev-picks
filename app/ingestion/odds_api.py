"""The Odds API client (read-only, GET-only) with multi-key rotation.

Secret hygiene: API keys travel only in query params of outbound requests;
they are NEVER included in exceptions, logs, or any stringified output.
Keys advance on 401/429 (invalid/exhausted); transport errors retry with
exponential backoff; other 4xx fail without retry (they would burn credits).
"""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.ingestion.base import EventDirectory, EventTeams
from app.ingestion.http_safety import UpstreamBodyTooLarge, request_httpx_bounded
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.the-odds-api.com/v4"
# Provider market keys -> internal Market enum
_MARKET_MAP = {
    "h2h": Market.H2H,
    "spreads": Market.SPREADS,
    "totals": Market.TOTALS,
}
# The Odds API exposes Betfair Exchange as regional keys (betfair_ex_uk/eu/au);
# fold them to the canonical name the value engine recognises as the sharp
# exchange anchor (app/edge/value.SHARP_BOOKS / EXCHANGE_COMMISSION). Pinnacle's
# key ("pinnacle") and "smarkets" already match SHARP_BOOKS, so they pass
# through unchanged — without this fold a free Betfair Exchange price would be
# treated as just another soft book and never anchor CLV.
_BOOK_CANONICAL = {
    "betfair_ex_uk": "betfair exchange",
    "betfair_ex_eu": "betfair exchange",
    "betfair_ex_au": "betfair exchange",
}


def _canonical_book(key: str) -> str:
    return _BOOK_CANONICAL.get(key, key)


class OddsApiError(Exception):
    """Raised when no key can fetch odds. Message never contains a key."""


@dataclass(frozen=True)
class OddsApiMetrics:
    requests: int
    retries: int
    rate_limited: int
    rejected_keys: int
    schema_rows_dropped: int
    quota_remaining_by_key: Mapping[int, int]


_TRANSIENT_STATUSES = frozenset({408, 500, 502, 503, 504})
_MAX_STATUS_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 60.0
_INVALID_KEY_COOLDOWN = timedelta(days=3650)
_DEFAULT_RATE_LIMIT_COOLDOWN = timedelta(minutes=1)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_EVENTS_PER_RESPONSE = 5_000
MAX_NESTED_ROWS = 20_000
MAX_SNAPSHOTS_PER_RESPONSE = 250_000
MAX_API_KEY_SLOTS = 16
MAX_API_KEY_BYTES = 256
MAX_EVENT_ID_BYTES = 200
MAX_TEAM_NAME_BYTES = 200
MAX_SPORT_KEY_BYTES = 100
MAX_BOOKMAKER_KEY_BYTES = 128
MAX_SELECTION_BYTES = 200


class OddsApiClient:
    """Read-only Odds API client. Keys rotate on 401/429."""

    def __init__(
        self,
        api_keys: Sequence[str],
        client: httpx.AsyncClient,
        base_url: str = DEFAULT_BASE_URL,
        regions: str = "eu",
        markets: str = "h2h,totals,spreads",
        directory: EventDirectory | None = None,
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._keys = _validate_api_keys(api_keys)
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._regions = regions
        self._markets = markets
        self._directory = directory
        self._now_fn = now_fn or (lambda: datetime.now(tz=UTC))
        self._sleep_fn = sleep_fn or asyncio.sleep
        self._cursor = 0
        self._cooldown_until: dict[int, datetime] = {}
        self._requests = 0
        self._retries = 0
        self._rate_limited = 0
        self._rejected_keys = 0
        self._schema_rows_dropped = 0
        self._quota_remaining: dict[int, int] = {}

    @property
    def metrics(self) -> OddsApiMetrics:
        return OddsApiMetrics(
            requests=self._requests,
            retries=self._retries,
            rate_limited=self._rate_limited,
            rejected_keys=self._rejected_keys,
            schema_rows_dropped=self._schema_rows_dropped,
            quota_remaining_by_key=dict(self._quota_remaining),
        )

    async def _get(self, url: str, params: dict[str, str]) -> httpx.Response:
        return await request_httpx_bounded(
            self._client,
            "GET",
            url,
            max_bytes=MAX_RESPONSE_BYTES,
            params=params,
            timeout=20.0,
        )

    async def fetch_odds(self, sport_key: str) -> list[OddsSnapshotIn]:
        normalized_sport_key = _validate_sport_key(sport_key)
        url = f"{self._base_url}/sports/{normalized_sport_key}/odds"
        attempted: set[int] = set()
        while len(attempted) < len(self._keys):
            now = self._now_fn()
            index = self._next_key_index(now, attempted)
            if index is None:
                break
            attempted.add(index)
            self._cursor = (index + 1) % len(self._keys)
            try:
                response = await self._request_with_status_retry(url, self._keys[index])
            except httpx.TransportError:
                # httpx transport exceptions retain the full request URL,
                # including the apiKey query parameter. Replace the exception
                # and suppress its context so an unhandled traceback cannot
                # disclose a credential.
                raise OddsApiError(
                    f"odds api transport failed for sport={normalized_sport_key}"
                ) from None
            except UpstreamBodyTooLarge:
                raise OddsApiError(
                    f"odds api response exceeded byte ceiling for sport={normalized_sport_key}"
                ) from None
            self._record_quota(index, response)
            if response.status_code == 401:
                self._rejected_keys += 1
                self._cooldown_until[index] = now + _INVALID_KEY_COOLDOWN
                logger.warning("odds api key slot %d rejected (status 401); cooling down", index)
                continue
            if response.status_code == 429:
                self._rate_limited += 1
                delay = _retry_after_seconds(response.headers.get("Retry-After"), now)
                cooldown = timedelta(
                    seconds=delay
                    if delay is not None
                    else _DEFAULT_RATE_LIMIT_COOLDOWN.total_seconds()
                )
                self._cooldown_until[index] = now + cooldown
                logger.warning("odds api key slot %d rate-limited; rotating", index)
                continue
            if response.status_code != 200:
                # Never include response.url (carries apiKey) in the error.
                raise OddsApiError(
                    "odds api returned status "
                    f"{response.status_code} for sport={normalized_sport_key}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise OddsApiError(
                    f"odds api returned non-JSON for sport={normalized_sport_key}"
                ) from exc
            if not isinstance(payload, list):
                raise OddsApiError(
                    f"odds api returned invalid JSON container for sport={normalized_sport_key}"
                )
            if len(payload) > MAX_EVENTS_PER_RESPONSE:
                raise OddsApiError(
                    f"odds api event ceiling exceeded for sport={normalized_sport_key}"
                )
            self._cooldown_until.pop(index, None)
            return self._parse(payload, sport_key=normalized_sport_key)
        raise OddsApiError(f"no usable odds api key available for sport={normalized_sport_key}")

    def _next_key_index(self, now: datetime, attempted: set[int]) -> int | None:
        for offset in range(len(self._keys)):
            index = (self._cursor + offset) % len(self._keys)
            if index in attempted:
                continue
            if self._cooldown_until.get(index, now) > now:
                continue
            return index
        return None

    async def _request_with_status_retry(self, url: str, key: str) -> httpx.Response:
        params = {
            "apiKey": key,
            "regions": self._regions,
            "markets": self._markets,
            "oddsFormat": "decimal",
        }
        for attempt in range(_MAX_STATUS_ATTEMPTS):
            self._requests += 1
            try:
                response = await self._get(url, params=params)
            except httpx.TransportError:
                if attempt + 1 == _MAX_STATUS_ATTEMPTS:
                    raise
                self._retries += 1
                await self._sleep_fn(0.5 * (2**attempt))
                continue
            if response.status_code not in _TRANSIENT_STATUSES:
                return response
            if attempt + 1 == _MAX_STATUS_ATTEMPTS:
                return response
            self._retries += 1
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"), self._now_fn())
            await self._sleep_fn(retry_after if retry_after is not None else 0.5 * (2**attempt))
        raise AssertionError("status retry loop exhausted without a response")

    def _record_quota(self, index: int, response: httpx.Response) -> None:
        raw = response.headers.get("x-requests-remaining")
        if raw is None:
            return
        try:
            remaining = int(raw)
        except ValueError:
            return
        if remaining >= 0:
            self._quota_remaining[index] = remaining

    def _parse(
        self,
        payload: list[dict[str, Any]],
        *,
        sport_key: str = "",
    ) -> list[OddsSnapshotIn]:
        now_fn = getattr(self, "_now_fn", None)
        now = now_fn() if callable(now_fn) else datetime.now(tz=UTC)
        snapshots: list[OddsSnapshotIn] = []
        for event in payload:
            if not isinstance(event, Mapping):
                self._drop_schema_row()
                continue
            raw_event_id = event.get("id")
            if not isinstance(raw_event_id, (str, int)) or isinstance(raw_event_id, bool):
                self._drop_schema_row()
                continue
            event_id = _bounded_text(raw_event_id, max_bytes=MAX_EVENT_ID_BYTES)
            if event_id is None:
                self._drop_schema_row()
                continue
            directory = getattr(self, "_directory", None)
            if directory is not None:
                teams = _event_teams(event, sport_key=sport_key)
                if teams is None:
                    # The pick pipeline rejects an unknown kickoff, and snapshot
                    # persistence cannot resolve an event without team context.
                    # Drop the whole malformed event rather than returning odds
                    # that look healthy but can never become actionable.
                    self._drop_schema_row()
                    continue
                directory.register(event_id, teams)
            for bookmaker in _mapping_rows(event.get("bookmakers")):
                raw_book_key = bookmaker.get("key")
                normalized_book_key = _bounded_text(
                    raw_book_key,
                    max_bytes=MAX_BOOKMAKER_KEY_BYTES,
                    strings_only=True,
                )
                if normalized_book_key is None:
                    self._drop_schema_row()
                    continue
                book_key = _canonical_book(normalized_book_key)
                raw_last_update = bookmaker.get("last_update")
                last_update = (
                    _parse_ts(raw_last_update) if isinstance(raw_last_update, str) else None
                )
                if last_update is None or last_update > now + timedelta(minutes=5):
                    self._drop_schema_row()
                    continue
                for market in _mapping_rows(bookmaker.get("markets")):
                    market_key = str(market.get("key", ""))
                    mapped = _MARKET_MAP.get(market_key)
                    if mapped is None:
                        continue
                    for outcome in _mapping_rows(market.get("outcomes")):
                        price = outcome.get("price")
                        name = _bounded_text(
                            outcome.get("name"),
                            max_bytes=MAX_SELECTION_BYTES,
                            strings_only=True,
                        )
                        point = outcome.get("point")
                        if (
                            not isinstance(price, int | float)
                            or isinstance(price, bool)
                            or not math.isfinite(float(price))
                            or not 1.0 < float(price) <= 1_000.0
                            or name is None
                        ):
                            self._drop_schema_row()
                            continue
                        if point is not None and (
                            not isinstance(point, int | float)
                            or isinstance(point, bool)
                            or not math.isfinite(float(point))
                        ):
                            self._drop_schema_row()
                            continue
                        if mapped in {Market.SPREADS, Market.TOTALS}:
                            # Direct devig downstream assumes a two-outcome,
                            # mutually-exclusive market. Integer lines carry a
                            # push outcome and quarter lines split the stake;
                            # neither has binary payoff semantics. Keep only
                            # true half-lines until push/split-aware pricing is
                            # implemented rather than manufacturing false EV.
                            if point is None or not _is_half_line(float(point)):
                                self._drop_schema_row()
                                continue
                        elif point is not None:
                            # A point-qualified h2h row is an unknown product,
                            # not a safe match-winner price.
                            self._drop_schema_row()
                            continue
                        # M136 (audit 2026-07-10): spreads selections must carry an
                        # EXPLICIT sign — settlement's _SIGNED_LINE_RE rejects an
                        # unsigned positive line ("Patriots 3.5"), leaving the pick
                        # permanently unsettleable. Totals stay unsigned ("Over 2.5").
                        if point is None:
                            selection = name
                        elif mapped is Market.SPREADS:
                            selection = f"{name} {float(point):+g}"
                        else:
                            selection = f"{name} {point}"
                        # Line-qualified devig group (audit #1): without a per-line
                        # market_detail, distinct totals/spreads lines (Over 2.5 vs
                        # Over 3.5) collapse into ONE devig group and corrupt the
                        # fair. Totals share the point across Over/Under; spreads are
                        # ±point opposite sides of the SAME line -> normalize via abs
                        # so the two sides group together.
                        # Parser audit 2026-07-03 F2: a bare number ("2.5") is
                        # persisted verbatim by snapshot_market_key but cannot
                        # be reversed by market_from_snapshot_key -> the row is
                        # silently skipped in every close/devig reconstruction.
                        # Emit the OddsPortal key vocabulary instead (the
                        # reverse mapper's single source of truth). Grouping is
                        # unchanged: totals share the point across Over/Under;
                        # spreads keep abs so both sides of one line stay one
                        # devig group (side identity rides the selection).
                        detail: str | None
                        if point is None:
                            detail = None
                        elif mapped is Market.SPREADS:
                            detail = f"asian_handicap_{_key_number(abs(float(point)))}"
                        else:
                            detail = f"over_under_{_key_number(float(point))}"
                        if len(snapshots) >= MAX_SNAPSHOTS_PER_RESPONSE:
                            raise OddsApiError("odds api snapshot ceiling exceeded")
                        snapshots.append(
                            OddsSnapshotIn(
                                event_id=event_id,
                                bookmaker=book_key,
                                market=mapped,
                                selection=selection,
                                market_detail=detail,
                                decimal_odds=float(price),
                                captured_at=last_update,
                                ingested_at=now,
                            )
                        )
        return snapshots

    def _drop_schema_row(self) -> None:
        # Some pure-parser tests instantiate via __new__; keep that supported.
        self._schema_rows_dropped = getattr(self, "_schema_rows_dropped", 0) + 1


def _validate_api_keys(api_keys: Sequence[str]) -> tuple[str, ...]:
    """Validate secret slots without ever interpolating a key into an error."""
    if isinstance(api_keys, (str, bytes, bytearray)) or not api_keys:
        raise ValueError("at least one Odds API key is required")
    if len(api_keys) > MAX_API_KEY_SLOTS:
        raise ValueError(f"at most {MAX_API_KEY_SLOTS} Odds API key slots are supported")

    validated: list[str] = []
    seen: set[str] = set()
    for key in api_keys:
        if (
            not isinstance(key, str)
            or not key
            or key.strip() != key
            or any(character.isspace() for character in key)
            or len(key.encode("utf-8")) > MAX_API_KEY_BYTES
        ):
            raise ValueError(
                f"each Odds API key must contain 1..{MAX_API_KEY_BYTES} non-whitespace bytes"
            )
        if key in seen:
            raise ValueError("Odds API key slots must be unique")
        seen.add(key)
        validated.append(key)
    return tuple(validated)


def _bounded_text(
    value: object,
    *,
    max_bytes: int,
    strings_only: bool = False,
) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if strings_only and not isinstance(value, str):
        return None
    if not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > max_bytes
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        return None
    return normalized


def _validate_sport_key(sport_key: str) -> str:
    normalized = _bounded_text(
        sport_key,
        max_bytes=MAX_SPORT_KEY_BYTES,
        strings_only=True,
    )
    if normalized is None or any(
        not (character.isascii() and (character.isalnum() or character in {"_", "-"}))
        for character in normalized
    ):
        raise OddsApiError("invalid Odds API sport key")
    return normalized


def _event_teams(event: Mapping[str, Any], *, sport_key: str) -> EventTeams | None:
    home = _bounded_text(
        event.get("home_team"),
        max_bytes=MAX_TEAM_NAME_BYTES,
        strings_only=True,
    )
    away = _bounded_text(
        event.get("away_team"),
        max_bytes=MAX_TEAM_NAME_BYTES,
        strings_only=True,
    )
    league = _bounded_text(
        sport_key,
        max_bytes=MAX_SPORT_KEY_BYTES,
        strings_only=True,
    )
    raw_kickoff = event.get("commence_time")
    kickoff = _parse_ts(raw_kickoff) if isinstance(raw_kickoff, str) else None
    if (
        home is None
        or away is None
        or league is None
        or kickoff is None
        or home.casefold() == away.casefold()
    ):
        return None
    return EventTeams(home=home, away=away, league=league, starts_at=kickoff)


def _key_number(value: float) -> str:
    """2.5 -> "2_5"; 10.25 -> "10_25" — the OddsPortal market-key number form."""
    return f"{value:g}".replace(".", "_")


def _is_half_line(value: float) -> bool:
    doubled = abs(value) * 2.0
    nearest = round(doubled)
    return math.isclose(doubled, nearest, rel_tol=0.0, abs_tol=1e-9) and nearest % 2 == 1


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Parser audit 2026-07-03 F10: an offset-less string parsed NAIVE and
    # flowed into captured_at (naive datetime = bug). The API documents UTC;
    # coerce explicitly like the OddsPortal parser does.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _mapping_rows(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    if len(value) > MAX_NESTED_ROWS:
        raise OddsApiError("odds api nested row ceiling exceeded")
    return tuple(row for row in value if isinstance(row, Mapping))


def _retry_after_seconds(raw: str | None, now: datetime) -> float | None:
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        pass
    else:
        if not math.isfinite(seconds):
            return None
        return min(_MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    seconds = (parsed.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(_MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))
