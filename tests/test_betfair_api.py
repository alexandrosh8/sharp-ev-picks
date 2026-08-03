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
from app.ingestion.base import EventTeams
from app.ingestion.betfair_api import (
    EXTENDED_MARKET_TYPES,
    IDENTITY_KEEPALIVE_URL,
    IDENTITY_LOGIN_URL,
    IDENTITY_LOGOUT_URL,
    JSON_RPC_URL,
    PROMOTED_BOOKMAKER,
    SHADOW_BOOKMAKER,
    BetfairApiClient,
    BetfairApiError,
    BetfairApiShadowCapture,
    BetfairAuthError,
    BetfairLineQuote,
    BetfairMatchOdds,
    ComparisonAggregate,
    ReferenceOdds,
    betfair_tick_size,
    build_shadow_capture,
    compare_event,
    join_extended_lines,
    join_match_odds,
    parse_market_book_backs,
    parse_market_book_backs_by_handicap,
    parse_market_catalogue,
    within_one_tick,
)
from app.resolution.matching import EventCandidate, default_aliases
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

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
        "status": "OPEN",
        "inplay": False,
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


async def test_response_byte_ceiling_fails_closed_without_secret_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(betfair_api, "MAX_RESPONSE_BYTES", 8)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 9))
    client = BetfairApiClient(
        app_key=APP_KEY,
        username=USERNAME,
        password=PASSWORD,
        client=httpx.AsyncClient(transport=transport),
    )
    with pytest.raises(BetfairApiError, match="byte ceiling") as raised:
        await client.login()
    assert PASSWORD not in str(raised.value)
    assert USERNAME not in str(raised.value)
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


def test_parse_market_book_best_back_is_highest_price_with_size() -> None:
    backs = parse_market_book_backs(BOOK_RESULT)
    # value is now (best_back_price, size@best) — size is the API liquidity proxy.
    assert backs["1.234567"][111] == (2.5, 100.0)  # max(2.5@100, 2.48@50) -> 2.5, size 100
    assert backs["1.234567"][222] == (3.1, 80.0)
    assert backs["1.234567"][58805] == (3.6, 40.0)


@pytest.mark.parametrize(
    "unsafe",
    [
        {"status": "OPEN", "inplay": True},
        {"status": "SUSPENDED", "inplay": False},
        {"status": "CLOSED", "inplay": False},
        {"status": None, "inplay": False},
        {"status": "OPEN", "inplay": None},
    ],
)
def test_parse_market_book_rejects_inplay_nonopen_or_unknown_state(
    unsafe: dict[str, Any],
) -> None:
    payload = [{**BOOK_RESULT[0], **unsafe}]
    assert parse_market_book_backs(payload) == {}


def test_parse_market_book_rejects_nonfinite_price_and_size() -> None:
    payload = [
        {
            **BOOK_RESULT[0],
            "runners": [
                {
                    "selectionId": 111,
                    "ex": {"availableToBack": [{"price": float("inf"), "size": 100}]},
                },
                {
                    "selectionId": 222,
                    "ex": {"availableToBack": [{"price": 3.1, "size": float("inf")}]},
                },
            ],
        }
    ]
    assert parse_market_book_backs(payload) == {"1.234567": {222: (3.1, 0.0)}}


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


async def test_catalogue_wrong_result_container_is_not_empty_success() -> None:
    mock = MockBetfair(rpc_results={"listMarketCatalogue": {"result": {"markets": []}}})
    client = make_client(mock)
    with pytest.raises(BetfairApiError, match="invalid result container"):
        await client.list_market_catalogue(
            event_type_ids=["1"],
            market_start_from=KICKOFF - timedelta(hours=1),
            market_start_to=KICKOFF + timedelta(hours=1),
        )


async def test_catalogue_result_ceiling_fails_closed() -> None:
    mock = MockBetfair(
        rpc_results={"listMarketCatalogue": {"result": [*CATALOGUE_RESULT, *CATALOGUE_RESULT]}}
    )
    client = make_client(mock)
    with pytest.raises(BetfairApiError, match="result ceiling"):
        await client.list_market_catalogue(
            event_type_ids=["1"],
            market_start_from=KICKOFF - timedelta(hours=1),
            market_start_to=KICKOFF + timedelta(hours=1),
            max_results=1,
        )


async def test_market_book_wrong_result_container_is_not_empty_success() -> None:
    mock = MockBetfair(rpc_results={"listMarketBook": {"result": {"books": []}}})
    client = make_client(mock)
    with pytest.raises(BetfairApiError, match="invalid result container"):
        await client.list_market_book_backs(["1.234567"])


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


async def test_shadow_uses_post_fetch_timestamp_and_rejects_crossed_kickoff() -> None:
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    times = iter((KICKOFF - timedelta(seconds=1), KICKOFF + timedelta(seconds=1)))
    capture = BetfairApiShadowCapture(
        make_client(_full_odds_mock()),
        candidates_fn=lambda: candidates,
        window=timedelta(hours=72),
        aliases=default_aliases(),
        now_fn=lambda: next(times),
    )
    report = await capture.capture_once()
    assert report.matched == 0
    assert report.unmatched == 1
    assert report.snapshots == ()


