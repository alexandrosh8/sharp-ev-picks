from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.ingestion.base import EventDirectory
from app.ingestion.odds_api import OddsApiClient, OddsApiError

NOW = datetime(2026, 7, 10, 12, 5, tzinfo=UTC)


def _payload(*, last_update: str = "2026-07-10T12:00:00Z") -> list[dict[str, Any]]:
    return [
        {
            "id": "ev1",
            "commence_time": "2026-07-11T19:00:00Z",
            "home_team": "Alpha",
            "away_team": "Beta",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "last_update": last_update,
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Alpha", "price": 2.1},
                                {"name": "Beta", "price": 1.8},
                            ],
                        }
                    ],
                }
            ],
        }
    ]


def _client(
    handler: Any,
    *,
    keys: tuple[str, ...] = ("key-a", "key-b"),
    sleep_fn: Any | None = None,
    directory: EventDirectory | None = None,
) -> OddsApiClient:
    async def no_sleep(seconds: float) -> None:  # noqa: ARG001
        return None

    return OddsApiClient(
        keys,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="https://odds.invalid/v4",
        directory=directory,
        now_fn=lambda: NOW,
        sleep_fn=sleep_fn or no_sleep,
    )


def test_spread_selections_carry_explicit_sign() -> None:
    """Audit 2026-07-10 (M136): settlement's _SIGNED_LINE_RE requires a signed
    line, so an unsigned positive spread ("Patriots 3.5") is permanently
    unsettleable. Spreads must format the point with an explicit sign; totals
    stay unsigned."""
    from app.ingestion.odds_api import OddsApiClient

    payload = [
        {
            "id": "ev1",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "last_update": "2026-07-10T12:00:00Z",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Patriots", "point": 3.5, "price": 1.95},
                                {"name": "Jets", "point": -3.5, "price": 1.95},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "point": 45.5, "price": 1.9},
                                {"name": "Under", "point": 45.5, "price": 1.9},
                            ],
                        },
                    ],
                }
            ],
        }
    ]
    snaps = OddsApiClient._parse(OddsApiClient.__new__(OddsApiClient), payload)
    by_sel = {s.selection for s in snaps}
    assert "Patriots +3.5" in by_sel
    assert "Jets -3.5" in by_sel
    assert "Over 45.5" in by_sel  # totals unsigned, unchanged


def test_push_and_split_lines_are_not_promoted_to_binary_direct_markets() -> None:
    payload = [
        {
            "id": "ev1",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "last_update": "2026-07-10T12:00:00Z",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Alpha", "point": -3, "price": 1.91},
                                {"name": "Beta", "point": 3, "price": 1.91},
                                {"name": "Alpha", "point": -2.25, "price": 1.95},
                                {"name": "Beta", "point": 2.25, "price": 1.95},
                                {"name": "Alpha", "point": -3.5, "price": 1.9},
                                {"name": "Beta", "point": 3.5, "price": 1.9},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "point": 44, "price": 1.9},
                                {"name": "Under", "point": 44, "price": 1.9},
                                {"name": "Over", "point": 44.25, "price": 1.9},
                                {"name": "Under", "point": 44.25, "price": 1.9},
                                {"name": "Over", "point": 44.5, "price": 1.9},
                                {"name": "Under", "point": 44.5, "price": 1.9},
                            ],
                        },
                    ],
                }
            ],
        }
    ]
    client = _client(lambda request: httpx.Response(200, json=[]))
    snapshots = client._parse(payload)

    assert {snapshot.selection for snapshot in snapshots} == {
        "Alpha -3.5",
        "Beta +3.5",
        "Over 44.5",
        "Under 44.5",
    }
    assert client.metrics.schema_rows_dropped == 8


async def test_429_uses_retry_after_cooldown_and_rotates_without_reusing_key() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.params["apiKey"]
        calls.append(key)
        if key == "key-a":
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json=_payload(), headers={"x-requests-remaining": "37"})

    client = _client(handler)
    assert len(await client.fetch_odds("soccer")) == 2
    assert calls == ["key-a", "key-b"]
    assert client._cooldown_until[0] == NOW + timedelta(seconds=60)
    # key-a is still cooling down, so the next cycle starts with key-b rather
    # than repeatedly spending a request on the exhausted first key.
    await client.fetch_odds("soccer")
    assert calls == ["key-a", "key-b", "key-b"]
    assert client.metrics.rate_limited == 1
    assert client.metrics.quota_remaining_by_key == {1: 37}


def test_retry_after_rejects_nonfinite_and_caps_huge_values() -> None:
    from app.ingestion.odds_api import _retry_after_seconds

    assert _retry_after_seconds("inf", NOW) is None
    assert _retry_after_seconds("NaN", NOW) is None
    assert _retry_after_seconds("999999999", NOW) == 60.0
    assert _retry_after_seconds("Wed, 21 Oct 2099 07:28:00 GMT", NOW) == 60.0


async def test_success_cursor_rotates_starting_key_across_cycles() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["apiKey"])
        return httpx.Response(200, json=[])

    client = _client(handler)
    await client.fetch_odds("soccer")
    await client.fetch_odds("soccer")
    assert calls == ["key-a", "key-b"]


async def test_5xx_retries_with_backoff_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[])

    client = _client(handler, keys=("key-a",), sleep_fn=sleep)
    assert await client.fetch_odds("soccer") == []
    assert calls == 3
    assert sleeps == [2.0, 2.0]
    assert client.metrics.retries == 2


