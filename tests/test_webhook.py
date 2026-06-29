"""WebhookSink delivery + secret hygiene (no network — httpx.MockTransport).

The webhook URL is the secret (it can embed a token). The sink must NEVER
raise (the dispatcher iterates sinks and one failure must not abort the rest)
and must never leak the URL into logs — type + status only.
"""

import logging
from collections.abc import Callable

import httpx
import pytest

from app.notifications.base import Alert
from app.notifications.webhook import WebhookSink

_SECRET_URL = "https://hooks.example.com/services/SECRET-TOKEN-abc123"


def _alert() -> Alert:
    return Alert(
        pick_id="pick-1",
        title="title",
        body="body",
        dedupe_key="key-1",
    )


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_successful_post_returns_true_and_sends_payload() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        sink = WebhookSink(_SECRET_URL, client)
        assert sink.configured
        assert await sink.send(_alert()) is True

    assert seen["url"] == _SECRET_URL
    assert b"pick-1" in seen["body"]  # type: ignore[operator]
    assert b"key-1" in seen["body"]  # type: ignore[operator]


async def test_non_2xx_handled_gracefully_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    async with _client(handler) as client:
        sink = WebhookSink(_SECRET_URL, client)
        with caplog.at_level(logging.ERROR, logger="app.notifications.webhook"):
            result = await sink.send(_alert())  # must not raise

    assert result is False
    assert "503" in caplog.text  # status surfaced for diagnosis
    assert _SECRET_URL not in caplog.text  # URL/secret never logged


async def test_transport_error_handled_gracefully_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client(handler) as client:
        sink = WebhookSink(_SECRET_URL, client)
        with caplog.at_level(logging.ERROR, logger="app.notifications.webhook"):
            result = await sink.send(_alert())  # must not raise

    assert result is False
    # The exception TYPE is logged, never the message/URL.
    assert "ConnectError" in caplog.text
    assert _SECRET_URL not in caplog.text
    assert "connection refused" not in caplog.text


async def test_unconfigured_empty_url_is_noop() -> None:
    posted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        posted = True
        return httpx.Response(200)

    async with _client(handler) as client:
        sink = WebhookSink("", client)
        assert sink.configured is False
        assert await sink.send(_alert()) is False

    assert posted is False  # never touched the network