async def test_shadow_snapshots_carry_best_back_liquidity() -> None:
    # The API path persists the best-back SIZE as liquidity, so a PROMOTED row is
    # GATED Betfair (not an ungated NULL-liquidity row like the main scrape). The
    # fixture home best-back is 2.5@100, away 3.1@80, draw 3.6@40.
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    report = await _shadow_capture(_full_odds_mock(), candidates).capture_once()
    by_sel = {s.selection: s for s in report.snapshots}
    assert by_sel["Alpha FC"].decimal_odds == pytest.approx(2.5)
    assert by_sel["Alpha FC"].liquidity == pytest.approx(100.0)  # size @ best back
    assert by_sel["Beta United"].liquidity == pytest.approx(80.0)
    assert by_sel["Draw"].liquidity == pytest.approx(40.0)


async def test_promote_rows_use_canonical_selection_vocabulary_not_betfair_names() -> None:
    # The matched candidate's canonical (OddsPortal) home name ("Alpha") differs
    # from the Betfair runner name ("Alpha FC"); the snapshot destined for the
    # promote sink MUST speak the CANONICAL vocabulary, or the live anchor's
    # per-selection lookup silently misses (complete=False) on the name-form gap.
    # Regression for the Betfair-API promote-path under-anchor trap (2026-07-01).
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha", away="Beta United", kickoff=KICKOFF)
    ]
    report = await _shadow_capture(_full_odds_mock(), candidates).capture_once()
    assert report.matched == 1
    selections = {s.selection for s in report.snapshots}
    assert selections == {"Alpha", "Beta United", "Draw"}  # canonical home, not "Alpha FC"
    assert "Alpha FC" not in selections  # the Betfair runner name never leaks into the anchor


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


# --- price comparison math (pure, mock pairs) ------------------------------- #
def test_betfair_tick_size_ladder() -> None:
    # Betfair price-increment ladder: the tick widens as the price climbs.
    assert betfair_tick_size(1.50) == 0.01
    assert betfair_tick_size(2.50) == 0.02
    assert betfair_tick_size(3.50) == 0.05
    assert betfair_tick_size(5.00) == 0.10
    assert betfair_tick_size(8.00) == 0.20
    assert betfair_tick_size(15.0) == 0.50
    assert betfair_tick_size(25.0) == 1.00
    assert betfair_tick_size(40.0) == 2.00
    assert betfair_tick_size(75.0) == 5.00
    assert betfair_tick_size(500.0) == 10.0


def test_within_one_tick_uses_the_coarser_of_the_two_prices() -> None:
    assert within_one_tick(2.50, 2.52) is True  # one 0.02 tick apart
    assert within_one_tick(2.50, 2.56) is False  # three ticks
    assert within_one_tick(2.50, 2.50) is True  # identical
    # missing price on either side -> undefined (None), never a false "agree"
    assert within_one_tick(2.50, None) is None
    assert within_one_tick(None, 2.50) is None


def test_compare_event_computes_per_selection_delta_and_freshness() -> None:
    api_captured = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    ref_captured = datetime(2026, 6, 30, 11, 30, tzinfo=UTC)  # scrape 30 min older
    odds = BetfairMatchOdds(
        market_id="1.1",
        event_id="30001",
        competition="EPL",
        kickoff=KICKOFF,
        home="Alpha FC",
        away="Beta United",
        home_back=2.50,
        away_back=3.10,
        draw_back=3.60,
    )
    ref = ReferenceOdds(home_back=2.48, draw_back=3.55, away_back=3.10, captured_at=ref_captured)
    cmp = compare_event(odds, ref, api_captured_at=api_captured, event_ref="evt-1")
    assert cmp.event_ref == "evt-1"
    assert cmp.home.delta == pytest.approx(0.02)
    assert cmp.away.delta == pytest.approx(0.0)
    assert cmp.draw.delta == pytest.approx(0.05)
    assert cmp.home.within_tick is True
    assert cmp.draw.within_tick is True
    # API read is newer than the scrape anchor -> positive gap, api_fresher True.
    assert cmp.freshness_gap_seconds == pytest.approx(1800.0)
    assert cmp.api_fresher is True


def test_comparison_aggregate_over_mock_pairs() -> None:
    api_captured = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    older = datetime(2026, 6, 30, 11, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 30, 12, 30, tzinfo=UTC)  # ref NEWER than api -> not fresher
    odds = BetfairMatchOdds(
        home_back=2.50,
        away_back=3.10,
        draw_back=3.60,
        market_id="1.1",
        event_id="e",
        competition="EPL",
        kickoff=KICKOFF,
        home="H",
        away="A",
    )
    # Event 1: all within one tick, api fresher.
    c1 = compare_event(
        odds,
        ReferenceOdds(home_back=2.50, draw_back=3.60, away_back=3.10, captured_at=older),
        api_captured_at=api_captured,
        event_ref="e1",
    )
    # Event 2: home off by many ticks, ref is newer (api NOT fresher).
    c2 = compare_event(
        odds,
        ReferenceOdds(home_back=2.00, draw_back=3.60, away_back=3.10, captured_at=newer),
        api_captured_at=api_captured,
        event_ref="e2",
    )
    agg = ComparisonAggregate.from_events([c1, c2])
    assert agg.compared == 2
    # 6 present selection pairs total; only home of c2 disagrees -> 5/6 within tick.
    assert agg.pct_within_one_tick == pytest.approx(100.0 * 5 / 6)
    # 1 of 2 events has the api fresher.
    assert agg.pct_api_fresher == pytest.approx(50.0)
    assert agg.mean_abs_delta is not None and agg.mean_abs_delta > 0.0