async def test_transport_failure_never_exposes_api_key() -> None:
    secret = "super-secret-api-key"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(f"failed request: {request.url}", request=request)

    client = _client(handler, keys=(secret,))
    with pytest.raises(OddsApiError) as raised:
        await client.fetch_odds("soccer")

    assert secret not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert calls == 3  # three total attempts, not three retries plus the initial request


async def test_invalid_json_container_is_not_empty_success() -> None:
    client = _client(lambda request: httpx.Response(200, json={"events": []}))
    with pytest.raises(OddsApiError, match="invalid JSON container"):
        await client.fetch_odds("soccer")


async def test_response_byte_ceiling_fails_closed_without_key_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion import odds_api

    monkeypatch.setattr(odds_api, "MAX_RESPONSE_BYTES", 8)
    client = _client(lambda request: httpx.Response(200, content=b"x" * 9), keys=("secret",))
    with pytest.raises(OddsApiError, match="byte ceiling") as raised:
        await client.fetch_odds("soccer")
    assert "secret" not in str(raised.value)


async def test_event_result_ceiling_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ingestion import odds_api

    monkeypatch.setattr(odds_api, "MAX_EVENTS_PER_RESPONSE", 1)
    client = _client(lambda request: httpx.Response(200, json=[{"id": "a"}, {"id": "b"}]))
    with pytest.raises(OddsApiError, match="event ceiling"):
        await client.fetch_odds("soccer")


@pytest.mark.parametrize(
    "keys",
    [
        (" ",),
        ("key with spaces",),
        ("x" * 257,),
        ("duplicate-key", "duplicate-key"),
    ],
)
def test_api_key_slots_reject_blank_oversized_whitespace_and_duplicates(
    keys: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError) as raised:
        _client(lambda request: httpx.Response(200, json=[]), keys=keys)
    message = str(raised.value)
    assert "key with spaces" not in message
    assert "duplicate-key" not in message
    assert "x" * 32 not in message


def test_api_key_fanout_is_bounded_to_sixteen_slots() -> None:
    accepted = tuple(f"key-{index}" for index in range(16))
    assert len(_client(lambda request: httpx.Response(200, json=[]), keys=accepted)._keys) == 16

    rejected = (*accepted, "key-16")
    with pytest.raises(ValueError, match="at most 16"):
        _client(lambda request: httpx.Response(200, json=[]), keys=rejected)


@pytest.mark.parametrize("sport_key", ["", "../soccer", "soccer?apiKey=leak", "x" * 101])
async def test_sport_key_is_bounded_and_cannot_escape_url_path(sport_key: str) -> None:
    client = _client(lambda request: httpx.Response(200, json=[]), keys=("key-a",))
    with pytest.raises(OddsApiError, match="invalid Odds API sport key"):
        await client.fetch_odds(sport_key)


async def test_fetch_registers_validated_team_and_kickoff_metadata() -> None:
    directory = EventDirectory()
    client = _client(
        lambda request: httpx.Response(200, json=_payload()),
        keys=("key-a",),
        directory=directory,
    )

    snapshots = await client.fetch_odds("soccer_epl")

    assert len(snapshots) == 2
    teams = directory.lookup("ev1")
    assert teams is not None
    assert teams.home == "Alpha"
    assert teams.away == "Beta"
    assert teams.league == "soccer_epl"
    assert teams.starts_at == datetime(2026, 7, 11, 19, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commence_time", None),
        ("commence_time", "not-a-timestamp"),
        ("home_team", ""),
        ("home_team", "x" * 201),
        ("away_team", "Alpha"),
    ],
)
async def test_event_with_unresolvable_identity_is_dropped_when_directory_is_required(
    field: str,
    value: object,
) -> None:
    payload = _payload()
    payload[0][field] = value
    directory = EventDirectory()
    client = _client(
        lambda request: httpx.Response(200, json=payload),
        keys=("key-a",),
        directory=directory,
    )

    assert await client.fetch_odds("soccer_epl") == []
    assert len(directory) == 0
    assert client.metrics.schema_rows_dropped == 1


def test_malformed_nested_rows_are_isolated_from_valid_siblings() -> None:
    client = _client(lambda request: httpx.Response(200, json=[]))
    payload: list[Any] = [
        "bad-event",
        {"id": None, "bookmakers": []},
        {"id": "bad-books", "bookmakers": "scalar"},
        {
            "id": "bad-nesting",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "last_update": "2026-07-10T12:00:00Z",
                    "markets": [None, {"key": "h2h", "outcomes": [None, "bad"]}],
                }
            ],
        },
        *_payload(),
    ]
    snaps = client._parse(payload)
    assert {snap.selection for snap in snaps} == {"Alpha", "Beta"}
    assert client.metrics.schema_rows_dropped >= 2


def test_future_timestamp_and_nonfinite_price_are_dropped() -> None:
    client = _client(lambda request: httpx.Response(200, json=[]))
    future = _payload(last_update=(NOW + timedelta(minutes=6)).isoformat())
    invalid_price = _payload()
    invalid_price[0]["id"] = "ev2"
    invalid_price[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = float("inf")
    snaps = client._parse([*future, *invalid_price])
    assert {snap.selection for snap in snaps} == {"Beta"}
    assert client.metrics.schema_rows_dropped >= 2
