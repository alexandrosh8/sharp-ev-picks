"""Fail-closed global HTTP request-body size enforcement."""

from __future__ import annotations

from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES: Final = 64 * 1024
_REQUEST_TOO_LARGE_BODY: Final = b'{"detail":"request body too large"}'


class RequestBodyLimitMiddleware:
    """Buffer and cap HTTP bodies before routing or Pydantic parsing begins.

    Checking only ``Content-Length`` does not protect chunked HTTP requests.
    Pre-reading at most the configured cap also guarantees that an inner app
    cannot start a success response before an oversized later chunk arrives.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        if self._declared_body_is_too_large(scope):
            await self._send_too_large(send)
            return

        messages: list[Message] = []
        body_bytes = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            body_bytes += len(message.get("body", b""))
            if body_bytes > self._max_body_bytes:
                await self._send_too_large(send)
                return
            if not message.get("more_body", False):
                break

        next_message = 0

        async def replay_receive() -> Message:
            nonlocal next_message
            if next_message < len(messages):
                message = messages[next_message]
                next_message += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self._app(scope, replay_receive, send)

    def _declared_body_is_too_large(self, scope: Scope) -> bool:
        raw_lengths = [
            value.strip()
            for name, value in scope.get("headers", ())
            if name.lower() == b"content-length"
        ]
        if not raw_lengths:
            return False
        # ASGI servers normally reject malformed or conflicting lengths first.
        # Treat anything other than one unsigned decimal value as over-limit so
        # a non-conforming server cannot turn ambiguity into a bypass.
        if len(raw_lengths) != 1 or not raw_lengths[0].isdigit():
            return True
        try:
            return int(raw_lengths[0]) > self._max_body_bytes
        except ValueError:
            # Python intentionally refuses decimal strings with thousands of
            # digits. A header can still fit a server's byte limit while
            # crossing that conversion guard, so classify it as oversized
            # instead of turning a hostile declaration into a 500 response.
            return True

    @staticmethod
    async def _send_too_large(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_REQUEST_TOO_LARGE_BODY)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _REQUEST_TOO_LARGE_BODY})