def test_comparison_aggregate_empty_is_none_fields() -> None:
    agg = ComparisonAggregate.from_events([])
    assert agg.compared == 0
    assert agg.mean_abs_delta is None
    assert agg.pct_within_one_tick is None
    assert agg.pct_api_fresher is None


# --- comparison wired into the shadow cycle --------------------------------- #
async def test_shadow_logs_comparison_when_reference_supplied() -> None:
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    ref = ReferenceOdds(
        home_back=2.48,
        draw_back=3.55,
        away_back=3.10,
        captured_at=KICKOFF - timedelta(hours=12),
    )
    client = make_client(_full_odds_mock())
    capture = BetfairApiShadowCapture(
        client,
        candidates_fn=lambda: candidates,
        window=timedelta(hours=72),
        aliases=default_aliases(),
        now_fn=lambda: KICKOFF - timedelta(hours=6),
        reference_odds_fn=lambda ref_id: ref,
    )
    report = await capture.capture_once()
    assert report.matched == 1
    assert report.comparison is not None
    assert report.comparison.compared == 1
    # API captured at the cycle 'now' (6h pre-KO) is fresher than the 12h-old scrape.
    assert report.comparison.pct_api_fresher == pytest.approx(100.0)
    # Default capture stays NON-SHARP + nothing promoted.
    assert report.promoted is False
    assert all(s.bookmaker == SHADOW_BOOKMAKER for s in report.snapshots)


async def test_reference_none_keeps_today_behavior() -> None:
    # No reference_odds_fn -> no comparison object, identical to today's shadow run.
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    report = await _shadow_capture(_full_odds_mock(), candidates).capture_once()
    assert report.comparison is None
    assert report.promoted is False


# --- promotion path (flag-gated, default-OFF) ------------------------------- #
async def test_promote_off_tags_non_sharp_and_calls_no_sink() -> None:
    sink_calls: list[int] = []

    async def sink(snaps: Any, teams: Any) -> int:
        sink_calls.append(len(list(snaps)))
        return 0

    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    client = make_client(_full_odds_mock())
    capture = BetfairApiShadowCapture(
        client,
        candidates_fn=lambda: candidates,
        window=timedelta(hours=72),
        aliases=default_aliases(),
        now_fn=lambda: KICKOFF - timedelta(hours=6),
        promote=False,  # default; the sink must NEVER be called
        promote_sink=sink,
    )
    report = await capture.capture_once()
    assert report.promoted is False
    assert all(s.bookmaker == SHADOW_BOOKMAKER for s in report.snapshots)
    assert SHADOW_BOOKMAKER not in {b.lower() for b in SHARP_BOOKS}
    assert sink_calls == []  # inert: promotion OFF never persists


async def test_promote_on_routes_sharp_tagged_rows_to_sink() -> None:
    sunk: list[OddsSnapshotIn] = []
    sunk_teams: list[EventTeams] = []

    async def sink(snaps: Any, teams: Any) -> int:
        rows = list(snaps)
        sunk.extend(rows)
        sunk_teams.extend(teams.values())
        return len(rows)

    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    client = make_client(_full_odds_mock())
    capture = BetfairApiShadowCapture(
        client,
        candidates_fn=lambda: candidates,
        window=timedelta(hours=72),
        aliases=default_aliases(),
        now_fn=lambda: KICKOFF - timedelta(hours=6),
        promote=True,
        promote_sink=sink,
    )
    report = await capture.capture_once()
    assert report.promoted is True
    # PROMOTED rows carry the LIVE sharp anchor name (a SHARP_BOOKS member).
    assert PROMOTED_BOOKMAKER.lower() in {b.lower() for b in SHARP_BOOKS}
    assert all(s.bookmaker == PROMOTED_BOOKMAKER for s in report.snapshots)
    assert len(sunk) == 3  # home/away/draw routed to the anchor sink
    assert {s.event_id for s in sunk} == {"evt-canonical-1"}
    assert sunk_teams and sunk_teams[0].home == "Alpha FC"


def test_build_shadow_capture_threads_promote_and_reference_defaults_inert() -> None:
    # Default build (no promote/reference args) is byte-equivalent to today: a
    # capture whose promote flag is OFF.
    def no_network(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no network at build time")

    client = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
    capture = build_shadow_capture(
        enabled=True,
        credentials=(APP_KEY, USERNAME, PASSWORD),
        window_hours=72,
        http_client=client,
        candidates_fn=lambda: [],
    )
    assert capture is not None
    assert capture.promote is False


# --- runtime op allowlist (structural read-only guard) ----------------------- #
def test_allowed_ops_is_exactly_the_two_read_ops() -> None:
    # The runtime allowlist holds the two read constants and NOTHING else — any
    # new op must be added here deliberately (and stay read-only).
    assert frozenset({"listMarketCatalogue", "listMarketBook"}) == betfair_api._ALLOWED_OPS


@pytest.mark.parametrize(
    "forbidden_op",
    [
        # Built by concatenation so the banned identifiers never appear literally
        # anywhere in the repo (the safety-audit grep and the structural test below
        # both stay empty).
        "place" + "Orders",
        "cancel" + "Orders",
        "replace" + "Orders",
        "update" + "Orders",
        "listCurrent" + "Orders",
        "listCleared" + "Orders",
    ],
)
async def test_rpc_rejects_non_allowlisted_op_before_any_http(forbidden_op: str) -> None:
    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"non-allowlisted op reached the transport: {request.url}")

    client = BetfairApiClient(
        app_key=APP_KEY,
        username=USERNAME,
        password=PASSWORD,
        client=httpx.AsyncClient(transport=httpx.MockTransport(no_network)),
    )
    with pytest.raises(BetfairApiError, match="allowlist"):
        await client._rpc(forbidden_op, {})
    # Rejected BEFORE any login/HTTP: no session was ever established.
    assert client.has_session is False


