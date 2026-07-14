"""Cycle-level orchestration for the OddsPortal JSON path — speed + reliability.

Pins the sourced 2026-06-24 "now"-tier patterns the JSON cut-over needs to be
GENUINELY fast + reliable:

  F3  bounded semaphore + gather(return_exceptions=True) — one bad match is one
      filtered gap, never a cancelled cycle.
  F5  process-lifetime cache for the static AES decrypt key (double-checked).
  R1  transient vs permanent classification by the libcurl int code (35 SSL-
      connect transient, 60 cert-verify permanent — opposite sides).
  R2  tenacity retry on transient exceptions AND transient HTTP statuses (429/
      5xx), with the backoff sleep OUTSIDE the semaphore slot.
  R3  fail-closed completeness gate (row collapse / missing market / gate error).

No network: scrapes are injected coroutines; failures are a fake curl error that
carries a ``.code`` exactly like curl_cffi's RequestException.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from app.ingestion.oddsportal_json_session import (
    DEFAULT_CONCURRENCY,
    PINNED_IMPERSONATE,
    _reset_key_cache_for_tests,
    cached_decrypt_key,
    classify_curl_error,
    retry_after_seconds,
    run_cycle,
    status_of,
)
from app.schemas.base import Market
from app.schemas.odds import OddsSnapshotIn

NOW = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


class FakeCurlError(Exception):
    """Stand-in for curl_cffi RequestException/CurlError: carries a libcurl int
    ``.code`` exactly like the real exceptions (R1 classifies by that int)."""

    def __init__(self, code: int, msg: str = "boom") -> None:
        super().__init__(msg)
        self.code = code


def _row(
    market: Market = Market.H2H, *, selection: str = "Home", detail: str = "1x2"
) -> OddsSnapshotIn:
    return OddsSnapshotIn(
        event_id="https://www.oddsportal.com/football/x/a-b-AbCdEf12/",
        bookmaker="bet365",
        market=market,
        selection=selection,
        decimal_odds=2.0,
        captured_at=NOW,
        ingested_at=NOW,
        market_detail=detail,
    )


# --- R1: error classification by libcurl int code ---------------------------


def test_classify_transient_codes() -> None:
    for code in (28, 7, 56, 55, 52, 18, 35):
        assert classify_curl_error(FakeCurlError(code)) == "transient", code


def test_classify_permanent_codes() -> None:
    for code in (6, 60, 77, 3, 1, 5):
        assert classify_curl_error(FakeCurlError(code)) == "permanent", code


def test_ssl_connect_35_transient_but_cert_verify_60_permanent() -> None:
    """The one researchers flagged: 35 SSL_CONNECT is transient (handshake blip),
    60 PEER_FAILED_VERIFICATION is permanent (CA/cert config). Opposite sides."""
    assert classify_curl_error(FakeCurlError(35)) == "transient"
    assert classify_curl_error(FakeCurlError(60)) == "permanent"


def test_classify_unknown_when_no_code_or_unlisted() -> None:
    assert classify_curl_error(RuntimeError("no code attr")) == "unknown"
    assert classify_curl_error(FakeCurlError(9999)) == "unknown"


# --- R2: retry on transient exception, NOT on permanent ---------------------


async def test_retry_recovers_after_transient_then_success() -> None:
    """A transient libcurl failure is retried; a subsequent success is returned —
    the match is NOT lost to one blip."""
    calls = {"n": 0}

    async def scrape(url: str) -> list[OddsSnapshotIn]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise FakeCurlError(28)  # OPERATION_TIMEDOUT (transient)
        return [_row()]

    outcome = await run_cycle(["u1"], scrape, markets=["1x2"])
    assert calls["n"] == 2  # retried once, then succeeded
    assert len(outcome.snapshots) == 1
    assert outcome.transient_failures == 0  # recovered, not counted as a failure


async def test_permanent_failure_not_retried_and_filtered() -> None:
    """A permanent failure is NOT retried (one call) and becomes ONE filtered gap
    — never crashes the cycle, never recovers the other matches' work."""
    calls = {"n": 0}

    async def scrape(url: str) -> list[OddsSnapshotIn]:
        calls["n"] += 1
        raise FakeCurlError(60)  # cert verify (permanent)

    outcome = await run_cycle(["u1"], scrape, markets=["1x2"])
    assert calls["n"] == 1  # not retried
    assert outcome.snapshots == []
    assert outcome.permanent_failures == 1


async def test_transient_http_status_result_is_retried() -> None:
    """curl_cffi does not raise on 429/5xx — the response comes back with that
    status. A scrape that returns a 429-status sentinel object is retried (R2)."""

    class Resp:
        def __init__(self, status: int) -> None:
            self.status_code = status

    seq: list[Any] = [Resp(429), [_row()]]  # first a 429-status object, then rows

    async def scrape(url: str) -> Any:
        return seq.pop(0)

    outcome = await run_cycle(["u1"], scrape, markets=["1x2"])
    # The 429 result was retried; the second attempt's rows landed.
    assert len(outcome.snapshots) == 1


