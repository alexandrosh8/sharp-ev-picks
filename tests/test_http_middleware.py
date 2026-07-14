"""HTTP compression, request-size, and browser-security middleware contracts."""

from collections import deque
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import Message, Scope

from app.api.request_limits import MAX_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware
from app.api.security_headers import DEFAULT_SECURITY_HEADERS, SecurityHeadersMiddleware
from app.main import create_app


def _middleware_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/large")
    async def large() -> PlainTextResponse:
        return PlainTextResponse("x" * 8192, headers={"X-Frame-Options": "SAMEORIGIN"})

    @app.get("/revalidate")
    async def revalidate() -> PlainTextResponse:
        return PlainTextResponse("manifest", headers={"Cache-Control": "no-cache"})

    @app.get("/no-store")
    async def no_store() -> PlainTextResponse:
        return PlainTextResponse("dashboard", headers={"Cache-Control": "no-store"})

    return app


def _body_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)

    @app.post("/body")
    async def body(payload: dict[str, str]) -> dict[str, str]:
        return payload

    return app


def test_large_responses_are_compressed_and_hardened() -> None:
    with TestClient(_middleware_app()) as client:
        response = client.get("/large", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.text == "x" * 8192
    assert response.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["vary"]
    assert int(response.headers["content-length"]) < 8192
    for name, expected in DEFAULT_SECURITY_HEADERS.items():
        assert response.headers[name] == expected
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"


def test_explicit_revalidation_cache_policy_is_preserved() -> None:
    with TestClient(_middleware_app()) as client:
        response = client.get("/revalidate")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "pragma" not in response.headers


def test_bare_no_store_policy_is_made_private_and_proxy_safe() -> None:
    with TestClient(_middleware_app()) as client:
        response = client.get("/no-store")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"


def test_application_installs_all_http_middlewares() -> None:
    app = create_app()
    middleware_classes = {entry.cls for entry in app.user_middleware}
    assert GZipMiddleware in middleware_classes
    assert RequestBodyLimitMiddleware in middleware_classes
    assert SecurityHeadersMiddleware in middleware_classes


def test_declared_oversized_body_is_rejected_before_json_parsing() -> None:
    payload = '{"value":"' + ("x" * MAX_REQUEST_BODY_BYTES) + '"}'
    with TestClient(_middleware_app()) as client:
        response = client.post("/large", content=payload)

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}
    assert response.headers["x-content-type-options"] == "nosniff"


def test_body_at_exact_limit_is_accepted() -> None:
    prefix = b'{"value":"'
    suffix = b'"}'
    payload = prefix + (b"x" * (MAX_REQUEST_BODY_BYTES - len(prefix) - len(suffix))) + suffix
    with TestClient(_body_app()) as client:
        response = client.post(
            "/body",
            content=payload,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_chunked_body_is_capped_without_content_length() -> None:
    downstream_called = False

    async def downstream(_scope: Scope, _receive: Any, _send: Any) -> None:
        nonlocal downstream_called
        downstream_called = True

    request_messages: deque[Message] = deque(
        [
            {
                "type": "http.request",
                "body": b"x" * MAX_REQUEST_BODY_BYTES,
                "more_body": True,
            },
            {"type": "http.request", "body": b"x", "more_body": False},
        ]
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return request_messages.popleft()

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/body",
        "raw_path": b"/body",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"transfer-encoding", b"chunked")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
        "state": {},
    }
    middleware = RequestBodyLimitMiddleware(downstream)
    await middleware(scope, receive, send)

    assert downstream_called is False
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_length_headers",
    [
        [(b"content-length", b"not-a-number")],
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"content-length", b"9" * 5_000)],
    ],
)
async def test_ambiguous_or_unparseable_content_length_fails_closed(
    content_length_headers: list[tuple[bytes, bytes]],
) -> None:
    downstream_called = False

    async def downstream(_scope: Scope, _receive: Any, _send: Any) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def receive() -> Message:
        raise AssertionError("invalid declared length must reject before reading the body")

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/body",
        "raw_path": b"/body",
        "query_string": b"",
        "root_path": "",
        "headers": content_length_headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
        "state": {},
    }
    await RequestBodyLimitMiddleware(downstream)(scope, receive, send)

    assert downstream_called is False
    assert sent[0]["status"] == 413