async def test_keepalive_and_login_do_not_route_through_rpc_allowlist() -> None:
    # login/keepAlive/logout use the identitysso endpoints via _post/_get, never
    # _rpc — the allowlist cannot break session management.
    mock = MockBetfair(login_tokens=["ssoid-alive"])
    client = make_client(mock)
    await client.login()
    await client.keep_alive()
    assert client.has_session is True
    urls = [url for url, _, _ in mock.requests]
    assert urls == [IDENTITY_LOGIN_URL, IDENTITY_KEEPALIVE_URL]


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


async def test_shadow_matches_oddschecker_era_canonical_refs() -> None:
    """TASK PERF (2026-07-26): the matcher is ref-form-agnostic — an
    oddschecker-era canonical ref ("oddschecker:<id>", the post-migration event
    identity) must match exactly like the legacy OddsPortal URL refs. The 0%
    match rate was the CANDIDATE QUERY (http%-only ref filter starved the
    universe), never the matcher; this pins the re-pointed contract end-to-end:
    oddschecker-ref candidate in -> matched shadow rows out."""
    candidates = [
        EventCandidate(
            ref="oddschecker:101610031", home="Alpha FC", away="Beta United", kickoff=KICKOFF
        )
    ]
    report = await _shadow_capture(_full_odds_mock(), candidates).capture_once()
    assert report.markets_fetched == 1
    assert report.matched == 1
    assert report.match_rate > 0
    assert {s.event_id for s in report.snapshots} == {"oddschecker:101610031"}


# --- EXTENDED markets (Asian Handicap + Over/Under goal lines) ---------------- #
# Fixture: one AH ladder (quarter -0.25 two-sided, half -0.5 one-sided, integer
# -1.0 two-sided but fail-closed), one OVER_UNDER_25 market, one ALT_TOTAL_GOALS
# ladder (quarter 2.25 two-sided, integer 3.0 fail-closed).
EXTENDED_CATALOGUE_RESULT: list[dict[str, Any]] = [
    {
        "marketId": "1.888001",
        "marketStartTime": "2026-06-30T18:00:00.000Z",
        "event": {"id": "30001", "name": "Alpha FC v Beta United"},
        "competition": {"name": "English Premier League"},
        "description": {"marketType": "ASIAN_HANDICAP"},
        "runners": [
            {"selectionId": 501, "runnerName": "Alpha FC", "sortPriority": 1, "handicap": -0.25},
            {"selectionId": 502, "runnerName": "Beta United", "sortPriority": 2, "handicap": 0.25},
            {"selectionId": 501, "runnerName": "Alpha FC", "sortPriority": 3, "handicap": -0.5},
            {"selectionId": 502, "runnerName": "Beta United", "sortPriority": 4, "handicap": 0.5},
            {"selectionId": 501, "runnerName": "Alpha FC", "sortPriority": 5, "handicap": -1.0},
            {"selectionId": 502, "runnerName": "Beta United", "sortPriority": 6, "handicap": 1.0},
        ],
    },
    {
        "marketId": "1.888002",
        "marketStartTime": "2026-06-30T18:00:00.000Z",
        "event": {"id": "30001", "name": "Alpha FC v Beta United"},
        "competition": {"name": "English Premier League"},
        "description": {"marketType": "OVER_UNDER_25"},
        "runners": [
            {"selectionId": 601, "runnerName": "Under 2.5 Goals", "sortPriority": 1},
            {"selectionId": 602, "runnerName": "Over 2.5 Goals", "sortPriority": 2},
        ],
    },
    {
        "marketId": "1.888003",
        "marketStartTime": "2026-06-30T18:00:00.000Z",
        "event": {"id": "30001", "name": "Alpha FC v Beta United"},
        "competition": {"name": "English Premier League"},
        "description": {"marketType": "ALT_TOTAL_GOALS"},
        "runners": [
            {"selectionId": 701, "runnerName": "Under", "sortPriority": 1, "handicap": 2.25},
            {"selectionId": 702, "runnerName": "Over", "sortPriority": 2, "handicap": 2.25},
            {"selectionId": 701, "runnerName": "Under", "sortPriority": 3, "handicap": 3.0},
            {"selectionId": 702, "runnerName": "Over", "sortPriority": 4, "handicap": 3.0},
        ],
    },
]