# --- F3: fan-out resilience — one bad match is one gap, siblings finish ------


async def test_one_bad_match_does_not_cancel_siblings() -> None:
    """gather(return_exceptions=True) (NOT TaskGroup): a permanent failure on one
    URL must not cancel the others — their rows still land."""

    async def scrape(url: str) -> list[OddsSnapshotIn]:
        if url == "bad":
            raise FakeCurlError(60)  # permanent — would CANCEL siblings under TaskGroup
        return [_row(selection=url)]

    urls = ["good1", "bad", "good2", "good3"]
    outcome = await run_cycle(urls, scrape, markets=["1x2"])
    sels = {s.selection for s in outcome.snapshots}
    assert sels == {"good1", "good2", "good3"}  # all three good ones survived
    assert outcome.permanent_failures == 1
    assert outcome.matches_with_odds == 3


async def test_semaphore_bounds_concurrency() -> None:
    """The bounded semaphore caps simultaneous in-flight scrapes at N."""
    live = {"now": 0, "max": 0}

    async def scrape(url: str) -> list[OddsSnapshotIn]:
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1
        return [_row(selection=url)]

    urls = [f"u{i}" for i in range(20)]
    await run_cycle(urls, scrape, markets=["1x2"], concurrency=3)
    assert live["max"] <= 3, f"concurrency exceeded the cap: peak={live['max']}"


async def test_match_url_ceiling_marks_cycle_incomplete_without_scheduling() -> None:
    calls = 0

    async def scrape(url: str) -> list[OddsSnapshotIn]:
        nonlocal calls
        calls += 1
        return [_row(selection=url)]

    outcome = await run_cycle(
        ["u1", "u2", "u3"],
        scrape,
        markets=["1x2"],
        max_match_urls=2,
    )
    assert not outcome.complete
    assert "URL ceiling" in outcome.reason
    assert calls == 0


async def test_per_match_snapshot_ceiling_marks_cycle_incomplete() -> None:
    async def scrape(url: str) -> list[OddsSnapshotIn]:
        return [_row(selection=f"{url}-1"), _row(selection=f"{url}-2")]

    outcome = await run_cycle(
        ["u1"],
        scrape,
        markets=["1x2"],
        max_snapshots_per_match=1,
    )
    assert not outcome.complete
    assert "one match exceeded snapshot ceiling" in outcome.reason
    assert outcome.snapshots == []


async def test_cycle_snapshot_ceiling_stops_scheduling_and_marks_incomplete() -> None:
    async def scrape(url: str) -> list[OddsSnapshotIn]:
        return [_row(selection=url)]

    outcome = await run_cycle(
        ["u1", "u2"],
        scrape,
        markets=["1x2"],
        concurrency=1,
        max_snapshots_per_cycle=1,
    )
    assert not outcome.complete
    assert "cycle snapshot ceiling" in outcome.reason
    assert len(outcome.snapshots) == 1


# --- R3: fail-closed completeness gate --------------------------------------


async def test_complete_when_rows_healthy() -> None:
    async def scrape(url: str) -> list[OddsSnapshotIn]:
        return [_row()]

    outcome = await run_cycle(["u1", "u2"], scrape, markets=["1x2"], prev_cycle_rows=2)
    assert outcome.complete
    assert outcome.reason == ""


async def test_incomplete_on_row_collapse_vs_prev_cycle() -> None:
    """Rows below 0.8x the previous cycle -> INCOMPLETE (silent partial-empty)."""

    async def scrape(url: str) -> list[OddsSnapshotIn]:
        return [_row()] if url == "u1" else []  # only 1 of 10 matches has odds

    urls = [f"u{i}" for i in range(10)]
    outcome = await run_cycle(urls, scrape, markets=["1x2"], prev_cycle_rows=100)
    assert not outcome.complete
    assert "row collapse" in outcome.reason


async def test_incomplete_on_missing_expected_market() -> None:
    """An expected market with 0 rows while another market has rows -> INCOMPLETE
    (one market's feed key/layout broke)."""

    async def scrape(url: str) -> list[OddsSnapshotIn]:
        return [_row(detail="1x2")]  # btts never appears

    outcome = await run_cycle(["u1"], scrape, markets=["1x2", "btts"])
    assert not outcome.complete
    assert "btts" in outcome.reason


async def test_empty_slate_is_complete_not_failed() -> None:
    """Zero listed matches is a separate '0 matches' signal, NOT an incomplete
    cycle (nothing to be partial about)."""

    async def scrape(url: str) -> list[OddsSnapshotIn]:  # pragma: no cover - never called
        return [_row()]

    outcome = await run_cycle([], scrape, markets=["1x2"])
    assert outcome.complete
    assert outcome.matches_total == 0


async def test_listed_matches_with_zero_rows_fail_closed() -> None:
    """A listing with matches but zero observations is not a successful cycle."""

    async def scrape(url: str) -> list[OddsSnapshotIn]:
        return []

    outcome = await run_cycle(["u1"], scrape, markets=["1x2", "btts"])
    assert not outcome.complete
    assert "0 rows" in outcome.reason


