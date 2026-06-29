"""STRICTLY READ-ONLY Betfair Exchange API client — MockTransport tests.

No live network and no real credentials anywhere. Covers: login/session/
keepAlive/expiry-relogin; listMarketCatalogue + listMarketBook parsing;
match_event_hardened integration (matched -> anchor, unmatched -> skipped);
shadow mode never replaces the OddsPortal anchor; default-off == fully inert;
secret hygiene; and a structural assertion that NO order/account method names
exist in the module.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.edge.value import SHARP_BOOKS
from app.ingestion import betfair_api
from app.ingestion.betfair_api import (
    IDENTITY_KEEPALIVE_URL,
    IDENTITY_LOGIN_URL,
    IDENTITY_LOGOUT_URL,
    JSON_RPC_URL,
    SHADOW_BOOKMAKER,
    BetfairApiClient,
    BetfairApiError,
    BetfairApiShadowCapture,
    BetfairAuthError,
    build_shadow_capture,
    join_match_odds,
    parse_market_book_backs,
    parse_market_catalogue,
)
from app.resolution.matching import EventCandidate, default_aliases

APP_KEY = "appkey-test-123"
USERNAME = "punter@example.test"
PASSWORD = "s3cr3t-PASSWORD-never-log"

KICKOFF = datetime(2026, 6, 30, 18, 0, tzinfo=UTC)

CATALOGUE_RESULT: list[dict[str, Any]] = [
    {
        "marketId": "1.234567",
        "marketStartTime": "2026-06-30T18:00:00.000Z",
        "event": {"id": "30001", "name": "Alpha FC v Beta United"},
        "competition": {"name": "English Premier League"},
        "runners": [
            {"selectionId": 111, "runnerName": "Alpha FC", "sortPriority": 1},
            {"selectionId": 222, "runnerName": "Beta United", "sortPriority": 2},
            {"selectionId": 58805, "runnerName": "The Draw", "sortPriority": 3},
        ],
    }
]

BOOK_RESULT: list[dict[str, Any]] = [
    {
        "marketId": "1.234567",
        "runners": [
            {
                "selectionId": 111,
                "ex": {
                    "availableToBack": [{"price": 2.5, "size": 100}, {"price": 2.48, "size": 50}]
                },
            },
            {"selectionId": 222, "ex": {"availableToBack": [{"price": 3.1, "size": 80}]}},
            {"selectionId": 58805, "ex": {"availableToBack": [{"price": 3.6, "size": 40}]}},
        ],
    }
]


class MockBetfair:
    """Records requests and answers the identity + JSON-RPC endpoints. RPC results
    are keyed by operation name; a list of envelopes is consumed across successive
    calls (so a session-expiry envelope can precede a success on the same op)."""

    def __init__(
        self,
        *,
        login_status: str = "SUCCESS",
        login_error: str = "",
        login_tokens: list[str] | None = None,
        rpc_results: dict[str, Any] | None = None,
    ) -> None:
        self.login_count = 0
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self._login_status = login_status
        self._login_error = login_error
        self._login_tokens = login_tokens or ["ssoid-token-1"]
        self._rpc: dict[str, list[Any]] = {}
        for op, value in (rpc_results or {}).items():
            self._rpc[op] = list(value) if isinstance(value, list) else [value]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((url, dict(request.headers), request.content))
        if url == IDENTITY_LOGIN_URL:
            idx = min(self.login_count, len(self._login_tokens) - 1)
            token = self._login_tokens[idx]
            self.login_count += 1
            return httpx.Response(
                200,
                json={"token": token, "status": self._login_status, "error": self._login_error},
            )
        if url == IDENTITY_KEEPALIVE_URL:
            return httpx.Response(200, json={"token": "ka", "status": "SUCCESS", "error": ""})
        if url == IDENTITY_LOGOUT_URL:
            return httpx.Response(200, json={"status": "SUCCESS", "error": ""})
        if url == JSON_RPC_URL:
            body = json.loads(request.content)
            op = str(body["method"]).rsplit("/", 1)[-1]
            queue = self._rpc.get(op)
            if not queue:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})
            envelope = queue.pop(0) if len(queue) > 1 else queue[0]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, **envelope})
        return httpx.Response(404)


def make_client(mock: MockBetfair) -> BetfairApiClient:
    transport = httpx.MockTransport(mock.handler)
    return BetfairApiClient(
        app_key=APP_KEY,
        username=USERNAME,
        password=PASSWORD,
        client=httpx.AsyncClient(transport=transport),
    )


def _session_expired_envelope() -> dict[str, Any]:
    return {
        "error": {
            "code": -32099,
            "message": "ANGX-0003",
            "data": {"APINGException": {"errorCode": "INVALID_SESSION_INFORMATION"}},
        }
    }


# --- session ---------------------------------------------------------------- #
async def test_login_success_sets_session_and_sends_app_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock = MockBetfair(login_tokens=["ssoid-abc"])
    client = make_client(mock)
    with caplog.at_level("INFO"):
        await client.login()
    assert client.has_session is True
    login_url, login_headers, login_body = mock.requests[0]
    assert login_url == IDENTITY_LOGIN_URL
    assert login_headers["x-application"] == APP_KEY  # app key identifies the app
    # The credentials travel in the login POST body (that IS the auth) — never logged.
    assert b"username=" in login_body and b"password=" in login_body
    assert PASSWORD not in caplog.text
    assert PASSWORD not in login_url
    # The session token is held in memory only — never logged.
    assert "ssoid-abc" not in caplog.text


async def test_login_failure_raises_without_leaking_password() -> None:
    mock = MockBetfair(login_status="FAIL", login_error="INVALID_USERNAME_OR_PASSWORD")
    client = make_client(mock)
    with pytest.raises(BetfairAuthError) as excinfo:
        await client.login()
    message = str(excinfo.value)
    assert "INVALID_USERNAME_OR_PASSWORD" in message  # category is safe to surface
    assert PASSWORD not in message
    assert USERNAME not in message
    assert client.has_session is False


async def test_keepalive_and_logout_manage_session() -> None:
    mock = MockBetfair()
    client = make_client(mock)
    await client.login()
    assert client.has_session is True
    await client.keep_alive()
    assert client.has_session is True
    assert any(u == IDENTITY_KEEPALIVE_URL for u, _, _ in mock.requests)
    await client.logout()
    assert client.has_session is False
    assert any(u == IDENTITY_LOGOUT_URL for u, _, _ in mock.requests)


async def test_session_expiry_triggers_single_relogin() -> None:
    # First catalogue call returns a session-expiry error, then succeeds after relogin.
    mock = MockBetfair(
        login_tokens=["ssoid-1", "ssoid-2"],
        rpc_results={
            "listMarketCatalogue": [_session_expired_envelope(), {"result": CATALOGUE_RESULT}]
        },
    )
    client = make_client(mock)
    markets = await client.list_market_catalogue(
        event_type_ids=["1"],
        market_start_from=KICKOFF - timedelta(hours=1),
        market_start_to=KICKOFF + timedelta(hours=1),
    )
    assert mock.login_count == 2  # initial + exactly one relogin
    assert len(markets) == 1
    assert markets[0].market_id == "1.234567"


# --- parsing ---------------------------------------------------------------- #
def test_parse_market_catalogue_resolves_runners_and_utc_start() -> None:
    markets = parse_market_catalogue(CATALOGUE_RESULT)
    assert len(markets) == 1
    market = markets[0]
    assert market.event_name == "Alpha FC v Beta United"
    assert market.competition == "English Premier League"
    assert market.market_start_time == KICKOFF
    assert market.market_start_time is not None and market.market_start_time.tzinfo is not None
    by_priority = {r.sort_priority: r for r in market.runners}
    assert by_priority[1].name == "Alpha FC"
    assert by_priority[2].name == "Beta United"
    assert any(r.selection_id == 58805 for r in market.runners)


def test_parse_market_book_best_back_is_highest_price() -> None:
    backs = parse_market_book_backs(BOOK_RESULT)
    assert backs["1.234567"][111] == 2.5  # max(2.5, 2.48)
    assert backs["1.234567"][222] == 3.1
    assert backs["1.234567"][58805] == 3.6


def test_join_match_odds_maps_home_away_draw() -> None:
    odds = join_match_odds(
        parse_market_catalogue(CATALOGUE_RESULT), parse_market_book_backs(BOOK_RESULT)
    )
    assert len(odds) == 1
    o = odds[0]
    assert o.home == "Alpha FC" and o.home_back == 2.5
    assert o.away == "Beta United" and o.away_back == 3.1
    assert o.draw_back == 3.6
    assert o.kickoff == KICKOFF


async def test_fetch_match_odds_joins_catalogue_and_book() -> None:
    mock = MockBetfair(
        rpc_results={
            "listMarketCatalogue": {"result": CATALOGUE_RESULT},
            "listMarketBook": {"result": BOOK_RESULT},
        }
    )
    client = make_client(mock)
    odds = await client.fetch_match_odds(
        market_start_from=KICKOFF - timedelta(hours=1),
        market_start_to=KICKOFF + timedelta(hours=1),
    )
    assert len(odds) == 1
    assert odds[0].home_back == 2.5 and odds[0].draw_back == 3.6


async def test_list_market_book_backs_batches_under_weight_cap() -> None:
    # Betfair listMarketBook caps at 200 weight-points/request; a single all-markets
    # call returns TOO_MUCH_DATA. 60 markets must split into <=25-market calls.
    mock = MockBetfair(rpc_results={"listMarketBook": {"result": []}})
    client = make_client(mock)
    await client.login()
    ids = [f"1.{i:06d}" for i in range(60)]
    await client.list_market_book_backs(ids)
    book_batches = [
        json.loads(content)["params"]["marketIds"]
        for (url, _headers, content) in mock.requests
        if url == JSON_RPC_URL and json.loads(content)["method"].endswith("listMarketBook")
    ]
    assert len(book_batches) == 3  # 25 + 25 + 10
    assert all(len(b) <= 25 for b in book_batches)
    assert sum(len(b) for b in book_batches) == 60


# --- error hygiene ---------------------------------------------------------- #
async def test_rpc_http_error_has_no_url_or_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IDENTITY_LOGIN_URL:
            return httpx.Response(
                200, json={"token": "ssoid-secret", "status": "SUCCESS", "error": ""}
            )
        return httpx.Response(500)

    client = BetfairApiClient(
        app_key=APP_KEY,
        username=USERNAME,
        password=PASSWORD,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(BetfairApiError) as excinfo:
        await client.list_market_book_backs(["1.234567"])
    message = str(excinfo.value)
    assert "ssoid-secret" not in message
    assert "api.betfair.com" not in message
    assert "listMarketBook" in message  # op name is safe + useful


def test_requires_all_credentials() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    with pytest.raises(ValueError):
        BetfairApiClient(
            app_key="",
            username=USERNAME,
            password=PASSWORD,
            client=httpx.AsyncClient(transport=transport),
        )


# --- shadow integration ----------------------------------------------------- #
def _shadow_capture(mock: MockBetfair, candidates: list[EventCandidate]) -> BetfairApiShadowCapture:
    client = make_client(mock)
    return BetfairApiShadowCapture(
        client,
        candidates_fn=lambda: candidates,
        window=timedelta(hours=72),
        aliases=default_aliases(),
        now_fn=lambda: KICKOFF - timedelta(hours=6),
    )


def _full_odds_mock() -> MockBetfair:
    return MockBetfair(
        rpc_results={
            "listMarketCatalogue": {"result": CATALOGUE_RESULT},
            "listMarketBook": {"result": BOOK_RESULT},
        }
    )


async def test_shadow_matched_builds_anchor_under_canonical_ref() -> None:
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    report = await _shadow_capture(_full_odds_mock(), candidates).capture_once()
    assert report.markets_fetched == 1
    assert report.matched == 1
    assert report.unmatched == 0
    assert len(report.snapshots) == 3
    assert {s.event_id for s in report.snapshots} == {"evt-canonical-1"}
    assert {s.selection for s in report.snapshots} == {"Alpha FC", "Beta United", "Draw"}


async def test_shadow_unmatched_is_skipped_never_guessed() -> None:
    # A different fixture -> the hardened matcher returns None -> no rows, never a guess.
    candidates = [
        EventCandidate(ref="evt-other", home="Gamma City", away="Delta Town", kickoff=KICKOFF)
    ]
    report = await _shadow_capture(_full_odds_mock(), candidates).capture_once()
    assert report.markets_fetched == 1
    assert report.matched == 0
    assert report.unmatched == 1
    assert report.snapshots == ()


async def test_shadow_never_uses_the_live_betfair_anchor_name() -> None:
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    report = await _shadow_capture(_full_odds_mock(), candidates).capture_once()
    # The OddsPortal-sourced live anchor is "betfair exchange" (a SHARP_BOOK). The
    # shadow rows carry a DISTINCT name that is NOT a sharp book, so they can never
    # replace / be promoted to the live anchor.
    assert SHADOW_BOOKMAKER not in {b.lower() for b in SHARP_BOOKS}
    assert all(s.bookmaker == SHADOW_BOOKMAKER for s in report.snapshots)
    assert all(s.bookmaker != "betfair exchange" for s in report.snapshots)
    # All persisted-nowhere rows are UTC-aware.
    assert all(s.captured_at.tzinfo is not None for s in report.snapshots)


# --- default-off inertness -------------------------------------------------- #
def test_build_shadow_capture_inert_when_disabled() -> None:
    calls: list[str] = []

    def candidates_fn() -> list[EventCandidate]:
        calls.append("called")
        return []

    def no_network(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no network when off")

    client = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
    # Disabled.
    assert (
        build_shadow_capture(
            enabled=False,
            credentials=(APP_KEY, USERNAME, PASSWORD),
            window_hours=72,
            http_client=client,
            candidates_fn=candidates_fn,
        )
        is None
    )
    # Enabled but credentials absent.
    assert (
        build_shadow_capture(
            enabled=True,
            credentials=None,
            window_hours=72,
            http_client=client,
            candidates_fn=candidates_fn,
        )
        is None
    )
    assert calls == []  # nothing ran


# --- structural safety ------------------------------------------------------ #
def test_no_order_or_account_methods_in_module() -> None:
    source = Path(betfair_api.__file__).read_text(encoding="utf-8")
    forbidden = [
        "placeOrder",
        "place_order",
        "placeBets",
        "place_bet",
        "cancelOrder",
        "cancel_order",
        "replaceOrders",
        "updateOrders",
        "listCurrentOrders",
        "listClearedOrders",
        "betfairlightweight",
    ]
    present = [token for token in forbidden if token in source]
    assert present == [], f"forbidden order/account identifiers in module: {present}"