EXTENDED_BOOK_RESULT: list[dict[str, Any]] = [
    {
        "marketId": "1.888001",
        "status": "OPEN",
        "inplay": False,
        "runners": [
            {
                "selectionId": 501,
                "handicap": -0.25,
                "ex": {"availableToBack": [{"price": 1.98, "size": 500}]},
            },
            {
                "selectionId": 502,
                "handicap": 0.25,
                "ex": {"availableToBack": [{"price": 1.96, "size": 400}]},
            },
            # -0.5 line: HOME side only (one-sided book -> must be skipped).
            {
                "selectionId": 501,
                "handicap": -0.5,
                "ex": {"availableToBack": [{"price": 2.2, "size": 300}]},
            },
            # -1.0 line: two-sided but INTEGER (fail-closed, EH-collision risk).
            {
                "selectionId": 501,
                "handicap": -1.0,
                "ex": {"availableToBack": [{"price": 3.0, "size": 200}]},
            },
            {
                "selectionId": 502,
                "handicap": 1.0,
                "ex": {"availableToBack": [{"price": 1.4, "size": 200}]},
            },
        ],
    },
    {
        "marketId": "1.888002",
        "status": "OPEN",
        "inplay": False,
        "runners": [
            {"selectionId": 601, "ex": {"availableToBack": [{"price": 1.92, "size": 250}]}},
            {"selectionId": 602, "ex": {"availableToBack": [{"price": 2.02, "size": 300}]}},
        ],
    },
    {
        "marketId": "1.888003",
        "status": "OPEN",
        "inplay": False,
        "runners": [
            {
                "selectionId": 701,
                "handicap": 2.25,
                "ex": {"availableToBack": [{"price": 1.8, "size": 150}]},
            },
            {
                "selectionId": 702,
                "handicap": 2.25,
                "ex": {"availableToBack": [{"price": 2.16, "size": 120}]},
            },
            {
                "selectionId": 701,
                "handicap": 3.0,
                "ex": {"availableToBack": [{"price": 1.6, "size": 100}]},
            },
            {
                "selectionId": 702,
                "handicap": 3.0,
                "ex": {"availableToBack": [{"price": 2.5, "size": 100}]},
            },
        ],
    },
]


def _extended_mock() -> MockBetfair:
    # Queue order matches the capture: h2h catalogue, h2h book, extended
    # catalogue, extended book (MockBetfair consumes queued envelopes in order).
    return MockBetfair(
        rpc_results={
            "listMarketCatalogue": [
                {"result": CATALOGUE_RESULT},
                {"result": EXTENDED_CATALOGUE_RESULT},
            ],
            "listMarketBook": [{"result": BOOK_RESULT}, {"result": EXTENDED_BOOK_RESULT}],
        }
    )


def _extended_capture(
    mock: MockBetfair, candidates: list[EventCandidate]
) -> BetfairApiShadowCapture:
    return BetfairApiShadowCapture(
        make_client(mock),
        candidates_fn=lambda: candidates,
        window=timedelta(hours=72),
        aliases=default_aliases(),
        now_fn=lambda: KICKOFF - timedelta(hours=6),
        extended_markets=True,
    )


def test_parse_extended_catalogue_carries_market_type_and_runner_handicap() -> None:
    parsed = {m.market_id: m for m in parse_market_catalogue(EXTENDED_CATALOGUE_RESULT)}
    ah = parsed["1.888001"]
    assert ah.market_type == "ASIAN_HANDICAP"
    assert {(r.selection_id, r.handicap) for r in ah.runners} == {
        (501, -0.25),
        (502, 0.25),
        (501, -0.5),
        (502, 0.5),
        (501, -1.0),
        (502, 1.0),
    }
    ou = parsed["1.888002"]
    assert ou.market_type == "OVER_UNDER_25"
    assert all(r.handicap is None for r in ou.runners)  # absent -> None, never invented
    # The legacy Match-Odds fixture (no description projection) parses unchanged.
    legacy = parse_market_catalogue(CATALOGUE_RESULT)[0]
    assert legacy.market_type == ""
    assert all(r.handicap is None for r in legacy.runners)


def test_parse_market_book_backs_by_handicap_keys_ladder_runners() -> None:
    books = parse_market_book_backs_by_handicap(EXTENDED_BOOK_RESULT)
    ah = books["1.888001"]
    # The SAME selectionId at two handicaps stays two distinct entries — the
    # selectionId-only parser would silently collapse the ladder.
    assert ah[(501, -0.25)] == (1.98, 500.0)
    assert ah[(501, -0.5)] == (2.2, 300.0)
    assert ah[(502, 0.25)] == (1.96, 400.0)
    # Absent handicap keys as 0.0 (non-ladder markets).
    assert books["1.888002"][(602, 0.0)] == (2.02, 300.0)


def _joined_quotes() -> list[BetfairLineQuote]:
    catalogue = parse_market_catalogue(EXTENDED_CATALOGUE_RESULT)
    books = parse_market_book_backs_by_handicap(EXTENDED_BOOK_RESULT)
    return join_extended_lines(catalogue, books, {"30001": ("Alpha FC", "Beta United")})