async def test_any_listed_match_fetch_failure_makes_cycle_incomplete() -> None:
    async def scrape(url: str) -> list[OddsSnapshotIn]:
        if url == "bad":
            raise FakeCurlError(60)
        return [_row()]

    outcome = await run_cycle(["good", "bad"], scrape, markets=["1x2"])
    assert not outcome.complete
    assert "fetch" in outcome.reason


async def test_wildcard_family_covered_by_its_expanded_line_keys() -> None:
    """A wildcard market family (over_under_games / asian_handicap_games) emits
    rows under EXPANDED line keys (over_under_games_171_5, ...), never the bare
    family key. The completeness gate must treat the family as COVERED when any
    of its lines produced rows — not flag it as a wholly-missing market (the
    false-INCOMPLETE regression seen live on basketball, 2026-06-25)."""

    async def scrape(url: str) -> list[OddsSnapshotIn]:
        return [
            _row(detail="home_away"),
            _row(detail="over_under_games_171_5"),
            _row(detail="over_under_games_172_5"),
            _row(detail="asian_handicap_games_-3_5"),
        ]

    outcome = await run_cycle(
        ["u1"],
        scrape,
        markets=["home_away", "over_under_games", "asian_handicap_games"],
    )
    assert outcome.complete, outcome.reason
    assert outcome.reason == ""


# --- F1 / F5 knobs ----------------------------------------------------------


def test_impersonate_is_pinned_not_bare_chrome() -> None:
    assert PINNED_IMPERSONATE == "chrome146"
    assert PINNED_IMPERSONATE != "chrome"


def test_default_concurrency_is_eight() -> None:
    assert DEFAULT_CONCURRENCY == 8


async def test_cached_decrypt_key_derives_once_per_process() -> None:
    """F5: the static AES key is derived once and cached for the process."""
    _reset_key_cache_for_tests()
    k1 = await cached_decrypt_key()
    k2 = await cached_decrypt_key()
    assert k1 == k2
    assert isinstance(k1, bytes)
    assert len(k1) == 32  # AES-256
    # It equals the module's own derive (cache is correct, not a placeholder).
    from app.ingestion.oddsportal_json import _derive_key

    assert k1 == _derive_key()


def test_retry_after_seconds_parses_and_caps() -> None:
    class R:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    assert retry_after_seconds(R({"Retry-After": "5"})) == 5.0
    assert retry_after_seconds(R({"retry-after": "1000"}), cap=30.0) == 30.0
    assert retry_after_seconds(R({"Retry-After": "NaN"})) is None
    assert retry_after_seconds(R({"Retry-After": "inf"})) is None
    assert retry_after_seconds(R({})) is None
    assert retry_after_seconds(R({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None


def test_transient_status_carries_retry_after_into_wait_policy() -> None:
    from tenacity import RetryCallState, Retrying

    from app.ingestion.oddsportal_json_session import (
        TransientHTTPStatusError,
        _wait_retry_after_or_backoff,
    )

    state = RetryCallState(Retrying(), None, (), {})
    error = TransientHTTPStatusError(429, {"Retry-After": "7"})
    state.set_exception((TransientHTTPStatusError, error, None))
    assert _wait_retry_after_or_backoff(state) == 7.0


def test_status_of_reads_status_code() -> None:
    class R:
        status_code = 503

    assert status_of(R()) == 503
    assert status_of(object()) is None


async def test_transient_http_status_actually_retries() -> None:
    """Audit 2026-07-10: the documented R2 retry was dead code — the retried
    coroutine returns a snapshot list, never a response, so retry_if_result
    could never fire and a transient 429 became a permanent one-cycle gap.
    The scrape layer now raises TransientHTTPStatusError and tenacity retries."""
    import asyncio

    from app.ingestion.oddsportal_json_session import (
        TransientHTTPStatusError,
        _scrape_one_with_retry,
    )

    calls = {"n": 0}

    async def flaky_scrape(url: str) -> list[str]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientHTTPStatusError(429)
        return ["row"]

    out = await _scrape_one_with_retry(
        "https://example.test/m", cast("Any", flaky_scrape), asyncio.Semaphore(1)
    )
    assert out == ["row"]
    assert calls["n"] == 3


async def test_permanent_status_still_a_gap_not_retried() -> None:
    """A permanent 4xx stays a single-attempt gap (no hammering)."""
    import asyncio

    from app.ingestion.oddsportal_json_session import _scrape_one_with_retry

    calls = {"n": 0}

    async def gap_scrape(url: str) -> list[str]:
        calls["n"] += 1
        return []  # permanent 404 path: scrape returns a gap, never raises

    out = await _scrape_one_with_retry(
        "https://example.test/m", cast("Any", gap_scrape), asyncio.Semaphore(1)
    )
    assert out == []
    assert calls["n"] == 1