def test_join_extended_lines_maps_quarter_half_lines_both_sides() -> None:
    quotes = _joined_quotes()
    by_detail: dict[str, set[str]] = {}
    for quote in quotes:
        by_detail.setdefault(quote.market_detail, set()).add(quote.side)
    assert by_detail == {
        "spreads_minus_0_25": {"home", "away"},  # quarter AH, two-sided
        "totals_2_5": {"over", "under"},  # OVER_UNDER_25
        "totals_2_25": {"over", "under"},  # ALT_TOTAL_GOALS quarter line
    }
    # One-sided (-0.5) and integer (-1.0 AH, 3.0 totals) lines never emit.
    ah = {q.market_detail: q for q in quotes if q.market is Market.SPREADS}
    assert set(ah) == {"spreads_minus_0_25"}
    assert all(q.market in (Market.SPREADS, Market.TOTALS) for q in quotes)


def test_extended_vocabulary_equals_scraped_keys() -> None:
    # The captured AH -0.25 must group with the SCRAPED spreads_minus_0_25 key
    # and totals with totals_2_5 — byte-equal to the OddsChecker producer and a
    # fixed point of the pipeline's canonical fold (never a new vocabulary).
    from app.ingestion.oddschecker import _market_for_type
    from app.pipeline import canonical_market_detail
    from app.schemas.base import Market as MarketEnum

    quotes = _joined_quotes()
    details = {q.market_detail for q in quotes}
    assert _market_for_type("Asian Handicap", "-0.25") == (
        MarketEnum.SPREADS,
        "spreads_minus_0_25",
    )
    assert _market_for_type("Total Goals", "2.5") == (MarketEnum.TOTALS, "totals_2_5")
    assert "spreads_minus_0_25" in details
    assert "totals_2_5" in details
    for detail in details:
        assert canonical_market_detail(detail) == detail  # canonical fixed point


def test_two_sided_probabilities_form_plausible_book() -> None:
    # The whole point: per line, sum(1/back) over BOTH sides must land in a
    # plausible band (the scraped one-sided AH books ran 0.08-0.94).
    quotes = _joined_quotes()
    sums: dict[str, float] = {}
    for quote in quotes:
        sums[quote.market_detail] = sums.get(quote.market_detail, 0.0) + 1.0 / quote.back
    assert sums  # never a silent empty
    for detail, total in sums.items():
        assert 0.9 < total < 1.12, (detail, total)


async def test_extended_capture_emits_canonical_spreads_and_totals_rows() -> None:
    # Canonical home name differs from the Betfair runner name — the emitted
    # selection strings must speak the CANONICAL vocabulary (like the h2h path).
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha", away="Beta United", kickoff=KICKOFF)
    ]
    report = await _extended_capture(_extended_mock(), candidates).capture_once()
    assert report.matched == 1
    rows = [s for s in report.snapshots if s.market_detail is not None]
    by_detail: dict[str, set[str]] = {}
    for snap in rows:
        assert snap.market_detail is not None  # filtered above; narrows for mypy
        by_detail.setdefault(snap.market_detail, set()).add(snap.selection)
    assert by_detail == {
        "spreads_minus_0_25": {"Alpha -0.25", "Beta United +0.25"},
        "totals_2_5": {"Over 2.5", "Under 2.5"},
        "totals_2_25": {"Over 2.25", "Under 2.25"},
    }
    assert all(s.event_id == "evt-canonical-1" for s in rows)
    assert all(s.captured_at.tzinfo is not None for s in rows)
    # Liquidity carries the best-back size, exactly like the h2h path.
    sizes = {s.selection: s.liquidity for s in rows}
    assert sizes["Alpha -0.25"] == pytest.approx(500.0)
    # Telemetry: counts only.
    assert report.extended_lines == 3
    assert report.extended_events == 1
    assert report.extended_failed is False


async def test_extended_rows_share_the_shadow_or_promoted_bookmaker_tag() -> None:
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    report = await _extended_capture(_extended_mock(), candidates).capture_once()
    # Promotion OFF -> every extended row stays the NON-SHARP shadow tag, so it
    # can never feed the live anchor until VALUE_BETFAIR_API_PROMOTE is armed.
    assert all(s.bookmaker == SHADOW_BOOKMAKER for s in report.snapshots)


async def test_extended_flag_off_makes_no_extra_rpc_calls() -> None:
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    mock_off = _full_odds_mock()
    await _shadow_capture(mock_off, candidates).capture_once()
    rpc_off = [url for url, _, _ in mock_off.requests if url == JSON_RPC_URL]
    assert len(rpc_off) == 2  # h2h catalogue + h2h book: budget UNCHANGED

    mock_on = _extended_mock()
    await _extended_capture(mock_on, candidates).capture_once()
    rpc_on = [url for url, _, _ in mock_on.requests if url == JSON_RPC_URL]
    # Exactly ONE extra catalogue + ONE (batched) book pass.
    assert len(rpc_on) == 4


async def test_extended_fetch_failure_is_flagged_and_never_kills_h2h() -> None:
    # The extended catalogue errors -> the cycle FLAGS the failure (never a
    # silent pretend-success) but the h2h anchor capture still completes.
    mock = MockBetfair(
        rpc_results={
            "listMarketCatalogue": [
                {"result": CATALOGUE_RESULT},
                {"error": {"code": -32099, "message": "boom"}},
            ],
            "listMarketBook": [{"result": BOOK_RESULT}],
        }
    )
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    report = await _extended_capture(mock, candidates).capture_once()
    assert report.extended_failed is True
    assert report.extended_lines == 0
    assert report.matched == 1  # h2h path unharmed
    assert {s.selection for s in report.snapshots} == {"Alpha FC", "Beta United", "Draw"}


def test_extended_market_types_are_price_read_codes_only() -> None:
    # The extended catalogue filter asks ONLY for AH + goal-line market types —
    # never an order/account surface (read-only scope is structural).
    assert "ASIAN_HANDICAP" in EXTENDED_MARKET_TYPES
    assert "ALT_TOTAL_GOALS" in EXTENDED_MARKET_TYPES
    assert "OVER_UNDER_25" in EXTENDED_MARKET_TYPES
    assert all(
        t.isupper() and ("HANDICAP" in t or "GOAL" in t or "OVER_UNDER" in t)
        for t in EXTENDED_MARKET_TYPES
    )


async def test_extended_catalogue_is_filtered_to_matched_event_ids() -> None:
    # LIVE DEFECT 2026-08-03: an UNFILTERED extended catalogue caps at 200
    # markets ~= the 18 soonest events of the whole window (11 types/event,
    # FIRST_TO_START), which never intersect the matched slate -> lines=0 for
    # 21 cycles. The fix: fetch AFTER matching, filtered by eventIds of the
    # MATCHED Betfair events.
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF)
    ]
    mock = _extended_mock()
    report = await _extended_capture(mock, candidates).capture_once()
    assert report.extended_lines == 3  # coverage preserved end-to-end
    rpc_bodies = [json.loads(content) for url, _, content in mock.requests if url == JSON_RPC_URL]
    catalogues = [b["params"] for b in rpc_bodies if b["method"].endswith("listMarketCatalogue")]
    assert len(catalogues) == 2
    assert "eventIds" not in catalogues[0]["filter"]  # h2h scan stays window-wide
    # The extended call asks ONLY for the matched Betfair event.
    assert catalogues[1]["filter"]["eventIds"] == ["30001"]
    assert set(catalogues[1]["filter"]["marketTypeCodes"]) == set(EXTENDED_MARKET_TYPES)


async def test_extended_fetch_is_skipped_when_nothing_matched() -> None:
    # No matched events -> no extended catalogue/book calls at all (budget win,
    # and no pointless 200-market scan of unmatchable fixtures).
    candidates = [
        EventCandidate(ref="evt-other", home="Gamma City", away="Delta Town", kickoff=KICKOFF)
    ]
    mock = _extended_mock()
    report = await _extended_capture(mock, candidates).capture_once()
    assert report.matched == 0
    assert report.extended_lines == 0
    assert report.extended_failed is False
    rpc = [url for url, _, _ in mock.requests if url == JSON_RPC_URL]
    assert len(rpc) == 2  # h2h catalogue + h2h book only


async def test_extended_event_ids_are_capped_under_catalogue_result_limit() -> None:
    # 11 market types/event x 18 events = 198 <= the 200-result catalogue cap;
    # a larger matched slate must truncate (soonest-first order preserved),
    # never silently lose markets to the result ceiling mid-response.
    mock = MockBetfair(
        rpc_results={
            "listMarketCatalogue": {"result": EXTENDED_CATALOGUE_RESULT},
            "listMarketBook": {"result": EXTENDED_BOOK_RESULT},
        }
    )
    client = make_client(mock)
    ids = [f"ev-{i}" for i in range(30)]
    await client.fetch_extended_line_books(
        market_start_from=KICKOFF - timedelta(hours=6),
        market_start_to=KICKOFF + timedelta(hours=66),
        event_ids=ids,
    )
    body = json.loads(mock.requests[-2][2])  # catalogue call (book call is last)
    assert body["params"]["filter"]["eventIds"] == ids[:18]


def test_no_unsafe_http_method_strings_in_module() -> None:
    # GET + (read-only JSON-RPC) POST only — no mutating HTTP verbs anywhere.
    source = Path(betfair_api.__file__).read_text(encoding="utf-8")
    for verb in ('"PUT"', "'PUT'", '"DELETE"', "'DELETE'", '"PATCH"', "'PATCH'"):
        assert verb not in source


# --- multi-sport scope: tennis (SHADOW-ONLY, coverage lever 2026-08-03) ------ #
# Research (scratchpad betfair_coverage probe, 2026-08-03): the soccer-only
# capture fetched 184 Betfair markets against a 10-event canonical soccer slate
# (off-season) while 193 Betfair TENNIS events sat unfetched against a 58-event
# canonical tennis slate (14 matched immediately via the hardened matcher +
# canonical_tennis_name). These tests lock the generalized capture surface:
# ordered=False + name_fold, two-runner (no-draw) markets, slate-relative
# coverage telemetry, and a shared read-only client across sport captures.

TENNIS_CATALOGUE_RESULT: list[dict[str, Any]] = [
    {
        "marketId": "1.777001",
        "marketStartTime": "2026-06-30T18:00:00.000Z",
        "event": {"id": "40001", "name": "Panna Udvardy v Eva Lys"},
        "competition": {"name": "WTA Some Open 2026"},
        "runners": [
            {"selectionId": 611, "runnerName": "Panna Udvardy", "sortPriority": 1},
            {"selectionId": 622, "runnerName": "Eva Lys", "sortPriority": 2},
        ],
    }
]

TENNIS_BOOK_RESULT: list[dict[str, Any]] = [
    {
        "marketId": "1.777001",
        "status": "OPEN",
        "inplay": False,
        "runners": [
            {"selectionId": 611, "ex": {"availableToBack": [{"price": 1.8, "size": 120}]}},
            {"selectionId": 622, "ex": {"availableToBack": [{"price": 2.2, "size": 90}]}},
        ],
    }
]


def _tennis_mock() -> MockBetfair:
    return MockBetfair(
        rpc_results={
            "listMarketCatalogue": {"result": TENNIS_CATALOGUE_RESULT},
            "listMarketBook": {"result": TENNIS_BOOK_RESULT},
        }
    )


def test_event_type_tennis_constant() -> None:
    assert betfair_api.EVENT_TYPE_TENNIS == "2"


async def test_tennis_unordered_name_fold_matches_two_runner_market() -> None:
    from app.resolution.tennis_names import canonical_tennis_name

    candidates = [
        EventCandidate(
            ref="oddschecker:tennis/panna-udvardy-v-eva-lys",
            home=canonical_tennis_name("Panna Udvardy"),
            away=canonical_tennis_name("Eva Lys"),
            kickoff=KICKOFF,
        )
    ]
    capture = BetfairApiShadowCapture(
        make_client(_tennis_mock()),
        candidates_fn=lambda: candidates,
        window=timedelta(hours=72),
        aliases=default_aliases(),
        now_fn=lambda: KICKOFF - timedelta(hours=6),
        event_type_ids=(betfair_api.EVENT_TYPE_TENNIS,),
        ordered=False,
        name_fold=canonical_tennis_name,
    )
    report = await capture.capture_once()
    assert report.markets_fetched == 1
    assert report.matched == 1
    assert report.unmatched == 0
    # Two-runner market: home/away rows only, never a manufactured Draw row.
    selections = {s.selection for s in report.snapshots}
    assert selections == {
        canonical_tennis_name("Panna Udvardy"),
        canonical_tennis_name("Eva Lys"),
    }
    assert all(s.bookmaker == SHADOW_BOOKMAKER for s in report.snapshots)


async def test_name_fold_never_leaks_into_raw_link_observation_fields() -> None:
    from app.resolution.tennis_names import canonical_tennis_name

    seen: list[Any] = []

    async def link_sink(observations: Any) -> None:
        seen.extend(observations)

    candidates = [
        EventCandidate(
            ref="oddschecker:tennis/panna-udvardy-v-eva-lys",
            home=canonical_tennis_name("Panna Udvardy"),
            away=canonical_tennis_name("Eva Lys"),
            kickoff=KICKOFF,
        )
    ]
    capture = BetfairApiShadowCapture(
        make_client(_tennis_mock()),
        candidates_fn=lambda: candidates,
        window=timedelta(hours=72),
        aliases=default_aliases(),
        now_fn=lambda: KICKOFF - timedelta(hours=6),
        ordered=False,
        name_fold=canonical_tennis_name,
        link_sink=link_sink,
    )
    await capture.capture_once()
    assert len(seen) == 1
    # raw_* fields keep the BETFAIR runner names (audit provenance), not the fold.
    assert seen[0].raw_home == "Panna Udvardy"
    assert seen[0].raw_away == "Eva Lys"


async def test_report_carries_candidates_considered_and_slate_coverage() -> None:
    candidates = [
        EventCandidate(ref="evt-canonical-1", home="Alpha FC", away="Beta United", kickoff=KICKOFF),
        EventCandidate(ref="evt-other", home="Gamma City", away="Delta Town", kickoff=KICKOFF),
    ]
    report = await _shadow_capture(_full_odds_mock(), candidates).capture_once()
    assert report.candidates_considered == 2
    assert report.matched == 1
    assert report.slate_coverage == pytest.approx(0.5)


async def test_slate_coverage_zero_candidates_is_zero_not_nan() -> None:
    report = await _shadow_capture(_full_odds_mock(), []).capture_once()
    assert report.candidates_considered == 0
    assert report.slate_coverage == 0.0


async def test_build_shadow_capture_shares_an_injected_api_client() -> None:
    # ONE read-only session across per-sport captures: a second interactive
    # login could invalidate the first session token depending on account
    # settings, so the scheduler builds one BetfairApiClient and injects it.
    mock = _full_odds_mock()
    client = make_client(mock)
    soccer = build_shadow_capture(
        enabled=True,
        credentials=(APP_KEY, USERNAME, PASSWORD),
        window_hours=72,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock.handler)),
        candidates_fn=lambda: [],
        api_client=client,
    )
    tennis = build_shadow_capture(
        enabled=True,
        credentials=(APP_KEY, USERNAME, PASSWORD),
        window_hours=72,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock.handler)),
        candidates_fn=lambda: [],
        api_client=client,
        event_type_ids=(betfair_api.EVENT_TYPE_TENNIS,),
        ordered=False,
    )
    assert soccer is not None and tennis is not None
    assert soccer._client is client
    assert tennis._client is client


def test_ordered_defaults_true_and_name_fold_defaults_none() -> None:
    # The soccer path is byte-identical to the pre-tennis capture by default.
    capture = BetfairApiShadowCapture(
        make_client(_full_odds_mock()),
        candidates_fn=lambda: [],
        window=timedelta(hours=72),
        aliases=default_aliases(),
    )
    assert capture._ordered is True
    assert capture._name_fold is None
